"""LLM 계층의 결정적 로직 테스트 (오프라인·API 호출 없음).

LLM 호출 자체(classify/merge 판정·rfi 생성)는 비결정적·유료라 테스트에서 제외하고,
병합·승급의 순수 로직만 검증한다.
"""

from __future__ import annotations

from src.llm.merge import _norm_name, dedupe_factories, merge_into
from src.llm.rfi import apply_upgrades
from src.schema import (
    EvidenceLevel,
    ProcessCapability,
    Provenance,
    SourceClass,
    SupplierRecord,
)


def test_norm_name_strips_legal_forms():
    assert _norm_name("(주)서진코팅") == _norm_name("서진코팅(주)") == "서진코팅"
    assert _norm_name("Seojin Co., Ltd.") == "seojin"


def test_merge_into_fills_empty_and_accumulates():
    base = SupplierRecord(
        name="(주)서진코팅",
        region="충북 청주",
        has_factory=True,
        provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                extractor="structured", verified=True)],
    )
    other = SupplierRecord(
        name="서진코팅",
        domain="seojin.co.kr",
        homepage="http://seojin.co.kr",
        products=["광학 코팅"],
        notes=["PMMA 하드코팅"],
        provenances=[Provenance(source_id="web_search", source_class=SourceClass.SPREAD,
                                extractor="web_search")],
    )
    m = merge_into(base, other)
    assert m.domain == "seojin.co.kr"  # 빈 필드 채움
    assert m.region == "충북 청주"  # base 값 보존
    assert "광학 코팅" in m.products
    assert len(m.provenances) == 2  # 출처 누적
    assert m.provenances[0].verified is True  # 앵커 근거 유지


def test_dedupe_factories_folds_same_name():
    """같은 회사명의 여러 공장 → 1건으로 접고 사업장 수를 notes에 기록."""
    anchors = [
        SupplierRecord(name="올리콘발저스코팅코리아(유)", region="경북 경산",
                       provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                               extractor="structured", verified=True)]),
        SupplierRecord(name="올리콘발저스코팅코리아(유)", region="부산 강서",
                       provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                               extractor="structured", verified=True)]),
        SupplierRecord(name="(주)다른회사", region="서울",
                       provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                               extractor="structured", verified=True)]),
    ]
    out = dedupe_factories(anchors)
    assert len(out) == 2  # 올리콘 2건 → 1건, 다른회사 1건
    olicon = next(r for r in out if "올리콘" in r.name)
    assert any("사업장 2개 통합" in n for n in olicon.notes)
    assert len(olicon.provenances) == 2  # 두 공장 출처 누적


def test_apply_upgrades_only_raises_level():
    rec = SupplierRecord(
        name="x",
        capabilities=[
            ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L1),
            ProcessCapability(code="AF", label="내지문", evidence=EvidenceLevel.L2),
        ],
    )
    out = apply_upgrades(
        rec,
        [
            {"code": "HC", "new_evidence": "L3", "basis": "시험성적서"},
            {"code": "AF", "new_evidence": "L1", "basis": "하향은 무시"},
        ],
    )
    caps = {c.code: c.evidence for c in out.capabilities}
    assert caps["HC"] is EvidenceLevel.L3  # L1 → L3 승급
    assert caps["AF"] is EvidenceLevel.L2  # L2 → L1 하향은 무시(상향만)


def test_apply_upgrades_ignores_unknown_code():
    rec = SupplierRecord(
        name="x",
        capabilities=[ProcessCapability(code="HC", label="하드코팅", evidence=EvidenceLevel.L1)],
    )
    out = apply_upgrades(rec, [{"code": "ZZZ", "new_evidence": "L3", "basis": "없는 공정"}])
    assert len(out.capabilities) == 1  # 알 수 없는 코드는 무시
