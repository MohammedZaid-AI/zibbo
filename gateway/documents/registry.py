"""Format -> extractor lookup."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.documents.base import DocumentExtractor
    from gateway.documents.models import DocumentFormat

logger = get_logger(__name__)


class DocumentExtractorRegistry:
    """Holds one extractor per format. First registration wins."""

    def __init__(self) -> None:
        self._by_format: dict[DocumentFormat, DocumentExtractor] = {}

    def register(self, extractor: DocumentExtractor) -> None:
        for fmt in extractor.formats:
            self._by_format.setdefault(fmt, extractor)
        logger.debug(
            "document_extractor_registered",
            extractor=extractor.name,
            formats=sorted(f.value for f in extractor.formats),
        )

    def for_format(self, fmt: DocumentFormat) -> DocumentExtractor | None:
        return self._by_format.get(fmt)

    @property
    def formats(self) -> tuple[str, ...]:
        return tuple(sorted(fmt.value for fmt in self._by_format))

    @property
    def fingerprint(self) -> str:
        """A stable digest of every registered extractor's name and version.

        The transformation cache keys documents on this, so version-bumping any
        extractor invalidates cached extractions across formats. That over-reaches
        slightly — a PDF bump also retires DOCX entries — but extractor bumps are rare
        and correctness is worth more than the extra cold runs it costs."""
        extractors = {id(e): e for e in self._by_format.values()}.values()
        material = ";".join(
            f"{extractor.name}@{extractor.version}"
            for extractor in sorted(extractors, key=lambda item: item.name)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
