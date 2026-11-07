"""Extractor Registry — 비용순 추출기 계층.

모든 추출기는 동일한 SupplierRecord 스키마로 출력한다(단일 계약).
신규 사이트 편입 우선순위:
  1) structured   : JSON-LD/sitemap/OpenAPI (앵커 — 팩토리온·KIPRIS)
  2) config_driven: YAML 셀렉터 (확산 디렉터리 — 설정 한 줄)
  3) sites/*      : 코드 어댑터 (안티봇·복잡 JS 사이트만)
  4) llm_extract  : LLM 폴백 (설정·코드 없어도 커버)
"""
