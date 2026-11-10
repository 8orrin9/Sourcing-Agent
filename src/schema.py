"""SupplierRecord — 모든 추출기·LLM 계층이 공유하는 단일 계약.

어떤 소스(앵커 API·확산 디렉터리·웹검색·LLM 폴백)에서 왔든 추출기는 이 스키마로
정규화해 출력한다. 다운스트림(엔티티 병합·스코어링·숏리스트)은 출처를 모른다.

존 분리: 이 스키마에는 외부 공개 정보만 담는다. 내부 단가·내부 품목코드·현 협력사명은
여기 들어오지 않는다(그 대조는 숏리스트 진입 후 내부망에서 수행).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class SourceClass(str, Enum):
    """소스 분류. 데모의 '앵커/확산' 개념을 그대로 계승.

    - ANCHOR: 검증 키(사업자번호·주소·출원인코드)를 확보해 엔티티 병합의 기준점이 되는 소스
              (팩토리온 공장등록, KIPRIS 특허 출원인).
    - SPREAD: 재현율을 넓히지만 회사명·URL 위주라 앵커에 흡착해 식별해야 하는 소스
              (다국어 웹검색, B2B 디렉터리, 전시회 참가사).
    """

    ANCHOR = "anchor"
    SPREAD = "spread"


class EvidenceLevel(str, Enum):
    """증거 수준. 데모 LV_DEF(L1/L2/L3)와 일치.

    - L1: 웹 언급만 있음.
    - L2: 설비 사양·사진 확보.
    - L3: 특허·인증·시험성적서·납품 실적으로 입증.
    """

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class CompanyKind(str, Enum):
    """업체 성격. 데모 TAG(trader 제외 등)의 판정 결과를 담는다."""

    MANUFACTURER = "manufacturer"  # 제조사 (공장 주소·생산품목 확인)
    TRADER = "trader"  # 유통사/대리점 (공장 주소 없음) → 제외 대상
    UNKNOWN = "unknown"  # 제조/유통 미확인


class Provenance(BaseModel):
    """출처 추적 — 어느 소스가, 어떤 추출기로, 무엇을 근거로 이 레코드를 만들었나.

    증거 우선 원칙: 모든 필드 값은 '어디서 왔는지' 추적 가능해야 한다.
    """

    source_id: str = Field(..., description="소스 식별자 (예: factorion, made_in_china, web_kr)")
    source_class: SourceClass
    extractor: str = Field(..., description="사용된 추출기 (structured|config|site_adapter|llm)")
    url: Optional[HttpUrl] = Field(None, description="수집 URL (API는 생략 가능)")
    verified: bool = Field(False, description="앵커 교차검증으로 식별이 확정됐는지")
    note: Optional[str] = Field(None, description="추출 시 메모 (예: '미검증 · 공공DB 폴백')")


class ProcessCapability(BaseModel):
    """공정 역량 한 건. 데모 PROC_DEF(HC 하드코팅, AF 내지문)에 대응.

    소싱 스펙의 공정 요구사항과 매칭하기 위한 단위.
    """

    code: str = Field(..., description="공정 코드 (예: HC, AF)")
    label: str = Field(..., description="공정명 (예: 하드코팅, 내지문 코팅)")
    evidence: EvidenceLevel = Field(EvidenceLevel.L1, description="이 역량의 입증 수준")
    basis: Optional[str] = Field(None, description="근거 (예: '하드코팅 조성물 특허 3건')")


class SpecCheck(BaseModel):
    """소싱 스펙 항목 1건에 대한 판정 (데모 STEP 2 '요구 조건 · 판정 · 증거 수준 · 근거').

    공정만이 아니라 설비·인증까지 스펙에 적힌 항목을 그대로 판정 단위로 쓴다
    → 품목이 바뀌어도(A 부품) 코드 수정 없이 스펙 항목이 곧 체크리스트가 된다.
    """

    item_id: str = Field(..., description="스펙 항목 id (P1, E1, C1 …)")
    group: str = Field(..., description="processes | equipment | certs")
    label: str
    required: bool = True
    verdict: str = Field("unknown", description="met | partial | unmet | unknown")
    evidence: Optional[EvidenceLevel] = None
    basis: Optional[str] = Field(None, description="근거 문장·출처")


class SupplierRecord(BaseModel):
    """공급사 후보 1건 — 추출기 출력의 정규화 단위.

    엔티티 병합 전에는 소스별로 부분 레코드가 여러 개 나올 수 있고(같은 회사의 웹/특허/공장
    조각), 병합 후에는 사업자번호·도메인·주소 일치로 하나로 합쳐진다.
    """

    # --- 식별 (엔티티 병합 키) ---
    name: str = Field(..., description="회사명 (원문)")
    name_normalized: Optional[str] = Field(None, description="정규화 회사명 (병합용·LLM 생성)")
    biz_reg_no: Optional[str] = Field(None, description="사업자등록번호 — 가장 강한 병합 키 (앵커 확보)")
    domain: Optional[str] = Field(None, description="대표 도메인 — 병합 보조 키")
    homepage: Optional[HttpUrl] = None

    # --- 소재지 ---
    country: Optional[str] = Field(None, description="국가 (KR·CN·VN 등)")
    region: Optional[str] = Field(None, description="지역 (예: 충북 청주, 베트남 박닌)")
    address: Optional[str] = Field(None, description="주소 — 앵커 병합 키")

    # --- 역량·성격 ---
    kind: CompanyKind = CompanyKind.UNKNOWN
    products: list[str] = Field(default_factory=list, description="생산품목 (공장등록·카탈로그)")
    capabilities: list[ProcessCapability] = Field(
        default_factory=list, description="공정 역량 (스펙 매칭 대상)"
    )
    has_factory: Optional[bool] = Field(None, description="자체 생산 공장 보유 여부 (제조사 판정)")
    employees: Optional[int] = Field(None, description="종업원 수 (공장등록 합산 또는 홈페이지) — 신뢰 게이트")
    since_year: Optional[int] = Field(None, description="최초 공장등록 연도 — 업력(신뢰 신호)")
    checks: list[SpecCheck] = Field(
        default_factory=list, description="소싱 스펙 항목별 판정 (공정·설비·인증) — 상세 수집 후 LLM"
    )

    # --- 연락 (개인정보 비저장: 부서·대표 연락처만) ---
    contact_dept: Optional[str] = Field(None, description="담당 부서 (개인 이름 저장 금지)")
    contact_public: Optional[str] = Field(None, description="대표 이메일/문의폼 (공개 정보만)")

    # --- 출처·증거 ---
    provenances: list[Provenance] = Field(
        default_factory=list, description="이 레코드를 구성한 모든 소스 (병합 시 누적)"
    )
    notes: list[str] = Field(default_factory=list, description="수집 메모 (예: '2023년 품질심사 탈락')")

    def merge_key_biz(self) -> Optional[str]:
        """사업자번호 병합 키 (숫자만)."""
        if not self.biz_reg_no:
            return None
        return "".join(ch for ch in self.biz_reg_no if ch.isdigit()) or None
