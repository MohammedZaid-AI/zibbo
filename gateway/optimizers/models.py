"""The vocabulary of the transformation pipeline.

Transformers return only ``TransformOutput`` — the rewritten text plus the names
of the steps they applied. Every measurement (bytes, tokens, timing) is computed
*by the pipeline*, uniformly, from that output.

That is what makes the dashboard requirement hold. A transformer cannot forget to
report tokens saved, cannot measure them differently from its neighbour, and gains
nothing to change when a new metric is added — the metric is derived here, once,
for all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ContentType(StrEnum):
    """What a payload actually is, as opposed to what it claims to be."""

    HTML = "html"
    JSON = "json"
    XML = "xml"
    CSV = "csv"
    TEXT = "text"
    PDF = "pdf"
    DOCX = "docx"
    IMAGE = "image"
    BINARY = "binary"
    UNKNOWN = "unknown"


class SkipReason(StrEnum):
    """Why a request was forwarded untouched. Recorded, never inferred."""

    DISABLED = "optimization_disabled"
    METHOD_NOT_ELIGIBLE = "method_not_eligible"
    ENDPOINT_NOT_ELIGIBLE = "endpoint_not_eligible"
    CONTENT_TYPE_NOT_ELIGIBLE = "content_type_not_eligible"
    BODY_TOO_LARGE = "body_too_large"
    EMPTY_BODY = "empty_body"
    MALFORMED_PAYLOAD = "malformed_payload"
    NO_ADAPTER = "no_payload_adapter"
    NO_SEGMENTS = "no_optimizable_segments"
    NOT_MODIFIED = "content_already_optimal"


@dataclass(frozen=True, slots=True)
class TransformationRequest:
    """What the pipeline is asked to consider. Provider-agnostic on purpose."""

    method: str
    path: str
    """Upstream-relative, e.g. ``chat/completions``."""
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class Detection:
    """The detector's verdict about one piece of content."""

    content_type: ContentType
    confidence: float
    source: str
    parsed: Any = None
    """Reuse of the sniffer's parse. Prevents the JSON transformer parsing twice."""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransformOutput:
    """All a transformer returns: the new text, and what it did."""

    content: str
    steps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransformationResult:
    """One transformer's effect on one segment, fully measured."""

    transformation_name: str
    detected_content_type: ContentType
    transformed_content: str
    original_size_bytes: int
    transformed_size_bytes: int
    original_token_count: int
    transformed_token_count: int
    execution_time_ms: float
    transformations_applied: tuple[str, ...]
    origin: str
    """Where in the payload this came from, e.g. ``messages[2].content``."""

    cache_hit: bool = False
    """Whether this result was served from the transformation cache rather than
    computed. When true, ``execution_time_ms`` is the warm-lookup cost, not the cold
    transformation cost the entry originally recorded."""

    @property
    def tokens_saved(self) -> int:
        return self.original_token_count - self.transformed_token_count

    @property
    def bytes_saved(self) -> int:
        return self.original_size_bytes - self.transformed_size_bytes

    @property
    def token_reduction_pct(self) -> float:
        return _pct(self.tokens_saved, self.original_token_count)

    @property
    def byte_reduction_pct(self) -> float:
        return _pct(self.bytes_saved, self.original_size_bytes)

    @property
    def changed(self) -> bool:
        return bool(self.transformations_applied)


@dataclass(frozen=True, slots=True)
class TransformationReport:
    """The pipeline's answer: a body to forward, and everything we learned."""

    body: bytes
    applied: bool
    execution_time_ms: float
    original_size_bytes: int
    transformed_size_bytes: int
    skip_reason: SkipReason | None = None
    results: tuple[TransformationResult, ...] = ()

    @classmethod
    def skipped(
        cls, body: bytes, reason: SkipReason, execution_time_ms: float = 0.0
    ) -> TransformationReport:
        size = len(body)
        return cls(
            body=body,
            applied=False,
            execution_time_ms=execution_time_ms,
            original_size_bytes=size,
            transformed_size_bytes=size,
            skip_reason=reason,
        )

    @property
    def original_token_count(self) -> int:
        return sum(result.original_token_count for result in self.results)

    @property
    def transformed_token_count(self) -> int:
        return sum(result.transformed_token_count for result in self.results)

    @property
    def tokens_saved(self) -> int:
        return self.original_token_count - self.transformed_token_count

    @property
    def bytes_saved(self) -> int:
        return self.original_size_bytes - self.transformed_size_bytes

    @property
    def token_reduction_pct(self) -> float:
        return _pct(self.tokens_saved, self.original_token_count)

    @property
    def byte_reduction_pct(self) -> float:
        return _pct(self.bytes_saved, self.original_size_bytes)

    @property
    def transformers_used(self) -> tuple[str, ...]:
        seen = {result.transformation_name for result in self.results if result.changed}
        return tuple(sorted(seen))

    @property
    def content_types_detected(self) -> tuple[str, ...]:
        seen = {result.detected_content_type.value for result in self.results}
        return tuple(sorted(seen))

    @property
    def cache_hits(self) -> int:
        return sum(1 for result in self.results if result.cache_hit)

    @property
    def cache_status(self) -> str | None:
        """``hit`` when every result came from cache, ``miss`` when none did,
        ``partial`` for a mix, ``None`` when there were no results to cache."""
        if not self.results:
            return None
        hits = self.cache_hits
        if hits == 0:
            return "miss"
        if hits == len(self.results):
            return "hit"
        return "partial"


def _pct(saved: int, original: int) -> float:
    if original <= 0:
        return 0.0
    return round(saved / original * 100, 2)
