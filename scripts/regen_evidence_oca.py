"""OCA 상세·판정 고정 — 결정적 재생성(데모 STEP 2/3 아티팩트).

라이브 발견(live_discover_oca)은 실제 후보 29곳을 찾았으나, 상세 수집(enrich_probe)은
대기업·중소 모두 홈페이지 크롤이 실패했다(폐도메인·robots 차단·회사명 불일치·타임아웃).
이는 regen_evidence.py가 애초에 지적한 라이브 상세 수집의 비결정·실패 문제 그대로다.

그래서 발견은 라이브 실데이터(실제 회사명·공장등록 앵커·종업원·지역)를 쓰되, 상세·판정은
'라이브로 실제 확인된 앵커 사실'만 고정값으로 둔다. 실존 회사에 지어낸 설비·인증을
'확인됨'으로 붙이지 않는다 — 홈페이지 크롤이 필요한 항목(설비·클린룸·ISO 세부)은 unknown
(확인 필요·RFI). 채점·게이트·평탄화는 실제 파이프라인 함수(score_all·shortlist·
_result_row)를 그대로 쓴다.

확인된 앵커 사실의 출처:
- 공장등록(팩토리온, ANCHOR): 주생산품·종업원·지역·공장 보유. live_discover 덤프에서 확인.
- 특허(KIPRIS, ANCHOR): 대상에스티 '롤러블/플렉시블 디스플레이용 광학점착필름'.
- 검색 경유 근거(웹, L1): 제론텍 OCA·ISO 9001 언급. enrich_probe 라이브에서 확인.

존 분리: 외부 공개 필드만. 내부 단가·품목코드·현 협력사명 없음.

실행: python scripts/regen_evidence_oca.py   (재실행해도 동일 JSON)
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
SPEC_PATH = ROOT / "data" / "synthetic" / "parsed_spec_oca.json"
OUT_PATH = ROOT / "data" / "output" / "shortlist_SR-2026-1118-OCA.json"

# 라이브 발견에서 제외한 금지어(존 분리) — 계획 파일 참조.
INCUMBENTS = ["CONG TY TNHH SAMJU VINA", "SAMJU VINA", "삼주비나", "3M VIETNAM LIMITED", "3M VIETNAM"]


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
    """라이브 발견 상위 국내 가공사 4곳 — 공장등록 앵커 + 실제 확인된 판정만."""
    from src.schema import SourceClass
    return [
        # 1) 대상에스티 — 공장등록 앵커 + KIPRIS 특허 + 홈페이지 확인 인증 12건. 최강 근거.
        # 홈페이지(daesangst.com): ISO 9001·14001·45001·27001·IATF 16949, 소부장 전문기업,
        # 기업부설연구소 2곳(서울·음성), 공장 2곳(충북 음성), 대상비나(베트남) 글로벌 네트워크.
        SupplierRecord(
            name="대상에스티", country="KR", region="충청북도 음성군", kind=CompanyKind.MANUFACTURER,
            homepage="https://www.daesangst.com", domain="daesangst.com",
            has_factory=True, employees=62,
            products=["롤러블 디스플레이용 광학점착필름", "플렉시블 디스플레이용 반사방지 광학투명점착필름",
                      "모바일·디스플레이 점착소재", "산업용 테이프"],
            capabilities=[ProcessCapability(code="OCA_LAM", label="OCA 라미네이션", evidence=L1,
                                            basis="공장등록 주생산품 '광학점착필름'"),
                          ProcessCapability(code="OPT_COAT", label="광학 점착 코팅", evidence=L3,
                                            basis="특허 '롤러블/플렉시블 디스플레이용 광학(투명)점착필름'")],
            checks=_checks(plan, {
                "P1": ("partial", L1, "공장등록 주생산품 '광학점착필름' · 라미네이션 설비 세부는 RFI 확인"),
                "P3": ("met", L3, "특허 '롤러블/플렉시블 디스플레이용 광학(투명)점착필름' · 모바일·디스플레이 점착소재"),
                "C1": ("met", L3, "홈페이지 인증 — ISO 9001 품질경영시스템(nqa)"),
                "C3": ("met", L3, "홈페이지 인증 — ISO 14001 환경경영시스템"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 종업원 62명"),
                         _anchor("kipris", note="광학점착필름 특허 출원인"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="https://www.daesangst.com",
                                    note="홈페이지 인증 12건 확인")],
            notes=["임직원 62명",
                   "[인증] ISO 9001 · ISO 14001 · ISO 45001 · ISO 27001 · IATF 16949",
                   "[인증] 소재부품장비 전문기업 · 대통령표창(2018)",
                   "[설비] 기업부설연구소 2곳(서울·음성) · 음성 1·2공장",
                   "[확인 필요] 라미네이터·오토클레이브·다이커터 등 공정 설비 사양 — RFI",
                   "대상비나(DAESANG S.T VINA) 베트남 법인 보유 — 현지 대응 가능"],
        ),
        # 2) 제론텍 — 공장등록 앵커 + 홈페이지 코팅설비 4기 확인. 코팅 전문(합착·다이컷 없음).
        # 홈페이지(zerontec.co.kr): 코팅기 4기(1,700mm MG/Slot Die, 열·UV), 슬리터 1기, 클린룸
        # 보유(등급 미표기). 제품: PET이형필름·Display 점착소재·기능성필름. 라미네이터·다이커터 없음.
        SupplierRecord(
            name="(주)제론텍", country="KR", region="충청북도 음성군", kind=CompanyKind.MANUFACTURER,
            homepage="https://zerontec.co.kr", domain="zerontec.co.kr",
            has_factory=True, employees=11,
            products=["PET 이형필름(광학용·불소)", "Display·산업용 점착소재", "기능성 코팅필름"],
            capabilities=[ProcessCapability(code="OPT_COAT", label="광학 점착 코팅", evidence=L2,
                                            basis="홈페이지 코팅설비 4기(MG·Slot Die, 열·UV 경화)")],
            checks=_checks(plan, {
                "P1": ("unmet", None, "홈페이지 설비현황 — 코팅기만, 합지(라미네이터) 능력 '無' 명시"),
                "P3": ("met", L2, "홈페이지 코팅설비 4기(1,700mm MG/Slot Die, 열·UV 경화) · 광학용 필름"),
                "P2": ("unmet", None, "홈페이지 설비현황 — 다이커터·타발기 없음(슬리터만 보유)"),
                "E2": ("met", L2, "홈페이지 — UV 경화 코팅설비(2호기 열/UV)"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 종업원 11명(사업장 단위)"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="https://zerontec.co.kr",
                                    note="홈페이지 코팅설비현황 확인")],
            notes=["임직원 11명(공장등록 사업장 단위)",
                   "[설비] 코팅기 4기(1,700mm MG/Slot Die, 열·UV, 월 Capa ~190만m) · 슬리터 1기",
                   "[설비] 클린룸 보유(등급 미표기)",
                   "[확인 필요] 합착(라미네이션)·다이컷 설비 없음 → 최종 가공 외주 여부 RFI",
                   "[확인 필요] ISO 9001·14001 인증서 사본 — 품질·환경방침만 공개 · RFI"],
        ),
        # 3) 에이엠텍 — 공장등록은 'OCA필름'이나 홈페이지 확인 결과 EV/ESS 배터리 소재 전문.
        # 홈페이지(am-tec.co.kr): 면압패드(Compression Pad, CS/CU Series)·불연재·TIM·Dri-Stik,
        # EV/ESS 열폭주 방지 소재. 코팅은 불연재용(Spray/Gravure/Dipping)으로 광학점착 비해당.
        # IATF 16949 인증(2016)·재료연구소 패밀리기업. OCA 광학점착 라미네이션 비해당 → 탈락.
        SupplierRecord(
            name="(주)에이엠텍", country="KR", region="경상북도 구미시", kind=CompanyKind.MANUFACTURER,
            homepage="http://www.am-tec.co.kr", domain="am-tec.co.kr",
            has_factory=True, employees=16,
            products=["면압패드(Compression Pad)", "불연재", "TIM 방열소재", "Dri-Stik"],
            checks=_checks(plan, {
                "P1": ("unmet", None, "홈페이지 — EV/ESS 배터리 면압패드·불연재 전문, OCA 광학점착 라미네이션 비해당"),
                "C1": ("met", L2, "홈페이지 연혁 — IATF 16949 인증(2016, ISO 9001 상위 규격)"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 종업원 16명"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="http://www.am-tec.co.kr",
                                    note="홈페이지 제품·연혁 확인")],
            notes=["임직원 16명(공장등록 사업장 단위)",
                   "[인증] IATF 16949(2016) · 재료연구소 패밀리기업 · 금오공대 산학협력가족회사",
                   "[제외 사유] EV/ESS 배터리 면압패드·불연재 전문 — OCA 광학점착 라미네이션 비해당(홈페이지 확인)"],
        ),
        # 4) 옵트론텍 — 홈페이지 확인 결과 광학필터 전문(삼성전기 협력사). OCA 라미네이션 비해당 확정.
        # 홈페이지(optrontec.net): 광학필터·밴드패스필터(모바일·전장·보안), ISO 9001·14001 보유.
        # '광학코팅'은 필터용 코팅이며 OCA 광학점착 라미네이션과 다른 공정 → 게이트 탈락이 타당.
        SupplierRecord(
            name="(주)옵트론텍", country="KR", region="광주광역시 북구", kind=CompanyKind.MANUFACTURER,
            homepage="https://www.optrontec.net", domain="optrontec.net",
            has_factory=True, employees=40,
            products=["광학필터", "밴드패스 필터", "모바일·전장·보안용 광학부품"],
            checks=_checks(plan, {
                "P1": ("unmet", None, "홈페이지 — 광학필터 전문, OCA 광학점착 라미네이션 비해당"),
                "C1": ("met", L2, "홈페이지 인증 — ISO 9001"),
                "C3": ("met", L2, "홈페이지 인증 — ISO 14001"),
            }),
            provenances=[_anchor("factorion", note="공장등록 확인 · 주생산품 '광학코팅' · 종업원 40명"),
                         Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                    extractor="site_detail", url="https://www.optrontec.net",
                                    note="홈페이지 제품·인증 확인")],
            notes=["임직원 40명(공장등록 사업장 단위)",
                   "[인증] ISO 9001 · ISO 14001 · 세계일류상품 · 삼성전기 우수협력사 · 코스닥 상장",
                   "[제외 사유] 광학필터 전문 — OCA 광학점착 라미네이션 비해당(홈페이지 확인)"],
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
