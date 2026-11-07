"""확산 추출기 — 선언형 설정(2계층).

비슷하게 생긴 B2B 디렉터리는 CSS 셀렉터를 YAML로 기술한다. 신규 사이트 편입 =
코드가 아니라 `config/*.yaml` 한 장 추가 → import 시 자동 등록. 롱테일을 싸게 커버.

한 YAML = 한 소스(source_id). item_selector로 업체 블록을 자르고, fields의
셀렉터·attr로 값을 뽑아 SupplierRecord로 정규화한다. 디렉터리는 제조사 자체
사이트가 아니므로 verified=False — 앵커(팩토리온) 흡착 대상(SourceClass.SPREAD).

존 분리: search_url에 내부 품목코드·단가·현 협력사명을 넣지 않는다(공정·소재 키워드만).
Fetcher를 공유해 캐시·레이트리밋·robots·골든 픽스처 주입을 그대로 쓴다.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup

from ..core.fetcher import Fetcher
from ..schema import Provenance, SourceClass, SupplierRecord
from .base import Extractor
from .registry import registry

CONFIG_DIR = Path(__file__).resolve().parent / "config"

# locale → 셀렉터/URL에 주입할 언어 토큰(web_search와 동일 키 체계)
LOCALE_HL = {"KR": "ko", "EN": "en", "ZH": "zh", "VI": "vi"}


class ConfigDrivenExtractor(Extractor):
    """YAML 설정 한 장으로 동작하는 디렉터리 추출기.

    query={'q': 검색어, 'locale': 'KR'|'EN'|'ZH'|'VI'}. Fetcher가 HTML을 주고,
    셀렉터로 파싱한다. 라이브 호출과 픽스처 파싱이 같은 _parse를 공유한다.
    """

    tier = 2

    def __init__(self, config: dict, fetcher: Fetcher | None = None):
        self.config = config
        self.source_id = config["source_id"]
        self.source_class = SourceClass(config.get("source_class", "spread"))
        self._fetcher = fetcher

    # 공유 Fetcher 지연 생성(테스트는 _parse만 쓰므로 네트워크 불필요)
    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    def extract(self, query: dict) -> list[SupplierRecord]:
        q = query.get("q")
        if not q:
            raise ValueError(f"{self.source_id} query에는 'q'(검색어)가 필요합니다")
        locale = query.get("locale", "KR")
        url = self.config["search_url"].format(q=q, hl=LOCALE_HL.get(locale, "en"))
        html = self.fetcher.get(url)
        return self._parse(html, base_url=url, locale_key=locale)

    def _parse(self, html: str, *, base_url: str = "", locale_key: str = "KR") -> list[SupplierRecord]:
        soup = BeautifulSoup(html, "lxml")
        records: list[SupplierRecord] = []
        for block in soup.select(self.config["item_selector"]):
            vals = {name: self._field(block, spec) for name, spec in self.config["fields"].items()}
            name = vals.get("name")
            if not name:
                continue  # 회사명 없는 블록은 건너뜀
            homepage = self._abs_url(vals.get("homepage"), base_url)
            domain = urlparse(homepage).netloc.replace("www.", "") if homepage else None
            prov = Provenance(
                source_id=self.source_id,
                source_class=self.source_class,
                extractor="config_driven",
                url=homepage,
                verified=False,  # 디렉터리 — 앵커 흡착 필요
                note=f"{locale_key} 디렉터리({self.source_id})",
            )
            records.append(
                SupplierRecord(
                    name=name,
                    homepage=homepage,
                    domain=domain,
                    country=vals.get("country") or None,
                    provenances=[prov],
                    notes=[vals["snippet"]] if vals.get("snippet") else [],
                )
            )
        return records

    @staticmethod
    def _field(block, spec: dict) -> str | None:
        """블록에서 한 필드 추출. spec={'selector','attr'}. attr='text'면 텍스트."""
        el = block.select_one(spec["selector"])
        if el is None:
            return None
        attr = spec.get("attr", "text")
        val = el.get_text(strip=True) if attr == "text" else el.get(attr)
        return val.strip() if isinstance(val, str) and val.strip() else None

    @staticmethod
    def _abs_url(href: str | None, base_url: str) -> str | None:
        """상대경로 href를 절대 URL로. 비URL/빈값은 None(pydantic 검증 보호)."""
        if not href:
            return None
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/") and base_url:
            p = urlparse(base_url)
            return f"{p.scheme}://{p.netloc}{href}"
        return None


def load_configs(fetcher: Fetcher | None = None) -> list[ConfigDrivenExtractor]:
    """config/*.yaml을 모두 읽어 추출기 인스턴스로. 레지스트리 등록은 호출부가."""
    extractors = []
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        extractors.append(ConfigDrivenExtractor(cfg, fetcher=fetcher))
    return extractors


# 레지스트리 등록 (import 시 1회) — YAML 추가하면 자동으로 늘어난다
for _ext in load_configs():
    registry.register(_ext)
