"""1계층(structured) 추출기 — 공공데이터포털 OpenAPI.

팩토리온(한국산업단지공단) 공장등록 생산정보 조회 서비스.

역할 = 발견 + 검증 앵커. (2026-11-10 전환)
- 발견: 주생산품(mainProductCn) 부분일치로 스펙 관련 공장을 직접 찾는다(search). 공장등록
  자체가 '공장이 있는 제조사'라는 공적 근거이므로, 웹검색보다 먼저 이 소스로 모집단을 잡는다.
- 검증: 특허 출원인·웹에서 찾은 회사명을 cmpnyNm으로 조회해 주소·공장을 확정한다(verify).
주생산품 표현이 자유어라("하드코팅 강판" 등 무관 업종 혼입) 관련성 판정은 LLM 계층이 한다.

※ mainProductCn·adres 요청변수는 공공데이터포털 명세(15087611)를 정리한 공개 코드에서
  확인한 이름이다. 라이브 키로 실측 확인이 남아 있다(문서 검색전환_261110.md).

실응답(XML) 필드명은 실제 호출로 확인했다. 서비스가 필드를 바꾸면 _to_record의
태그명만 고치면 된다(다운스트림 무영향).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

from ..schema import (
    CompanyKind,
    Provenance,
    SourceClass,
    SupplierRecord,
)
from .base import Extractor
from .registry import registry

# 팩토리온 생산정보 서비스 엔드포인트 (회사명·주생산품·주소 조회 오퍼레이션)
FACTORION_URL = "https://apis.data.go.kr/B550624/fctryRegistInfo/getFctryPrdctnService_v2"

# query 키 → 요청변수
_QUERY_PARAMS = {"company": "cmpnyNm", "product": "mainProductCn", "address": "adres"}


class FactorionExtractor(Extractor):
    """팩토리온 생산정보 조회.

    query={'company': 회사명 | 'product': 주생산품 | 'address': 주소, 'rows': N, 'page': P}.
    하나 이상의 조건이 필요하다(조합 가능).
    """

    tier = 1
    source_id = "factorion"
    source_class = SourceClass.ANCHOR

    def __init__(self, api_key: str | None = None, *, timeout: float = 20.0):
        self.api_key = api_key or os.getenv("DATA_GO_KR_API_KEY")
        self.timeout = timeout

    def extract(self, query: dict) -> list[SupplierRecord]:
        return self._request(query)[0]

    def _request(self, query: dict) -> tuple[list[SupplierRecord], int]:
        """한 페이지 조회 → (레코드, totalCount)."""
        params = self.build_params(query)
        params["serviceKey"] = self.api_key
        resp = httpx.get(FACTORION_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return self._parse(resp.text), self._total(resp.text)

    @staticmethod
    def build_params(query: dict) -> dict:
        """query → 요청 파라미터(serviceKey 제외). 조건이 하나도 없으면 오류."""
        cond = {k: query[q] for q, k in _QUERY_PARAMS.items() if query.get(q)}
        if not cond:
            raise ValueError("factorion query에는 'company'·'product'·'address' 중 하나가 필요합니다")
        return {"pageNo": int(query.get("page", 1)), "numOfRows": int(query.get("rows", 20)), **cond}

    def search(self, *, product: str | None = None, address: str | None = None,
               rows: int = 100, max_pages: int = 3) -> list[SupplierRecord]:
        """주생산품(·주소)으로 공장을 찾는다(발견). totalCount까지 최대 max_pages 페이지."""
        out: list[SupplierRecord] = []
        for page in range(1, max_pages + 1):
            recs, total = self._request({"product": product, "address": address,
                                         "rows": rows, "page": page})
            out.extend(recs)
            if not recs or page * rows >= total:
                break
        return out

    @staticmethod
    def _total(xml_text: str) -> int:
        try:
            return int(ET.fromstring(xml_text).findtext("./body/totalCount") or 0)
        except (ET.ParseError, ValueError):
            return 0

    def verify(self, companies: list[str], *, rows: int = 5) -> dict[str, list[SupplierRecord]]:
        """회사명 리스트를 일괄 조회해 {회사명: 매칭 레코드들}로 반환 (검증 앵커 용도)."""
        return {name: self.extract({"company": name, "rows": rows}) for name in companies}

    def _parse(self, xml_text: str) -> list[SupplierRecord]:
        root = ET.fromstring(xml_text)
        if root.tag == "OpenAPI_ServiceResponse":  # 게이트웨이 오류(서비스키 미등록·트래픽 초과 등)
            h = "./cmmMsgHeader/"
            raise RuntimeError(f"공공데이터포털 오류 {root.findtext(h + 'returnReasonCode')}: "
                               f"{root.findtext(h + 'errMsg')} ({root.findtext(h + 'returnAuthMsg')})")
        code = root.findtext("./header/resultCode")
        if code != "00":
            msg = root.findtext("./header/resultMsg")
            if code == "03" or (msg and "NODATA" in msg):  # 결과 없음은 정상
                return []
            raise RuntimeError(f"팩토리온 오류 {code}: {msg}")

        return [self._to_record(item) for item in root.findall("./body/items/item")]

    def _to_record(self, item: ET.Element) -> SupplierRecord:
        def t(tag: str) -> str | None:
            v = item.findtext(tag)
            return v.strip() if v and v.strip() else None

        address = t("rnAdres")
        homepage = t("hmpadr")
        emply = t("allEmplyCo")

        # 생산품목: 주생산품 + 업종명 (원문 보존 — 공정 판정은 LLM이 수행)
        products = [p for p in (t("mainProductCn"), t("indutyNm")) if p]

        prov = Provenance(
            source_id=self.source_id,
            source_class=self.source_class,
            extractor="structured",
            url=FACTORION_URL,
            verified=True,  # 공장관리번호·주소 확보 → 식별 확정
            note=f"공장관리번호 {t('fctryManageNo')}" if t("fctryManageNo") else None,
        )

        notes = [f"종업원 {emply}명"] if emply else []
        regist = t("frstFctryRegistDe")  # 최초 공장등록일(YYYYMMDD 등) → 업력
        since = int(regist[:4]) if regist and regist[:4].isdigit() else None
        if t("irsttNm"):
            notes.append(f"산업단지: {t('irsttNm')}")

        return SupplierRecord(
            name=t("cmpnyNm") or "(미상)",
            homepage=self._safe_url(homepage),
            domain=self._domain(homepage),
            country="KR",
            region=self._region(address),
            address=address,
            kind=CompanyKind.MANUFACTURER,  # 공장등록 = 제조사
            products=products,
            has_factory=True,
            employees=int(emply) if emply and emply.isdigit() else None,
            since_year=since,
            contact_dept=t("cvplChrgOrgnztNm"),
            contact_public=t("cmpnyTelno") or t("cmpnyFxnum"),
            provenances=[prov],
            notes=notes,
        )

    @staticmethod
    def _region(address: str | None) -> str | None:
        """도로명주소 앞 두 토큰(시도·시군구)만 지역으로."""
        if not address:
            return None
        return " ".join(address.split()[:2])

    @staticmethod
    def _looks_like_url(url: str | None) -> str | None:
        """공공데이터 hmpadr에는 주소 조각 등 쓰레기가 섞인다. URL 형태일 때만 정규화.

        조건: 공백 없음 + 도메인에 점(.) 포함. 아니면 None(홈페이지 미상 처리).
        """
        if not url:
            return None
        cand = url if url.startswith(("http://", "https://")) else "http://" + url
        netloc = urlparse(cand).netloc
        if " " in url or "." not in netloc:
            return None
        return cand

    @classmethod
    def _safe_url(cls, url: str | None):
        return cls._looks_like_url(url)

    @classmethod
    def _domain(cls, url: str | None) -> str | None:
        cand = cls._looks_like_url(url)
        if not cand:
            return None
        return urlparse(cand).netloc.replace("www.", "") or None


# 레지스트리 등록 (import 시 1회)
registry.register(FactorionExtractor())
