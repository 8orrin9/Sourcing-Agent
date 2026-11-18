"""상세 수집 크롤 테스트 — 링크 우선순위·PDF 파싱·실패 내성 (오프라인, 가짜 Fetcher)."""

from __future__ import annotations

from pathlib import Path

from src.extractors import site_detail
from src.extractors.site_detail import SiteDetailExtractor

PDF = (Path(__file__).resolve().parents[1] / "data" / "fixtures" / "spec_doc" / "spec_sample.pdf").read_bytes()

HOME = """<html><body><nav>
<a href="/about">회사소개</a> <a href="/equipment">설비현황</a> <a href="/news/1">공지</a>
<a href="/cert">인증 · 품질</a> <a href="https://other.example/about">외부 사이트</a>
<a href="/files/ISO9001_certificate.pdf">ISO 인증서</a> <a href="/files/notice.pdf">안내문</a>
<a href="mailto:a@b.c">메일</a> <a href="/secret">비공개</a>
</nav><p>서진코팅 홈</p></body></html>"""

PAGES = {
    "https://seojin.example": HOME,
    "https://seojin.example/about": "<p>임직원 84명 · 오창 공장</p>",
    "https://seojin.example/equipment": "<p>R2R 코터 2기 · UV 경화기 3기</p>",
}


class FakeFetcher:
    def get(self, url, **kw):
        if url not in PAGES:
            raise PermissionError(f"robots.txt 차단: {url}")
        return PAGES[url]

    def get_bytes(self, url, **kw):
        if url.endswith("ISO9001_certificate.pdf"):
            return PDF
        raise FileNotFoundError(url)


def test_plan_links_prioritizes_and_stays_on_domain():
    pages, docs = SiteDetailExtractor.plan_links(HOME, "https://seojin.example")
    assert "https://other.example/about" not in pages            # 다른 도메인 제외
    assert "https://seojin.example/news/1" not in pages          # 키워드 없는 페이지 제외
    assert set(pages) == {"https://seojin.example/about", "https://seojin.example/equipment",
                          "https://seojin.example/cert"}
    assert docs[0].endswith("ISO9001_certificate.pdf")           # 인증서 PDF가 먼저


def test_crawl_collects_pages_and_pdf_and_survives_failures():
    got = SiteDetailExtractor(fetcher=FakeFetcher()).crawl("https://seojin.example")
    assert "https://seojin.example/equipment" in got["pages"]
    assert "https://seojin.example/cert" in got["failed"]       # robots 차단은 건너뜀
    assert any(u.endswith("notice.pdf") for u in got["failed"])
    assert got["docs"] == [{"name": "ISO9001_certificate.pdf",
                            "url": "https://seojin.example/files/ISO9001_certificate.pdf",
                            "chars": got["docs"][0]["chars"]}]
    assert "ISO 9001" in got["text"] and "R2R 코터" in got["text"]
    # 문서는 하위 페이지보다 앞에(절단 방지)
    assert got["text"].index("[문서:") < got["text"].index("R2R 코터")


def test_extract_from_homepage_records_docs_and_employees(monkeypatch):
    seen = {}

    def fake(**kw):
        seen["text"] = kw["user"]
        return {"name": "서진코팅", "country": "KR", "region": "충북 청주", "address": "",
                "is_manufacturer": True, "employees": "1,084명", "equipment": ["R2R 코터 2기"],
                "certifications": ["ISO 9001"], "products": ["하드코팅 시트"],
                "contact_dept": "영업팀", "contact_public": "", "summary": "하드코팅 제조사"}

    monkeypatch.setattr(site_detail, "complete_json", fake)
    rec = SiteDetailExtractor(fetcher=FakeFetcher()).extract({"homepage": "https://seojin.example"})[0]
    assert "연필경도 3H" in seen["text"]                         # PDF 본문이 판정 입력에 포함
    assert rec.employees == 1084
    assert "[문서] ISO9001_certificate.pdf (https://seojin.example/files/ISO9001_certificate.pdf)" in rec.notes
    assert rec.provenances[0].verified is False
