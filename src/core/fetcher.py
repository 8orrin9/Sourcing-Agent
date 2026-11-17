"""Fetcher — 사이트 무관 공통 수집 계층.

fetch · rate-limit · retry · robots.txt · 디스크 캐시. 사이트별 차이는 담지 않는다.

캐시: 같은 URL 재요청은 디스크(data/cache)에서 돌려줘 시연 안정성·반복 개발 속도를 높인다.
      골든 픽스처(저장된 실HTML)도 같은 캐시 경로로 주입할 수 있다.
TLS:  사내망이 자체 서명 CA로 HTTPS를 가로채는 환경을 위해, CA 번들/검증 여부를
      환경변수로 받는다(SOURCING_CA_BUNDLE · SOURCING_SSL_VERIFY).
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.robotparser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
DEFAULT_UA = "SourcingAgent/0.1 (+PoC; respects robots.txt)"


def _ssl_verify() -> bool | str:
    """TLS 검증 설정. 사내 CA 번들 경로가 있으면 그걸, 없으면 bool."""
    bundle = os.getenv("SOURCING_CA_BUNDLE")
    if bundle:
        return bundle
    return os.getenv("SOURCING_SSL_VERIFY", "true").lower() != "false"


class Fetcher:
    """공통 HTTP 수집기. 추출기들은 이 한 객체를 공유해 캐시·레이트리밋을 함께 쓴다."""

    def __init__(
        self,
        *,
        rate_limit_sec: float = 1.0,
        timeout: float = 20.0,
        user_agent: str = DEFAULT_UA,
        use_cache: bool = True,
        respect_robots: bool = True,
    ):
        self.rate_limit_sec = rate_limit_sec
        self.user_agent = user_agent
        self.use_cache = use_cache
        self.respect_robots = respect_robots
        self._last_req_at = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            verify=_ssl_verify(),
        )
        if use_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --- 캐시 ---
    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return CACHE_DIR / f"{key}.html"

    def _cache_get(self, url: str) -> Optional[str]:
        if not self.use_cache:
            return None
        p = self._cache_path(url)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def _cache_put(self, url: str, body: str) -> None:
        if self.use_cache:
            self._cache_path(url).write_text(body, encoding="utf-8")

    # --- robots.txt ---
    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(origin)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{origin}/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None  # robots 못 읽으면 보수적으로 허용
            self._robots[origin] = rp
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # --- rate limit ---
    def _throttle(self) -> None:
        wait = self.rate_limit_sec - (time.monotonic() - self._last_req_at)
        if wait > 0:
            time.sleep(wait)
        self._last_req_at = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        self._throttle()
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp

    def get(self, url: str, *, params: Optional[dict] = None, bypass_cache: bool = False) -> str:
        """URL 본문을 문자열로 반환. 캐시 우선, robots 준수, 실패 시 재시도.

        params가 있으면 캐시 키는 최종 요청 URL 기준으로 잡힌다.
        """
        req = self._client.build_request("GET", url, params=params)
        full_url = str(req.url)

        if not bypass_cache:
            cached = self._cache_get(full_url)
            if cached is not None:
                return cached

        if not self._allowed(full_url):
            raise PermissionError(f"robots.txt 차단: {full_url}")

        resp = self._get(url, params=params)
        body = resp.text
        self._cache_put(full_url, body)
        return body

    def get_bytes(self, url: str, *, max_bytes: int = 10_000_000) -> bytes:
        """PDF 등 바이너리 다운로드. 캐시(.bin)·robots·재시도는 get()과 같고, 크기 상한을 둔다."""
        p = self._cache_path(url).with_suffix(".bin")
        if self.use_cache and p.exists():
            return p.read_bytes()
        if not self._allowed(url):
            raise PermissionError(f"robots.txt 차단: {url}")
        data = self._get(url).content
        if len(data) > max_bytes:
            raise ValueError(f"파일이 너무 큼({len(data)} bytes): {url}")
        if self.use_cache:
            p.write_bytes(data)
        return data

    def render(self, url: str, *, timeout_ms: int = 20000) -> str:
        """JS 렌더링 후 HTML(Playwright). 직접 요청이 봇 차단·빈 본문일 때만 쓰는 비싼 경로.

        playwright 미설치면 ImportError. 캐시·robots는 get()과 같은 규칙을 따른다.
        """
        cached = self._cache_get(url + "#rendered")
        if cached is not None:
            return cached
        if not self._allowed(url):
            raise PermissionError(f"robots.txt 차단: {url}")
        from playwright.sync_api import sync_playwright  # 선택 의존성 — 필요할 때만 import

        self._throttle()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(user_agent=self.user_agent)
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                html = page.content()
            finally:
                browser.close()
        self._cache_put(url + "#rendered", html)
        return html

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def classify_error(e: Exception) -> str:
    """수집 실패를 원인별로 분류 — 환경(차단)·데이터(도메인)·사이트 문제를 로그에서 구분하려고.

    blocked: 실행 환경 프록시가 거부(사내망·클라우드 네트워크 정책) / http_403: 사이트의 봇 차단
    dns: 도메인 없음(낡은 홈페이지 주소 등) / timeout / robots / http_4xx·http_5xx / error
    """
    if isinstance(e, PermissionError):
        return "robots"
    if isinstance(e, httpx.ProxyError):
        return "blocked"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return "http_403" if code == 403 else f"http_{code // 100}xx"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    msg = str(e)
    if "Name or service not known" in msg or "getaddrinfo" in msg or "nodename nor servname" in msg:
        return "dns"
    if isinstance(e, httpx.ConnectError):
        return "connect"
    return "error"


FAIL_LABEL = {
    "blocked": "실행 환경 네트워크 차단", "http_403": "사이트 봇 차단(403)", "dns": "도메인 없음(DNS)",
    "timeout": "응답 시간 초과", "robots": "robots.txt 차단", "connect": "연결 실패",
    "http_4xx": "페이지 없음(4xx)", "http_5xx": "서버 오류(5xx)", "error": "기타 오류",
}
