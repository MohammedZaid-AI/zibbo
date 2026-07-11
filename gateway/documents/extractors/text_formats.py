"""Extractors for the textual document formats: CSV, XML, HTML, Markdown, plain text.

Each decodes the bytes to text and hands off to a shared converter, so an uploaded
``.csv`` file and a CSV pasted into a prompt become the same Markdown. HTML reuses
the Phase 3 transformer; Markdown and plain text reuse the text normalizer. None of
this duplicates logic — the extractors are thin adapters from *bytes* onto the
converters that already exist.
"""

from __future__ import annotations

from typing import ClassVar

from gateway.documents.base import DocumentExtractor
from gateway.documents.convert import csv_to_markdown, xml_to_markdown
from gateway.documents.models import DocumentFormat
from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import HtmlOptions, TextOptions
from gateway.optimizers.transformers import HtmlTransformer, normalize_text


def _decode(data: bytes) -> str | None:
    """Decode bytes to text, honouring a UTF-8/UTF-16 BOM, else UTF-8 lenient."""
    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                break
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


class CsvExtractor(DocumentExtractor):
    name: ClassVar[str] = "csv"
    formats: ClassVar[frozenset[DocumentFormat]] = frozenset({DocumentFormat.CSV})

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        text = _decode(data)
        return csv_to_markdown(text) if text else None


class XmlExtractor(DocumentExtractor):
    name: ClassVar[str] = "xml"
    formats: ClassVar[frozenset[DocumentFormat]] = frozenset({DocumentFormat.XML})

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        text = _decode(data)
        return xml_to_markdown(text) if text else None


class HtmlExtractor(DocumentExtractor):
    name: ClassVar[str] = "html"
    formats: ClassVar[frozenset[DocumentFormat]] = frozenset({DocumentFormat.HTML})

    _transformer: ClassVar[HtmlTransformer] = HtmlTransformer(HtmlOptions())
    _detection: ClassVar[Detection] = Detection(ContentType.HTML, 1.0, "document")

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        text = _decode(data)
        if not text:
            return None
        return self._transformer.transform(text, self._detection).content or None


class MarkdownExtractor(DocumentExtractor):
    """Markdown and plain text: normalize only, never rewrite."""

    name: ClassVar[str] = "markdown"
    formats: ClassVar[frozenset[DocumentFormat]] = frozenset(
        {DocumentFormat.MARKDOWN, DocumentFormat.TEXT}
    )

    _options: ClassVar[TextOptions] = TextOptions()

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        text = _decode(data)
        if not text:
            return None
        normalized, _ = normalize_text(text, self._options)
        return normalized or None
