"""업체 상세 수집 — 사이트 구조 무관 LLM 추출(STEP 2).

후보 '발견'(llm_extract: 디렉터리에서 회사 목록)과 달리, 여기서는 이미 식별된 한
업체의 홈페이지를 열어 **상세 프로필**(설비·인증·제품·규모·연락)을 뽑는다. 업체마다
사이트 구조가 제각각(div/section · 구식 table · 프레임 등)이라, CSS 셀렉터로는
일일이 대응할 수 없다 → 구조에 의존하지 않는 LLM 추출로 '한 장의 회사 소개'를 읽는다.

Fetcher를 공유해 robots·캐시·레이트리밋·TLS·골든 픽스처 주입을 그대로 쓴다. 여러
페이지(회사·설비·인증·제품)를 모아 한 번에 판정하되, 토큰 상한으로 자른다.

crawl(홈페이지): 첫 화면의 같은 도메인 링크를 키워드(회사소개·설비·인증·제품 …)로
우선순위를 매겨 최대 MAX_PAGES쪽을 받고, PDF 링크(카탈로그·인증서·회사소개서 …)는
최대 MAX_DOCS개를 내려받아 텍스트를 뽑아 함께 판정에 넣는다(2026-11-10 추가).

존 분리: 추출 지시에 내부 단가·품목코드·현 협력사명을 넣지 않는다.
개인정보 비저장: 담당 '부서'와 '대표' 연락처(이메일·전화)만 담고, 개인 이름은 안 담는다.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.documents import read_document
from ..core.fetcher import Fetcher
from ..llm.client import complete_json
from ..schema import CompanyKind, Provenance, SourceClass, SupplierRecord
from .base import Extractor
from .registry import registry

# 업체당 LLM에 넘길 최대 글자수(여러 페이지 합산 후 절단) — 토큰·비용 상한
MAX_CHARS = 16000
# 업체당 수집 예산: 홈 외 페이지 수, PDF 수, PDF 1건당 글자수
MAX_PAGES = 8
MAX_DOCS = 3
MAX_DOC_CHARS = 3500   # PDF 1건 (3건이면 10,500자)
MAX_PAGE_CHARS = 1500  # 홈·하위 페이지 1쪽 — 메뉴·푸터 반복이 많아 짧게

# 링크 우선순위 키워드 (href + 링크 텍스트에서 찾는다)
PAGE_KEYWORDS = ("about", "company", "intro", "회사", "소개", "개요", "equipment", "facility",
                 "설비", "시설", "공정", "cert", "인증", "품질", "quality", "product", "제품",
                 "tech", "기술", "연혁", "history")
DOC_KEYWORDS = ("catalog", "카탈로그", "brochure", "cert", "인증", "profile", "회사소개", "소개서",
                "spec", "사양", "성적서", "report", "iso")

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "country": {"type": "string"},        # 없으면 빈 문자열
        "region": {"type": "string"},         # 시도·시군구 수준
        "address": {"type": "string"},
        "is_manufacturer": {"type": "boolean"},
        "employees": {"type": "string"},       # 원문 그대로(예: "84명", "approx. 60")
        "equipment": {"type": "array", "items": {"type": "string"}},   # 설비
        "certifications": {"type": "array", "items": {"type": "string"}},  # 인증
        "products": {"type": "array", "items": {"type": "string"}},
        "contact_dept": {"type": "string"},    # 담당 '부서'(개인 이름 금지)
        "contact_public": {"type": "string"},  # 대표 이메일/전화(공개)
        "summary": {"type": "string"},
    },
    "required": ["name", "country", "region", "address", "is_manufacturer",
                 "employees", "equipment", "certifications", "products",
                 "contact_dept", "contact_public", "summary"],
    "additionalProperties": False,
}

_SYSTEM = (
    "너는 B2B 공급사 홈페이지에서 '회사 상세 프로필'을 추출하는 파서다. 여러 페이지"
    "(회사소개·설비·인증·제품·연락처)를 이어붙인 텍스트가 주어진다. 사이트마다 HTML "
    "구조가 다르니 구조가 아니라 '의미'로 읽어라.\n"
    "추출 규칙:\n"
    "(1) 회사명·국가·지역·주소·임직원 규모(원문 표기 그대로).\n"
    "(2) 보유 '설비'(코터·경화기·레이저·클린룸 등)와 '인증'(ISO·IATF·시험성적서·특허)을 "
    "각각 배열로. 페이지에 드러난 것만, 추측 금지.\n"
    "(3) 실제 제조사면 is_manufacturer=true, 유통·무역·에이전트면 false.\n"
    "(4) 연락처는 담당 '부서'와 대표 이메일/전화만. 개인 이름은 절대 담지 말 것.\n"
    "(5) 없는 값은 빈 문자열/빈 배열. 한국어로 요약(summary) 1~2문장."
)


class SiteDetailExtractor(Extractor):
    """업체 상세 프로필 LLM 추출. query={'urls': [...]} 또는 {'html': 원문, 'url': 대표URL}.

    urls를 주면 Fetcher로 각 페이지를 받아 이어붙인 뒤 추출(라이브). html을 직접 주면
    그대로 파싱(픽스처·테스트). 추출 결과는 1개 SupplierRecord.
    """

    # 상세 수집은 한 사이트의 구조 해석이라 가장 비싼 계층(LLM) 성격 — llm_extract와 동급.
    tier = 4
    source_id = "site_detail"
    source_class = SourceClass.SPREAD  # 업체 자체 주장 → 미검증(앵커 흡착·RFI로 승격)

    def __init__(self, fetcher: Fetcher | None = None):
        self._fetcher = fetcher

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    def extract(self, query: dict) -> list[SupplierRecord]:
        """query={'homepage': URL}(크롤+PDF) | {'urls': [...]} | {'html': 원문, 'url': 대표URL}."""
        if query.get("homepage"):
            got = self.crawl(query["homepage"])
            rec = self._extract_text(got["text"], base_url=query["homepage"], docs=got["docs"])
            return [rec] if rec else []
        html = query.get("html")
        urls = query.get("urls") or ([query["url"]] if query.get("url") else [])
        if html is None:
            if not urls:
                raise ValueError("site_detail query에는 'homepage'·'urls'/'url'·'html' 중 하나가 필요합니다")
            html = "\n\n".join(self.fetcher.get(u) for u in urls)
        base_url = urls[0] if urls else (query.get("url") or "")
        rec = self._extract(html, base_url=base_url)
        return [rec] if rec else []

    def crawl(self, homepage: str) -> dict:
        """홈 → 우선순위 링크 MAX_PAGES쪽 + PDF MAX_DOCS건. 실패한 URL은 건너뛰고 기록한다.

        반환: {'text': 판정용 텍스트, 'pages': [url], 'docs': [{name,url,chars}], 'failed': [url]}
        """
        home = self.fetcher.get(homepage)
        pages_html, pages, failed = [home], [homepage], []
        page_links, doc_links = self.plan_links(home, homepage)
        for u in page_links[:MAX_PAGES]:
            try:
                pages_html.append(self.fetcher.get(u))
                pages.append(u)
            except Exception:  # robots 차단·404 등 — 한 페이지 실패가 전체를 막지 않게
                failed.append(u)
        docs, doc_texts = [], []
        for u in doc_links[:MAX_DOCS]:
            try:
                body = read_document(self.fetcher.get_bytes(u), filename=u)[:MAX_DOC_CHARS]
            except Exception:
                failed.append(u)
                continue
            name = urlparse(u).path.rsplit("/", 1)[-1] or u
            docs.append({"name": name, "url": u, "chars": len(body)})
            doc_texts.append(f"[문서: {name}]\n{body}")
        # 순서: 홈 → 문서(인증서·카탈로그는 근거 가치가 커서 절단되지 않게 앞에) → 하위 페이지
        page_texts = [self._clean(h)[:MAX_PAGE_CHARS] for h in pages_html]
        texts = page_texts[:1] + doc_texts + page_texts[1:]
        return {"text": "\n\n".join(texts), "pages": pages, "docs": docs, "failed": failed}

    @staticmethod
    def plan_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
        """같은 도메인 링크를 (페이지, PDF)로 나누고 키워드 적중 수로 정렬한다."""
        soup = BeautifulSoup(html, "lxml")
        host = urlparse(base_url).netloc.replace("www.", "")
        scored_pages: dict[str, int] = {}
        scored_docs: dict[str, int] = {}
        for a in soup.find_all("a", href=True):
            url = urljoin(base_url, a["href"]).split("#")[0]
            if urlparse(url).scheme not in ("http", "https"):
                continue
            if urlparse(url).netloc.replace("www.", "") != host or url.rstrip("/") == base_url.rstrip("/"):
                continue
            key = f"{url} {a.get_text(' ', strip=True)}".lower()
            if urlparse(url).path.lower().endswith(".pdf"):
                scored_docs[url] = max(scored_docs.get(url, 0), sum(k in key for k in DOC_KEYWORDS))
            else:
                hits = sum(k in key for k in PAGE_KEYWORDS)
                if hits:
                    scored_pages[url] = max(scored_pages.get(url, 0), hits)
        order = lambda d: [u for u, _ in sorted(d.items(), key=lambda kv: -kv[1])]
        return order(scored_pages), order(scored_docs)

    def _extract(self, html: str, *, base_url: str = "") -> SupplierRecord | None:
        return self._extract_text(self._clean(html), base_url=base_url)

    def _extract_text(self, text: str, *, base_url: str = "",
                      docs: list[dict] | None = None) -> SupplierRecord | None:
        text = text[:MAX_CHARS]
        out = complete_json(system=_SYSTEM, user=text, schema=_SCHEMA, schema_name="site_detail")
        if not out.get("name"):
            return None

        homepage = base_url or None
        domain = urlparse(base_url).netloc.replace("www.", "") if base_url else None

        # 설비·인증은 notes에 근거로 남긴다(공정 역량 판정·점수 '규모 단서'에 활용)
        notes: list[str] = []
        if out.get("summary"):
            notes.append(out["summary"])
        if out.get("employees"):
            notes.append(f"임직원 {out['employees']}")
        for eq in out.get("equipment", []):
            notes.append(f"[설비] {eq}")
        for ct in out.get("certifications", []):
            notes.append(f"[인증] {ct}")
        for d in docs or []:
            notes.append(f"[문서] {d['name']} ({d['url']})")
        emp = re.search(r"\d[\d,]*", out.get("employees") or "")

        prov = Provenance(
            source_id=self.source_id,
            source_class=self.source_class,
            extractor="site_detail",
            url=homepage,
            verified=False,  # 업체 자체 주장 — 앵커 교차검증/RFI 전
            note=f"홈페이지 상세 수집({domain})" if domain else "홈페이지 상세 수집",
        )

        return SupplierRecord(
            name=out["name"],
            homepage=homepage,
            domain=domain,
            country=out.get("country") or None,
            region=out.get("region") or None,
            address=out.get("address") or None,
            kind=CompanyKind.MANUFACTURER if out.get("is_manufacturer") else CompanyKind.TRADER,
            products=[p for p in out.get("products", []) if p],
            employees=int(emp.group().replace(",", "")) if emp else None,
            contact_dept=out.get("contact_dept") or None,
            contact_public=out.get("contact_public") or None,
            provenances=[prov],
            notes=notes,
        )

    @staticmethod
    def _clean(html: str) -> str:
        """스크립트·스타일 제거 후 앵커에 URL을 붙여 텍스트로 축약(토큰 절약)."""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()
        for a in soup.find_all("a"):
            href = a.get("href")
            if href:
                a.append(f" <{href}>")
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln for ln in (l.strip() for l in text.splitlines()) if ln]
        return "\n".join(lines)[:MAX_CHARS]

    @staticmethod
    def _safe_url(href: str | None, base_url: str) -> str | None:
        """상대/프로토콜상대 href 절대화(비URL은 None)."""
        if not href:
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if base_url and href.startswith("/"):
            return urljoin(base_url, href)
        return None


# 레지스트리 등록 (import 시 1회)
registry.register(SiteDetailExtractor())
