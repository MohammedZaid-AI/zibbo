"""Deterministic document extraction.

Turns uploaded documents — PDF, DOCX, CSV, XML, HTML, Markdown, plain text — into
clean Markdown before they reach the provider. No model, no summarization, no
rewriting: only extraction and structural cleanup, byte-for-byte reproducible.

The gateway core knows none of this. The pipeline calls one method,
``DocumentService.extract``, and every format lives in its own isolated extractor
module. Adding PPTX, XLSX, EPUB, RTF or EML is one extractor plus one registration.

**Safety is the first property.** An extractor never raises; an encrypted PDF, a
corrupt DOCX, or an unsupported format returns "nothing extracted", and the pipeline
forwards the original document untouched. A document is never corrupted, only ever
replaced by a strictly cheaper representation or left exactly as it arrived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.documents.detection import detect_format
from gateway.documents.extractors import (
    CsvExtractor,
    DocxExtractor,
    HtmlExtractor,
    MarkdownExtractor,
    PdfExtractor,
    XmlExtractor,
)
from gateway.documents.models import DocumentFormat, ExtractionResult
from gateway.documents.options import DocumentOptions
from gateway.documents.registry import DocumentExtractorRegistry
from gateway.documents.service import DocumentService

if TYPE_CHECKING:
    from gateway.config import Settings

__all__ = [
    "DocumentExtractorRegistry",
    "DocumentFormat",
    "DocumentOptions",
    "DocumentService",
    "ExtractionResult",
    "build_document_registry",
    "build_document_service",
    "detect_format",
]


def build_document_registry() -> DocumentExtractorRegistry:
    """The one place extractors are named. Phase-future formats append here."""
    registry = DocumentExtractorRegistry()
    registry.register(PdfExtractor())
    registry.register(DocxExtractor())
    registry.register(CsvExtractor())
    registry.register(XmlExtractor())
    registry.register(HtmlExtractor())
    registry.register(MarkdownExtractor())
    return registry


def build_document_service(settings: Settings) -> DocumentService:
    return DocumentService(build_document_registry(), DocumentOptions.from_settings(settings))
