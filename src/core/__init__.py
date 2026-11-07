"""Core Engine — 사이트 무관 공통 수집 계층.

fetch · rate-limit · retry · robots · 캐시 · (필요 사이트만) JS 렌더.
사이트별 차이는 여기 두지 않는다. 추출 로직은 extractors/ 레지스트리가 담당.
"""
