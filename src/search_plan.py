"""검색 계획 — 확정된 소싱 스펙을 '역할별'로 나눈다(결정적, LLM 없음).

스펙의 모든 항목이 검색어가 되면 검색 품질이 떨어진다. 항목마다 역할이 다르다.

| 역할 | 스펙 항목 | 쓰는 곳 |
| --- | --- | --- |
| discovery (발견) | 필수 공정의 검색어 + 품목 일반어 | 공장등록 생산품 검색, KIPRIS 기술 검색, 웹 검색 |
| gates (게이트) | 필수 지역·규모·제외 | 신뢰 게이트(후보 탈락) |
| checks (검증) | 공정·설비·인증 전부 | 상세 수집 후 LLM 스펙 체크 → 점수 |
| bonus (가점) | 우대 지역·규모 | 점수 가점 |

우대 공정은 발견 검색어에 넣지 않는다(후보를 좁히거나 흐리지 않게). 검증·가점에만 쓴다.

존 분리: 내부 품목코드(item)·금지어(현 협력사명 등)가 들어간 검색어는 여기서 결정적으로
제거하고, 제거 내역을 removed에 남긴다(데모 로그 '[필터] 내부 품목코드 포함 검색어 제외').
"""

from __future__ import annotations

# 웹 검색 로케일 ← 지역 국가코드. EN(영문 글로벌)은 해외 지역이 하나라도 있으면 쓴다.
_COUNTRY_LOCALE = {"KR": "KR", "VN": "VI", "CN": "ZH"}


def _dedup(xs):
    seen, out = set(), []
    for x in xs:
        k = x.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _items(spec: dict, group: str, *, required: bool | None = None) -> list[dict]:
    return [x for x in spec["items"]
            if x["group"] == group and (required is None or x["required"] is required)]


def _rule(it: dict, key: str):
    return (it.get("rule") or {}).get(key)


def build_search_plan(spec: dict, *, forbidden: list[str] | None = None) -> dict:
    """확정 스펙 → 검색 계획.

    forbidden: 검색어에서 제외할 금지어(현 협력사명 등). 내부 품목코드(spec['item'])는 자동 포함.
    """
    banned = [w for w in [spec.get("item"), *(forbidden or [])] if w]
    removed: list[str] = []

    def clean(terms: list[str]) -> list[str]:
        ok = []
        for t in _dedup(terms):
            if any(b.lower() in t.lower() for b in banned):
                removed.append(t)
            else:
                ok.append(t)
        return ok

    req_proc = _items(spec, "processes", required=True)

    # --- 지역: 필수 국가 ∪ 우대 국가 = 허용, 필수 국가 = 우선 ---
    req_c = _dedup(c for it in _items(spec, "region", required=True) for c in (_rule(it, "countries") or []))
    pref_c = _dedup(c for it in _items(spec, "region", required=False) for c in (_rule(it, "countries") or []))
    near = [{"near": _rule(it, "near"), "km": _rule(it, "km"), "required": it["required"]}
            for it in _items(spec, "region") if _rule(it, "near")]
    allowed = _dedup(req_c + pref_c)

    locales = _dedup(_COUNTRY_LOCALE[c] for c in allowed if c in _COUNTRY_LOCALE) or ["KR"]
    if any(c != "KR" for c in allowed):
        locales.append("EN")

    web = {loc: clean([q for it in req_proc for q in it["terms"]["web"].get(loc, [])])
           for loc in locales}

    # --- 규모·제외 ---
    def scale_rule(key: str, required: bool):
        vals = [_rule(it, key) for it in _items(spec, "scale", required=required) if _rule(it, key)]
        return max(vals) if vals else None

    excludes = {_rule(it, "exclude") for it in _items(spec, "exclude", required=True)}

    return {
        "request_id": spec.get("request_id"),
        "target_item": spec.get("target_item"),
        "discovery": {
            "factorion_products": clean(
                [t for it in req_proc for t in it["terms"]["product"]] + spec.get("item_terms", [])),
            "kipris": {
                "keywords": clean([t for it in req_proc for t in it["terms"]["patent"]]),
                "ipc": _dedup(t for it in req_proc for t in it["terms"]["ipc"]),
            },
            "web": {loc: qs for loc, qs in web.items() if qs},
        },
        "gates": {
            "countries": allowed,          # 비어 있으면 국가 제한 없음
            "min_employees": scale_rule("min_employees", True),
            "min_revenue_krw": scale_rule("min_revenue_krw", True),
            "exclude_traders": "trader" in excludes,
            "exclude_incumbent": "incumbent" in excludes,
        },
        "checks": [
            {**{k: it.get(k) for k in ("id", "group", "label", "required", "code", "min_evidence")},
             # 항목 동의어(국·영문 검색어) — 판정 인용에 항목 핵심어가 있는지 확인할 때 쓴다
             "terms": list(dict.fromkeys((it.get("terms") or {}).get("patent", [])
                                         + (it.get("terms") or {}).get("product", [])))}
            for it in spec["items"] if it["group"] in ("processes", "equipment", "certs")
        ],
        "bonus": {
            "priority_countries": req_c,   # 우선 국가(전점) — 그 외 허용 국가는 부분점
            "near": near,
            "min_employees": scale_rule("min_employees", False),
            "min_revenue_krw": scale_rule("min_revenue_krw", False),
        },
        "removed": removed,
    }
