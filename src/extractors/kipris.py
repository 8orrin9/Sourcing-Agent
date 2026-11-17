"""1계층(structured) 추출기 — KIPRIS Plus 특허·실용신안 서지정보.

한국특허정보원(KIPRIS Plus) 특허/실용/공개/등록공보 조회 서비스.

역할 = 준앵커(semi-anchor). 팩토리온과 달리 출원인 주소·사업자번호를 주지 않아
식별을 확정(verified)하지 못한다. 대신 "이 회사가 특허를 보유했다 + 어떤 기술
분야인가(발명명칭·IPC)"를 근거로 제공해, 공정 역량(HC/AF) 판정의 L3 증거가 된다.
주소 조인(제조사 확정)은 팩토리온이 담당하고, KIPRIS는 역량 증거를 보탠다.

출원인명(applicant) 검색만 쓴다 — 팩토리온 cmpnyNm 패턴과 동일하게, 확산 소스가
찾은 회사명을 넣어 특허 보유·기술분야를 교차검증한다. 한 출원인의 여러 특허는
1건의 SupplierRecord로 집계한다(출원인 = 기업 1개).

실응답(XML) 필드명은 실제 호출로 확인했다(getAdvancedSearch · applicant 파라미터).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import httpx

from ..schema import (
    CompanyKind,
    Provenance,
    SourceClass,
    SupplierRecord,
)
from .base import Extractor
from .registry import registry

# KIPRIS Plus 특허실용신안 서지 검색 (출원인명 advanced search 오퍼레이션)
KIPRIS_URL = (
    "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"
)
# 한 레코드에 담을 대표 특허 수(제목·IPC 근거용). 응답 자체도 numOfRows로 제한.
_MAX_TITLES = 5


class KiprisExtractor(Extractor):
    """KIPRIS 특허 서지 조회. query={'company': 회사명(출원인), 'rows': N}.

    준앵커: 확산 소스가 찾은 회사명을 출원인명으로 조회해 특허 보유·기술분야를 확보한다.
    한 출원인의 여러 특허를 1건으로 집계(출원인 = 기업)해 반환한다.
    """

    tier = 1
    source_id = "kipris"
    source_class = SourceClass.ANCHOR

    def __init__(self, api_key: str | None = None, *, timeout: float = 25.0):
        self.api_key = api_key or os.getenv("KIPRIS_API_KEY")
        self.timeout = timeout

    def extract(self, query: dict) -> list[SupplierRecord]:
        company = query.get("company")
        if not company:
            raise ValueError("kipris query에는 'company'(출원인 회사명)가 필요합니다")
        rows = int(query.get("rows", 10))

        resp = httpx.get(
            KIPRIS_URL,
            params={
                "applicant": company,
                "ServiceKey": self.api_key,
                "numOfRows": rows,
                "pageNo": 1,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse(resp.text, company)

    def verify(self, companies: list[str], *, rows: int = 5) -> dict[str, list[SupplierRecord]]:
        """회사명 리스트를 일괄 조회해 {회사명: 매칭 레코드들}로 반환 (준앵커 용도)."""
        return {name: self.extract({"company": name, "rows": rows}) for name in companies}

    def _parse(self, xml_text: str, query_name: str) -> list[SupplierRecord]:
        root = ET.fromstring(xml_text)
        if root.findtext("./header/successYN") == "N" or root.findtext("./header/resultCode") != "00":
            msg = root.findtext("./header/resultMsg")
            raise RuntimeError(f"KIPRIS 오류 {root.findtext('./header/resultCode')}: {msg}")

        items = root.findall("./body/items/item")
        if not items:  # 특허 없음은 정상(가공업체 등)
            return []

        total = root.findtext(".//totalCount")
        return [self._to_record(items, query_name, total)]

    def _to_record(
        self, items: list[ET.Element], query_name: str, total: str | None
    ) -> SupplierRecord:
        """한 출원인의 특허 다건을 1개 레코드로 집계."""

        def t(item: ET.Element, tag: str) -> str | None:
            v = item.findtext(tag)
            return v.strip() if v and v.strip() else None

        # 출원인명 — 응답값 우선(법인격 표기 포함), 없으면 조회명
        applicant = t(items[0], "applicantName") or query_name

        # 특허 제목 → 기술분야 근거(products). IPC는 분류 보조로 notes에.
        titles = [t(it, "inventionTitle") for it in items[:_MAX_TITLES]]
        titles = [x for x in titles if x]
        ipcs: list[str] = []
        for it in items[:_MAX_TITLES]:
            raw = t(it, "ipcNumber")
            if raw:
                ipcs.extend(code.strip() for code in raw.split("|") if code.strip())

        count = total or str(len(items))
        prov = Provenance(
            source_id=self.source_id,
            source_class=self.source_class,
            extractor="structured",
            url=KIPRIS_URL,
            verified=False,  # 준앵커: 주소·사업자번호 미제공 → 식별 미확정
            note=f"특허 {count}건 보유 (출원인명 검색)",
        )

        notes = [f"[특허] {ti}" for ti in titles]
        if ipcs:
            # IPC 상위 분류만(중복 제거, 순서 보존) — 기술분야 단서
            seen: list[str] = []
            for c in ipcs:
                head = c.split()[0] if c else c
                if head and head not in seen:
                    seen.append(head)
            notes.append(f"[IPC] {', '.join(seen[:8])}")

        return SupplierRecord(
            name=applicant,
            country="KR",
            kind=CompanyKind.UNKNOWN,  # 특허 보유≠제조사 확정 → LLM/팩토리온 조인이 판정
            products=titles,  # 기술분야 근거 — classify가 공정 역량으로 해석
            provenances=[prov],
            notes=notes,
        )


# 레지스트리 등록 (import 시 1회)
registry.register(KiprisExtractor())
