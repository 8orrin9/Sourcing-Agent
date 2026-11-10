"""상세 수집 사다리·근거 검증 테스트 (오프라인).

PMMA 라이브 실행(2026-11-10)에서 드러난 문제의 재발 방지:
- 수집 실패인데 '설비 사양 확보'로 판정 → 인용 검증으로 unknown
- 특허 제목만으로 공정 L2 → 출처별 상한 L1
- 채용공고·계열사 사이트를 홈페이지로 채택 → 차단 도메인·회사명 대조
- 홈페이지 차단 시 근거 0 → 검색 경유 근거
"""

from __future__ import annotations

import httpx
import pytest

from src import pipeline
from src.core.fetcher import classify_error
from src.extractors import search_evidence
from src.extractors.site_detail import CrawlError, SiteDetailExtractor
from src.extractors.web_search import is_non_homepage
from src.llm.classify import available_sources, verify_check
from src.llm.merge import dedupe_factories
from src.schema import CompanyKind, EvidenceLevel, Provenance, SourceClass, SupplierRecord

L1, L2, L3 = EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3


def _rec(**kw):
    base = dict(name="(주)누리코팅", country="KR", has_factory=True, products=["하드코팅필름"],
                provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                        extractor="structured", verified=True)])
    base.update(kw)
    return SupplierRecord(**base)


def _c(verdict="met", evidence="L2", source="patent", quote="하드코팅 필름"):
    return {"verdict": verdict, "evidence": evidence, "source": source, "quote": quote, "basis": "b"}


# --- 근거 검증(원인2) -----------------------------------------------------------
def test_patent_title_capped_at_l1():
    rec = _rec(notes=["[특허] 하드코팅 필름 및 제조방법"])
    text = "생산품/업종: 하드코팅필름\n수집 근거:\n  - [특허] 하드코팅 필름 및 제조방법"
    v, ev, basis = verify_check(_c(evidence="L2"), rec, text)
    assert (v, ev) == ("met", L1) and "출처 patent" in basis


def test_source_not_collected_becomes_unknown():
    rec = _rec(notes=["[특허] 하드코팅 필름"])           # 홈페이지 수집 없음
    v, ev, basis = verify_check(_c(source="homepage", quote="하드코팅 필름"), rec, "[특허] 하드코팅 필름")
    assert (v, ev) == ("unknown", None) and "출처 확인 불가" in basis


def test_quote_not_in_text_becomes_unknown():
    rec = _rec(notes=["[인증] ISO 14001"],
               provenances=[Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                       extractor="site_detail")])
    v, ev, basis = verify_check(_c(source="homepage", quote="ISO 9001 인증서"), rec, "[인증] ISO 14001")
    assert (v, ev) == ("unknown", None) and "인용 확인 실패" in basis


def test_homepage_capped_l2_document_allows_l3():
    rec = _rec(notes=["[설비] R2R 코터 2기", "[문서] iso9001.pdf (https://a.example/iso9001.pdf)"],
               provenances=[Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                       extractor="site_detail")])
    text = "\n".join(rec.notes)
    assert verify_check(_c(evidence="L3", source="homepage", quote="R2R 코터 2기"), rec, text)[1] is L2
    assert verify_check(_c(evidence="L3", source="document", quote="iso9001.pdf"), rec, text)[1] is L3
    assert available_sources(rec) == {"factory_registry", "homepage", "document"}


def test_misattributed_source_is_corrected():
    # 특허 제목을 '공장등록' 출처로 댄 경우(라이브 실행에서 관찰) → 실제 출처 patent로 바로잡음
    rec = _rec(notes=["[특허] 고기능성 AF 하드코팅 필름"])
    v, ev, basis = verify_check(_c(source="factory_registry", quote="고기능성 AF 하드코팅 필름", evidence="L1"),
                                rec, "생산품/업종: 하드코팅필름\n  - [특허] 고기능성 AF 하드코팅 필름")
    assert (v, ev) == ("met", L1) and "출처 patent" in basis


def test_quote_ignores_spacing_and_quotes():
    rec = _rec(notes=["[검색근거] 누리코팅 — 내지문 AF 코팅 라인 증설 <https://n.example>"])
    v, ev, _ = verify_check(_c(source="search", quote="'내지문AF 코팅라인'"), rec, rec.notes[0])
    assert (v, ev) == ("met", L1)


# --- 검색 경유 근거 -------------------------------------------------------------
def test_search_notes_keep_own_domain_and_name_mentions_only():
    res = [
        {"title": "설비 현황", "link": "https://nuricoat.co.kr/facility", "snippet": "UV 경화기 3대"},
        {"title": "누리코팅, 하드코팅 라인 증설", "link": "https://news.example/a", "snippet": "…"},
        {"title": "누리코팅 기업 정보", "link": "https://info.example/c", "snippet": "해양 기자재 전문"},
        {"title": "다른회사 하드코팅", "link": "https://other.example/b", "snippet": "하드코팅 전문"},
        {"title": "카탈로그", "link": "https://nuricoat.co.kr/catalog.pdf", "snippet": "제품 사양"},
        {"title": "누리코팅 특허", "link": "https://patents.google.com/x", "snippet": "누리코팅"},
    ]
    notes, pdfs = search_evidence.to_notes("(주)누리코팅", res, domain="nuricoat.co.kr", keywords=["하드코팅"])
    assert len(notes) == 3 and all(n.startswith("[검색근거]") for n in notes)
    assert not any(d in n for n in notes for d in ("other.example", "patents.google", "info.example"))
    assert pdfs == ["https://nuricoat.co.kr/catalog.pdf"]


def test_search_queries_use_domain_then_name():
    qs = search_evidence.build_queries("(주)누리코팅", domain="nuricoat.co.kr",
                                       keywords=["하드코팅", "내지문"])
    # 짧은 검색어 — OR을 길게 붙이면 구글이 site: 조건을 무시했다(라이브 확인)
    assert qs == ["site:nuricoat.co.kr", "누리코팅 하드코팅"]


# --- 홈페이지 확정·실패 분류 ------------------------------------------------------
def test_non_homepage_domains():
    assert is_non_homepage("https://lab.incruit.com/jobs/2610160002935")
    assert is_non_homepage("www.saramin.co.kr/x") and is_non_homepage("https://blog.naver.com/a")
    assert not is_non_homepage("https://www.nuricoat.co.kr") and not is_non_homepage(None)


def test_classify_error():
    req = httpx.Request("GET", "https://a.example")
    assert classify_error(httpx.ProxyError("403 Forbidden")) == "blocked"
    assert classify_error(httpx.HTTPStatusError("x", request=req,
                                                response=httpx.Response(403, request=req))) == "http_403"
    assert classify_error(httpx.ConnectError("[Errno -2] Name or service not known")) == "dns"
    assert classify_error(httpx.ReadTimeout("t")) == "timeout"
    assert classify_error(PermissionError("robots")) == "robots"


class _F:
    """가짜 Fetcher: get 실패 종류·render 가능 여부를 지정."""

    def __init__(self, get_exc=None, html="", render_html=None):
        self.get_exc, self.html, self.render_html, self.rendered = get_exc, html, render_html, False

    def get(self, url):
        if self.get_exc:
            raise self.get_exc
        return self.html

    def render(self, url):
        self.rendered = True
        if self.render_html is None:
            raise ImportError("playwright 미설치")
        return self.render_html


def _status(code):
    req = httpx.Request("GET", "https://a.example")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


def test_fetch_home_renders_on_bot_block_only():
    big = "<p>" + "회사 소개 " * 100 + "</p>"
    f = _F(get_exc=_status(403), render_html=big)
    html, rendered = SiteDetailExtractor(fetcher=f)._fetch_home("https://a.example")
    assert rendered and html == big
    blocked = _F(get_exc=httpx.ProxyError("403 Forbidden"), render_html=big)
    with pytest.raises(CrawlError) as ei:
        SiteDetailExtractor(fetcher=blocked)._fetch_home("https://a.example")
    assert ei.value.reason == "blocked" and not blocked.rendered   # 환경 차단은 렌더링해도 같음


def test_fetch_home_short_page_falls_back_when_render_unavailable():
    f = _F(html="<p>짧은 첫 화면</p>")                          # JS 앱 의심, 렌더링 불가
    html, rendered = SiteDetailExtractor(fetcher=f)._fetch_home("https://a.example")
    assert f.rendered and not rendered and "짧은" in html


# --- 대표 공장(엘지화학 익산 의약품 공장 사례) -----------------------------------
def test_dedupe_prefers_relevant_factory_as_representative():
    pharma = _rec(name="(주)엘지화학", address="전북 익산시", products=["의약품"], homepage="http://www.lgls.co.kr")
    film = _rec(name="(주)엘지화학", address="충북 청주시", products=["필름소재", "플라스틱 시트"],
                homepage="http://www.lgchem.com")
    out = dedupe_factories([pharma, film], prefer=["광학필름", "아크릴시트", "하드코팅"])
    assert len(out) == 1 and out[0].address == "충북 청주시" and str(out[0].homepage).startswith("http://www.lgchem")
    assert dedupe_factories([pharma, film])[0].address == "전북 익산시"   # prefer 없으면 기존 동작


# --- 파이프라인 상세 수집 사다리 ------------------------------------------------
class _Web:
    def __init__(self):
        self.qs = []

    def search(self, q, *, locale, num):
        self.qs.append(q)
        return [{"title": "누리코팅 내지문 코팅 라인", "link": "https://news.example/n", "snippet": "AF 코팅",
                 "domain": "news.example", "aggregator": False, "locale": locale}]


class _Site:
    def __init__(self, exc=None, name="누리코팅"):
        self.exc, self.name = exc, name

    def extract(self, query):
        if self.exc:
            raise self.exc
        return [SupplierRecord(name=self.name, notes=["회사 소개"],
                               provenances=[Provenance(source_id="site_detail", source_class=SourceClass.SPREAD,
                                                       extractor="site_detail")])]


def test_enrich_falls_back_to_search_evidence_when_blocked():
    log = pipeline.Log()
    web = _Web()
    rec = _rec(homepage="https://nuricoat.co.kr")
    out = pipeline.enrich_sites([rec], web, _Site(exc=CrawlError("blocked", "403")), log, keywords=["내지문"])
    assert any(n.startswith("[검색근거]") for n in out[0].notes)
    assert any(q.startswith("site:nuricoat.co.kr") for q in web.qs)
    assert any("실행 환경 네트워크 차단 1곳" in l["msg"] for l in log)


def test_enrich_rejects_other_company_site(monkeypatch):
    monkeypatch.setattr(pipeline, "is_same_company",
                        lambda a, b: {"same_company": False, "confidence": "high", "reason": "계열사"})
    log = pipeline.Log()
    out = pipeline.enrich_sites([_rec(homepage="https://lgls.example")], _Web(),
                                _Site(name="LG화학 생명과학"), log, keywords=["하드코팅"])
    r = out[0]
    assert not any(p.source_id == "site_detail" for p in r.provenances)
    assert any("회사명 불일치" in n for n in r.notes)
    assert any("회사명 불일치 1곳" in l["msg"] for l in log)


def test_enrich_drops_job_site_homepage(monkeypatch):
    monkeypatch.setattr(pipeline, "pick_homepage", lambda name, region, res: (None, "확신 없음"))
    out = pipeline.enrich_sites([_rec(homepage="https://lab.incruit.com/jobs/1")], _Web(), _Site(),
                                pipeline.Log(), keywords=["하드코팅"])
    assert out[0].homepage is None and any("외부 사이트" in n for n in out[0].notes)


def test_priority_ignores_size_and_counts_discovery_paths():
    big = _rec(name="대기업", employees=5000, notes=["[발견] 특허 출원인 → 공장등록 확인"])
    small = _rec(name="소규모", employees=8, notes=["[발견] 생산품 검색어 '하드코팅'",
                                                   "[발견] 생산품 검색어 '아크릴시트'",
                                                   "[발견] 특허 출원인 → 공장등록 확인"])
    assert [r.name for r in sorted([big, small], key=pipeline._priority)] == ["소규모", "대기업"]
