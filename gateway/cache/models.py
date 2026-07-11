"""The cache's vocabulary: keys, the cached value, and running statistics.

A cache key is derived from *everything that determines the transformation output* —
the content itself, the transformers that could act on it, the gateway version, the
active options, and the tokenizer encoding (because token counts are cached too).
Change any one and the digest changes, so a stale result can never be served.

The cached value is the content-derived half of a ``TransformationResult``: the
rewritten text, its measurements, and the metadata the spec asks us to keep
(transformer name and version, token counts, bytes, cold execution time). The
positional half — where in the payload the segment lived — is supplied fresh on every
request and is deliberately *not* stored.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final

# Bumped if the on-the-wire shape of a cached entry changes. A stored entry written
# under a different schema is treated as a miss, so a format change is self-healing
# rather than a corrupt read.
CACHE_SCHEMA_VERSION: Final = 1

_SEP: Final = "\x1f"  # unit separator — cannot occur in any of the joined fields


def content_hash(data: bytes) -> str:
    """The deterministic identity of some original content: SHA-256, hex."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Every input that can change a transformation's output.

    ``digest`` folds them into one opaque storage key. The fields are kept separate
    rather than pre-joined so a caller (and a test) can see exactly what a key commits
    to, and so the storage digest stays an implementation detail of this class.
    """

    kind: str
    """``text`` or ``document`` — keeps the two key spaces from ever colliding."""

    content_hash: str
    """SHA-256 of the original content, computed *before* transformation."""

    transformer_version: str
    """Registry/service fingerprint: every applicable transformer's name and version."""

    gateway_version: str
    options_fingerprint: str
    """Digest of the transformation options in force."""

    encoding: str
    """Tokenizer encoding, because the cached token counts are specific to it."""

    qualifier: str = ""
    """Extra discriminators that steer transformation without being part of the
    content bytes — for a document, its declared media type and filename, which can
    change how the format is detected."""

    def digest(self) -> str:
        raw = _SEP.join(
            (
                self.kind,
                self.content_hash,
                self.transformer_version,
                self.gateway_version,
                self.options_fingerprint,
                self.encoding,
                self.qualifier,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedTransformation:
    """The stored result of one transformation — everything but where it lived.

    The `execution_time_ms` recorded here is the *cold* time: what the transformation
    cost the first time. A warm hit that reuses this entry costs microseconds, which
    the pipeline measures separately; keeping the cold figure lets a report say what
    the cache saved.
    """

    transformation_name: str
    transformer_version: str
    content_type: str
    transformed_content: str
    original_size_bytes: int
    transformed_size_bytes: int
    original_token_count: int
    transformed_token_count: int
    steps: tuple[str, ...]
    execution_time_ms: float
    created_at: float = field(default_factory=time.time)

    @property
    def changed(self) -> bool:
        return bool(self.steps)

    def to_bytes(self) -> bytes:
        """Serialize for a byte-oriented backend. Compact, deterministic."""
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "name": self.transformation_name,
            "tv": self.transformer_version,
            "ct": self.content_type,
            "content": self.transformed_content,
            "ob": self.original_size_bytes,
            "tb": self.transformed_size_bytes,
            "ot": self.original_token_count,
            "tt": self.transformed_token_count,
            "steps": list(self.steps),
            "ms": self.execution_time_ms,
            "at": self.created_at,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> CachedTransformation | None:
        """Deserialize, returning ``None`` for anything malformed or stale.

        A corrupted entry, a truncated write, or one from an older schema must never
        raise into the request path — it is simply not a usable hit, so we treat it as
        a miss and let the transformation run.
        """
        try:
            payload: Any = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA_VERSION:
            return None
        try:
            return cls(
                transformation_name=str(payload["name"]),
                transformer_version=str(payload["tv"]),
                content_type=str(payload["ct"]),
                transformed_content=str(payload["content"]),
                original_size_bytes=int(payload["ob"]),
                transformed_size_bytes=int(payload["tb"]),
                original_token_count=int(payload["ot"]),
                transformed_token_count=int(payload["tt"]),
                steps=tuple(str(step) for step in payload["steps"]),
                execution_time_ms=float(payload["ms"]),
                created_at=float(payload["at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class CacheStatsSnapshot:
    """An immutable read of the counters at one instant."""

    hits: int
    misses: int
    stores: int
    errors: int
    corrupted: int

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return round(self.hits / self.lookups, 4) if self.lookups else 0.0


class CacheStats:
    """Thread-safe counters. Cheap, lock-guarded integer increments.

    The cache is consulted from worker threads (large bodies) and the event-loop
    thread (small ones) alike, so the counters take a lock. They hold no user content —
    only how often the cache helped.
    """

    __slots__ = ("_corrupted", "_errors", "_hits", "_lock", "_misses", "_stores")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._errors = 0
        self._corrupted = 0

    def record_hit(self) -> None:
        with self._lock:
            self._hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    def record_store(self) -> None:
        with self._lock:
            self._stores += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def record_corrupted(self) -> None:
        with self._lock:
            self._corrupted += 1

    def snapshot(self) -> CacheStatsSnapshot:
        with self._lock:
            return CacheStatsSnapshot(
                hits=self._hits,
                misses=self._misses,
                stores=self._stores,
                errors=self._errors,
                corrupted=self._corrupted,
            )
