"""Extractor Registry — 추출기 등록·조회.

신규 사이트 편입 = 여기 한 줄 등록(또는 2계층이면 YAML 추가 후 자동 등록).
소스별로 등록하되, 한 소스에 여러 계층을 둬서 실패 시 더 비싼 계층으로 폴백할 수 있다.
"""

from __future__ import annotations

from .base import Extractor


class Registry:
    """source_id → 추출기들(계층 오름차순). get()은 가장 싼 계층부터 돌려준다."""

    def __init__(self) -> None:
        self._by_source: dict[str, list[Extractor]] = {}

    def register(self, extractor: Extractor) -> Extractor:
        bucket = self._by_source.setdefault(extractor.source_id, [])
        bucket.append(extractor)
        bucket.sort(key=lambda e: e.tier)  # 비용순: 싼 계층 우선
        return extractor

    def for_source(self, source_id: str) -> list[Extractor]:
        """해당 소스의 추출기를 계층 오름차순으로. 앞에서부터 시도→실패 시 다음(폴백)."""
        return list(self._by_source.get(source_id, []))

    def sources(self) -> list[str]:
        return list(self._by_source)

    def all(self) -> list[Extractor]:
        return [e for bucket in self._by_source.values() for e in bucket]


#: 프로세스 전역 레지스트리. 각 추출기 모듈이 import 시 여기에 등록한다.
registry = Registry()
