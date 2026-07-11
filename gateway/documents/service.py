"""The one entry point the pipeline uses: bytes in, Markdown out (or nothing).

``DocumentService.extract`` detects the format, checks it is permitted and within the
size limit, dispatches to the right extractor, and returns an :class:`ExtractionResult`.
It never raises and never modifies its input. Everything downstream — deciding whether
the Markdown is worth substituting, counting the tokens saved — is the pipeline's job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.documents.detection import detect_format
from gateway.documents.models import DocumentFormat, ExtractionResult
from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.documents.options import DocumentOptions
    from gateway.documents.registry import DocumentExtractorRegistry

logger = get_logger(__name__)


class DocumentService:
    def __init__(self, registry: DocumentExtractorRegistry, options: DocumentOptions) -> None:
        self._registry = registry
        self._options = options

    @property
    def enabled(self) -> bool:
        return self._options.enabled

    @property
    def version(self) -> str:
        """Cache fingerprint: changes when any extractor's version does."""
        return self._registry.fingerprint

    def extract(
        self, data: bytes, *, media_type: str | None = None, filename: str | None = None
    ) -> ExtractionResult:
        """Best-effort extraction. Always returns a result; ``markdown`` may be ``None``."""
        if not self._options.enabled or not data:
            return ExtractionResult(DocumentFormat.UNKNOWN, None, detail="disabled_or_empty")

        if len(data) > self._options.max_decoded_bytes:
            return ExtractionResult(
                DocumentFormat.UNKNOWN, None, detail="over_size_limit", original_bytes=len(data)
            )

        fmt = detect_format(data, media_type=media_type, filename=filename)
        if fmt is DocumentFormat.UNKNOWN or not self._options.permits(fmt):
            return ExtractionResult(
                fmt, None, detail="unsupported_format", original_bytes=len(data)
            )

        extractor = self._registry.for_format(fmt)
        if extractor is None:
            return ExtractionResult(fmt, None, detail="no_extractor", original_bytes=len(data))

        return extractor.extract(data, fmt)
