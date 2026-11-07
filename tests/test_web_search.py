"""웹검색(SerpApi) 추출기 파싱 테스트 (오프라인·픽스처).

라이브 호출과 동일한 _parse를 검증한다. 키가 없어도 로직은 테스트된다.
픽스처는 SerpApi 실응답(organic_results) 저장본.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.extractors.web_search import WebSearchExtractor
from src.schema import SourceClass

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "web_search"
    / "serpapi_pmma_kr.json"
)


def _records():
    body = FIXTURE.read_text(encoding="utf-8")
    return WebSearchExtractor(api_key="dummy")._parse(body, locale_key="KR")


def test_parses_organic_results():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recs = _records()
    # organic_results 중 link 있는 항목 수와 일치
    expected = sum(1 for o in data["organic_results"] if o.get("link"))
    assert len(recs) == expected
    assert recs  # 비어있지 않음


def test_all_spread_and_unverified():
    for r in _records():
        prov = r.provenances[0]
        assert prov.source_class is SourceClass.SPREAD
        assert prov.verified is False  # 웹검색은 앵커 흡착 전까지 미검증
        assert "웹검색" in (prov.note or "")


def test_company_name_stripped_from_title():
    """제목의 구분자(' | ', ' - ') 앞 토막만 회사명으로."""
    assert WebSearchExtractor._company_name("서진코팅 - 광학용 하드코팅") == "서진코팅"
    assert WebSearchExtractor._company_name("누리옵틱스 | 아크릴 시트") == "누리옵틱스"
    assert WebSearchExtractor._company_name("") == "(미상)"


def test_aggregator_domain_dropped():
    """집계 도메인(alibaba/made-in-china 등)은 link 기준으로 판정해 homepage/domain 비움."""
    body = json.dumps(
        {
            "organic_results": [
                {"title": "PMMA sheet - Made-in-China.com",
                 "link": "https://www.made-in-china.com/x", "snippet": "s"},
                {"title": "서진코팅", "link": "http://seojincoating.co.kr", "snippet": "s"},
            ]
        }
    )
    recs = WebSearchExtractor(api_key="x")._parse(body, locale_key="EN")
    mic = next(r for r in recs if "made-in-china" in str(r.provenances[0].url or ""))
    own = next(r for r in recs if r.name == "서진코팅")
    assert mic.domain is None and mic.homepage is None
    assert "디렉터리/집계" in mic.provenances[0].note
    assert own.domain == "seojincoating.co.kr" and own.homepage is not None
