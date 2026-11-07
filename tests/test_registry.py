"""Registry 동작 검증 — 등록·계층 정렬·폴백 순서."""

from __future__ import annotations

from src.extractors.base import Extractor
from src.extractors.registry import Registry
from src.schema import SourceClass, SupplierRecord


class _Dummy(Extractor):
    def __init__(self, tier: int, source_id: str, cls: SourceClass):
        self.tier = tier
        self.source_id = source_id
        self.source_class = cls

    def extract(self, query: dict) -> list[SupplierRecord]:
        return [SupplierRecord(name=f"{self.source_id}-t{self.tier}")]


def test_register_sorts_by_tier():
    reg = Registry()
    reg.register(_Dummy(4, "site_x", SourceClass.SPREAD))  # LLM 폴백 먼저 등록
    reg.register(_Dummy(2, "site_x", SourceClass.SPREAD))  # config는 나중에
    tiers = [e.tier for e in reg.for_source("site_x")]
    assert tiers == [2, 4]  # 조회는 비용순(싼 계층 먼저)


def test_sources_and_all():
    reg = Registry()
    reg.register(_Dummy(1, "factorion", SourceClass.ANCHOR))
    reg.register(_Dummy(2, "ec21", SourceClass.SPREAD))
    assert set(reg.sources()) == {"factorion", "ec21"}
    assert len(reg.all()) == 2


def test_unknown_source_returns_empty():
    assert Registry().for_source("nope") == []
