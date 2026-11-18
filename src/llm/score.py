"""스코어링 — 스펙 적합도 + 신뢰도 점수(결정적·규칙 기반·투명).

숏리스트 선정 근거는 "설명 가능"해야 한다(증거 우선·HITL). 그래서 점수는 LLM이 아니라
명시적 규칙으로 계산하고, 각 축의 사유를 함께 반환한다. LLM이 만든 건 스펙 항목별
판정(checks)·kind뿐이고, 그걸 검색 계획(search_plan)과 대조하는 산술은 여기서 한다.

배점(합 100) — 스펙 역할 분리를 그대로 따른다.
| 축 | 배점 | 근거 |
| --- | --- | --- |
| process   | 35 | 필수 공정 판정 × 증거 수준(L1=1·L2=2·L3=3, partial은 절반) |
| equipment | 10 | 필수 설비 충족 비율 |
| certs     | 10 | 필수 인증·환경 충족 비율 |
| trust     | 25 | 공장등록 10 · 규모(최소 종업원 충족) 8 · 업력 10년+ 4 · 특허 보유 3 |
| region    | 10 | 우선 국가 10 · 그 외 허용 국가 5 |
| preferred | 10 | 우대 항목(공정·설비·인증) 충족 + 근접 지역 |

게이트(점수와 무관하게 탈락): 유통사, 허용 국가 밖, 필수 공정 근거 0개.
확인 안 된 값(unknown)은 탈락시키지 않는다 — '현장 확인 필요'.

규모(최소 종업원)는 게이트가 아니라 경고다(2026-11-10 변경). 공장등록 종업원 수는 '사업장 단위
등록값'이라 회사 전체 규모가 아니고 갱신도 늦다. PMMA 라이브 실행에서 이 값으로 70곳 중 38곳이
관련성 판정 전에 탈락해, 실제 품목을 만드는 중소 가공사가 보이지도 않았다. 이제 기준 미달은
신뢰 점수의 규모 항목(8점)을 받지 못하고 scale_warning으로 표시만 한다(RFI에서 확인).
존 분리: 내부 단가·현 협력사 비교는 여기 없다(숏리스트 진입 후 내부망에서).
"""

from __future__ import annotations

from datetime import date

from ..schema import CompanyKind, EvidenceLevel, SupplierRecord

_EVIDENCE_PTS = {EvidenceLevel.L1: 1, EvidenceLevel.L2: 2, EvidenceLevel.L3: 3}

WEIGHTS = {"process": 35, "equipment": 10, "certs": 10, "trust": 25, "region": 10, "preferred": 10}
TRUST_PARTS = {"factory": 10, "scale": 8, "since": 4, "patent": 3}
CURRENT_YEAR = date.today().year

# 시도 표기 정규화 + 인접 시도(근접 지역 판정의 거리 근사 — 지오코딩 없이 결정적으로)
_SIDO = {
    "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주": "광주", "대전": "대전",
    "울산": "울산", "세종": "세종", "경기": "경기", "강원": "강원", "충북": "충북", "충청북도": "충북",
    "충남": "충남", "충청남도": "충남", "전북": "전북", "전라북도": "전북", "전남": "전남",
    "전라남도": "전남", "경북": "경북", "경상북도": "경북", "경남": "경남", "경상남도": "경남", "제주": "제주",
}
_ADJ = {
    "서울": {"경기", "인천"}, "인천": {"서울", "경기"}, "경기": {"서울", "인천", "강원", "충북", "충남"},
    "강원": {"경기", "충북", "경북"}, "충북": {"경기", "강원", "충남", "세종", "대전", "경북", "전북"},
    "충남": {"경기", "충북", "세종", "대전", "전북"}, "세종": {"충북", "충남", "대전"},
    "대전": {"충북", "충남", "세종"}, "전북": {"충북", "충남", "전남", "경북", "경남"},
    "전남": {"전북", "광주", "경남"}, "광주": {"전남"}, "경북": {"강원", "충북", "전북", "경남", "대구", "울산"},
    "경남": {"전북", "전남", "경북", "부산", "울산", "대구"}, "대구": {"경북", "경남"},
    "울산": {"경북", "경남", "부산"}, "부산": {"경남", "울산"}, "제주": set(),
}


def sido(text: str | None) -> str | None:
    for tok in (text or "").split():
        for k, v in _SIDO.items():
            if tok.startswith(k):
                return v
    return None


def _near_hit(rec: SupplierRecord, near: dict) -> bool:
    """같은 시도면 근접, km≥100이면 인접 시도도 근접으로 본다(근사)."""
    target, mine = sido(near.get("near")), sido(rec.address or rec.region)
    if not target or not mine:
        return False
    return mine == target or ((near.get("km") or 0) >= 100 and mine in _ADJ.get(target, set()))


def _ratio(checks, group: str, required: bool) -> tuple[float, int, list[str]]:
    """그룹 항목의 충족 점수 합(met=1·partial=0.5), 항목 수, 사유."""
    items = [c for c in checks if c.group == group and c.required is required]
    got = sum(1.0 if c.verdict == "met" else 0.5 if c.verdict == "partial" else 0 for c in items)
    return got, len(items), [f"{c.label}:{c.verdict}" for c in items]


def score_supplier(rec: SupplierRecord, plan: dict) -> dict:
    """레코드를 검색 계획에 대조해 0~100 점수 + 축별 근거 + 게이트 결과를 반환.

    {'score', 'breakdown': [{'axis','points','max','reason'}], 'passed_gate', 'gate_reasons'}
    """
    gates, bonus = plan["gates"], plan["bonus"]
    bd: list[dict] = []

    # 1) 필수 공정 — 증거 수준 가중
    procs = [c for c in rec.checks if c.group == "processes" and c.required]
    raw = sum(_EVIDENCE_PTS.get(c.evidence, 0) * (1 if c.verdict == "met" else 0.5)
              for c in procs if c.verdict in ("met", "partial") and c.evidence)
    n_proc = len([c for c in plan["checks"] if c["group"] == "processes" and c["required"]])
    pts = round(WEIGHTS["process"] * raw / (3 * n_proc)) if n_proc else 0
    have = [f"{c.label}:{c.evidence.value}" for c in procs
            if c.verdict in ("met", "partial") and c.evidence]
    bd.append({"axis": "process", "points": pts, "max": WEIGHTS["process"],
               "reason": f"필수 공정 {n_proc}개 중 {len(have)}개 근거({', '.join(have) or '없음'})"})

    # 2·3) 필수 설비·인증
    for axis, group in (("equipment", "equipment"), ("certs", "certs")):
        got, n, why = _ratio(rec.checks, group, True)
        n = n or len([c for c in plan["checks"] if c["group"] == group and c["required"]])
        bd.append({"axis": axis, "points": round(WEIGHTS[axis] * got / n) if n else 0,
                   "max": WEIGHTS[axis], "reason": ", ".join(why) or "판정 없음"})

    # 4) 신뢰 — 공장등록·규모·업력·특허
    min_emp = gates.get("min_employees")
    factory = bool(rec.has_factory) and any(p.verified for p in rec.provenances)
    scale_ok = rec.employees is not None and rec.employees >= (min_emp or 50)
    since_ok = rec.since_year is not None and CURRENT_YEAR - rec.since_year >= 10
    patent = any(p.source_id == "kipris" for p in rec.provenances)
    t = (TRUST_PARTS["factory"] * factory + TRUST_PARTS["scale"] * scale_ok
         + TRUST_PARTS["since"] * since_ok + TRUST_PARTS["patent"] * patent)
    emp_txt = f"종업원 {rec.employees}명" if rec.employees is not None else "종업원 확인 필요"
    bd.append({"axis": "trust", "points": t, "max": WEIGHTS["trust"],
               "reason": " · ".join([
                   "공장등록 확인" if factory else "공장등록 미확인", emp_txt,
                   f"업력 {CURRENT_YEAR - rec.since_year}년" if rec.since_year else "업력 미상",
                   "특허 보유" if patent else "특허 없음"])})

    # 5) 지역
    allowed, priority = gates.get("countries") or [], bonus.get("priority_countries") or []
    if rec.country and rec.country in priority:
        reg = WEIGHTS["region"]
    elif rec.country and rec.country in allowed:
        reg = WEIGHTS["region"] // 2
    else:
        reg = 0
    bd.append({"axis": "region", "points": reg, "max": WEIGHTS["region"],
               "reason": f"국가 {rec.country or '미상'} · {rec.region or rec.address or '지역 미상'}"})

    # 6) 우대 — 우대 항목 충족 + 근접 지역
    got = n = 0
    why = []
    for group in ("processes", "equipment", "certs"):
        g, k, w = _ratio(rec.checks, group, False)
        got, n, why = got + g, n + k, why + w
    for nr in bonus.get("near") or []:
        hit = _near_hit(rec, nr)
        got, n = got + hit, n + 1
        why.append(f"{nr['near']} {nr.get('km') or ''}km:{'근접' if hit else '아님'}")
    bd.append({"axis": "preferred", "points": round(WEIGHTS["preferred"] * got / n) if n else 0,
               "max": WEIGHTS["preferred"], "reason": ", ".join(why) or "우대 조건 없음"})

    # 게이트
    reasons = []
    if gates.get("exclude_traders") and rec.kind is CompanyKind.TRADER:
        reasons.append("유통·무역사")
    if allowed and rec.country and rec.country not in allowed:
        reasons.append(f"허용 지역 밖({rec.country})")
    if n_proc and not have:
        reasons.append("필수 공정 근거 없음")

    return {"score": sum(x["points"] for x in bd), "breakdown": bd,
            "passed_gate": not reasons, "gate_reasons": reasons,
            "scale_warning": scale_warning(rec, plan)}


def scale_warning(rec: SupplierRecord, plan: dict) -> str | None:
    """최소 종업원 기준 미달 경고(탈락 아님). 값을 모르면 None."""
    min_emp = plan["gates"].get("min_employees")
    if min_emp and rec.employees is not None and rec.employees < min_emp:
        return (f"종업원 {rec.employees}명 < 기준 {min_emp}명 "
                f"(공장등록 사업장 단위 값 — 회사 전체 규모는 RFI로 확인)")
    return None


def pre_gate(rec: SupplierRecord, plan: dict) -> str | None:
    """상세 수집 전 신뢰 게이트 — 이미 알려진 값만으로 탈락 사유를 낸다(없으면 None).

    비싼 상세 수집·LLM 판정 전에 지역·유통사를 먼저 거른다. 모르는 값은 통과.
    규모(종업원)는 거르지 않는다 — scale_warning 참고.
    """
    g = plan["gates"]
    if g.get("exclude_traders") and rec.kind is CompanyKind.TRADER:
        return "유통·무역사"
    if g.get("countries") and rec.country and rec.country not in g["countries"]:
        return f"허용 지역 밖({rec.country})"
    return None
