"""KIPRIS 추출기 골든 픽스처 회귀 테스트 (오프라인·네트워크 불필요).

저장된 실응답 XML(getAdvancedSearch·출원인명 '코팅코리아')을 파싱해, 서비스 구조가
바뀌어 추출기가 깨지면 잡아낸다.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.kipris import KiprisExtractor
from src.schema import CompanyKind, SourceClass

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "kipris"
    / "getAdvancedSearch_coating.xml"
)


def _records():
    xml = FIXTURE.read_text(encoding="utf-8")
    return KiprisExtractor()._parse(xml, "코팅코리아")


def test_multiple_patents_fold_to_one_record():
    """한 출원인의 특허 여러 건 → 1개 레코드로 집계(출원인 = 기업 1개)."""
    recs = _records()
    assert len(recs) == 1
    r = recs[0]
    assert r.name == "주식회사 코팅코리아"  # 응답의 출원인명(법인격 포함) 사용
    assert len(r.products) == 5  # numOfRows=5 대표 특허 제목


def test_semi_anchor_not_verified():
    """준앵커: 주소·사업자번호 미제공 → verified=False, kind 미확정."""
    r = _records()[0]
    prov = r.provenances[0]
    assert prov.source_id == "kipris"
    assert prov.source_class is SourceClass.ANCHOR
    assert prov.verified is False  # 식별 미확정 — 판정은 팩토리온/LLM 몫
    assert r.kind is CompanyKind.UNKNOWN


def test_patent_count_in_provenance():
    """provenance note에 totalCount(보유 특허 수)가 담긴다."""
    r = _records()[0]
    assert "특허 37건 보유" in r.provenances[0].note


def test_titles_and_ipc_as_evidence():
    """특허 제목은 products·notes에, IPC 상위분류는 notes에 — classify 근거."""
    r = _records()[0]
    assert any(n.startswith("[특허]") for n in r.notes)
    ipc_note = next((n for n in r.notes if n.startswith("[IPC]")), None)
    assert ipc_note and "B05D" in ipc_note  # 코팅 관련 IPC 클래스


def test_tier1_registered():
    """레지스트리에 1계층(structured) 앵커로 등록됐는지."""
    from src.extractors.registry import registry
    import src.extractors.kipris  # noqa: F401 — import 시 등록

    exts = registry.for_source("kipris")
    assert exts and exts[0].tier == 1
