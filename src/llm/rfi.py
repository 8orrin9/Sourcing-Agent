"""LLM RFI 회신 — 시뮬 생성 + 파싱(증거 등급 승급).

데모 흐름: 숏리스트 업체에 RFI(정보 요청) 발송 → 회신의 인증서·시험성적서를 파싱해
역량표의 증거 수준을 L1→L2/L3로 승급. 실제 외부 메일 발송은 시연 불가라, 회신을
LLM으로 3분기 생성(완전/부분/무응답)한 뒤, 그 회신을 다시 파싱하는 왕복을 실연한다.

존 분리: RFI 본문·회신에 내부 단가·내부 품목코드·현 협력사명을 넣지 않는다.
"""

from __future__ import annotations

from ..schema import EvidenceLevel, SupplierRecord
from .client import complete_json

# 회신 시나리오 3분기 (데모 RFI_REPLY 계승: 완전회신/부분회신/무응답)
SCENARIOS = ("full", "partial", "none")

_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "responded": {"type": "boolean"},
        "body": {"type": "string"},
        "attachments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "doc_type": {
                        "type": "string",
                        "enum": ["certificate", "test_report", "spec_sheet", "company_profile", "other"],
                    },
                    "about_process": {"type": "string"},
                },
                "required": ["filename", "doc_type", "about_process"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["responded", "body", "attachments"],
    "additionalProperties": False,
}

_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "upgrades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "new_evidence": {"type": "string", "enum": ["L1", "L2", "L3"]},
                    "basis": {"type": "string"},
                },
                "required": ["code", "new_evidence", "basis"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["upgrades"],
    "additionalProperties": False,
}

_GEN_SYSTEM = (
    "너는 공급사 영업담당 역할로 RFI(정보 요청) 회신을 작성하는 시뮬레이터다. "
    "시나리오에 맞춰 현실적인 회신을 생성한다. "
    "full=요청 정보를 충실히 회신하고 인증서·시험성적서를 첨부, "
    "partial=일부만 회신하고 설비 사양 정도만 첨부, "
    "none=회신하지 않음(responded=false, body는 빈 문자열, attachments 빈 배열). "
    "내부 단가·품목코드·경쟁사명은 언급하지 말 것. 한국어로 작성."
)

_PARSE_SYSTEM = (
    "너는 RFI 회신 분석기다. 회신 본문과 첨부 목록을 보고, 각 공정(code)의 증거 수준이 "
    "어디까지 입증되는지 판정한다. L1=웹/구두 언급, L2=설비·사양·사진·공정흐름 확보, "
    "L3=인증서·시험성적서·납품실적 원본으로 입증. 첨부 문서 유형과 내용에 근거해 승급 항목만 "
    "반환하라. 근거 없는 승급 금지."
)


def generate_reply(rec: SupplierRecord, requested_processes: list[str], scenario: str) -> dict:
    """RFI 회신을 시나리오대로 생성. {'responded','body','attachments'}."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario는 {SCENARIOS} 중 하나")
    user = (
        f"공급사: {rec.name} ({rec.region or '지역 미상'})\n"
        f"요청 공정 정보: {', '.join(requested_processes)}\n"
        f"시나리오: {scenario}"
    )
    return complete_json(system=_GEN_SYSTEM, user=user, schema=_REPLY_SCHEMA, schema_name="rfi_reply")


def parse_reply(reply: dict, requested_processes: list[str]) -> list[dict]:
    """회신에서 증거 등급 승급 항목을 파싱. [{'code','new_evidence','basis'}]."""
    if not reply.get("responded"):
        return []
    atts = "\n".join(f"- {a['filename']} ({a['doc_type']}, 공정:{a['about_process']})"
                     for a in reply.get("attachments", [])) or "(첨부 없음)"
    user = (
        f"요청 공정: {', '.join(requested_processes)}\n\n"
        f"회신 본문:\n{reply.get('body', '')}\n\n첨부:\n{atts}"
    )
    out = complete_json(system=_PARSE_SYSTEM, user=user, schema=_PARSE_SCHEMA, schema_name="rfi_parse")
    return out["upgrades"]


def apply_upgrades(rec: SupplierRecord, upgrades: list[dict]) -> SupplierRecord:
    """파싱된 승급을 레코드 capabilities에 반영(상향만, 원본 복제 수정)."""
    order = {EvidenceLevel.L1: 1, EvidenceLevel.L2: 2, EvidenceLevel.L3: 3}
    m = rec.model_copy(deep=True)
    by_code = {c.code: c for c in m.capabilities}
    for up in upgrades:
        cap = by_code.get(up["code"])
        new = EvidenceLevel(up["new_evidence"])
        if cap and order[new] > order[cap.evidence]:
            cap.evidence = new
            cap.basis = f"{cap.basis or ''} → RFI회신: {up['basis']}".strip(" →")
            m.notes.append(f"[RFI 승급] {up['code']} → {new.value}")
    return m
