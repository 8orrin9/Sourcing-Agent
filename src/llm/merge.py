"""엔티티 병합 — 특허 출원인·웹 기업 ↔ 공장등록(앵커) 레코드.

공장등록 우선 흐름(2026-11-10)에서 특허 출원인·웹에서 찾은 회사는 공장등록 회사명 조회로
'공장이 있는 제조사'인지 확정한다(resolve_to_factory). 사업자번호가 없어(팩토리온 미제공)
결정적 키가 없고 표기 변형('서진코팅' vs '(주)서진코팅')이 많아, 정규화 회사명이 정확히
같으면 규칙으로, 포함관계면 LLM으로 "같은 회사인가"를 판정한다.

병합 방향: 앵커(검증됨)를 기준(base)으로, 다른 소스의 URL·특허·스니펫을 흡착한다.
결과 레코드는 verified=True(앵커 근거) + 다른 출처도 provenances에 누적.
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


_LEGAL = re.compile(r"\(주\)|㈜|주식회사|\(유\)|㈲|유한회사|co\.?,?\s*ltd\.?|inc\.?|corp\.?", re.I)


def _norm_name(name: str) -> str:
    """법인격·공백·괄호 제거한 거친 정규화(1차 후보 좁히기용)."""
    return re.sub(r"\s+", "", _LEGAL.sub("", name)).lower()


def query_name(name: str) -> str:
    """공장등록 회사명(부분일치) 조회용 — 법인격만 떼고 띄어쓰기는 유지."""
    return re.sub(r"\s+", " ", _LEGAL.sub("", name)).strip(" ,.")


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
            prev = by_norm[key]
            merged = merge_into(prev, a)
            # 종업원은 사업장 합산, 업력은 가장 이른 공장등록 연도
            if prev.employees is not None or a.employees is not None:
                merged.employees = (prev.employees or 0) + (a.employees or 0)
            years = [y for y in (prev.since_year, a.since_year) if y]
            merged.since_year = min(years) if years else None
            by_norm[key] = merged
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
                  "contact_dept", "contact_public", "name_normalized", "biz_reg_no",
                  "employees", "since_year", "has_factory"):
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


def resolve_to_factory(
    rec: SupplierRecord,
    anchors: list[SupplierRecord],
    *,
    min_confidence: str = "medium",
) -> SupplierRecord | None:
    """rec(특허 출원인·웹 기업)를 공장등록 조회 결과(anchors) 중 같은 회사에 붙인다.

    정규화명이 같으면 규칙으로 확정, 포함관계면 LLM 판정(min_confidence 이상)으로 확정.
    확정되면 앵커 기준 병합 레코드, 아니면 None.
    """
    rank = {"low": 0, "medium": 1, "high": 2}
    want = _norm_name(rec.name)
    if not want:
        return None
    for a in anchors:
        if _norm_name(a.name) == want:
            return merge_into(a, rec)
    for a in anchors:
        an = _norm_name(a.name)
        if an and (want in an or an in want):
            verdict = is_same_company(a, rec)
            if verdict["same_company"] and rank[verdict["confidence"]] >= rank[min_confidence]:
                m = merge_into(a, rec)
                m.notes.append(f"[병합] {verdict['reason']}")
                return m
    return None
