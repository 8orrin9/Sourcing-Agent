"""LLM 판정 — 제조사 분류 + 공정 역량 추출.

확산 소스(웹검색)는 특허·학회·블로그·디렉터리 노이즈가 많다. 또 팩토리온 주생산품은
자유어라 키워드 매칭이 안 된다. 이 두 문제를 LLM 구조화 출력으로 해결한다:

1) is_manufacturer — 제조사 자체인지(디렉터리/특허/뉴스/연구기관 아님)
2) capabilities    — 소싱 스펙의 공정(HC/AF 등)을 보유하는지 + 증거 수준(L1~L3)

증거 우선: 판정 근거(reason)를 함께 받아 Provenance/notes에 남긴다.
"""

from __future__ import annotations

from ..schema import EvidenceLevel, ProcessCapability, SupplierRecord
from .client import complete_json

# 스펙의 공정 코드 → 설명 (데모 PROC_DEF 계승). 판정 대상 공정.
PROCESS_DEFS = {
    "HC": "하드코팅 (hard coating)",
    "AF": "내지문/지문방지 코팅 (anti-fingerprint coating)",
    "LASER": "레이저 커팅/가공 (laser cutting)",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "is_manufacturer": {"type": "boolean"},
        "kind_reason": {"type": "string"},
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": list(PROCESS_DEFS)},
                    "evidence": {"type": "string", "enum": ["L1", "L2", "L3"]},
                    "basis": {"type": "string"},
                },
                "required": ["code", "evidence", "basis"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_manufacturer", "kind_reason", "capabilities"],
    "additionalProperties": False,
}

_SYSTEM = (
    "너는 B2B 소싱 애널리스트다. 주어진 공급사 후보 정보(회사명·생산품·스니펫·출처)를 보고 "
    "(1) 이 주체가 실제 '제조 기업'인지, (2) 요청된 공정 역량 보유 여부와 증거 수준을 판정한다.\n"
    "제조사가 아닌 것(is_manufacturer=false)의 예: 특허 문서, 학술/학회 자료, 뉴스 기사, "
    "연구기관·대학, 블로그/카페 글, B2B 디렉터리·마켓플레이스 페이지, 단순 유통·무역사.\n"
    "판단 단서: 출처 도메인이 patents.*·*.or.kr(학회)·blog.*·news 등이면 제조사가 아니다. "
    "회사명이 기업명이 아니라 '문서 제목·기술 설명'처럼 길고 서술적이면 제조사가 아니다. "
    "애매하면 보수적으로 is_manufacturer=false로 판정하라.\n"
    "증거 수준: L1=웹 언급만, L2=설비·사양·사진 확보, L3=특허·인증·시험성적서·납품실적으로 입증. "
    "근거가 없으면 해당 공정을 넣지 말 것. 추측 금지, 텍스트에 드러난 근거만 사용."
)


def classify_supplier(rec: SupplierRecord, *, processes: list[str] | None = None) -> SupplierRecord:
    """레코드를 LLM으로 판정해 kind·capabilities·notes를 채워 반환(원본 복제 수정).

    processes: 판정할 공정 코드 목록(기본 전체). 소싱 스펙에 맞춰 좁힐 수 있다.
    """
    codes = processes or list(PROCESS_DEFS)
    wanted = {c: PROCESS_DEFS[c] for c in codes if c in PROCESS_DEFS}

    user = _build_user(rec, wanted)
    out = complete_json(system=_SYSTEM, user=user, schema=_SCHEMA, schema_name="classify")

    from ..schema import CompanyKind

    caps = [
        ProcessCapability(
            code=c["code"],
            label=PROCESS_DEFS.get(c["code"], c["code"]),
            evidence=EvidenceLevel(c["evidence"]),
            basis=c["basis"],
        )
        for c in out["capabilities"]
        if c["code"] in wanted
    ]

    updated = rec.model_copy(deep=True)
    updated.kind = CompanyKind.MANUFACTURER if out["is_manufacturer"] else CompanyKind.TRADER
    # 기존 capabilities와 병합(앵커가 넣은 것 우선 유지 후 추가)
    existing = {c.code for c in updated.capabilities}
    updated.capabilities.extend(c for c in caps if c.code not in existing)
    updated.notes.append(f"[LLM] {out['kind_reason']}")
    return updated


def _build_user(rec: SupplierRecord, wanted: dict[str, str]) -> str:
    lines = [
        f"회사명: {rec.name}",
        f"생산품/업종: {', '.join(rec.products) if rec.products else '(미상)'}",
        f"소재지: {rec.region or '(미상)'}",
        f"출처: {', '.join(p.note or p.source_id for p in rec.provenances)}",
    ]
    if rec.notes:
        lines.append(f"스니펫: {' / '.join(rec.notes)}")
    lines.append("")
    lines.append("판정할 공정:")
    for code, desc in wanted.items():
        lines.append(f"  - {code}: {desc}")
    return "\n".join(lines)
