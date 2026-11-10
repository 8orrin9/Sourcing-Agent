"""검색엔진 경유 근거 수집 — 홈페이지에 못 들어가도 근거를 모은다(상세 수집 사다리 ④·⑤).

홈페이지 직접 수집은 실행 환경의 네트워크 차단·봇 차단·낡은 도메인으로 자주 실패한다
(PMMA 라이브 실행에서 23곳 전부 실패). 검색엔진은 이미 그 페이지를 읽었으므로, SerpApi로
그 회사 도메인·회사명 + 공정·설비·인증 키워드를 검색해 제목·스니펫을 근거로 쓴다.

- 근거 표기: notes에 '[검색근거] 제목 — 스니펫 <URL>'. 판정 단계에서 출처 'search'로 인용되며
  증거 수준은 L1(웹 언급)로 상한이 걸린다(스니펫만으로 설비 사양 확보로 볼 수 없음).
- 회사명 검색은 동명이인 회사 결과가 섞이므로, 제목·스니펫에 회사명과 함께 공정·설비 단어가
  들어간 결과만 남긴다(자사 도메인 결과는 그대로).
- 검색어는 짧게 쓴다. 라이브 확인 결과 'site:도메인 A OR B OR …'처럼 OR을 길게 붙이면 구글이
  site: 조건을 무시하고 무관한 결과를 냈고, 'site:도메인' 단독·'회사명 키워드1개'가 정확했다.
- 결과 중 PDF(카탈로그·인증서 등)는 링크를 따로 돌려줘 내려받기를 시도하게 한다(⑤).

존 분리: 외부로 나가는 검색어는 회사명·도메인과 공정·설비·인증 일반어뿐이다.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..llm.merge import _norm_name, query_name

# 업체당 검색 예산(SerpApi 호출 수 = 비용)
MAX_QUERIES = 2
MAX_NOTES = 6
SNIPPET_CHARS = 220

# 타 사이트 결과를 근거로 인정할 때 회사명과 함께 있어야 하는 단어(공정어에 더해)
_FACILITY = ["설비", "장비", "클린룸", "인증", "ISO", "코팅", "필름", "시트", "생산", "제조",
             "equipment", "coating", "film", "sheet", "manufactur", "cleanroom", "certif"]
# 근거로 쓰지 않는 결과(특허 원문은 KIPRIS 경로가 따로 다룬다)
_SKIP_DOMAINS = ("kipris.or.kr", "patents.google.com", "freepatentsonline.com")


def build_queries(name: str, *, domain: str | None, keywords: list[str], locale: str = "KR") -> list[str]:
    """도메인이 있으면 'site:도메인'(그 사이트의 대표 페이지들), 다음으로 '회사명 공정어1개'."""
    qs = []
    if domain:
        qs.append(f"site:{domain}")
    kw = next((k for k in keywords if k), "")
    qs.append(f"{query_name(name)} {kw}".strip())
    return qs[:MAX_QUERIES]


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _relevant_mention(name: str, text: str, keywords: list[str]) -> bool:
    """회사명 + (공정어·설비 단어 중 하나)가 함께 나오는지 — 동명이인 회사 결과 배제."""
    t, want = _squash(text), _norm_name(name)
    if not want or want not in t:
        return False
    rest = t.replace(want, " ")  # 회사명 자체의 단어('누리코팅'의 '코팅')는 근거로 치지 않는다
    return any(_squash(w) in rest for w in [*keywords, *_FACILITY] if w)


def to_notes(name: str, results: list[dict], *, domain: str | None,
             keywords: list[str] | None = None) -> tuple[list[str], list[str]]:
    """검색결과 → (근거 notes, PDF 링크). 자사 도메인 결과는 그대로, 그 외는 회사명+공정어 언급 시만."""
    notes, pdfs, seen = [], [], set()
    for r in results:
        link = r.get("link") or ""
        host = urlparse(link).netloc.lower().replace("www.", "")
        if not link or link in seen or any(host.endswith(d) for d in _SKIP_DOMAINS):
            continue
        own = bool(domain) and host.endswith(domain)
        title, snip = r.get("title") or "", (r.get("snippet") or "")[:SNIPPET_CHARS]
        if not own and not _relevant_mention(name, f"{title} {snip}", keywords or []):
            continue  # 다른 회사·동명이인 결과 섞임 방지
        seen.add(link)
        if urlparse(link).path.lower().endswith(".pdf"):
            pdfs.append(link)
        notes.append(f"[검색근거] {title} — {snip} <{link}>")
    return notes[:MAX_NOTES], pdfs


def gather(name: str, web, *, domain: str | None, keywords: list[str],
           locale: str = "KR", num: int = 8) -> dict:
    """검색으로 근거 수집 → {'notes': [...], 'pdfs': [...], 'queries': [...], 'errors': [...]}.

    web: WebSearchExtractor(또는 같은 search(q, locale, num) 인터페이스).
    """
    out = {"notes": [], "pdfs": [], "queries": [], "errors": []}
    for q in build_queries(name, domain=domain, keywords=keywords, locale=locale):
        out["queries"].append(q)
        try:
            res = web.search(q, locale=locale, num=num)
        except Exception as e:  # 검색 한 건 실패가 전체를 막지 않게
            out["errors"].append(f"{q}: {e}")
            continue
        notes, pdfs = to_notes(name, res, domain=domain, keywords=keywords)
        out["notes"] += [n for n in notes if n not in out["notes"]]
        out["pdfs"] += [p for p in pdfs if p not in out["pdfs"]]
    out["notes"] = out["notes"][:MAX_NOTES]
    return out
