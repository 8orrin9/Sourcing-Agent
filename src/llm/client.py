"""LLM 클라이언트 래퍼 — OpenAI 호출을 한 곳에 격리.

다운스트림(엔티티 병합·스코어링·회신 파싱)은 이 모듈의 함수에만 의존한다.
모델·프로바이더를 바꿔도 여기만 고치면 된다.

사내망 TLS 주의: pip-system-certs(httpx 직접 호출용)가 ssl.verify_mode를 몽키패치하면,
openai SDK의 truststore와 충돌해 RecursionError가 난다. 이를 피하려고 openai에는
명시적 httpx.Client(독립 SSL 컨텍스트)를 주입한다.
"""

from __future__ import annotations

import json
import os
import ssl
from functools import lru_cache
from typing import Any

import httpx
from openai import OpenAI

# 기본 모델 — 비용·속도 균형. 호출부에서 override 가능.
DEFAULT_MODEL = os.getenv("SOURCING_LLM_MODEL", "gpt-4o-mini")


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """OpenAI 클라이언트(프로세스 1회 생성). truststore 재귀 회피용 httpx 주입."""
    ctx = ssl.create_default_context()
    http_client = httpx.Client(verify=ctx, timeout=60.0)
    return OpenAI(http_client=http_client)


def complete_json(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str = "result",
    model: str | None = None,
    temperature: float = 0.0,
) -> dict:
    """JSON Schema를 강제해 구조화 출력을 받는다(structured output).

    schema 미준수 시 SDK가 거부하므로, 반환값은 schema를 만족하는 dict로 신뢰할 수 있다.
    """
    client = get_client()
    resp = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
    )
    return json.loads(resp.choices[0].message.content)
