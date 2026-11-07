"""확산 추출기 — 다국어 웹검색 (SerpApi / Google Search).

발견(discovery) 소스. 공정·소재 키워드로 다국어 검색해 공급사 후보의 회사명·URL을
넓게 확보한다. 식별 정보는 약하므로(회사명·링크 위주), 앵커(팩토리온)에 흡착해
검증한다 — SourceClass.SPREAD.

존 분리: 검색 쿼리에 내부 품목코드·단가·현 협력사명을 넣지 않는다(공정·소재 키워드만).
SerpApi(https://serpapi.com/search) GET + api_key 파라미터, 응답은
organic_results[].{title,link,snippet}. 라이브 호출과 픽스처 파싱이 같은 _parse를
쓴다(키 없을 때도 로직 검증 가능).
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx

from ..schema import Provenance, SourceClass, SupplierRecord
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
    """Serper 웹검색. query={'q': 검색어, 'locale': 'KR'|'EN'|'ZH'|'VI', 'num': N}."""

    tier = 2
    source_id = "web_search"
    source_class = SourceClass.SPREAD

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("SERPER_API_KEY")
        self.timeout = timeout

    def extract(self, query: dict) -> list[SupplierRecord]:
        q = query.get("q")
        if not q:
            raise ValueError("web_search query에는 'q'(검색어)가 필요합니다")
        locale = LOCALE.get(query.get("locale", "KR"), LOCALE["KR"])
        num = int(query.get("num", 10))

        if not self.api_key:
            raise RuntimeError("SERPER_API_KEY 미설정 — 라이브 검색 불가 (픽스처는 _parse 사용)")

        resp = httpx.get(
            SERPAPI_URL,
            params={"engine": "google", "q": q, "num": num, "api_key": self.api_key, **locale},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.text, locale_key=query.get("locale", "KR"))

    def _parse(self, body: str, *, locale_key: str = "KR") -> list[SupplierRecord]:
        data = json.loads(body) if isinstance(body, str) else body
        records: list[SupplierRecord] = []
        for o in data.get("organic_results", []):
            link = o.get("link")
            title = o.get("title") or ""
            if not link:
                continue
            domain = urlparse(link).netloc.replace("www.", "")
            is_aggregator = any(domain.endswith(d) for d in AGGREGATOR_DOMAINS)

            prov = Provenance(
                source_id=self.source_id,
                source_class=self.source_class,
                extractor="web_search",
                url=link,
                verified=False,  # 웹검색은 미검증 — 앵커 흡착 필요
                note=f"{locale_key} 웹검색" + (" · 디렉터리/집계" if is_aggregator else ""),
            )
            records.append(
                SupplierRecord(
                    name=self._company_name(title),
                    homepage=None if is_aggregator else link,
                    domain=None if is_aggregator else (domain or None),
                    provenances=[prov],
                    notes=[o["snippet"]] if o.get("snippet") else [],
                )
            )
        return records

    @staticmethod
    def _company_name(title: str) -> str:
        """검색 결과 제목에서 회사명 추정 — 구분자 앞 토막만.

        정밀 정규화(법인격 제거·언어별 처리)는 LLM 엔티티 병합이 담당. 여기선 거친 1차.
        """
        for sep in (" - ", " | ", " – ", " : ", "_"):
            if sep in title:
                title = title.split(sep)[0]
                break
        return title.strip() or "(미상)"


# 레지스트리 등록 (import 시 1회)
registry.register(WebSearchExtractor())
