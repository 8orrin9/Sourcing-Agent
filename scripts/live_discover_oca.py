"""OCA 발견~선별 라이브 실행 + 검토용 덤프 (2단계 파일럿, 상세·판정 전까지).

pipeline.run()은 최종 숏리스트만 상세 반환하고 중간 단계(pool/gated/screened)는
숫자만 준다. 데모 STEP 1(롱리스트)을 실데이터로 채우려면 중간 산출의 '회사 목록'이
필요하므로, run()을 수정하지 않고 발견~선별 단계 함수를 그대로 조합해 중간 산출을
사람이 검토 가능한 JSON·콘솔 표로 덤프한다.

- 실행 범위: discover_factorion → discover_kipris → discover_web → merge_pool →
  trust_gate → screen_candidates. enrich/classify/score는 호출하지 않는다(비용·비결정).
- 존 분리: 현 협력사명을 forbidden으로 넘겨 검색어에서 제거(plan["removed"]에 남김).
- 라이브: 외부 API(팩토리온·KIPRIS·Serper) + LLM(웹 회사 추출·관련성 선별). 소스별
  장애는 pipeline이 건별 skip하고 log에 남긴다.

실행: python scripts/live_discover_oca.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import (
    Log,
    Sources,
    discover_factorion,
    discover_kipris,
    discover_web,
    load_spec,
    merge_pool,
    scale_warning,
    screen_candidates,
    trust_gate,
)
from src.schema import SupplierRecord
from src.search_plan import build_search_plan

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "data" / "synthetic" / "parsed_spec_oca.json"
OUT_PATH = ROOT / "data" / "output" / "discover_SR-2026-1118-OCA.json"

# 존 분리: OCA 현 협력사(비교 기준으로만 내부 표시) — 검색어에서 제거.
INCUMBENTS = ["CONG TY TNHH SAMJU VINA", "SAMJU VINA", "삼주비나", "3M VIETNAM LIMITED", "3M VIETNAM"]

REQUIRED_KEYS = ["OPENAI_API_KEY", "DATA_GO_KR_API_KEY", "KIPRIS_API_KEY", "SERPER_API_KEY"]


def _discovery_path(rec: SupplierRecord) -> list[str]:
    return [n[len("[발견]"):].strip() for n in rec.notes if n.startswith("[발견]")]


def _row(rec: SupplierRecord, stage: str, plan: dict, *,
         gate_reason: str | None = None, screen_reason: str | None = None) -> dict:
    """검토용 평탄 행 — 존 분리: 외부 공개 필드만."""
    return {
        "name": rec.name,
        "country": rec.country,
        "region": rec.region,
        "discovery_path": _discovery_path(rec),
        "has_factory": rec.has_factory,
        "sources": sorted({p.source_id for p in rec.provenances}),
        "stage": stage,
        "gate_reason": gate_reason,
        "screen_reason": screen_reason,
        "scale_warning": scale_warning(rec, plan),
    }


def _all_search_terms(plan: dict) -> list[str]:
    d = plan["discovery"]
    terms = list(d["factorion_products"]) + list(d["kipris"]["keywords"])
    for qs in d["web"].values():
        terms += qs
    return terms


def _verify_plan(plan: dict) -> None:
    g = plan["gates"]
    assert g["countries"] == ["KR", "VN"], f"countries={g['countries']}"
    assert g["min_employees"] == 50, f"min_employees={g['min_employees']}"
    assert len(plan["checks"]) == 11, f"checks={len(plan['checks'])}"
    # 존 분리: 최종 검색어 어디에도 금지어가 없어야 한다(removed가 비어도 정상 — 스펙에
    # 애초에 협력사명이 없었다는 뜻). 실제로 섞여 들어갔다면 여기서 잡는다.
    leaked = [t for t in _all_search_terms(plan)
              if any(b.lower() in t.lower() for b in INCUMBENTS)]
    assert not leaked, f"검색어에 금지어 유출: {leaked}"
    print(f"[검증] plan OK · countries={g['countries']} · min_employees={g['min_employees']} "
          f"· checks={len(plan['checks'])} · removed={plan['removed']} · 금지어 유출 없음")


def _check_keys() -> None:
    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if "SERPER_API_KEY" in missing:
        sys.exit("[중단] SERPER_API_KEY 없음 — OCA는 해외 발견이 핵심이라 웹 검색이 필수입니다.")
    if "OPENAI_API_KEY" in missing:
        sys.exit("[중단] OPENAI_API_KEY 없음 — 웹 회사 추출·관련성 선별이 LLM 의존입니다.")
    if missing:
        print(f"[경고] 키 없음: {missing} — 해당 소스는 건너뜁니다.")


def discover_oca(log: Log) -> dict:
    """OCA 발견~선별 라이브 실행 → 중간 산출 묶음. enrich 실험 스크립트에서도 재사용한다."""
    spec = load_spec(SPEC_PATH)
    plan = build_search_plan(spec, forbidden=INCUMBENTS)
    _verify_plan(plan)

    excluded: list[dict] = []
    src = Sources()

    fac = discover_factorion(plan, src.factorion, log)
    pat = discover_kipris(plan, src.kipris, src.factorion, log)
    web = discover_web(plan, src.web, src.factorion, log)

    pool = merge_pool(fac + pat + web)
    log.add("통합", f"공장등록 {len(fac)} + 특허 {len(pat)} + 웹 {len(web)} → 회사 {len(pool)}곳")
    assert len(pool) <= len(fac) + len(pat) + len(web)

    gated = trust_gate(pool, plan, log, excluded)
    assert len(gated) + len(excluded) == len(pool), "게이트 통과+제외 ≠ 통합"

    core = [c["label"] for c in plan["checks"] if c["group"] == "processes" and c["required"]]
    screened, dropped = screen_candidates(gated, target=plan.get("target_item") or "", processes=core)
    assert len(screened) + len(dropped) == len(gated), "선별 통과+제외 ≠ 게이트 통과"
    log.add("선별", f"{len(gated)}곳 → 관련 {len(screened)}곳 · 무관 {len(dropped)}곳")

    return {"spec": spec, "plan": plan, "src": src, "excluded": excluded,
            "counts": {"factorion": len(fac), "kipris": len(pat), "web": len(web)},
            "pool": pool, "gated": gated, "screened": screened, "dropped": dropped}


def main() -> None:
    _check_keys()
    log = Log()
    d = discover_oca(log)
    spec, plan, excluded = d["spec"], d["plan"], d["excluded"]
    pool, gated, screened, dropped = d["pool"], d["gated"], d["screened"], d["dropped"]

    # --- 검토용 행 구성: 생존 스테이지 + 탈락 사유를 한 레코드에 합친다 ---
    gate_reasons = {e["name"]: e["reason"] for e in excluded}
    screen_reasons = {r.name: why for r, why in dropped}
    screened_names = {r.name for r in screened}
    gated_names = {r.name for r in gated}

    rows = []
    for r in pool:
        if r.name in screened_names:
            rows.append(_row(r, "screened", plan))
        elif r.name in gated_names:
            rows.append(_row(r, "gated", plan, screen_reason=screen_reasons.get(r.name)))
        else:
            rows.append(_row(r, "pool", plan, gate_reason=gate_reasons.get(r.name)))

    out = {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "search_plan": plan,
        "counts": {
            **d["counts"], "pool": len(pool),
            "gated": len(gated), "screened": len(screened),
        },
        "log": list(log),
        "candidates": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --- 콘솔 표 ---
    print(f"\n덤프 완료 → {OUT_PATH.relative_to(ROOT)}  (후보 {len(rows)}곳)\n")
    print(f"  {'스테이지':<10} {'국가':<5} {'공장':<4} {'소스':<22} 회사 · 사유")
    for r in sorted(rows, key=lambda x: {"screened": 0, "gated": 1, "pool": 2}[x["stage"]]):
        why = r["gate_reason"] or r["screen_reason"] or ""
        warn = f" ⚠{r['scale_warning']}" if r["scale_warning"] else ""
        print(f"  {r['stage']:<10} {str(r['country'] or '-'):<5} "
              f"{'Y' if r['has_factory'] else '-':<4} {','.join(r['sources'])[:22]:<22} "
              f"{r['name']}{warn}" + (f"  · {why}" if why else ""))


if __name__ == "__main__":
    main()
