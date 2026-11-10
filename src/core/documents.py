"""문서 텍스트 추출 — 사양서(STEP 0)·업체 카탈로그/인증서(STEP 2) 공용.

PDF는 pypdf로 텍스트 레이어를 읽는다. 스캔 이미지 PDF(텍스트 없음)는 빈 문자열이 되며,
OCR은 아직 범위 밖이다(설계서 '멀티모달' 항목).
"""

from __future__ import annotations

import io
from pathlib import Path


def read_document(data: bytes | str | Path, *, filename: str | None = None) -> str:
    """파일 경로 또는 원본 바이트 → 텍스트. PDF가 아니면 UTF-8 텍스트로 본다."""
    if isinstance(data, (str, Path)):
        path = Path(data)
        filename = filename or path.name
        data = path.read_bytes()
    if (filename or "").lower().endswith(".pdf") or data[:5] == b"%PDF-":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return data.decode("utf-8", errors="replace").strip()
