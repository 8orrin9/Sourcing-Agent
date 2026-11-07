"""LLM 폴백 추출기(4계층)의 결정적 로직 테스트 (오프라인·API 호출 없음).

LLM 호출(_extract 전체)은 비결정적·유료라 제외하고, HTML 정리(_clean)·URL
정규화(_safe_url)·레지스트리 등록 등 순수 로직만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

from src.extractors.llm_extract import LlmFallbackExtractor
from src.extractors.registry import registry

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "llm_extract"
    / "made_in_china_coating.html"
)


def test_registered_as_tier4_fallback():
    exts = {type(e).__name__: e for e in registry.all()}
    ext = exts["LlmFallbackExtractor"]
    assert ext.tier == 4  # 가장 비싼 계층 = 최종 폴백


def test_clean_strips_scripts_and_embeds_links():
    html = (
        "<html><head><style>.x{}</style></head><body>"
        "<script>var a=1;</script>"
        "<h2><a href='https://acme.com'>Acme Co.</a></h2>"
        "<p>hard coating film</p></body></html>"
    )
    text = LlmFallbackExtractor._clean(html)
    assert "var a=1" not in text  # 스크립트 제거
    assert ".x{}" not in text  # 스타일 제거
    assert "Acme Co." in text
    assert "<https://acme.com>" in text  # 링크 URL 임베드
    assert "hard coating film" in text


def test_clean_truncates_to_max_chars():
    from src.extractors.llm_extract import MAX_CHARS
    big = "<body>" + ("가" * (MAX_CHARS + 500)) + "</body>"
    assert len(LlmFallbackExtractor._clean(big)) <= MAX_CHARS


def test_safe_url_normalizes_and_rejects_garbage():
    f = LlmFallbackExtractor._safe_url
    assert f("//cdn.x.com/a", "") == "https://cdn.x.com/a"  # 프로토콜 상대
    assert f("/co/123", "https://dir.com/search") == "https://dir.com/co/123"  # 상대
    assert f("https://x.com", "") == "https://x.com"
    assert f("", "https://dir.com") is None  # 빈값
    assert f("문의하기", "https://dir.com") is None  # 비URL 텍스트


def test_real_html_cleans_without_error():
    """실제 픽스처(중문)를 정리해도 깨지지 않고 회사명이 남는다."""
    text = LlmFallbackExtractor._clean(FIXTURE.read_text(encoding="utf-8"))
    assert "Suzhou BrightFilm Optical Material Co., Ltd." in text
    assert "made-in-china.com" in text  # 링크 임베드됨
