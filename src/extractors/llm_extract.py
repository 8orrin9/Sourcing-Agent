"""확산 추출기 — LLM 폴백(4계층).

전용 추출기(3계층)도 YAML 셀렉터(2계층)도 없는 사이트를 커버한다. 정리한 HTML +
목표 스키마를 LLM에 주고 공급사 레코드를 뽑는다. 구조가 바뀌어도 견디고, 설정·코드
없는 신규 사이트를 즉시 커버 — "사이트 pool 자생 확장"의 열쇠.

가장 비싼 계층이라 레지스트리 맨 뒤(tier=4). 안티봇이 강한 글로벌 디렉터리(중국 등)는
라이브 대신 저장 스냅샷(data/fixtures)에서 같은 _extract 로직을 돌려 시연을 안정화한다.

존 분리: 추출 지시에 내부 단가·품목코드·현 협력사명을 넣지 않는다.
LLM이 제조사/유통사 여부도 함께 판정하되, 확산 소스라 verified=False(앵커 흡착 대상).
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.fetcher import Fetcher
from ..llm.client import complete_json
from ..schema import CompanyKind, Provenance, SourceClass, SupplierRecord
from .base import Extractor
from .registry import registry

# 토큰 절약 — 정리 후 LLM에 넘길 최대 글자수(초과분은 자른다)
MAX_CHARS = 12000

_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "homepage": {"type": "string"},  # 없으면 빈 문자열
                    "country": {"type": "string"},
                    "region": {"type": "string"},
                    "products": {"type": "array", "items": {"type": "string"}},
                    "is_manufacturer": {"type": "boolean"},
                    "snippet": {"type": "string"},
                },
                "required": ["name", "homepage", "country", "region",
                             "products", "is_manufacturer", "snippet"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["companies"],
    "additionalProperties": False,
}

_SYSTEM = (
    "너는 B2B 디렉터리 페이지에서 공급사 목록을 추출하는 파서다. 주어진 (정리된) HTML "
    "텍스트와 링크에서, 검색 결과로 나열된 '기업'만 뽑아 구조화하라.\n"
    "규칙: (1) 광고·배너·페이지네이션·카테고리 메뉴는 제외. (2) 각 기업의 회사명·홈페이지"
    "링크·국가/지역·주요 생산품·한줄소개를 채운다. 없는 값은 빈 문자열/빈 배열. "
    "(3) 무역상·소싱 에이전트·유통사는 is_manufacturer=false, 실제 제조사만 true. "
    "(4) 추측 금지 — 페이지에 드러난 정보만. 중문/영문/베트남어 등 원문 언어 그대로 둬도 된다."
)


class LlmFallbackExtractor(Extractor):
    """LLM으로 임의 디렉터리 HTML을 파싱. query={'url': 페이지 URL} 또는 {'html','url'}.

    url만 주면 Fetcher로 받아오고(라이브), html을 직접 주면 그대로 파싱(픽스처).
    """

    tier = 4
    source_id = "llm_extract"
    source_class = SourceClass.SPREAD

    def __init__(self, fetcher: Fetcher | None = None):
        self._fetcher = fetcher

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    def extract(self, query: dict) -> list[SupplierRecord]:
        url = query.get("url")
        html = query.get("html")
        if html is None:
            if not url:
                raise ValueError("llm_extract query에는 'url' 또는 'html'이 필요합니다")
            html = self.fetcher.get(url)
        return self._extract(html, base_url=url or "", source_id=query.get("source_id"))

    def _extract(self, html: str, *, base_url: str = "", source_id: str | None = None) -> list[SupplierRecord]:
        text = self._clean(html)
        out = complete_json(system=_SYSTEM, user=text, schema=_SCHEMA, schema_name="dir_companies")
        sid = source_id or (urlparse(base_url).netloc.replace("www.", "") or self.source_id)
        records: list[SupplierRecord] = []
        for c in out["companies"]:
            if not c.get("name"):
                continue
            homepage = self._safe_url(c.get("homepage"), base_url)
            domain = urlparse(homepage).netloc.replace("www.", "") if homepage else None
            prov = Provenance(
                source_id=sid,
                source_class=self.source_class,
                extractor="llm_extract",
                url=homepage,
                verified=False,  # LLM 추출·확산 — 앵커 흡착 필요
                note=f"LLM 폴백 추출({sid})",
            )
            records.append(
                SupplierRecord(
                    name=c["name"],
                    homepage=homepage,
                    domain=domain,
                    country=c.get("country") or None,
                    region=c.get("region") or None,
                    products=[p for p in c.get("products", []) if p],
                    kind=CompanyKind.MANUFACTURER if c.get("is_manufacturer") else CompanyKind.TRADER,
                    provenances=[prov],
                    notes=[c["snippet"]] if c.get("snippet") else [],
                )
            )
        return records

    @staticmethod
    def _clean(html: str) -> str:
        """스크립트·스타일·주석 제거 후 '[링크] 텍스트 <url>' 흐름으로 축약(토큰 절약)."""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()
        # 앵커는 텍스트 옆에 URL을 붙여 LLM이 홈페이지를 집도록
        for a in soup.find_all("a"):
            href = a.get("href")
            if href:
                a.append(f" <{href}>")
        text = soup.get_text(separator="\n", strip=True)
        # 빈 줄 접기
        lines = [ln for ln in (l.strip() for l in text.splitlines()) if ln]
        joined = "\n".join(lines)
        return joined[:MAX_CHARS]

    @staticmethod
    def _safe_url(href: str | None, base_url: str) -> str | None:
        """상대/프로토콜상대 href를 절대화. 비URL은 None(pydantic 검증 보호)."""
        if not href:
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if base_url and href.startswith("/"):
            return urljoin(base_url, href)
        return None


# 레지스트리 등록 (import 시 1회) — 모든 소스의 최종 폴백
registry.register(LlmFallbackExtractor())
