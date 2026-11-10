"""발견 소스 전환 테스트 — 팩토리온 생산품 검색·KIPRIS 기술 검색 (오프라인·실응답 픽스처)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.extractors import kipris as kipris_mod
from src.extractors import structured
from src.extractors.kipris import KiprisExtractor, is_company_applicant
from src.extractors.structured import FactorionExtractor

FIX = Path(__file__).resolve().parents[1] / "data" / "fixtures"
FAC_XML = (FIX / "factorion" / "getFctryPrdctnService_v2_coating.xml").read_text(encoding="utf-8")
KIP_XML = (FIX / "kipris" / "getAdvancedSearch_coating.xml").read_text(encoding="utf-8")


def test_factorion_params_product_and_address():
    p = FactorionExtractor.build_params({"product": "하드코팅필름", "address": "충청북도", "rows": 50})
    assert p == {"pageNo": 1, "numOfRows": 50, "mainProductCn": "하드코팅필름", "adres": "충청북도"}
    assert FactorionExtractor.build_params({"company": "서진"})["cmpnyNm"] == "서진"
    with pytest.raises(ValueError):
        FactorionExtractor.build_params({"rows": 10})


def test_factorion_record_has_trust_fields():
    recs = FactorionExtractor()._parse(FAC_XML)
    assert all(r.has_factory and r.provenances[0].verified for r in recs)
    assert recs[0].employees == 21                     # allEmplyCo → 숫자
    assert all(r.since_year is None or 1950 < r.since_year < 2100 for r in recs)


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_factorion_search_pages_until_total(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params)
        return _Resp(FAC_XML)

    monkeypatch.setattr(structured.httpx, "get", fake_get)
    recs = FactorionExtractor(api_key="k").search(product="코팅", rows=10, max_pages=5)
    total = FactorionExtractor._total(FAC_XML)
    assert len(calls) == min(5, -(-total // 10))          # totalCount까지만 페이지 이동
    assert all(c["mainProductCn"] == "코팅" for c in calls)
    assert len(recs) == 10 * len(calls)


def test_is_company_applicant():
    assert is_company_applicant("주식회사 코팅코리아")
    assert is_company_applicant("Seojin Coating Co., Ltd.")
    assert not is_company_applicant("한국과학기술원 산학협력단")
    assert not is_company_applicant("서울대학교")
    assert not is_company_applicant("한국화학연구원")
    assert not is_company_applicant("홍길동")               # 개인 출원인


def test_kipris_tech_search_groups_by_applicant(monkeypatch):
    seen = {}

    def fake_get(url, params, timeout):
        seen.update(params)
        return _Resp(KIP_XML)

    monkeypatch.setattr(kipris_mod.httpx, "get", fake_get)
    recs = KiprisExtractor(api_key="k").search_tech(word="하드코팅", ipc="C09D")
    assert seen["word"] == "하드코팅" and seen["ipcNumber"] == "C09D"
    assert [r.name for r in recs] == ["주식회사 코팅코리아"]
    assert "특허 기술검색" in recs[0].provenances[0].note
    assert any(n.startswith("[특허]") for n in recs[0].notes)


def test_kipris_applicant_lookup_drops_other_applicants():
    mixed = KIP_XML.replace("<applicantName>주식회사 코팅코리아</applicantName>",
                            "<applicantName>코팅코리아테크 주식회사</applicantName>", 1)
    rec = KiprisExtractor()._parse(mixed, "코팅코리아")[0]
    # 첫 특허는 다른 출원인 → 제외, 건수는 일치분만
    assert "피복강관 제조방법" not in rec.products
    assert "특허 4건" in rec.provenances[0].note
