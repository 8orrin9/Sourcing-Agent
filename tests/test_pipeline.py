"""파이프라인 결정적 단계 테스트 (오프라인·LLM/API 호출 없음).

discover(웹검색)·anchor_merge(팩토리온+LLM)·classify_all(LLM)은 네트워크·유료라
제외하고, 스코어링→숏리스트→결과직렬화의 결정적 조립만 검증한다.
"""

from __future__ import annotations

from src import pipeline
from src.schema import (
    CompanyKind,
    EvidenceLevel,
    ProcessCapability,
    Provenance,
    SourceClass,
    SupplierRecord,
)

_SPEC = {
    "request_id": "SR-TEST-001",
    "target_item": "PMMA 하드코팅 시트",
    "process_codes": {"HC": "하드코팅", "AF": "내지문"},
    "spec": {"region": {"preferred": ["국내"]}},
}


def _mfr(name, ev, *, kind=CompanyKind.MANUFACTURER, verified=True, country="KR"):
    return SupplierRecord(
        name=name, country=country, kind=kind,
        capabilities=[
            ProcessCapability(code="HC", label="하드코팅", evidence=ev),
            ProcessCapability(code="AF", label="내지문", evidence=ev),
        ],
        provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                extractor="structured", verified=verified)],
    )


def test_score_all_attaches_scoring():
    recs = [_mfr("A사", EvidenceLevel.L3), _mfr("B사", EvidenceLevel.L1)]
    scored = pipeline.score_all(recs, _SPEC)
    assert len(scored) == 2
    assert all("record" in x and "scoring" in x for x in scored)
    # L3 업체가 L1보다 높은 점수
    assert scored[0]["scoring"]["score"] > scored[1]["scoring"]["score"]


def test_shortlist_sorts_and_gates():
    recs = [
        _mfr("고점사", EvidenceLevel.L3),
        _mfr("저점사", EvidenceLevel.L1),
        _mfr("무역사", EvidenceLevel.L3, kind=CompanyKind.TRADER),  # 게이트 탈락
    ]
    short = pipeline.shortlist(pipeline.score_all(recs, _SPEC), top_n=6)
    names = [x["record"].name for x in short]
    assert names == ["고점사", "저점사"]  # 점수순, 무역사 제외
    assert "무역사" not in names


def test_shortlist_respects_top_n():
    recs = [_mfr(f"사{i}", EvidenceLevel.L2) for i in range(10)]
    short = pipeline.shortlist(pipeline.score_all(recs, _SPEC), top_n=3)
    assert len(short) == 3


def test_result_row_exposes_only_public_fields():
    x = pipeline.score_all([_mfr("공개사", EvidenceLevel.L3)], _SPEC)[0]
    row = pipeline._result_row(x)
    assert set(row) == {"name", "country", "region", "address", "domain", "kind",
                        "homepage", "verified", "employees", "equipment",
                        "certifications", "products", "contact_dept", "contact_public",
                        "capabilities", "score", "breakdown", "sources"}
    # 존 분리: 내부 단가·품목코드·현협력사명 같은 키는 없다 (추가 필드는 모두 외부 공개 정보)
    assert not ({"unit_price", "mat_code", "incumbent", "internal_price"} & set(row))
    assert row["verified"] is True
    assert row["sources"] == ["factorion"]


def test_run_requires_longlist_or_live():
    import pytest
    with pytest.raises(ValueError):
        pipeline.run(_SPEC)  # longlist 없고 live=False → 에러
