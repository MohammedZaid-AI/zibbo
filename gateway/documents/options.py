"""Configuration for the document subsystem, decoupled from ``Settings``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gateway.documents.models import DocumentFormat

if TYPE_CHECKING:
    from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class DocumentOptions:
    enabled: bool = True

    max_decoded_bytes: int = 16_000_000
    """A decoded document larger than this is left as-is. Base64 inflates ~1.33x, so
    this pairs with the request body cap rather than duplicating it."""

    enabled_formats: frozenset[DocumentFormat] = field(
        default_factory=lambda: frozenset(DocumentFormat)
    )

    def permits(self, fmt: DocumentFormat) -> bool:
        return self.enabled and fmt in self.enabled_formats

    @classmethod
    def from_settings(cls, settings: Settings) -> DocumentOptions:
        formats = frozenset(
            fmt for fmt in DocumentFormat if fmt.value not in settings.documents_disabled_formats
        )
        return cls(
            enabled=settings.documents_enabled,
            max_decoded_bytes=settings.documents_max_decoded_bytes,
            enabled_formats=formats,
        )
