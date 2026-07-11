"""The vocabulary of document extraction.

A document extractor turns *bytes* into Markdown. That is the one thing that
separates this subsystem from the text transformers of Phase 3: those take a
``str`` pasted into a prompt, these take a decoded file. Both end at the same
place — clean Markdown that costs far fewer tokens than what came in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DocumentFormat(StrEnum):
    """A format the extraction subsystem can recognize.

    Distinct from ``ContentType`` (which classifies text segments) because these are
    identified from *bytes* — magic numbers, ZIP structure — not from text sniffing.
    """

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    CSV = "csv"
    XML = "xml"
    HTML = "html"
    MARKDOWN = "markdown"
    TEXT = "text"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The outcome of trying to extract one document.

    ``markdown`` is ``None`` when nothing usable came out — an unsupported format, a
    parser that failed, an encrypted PDF, a corrupt DOCX. The caller must treat that
    as "leave the original alone", never as an error: a document we cannot read is
    forwarded verbatim, and the request is not touched.
    """

    format: DocumentFormat
    markdown: str | None
    detail: str | None = None
    """Why extraction produced nothing, for logs. Never contains document content."""

    original_bytes: int = 0
    extracted_chars: int = 0

    @property
    def extracted(self) -> bool:
        return self.markdown is not None and bool(self.markdown.strip())
