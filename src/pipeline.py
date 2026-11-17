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

from .extractors.kipris import KiprisExtractor
from .extractors.registry import registry
from .extractors.structured import FactorionExtractor
from .llm.classify import classify_supplier
from .llm.merge import _norm_name, absorb_candidates, dedupe_factories, merge_into
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


# --- 2b) 특허 근거 보강 (준앵커) -------------------------------------------
def enrich_patents(
    records: list[SupplierRecord],
    *,
    kipris: KiprisExtractor | None = None,
) -> list[SupplierRecord]:
    """회사명으로 KIPRIS를 조회해 특허 보유·기술분야 근거를 기존 레코드에 보강한다.

    KIPRIS는 준앵커라 새 후보를 만들지 않는다(STEP_1-1: "팩토리온이 앵커, 특허는
    팩토리온에 조인되는 준앵커"). 정규화 회사명이 일치하는 레코드에 특허 제목·IPC·
    provenance를 merge_into로 누적할 뿐이다. 매칭 실패는 감점이 아니라 '근거 없음'.
    주소를 안 주므로 verified는 올리지 않는다(식별 확정은 팩토리온 몫).
    """
    kipris = kipris or KiprisExtractor()
    names = sorted({r.name for r in records if r.name and r.name != "(미상)"})
    patents: dict[str, SupplierRecord] = {}
    for name, recs in kipris.verify(names).items():
        if recs:  # 출원인 1명당 1건으로 집계되어 옴
            patents[_norm_name(name)] = recs[0]

    out = []
    for r in records:
        pat = patents.get(_norm_name(r.name))
        out.append(merge_into(r, pat) if pat else r)
    return out


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
    enrich: bool = False,
    top_n: int = 6,
) -> dict:
    """전체 흐름 실행. 결과 dict(JSON 직렬화 가능) 반환.

    longlist 주입 시 discover 생략(오프라인·데모 재생). live=True면 discover 실행.
    enrich=True(또는 live)면 KIPRIS로 특허 근거를 보강(준앵커, API 호출).
    """
    spec = spec or load_spec()
    processes = list(spec.get("process_codes", {}))

    if longlist is None:
        if not live:
            raise ValueError("longlist를 주입하거나 live=True로 discover를 켜야 합니다")
        longlist = discover(spec)

    anchors, leftover = anchor_merge(longlist)
    enriched = enrich_patents(anchors + leftover) if (live or enrich) else (anchors + leftover)
    classified = classify_all(enriched, processes=processes)
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
    # notes에 접두로 남긴 설비·인증·임직원 근거를 상세수집 화면용으로 평탄화(존 분리 유지)
    equipment = [n[len("[설비]"):].strip() for n in r.notes if n.startswith("[설비]")]
    certifications = [n[len("[인증]"):].strip() for n in r.notes if n.startswith("[인증]")]
    employees = next((n[len("임직원"):].strip() for n in r.notes if n.startswith("임직원")), None)
    return {
        "name": r.name,
        "country": r.country,
        "region": r.region,
        "address": r.address,
        "domain": r.domain,
        "kind": r.kind.value,
        "homepage": str(r.homepage) if r.homepage else None,
        "verified": any(p.verified for p in r.provenances),
        "employees": employees,
        "equipment": equipment,
        "certifications": certifications,
        "products": list(r.products),
        "contact_dept": r.contact_dept,
        "contact_public": r.contact_public,
        "capabilities": [{"code": c.code, "evidence": c.evidence.value, "basis": c.basis}
                         for c in r.capabilities],
        "score": x["scoring"]["score"],
        "breakdown": x["scoring"]["breakdown"],
        "sources": [p.source_id for p in r.provenances],
    }
