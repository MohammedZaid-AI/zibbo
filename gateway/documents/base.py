"""The DocumentExtractor interface.

One extractor per format. Each is independent: it declares which formats it
handles, and it must never raise — a parser blowing up on a malformed file is
expected, and the extractor catches it and returns ``None``. That is the contract
that lets the safety promise ("a document we cannot read is forwarded unchanged")
hold without the pipeline knowing anything about PDFs.

Extractors that need a heavy dependency (pdfplumber, python-docx) import it lazily,
inside ``extract``, and report the dependency's absence as "cannot extract" rather
than crashing the gateway at startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from gateway.documents.models import DocumentFormat, ExtractionResult
from gateway.logging import get_logger

logger = get_logger(__name__)


class DocumentExtractor(ABC):
    """Turns the bytes of one document format into Markdown."""

    name: ClassVar[str]
    formats: ClassVar[frozenset[DocumentFormat]]

    version: ClassVar[str] = "1"
    """Bumped when extraction output for the same bytes changes. Part of the document
    service's cache fingerprint, so incrementing it invalidates that format's cache."""

    def can_extract(self, fmt: DocumentFormat) -> bool:
        return fmt in self.formats

    @abstractmethod
    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        """Return Markdown, or ``None`` if this document cannot be read.

        May assume ``data`` is non-empty and ``fmt`` is one this extractor claims.
        Must not raise on malformed input — catch and return ``None``.
        """

    def extract(self, data: bytes, fmt: DocumentFormat) -> ExtractionResult:
        """Safe entry point. Never raises, whatever the extractor does inside."""
        try:
            markdown = self._extract(data, fmt)
        except Exception as exc:  # noqa: BLE001 — a broken parser must not break a request
            logger.warning(
                "document_extraction_failed",
                extractor=self.name,
                format=fmt.value,
                cause=f"{type(exc).__name__}: {exc}",
            )
            return ExtractionResult(fmt, None, detail=type(exc).__name__, original_bytes=len(data))

        return ExtractionResult(
            format=fmt,
            markdown=markdown,
            detail=None if markdown else "no_text_extracted",
            original_bytes=len(data),
            extracted_chars=len(markdown or ""),
        )
