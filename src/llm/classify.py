"""LLM 판정 — ① 관련성 1차 선별(배치) ② 스펙 항목별 체크(업체별).

① screen_candidates: 공장등록 주생산품은 자유어라 '하드코팅'으로 검색해도 하드코팅 강판·
   종이 코팅이 섞인다. 상세 수집(비싼 단계) 전에 생산품·업종·특허 제목만 보고 요청 품목의
   핵심 공정과 관련 있는 회사만 남긴다. 25건씩 묶어 한 번에 판정한다.

② classify_supplier: 상세 수집까지 끝난 업체를 스펙의 공정·설비·인증 항목 하나하나에
   대조해 met/partial/unmet/unknown + 증거 수준(L1~L3) + 근거를 받는다. 판정 대상이
   코드에 박힌 공정 목록이 아니라 '스펙 항목' 자체라서 품목이 바뀌어도 그대로 동작한다.
   공정 항목 판정은 capabilities에도 옮겨 담는다(RFI 승급 등 기존 사용처 호환).

증거 우선: 판정마다 근거(basis)를 남기고, 텍스트에 없는 것은 unknown(감점 아닌 '확인 필요').

근거 검증(2026-11-10, PMMA 라이브 실행 후 보강) — LLM 판정을 코드가 다시 확인한다.
- 판정마다 출처 종류(source)와 근거 원문 인용(quote)을 받는다. quote가 실제로 넘긴 수집
  텍스트에 없으면 그 판정은 unknown으로 내린다(수집 실패인데 '설비 사양 확보'로 본 사례 차단).
- 출처 종류별 증거 수준 상한을 코드로 강제한다. 특허 '제목'은 기술 보유 근거일 뿐 양산 공정
  근거가 아니라 L1이 상한이다(특허 제목만으로 L2 met → 특허 많은 대기업이 상위 독식하던 문제).
  | source           | 필요한 수집 근거                 | 상한 |
  | factory_registry | 공장등록(생산품·업종)            | L1 |
  | patent           | [특허] 제목                      | L1 |
  | search           | [검색근거] 검색 제목·스니펫      | L1 |
  | homepage         | 홈페이지 상세 수집([설비]·[인증] 등) | L2 |
  | document         | [문서]·[문서발췌] 카탈로그·인증서 | L3 |
"""

from __future__ import annotations

import re

from ..schema import CompanyKind, EvidenceLevel, ProcessCapability, SpecCheck, SupplierRecord
from .client import complete_json

SCREEN_BATCH = 25

# 출처 종류별 증거 수준 상한
SOURCE_CAP = {
    "factory_registry": EvidenceLevel.L1,
    "patent": EvidenceLevel.L1,
    "search": EvidenceLevel.L1,
    "homepage": EvidenceLevel.L2,
    "document": EvidenceLevel.L3,
}
_LEVEL = {EvidenceLevel.L1: 1, EvidenceLevel.L2: 2, EvidenceLevel.L3: 3}
MIN_QUOTE = 2  # 정규화 후 이보다 짧은 인용은 검증 불가로 본다

_SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "relevant", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SCREEN_SYSTEM = (
    "너는 제조 소싱 1차 선별자다. 요청 품목과 핵심 공정, 그리고 후보 회사 목록(번호·회사명·"
    "생산품/업종·특허 제목)이 주어진다. 각 후보가 요청 품목의 핵심 공정을 수행할 가능성이 있는지 "
    "판정한다. 같은 공정명이라도 소재·용도가 전혀 다르면(예: 강판 하드코팅, 종이 코팅, 도장) "
    "relevant=false. 인접 산업이라도 같은 소재·공정이면(예: 자동차 내장용 아크릴 하드코팅) true. "
    "정보가 너무 적어 판단이 어려우면 true로 남겨 상세 수집에서 확인하게 한다. reason은 한 문장."
)

_CHECK_SYSTEM = (
    "너는 B2B 소싱 애널리스트다. 공급사 후보의 수집 정보(공장등록·특허·홈페이지·카탈로그/인증서 "
    "발췌)를 보고 (1) 실제 '제조 기업'인지, (2) 소싱 스펙 항목 하나하나를 충족하는지 판정한다.\n"
    "제조사가 아닌 것(is_manufacturer=false): 단순 유통·무역사, 대리점, 연구기관, 문서·디렉터리.\n"
    "항목 판정 verdict: met=근거로 충족 확인, partial=일부만 충족(예: 등급 미달, 외주 처리), "
    "unmet=충족하지 않음이 드러남, unknown=정보 없음(추측 금지).\n"
    "evidence: L1=웹·소개 문구 언급만, L2=설비 사양·사진·제품 사양 확보, "
    "L3=인증서·시험성적서·납품 실적 문서로 입증. unknown이면 null.\n"
    "source: 근거가 나온 곳 — factory_registry(공장등록 생산품·업종), patent([특허] 제목), "
    "search([검색근거] 검색 결과), homepage(홈페이지 수집: [설비]·[인증]·소개 문구), "
    "document([문서]·[문서발췌] 카탈로그·인증서). unknown이면 none.\n"
    "quote: 위 수집 정보에서 근거가 된 부분을 '그대로' 짧게 복사(20자 안팎). 바꿔 쓰거나 지어내지 말 것. "
    "unknown이면 빈 문자열.\n"
    "주의: 특허 제목은 '그 기술을 보유했다'는 뜻이지 그 공정을 양산한다는 뜻이 아니다 → evidence는 L1. "
    "회사가 유명하다는 사전 지식으로 판정하지 말고, 주어진 수집 정보에 있는 것만 쓴다.\n"
    "basis에는 근거 문장과 출처(예: '[문서] ISO9001.pdf', '공장등록 생산품')를 적는다."
)


def screen_candidates(records: list[SupplierRecord], *, target: str,
                      processes: list[str]) -> tuple[list[SupplierRecord], list[tuple[SupplierRecord, str]]]:
    """관련성 1차 선별 → (남긴 레코드, [(제외 레코드, 사유)])."""
    kept: list[SupplierRecord] = []
    dropped: list[tuple[SupplierRecord, str]] = []
    for start in range(0, len(records), SCREEN_BATCH):
        chunk = records[start:start + SCREEN_BATCH]
        lines = []
        for i, r in enumerate(chunk):
            patents = [n[4:].strip() for n in r.notes if n.startswith("[특허]")][:3]
            lines.append(f"[{i}] {r.name} | 생산품/업종: {', '.join(r.products) or '미상'}"
                         + (f" | 특허: {'; '.join(patents)}" if patents else ""))
        user = (f"[요청 품목] {target}\n[핵심 공정] {', '.join(processes)}\n\n[후보]\n" + "\n".join(lines))
        out = complete_json(system=_SCREEN_SYSTEM, user=user, schema=_SCREEN_SCHEMA,
                            schema_name="screen")
        verdict = {x["index"]: x for x in out["results"]}
        for i, r in enumerate(chunk):
            v = verdict.get(i)
            if v is None or v["relevant"]:  # 응답 누락은 남긴다(보수적)
                kept.append(r)
            else:
                dropped.append((r, v["reason"]))
    return kept, dropped


def _check_schema(item_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "is_manufacturer": {"type": "boolean"},
            "kind_reason": {"type": "string"},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "enum": item_ids},
                        "verdict": {"type": "string", "enum": ["met", "partial", "unmet", "unknown"]},
                        "evidence": {"type": ["string", "null"], "enum": ["L1", "L2", "L3", None]},
                        "source": {"type": "string", "enum": [*SOURCE_CAP, "none"]},
                        "quote": {"type": "string"},
                        "basis": {"type": "string"},
                    },
                    "required": ["item_id", "verdict", "evidence", "source", "quote", "basis"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["is_manufacturer", "kind_reason", "checks"],
        "additionalProperties": False,
    }


def classify_supplier(rec: SupplierRecord, *, checks: list[dict]) -> SupplierRecord:
    """스펙 항목(search_plan['checks'])별로 판정해 kind·checks·capabilities를 채운다(복제 수정).

    공장등록 레코드는 '공장이 있는 제조사'라는 공적 근거가 있어 LLM이 유통사로 판정해도
    제조사로 유지한다(LLM 판단보다 공적 기록 우선).
    """
    by_id = {c["id"]: c for c in checks}
    user = _build_user(rec, checks)
    out = complete_json(system=_CHECK_SYSTEM, user=user,
                        schema=_check_schema(list(by_id)), schema_name="spec_check")
    evidence_text = user.split("\n판정할 스펙 항목:")[0]  # 인용 검증은 수집 정보 부분에서만

    updated = rec.model_copy(deep=True)
    if rec.has_factory:
        updated.kind = CompanyKind.MANUFACTURER
    else:
        updated.kind = CompanyKind.MANUFACTURER if out["is_manufacturer"] else CompanyKind.TRADER
    updated.notes.append(f"[LLM] {out['kind_reason']}")

    got = {c["item_id"]: c for c in out["checks"] if c["item_id"] in by_id}
    updated.checks = []
    for item_id, spec in by_id.items():
        c = got.get(item_id, {"verdict": "unknown", "evidence": None, "basis": "판정 누락"})
        verdict, ev, basis = verify_check(c, rec, evidence_text)
        updated.checks.append(SpecCheck(item_id=item_id, group=spec["group"], label=spec["label"],
                                        required=spec["required"], verdict=verdict,
                                        evidence=ev, basis=basis))

    existing = {c.code for c in updated.capabilities}
    for ck in updated.checks:
        code = by_id[ck.item_id].get("code")
        if ck.group == "processes" and code and ck.verdict in ("met", "partial") and ck.evidence \
                and code not in existing:
            updated.capabilities.append(ProcessCapability(code=code, label=ck.label,
                                                          evidence=ck.evidence, basis=ck.basis))
    return updated


def _squash(text: str) -> str:
    return re.sub(r"[\s\"'“”‘’`]+", "", text).lower()


def available_sources(rec: SupplierRecord) -> set[str]:
    """이 레코드에 실제로 수집된 근거 종류 — LLM이 없는 출처를 대는 것을 막는다."""
    notes = rec.notes
    have = set()
    if rec.has_factory and rec.products:
        have.add("factory_registry")
    if any(n.startswith("[특허]") for n in notes):
        have.add("patent")
    if any(n.startswith("[검색근거]") for n in notes):
        have.add("search")
    if any(p.source_id == "site_detail" for p in rec.provenances):
        have.add("homepage")
    if any(n.startswith(("[문서]", "[문서발췌]")) for n in notes):
        have.add("document")
    return have


def verify_check(c: dict, rec: SupplierRecord, evidence_text: str) -> tuple[str, EvidenceLevel | None, str]:
    """LLM 판정 1건을 규칙으로 검증 → (verdict, evidence, basis).

    1) unknown은 그대로. 2) 출처가 실제 수집 근거에 없으면 unknown.
    3) 인용(quote)이 수집 텍스트에 없으면 unknown. 4) 출처별 상한으로 증거 수준을 낮춘다.
    """
    verdict, basis = c["verdict"], c.get("basis") or ""
    if verdict == "unknown":
        return "unknown", None, basis
    src, quote = c.get("source") or "none", (c.get("quote") or "").strip()
    if src not in SOURCE_CAP or src not in available_sources(rec):
        return "unknown", None, f"근거 출처 확인 불가({src}) — {basis}"
    q = _squash(quote)
    if len(q) < MIN_QUOTE or q not in _squash(evidence_text):
        return "unknown", None, f"근거 인용 확인 실패('{quote[:40]}') — {basis}"
    ev = EvidenceLevel(c["evidence"]) if c.get("evidence") else None
    cap = SOURCE_CAP[src]
    if verdict in ("met", "partial"):
        ev = EvidenceLevel.L1 if ev is None else (cap if _LEVEL[ev] > _LEVEL[cap] else ev)
    return verdict, ev, f"{basis} [출처 {src}: '{quote[:60]}']"


def _build_user(rec: SupplierRecord, checks: list[dict]) -> str:
    lines = [
        f"회사명: {rec.name}",
        f"생산품/업종: {', '.join(rec.products) if rec.products else '(미상)'}",
        f"소재지: {rec.region or rec.address or '(미상)'} / 국가: {rec.country or '(미상)'}",
        f"공장등록: {'있음' if rec.has_factory else '확인 안 됨'}"
        + (f" / 종업원 {rec.employees}명" if rec.employees is not None else ""),
        f"출처: {', '.join(p.note or p.source_id for p in rec.provenances)}",
    ]
    if rec.notes:
        lines.append("수집 근거:\n" + "\n".join(f"  - {n}" for n in rec.notes))
    lines.append("\n판정할 스펙 항목:")
    for c in checks:
        req = "필수" if c["required"] else "우대"
        extra = f", 최소 증거 {c['min_evidence']}" if c.get("min_evidence") else ""
        lines.append(f"  - {c['id']} [{req}{extra}] {c['label']}")
    return "\n".join(lines)
