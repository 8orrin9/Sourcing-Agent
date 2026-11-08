"""수집 증거 콘솔 실데이터 재생성 — Best-Case 하이브리드(결정적·오프라인).

콘솔(데모 STEP 1)에 올리는 숏리스트 3사를 결정적으로 재생성한다. 라이브 재실행
(run(live=True))은 LLM 비결정·외부 API·네트워크를 요구하므로 데모 아티팩트로 부적합해
쓰지 않는다. 대신:

- 서진코팅·동림첨단소재: site_detail 라이브 수집에서 이미 확인된 값
  (data/fixtures/site_detail/*.html)으로 SupplierRecord를 직접 구성 → 결정적
  score_supplier로 채점. LLM/크롤 단계만 '이미 확인된 사실'로 대체하고, 점수·게이트·
  평탄화는 실제 파이프라인 함수(score_all·shortlist·_result_row)를 그대로 쓴다.
- 올리콘발저스코팅코리아(유): 기존 라이브 산출(shortlist_SR-2026-1025-014.json)의
  검증 앵커 행을 그대로 로드(공공DB verified=True). 상세 필드는 공개 근거가 없어
  콘솔/상세수집 화면에서 '논리적 보완'으로 음영 표기한다(여기선 비워 둠).

존 분리: 외부 공개 필드만. 내부 단가·품목코드·현 협력사명 없음.
개인정보 비저장: 담당 '부서'와 대표 연락처만(개인 이름 없음).

실행: python scripts/regen_evidence.py
재실행해도 동일 JSON(결정적). 점수: 동림 85 · 서진 78 · 올리콘 77.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm.score import score_supplier
from src.pipeline import _result_row, load_spec
from src.schema import (
    CompanyKind,
    EvidenceLevel,
    ProcessCapability,
    Provenance,
    SourceClass,
    SupplierRecord,
)

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


def _build_record(d: dict) -> SupplierRecord:
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
        provenances=[prov],
        notes=notes,
    )


def _score_row(rec: SupplierRecord, spec: dict) -> dict:
    processes = list(spec.get("process_codes", {}))
    preferred = spec.get("spec", {}).get("region", {}).get("preferred", [])
    scoring = score_supplier(rec, required_processes=processes, preferred_regions=preferred)
    return _result_row({"record": rec, "scoring": scoring})


# 상세 수집 화면이 읽는 필드(올리콘처럼 상세가 공개 근거 없는 행에서도 키 존재 보장)
_DETAIL_KEYS = ("address", "domain", "employees", "equipment", "certifications",
                "products", "contact_dept", "contact_public")


def _load_oerlikon() -> dict:
    """기존 라이브 산출에서 올리콘(검증 앵커) 행을 로드. 상세 필드는 비워 둔다
    (공개 근거 없음 → 콘솔/상세수집에서 '논리적 보완' 음영 표기)."""
    prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    row = next(r for r in prev["shortlist"] if r["name"].startswith("올리콘"))
    for k in _DETAIL_KEYS:
        row.setdefault(k, [] if k in ("equipment", "certifications", "products") else None)
    return row


def main() -> None:
    spec = load_spec()
    rows = [_score_row(_build_record(_SITE_DETAIL["seojin"]), spec),
            _score_row(_build_record(_SITE_DETAIL["dongrim"]), spec),
            _load_oerlikon()]
    rows.sort(key=lambda r: r["score"], reverse=True)

    out = {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "counts": {"longlist": 13, "anchored": 1, "leftover": 12, "shortlist": len(rows)},
        "shortlist": rows,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"재생성 완료 → {OUT_PATH.relative_to(ROOT)}")
    for r in rows:
        print(f"  {r['score']:>3}  {r['name']}  (verified={r['verified']})")


if __name__ == "__main__":
    main()
