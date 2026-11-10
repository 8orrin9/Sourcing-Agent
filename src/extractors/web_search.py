"""확산 추출기 — 다국어 웹검색 (SerpApi / Google Search).

역할(2026-11-10 전환): ① 공장등록 후보의 공식 홈페이지 찾기(상세 수집 입구),
② 해외·공장등록 공백을 메우는 보조 발견. 결과에서 회사명을 제목 자르기로 추정하지 않고,
검색 직후 LLM(llm/company_extract)이 실제 기업만 추출한다 — SourceClass.SPREAD.

존 분리: 검색 쿼리에 내부 품목코드·단가·현 협력사명을 넣지 않는다(공정·소재 키워드만).
SerpApi(https://serpapi.com/search) GET + api_key 파라미터, 응답은
organic_results[].{title,link,snippet}. 라이브 호출과 픽스처 파싱이 같은 parse_results를
쓴다(키 없을 때도 로직 검증 가능).
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx

from ..schema import SourceClass, SupplierRecord
from .base import Extractor
from .registry import registry

SERPAPI_URL = "https://serpapi.com/search"

# 국가·언어 코드 (데모 다국어 쿼리: 한·영·중·베)
LOCALE = {
    "KR": {"gl": "kr", "hl": "ko"},
    "EN": {"gl": "us", "hl": "en"},
    "ZH": {"gl": "cn", "hl": "zh-cn"},
    "VI": {"gl": "vn", "hl": "vi"},
}

# 후보에서 걸러낼 디렉터리·마켓플레이스 도메인(제조사 자체 사이트가 아님 → 회사명 추출만)
AGGREGATOR_DOMAINS = {
    "alibaba.com", "made-in-china.com", "globalsources.com", "ec21.com",
    "tradekorea.com", "gobizkorea.com", "indiamart.com", "linkedin.com",
    "facebook.com", "youtube.com", "wikipedia.org",
}


class WebSearchExtractor(Extractor):
    """SerpApi 웹검색. query={'q': 검색어, 'locale': 'KR'|'EN'|'ZH'|'VI', 'num': N, 'target': 품목}.

    extract()는 검색 직후 LLM으로 실제 기업만 추출해 SupplierRecord로 돌려준다.
    search()는 원시 결과(제목·URL·스니펫)를 돌려준다(공식 홈페이지 판별 등에 사용).
    """

    tier = 2
    source_id = "web_search"
    source_class = SourceClass.SPREAD

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("SERPER_API_KEY")
        self.timeout = timeout

    def search(self, q: str, *, locale: str = "KR", num: int = 10) -> list[dict]:
        if not q:
            raise ValueError("web_search에는 검색어(q)가 필요합니다")
        if not self.api_key:
            raise RuntimeError("SERPER_API_KEY 미설정 — 라이브 검색 불가 (픽스처는 parse_results 사용)")
        resp = httpx.get(
            SERPAPI_URL,
            params={"engine": "google", "q": q, "num": num, "api_key": self.api_key,
                    **LOCALE.get(locale, LOCALE["KR"])},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self.parse_results(resp.text, locale=locale)

    def extract(self, query: dict) -> list[SupplierRecord]:
        from ..llm.company_extract import extract_companies

        results = self.search(query.get("q", ""), locale=query.get("locale", "KR"),
                              num=int(query.get("num", 10)))
        records, _dropped = extract_companies(results, target=query.get("target", ""))
        return records

    @staticmethod
    def parse_results(body: str | dict, *, locale: str = "KR") -> list[dict]:
        """SerpApi 응답 → 원시 결과 목록. 회사명은 여기서 추정하지 않는다(LLM 몫)."""
        data = json.loads(body) if isinstance(body, str) else body
        out = []
        for o in data.get("organic_results", []):
            link = o.get("link")
            if not link:
                continue
            domain = urlparse(link).netloc.replace("www.", "")
            out.append({
                "title": o.get("title") or "",
                "link": link,
                "snippet": o.get("snippet") or "",
                "domain": domain,
                "aggregator": any(domain.endswith(d) for d in AGGREGATOR_DOMAINS),
                "locale": locale,
            })
        return out


# 레지스트리 등록 (import 시 1회)
registry.register(WebSearchExtractor())
