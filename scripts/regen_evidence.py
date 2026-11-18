"""수집 증거 콘솔 데이터 재생성 — Best-Case 하이브리드(결정적·오프라인).

콘솔(데모 STEP 1)에 올리는 숏리스트 3사를 결정적으로 재생성한다. 라이브 재실행
(pipeline.run)은 LLM 비결정·외부 API·네트워크를 요구하므로 데모 아티팩트로 부적합해
쓰지 않는다. 대신 '이미 확인된 사실'로 레코드를 만들고, 점수·게이트·평탄화는 실제
파이프라인 함수(build_search_plan·score_all·shortlist·_result_row)를 그대로 쓴다.

- 서진코팅·동림첨단소재: site_detail 라이브 수집에서 확인된 값(data/fixtures/site_detail/*.html).
  스펙 항목별 판정(checks)은 그 설비·인증 사실을 스펙 항목에 대응시킨 값이다.
- 올리콘발저스코팅코리아(유): 공장등록(공공DB) 검증 앵커. 상세 공개 근거가 없어 설비·인증은
  unknown(확인 필요)으로 둔다.

2026-11-10: 스펙 체크·신뢰 점수(공장등록·규모·업력·특허) 체계로 갱신. 기존에 읽던
data/output 산출 파일 의존을 없앴다(저장소에 없는 파일이라 재현 불가였음).

존 분리: 외부 공개 필드만. 내부 단가·품목코드·현 협력사명 없음.
개인정보 비저장: 담당 '부서'와 대표 연락처만(개인 이름 없음).

실행: python scripts/regen_evidence.py   (재실행해도 동일 JSON)
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
    SourceClass,
    SpecCheck,
    SupplierRecord,
)
from src.search_plan import build_search_plan

L1, L2, L3 = EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "output" / "shortlist_SR-2026-1025-014.json"

# 상세 수집(site_detail) 라이브에서 확인된 Best-Case 2사 — 픽스처 근거.
# capabilities/kind는 라이브 LLM 판정 결과를 '이미 확인된 사실'로 고정하고,
# 채점은 결정적 규칙(score_supplier)으로 한다.
_SITE_DETAIL = {
    "seojin": dict(
        name="서진코팅",
        homepage="https://www.seojincoat.co.kr",
        domain="seojincoat.co.kr",
        country="KR",
        region="충청북도 청주시",
        address="충청북도 청주시 흥덕구 오송읍 오송생명로 123",
        kind=CompanyKind.MANUFACTURER,
        products=["PMMA 하드코팅 시트(T0.3~T1.0)", "AF 코팅 광학필름", "디스플레이 보호 커버"],
        contact_dept="영업관리팀",
        contact_public="sales@seojincoat.co.kr",
        capabilities=[
            ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L3,
                              basis="하드코팅 조성물 특허 3건 · 연필경도 3H 시험성적서 · R2R 코터 2기"),
            ProcessCapability(code="AF", label="내지문(AF) 코팅", evidence=EvidenceLevel.L2,
                              basis="AF 진공 증착 코터 1기 · 내지문 코팅 라인"),
        ],
        employees="84명",
        checks={
            "P1": ("met", L3, "하드코팅 조성물 특허 3건 · 연필경도 3H 시험성적서"),
            "P2": ("met", L2, "AF 진공 증착 코터 1기"),
            "P3": ("met", L2, "CO2 / UV 레이저 외형 가공기 각 1기"),
            "E1": ("met", L2, "R2R 하드코팅 코터 2기"),
            "E2": ("met", L2, "UV 경화 라인 3기"),
            "E3": ("met", L2, "AF 진공 증착 코터 1기"),
            "E4": ("met", L2, "UV 레이저 외형 가공기 1기"),
            "C1": ("met", L3, "ISO 9001:2015 (KFQ · 유효 2027.03)"),
            "C2": ("met", L2, "클린룸 Class 1,000(코팅 라인)"),
            "C3": ("met", L3, "ISO 14001:2015"),
        },
        equipment=[
            "R2R(Roll-to-Roll) 하드코팅 코터 2기",
            "UV 경화 라인(질소 퍼지형) 3기",
            "AF(내지문) 진공 증착 코터 1기",
            "CO2 / UV 레이저 외형 가공기 각 1기",
            "클린룸 Class 1,000(코팅 라인) · Class 10,000(검사)",
        ],
        certifications=[
            "ISO 9001:2015 품질경영시스템(KFQ · 유효 2027.03)",
            "ISO 14001:2015 환경경영시스템",
            "하드코팅 조성물 특허 3건(연필경도 3H 시험성적서)",
        ],
    ),
    "dongrim": dict(
        name="동림첨단소재",
        homepage="https://www.dongrim-am.com",
        domain="dongrim-am.com",
        country="KR",
        region="경기도 평택시",
        address="경기도 평택시 포승읍 평택항로 55",
        kind=CompanyKind.MANUFACTURER,
        products=["하드코팅 필름", "AF 코팅 수탁가공", "광학 점착필름"],
        contact_dept="구매자재팀",
        contact_public="info@dongrim-am.com",
        capabilities=[
            ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L3,
                              basis="광학용 HC 시트 시험성적서(연필경도 3H) · Micro-gravure 코팅 라인"),
            ProcessCapability(code="AF", label="내지문(AF) 코팅", evidence=EvidenceLevel.L3,
                              basis="AF 코팅 접촉각 115° 시험성적서(KTR) · AF dip coater"),
        ],
        employees="약 60명",
        checks={
            "P1": ("met", L3, "광학용 HC 시트 시험성적서(연필경도 3H)"),
            "P2": ("met", L3, "AF 코팅 접촉각 115° 시험성적서(KTR)"),
            "P3": ("partial", L1, "레이저 가공 설비 없음(외주 처리)"),
            "E1": ("partial", L2, "Micro-gravure 코팅 라인 1기(R2R 방식 명시 없음)"),
            "E2": ("met", L2, "UV 경화 오븐 2기"),
            "E3": ("met", L2, "AF dip coater 1기"),
            "E4": ("unmet", None, "레이저 가공 설비 없음"),
            "C1": ("met", L3, "ISO 9001(TUV · 유효 2026.11)"),
            "C2": ("met", L2, "클린룸 Class 1,000"),
        },
        equipment=[
            "Micro-gravure 코팅 라인 1기",
            "UV 경화 오븐 2기",
            "AF(내지문) dip coater 1기",
            "레이저 가공 설비 없음(외주 처리)",
            "클린룸 Class 1,000",
        ],
        certifications=[
            "ISO 9001(TUV · 유효 2026.11)",
            "IATF 16949 심사 진행 중",
            "AF 코팅 접촉각 115° 시험성적서(KTR)",
        ],
    ),
}


def _checks(plan: dict, facts: dict) -> list[SpecCheck]:
    """확인된 사실(facts: id → (verdict, evidence, basis)) → 스펙 항목 판정. 없는 항목은 unknown."""
    out = []
    for c in plan["checks"]:
        v, ev, basis = facts.get(c["id"], ("unknown", None, "공개 근거 없음 · RFI 확인 필요"))
        out.append(SpecCheck(item_id=c["id"], group=c["group"], label=c["label"],
                             required=c["required"], verdict=v, evidence=ev, basis=basis))
    return out


def _oerlikon(plan: dict) -> SupplierRecord:
    """공장등록 검증 앵커 — 기존 라이브 실행에서 확인된 사업장(경북 경산 외)·생산품목."""
    return SupplierRecord(
        name="올리콘발저스코팅코리아(유)", country="KR", region="경상북도 경산시",
        address="경상북도 경산시 (공장등록)", kind=CompanyKind.MANUFACTURER, has_factory=True,
        products=["정밀 코팅·피막처리"],
        checks=_checks(plan, {
            "P1": ("partial", L2, "공장등록 생산품목 '정밀 코팅·피막처리' · 웹검색 광학/공구 코팅"),
            "P2": ("partial", L1, "내지문 코팅 관련 웹 언급 · 구체 증거 부족"),
        }),
        capabilities=[ProcessCapability(code="HC", label="하드코팅", evidence=L2),
                      ProcessCapability(code="AF", label="내지문(AF) 코팅", evidence=L1)],
        provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                extractor="structured", verified=True, note="공장등록 사업장 4곳 통합"),
                     Provenance(source_id="web_search", source_class=SourceClass.SPREAD,
                                extractor="llm", note="KR 웹검색 · AI 회사 추출")],
        notes=["[사업장 4개 통합]", "[확인 필요] 홈페이지 미확보 — RFI·현장 확인"],
    )


def _build_record(d: dict, plan: dict) -> SupplierRecord:
    """site_detail 확인값 → SupplierRecord. notes에 설비·인증·임직원을 근거로 남긴다."""
    notes: list[str] = [f"임직원 {d['employees']}"]
    notes += [f"[설비] {e}" for e in d["equipment"]]
    notes += [f"[인증] {c}" for c in d["certifications"]]
    prov = Provenance(
        source_id="site_detail",
        source_class=SourceClass.SPREAD,
        extractor="site_detail",
        url=d["homepage"],
        verified=False,  # 업체 자체 주장 — 앵커 교차검증/RFI 전
        note=f"홈페이지 상세 수집({d['domain']})",
    )
    return SupplierRecord(
        name=d["name"],
        homepage=d["homepage"],
        domain=d["domain"],
        country=d["country"],
        region=d["region"],
        address=d["address"],
        kind=d["kind"],
        products=d["products"],
        capabilities=d["capabilities"],
        contact_dept=d["contact_dept"],
        contact_public=d["contact_public"],
        employees=int("".join(ch for ch in d["employees"] if ch.isdigit())),
        checks=_checks(plan, d["checks"]),
        provenances=[prov],
        notes=notes,
    )


def main() -> None:
    spec = load_spec()
    plan = build_search_plan(spec)
    recs = [_build_record(_SITE_DETAIL["seojin"], plan), _build_record(_SITE_DETAIL["dongrim"], plan),
            _oerlikon(plan)]
    short = shortlist(score_all(recs, plan), top_n=6)
    rows = [_result_row(x) for x in short]

    out = {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "counts": {"pool": len(recs), "shortlist": len(rows)},
        "shortlist": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"재생성 완료 → {OUT_PATH.relative_to(ROOT)}")
    for r in rows:
        bd = " · ".join(f"{b['axis']} {b['points']}/{b['max']}" for b in r["breakdown"])
        print(f"  {r['score']:>3}  {r['name']}  (verified={r['verified']})  {bd}")


if __name__ == "__main__":
    main()
