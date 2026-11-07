"""LLM 엔티티 병합 — 확산 후보 ↔ 앵커 레코드 흡착.

웹검색은 회사명·URL만 주고, 팩토리온 앵커는 주소·공장·생산품을 준다. 같은 회사의
서로 다른 조각을 하나로 합쳐야 한다. 사업자번호가 없어(팩토리온 미제공) 결정적 키가
없고, 회사명 표기 변형('서진코팅' vs '(주)서진코팅' vs '서진코팅(주)')이 많아
규칙 매칭이 취약하다. → LLM으로 "같은 회사인가"를 판정.

병합 방향: 앵커(검증됨)를 기준(base)으로, 확산 후보의 URL·스니펫을 흡착한다.
결과 레코드는 verified=True(앵커 근거) + 확산 출처도 provenances에 누적.
"""

from __future__ import annotations

import re

from ..schema import SupplierRecord
from .client import complete_json

_SCHEMA = {
    "type": "object",
    "properties": {
        "same_company": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["same_company", "confidence", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "너는 기업 엔티티 해석기다. 두 레코드가 '동일한 실제 기업'을 가리키는지 판정한다. "
    "회사명은 법인격 표기((주)·㈜·Co.,Ltd 등)·띄어쓰기·영문/한글 변형이 흔하니 이를 감안하라. "
    "지역·생산품이 모순되면 다른 회사다. 애매하면 confidence=low로 두되 same_company 판단은 신중히. "
    "근거(reason)를 반드시 제시하라."
)


def _norm_name(name: str) -> str:
    """법인격·공백·괄호 제거한 거친 정규화(1차 후보 좁히기용)."""
    n = re.sub(r"\(주\)|㈜|주식회사|\(유\)|㈲|유한회사|co\.?,?\s*ltd\.?|inc\.?|corp\.?",
               "", name, flags=re.I)
    return re.sub(r"\s+", "", n).lower()


def dedupe_factories(anchors: list[SupplierRecord]) -> list[SupplierRecord]:
    """같은 기업의 여러 공장 레코드를 회사명 기준으로 1건으로 접는다(결정적).

    팩토리온은 한 기업의 사업장을 각각 등록해 같은 회사명이 주소만 다른 여러 레코드로
    나온다. 정규화 회사명이 같으면 첫 레코드에 흡착하고, 공장 수를 notes에 남긴다.
    LLM 없이 규칙으로 처리(회사명 완전일치 수준이라 안전).
    """
    by_norm: dict[str, SupplierRecord] = {}
    counts: dict[str, int] = {}
    for a in anchors:
        key = _norm_name(a.name)
        if key in by_norm:
            by_norm[key] = merge_into(by_norm[key], a)
            counts[key] += 1
        else:
            by_norm[key] = a.model_copy(deep=True)
            counts[key] = 1
    out = []
    for key, rec in by_norm.items():
        if counts[key] > 1:
            rec.notes.append(f"[사업장 {counts[key]}개 통합]")
        out.append(rec)
    return out


def is_same_company(a: SupplierRecord, b: SupplierRecord) -> dict:
    """두 레코드가 동일 기업인지 LLM 판정. {'same_company','confidence','reason'}."""
    user = (
        f"[A]\n회사명: {a.name}\n지역: {a.region or '미상'}\n생산품: {', '.join(a.products) or '미상'}\n"
        f"[B]\n회사명: {b.name}\n지역: {b.region or '미상'}\n생산품: {', '.join(b.products) or '미상'}"
    )
    return complete_json(system=_SYSTEM, user=user, schema=_SCHEMA, schema_name="entity_match")


def merge_into(base: SupplierRecord, other: SupplierRecord) -> SupplierRecord:
    """other의 정보를 base에 흡착(base 우선, 빈 필드만 채우고 출처·노트는 누적)."""
    m = base.model_copy(deep=True)
    for field in ("homepage", "domain", "country", "region", "address",
                  "contact_dept", "contact_public", "name_normalized", "biz_reg_no"):
        if getattr(m, field) in (None, "") and getattr(other, field):
            setattr(m, field, getattr(other, field))
    for p in other.products:
        if p not in m.products:
            m.products.append(p)
    for c in other.capabilities:
        if c.code not in {x.code for x in m.capabilities}:
            m.capabilities.append(c)
    m.provenances.extend(other.provenances)
    for n in other.notes:
        if n not in m.notes:
            m.notes.append(n)
    return m


def absorb_candidates(
    anchors: list[SupplierRecord],
    candidates: list[SupplierRecord],
    *,
    min_confidence: str = "medium",
) -> tuple[list[SupplierRecord], list[SupplierRecord]]:
    """확산 후보를 앵커에 흡착. (병합된 앵커들, 흡착 못 한 잔여 후보) 반환.

    1차로 정규화 회사명이 겹치는 앵커만 LLM에 물어 호출 수를 줄인다(비용 절감).
    min_confidence 이상일 때만 병합.
    """
    rank = {"low": 0, "medium": 1, "high": 2}
    threshold = rank[min_confidence]
    merged = [a.model_copy(deep=True) for a in anchors]
    anchor_norms = [_norm_name(a.name) for a in merged]
    leftover: list[SupplierRecord] = []

    for cand in candidates:
        cn = _norm_name(cand.name)
        # 1차 필터: 정규화명이 서로 포함관계인 앵커 후보만
        idxs = [i for i, an in enumerate(anchor_norms) if cn and (cn in an or an in cn)]
        hit = False
        for i in idxs:
            verdict = is_same_company(merged[i], cand)
            if verdict["same_company"] and rank[verdict["confidence"]] >= threshold:
                merged[i] = merge_into(merged[i], cand)
                merged[i].notes.append(f"[병합] {verdict['reason']}")
                hit = True
                break
        if not hit:
            leftover.append(cand)
    return merged, leftover
