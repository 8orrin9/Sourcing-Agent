"""STEP 0 소싱 스펙 파싱 — 자연어 요청 + 사양서(선택) → 구조화 스펙.

담당자가 쓴 요청문과 첨부 사양서를 LLM이 읽어, 데모 STEP 0의 6개 그룹
(공정·설비·인증/환경·지역·규모·제외)으로 항목을 제안한다. 항목마다 필수/우대 제안과
사양서 근거(source)를 붙이고, 공정 항목에는 **소스별 검색어**를 함께 만든다.

소스마다 쓰는 언어가 다르기 때문이다.
- 공장등록(팩토리온) 생산품란: 짧은 품목 명사 ("하드코팅필름", "아크릴판")
- 특허(KIPRIS): 기술 용어 + IPC 분류
- 웹: 공정 + 제조사/공장 의도어, 로케일별 언어

검색어를 항목 단위로 붙여 두면, 담당자가 화면에서 필수/우대를 바꿔도 LLM을 다시
부르지 않고 검색 계획(search_plan.build_search_plan)만 결정적으로 다시 만들 수 있다.

존 분리: item(내부 품목코드)은 결과에 보관만 하고, 검색어에서는 search_plan이
결정적으로 한 번 더 걸러낸다(LLM 지시만 믿지 않는다).
"""

from __future__ import annotations

from pathlib import Path

from ..core.documents import read_document
from .client import complete_json

GROUPS = ("processes", "equipment", "certs", "region", "scale", "exclude")
LOCALES = ("KR", "EN", "ZH", "VI")

# 사양서 본문을 LLM에 넘길 최대 글자수 — 토큰·비용 상한
MAX_DOC_CHARS = 20000

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_NULL_STR = {"type": ["string", "null"]}
_NULL_NUM = {"type": ["number", "null"]}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "group": {"type": "string", "enum": list(GROUPS)},
        "label": {"type": "string"},
        "required": {"type": "boolean"},
        "source": {"type": "string"},
        # 공정 항목만 채운다(그 외 그룹은 빈 값)
        "code": _NULL_STR,
        "min_evidence": {"type": ["string", "null"], "enum": ["L1", "L2", "L3", None]},
        "terms": {
            "type": "object",
            "properties": {
                "product": _STR_LIST,
                "patent": _STR_LIST,
                "ipc": _STR_LIST,
                "web": {
                    "type": "object",
                    "properties": {loc: _STR_LIST for loc in LOCALES},
                    "required": list(LOCALES),
                    "additionalProperties": False,
                },
            },
            "required": ["product", "patent", "ipc", "web"],
            "additionalProperties": False,
        },
        # 지역·규모·제외 항목의 기계 판정 규칙(해당 없으면 null)
        "rule": {
            "type": "object",
            "properties": {
                "countries": {"type": ["array", "null"], "items": {"type": "string"}},
                "near": _NULL_STR,
                "km": _NULL_NUM,
                "min_employees": _NULL_NUM,
                "min_revenue_krw": _NULL_NUM,
                "exclude": {"type": ["string", "null"], "enum": ["trader", "incumbent", None]},
            },
            "required": ["countries", "near", "km", "min_employees", "min_revenue_krw", "exclude"],
            "additionalProperties": False,
        },
    },
    "required": ["group", "label", "required", "source", "code", "min_evidence", "terms", "rule"],
    "additionalProperties": False,
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "target_item": {"type": "string"},
        "item_terms": _STR_LIST,
        "items": {"type": "array", "items": _ITEM_SCHEMA},
        "mapping_notes": _STR_LIST,
    },
    "required": ["target_item", "item_terms", "items", "mapping_notes"],
    "additionalProperties": False,
}

_SYSTEM = (
    "너는 제조업 구매(소싱) 스펙 분석가다. 구매 담당자의 요청문과 자재 사양서를 읽고 "
    "대체 공급사 탐색용 소싱 스펙을 만든다.\n"
    "그룹: processes(필요 공정·소재 가공), equipment(필요 설비·환경 설비), "
    "certs(인증·환경 조건, 예: ISO 9001, 클린룸 등급), region(공급 지역), "
    "scale(규모: 종업원·매출), exclude(제외 대상: 유통사, 현 협력사).\n"
    "규칙:\n"
    "1) 사양 수치를 공정으로 번역하라(예: '연필경도 3H' → 하드코팅, '접촉각 110°' → AF 코팅). "
    "source에 근거(사양서 항 번호 또는 요청문 구절)를 적고, 번역했다면 mapping_notes에 "
    "'원문 → 공정' 형식으로 남겨라.\n"
    "2) required: '필수', '~할 것', '반드시' → true. '우선', '검토', '가능하면', '우대' → false. "
    "단, '국내 우선'처럼 기본 지역은 true로, '해외 포함 검토'는 false로 둔다.\n"
    "3) processes 항목은 code(영문 대문자 약어, 예: HC, AF, LASER)와 min_evidence(L1~L3)를 채우고 "
    "terms를 만든다. 그 외 그룹은 code·min_evidence=null, terms의 배열은 모두 빈 배열.\n"
    "   - terms는 품목을 대표하는 '핵심 공정'에만 만든다. 레이저 커팅·절단·타발·포장처럼 "
    "어느 업종에나 있는 범용 가공은 검색하면 무관한 업체가 쏟아지므로 terms를 모두 빈 배열로 둔다"
    "(그래도 항목 자체는 넣는다 — 상세 수집 후 검증한다).\n"
    "   - terms.product: 공장등록 생산품란에 실제로 적힐 법한 짧은 한국어 품목 명사 3~6개 "
    "(예: '하드코팅필름', '하드코팅시트', '코팅필름'). 문장 금지.\n"
    "   - terms.patent: 특허 검색용 기술 용어 2~5개(한·영).\n"
    "   - terms.ipc: 관련 IPC 서브클래스 또는 메인그룹 1~3개(예: 'C09D', 'C08J 7/04').\n"
    "   - terms.web: 로케일별(KR 한국어, EN 영어, ZH 중국어 간체, VI 베트남어) 검색어 1~2개. "
    "공정·소재 + '제조/공장/manufacturer' 같은 의도어를 넣는다.\n"
    "4) item_terms: 품목·소재를 가리키는 일반 명사 2~4개(공장등록 생산품 검색에 같이 쓴다).\n"
    "5) region.rule.countries는 ISO 2자리 국가코드(KR, VN, CN 등). 'OO 사업장 Nkm 이내'는 "
    "near에 시도·시군구, km에 숫자. scale은 min_employees 또는 min_revenue_krw(원 단위). "
    "exclude는 trader 또는 incumbent. 해당 없는 rule 필드는 null.\n"
    "6) 금지: 내부 품목코드, 단가, 현 협력사 회사명을 terms에 넣지 말 것. 근거 없는 항목을 지어내지 말 것.\n"
    "7) 사양서·요청문에 지역·제외 조건이 없으면 기본으로 region '국내'(필수), "
    "exclude '무역·유통사'(필수)를 넣는다."
)


def build_user_prompt(request_text: str, document_text: str | None = None) -> str:
    parts = [f"[요청문]\n{request_text.strip()}"]
    if document_text:
        parts.append(f"[사양서]\n{document_text[:MAX_DOC_CHARS]}")
    else:
        parts.append("[사양서]\n(첨부 없음 — 요청문만으로 스펙을 만든다)")
    return "\n\n".join(parts)


def parse_spec(
    request_text: str,
    *,
    document: bytes | str | Path | None = None,
    document_name: str | None = None,
    request_id: str | None = None,
    item: str | None = None,
) -> dict:
    """요청문(+사양서)을 구조화 스펙으로 파싱한다.

    반환 형식은 data/synthetic/parsed_spec_pmma.json 과 같다. 항목에 id(P1, E1 …)를 붙인다.
    item(내부 품목코드)은 결과에 보관만 하고 LLM 프롬프트에는 넣지 않는다.
    """
    doc_text = read_document(document, filename=document_name) if document is not None else None
    out = complete_json(
        system=_SYSTEM,
        user=build_user_prompt(request_text, doc_text),
        schema=_SCHEMA,
        schema_name="sourcing_spec",
    )
    return finalize(out, request_id=request_id, item=item)


_ID_PREFIX = {"processes": "P", "equipment": "E", "certs": "C",
              "region": "G", "scale": "S", "exclude": "X"}


def finalize(raw: dict, *, request_id: str | None = None, item: str | None = None) -> dict:
    """LLM 출력에 항목 id를 붙이고 그룹 순서로 정렬(결정적 후처리)."""
    counters = {g: 0 for g in GROUPS}
    items = []
    for g in GROUPS:
        for it in (x for x in raw["items"] if x["group"] == g):
            counters[g] += 1
            items.append({"id": f"{_ID_PREFIX[g]}{counters[g]}", **it})
    return {
        "request_id": request_id,
        "item": item,
        "target_item": raw["target_item"],
        "item_terms": raw["item_terms"],
        "items": items,
        "mapping_notes": raw["mapping_notes"],
    }


def to_ui_spec(spec: dict) -> dict:
    """화면 JSON(그룹별 required/preferred 문자열 목록)으로 변환."""
    out: dict = {"request_id": spec.get("request_id"), "item": spec.get("item")}
    for g in GROUPS:
        its = [x for x in spec["items"] if x["group"] == g]
        out[g] = {"required": [x["label"] for x in its if x["required"]],
                  "preferred": [x["label"] for x in its if not x["required"]]}
    return out


def apply_selection(spec: dict, ui_spec: dict) -> dict:
    """담당자가 화면에서 바꾼 필수/우대(ui_spec)를 파싱 스펙에 반영한다.

    화면 목록에서 빠진 항목은 삭제된 것으로 본다. 라벨로 대조한다.
    """
    out = {**spec, "items": []}
    for it in spec["items"]:
        sel = ui_spec.get(it["group"], {})
        if it["label"] in sel.get("required", []):
            out["items"].append({**it, "required": True})
        elif it["label"] in sel.get("preferred", []):
            out["items"].append({**it, "required": False})
    return out
