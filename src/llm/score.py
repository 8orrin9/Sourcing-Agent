"""스코어링 — 스펙 적합도 점수(결정적·규칙 기반·투명).

숏리스트 선정 근거는 "설명 가능"해야 한다(증거 우선·HITL). 그래서 점수는 LLM이 아니라
명시적 규칙으로 계산하고, 각 항목의 가점 사유를 함께 반환한다. LLM이 만든 건
capabilities·kind뿐이고, 그걸 스펙과 대조하는 산술은 여기서 결정적으로 한다.

데모 스코어 축을 계승: 공정 역량(증거수준)·제조사 여부·소재지·검증(앵커)·규모.
존 분리: 내부 단가·현 협력사 비교는 여기 없다(숏리스트 진입 후 내부망에서).
"""

from __future__ import annotations

from ..schema import CompanyKind, EvidenceLevel, SupplierRecord

# 증거 수준 → 가중 (L3이 가장 강함)
_EVIDENCE_PTS = {EvidenceLevel.L1: 1, EvidenceLevel.L2: 2, EvidenceLevel.L3: 3}

# 배점(합 100). 데모 축과 정합: 역량 최우선, 제조사·검증·소재지·규모 보조.
WEIGHTS = {
    "capability": 45,  # 요청 공정 보유 + 증거수준
    "manufacturer": 20,  # 제조사 여부(유통사 제외 원칙)
    "verified": 15,  # 앵커 교차검증(식별 확정)
    "region": 12,  # 소재지 적합(국내·선호지역)
    "scale": 8,  # 규모(종업원 등)
}


def score_supplier(
    rec: SupplierRecord,
    *,
    required_processes: list[str],
    preferred_regions: list[str] | None = None,
) -> dict:
    """레코드를 스펙에 대조해 0~100 점수 + 항목별 근거를 반환.

    {'score': int, 'breakdown': [{'axis','points','max','reason'}], 'passed_gate': bool}
    passed_gate: 유통사이거나 요청 공정을 하나도 못 가지면 False(숏리스트 부적격).
    """
    preferred_regions = preferred_regions or []
    bd: list[dict] = []

    # 1) 공정 역량 — 요청 공정별 증거수준 합 / 최대치
    caps = {c.code: c for c in rec.capabilities}
    got = [caps[p] for p in required_processes if p in caps]
    max_cap_raw = len(required_processes) * _EVIDENCE_PTS[EvidenceLevel.L3]
    cap_raw = sum(_EVIDENCE_PTS[c.evidence] for c in got)
    cap_pts = round(WEIGHTS["capability"] * cap_raw / max_cap_raw) if max_cap_raw else 0
    bd.append({
        "axis": "capability", "points": cap_pts, "max": WEIGHTS["capability"],
        "reason": f"요청 {len(required_processes)}공정 중 {len(got)}개 보유"
                  f"({', '.join(f'{c.code}:{c.evidence.value}' for c in got) or '없음'})",
    })

    # 2) 제조사 여부
    mfr = WEIGHTS["manufacturer"] if rec.kind is CompanyKind.MANUFACTURER else 0
    bd.append({"axis": "manufacturer", "points": mfr, "max": WEIGHTS["manufacturer"],
               "reason": f"kind={rec.kind.value}"})

    # 3) 검증(앵커 교차검증)
    verified = any(p.verified for p in rec.provenances)
    ver = WEIGHTS["verified"] if verified else 0
    bd.append({"axis": "verified", "points": ver, "max": WEIGHTS["verified"],
               "reason": "앵커 검증됨" if verified else "미검증(확산만)"})

    # 4) 소재지 — 국내 또는 선호지역 매칭
    loc = f"{rec.country or ''} {rec.region or ''}".strip()
    region_hit = any(r and r in loc for r in preferred_regions) or (rec.country in ("KR", "Korea", "한국", "국내"))
    reg = WEIGHTS["region"] if region_hit else 0
    bd.append({"axis": "region", "points": reg, "max": WEIGHTS["region"],
               "reason": f"소재지='{loc or '미상'}'" + (" 적합" if region_hit else "")})

    # 5) 규모 — notes에 종업원수 단서가 있으면 가점(거친 휴리스틱)
    scale_hit = any("종업원" in n or "명" in n or "employ" in n.lower() for n in rec.notes)
    sc = WEIGHTS["scale"] if scale_hit else 0
    bd.append({"axis": "scale", "points": sc, "max": WEIGHTS["scale"],
               "reason": "규모 단서 있음" if scale_hit else "규모 미상"})

    score = sum(x["points"] for x in bd)
    passed_gate = rec.kind is not CompanyKind.TRADER and len(got) > 0
    return {"score": score, "breakdown": bd, "passed_gate": passed_gate}
