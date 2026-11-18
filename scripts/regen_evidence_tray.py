"""TRAY 상세·판정 고정 — 결정적 재생성(데모 STEP 2/3 아티팩트).

OCA 파일럿(regen_evidence_oca)과 동일 패턴. 발견·게이트·선별은 라이브
(live_discover_tray)로 실제 후보 풀을 얻되, 상세·판정은 '라이브로 실제 확인된
앵커 사실 + 홈페이지 실측'만 고정값으로 둔다. 실존 회사에 지어낸 설비·인증을
'확인됨'으로 붙이지 않는다 — 크롤이 필요한 항목(설비 세부·ISO 사본)은 unknown
(확인 필요·RFI). 채점·게이트·평탄화는 실제 파이프라인 함수를 그대로 쓴다.

확인된 사실의 출처:
- 공장등록(팩토리온, ANCHOR): 주생산품·종업원·지역·공장 보유. live_discover 덤프에서 확인.
- 특허(KIPRIS, ANCHOR): 에이테크솔루션·썬텍솔루션 출원인(src=factorion,kipris).
- 홈페이지 실측(site_detail, SPREAD): 사용자 제공 URL 직접 수집(2026-11-19).
  · 이에스디웍 esdwork.com — ESD 트레이 수직통합(진공/사출 성형·압출시트·코팅액·쿠션패드·금형 In Line)
  · 은성프라스틱 eunsungplastic.co.kr — 진공성형 전문(1991~), 성형기 4종, 트레이·블리스터
  · 에이테크솔루션 atechsolution.co.kr — 진공성형 금형+부품양산, 배터리 Cell Carrier, 코스닥
  · 썬텍솔루션 suntechfirst.com — 진공성형 '금형' 제작 전문(MCT 8대), 뿌리기술 전문기업

존 분리: 외부 공개 필드만. 내부 단가·품목코드·현 협력사명 없음.

실행: python scripts/regen_evidence_tray.py   (재실행해도 동일 JSON)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import _result_row, load_spec, score_all, shortlist
from src.schema import (
    CompanyKind,
    EvidenceLevel,
    ProcessCapability,
    Provenance,
    SpecCheck,
    SupplierRecord,
)
from src.search_plan import build_search_plan

L1, L2, L3 = EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "data" / "synthetic" / "parsed_spec_tray.json"
OUT_PATH = ROOT / "data" / "output" / "shortlist_SR-2026-1118-TRAY.json"

# 라이브 발견에서 제외한 금지어(존 분리) — TRAY 현 협력사(전부 중국). 계획 파일 참조.
INCUMBENTS = ["HUIZHOUCITY GERUITE INDUSTRIAL", "GERUITE",
              "DONG GUAN SHINSUNG PLASTIC", "SHINSUNG PLASTIC",
              "ESD STATIC CONTROL", "ESD(TIANJIN) STATIC CONTROL",
              "DONGGUAN ESD STATIC CONTROL", "NANO CHEM TECH TIANJIN"]


def _anchor(source_id: str, *, note: str, verified: bool = True) -> Provenance:
    from src.schema import SourceClass
    return Provenance(source_id=source_id, source_class=SourceClass.ANCHOR,
                      extractor="structured", verified=verified, note=note)


def _checks(plan: dict, facts: dict) -> list[SpecCheck]:
    """확인된 사실(facts: id → (verdict, evidence, basis)) → 스펙 항목 판정. 없는 항목은 unknown."""
    out = []
    for c in plan["checks"]:
        v, ev, basis = facts.get(c["id"], ("unknown", None, "공개 근거 없음 · RFI 확인 필요"))
        out.append(SpecCheck(item_id=c["id"], group=c["group"], label=c["label"],
                             required=c["required"], verdict=v, evidence=ev, basis=basis))
    return out


def _records(plan: dict) -> list[SupplierRecord]:
    """라이브 발견 국내 가공사 4곳 — 공장등록 앵커 + 홈페이지 실측 판정만."""
    from src.schema import SourceClass
    return [
        # 1) 이에스디웍 — ESD 트레이 수직통합. 공장등록 앵커 + 홈페이지 실측. 최강 근거.
        # 홈페이지(esdwork.com): 대전방지 Tray·압출시트·Cushion pad·사출 Tray·진공/사출 금형·
        # 대전방지 코팅액 자체 제조. "In Line 생산: 금형 설계·가공, 코팅액 제조, 시트 압출, 성형".
        # 발포라인·압출라인·멀티코팅라인. ISO 9001·14001(2006), 기업부설연구소(2007),
        # 3천만불 수출탑·대통령표창, 글로벌 법인(중국 천진·동관·상주, 베트남, 헝가리). 화성 본사·1공장.
        # ※ 라이브 선별(LLM)이 '전도성 타일 무관'으로 오분류 탈락시켰으나, 홈페이지 실측으로 정정.
        SupplierRecord(
            name="(주)이에스디웍", country="KR", region="경기도 화성시", kind=CompanyKind.MANUFACTURER,
            homepage="http://www.esdwork.com", domain="esdwork.com",
            has_factory=True, employees=None,
            products=["대전방지 진공성형 Tray", "대전방지 압출시트", "Cushion pad(완충패드)",
                      "사출 Tray", "진공/사출 금형", "대전방지 코팅액"],
            capabilities=[ProcessCapability(code="VFORM", label="진공성형(열성형)", evidence=L2,
                                            basis="홈페이지 — 진공성형 Tray·In Line 성형 공정"),
                          ProcessCapability(code="ESD", label="ESD 대전방지", evidence=L2,
                                            basis="홈페이지 — 대전방지 코팅액 자체 제조·멀티코팅라인"),
                          ProcessCapability(code="FOAM", label="패드쿠션 폼", evidence=L2,
                                            basis="홈페이지 — Cushion pad·발포라인 보유"),
                          ProcessCapability(code="EXTRUDE", label="KPS 시트 압출", evidence=L2,
                                            basis="홈페이지 — 대전방지 압출시트·압출라인 보유")],
            checks=_checks(plan, {
                "P1": ("met", L2, "홈페이지 — 진공성형 Tray 생산 · In Line 성형 공정 보유"),
                "P2": ("met", L2, "홈페이지 — 대전방지 코팅액 자체 제조 · 멀티코팅라인(ESD 수직통합)"),
                "P3": ("met", L1, "홈페이지 — Cushion pad(완충패드) · 발포라인 보유"),
                "P4": ("met", L1, "홈페이지 — 대전방지 압출시트 · 압출라인 보유(원단 자체 조달)"),
                "E1": ("met", L2, "홈페이지 — 진공/사출 성형 설비(In Line 성형)"),
                "E2": ("met", L2, "홈페이지 — 압출라인 보유"),
                "C1": ("met", L3, "홈페이지 — ISO 9001 인증(2006)"),
                "C3": ("met", L3, "홈페이지 — ISO 14001 인증(2006)"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 경기 화성 본사·1공장"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="http://www.esdwork.com",
                                    note="홈페이지 제품·공정·인증 실측(2026-11-19)")],
            notes=["[설비] 발포라인 · 압출라인 · 멀티코팅라인 · 진공/사출 성형 · 금형 설계·가공(In Line)",
                   "[인증] ISO 9001 · ISO 14001(2006) · 기업부설연구소(2007)",
                   "[인증] 3천만불 수출탑 · 대통령표창",
                   "[지역] 글로벌 법인 — 중국(천진·동관·상주) · 베트남 · 헝가리 · 자회사 이에스디케미칼(서산)",
                   "[확인 필요] 표면저항 측정기 · RoHS 성적서 · 종업원/매출 규모 — RFI",
                   "ESD 트레이 수직통합(성형·코팅·압출·폼·금형) — 사양서 전 공정 대응 가능"],
        ),
        # 2) 은성프라스틱 — 진공성형 전문(1991~). 공장등록 앵커 + 홈페이지 실측.
        # 홈페이지(eunsungplastic.co.kr): 성형기 4종(압진공/진공/압공/겟도바시), 제품 블리스터·
        # 트레이·클램쉘·컨테이너·상하함 케이스. 재료 PVC/PP/PS/PET/ABS 시트(외부 조달).
        # 생활·식품·전자부품 케이스 중심 — 대전방지(ESD)·폼 가공 언급 없음. 용인 18명.
        SupplierRecord(
            name="(주)은성프라스틱", country="KR", region="경기도 용인시", kind=CompanyKind.MANUFACTURER,
            homepage="http://www.eunsungplastic.co.kr", domain="eunsungplastic.co.kr",
            has_factory=True, employees=18, since_year=1991,
            products=["진공성형 트레이", "블리스터", "클램쉘", "컨테이너", "상·하함 케이스"],
            capabilities=[ProcessCapability(code="VFORM", label="진공성형(열성형)", evidence=L2,
                                            basis="홈페이지 — 진공성형 전문(1991~)·트레이 생산·성형기 4종")],
            checks=_checks(plan, {
                "P1": ("met", L2, "홈페이지 — 진공성형 전문(1991~) · 트레이 생산 · 성형기 4종 보유"),
                "P2": ("unmet", None, "홈페이지 — 생활·식품·전자부품 케이스 중심, 대전방지(ESD) 처리 언급 없음"),
                "P3": ("unmet", None, "홈페이지 — 진공성형 용기 전문, 패드쿠션 폼 가공 없음"),
                "E1": ("met", L2, "홈페이지 설비 — 압진공·진공·압공·겟도바시 성형기 4종"),
                "E2": ("unmet", None, "홈페이지 — 시트(PVC/PP/PS/PET/ABS) 외부 조달, 자체 압출 없음"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 종업원 18명(사업장 단위)"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="http://www.eunsungplastic.co.kr",
                                    note="홈페이지 제품·설비 실측(2026-11-19)")],
            notes=["임직원 18명(공장등록 사업장 단위) · 1991년 창업(업력 30년+)",
                   "[설비] 압진공·진공·압공·겟도바시 성형기 4종",
                   "[확인 필요] ESD 대전방지 처리 · 패드쿠션 폼 · ISO 9001 인증 — RFI",
                   "[판정] 진공성형 기반기술 보유, 단 디스플레이 ESD 포장 트레이는 비주력(RFI로 전환 가능성 확인)"],
        ),
        # 3) 에이테크솔루션 — 진공성형 금형+부품양산(코스닥). 공장등록+KIPRIS 앵커 + 홈페이지 실측.
        # 홈페이지(atechsolution.co.kr): 진공성형·발포·사출 금형 + 부품양산, 배터리 Cell Carrier,
        # 태국법인. 진공성형 양산 역량은 실재하나 '금형·부품양산'이 주력이고 디스플레이 ESD 포장
        # 트레이는 비해당. 광주 광산구.
        SupplierRecord(
            name="에이테크솔루션(주)", country="KR", region="광주광역시 광산구", kind=CompanyKind.MANUFACTURER,
            homepage="https://www.atechsolution.co.kr", domain="atechsolution.co.kr",
            has_factory=True, employees=None,
            products=["진공성형 금형", "발포 금형", "사출 금형", "배터리 Cell Carrier", "부품 양산"],
            capabilities=[ProcessCapability(code="VFORM", label="진공성형(열성형)", evidence=L1,
                                            basis="홈페이지 — 진공성형 금형·부품양산·배터리 Cell Carrier")],
            checks=_checks(plan, {
                "P1": ("partial", L1, "홈페이지 — 진공성형 금형·부품양산(Cell Carrier) · 디스플레이 ESD 포장 트레이 비주력"),
                "P2": ("unmet", None, "홈페이지 — 대전방지(ESD) 포장 처리 비해당"),
                "P3": ("unmet", None, "홈페이지 — 발포 '금형' 제작, 패드쿠션 폼 가공 완제품 아님"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 광주 광산구"),
                         _anchor("kipris", note="진공성형·금형 특허 출원인"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="https://www.atechsolution.co.kr",
                                    note="홈페이지 제품·사업 실측(2026-11-19)")],
            notes=["코스닥 상장 · 태국법인 보유",
                   "[제품] 진공성형·발포·사출 금형 + 부품양산 · 배터리 Cell Carrier",
                   "[확인 필요] 진공성형기(완제품 양산 설비) · ISO/IATF · 종업원 규모 — RFI",
                   "[판정] 진공성형 금형·부품양산 주력 — 디스플레이 ESD 포장 트레이 비해당(홈페이지 확인)"],
        ),
        # 4) 썬텍솔루션 — 진공성형 '금형' 제작 전문(MCT 8대). 공장등록+KIPRIS 앵커 + 홈페이지 실측.
        # 홈페이지(suntechfirst.com): 진공성형 금형 제작(화천 SIRIUS/VESTA·두산 VM MCT 8대),
        # 뿌리기술 전문기업(2025), 특허 2건, ISO 9001·14001(2022 기준). 화성 15명.
        # 금형만 제작 — 트레이 성형 완제품은 생산하지 않음 → 필수 공정 근거 0 → 게이트 탈락.
        SupplierRecord(
            name="(주)썬텍솔루션", country="KR", region="경기도 화성시", kind=CompanyKind.MANUFACTURER,
            homepage="https://suntechfirst.com", domain="suntechfirst.com",
            has_factory=True, employees=15,
            products=["진공성형 금형", "열성형 금형"],
            checks=_checks(plan, {
                "P1": ("unmet", None, "홈페이지 — 진공성형 '금형' 제작 전문(MCT 8대), 트레이 성형 완제품 비해당"),
                "P2": ("unmet", None, "홈페이지 — 금형 제작, 대전방지(ESD) 처리 비해당"),
                "P3": ("unmet", None, "홈페이지 — 금형 제작, 패드쿠션 폼 가공 없음"),
                "E1": ("unmet", None, "홈페이지 — 금형가공기(MCT) 8대 보유, 성형기(진공성형기) 없음"),
                "C1": ("met", L2, "홈페이지 — ISO 9001 인증(2022 기준)"),
                "C3": ("met", L2, "홈페이지 — ISO 14001 인증(2022 기준)"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 종업원 15명(사업장 단위)"),
                         _anchor("kipris", note="진공성형 금형 특허 출원인(2건)"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="https://suntechfirst.com",
                                    note="홈페이지 제품·설비·인증 실측(2026-11-19)")],
            notes=["임직원 15명(공장등록 사업장 단위)",
                   "[설비] MCT 8대(화천 SIRIUS·VESTA · 두산 VM) — 금형가공 설비",
                   "[인증] ISO 9001·14001(2022) · 뿌리기술 전문기업(2025) · 특허 2건",
                   "[제외 사유] 진공성형 '금형' 제작 전문 — 트레이 성형 완제품 비해당(홈페이지 확인)"],
        ),
    ]


def main() -> None:
    spec = load_spec(SPEC_PATH)
    plan = build_search_plan(spec, forbidden=INCUMBENTS)
    recs = _records(plan)
    scored = score_all(recs, plan)
    short = shortlist(scored, top_n=6)

    results = [_result_row(x) for x in scored]          # STEP 2용: 상세 수집 전체
    shortlist_names = [x["record"].name for x in short]  # STEP 3용: 게이트 통과 상위

    out = {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "counts": {"pool": len(recs), "shortlist": len(short)},
        "shortlist_names": shortlist_names,
        "results": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"재생성 완료 → {OUT_PATH.relative_to(ROOT)}")
    for x in scored:
        r, sc = x["record"], x["scoring"]
        gate = "통과" if sc["passed_gate"] else f"탈락({', '.join(sc['gate_reasons'])})"
        bd = " · ".join(f"{b['axis']} {b['points']}/{b['max']}" for b in sc["breakdown"])
        print(f"  [{sc['score']:>3}·{gate}] {r.name}")
        print(f"        {bd}")
    print(f"\n숏리스트({len(shortlist_names)}곳): {', '.join(shortlist_names)}")


if __name__ == "__main__":
    main()
