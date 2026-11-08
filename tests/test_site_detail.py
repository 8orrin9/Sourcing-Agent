"""업체 상세 수집 추출기 테스트 (오프라인 — _clean·등록만, LLM 호출 제외).

LLM 추출 자체는 비결정적·유료라 테스트하지 않는다(라이브 수동 검증). 여기서는
구조가 다른 두 사이트에서 _clean이 텍스트를 뽑아내는지(구조 무관성)와 레지스트리
등록·스키마 계약을 본다.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.site_detail import MAX_CHARS, SiteDetailExtractor

FIX = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "site_detail"


def _clean(name: str) -> str:
    return SiteDetailExtractor._clean((FIX / name).read_text(encoding="utf-8"))


def test_clean_strips_scripts_and_keeps_meaning_modern():
    """현대적 div/section 사이트: 스크립트 제거 + 의미 텍스트 보존."""
    out = _clean("seojin_about.html")
    assert "var ga" not in out  # <script> 제거
    assert "서진코팅" in out
    assert "R2R" in out and "클린룸" in out  # 설비 텍스트 보존
    assert "ISO 9001" in out  # 인증 텍스트 보존


def test_clean_works_on_legacy_table_layout():
    """구식 table 레이아웃 사이트(구조가 완전히 다름)도 같은 _clean으로 처리."""
    out = _clean("dongrim_company.html")
    assert "동림첨단소재" in out
    assert "UV curing oven" in out  # <td> 안 텍스트 추출
    assert "ISO 9001" in out


def test_clean_embeds_anchor_urls():
    """앵커 href를 텍스트 옆에 심어 LLM이 연락/링크를 집도록."""
    out = _clean("seojin_about.html")
    assert "<mailto:sales@seojincoat.co.kr>" in out


def test_clean_truncates_to_max_chars():
    big = "<html><body>" + "가" * (MAX_CHARS + 5000) + "</body></html>"
    assert len(SiteDetailExtractor._clean(big)) <= MAX_CHARS


def test_registered_as_tier4():
    from src.extractors.registry import registry
    import src.extractors.site_detail  # noqa: F401 — import 시 등록

    exts = registry.for_source("site_detail")
    assert exts and exts[0].tier == 4


def test_safe_url_normalizes():
    f = SiteDetailExtractor._safe_url
    assert f("//cdn.x.com/a", "http://x.com") == "https://cdn.x.com/a"
    assert f("/about", "http://x.com/home") == "http://x.com/about"
    assert f("mailto:a@b.com", "http://x.com") is None  # 비URL은 None
