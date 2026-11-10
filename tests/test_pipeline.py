"""파이프라인 테스트 (오프라인) — 가짜 소스·고정 LLM 응답으로 공장등록 우선 흐름 전체를 돌린다.

외부 API(팩토리온·KIPRIS·SerpApi·홈페이지)는 Sources에 가짜를 끼우고, LLM 호출은
모듈의 complete_json/판정 함수를 고정 응답으로 바꿔 결정적으로 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import pipeline
from src.llm.merge import _norm_name
from src.schema import CompanyKind, Provenance, SourceClass, SupplierRecord
from src.search_plan import build_search_plan

SPEC = json.loads((Path(__file__).resolve().parents[1] / "data" / "synthetic"
                   / "parsed_spec_pmma.json").read_text(encoding="utf-8"))


def _factory(name, *, emp, addr="충청북도 청주시 흥덕구", fid="F1", products=("하드코팅필름",), home=None):
    return SupplierRecord(
        name=name, country="KR", region=" ".join(addr.split()[:2]), address=addr,
        kind=CompanyKind.MANUFACTURER, has_factory=True, employees=emp, since_year=2005,
        products=list(products), homepage=home,
        provenances=[Provenance(source_id="factorion", source_class=SourceClass.ANCHOR,
                                extractor="structured", verified=True, note=f"공장관리번호 {fid}")])


class FakeFactorion:
    def __init__(self):
        self.calls = []

    def search(self, *, product, rows, max_pages):
        self.calls.append(product)
        if product == "하드코팅필름":
            return [_factory("(주)서진코팅", emp=84, fid="F1"),
                    _factory("서진코팅", emp=40, fid="F2", addr="충청북도 음성군 대소면"),
                    _factory("대한강판", emp=200, fid="F3", products=("하드코팅 강판",)),
                    _factory("소규모코팅", emp=12, fid="F4")]
        if product == "하드코팅시트":
            return [_factory("(주)서진코팅", emp=84, fid="F1")]  # 같은 공장 중복
        return []

    def extract(self, query):
        name = query["company"]
        if name == "누리옵틱스":
            return [_factory("누리옵틱스(주)", emp=67, fid="F9", addr="경기도 안산시 단원구",
                             home="https://nurioptics.example")]
        return []


class FakeKipris:
    def search_tech(self, word=None, ipc=None):
        if word == "하드코팅" and not ipc:
            return [SupplierRecord(name="주식회사 누리옵틱스", country="KR", notes=["[특허] 하드코팅 시트"],
                                   provenances=[Provenance(source_id="kipris",
                                                           source_class=SourceClass.ANCHOR,
                                                           extractor="structured")]),
                    SupplierRecord(name="공장없는랩 주식회사", country="KR", notes=["[특허] 코팅 조성물"])]
        return []

    def extract(self, query):
        return []


class FakeWeb:
    def search(self, q, *, locale, num):
        return [{"title": f"{q} 결과", "link": "https://x.example", "snippet": "", "domain": "x.example",
                 "aggregator": False, "locale": locale}]


class FakeSite:
    def extract(self, query):
        return [SupplierRecord(name="상세", employees=999, notes=["[설비] R2R 코터 2기",
                                                                   "[문서] cert.pdf (https://d.example/cert.pdf)"],
                               provenances=[Provenance(source_id="site_detail",
                                                       source_class=SourceClass.SPREAD,
                                                       extractor="site_detail")])]


@pytest.fixture
def patched(monkeypatch):
    # 웹: 해외 1곳(VN) 추출
    monkeypatch.setattr(pipeline, "extract_companies", lambda results, target: (
        [SupplierRecord(name="Minh An Film", country="VN", kind=CompanyKind.MANUFACTURER,
                        provenances=[Provenance(source_id="web_search", source_class=SourceClass.SPREAD,
                                                extractor="llm")])],
        [{"index": 0, "reason": "특허 문서", "title": "t"}]))
    # 관련성 선별: 강판은 무관
    monkeypatch.setattr(pipeline, "screen_candidates", lambda recs, target, processes: (
        [r for r in recs if "강판" not in r.name], [(r, "강판 코팅") for r in recs if "강판" in r.name]))
    monkeypatch.setattr(pipeline, "pick_homepage", lambda name, region, res: (None, "확신 없음"))
    monkeypatch.setattr(pipeline, "is_same_company",
                        lambda a, b: {"same_company": True, "confidence": "high", "reason": "테스트"})

    def fake_classify(rec, *, checks):
        from src.schema import EvidenceLevel, SpecCheck
        r = rec.model_copy(deep=True)
        r.checks = [SpecCheck(item_id=c["id"], group=c["group"], label=c["label"],
                              required=c["required"], verdict="met", evidence=EvidenceLevel.L2)
                    for c in checks]
        return r

    monkeypatch.setattr(pipeline, "classify_supplier", fake_classify)


def test_run_factorion_first_funnel(patched):
    fac = FakeFactorion()
    out = pipeline.run(SPEC, sources=pipeline.Sources(factorion=fac, kipris=FakeKipris(),
                                                       web=FakeWeb(), site=FakeSite()))
    # 발견 검색어는 계획의 생산품 검색어 순서대로
    assert fac.calls == build_search_plan(SPEC)["discovery"]["factorion_products"]
    c = out["counts"]
    assert c["factorion"] == 3        # 서진(공장 2곳 통합)·대한강판·소규모코팅
    assert c["kipris"] == 1           # 공장등록 확인된 출원인만(누리옵틱스)
    assert c["web"] == 1              # 해외 VN 미확인 유지
    names = [r["name"] for r in out["shortlist"]]
    assert "대한강판" not in names
    small = next(r for r in out["shortlist"] if r["name"] == "소규모코팅")   # 규모 미달은 남기고 경고
    assert small["scale_warning"].startswith("종업원 12명 < 기준 50명")
    reasons = {e["name"]: e["reason"] for e in out["excluded"]}
    assert "소규모코팅" not in reasons
    assert reasons["대한강판"] == "강판 코팅"
    seojin = next(r for r in out["shortlist"] if _norm_name(r["name"]) == "서진코팅")
    assert seojin["employees"] == 124                     # 사업장 합산(84+40), 중복 공장 제외
    assert seojin["verified"] and "site_detail" not in seojin["sources"]   # 홈페이지 미확보
    nuri = next(r for r in out["shortlist"] if r["name"].startswith("누리"))
    assert nuri["documents"] == ["cert.pdf (https://d.example/cert.pdf)"]
    assert nuri["employees"] == 67                        # 공장등록 값이 홈페이지 값보다 우선
    assert {"factorion", "kipris", "site_detail"} <= set(nuri["sources"])
    assert any(l["stage"] == "웹" and "1건 제외" in l["msg"] for l in out["log"])


def test_result_row_exposes_only_public_fields(patched):
    out = pipeline.run(SPEC, sources=pipeline.Sources(factorion=FakeFactorion(), kipris=FakeKipris(),
                                                       web=FakeWeb(), site=FakeSite()))
    row = out["shortlist"][0]
    assert not ({"unit_price", "mat_code", "incumbent", "internal_price", "item"} & set(row))
    assert "MAT-PM-0512" not in json.dumps(out["search_plan"]["discovery"], ensure_ascii=False)


def test_shortlist_respects_top_n_and_gate():
    plan = build_search_plan(SPEC)
    recs = [_factory(f"사{i}", emp=100, fid=f"F{i}") for i in range(8)]
    scored = pipeline.score_all(recs, plan)                # checks 없음 → 공정 근거 없음 → 게이트 탈락
    assert pipeline.shortlist(scored, top_n=3) == []
