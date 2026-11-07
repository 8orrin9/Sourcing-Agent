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

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
