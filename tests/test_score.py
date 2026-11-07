"""스코어링 결정적 로직 테스트 (오프라인·규칙 기반이라 완전 결정적).

점수는 LLM이 아니라 명시 규칙으로 계산하므로 입력→출력이 고정된다.
숏리스트 선정 근거의 설명가능성(breakdown)과 게이트(유통사·무역량 0 탈락)를 검증.
"""

from __future__ import annotations

from src.llm.score import WEIGHTS, score_supplier
from src.schema import (
    CompanyKind,
    EvidenceLevel,
    ProcessCapability,
    Provenance,
    SourceClass,
    SupplierRecord,
)


def _rec(**kw) -> SupplierRecord:
    base = dict(name="테스트사", kind=CompanyKind.MANUFACTURER)
    base.update(kw)
    return SupplierRecord(**base)


def test_full_spec_match_scores_high():
    rec = _rec(
        country="KR",
        region="충북 청주",
        capabilities=[
            ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L3),
            ProcessCapability(code="AF", label="내지문", evidence=EvidenceLevel.L3),
        ],
        provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                extractor="structured", verified=True)],
        notes=["종업원 120명"],
    )
    out = score_supplier(rec, required_processes=["HC", "AF"], preferred_regions=["국내"])
    # 모든 축 만점 → 100
    assert out["score"] == sum(WEIGHTS.values()) == 100
    assert out["passed_gate"] is True


def test_breakdown_is_explainable():
    rec = _rec(
        capabilities=[ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L1)],
    )
    out = score_supplier(rec, required_processes=["HC", "AF"])
    axes = {b["axis"] for b in out["breakdown"]}
    assert axes == {"capability", "manufacturer", "verified", "region", "scale"}
    # 각 항목에 사유 문자열이 붙는다
    assert all(b["reason"] for b in out["breakdown"])
    # HC L1만 보유 → capability 부분점수(만점 미만)
    cap = next(b for b in out["breakdown"] if b["axis"] == "capability")
    assert 0 < cap["points"] < WEIGHTS["capability"]


def test_trader_fails_gate():
    rec = _rec(
        kind=CompanyKind.TRADER,
        capabilities=[ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L2)],
    )
    out = score_supplier(rec, required_processes=["HC"])
    assert out["passed_gate"] is False  # 유통사는 게이트 탈락


def test_no_capability_fails_gate():
    rec = _rec(capabilities=[])
    out = score_supplier(rec, required_processes=["HC", "AF"])
    assert out["passed_gate"] is False  # 요청 공정 하나도 없으면 탈락


def test_unverified_spread_loses_verified_points():
    rec = _rec(
        capabilities=[ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L2)],
        provenances=[Provenance(source_id="web_search", source_class=SourceClass.SPREAD,
                                extractor="web_search", verified=False)],
    )
    out = score_supplier(rec, required_processes=["HC"])
    ver = next(b for b in out["breakdown"] if b["axis"] == "verified")
    assert ver["points"] == 0  # 미검증 → 검증 가점 없음
