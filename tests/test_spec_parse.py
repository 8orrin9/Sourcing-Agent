"""STEP 0 스펙 파싱·검색 계획 테스트 (오프라인).

LLM 호출(complete_json)은 고정 응답으로 바꿔 끼워, 프롬프트 조립·후처리·검색 계획의
결정적 로직만 검증한다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.llm import spec_parse
from src.search_plan import build_search_plan

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "data" / "synthetic" / "parsed_spec_pmma.json").read_text(encoding="utf-8"))
PDF = ROOT / "data" / "fixtures" / "spec_doc" / "spec_sample.pdf"


def test_read_document_extracts_pdf_text():
    text = spec_parse.read_document(PDF)
    assert "연필경도 3H" in text and "ISO 9001" in text


def test_parse_spec_sends_doc_and_hides_item_code(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        raw = {k: SPEC[k] for k in ("target_item", "item_terms", "mapping_notes")}
        raw["items"] = [{k: v for k, v in it.items() if k != "id"} for it in SPEC["items"]]
        return raw

    monkeypatch.setattr(spec_parse, "complete_json", fake)
    out = spec_parse.parse_spec("PMMA 시트 대체 공급원 탐색", document=PDF,
                                request_id="SR-1", item="MAT-PM-0512")
    assert "연필경도 3H" in seen["user"]          # 사양서 본문이 프롬프트에 들어감
    assert "MAT-PM-0512" not in seen["user"]       # 내부 품목코드는 LLM에 안 보냄
    assert out["item"] == "MAT-PM-0512" and out["request_id"] == "SR-1"
    assert [i["id"] for i in out["items"][:4]] == ["P1", "P2", "P3", "P4"]
    assert out["items"][-1]["id"] == "X2"


def test_ui_spec_roundtrip_and_selection():
    ui = spec_parse.to_ui_spec(SPEC)
    assert ui["processes"]["required"] == ["하드코팅(HC) L2 이상", "AF 코팅 L2 이상", "레이저 커팅 L1 이상"]
    assert ui["region"]["preferred"] == ["베트남", "아산 사업장 150km 이내"]
    # 담당자가 AF를 우대로 내리고 ISO 14001을 삭제
    ui["processes"]["required"].remove("AF 코팅 L2 이상")
    ui["processes"]["preferred"].append("AF 코팅 L2 이상")
    ui["certs"]["preferred"].remove("ISO 14001")
    sel = spec_parse.apply_selection(SPEC, ui)
    af = next(i for i in sel["items"] if i["code"] == "AF")
    assert af["required"] is False
    assert not any(i["label"] == "ISO 14001" for i in sel["items"])


def test_selection_adds_user_items():
    ui = spec_parse.to_ui_spec(SPEC)
    ui["certs"]["required"].append("IATF 16949")          # 담당자 직접 추가(필수)
    ui["equipment"]["preferred"].append("슬롯다이 코터")    # 직접 추가(우대)
    sel = spec_parse.apply_selection(SPEC, ui)
    added = {i["label"]: i for i in sel["items"] if i["source"] == "담당자 직접 추가"}
    assert set(added) == {"IATF 16949", "슬롯다이 코터"}
    assert added["IATF 16949"]["required"] and added["IATF 16949"]["group"] == "certs"
    assert added["슬롯다이 코터"]["required"] is False
    assert added["IATF 16949"]["terms"]["product"] == []  # 발견 검색어 없음 → 판정·가점에만
    plan = build_search_plan(sel)
    assert any(c["label"] == "IATF 16949" and c["required"] for c in plan["checks"])


def test_plan_roles_split():
    plan = build_search_plan(SPEC)
    d = plan["discovery"]
    # 필수 핵심 공정 + 품목 일반어만 발견 검색어. 우대 공정(PMMA 원판)·범용 공정(레이저)은 제외
    assert "하드코팅필름" in d["factorion_products"] and "아크릴시트" in d["factorion_products"]
    assert "아크릴판" not in d["factorion_products"]
    assert "B23K 26/38" not in d["kipris"]["ipc"]
    # 지역 → 로케일: 국내(KR) + 베트남(VI) + 해외가 있으니 영문(EN). 중국어는 없음
    assert set(d["web"]) == {"KR", "VI", "EN"}
    g = plan["gates"]
    assert g["countries"] == ["KR", "VN"] and g["min_employees"] == 50
    assert g["exclude_traders"] and g["exclude_incumbent"]
    assert plan["bonus"]["priority_countries"] == ["KR"]
    assert plan["bonus"]["min_revenue_krw"] == 10_000_000_000
    assert {c["id"] for c in plan["checks"]} >= {"P1", "P3", "E1", "C2"}


def test_plan_follows_required_toggle():
    spec = copy.deepcopy(SPEC)
    next(i for i in spec["items"] if i["code"] == "AF")["required"] = False
    d = build_search_plan(spec)["discovery"]
    assert "내지문코팅" not in d["factorion_products"]   # 우대로 내리면 발견 검색어에서 빠짐


def test_plan_removes_internal_code_and_forbidden_terms():
    spec = copy.deepcopy(SPEC)
    spec["items"][0]["terms"]["product"].append("MAT-PM-0512 시트")
    spec["items"][0]["terms"]["web"]["KR"].append("한결케미칼 하드코팅")
    plan = build_search_plan(spec, forbidden=["한결케미칼"])
    assert "MAT-PM-0512 시트" not in plan["discovery"]["factorion_products"]
    assert all("한결" not in q for q in plan["discovery"]["web"]["KR"])
    assert set(plan["removed"]) == {"MAT-PM-0512 시트", "한결케미칼 하드코팅"}
