"""Analytics value types — all metadata, never content.

Every field here is a count, a size, a duration, or a name. There is deliberately no
field that could hold a byte of a user's prompt: the analytics engine records *that* a
request was optimized and by how much, never *what* was in it. That constraint is what
lets ``/zibbo logs`` and the stats endpoints be safe to show and safe to log.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OptimizationEvent:
    """One request's optimization outcome, reduced to metadata.

    Produced from a ``TransformationReport`` plus request context. This is the unit the
    engine aggregates and the shape ``/zibbo logs`` replays.
    """

    timestamp: float
    provider: str
    endpoint: str
    """Upstream-relative path, e.g. ``chat/completions``. Not the body."""

    applied: bool
    skip_reason: str | None
    content_types: tuple[str, ...]
    transformers: tuple[str, ...]
    tokens_before: int
    tokens_after: int
    bytes_before: int
    bytes_after: int
    cache_hits: int
    cache_lookups: int
    execution_time_ms: float
    steps: tuple[str, ...] = ()
    """The individual transformation steps applied, e.g. ``removed_scripts``,
    ``converted_to_markdown`` — the detail behind ``zibbo explain``. Metadata, never
    content. Defaulted so older call sites and tests stay valid."""

    auth_method: str | None = None
    """The *kind* of credential observed on this request (``api_key``, ``oauth_token``,
    …), classified from the auth header name only — never the value. This is how the
    gateway *observes* authentication and routing as reality rather than intent. ``None``
    when no credential header was present. Defaulted so older call sites stay valid."""

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after

    @property
    def cache_status(self) -> str | None:
        """Display label: ``hit`` (all served from cache), ``miss`` (none),
        ``partial`` (a mix), or ``None`` when nothing was cacheable."""
        if self.cache_lookups == 0:
            return None
        if self.cache_hits == 0:
            return "miss"
        if self.cache_hits == self.cache_lookups:
            return "hit"
        return "partial"


@dataclass(frozen=True, slots=True)
class TransformerTally:
    """How much one transformer did over a window."""

    name: str
    count: int
    tokens_saved: int


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Aggregates over one window — either ``today`` or ``all-time``."""

    requests: int = 0
    optimized: int = 0
    skipped: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    transformations: int = 0
    latency_ms_sum: float = 0.0
    latency_samples: int = 0
    per_transformer: dict[str, TransformerTally] = field(default_factory=dict)

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def token_reduction_pct(self) -> float:
        return round(self.tokens_saved / self.tokens_before * 100, 2) if self.tokens_before else 0.0

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after

    @property
    def cache_hit_rate(self) -> float:
        lookups = self.cache_hits + self.cache_misses
        return round(self.cache_hits / lookups, 4) if lookups else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.latency_ms_sum / self.latency_samples, 3) if self.latency_samples else 0.0

    @property
    def top_transformer(self) -> TransformerTally | None:
        if not self.per_transformer:
            return None
        return max(self.per_transformer.values(), key=lambda tally: tally.tokens_saved)
