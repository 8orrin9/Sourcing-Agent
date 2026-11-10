"""파이프라인 — 공장등록 우선 발견 → 신뢰 게이트 → 상세 수집 → 스펙 체크 → 숏리스트.

2026-11-10 전환: 웹검색으로 회사를 찾고 공공DB로 보완하던 흐름을 뒤집었다. 공장등록
(팩토리온)이 '공장이 있는 제조사'를 생산품으로 직접 찾아 모집단을 만들고, 특허(KIPRIS)는
기술 보유 기업을 추가로 찾아 공장등록으로 확정하며, 웹은 홈페이지를 찾아 상세 정보를
수집하는 데(및 해외 보완 발견에) 쓴다.

```
[plan]          확정 스펙 → 검색 계획(발견·게이트·검증·가점)          search_plan.py
[discover]      ① 공장등록 생산품 검색 (발견 + 신원 확정)
                ② KIPRIS 기술검색 → 출원인 → 공장등록 회사명 대조
                ③ 웹검색 → LLM 회사 추출 → 국내는 공장등록 대조, 해외는 미확인 유지
[merge]         회사 단위 통합(공장관리번호·정규화 회사명)
[gate]          신뢰 게이트: 지역·최소 종업원·유통사 (알려진 값만, 모르면 통과)
[screen]        LLM 관련성 1차 선별(생산품 자유어의 동음이의 제거)
[enrich]        상위 N곳: 특허 보강 + 홈페이지 확정(웹+LLM) + 크롤·PDF 파싱
[check/score]   LLM 스펙 항목별 판정 → 규칙 점수 + 게이트 → 숏리스트
```

존 분리: 외부로 나가는 것은 검색 계획의 발견 검색어(공정·소재 일반어)와 회사명뿐이다.
내부 품목코드·금지어는 build_search_plan에서 제거된다.
모든 외부 호출은 실패해도 해당 건만 건너뛰고 log에 남긴다(한 소스 장애가 전체를 막지 않게).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .extractors.kipris import KiprisExtractor
from .extractors.site_detail import SiteDetailExtractor
from .extractors.structured import FactorionExtractor
from .extractors.web_search import WebSearchExtractor
from .llm.classify import classify_supplier, screen_candidates
from .llm.company_extract import extract_companies, pick_homepage
from .llm.merge import _norm_name, dedupe_factories, merge_into, query_name, resolve_to_factory
from .llm.score import pre_gate, score_supplier
from .schema import SupplierRecord
from .search_plan import build_search_plan

SPEC_PATH = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "parsed_spec_pmma.json"


def load_spec(path: Path | None = None) -> dict:
    return json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))


@dataclass
class Sources:
    """외부 소스 묶음 — 테스트에서는 가짜로 바꿔 끼운다."""

    factorion: FactorionExtractor = field(default_factory=FactorionExtractor)
    kipris: KiprisExtractor = field(default_factory=KiprisExtractor)
    web: WebSearchExtractor = field(default_factory=WebSearchExtractor)
    site: SiteDetailExtractor = field(default_factory=SiteDetailExtractor)


class Log(list):
    """단계별 실행 로그 [{stage, msg}] — 데모 실행 로그·감사 추적용."""

    def add(self, stage: str, msg: str) -> None:
        self.append({"stage": stage, "msg": msg})
        print(f"[{stage}] {msg}")


def _factory_id(rec: SupplierRecord) -> str | None:
    for p in rec.provenances:
        m = re.search(r"공장관리번호 (\S+)", p.note or "")
        if m:
            return m.group(1)
    return None


# --- ① 공장등록 생산품 검색 ------------------------------------------------
def discover_factorion(plan: dict, fac: FactorionExtractor, log: Log, *,
                       rows: int = 100, max_pages: int = 2) -> list[SupplierRecord]:
    """발견 검색어(생산품)마다 공장등록을 조회 → 공장 중복 제거 → 회사 단위로 접기."""
    by_factory: dict[str, SupplierRecord] = {}
    for term in plan["discovery"]["factorion_products"]:
        try:
            recs = fac.search(product=term, rows=rows, max_pages=max_pages)
        except Exception as e:
            log.add("공장등록", f"생산품 '{term}' 조회 실패: {e}")
            continue
        log.add("공장등록", f"생산품 '{term}' → {len(recs)}건")
        for r in recs:
            key = _factory_id(r) or f"{r.name}|{r.address}"
            if key in by_factory:
                by_factory[key].notes.append(f"[발견] 생산품 검색어 '{term}'")
            else:
                r.notes.append(f"[발견] 생산품 검색어 '{term}'")
                by_factory[key] = r
    companies = dedupe_factories(list(by_factory.values()))
    log.add("공장등록", f"공장 {len(by_factory)}곳 → 회사 {len(companies)}곳")
    return companies


def _resolve_kr(rec: SupplierRecord, fac: FactorionExtractor) -> SupplierRecord | None:
    """회사명으로 공장등록 조회 → 같은 회사면 앵커 기준 병합 레코드."""
    anchors = dedupe_factories(fac.extract({"company": query_name(rec.name), "rows": 20}))
    return resolve_to_factory(rec, anchors)


# --- ② KIPRIS 기술검색 → 출원인 → 공장등록 대조 ------------------------------
def discover_kipris(plan: dict, kipris: KiprisExtractor, fac: FactorionExtractor, log: Log, *,
                    max_applicants: int = 40) -> list[SupplierRecord]:
    """키워드 단독·IPC+대표 키워드로 특허 검색 → 기업 출원인 상위 N → 공장등록 확정분만 후보."""
    kp = plan["discovery"]["kipris"]
    queries = [{"word": w} for w in kp["keywords"]]
    queries += [{"ipc": c, "word": kp["keywords"][0] if kp["keywords"] else None} for c in kp["ipc"]]
    apps: dict[str, SupplierRecord] = {}
    for q in queries:
        try:
            recs = kipris.search_tech(**q)
        except Exception as e:
            log.add("특허", f"{q} 검색 실패: {e}")
            continue
        log.add("특허", f"{q} → 기업 출원인 {len(recs)}곳")
        for r in recs:
            k = _norm_name(r.name)
            apps[k] = merge_into(apps[k], r) if k in apps else r
    ranked = sorted(apps.values(), key=lambda r: -len([n for n in r.notes if n.startswith("[특허]")]))
    resolved, missed = [], 0
    for r in ranked[:max_applicants]:
        try:
            hit = _resolve_kr(r, fac)
        except Exception as e:
            log.add("특허", f"'{r.name}' 공장등록 대조 실패: {e}")
            continue
        if hit:
            hit.notes.append("[발견] 특허 출원인 → 공장등록 확인")
            resolved.append(hit)
        else:
            missed += 1
    log.add("특허", f"출원인 {len(apps)}곳 중 상위 {min(len(apps), max_applicants)}곳 대조 → "
                   f"공장등록 확인 {len(resolved)}곳 · 미확인 {missed}곳(제외)")
    return resolved


# --- ③ 웹검색 → LLM 회사 추출 -------------------------------------------------
def discover_web(plan: dict, web: WebSearchExtractor, fac: FactorionExtractor, log: Log, *,
                 per_query: int = 10) -> list[SupplierRecord]:
    """로케일별 검색 → 검색 직후 LLM이 기업만 추출 → 국내 기업은 공장등록 대조."""
    results = []
    for loc, qs in plan["discovery"]["web"].items():
        for q in qs:
            try:
                results.extend(web.search(q, locale=loc, num=per_query))
            except Exception as e:
                log.add("웹", f"[{loc}] '{q}' 검색 실패: {e}")
    if not results:
        return []
    companies, dropped = extract_companies(results, target=plan.get("target_item") or "")
    log.add("웹", f"검색 결과 {len(results)}건 → AI 회사 추출 {len(companies)}곳 · "
                 f"문서·마켓·뉴스 등 {len(dropped)}건 제외")
    out, verified = [], 0
    for c in companies:
        if c.country in (None, "KR"):
            try:
                hit = _resolve_kr(c, fac)
            except Exception as e:
                log.add("웹", f"'{c.name}' 공장등록 대조 실패: {e}")
                hit = None
            if hit:
                verified += 1
                hit.notes.append("[발견] 웹 → 공장등록 확인")
                out.append(hit)
                continue
        out.append(c)  # 해외·미등록은 미확인으로 유지(점수에서 공장등록 가점 없음)
    log.add("웹", f"국내 공장등록 확인 {verified}곳 · 미확인 유지 {len(out) - verified}곳")
    return out


# --- 통합·게이트·선별 ---------------------------------------------------------
def merge_pool(records: list[SupplierRecord]) -> list[SupplierRecord]:
    """여러 소스의 같은 회사를 하나로(공장등록 레코드를 기준으로)."""
    pool: dict[str, SupplierRecord] = {}
    for r in sorted(records, key=lambda r: not r.has_factory):  # 공장등록 먼저 → base
        k = _norm_name(r.name)
        pool[k] = merge_into(pool[k], r) if k in pool else r
    return list(pool.values())


def trust_gate(records: list[SupplierRecord], plan: dict, log: Log, excluded: list[dict]):
    kept = []
    for r in records:
        why = pre_gate(r, plan)
        if why:
            excluded.append({"name": r.name, "stage": "신뢰 게이트", "reason": why})
        else:
            kept.append(r)
    log.add("게이트", f"{len(records)}곳 → 통과 {len(kept)}곳 · 제외 {len(records) - len(kept)}곳")
    return kept


def _priority(r: SupplierRecord):
    """상세 수집 우선순위: 공장등록 확인 > 특허 보유 > 종업원 수."""
    return (not r.has_factory, not any(p.source_id == "kipris" for p in r.provenances),
            -(r.employees or 0))


# --- 보강(상위 N) -------------------------------------------------------------
def enrich_patents(records: list[SupplierRecord], kipris: KiprisExtractor, log: Log) -> list[SupplierRecord]:
    """특허 근거가 아직 없는 회사만 출원인명으로 KIPRIS 조회해 보강(새 후보는 만들지 않음)."""
    out, added = [], 0
    for r in records:
        if any(p.source_id == "kipris" for p in r.provenances):
            out.append(r)
            continue
        try:
            pats = kipris.extract({"company": query_name(r.name), "rows": 5})
        except Exception as e:
            log.add("특허", f"'{r.name}' 보강 실패: {e}")
            pats = []
        if pats:
            added += 1
            out.append(merge_into(r, pats[0]))
        else:
            out.append(r)
    log.add("특허", f"보강 대상 {len(records)}곳 중 특허 확인 {added}곳")
    return out


def enrich_sites(records: list[SupplierRecord], web: WebSearchExtractor,
                 site: SiteDetailExtractor, log: Log) -> list[SupplierRecord]:
    """홈페이지 확정(공장등록 값 → 없으면 웹검색 + LLM 판별) → 크롤·PDF → 상세 병합."""
    out = []
    for r in records:
        home = str(r.homepage) if r.homepage else None
        if not home:
            try:
                loc = "KR" if r.country in (None, "KR") else "EN"
                res = web.search(f"{query_name(r.name)} {r.region or ''}".strip(), locale=loc, num=8)
                home, why = pick_homepage(r.name, r.region, res)
                log.add("상세", f"[{r.name}] 홈페이지 {'확정 ' + home if home else '미확보'} · {why}")
            except Exception as e:
                log.add("상세", f"[{r.name}] 홈페이지 검색 실패: {e}")
        if not home:
            r = r.model_copy(deep=True)
            r.notes.append("[확인 필요] 홈페이지 미확보 — RFI·현장 확인")
            out.append(r)
            continue
        try:
            detail = site.extract({"homepage": home})
        except Exception as e:
            log.add("상세", f"[{r.name}] 수집 실패: {e}")
            detail = []
        if detail:
            merged = merge_into(r, detail[0])
            if not merged.homepage:
                merged.homepage = home
            docs = [n for n in detail[0].notes if n.startswith("[문서]")]
            log.add("상세", f"[{r.name}] 수집 완료 · 문서 {len(docs)}건")
            out.append(merged)
        else:
            out.append(r)
    return out


# --- 판정·점수·숏리스트 -------------------------------------------------------
def classify_all(records: list[SupplierRecord], plan: dict) -> list[SupplierRecord]:
    """각 레코드를 스펙 항목별로 LLM 판정."""
    return [classify_supplier(r, checks=plan["checks"]) for r in records]


def score_all(records: list[SupplierRecord], plan: dict) -> list[dict]:
    """[{'record','scoring'}] — 규칙 점수 + 축별 근거 + 게이트."""
    return [{"record": r, "scoring": score_supplier(r, plan)} for r in records]


def shortlist(scored: list[dict], *, top_n: int = 6) -> list[dict]:
    """게이트 통과분만 점수 내림차순 정렬 → 상위 N."""
    passed = [x for x in scored if x["scoring"]["passed_gate"]]
    passed.sort(key=lambda x: x["scoring"]["score"], reverse=True)
    return passed[:top_n]


# --- 오케스트레이션 -----------------------------------------------------------
def run(
    spec: dict | None = None,
    *,
    sources: Sources | None = None,
    forbidden: list[str] | None = None,
    enrich_top: int = 30,
    top_n: int = 6,
) -> dict:
    """전체 흐름 실행(라이브). 결과 dict(JSON 직렬화 가능) 반환.

    spec: 확정 스펙(parse_spec + apply_selection 결과). forbidden: 검색어 금지어(현 협력사명 등).
    enrich_top: 상세 수집·LLM 판정까지 진행할 최대 업체 수(비용 상한).
    """
    spec = spec or load_spec()
    src = sources or Sources()
    log, excluded = Log(), []
    plan = build_search_plan(spec, forbidden=forbidden)
    if plan["removed"]:
        log.add("계획", f"내부 정보 포함 검색어 {len(plan['removed'])}건 제외")

    fac = discover_factorion(plan, src.factorion, log)
    pat = discover_kipris(plan, src.kipris, src.factorion, log)
    web = discover_web(plan, src.web, src.factorion, log)
    pool = merge_pool(fac + pat + web)
    log.add("통합", f"공장등록 {len(fac)} + 특허 {len(pat)} + 웹 {len(web)} → 회사 {len(pool)}곳")

    gated = trust_gate(pool, plan, log, excluded)
    core = [c["label"] for c in plan["checks"] if c["group"] == "processes" and c["required"]]
    screened, dropped = screen_candidates(gated, target=plan.get("target_item") or "", processes=core)
    excluded += [{"name": r.name, "stage": "관련성 선별", "reason": why} for r, why in dropped]
    log.add("선별", f"{len(gated)}곳 → 관련 {len(screened)}곳 · 무관 {len(dropped)}곳")

    top = sorted(screened, key=_priority)[:enrich_top]
    enriched = enrich_sites(enrich_patents(top, src.kipris, log), src.web, src.site, log)
    scored = score_all(classify_all(enriched, plan), plan)
    excluded += [{"name": x["record"].name, "stage": "스펙 게이트",
                  "reason": ", ".join(x["scoring"]["gate_reasons"])}
                 for x in scored if not x["scoring"]["passed_gate"]]
    short = shortlist(scored, top_n=top_n)

    return {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "search_plan": plan,
        "counts": {
            "factorion": len(fac), "kipris": len(pat), "web": len(web), "pool": len(pool),
            "gated": len(gated), "screened": len(screened), "enriched": len(enriched),
            "shortlist": len(short),
        },
        "log": list(log),
        "excluded": excluded,
        "shortlist": [_result_row(x) for x in short],
    }


def _result_row(x: dict) -> dict:
    """결과 레코드 → 데모가 읽을 평탄한 dict(존 분리: 외부 공개 필드만)."""
    r: SupplierRecord = x["record"]
    equipment = [n[len("[설비]"):].strip() for n in r.notes if n.startswith("[설비]")]
    certifications = [n[len("[인증]"):].strip() for n in r.notes if n.startswith("[인증]")]
    documents = [n[len("[문서]"):].strip() for n in r.notes if n.startswith("[문서]")]
    return {
        "name": r.name,
        "country": r.country,
        "region": r.region,
        "address": r.address,
        "domain": r.domain,
        "kind": r.kind.value,
        "homepage": str(r.homepage) if r.homepage else None,
        "verified": any(p.verified for p in r.provenances),
        "employees": r.employees,
        "since_year": r.since_year,
        "equipment": equipment,
        "certifications": certifications,
        "documents": documents,
        "products": list(r.products),
        "contact_dept": r.contact_dept,
        "contact_public": r.contact_public,
        "capabilities": [{"code": c.code, "evidence": c.evidence.value, "basis": c.basis}
                         for c in r.capabilities],
        "checks": [{"id": c.item_id, "label": c.label, "required": c.required, "verdict": c.verdict,
                    "evidence": c.evidence.value if c.evidence else None, "basis": c.basis}
                   for c in r.checks],
        "score": x["scoring"]["score"],
        "breakdown": x["scoring"]["breakdown"],
        "sources": sorted({p.source_id for p in r.provenances}),
    }
