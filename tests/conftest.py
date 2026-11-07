"""골든 픽스처 회귀 테스트 공통 설정.

픽스처 = 저장된 실HTML/실응답(data/fixtures/<source>/input.*) + 기대 정규화 결과
(expected.json). 사이트 구조가 바뀌어 추출기가 깨지면 이 테스트가 잡아낸다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "data" / "fixtures"


def load_fixture(source: str, name: str) -> str:
    """data/fixtures/<source>/<name> 원문(HTML/JSON 등)을 문자열로 로드."""
    return (FIXTURES_DIR / source / name).read_text(encoding="utf-8")


def load_expected(source: str) -> list[dict]:
    """해당 소스의 기대 정규화 결과(expected.json)를 로드."""
    return json.loads((FIXTURES_DIR / source / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
