"""설정형(2계층) 디렉터리 추출기 파싱 테스트 (오프라인·픽스처).

라이브 호출과 동일한 _parse를 검증한다. 네트워크 없이 YAML 셀렉터·정규화만 테스트.
픽스처는 EC21 product-search 결과 페이지 구조 모사본.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.config_driven import ConfigDrivenExtractor, load_configs
from src.schema import SourceClass

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "config_driven"
    / "ec21_hardcoating.html"
)


def _ec21() -> ConfigDrivenExtractor:
    return {e.source_id: e for e in load_configs()}["ec21"]


def _records():
    html = FIXTURE.read_text(encoding="utf-8")
    return _ec21()._parse(html, base_url="https://www.ec21.com/product-search/x.html", locale_key="EN")


def test_configs_load_from_yaml():
    """config/*.yaml이 로드돼 추출기 인스턴스가 된다(신규 사이트 = YAML 추가)."""
    ids = {e.source_id for e in load_configs()}
    assert {"ec21", "gobizkorea"} <= ids
    for e in load_configs():
        assert e.tier == 2
        assert e.source_class is SourceClass.SPREAD


def test_parses_company_blocks():
    recs = _records()
    names = {r.name for r in recs}
    # 광고(회사명 없는) 블록은 제외 → 3건
    assert len(recs) == 3
    assert "Sunrise Optical Film Co., Ltd." in names


def test_all_spread_and_unverified():
    for r in _records():
        prov = r.provenances[0]
        assert prov.source_class is SourceClass.SPREAD
        assert prov.verified is False  # 디렉터리는 앵커 흡착 전까지 미검증
        assert "디렉터리(ec21)" in (prov.note or "")


def test_relative_and_protocol_urls_absolutized():
    recs = {r.name: r for r in _records()}
    # 상대경로 /global/... → 절대 URL
    assert str(recs["Sunrise Optical Film Co., Ltd."].homepage).startswith("https://www.ec21.com/global/")
    # 프로토콜 상대 //... → https
    assert str(recs["Minh An Optical JSC"].homepage).startswith("https://minhan-vn")


def test_country_and_snippet_extracted():
    recs = {r.name: r for r in _records()}
    sz = recs["Shenzhen ClearCoat Tech"]
    assert sz.country == "China"
    assert sz.notes and "AF coating" in sz.notes[0]
