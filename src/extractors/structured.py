"""1계층(structured) 추출기 — 공공데이터포털 OpenAPI.

팩토리온(한국산업단지공단) 공장등록 생산정보 조회 서비스.

역할 = 검증 앵커(verification anchor). 이 서비스는 회사명(cmpnyNm) 부분일치 검색만
지원하고 생산품으로는 검색되지 않으며, 주생산품 표현도 제각각이다. 따라서 "발견"이
아니라, 확산 소스(웹검색·디렉터리)가 찾은 회사명을 넣어 주소·공장·생산품을
교차검증하는 데 쓴다. 공정 역량 판정(HC/AF)은 LLM 계층이 수행한다.

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

# 팩토리온 생산정보 서비스 엔드포인트 (회사명 조회 오퍼레이션)
FACTORION_URL = "https://apis.data.go.kr/B550624/fctryRegistInfo/getFctryPrdctnService_v2"


class FactorionExtractor(Extractor):
    """팩토리온 생산정보 조회. query={'company': 회사명, 'rows': N}.

    검증 앵커: 확산 소스가 찾은 회사명으로 조회해 주소·공장·생산품을 확보한다.
    """

    tier = 1
    source_id = "factorion"
    source_class = SourceClass.ANCHOR

    def __init__(self, api_key: str | None = None, *, timeout: float = 20.0):
        self.api_key = api_key or os.getenv("DATA_GO_KR_API_KEY")
        self.timeout = timeout

    def extract(self, query: dict) -> list[SupplierRecord]:
        company = query.get("company")
        if not company:
            raise ValueError("factorion query에는 'company'(회사명)가 필요합니다")
        rows = int(query.get("rows", 20))

        resp = httpx.get(
            FACTORION_URL,
            params={
                "serviceKey": self.api_key,
                "pageNo": 1,
                "numOfRows": rows,
                "cmpnyNm": company,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.text)

    def verify(self, companies: list[str], *, rows: int = 5) -> dict[str, list[SupplierRecord]]:
        """회사명 리스트를 일괄 조회해 {회사명: 매칭 레코드들}로 반환 (검증 앵커 용도)."""
        return {name: self.extract({"company": name, "rows": rows}) for name in companies}

    def _parse(self, xml_text: str) -> list[SupplierRecord]:
        root = ET.fromstring(xml_text)
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
