"""스코어링 결정적 로직 테스트 (오프라인·규칙 기반이라 완전 결정적).

점수는 LLM이 아니라 명시 규칙으로 계산하므로 입력→출력이 고정된다.
검색 계획(역할 분리)에 맞춘 축별 근거와 게이트(유통사·지역·규모·공정 근거)를 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.llm.score import WEIGHTS, pre_gate, score_supplier, sido
from src.schema import (
    CompanyKind,
    EvidenceLevel,
    Provenance,
    SourceClass,
    SpecCheck,
    SupplierRecord,
)
from src.search_plan import build_search_plan

SPEC = json.loads((Path(__file__).resolve().parents[1] / "data" / "synthetic"
                   / "parsed_spec_pmma.json").read_text(encoding="utf-8"))
PLAN = build_search_plan(SPEC)


def _checks(verdict="met", ev=EvidenceLevel.L3):
    return [SpecCheck(item_id=c["id"], group=c["group"], label=c["label"], required=c["required"],
                      verdict=verdict, evidence=ev if verdict != "unknown" else None)
            for c in PLAN["checks"]]


def _rec(**kw) -> SupplierRecord:
    base = dict(name="테스트사", kind=CompanyKind.MANUFACTURER, country="KR",
                region="충남 천안", address="충청남도 천안시 서북구", has_factory=True,
                employees=120, since_year=2001, checks=_checks(),
                provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                        extractor="structured", verified=True),
                             Provenance(source_id="kipris", source_class=SourceClass.ANCHOR,
                                        extractor="structured")])
    base.update(kw)
    return SupplierRecord(**base)


def test_full_match_scores_100():
    out = score_supplier(_rec(), PLAN)
    assert out["score"] == sum(WEIGHTS.values()) == 100
    assert out["passed_gate"] and out["gate_reasons"] == []


def test_breakdown_axes_and_reasons():
    out = score_supplier(_rec(employees=None, since_year=None), PLAN)
    axes = {b["axis"]: b for b in out["breakdown"]}
    assert set(axes) == set(WEIGHTS)
    assert "종업원 확인 필요" in axes["trust"]["reason"]
    assert axes["trust"]["points"] == 13                  # 공장등록 10 + 특허 3
    assert out["passed_gate"]                             # 모르는 값은 탈락시키지 않는다


def test_partial_and_low_evidence_reduce_process_points():
    full = score_supplier(_rec(), PLAN)["breakdown"][0]["points"]
    l1 = score_supplier(_rec(checks=_checks(ev=EvidenceLevel.L1)), PLAN)["breakdown"][0]["points"]
    part = score_supplier(_rec(checks=_checks(verdict="partial")), PLAN)["breakdown"][0]["points"]
    assert full == 35 and l1 < part < full


def test_gates():
    assert "유통·무역사" in score_supplier(_rec(kind=CompanyKind.TRADER), PLAN)["gate_reasons"]
    assert "허용 지역 밖(CN)" in score_supplier(_rec(country="CN"), PLAN)["gate_reasons"]
    assert "종업원 30명 < 50명" in score_supplier(_rec(employees=30), PLAN)["gate_reasons"]
    assert "필수 공정 근거 없음" in score_supplier(_rec(checks=_checks("unknown")), PLAN)["gate_reasons"]


def test_region_priority_vs_allowed():
    kr = score_supplier(_rec(), PLAN)["breakdown"][4]["points"]
    vn = score_supplier(_rec(country="VN", region="베트남 박닌", address=None,
                             has_factory=False), PLAN)
    assert kr == 10 and vn["breakdown"][4]["points"] == 5 and vn["passed_gate"]


def test_near_region_uses_sido_adjacency():
    # 아산(충남) 150km: 충남·충북(인접)은 근접, 부산은 아님
    pref = lambda addr: next(b for b in score_supplier(_rec(address=addr), PLAN)["breakdown"]
                             if b["axis"] == "preferred")["reason"]
    assert "근접" in pref("충청북도 청주시 흥덕구")
    assert "아님" in pref("부산광역시 강서구")
    assert sido("충청남도 아산시") == sido("충남 아산") == "충남"


def test_pre_gate_only_uses_known_values():
    assert pre_gate(_rec(employees=None, country=None), PLAN) is None
    assert pre_gate(_rec(employees=10), PLAN) == "종업원 10명 < 50명"
