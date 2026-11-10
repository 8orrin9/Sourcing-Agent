"""1계층(structured) 추출기 — KIPRIS Plus 특허·실용신안 서지정보.

한국특허정보원(KIPRIS Plus) 특허/실용/공개/등록공보 조회 서비스.

역할 = 준앵커(semi-anchor). 팩토리온과 달리 출원인 주소·사업자번호를 주지 않아
식별을 확정(verified)하지 못한다. 대신 "이 회사가 특허를 보유했다 + 어떤 기술
분야인가(발명명칭·IPC)"를 근거로 제공해, 공정 역량(HC/AF) 판정의 L3 증거가 된다.
주소 조인(제조사 확정)은 팩토리온이 담당하고, KIPRIS는 역량 증거를 보탠다.

두 가지로 쓴다. (2026-11-10 전환)
- search_tech: 기술 키워드·IPC로 특허를 찾아 '출원인 목록'을 만든다(발견). 대학·연구기관·
  개인 출원인은 제외한다. 출원인은 팩토리온 회사명 조회로 공장을 확정해야 후보가 된다.
- verify(출원인명): 이미 찾은 회사의 특허 보유·기술분야를 보강한다. 부분일치 검색이라
  다른 출원인의 특허가 섞일 수 있어, 출원인명이 정확히 같은 특허만 집계한다.

※ search_tech의 word·ipcNumber 요청변수는 KIPRIS Plus getAdvancedSearch 명세 기준이며
  라이브 키로 실측 확인이 남아 있다(문서 검색전환_261110.md).

실응답(XML) 필드명은 실제 호출로 확인했다(getAdvancedSearch · applicant 파라미터).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

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

# 기업이 아닌 출원인(대학·연구기관·공공기관) 판별어
_NON_COMPANY = re.compile(r"대학|산학협력|연구원|연구소|재단|진흥원|공사$|공단|협회|학교|"
                          r"university|institute|college|foundation", re.I)
# 법인 표지 — 없으면서 한글 2~4자면 개인 출원인으로 본다
_CORP_MARK = re.compile(r"주식회사|\(주\)|㈜|유한|합자|회사|co\.|ltd|inc|corp|gmbh", re.I)


def is_company_applicant(name: str) -> bool:
    """출원인이 기업인지(대학·연구기관·개인 제외). 결정적 규칙."""
    n = name.strip()
    if not n or _NON_COMPANY.search(n):
        return False
    if not _CORP_MARK.search(n) and re.fullmatch(r"[가-힣]{2,4}", n):
        return False  # 개인 이름
    return True


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

    def search_tech(self, *, word: str | None = None, ipc: str | None = None,
                    rows: int = 100) -> list[SupplierRecord]:
        """기술 키워드·IPC로 특허 검색 → 출원인(기업)별 1건으로 집계(발견).

        반환 레코드는 특허 건수 내림차순. 대학·연구기관·개인 출원인은 제외한다.
        """
        if not (word or ipc):
            raise ValueError("search_tech에는 word 또는 ipc가 필요합니다")
        params = {"ServiceKey": self.api_key, "numOfRows": rows, "pageNo": 1}
        if word:
            params["word"] = word
        if ipc:
            params["ipcNumber"] = ipc
        resp = httpx.get(KIPRIS_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return self.parse_applicants(resp.text, note=f"특허 기술검색({word or ''} {ipc or ''})".strip())

    def parse_applicants(self, xml_text: str, *, note: str = "특허 기술검색") -> list[SupplierRecord]:
        """검색 응답의 특허들을 출원인별로 묶는다. 출원인 '|' 다중 표기도 분리."""
        items = self._items(xml_text)
        by_app: dict[str, list[ET.Element]] = defaultdict(list)
        for it in items:
            for app in (it.findtext("applicantName") or "").split("|"):
                if is_company_applicant(app):
                    by_app[app.strip()].append(it)
        recs = [self._to_record(its, app, str(len(its))) for app, its in by_app.items()]
        for r in recs:
            r.provenances[0].note = f"{note} · 특허 {len(by_app[r.name])}건"
        return sorted(recs, key=lambda r: -len(by_app[r.name]))

    def _items(self, xml_text: str) -> list[ET.Element]:
        root = ET.fromstring(xml_text)
        if root.findtext("./header/successYN") == "N" or root.findtext("./header/resultCode") != "00":
            msg = root.findtext("./header/resultMsg")
            raise RuntimeError(f"KIPRIS 오류 {root.findtext('./header/resultCode')}: {msg}")
        return root.findall("./body/items/item")

    def _parse(self, xml_text: str, query_name: str) -> list[SupplierRecord]:
        """출원인명 조회 응답 → 1건. 부분일치로 섞인 다른 출원인의 특허는 버린다."""
        from ..llm.merge import _norm_name

        want = _norm_name(query_name)
        all_items = self._items(xml_text)
        items = [it for it in all_items
                 if any(_norm_name(a) == want for a in (it.findtext("applicantName") or "").split("|"))]
        if not items:  # 특허 없음은 정상(가공업체 등)
            return []
        # 응답이 모두 같은 출원인이면 totalCount(전체 보유 건수)를 믿고, 섞였으면 일치 건수만
        total = ET.fromstring(xml_text).findtext(".//totalCount") if len(items) == len(all_items) else None
        return [self._to_record(items, query_name, total or str(len(items)))]

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
