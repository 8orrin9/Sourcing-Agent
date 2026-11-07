"""파이프라인 — 롱리스트 → 앵커 흡착 → LLM 분류 → 스코어링 → 숏리스트.

데모의 외부 수집 영역을 하나의 흐름으로 엮는다. 각 단계는 독립 함수라 따로 테스트·
교체할 수 있고, run()이 체이닝한다.

```
[discover]      확산 수집(웹검색·디렉터리·LLM폴백) → 회사명·URL 후보 롱리스트
     v
[anchor_merge]  팩토리온 앵커 조회 → 확산 후보를 앵커에 흡착(주소·공장 검증)
     v
[classify]      LLM: 제조사 여부 + 공정 역량(HC/AF)·증거수준
     v
[score]         규칙 기반 스펙 적합도 점수(설명 가능) + 게이트(유통사·무역량 0 탈락)
     v
[shortlist]     점수순 상위 N → 결과 JSON
```

존 분리: discover 쿼리·RFI에 내부 단가·품목코드·현 협력사명을 넣지 않는다.
스펙의 search_queries(공정·소재 키워드만)와 required 공정만 외부로 나간다.

오프라인/데모 안정화: run(spec, longlist=...)로 수집 결과를 주입하면 discover를
건너뛴다(픽스처·저장 스냅샷 재생). live=True일 때만 실제 API/크롤을 돈다.
"""

from __future__ import annotations

import json
from pathlib import Path

from .extractors.registry import registry
from .extractors.structured import FactorionExtractor
from .llm.classify import classify_supplier
from .llm.merge import absorb_candidates, dedupe_factories
from .llm.score import score_supplier
from .schema import SupplierRecord

SPEC_PATH = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "sourcing_spec.json"


def load_spec(path: Path | None = None) -> dict:
    return json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))


# --- 1) 발견(확산) ---------------------------------------------------------
def discover(spec: dict, *, per_query: int = 10) -> list[SupplierRecord]:
    """스펙의 다국어 검색어로 확산 수집. 웹검색 추출기(2계층)를 로케일별로 돈다.

    디렉터리(config)·LLM폴백(4계층)은 라이브 URL이 필요해 데모에선 longlist 주입으로
    대체한다(안티봇 사이트는 저장 스냅샷 재생). 여기선 웹검색만 라이브.
    """
    web = registry.for_source("web_search")[0]
    out: list[SupplierRecord] = []
    for locale, queries in spec.get("search_queries", {}).items():
        if locale.startswith("_"):
            continue
        for q in queries:
            try:
                out.extend(web.extract({"q": q, "locale": locale, "num": per_query}))
            except Exception as e:  # 한 쿼리 실패가 전체를 막지 않게
                print(f"[discover] {locale} '{q}' 실패: {e}")
    return out


# --- 2) 앵커 흡착 ----------------------------------------------------------
def anchor_merge(
    candidates: list[SupplierRecord],
    *,
    anchor: FactorionExtractor | None = None,
    min_confidence: str = "medium",
) -> tuple[list[SupplierRecord], list[SupplierRecord]]:
    """확산 후보의 회사명으로 팩토리온을 조회해 앵커를 만들고, 후보를 흡착한다.

    (검증된 앵커들, 흡착 못한 잔여 확산 후보) 반환. 잔여도 숏리스트 후보로 남긴다
    (앵커에 없는 해외 업체 등). 흡착 판정은 LLM(merge.absorb_candidates)이 수행.
    """
    anchor = anchor or FactorionExtractor()
    names = sorted({c.name for c in candidates if c.name and c.name != "(미상)"})
    anchors: list[SupplierRecord] = []
    for name, recs in anchor.verify(names).items():
        anchors.extend(recs)
    # 팩토리온은 한 기업의 여러 공장을 각각 등록 → 회사명 기준 1건으로 접는다
    anchors = dedupe_factories(anchors)
    merged, leftover = absorb_candidates(anchors, candidates, min_confidence=min_confidence)
    return merged, leftover


# --- 3) LLM 분류 -----------------------------------------------------------
def classify_all(records: list[SupplierRecord], *, processes: list[str]) -> list[SupplierRecord]:
    """각 레코드를 LLM으로 분류(제조사 여부·공정 역량)."""
    return [classify_supplier(r, processes=processes) for r in records]


# --- 4) 스코어링 -----------------------------------------------------------
def score_all(records: list[SupplierRecord], spec: dict) -> list[dict]:
    """스펙 적합도 점수 + 근거를 붙여 반환. [{'record','scoring'}]."""
    processes = list(spec.get("process_codes", {}))
    preferred_regions = spec.get("spec", {}).get("region", {}).get("preferred", [])
    scored = []
    for r in records:
        s = score_supplier(r, required_processes=processes, preferred_regions=preferred_regions)
        scored.append({"record": r, "scoring": s})
    return scored


# --- 5) 숏리스트 -----------------------------------------------------------
def shortlist(scored: list[dict], *, top_n: int = 6) -> list[dict]:
    """게이트 통과분만 점수 내림차순 정렬 → 상위 N."""
    passed = [x for x in scored if x["scoring"]["passed_gate"]]
    passed.sort(key=lambda x: x["scoring"]["score"], reverse=True)
    return passed[:top_n]


# --- 오케스트레이션 --------------------------------------------------------
def run(
    spec: dict | None = None,
    *,
    longlist: list[SupplierRecord] | None = None,
    live: bool = False,
    top_n: int = 6,
) -> dict:
    """전체 흐름 실행. 결과 dict(JSON 직렬화 가능) 반환.

    longlist 주입 시 discover 생략(오프라인·데모 재생). live=True면 discover 실행.
    """
    spec = spec or load_spec()
    processes = list(spec.get("process_codes", {}))

    if longlist is None:
        if not live:
            raise ValueError("longlist를 주입하거나 live=True로 discover를 켜야 합니다")
        longlist = discover(spec)

    anchors, leftover = anchor_merge(longlist)
    classified = classify_all(anchors + leftover, processes=processes)
    scored = score_all(classified, spec)
    short = shortlist(scored, top_n=top_n)

    return {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "counts": {
            "longlist": len(longlist),
            "anchored": len(anchors),
            "leftover": len(leftover),
            "shortlist": len(short),
        },
        "shortlist": [_result_row(x) for x in short],
    }


def _result_row(x: dict) -> dict:
    """결과 레코드 → 데모가 읽을 평탄한 dict(존 분리: 외부 공개 필드만)."""
    r: SupplierRecord = x["record"]
    return {
        "name": r.name,
        "country": r.country,
        "region": r.region,
        "kind": r.kind.value,
        "homepage": str(r.homepage) if r.homepage else None,
        "verified": any(p.verified for p in r.provenances),
        "capabilities": [{"code": c.code, "evidence": c.evidence.value, "basis": c.basis}
                         for c in r.capabilities],
        "score": x["scoring"]["score"],
        "breakdown": x["scoring"]["breakdown"],
        "sources": [p.source_id for p in r.provenances],
    }
