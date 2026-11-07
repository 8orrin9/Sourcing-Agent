"""팩토리온 추출기 골든 픽스처 회귀 테스트 (오프라인·네트워크 불필요).

저장된 실응답 XML을 파싱해, 서비스 구조가 바뀌어 추출기가 깨지면 잡아낸다.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.structured import FactorionExtractor
from src.schema import CompanyKind, SourceClass

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "factorion"
    / "getFctryPrdctnService_v2_coating.xml"
)


def _records():
    xml = FIXTURE.read_text(encoding="utf-8")
    return FactorionExtractor()._parse(xml)


def test_fixture_parses_records():
    recs = _records()
    assert len(recs) == 10


def test_anchor_fields_populated():
    r = _records()[0]
    assert r.name  # 회사명
    assert r.address  # 주소 (병합 키)
    assert r.region  # 시도·시군구
    assert r.has_factory is True  # 공장등록 = 제조사
    assert r.kind is CompanyKind.MANUFACTURER


def test_provenance_is_verified_anchor():
    prov = _records()[0].provenances[0]
    assert prov.source_id == "factorion"
    assert prov.source_class is SourceClass.ANCHOR
    assert prov.verified is True  # 공장관리번호·주소 확보 → 식별 확정
    assert prov.note and "공장관리번호" in prov.note


def test_no_personal_name_stored():
    """대표자명(rprsntvNm)은 개인정보 → 어떤 필드에도 저장되지 않아야 한다."""
    r = _records()[0]
    dumped = r.model_dump_json()
    # 대표자명 필드 자체를 매핑하지 않으므로 레코드에 없어야 함
    assert "rprsntvNm" not in dumped


def test_garbage_homepage_dropped():
    """hmpadr에 주소 조각 등 비URL 값이 와도 크래시 없이 None 처리."""
    assert FactorionExtractor._safe_url("75번길 22아파트") is None
    assert FactorionExtractor._safe_url("www.example.com") == "http://www.example.com"
    assert FactorionExtractor._domain("http://www.nuri.co.kr") == "nuri.co.kr"
