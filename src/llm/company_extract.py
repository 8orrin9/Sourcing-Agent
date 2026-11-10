"""웹 검색 결과 → 회사 추출 / 공식 홈페이지 판별 (LLM).

이전에는 검색결과 제목을 구분자로 잘라 앞부분을 회사명으로 썼다. 실제 결과는 특허·학회·
판매 페이지가 대부분이라 '회사명'의 대부분이 문서 제목이었고, 노이즈는 팩토리온·KIPRIS
호출을 다 거친 뒤 분류 단계에서야 걸러졌다. 이제는 검색 직후 LLM이 결과 묶음을 읽고
실제 기업만 뽑는다(문서·마켓·뉴스·학회는 이 자리에서 버린다).

pick_homepage는 공장등록에 홈페이지가 없는 후보의 공식 사이트를 웹 검색 결과에서 고른다
(상세 수집 STEP 2의 입구).
"""

from __future__ import annotations

from ..schema import CompanyKind, Provenance, SourceClass, SupplierRecord
from .client import complete_json

# LLM 1회에 넣는 검색결과 수 — 맥락 유지와 비용의 균형
BATCH = 20

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "country": {"type": "string"},
                    "kind": {"type": "string", "enum": ["manufacturer", "trader", "unknown"]},
                    "official_site_index": {"type": ["integer", "null"]},
                    "source_indexes": {"type": "array", "items": {"type": "integer"}},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "country", "kind", "official_site_index",
                             "source_indexes", "evidence"],
                "additionalProperties": False,
            },
        },
        "dropped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["index", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["companies", "dropped"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = (
    "너는 B2B 소싱 리서처다. 웹 검색 결과 목록(번호·제목·URL·스니펫)에서 '요청 품목을 "
    "공급할 수 있는 실제 기업'만 뽑는다.\n"
    "규칙:\n"
    "1) 회사명은 결과에 실제로 드러난 법인명만 쓴다. 제목·문장을 회사명으로 쓰지 말 것. "
    "회사명을 확인할 수 없으면 뽑지 말 것.\n"
    "2) 특허 문서, 논문·학회, 뉴스, 블로그, 위키, 마켓플레이스의 상품 목록 자체는 기업이 아니다 "
    "→ dropped에 index와 사유. 단, 마켓·전시회 페이지 본문에 판매 기업명이 분명히 있으면 그 기업을 뽑는다.\n"
    "3) kind: 공장·생산·제조 근거가 있으면 manufacturer, 유통·무역·대리점이면 trader, 모르면 unknown.\n"
    "4) official_site_index: 그 기업의 자체 도메인 결과 번호(마켓·디렉터리 URL은 아님). 없으면 null.\n"
    "5) country: ISO 2자리(KR, VN, CN …), 모르면 빈 문자열. evidence: 판단 근거 한 문장.\n"
    "6) 같은 기업이 여러 결과에 나오면 한 번만 뽑고 source_indexes에 모두 적는다."
)

_HOMEPAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["index", "reason"],
    "additionalProperties": False,
}

_HOMEPAGE_SYSTEM = (
    "너는 기업 공식 홈페이지 판별기다. 회사명·소재지와 웹 검색 결과 목록이 주어진다. "
    "그 회사의 자체 공식 홈페이지인 결과 번호를 고른다. 마켓플레이스, 디렉터리, 채용 사이트, "
    "뉴스, 블로그, 이름만 비슷한 다른 회사는 공식 홈페이지가 아니다. 확신이 없으면 index=null."
)


def _format(results: list[dict], offset: int = 0) -> str:
    return "\n".join(
        f"[{offset + i}] {r.get('title', '')}\n    URL: {r.get('link', '')}\n    {r.get('snippet', '')}"
        for i, r in enumerate(results)
    )


def extract_companies(results: list[dict], *, target: str = "") -> tuple[list[SupplierRecord], list[dict]]:
    """검색결과 → (기업 레코드, 버린 결과[{index, reason, title}]).

    results: web_search.WebSearchExtractor.search()의 원시 결과(title·link·snippet·locale·aggregator).
    """
    records: list[SupplierRecord] = []
    dropped: list[dict] = []
    for start in range(0, len(results), BATCH):
        chunk = results[start:start + BATCH]
        user = f"[요청 품목] {target or '(미지정)'}\n\n[검색 결과]\n{_format(chunk, start)}"
        out = complete_json(system=_EXTRACT_SYSTEM, user=user, schema=_EXTRACT_SCHEMA,
                            schema_name="company_extract")
        for c in out["companies"]:
            if c["name"].strip():
                records.append(_to_record(c, results))
        for d in out["dropped"]:
            if 0 <= d["index"] < len(results):
                dropped.append({**d, "title": results[d["index"]].get("title", "")})
    return records, dropped


def _to_record(c: dict, results: list[dict]) -> SupplierRecord:
    idxs = [i for i in c["source_indexes"] if 0 <= i < len(results)]
    site = c["official_site_index"]
    own = results[site] if site is not None and 0 <= site < len(results) else None
    if own and own.get("aggregator"):
        own = None
    provs = [
        Provenance(
            source_id="web_search",
            source_class=SourceClass.SPREAD,
            extractor="llm",
            url=results[i]["link"],
            verified=False,  # 웹은 미검증 — 국내 기업은 공장등록 대조로 확정
            note=f"{results[i].get('locale', '')} 웹검색 · AI 회사 추출".strip(),
        )
        for i in idxs
    ]
    return SupplierRecord(
        name=c["name"].strip(),
        country=c["country"] or None,
        kind=CompanyKind(c["kind"]),
        homepage=own["link"] if own else None,
        domain=own["domain"] if own else None,
        provenances=provs,
        notes=[c["evidence"]] if c["evidence"] else [],
    )


def pick_homepage(name: str, region: str | None, results: list[dict]) -> tuple[str | None, str]:
    """회사의 공식 홈페이지 URL을 검색결과에서 고른다 → (url 또는 None, 사유)."""
    if not results:
        return None, "검색 결과 없음"
    user = f"[회사] {name} / 소재지: {region or '미상'}\n\n[검색 결과]\n{_format(results)}"
    out = complete_json(system=_HOMEPAGE_SYSTEM, user=user, schema=_HOMEPAGE_SCHEMA,
                        schema_name="homepage_pick")
    i = out["index"]
    if i is None or not (0 <= i < len(results)) or results[i].get("aggregator"):
        return None, out["reason"]
    return results[i]["link"], out["reason"]
