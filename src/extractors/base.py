"""추출기 공통 계약 (ABC).

모든 추출기(구조화·설정·코드·LLM)는 Extractor를 상속하고, 입력을 받아
SupplierRecord 리스트로 정규화해 돌려준다. 다운스트림은 어느 계층이 만들었는지 모른다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schema import SourceClass, SupplierRecord


class Extractor(ABC):
    """추출기 베이스. 하위 클래스는 tier·source_id·source_class를 선언하고 extract를 구현."""

    #: 비용순 계층 (1=structured, 2=config, 3=site_adapter, 4=llm). 레지스트리 선택 우선순위.
    tier: int
    #: 소스 식별자 (예: factorion, made_in_china). Provenance.source_id와 일치.
    source_id: str
    #: 앵커/확산 분류.
    source_class: SourceClass

    @abstractmethod
    def extract(self, query: dict) -> list[SupplierRecord]:
        """query(검색 조건·URL·원문 등)를 받아 정규화된 공급사 레코드 리스트를 반환.

        query 스키마는 계층마다 다르다(API 파라미터 / URL / HTML 원문). 레지스트리가
        적절한 추출기에 라우팅하므로, 각 추출기는 자기가 이해하는 키만 읽는다.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 디버그 편의
        return f"<{type(self).__name__} tier={self.tier} source={self.source_id}>"
