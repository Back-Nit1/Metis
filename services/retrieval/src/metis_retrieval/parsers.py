"""Text extraction for corpus files. Malformed input raises a permanent error.

* text / Markdown / code / CSV: UTF-8, then latin-1; a NUL byte in the first
  8 KiB means binary and is rejected as unsupported
* PDF: the pypdf text layer only. No text layer -> ``UNSUPPORTED_MEDIA``
  (honest: not OCR'd). Corrupt or encrypted -> ``VALIDATION_FAILED``.

Unsupported media is skipped with a visible reason. Only malformed input
(``VALIDATION_FAILED``) dead-letters.
"""

from __future__ import annotations

from pathlib import Path

from metis_contracts import ErrorCode, MetisError

SNIFF_BYTES = 8192


def parse_file(path: str | Path) -> str:
    """Return the extracted text of one file."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return _parse_pdf(p)
    return parse_text(p.read_bytes(), name=p.name)


def is_indexable(path: Path) -> bool:
    """False for binaries (images, databases, archives) we cannot read yet."""
    if path.suffix.lower() == ".pdf":
        return True
    try:
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(SNIFF_BYTES)
    except OSError:
        return True  # let ingestion report the real error


def parse_text(data: bytes, *, name: str = "file") -> str:
    if b"\x00" in data[:SNIFF_BYTES]:
        raise MetisError(ErrorCode.UNSUPPORTED_MEDIA, f"{name} looks binary; only text and PDF are indexed")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _parse_pdf(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise MetisError(ErrorCode.VALIDATION_FAILED, f"{path.name} is an encrypted PDF")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except MetisError:
        raise
    except Exception as exc:
        raise MetisError(
            ErrorCode.VALIDATION_FAILED, f"{path.name} is not a readable PDF: {exc}"[:500]
        ) from exc
    if not text.strip():
        raise MetisError(
            ErrorCode.UNSUPPORTED_MEDIA, f"{path.name} has no text layer (scanned PDFs are not OCR'd)"
        )
    return text
