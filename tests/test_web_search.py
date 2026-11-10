"""웹검색(SerpApi) + LLM 회사 추출 테스트 (오프라인·픽스처).

parse_results는 실응답 저장본으로, LLM 추출은 고정 응답으로 바꿔 끼워 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.extractors.web_search import WebSearchExtractor
from src.llm import company_extract
from src.schema import CompanyKind, SourceClass

FIXTURE = (Path(__file__).resolve().parents[1] / "data" / "fixtures" / "web_search"
           / "serpapi_pmma_kr.json")


def _results():
    return WebSearchExtractor.parse_results(FIXTURE.read_text(encoding="utf-8"), locale="KR")


def test_parse_results_keeps_raw_fields_only():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = _results()
    assert len(res) == sum(1 for o in data["organic_results"] if o.get("link"))
    assert set(res[0]) == {"title", "link", "snippet", "domain", "aggregator", "locale"}
    # 회사명을 제목에서 추정하지 않는다
    assert "name" not in res[0]


def test_aggregator_flag():
    res = _results()
    ali = [r for r in res if "alibaba.com" in r["domain"]]
    assert ali and all(r["aggregator"] for r in ali)
    assert not any(r["aggregator"] for r in res if r["domain"] == "patents.google.com")


def test_extract_companies_drops_documents(monkeypatch):
    res = _results() + [{"title": "서진코팅 | 광학 하드코팅", "link": "https://www.seojincoat.co.kr",
                         "snippet": "오창 공장 R2R 코터", "domain": "seojincoat.co.kr",
                         "aggregator": False, "locale": "KR"}]
    last = len(res) - 1

    def fake(**kw):
        assert "[요청 품목] PMMA 하드코팅 시트" in kw["user"]
        return {
            "companies": [{"name": "서진코팅", "country": "KR", "kind": "manufacturer",
                           "official_site_index": last, "source_indexes": [last],
                           "evidence": "오창 공장 R2R 코터 언급"}],
            "dropped": [{"index": 0, "reason": "특허 문서"}, {"index": 2, "reason": "학회 초록"}],
        }

    monkeypatch.setattr(company_extract, "complete_json", fake)
    recs, dropped = company_extract.extract_companies(res, target="PMMA 하드코팅 시트")
    assert [r.name for r in recs] == ["서진코팅"]
    r = recs[0]
    assert r.kind is CompanyKind.MANUFACTURER and r.domain == "seojincoat.co.kr"
    assert r.provenances[0].source_class is SourceClass.SPREAD and not r.provenances[0].verified
    assert "AI 회사 추출" in r.provenances[0].note
    assert {d["reason"] for d in dropped} == {"특허 문서", "학회 초록"}


def test_marketplace_link_never_becomes_homepage(monkeypatch):
    res = _results()
    ali = next(i for i, r in enumerate(res) if r["aggregator"])
    monkeypatch.setattr(company_extract, "complete_json", lambda **kw: {
        "companies": [{"name": "Foshan X Acrylic Co., Ltd.", "country": "CN", "kind": "unknown",
                       "official_site_index": ali, "source_indexes": [ali], "evidence": "판매자"}],
        "dropped": []})
    recs, _ = company_extract.extract_companies(res)
    assert recs[0].homepage is None and recs[0].domain is None


def test_pick_homepage(monkeypatch):
    res = [{"title": "누리옵틱스 채용", "link": "https://jobs.example/nuri", "snippet": "",
            "domain": "jobs.example", "aggregator": False},
           {"title": "누리옵틱스", "link": "https://nurioptics.co.kr", "snippet": "안산 공장",
            "domain": "nurioptics.co.kr", "aggregator": False}]
    monkeypatch.setattr(company_extract, "complete_json", lambda **kw: {"index": 1, "reason": "자체 도메인"})
    url, why = company_extract.pick_homepage("누리옵틱스", "경기 안산", res)
    assert url == "https://nurioptics.co.kr" and why == "자체 도메인"
    monkeypatch.setattr(company_extract, "complete_json", lambda **kw: {"index": None, "reason": "확신 없음"})
    assert company_extract.pick_homepage("누리옵틱스", None, res) == (None, "확신 없음")
    assert company_extract.pick_homepage("누리옵틱스", None, [])[0] is None
