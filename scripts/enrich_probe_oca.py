"""OCA 상세 수집(enrich) 라이브 1회 탐색 — 실제 수집 품질 확인용.

목적: screened 후보 상위 N곳에 홈페이지 크롤·PDF·LLM 판정을 실제로 돌려, 상세 수집이
얼마나 되는지 '눈으로' 본다. 비결정·네트워크 의존이라 데모 산출물(shortlist JSON)에는
쓰지 않고, 여기서 확인한 사실을 손으로 regen_evidence_oca.py의 고정값에 반영한다.

run()의 enrich~classify~score 구간(pipeline.py 403~410)을 그대로 재현한다. 발견은
live_discover_oca.discover_oca()를 재사용(두 번 돌리지 않음).

실행: python scripts/enrich_probe_oca.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.live_discover_oca import discover_oca
from src.pipeline import (
    Log,
    _priority,
    _result_row,
    classify_all,
    enrich_patents,
    enrich_sites,
    score_all,
    shortlist,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "output" / "enrich_probe_SR-2026-1118-OCA.json"

# 데모에서 제외하기로 한 노이즈(고객 계열사·합병 소멸). enrich 비용도 아끼려 사전 제외.
EXCLUDE_NAMES = {"삼성에스디아이(주)", "제일모직(주)"}
ENRICH_TOP = 5

# 중소 가공사 우선 — 홈페이지에 설비·인증을 직접 올려두는 업체가 라이브 수집에 유리.
# 사양서 공정과 직접 맞닿는 주생산품(OCA필름·점착코팅·광학코팅) 공장등록 앵커.
PREFER_NAMES = ["(주)에이엠텍", "(주)제론텍 양성공장", "(주)옵트론티에스",
                "주식회사 테이팩스 새만금공장", "대상에스티"]


def main() -> None:
    log = Log()
    d = discover_oca(log)
    plan, src = d["plan"], d["src"]

    screened = [r for r in d["screened"] if r.name not in EXCLUDE_NAMES]
    prefer = {n: i for i, n in enumerate(PREFER_NAMES)}
    # PREFER_NAMES를 먼저(지정 순서대로), 그 외는 _priority 순서로 뒤에 붙인다.
    top = sorted(screened, key=lambda r: (prefer.get(r.name, len(prefer)), _priority(r)))[:ENRICH_TOP]
    log.add("선정", f"상세 수집 대상 {len(top)}곳: " + ", ".join(r.name for r in top))

    enriched = enrich_sites(
        enrich_patents(top, src.kipris, log), src.web, src.site, log,
        keywords=plan["discovery"]["kipris"]["keywords"])
    scored = score_all(classify_all(enriched, plan), plan)
    short = shortlist(scored, top_n=ENRICH_TOP)
    rows = [_result_row(x) for x in scored]  # 게이트 탈락 포함 전부 저장(검수용)

    out = {
        "request_id": d["spec"].get("request_id"),
        "target_item": d["spec"].get("target_item"),
        "enrich_top": ENRICH_TOP,
        "shortlist_names": [x["record"].name for x in short],
        "log": list(log),
        "results": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --- 수집 품질 요약 ---
    print(f"\n덤프 완료 → {OUT_PATH.relative_to(ROOT)}\n")
    for x in scored:
        r = x["record"]
        equip = sum(1 for n in r.notes if n.startswith("[설비]"))
        cert = sum(1 for n in r.notes if n.startswith("[인증]"))
        doc = sum(1 for n in r.notes if n.startswith("[문서]"))
        met = sum(1 for c in r.checks if c.verdict == "met")
        home = str(r.homepage) if r.homepage else "홈페이지 미확보"
        gate = "통과" if x["scoring"]["passed_gate"] else "게이트탈락"
        print(f"  [{x['scoring']['score']:>3}·{gate}] {r.name}")
        print(f"        {home} · 설비 {equip} · 인증 {cert} · 문서 {doc} · met {met}/{len(r.checks)}")


if __name__ == "__main__":
    main()
