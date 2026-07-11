"""The transformation cache the pipeline talks to.

One object over a byte-store backend that: builds keys, serializes and deserializes
entries, counts hits and misses, and — above all — never lets a cache problem reach a
request. Every method here is safe to call blindly; a backend that is down, a value
that is corrupt, or a serializer that chokes all resolve to "no usable cache", and the
transformation simply runs.

What is cacheable is decided by the *pipeline*, not here (see its ``_process_segment``).
This class only enforces the mechanical guarantees: deterministic keys, self-healing
reads, isolated failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.cache.models import (
    CachedTransformation,
    CacheKey,
    CacheStats,
    content_hash,
)
from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.cache.backend import CacheBackend
    from gateway.cache.models import CacheStatsSnapshot

logger = get_logger(__name__)


class TransformationCache:
    """Keys, (de)serialization, stats and safety over a :class:`CacheBackend`."""

    def __init__(
        self,
        backend: CacheBackend,
        *,
        gateway_version: str,
        options_fingerprint: str,
        enabled: bool = True,
        ttl_seconds: int = 0,
    ) -> None:
        self._backend = backend
        self._gateway_version = gateway_version
        self._options_fingerprint = options_fingerprint
        self._enabled = enabled
        self._ttl_seconds = ttl_seconds
        self._stats = CacheStats()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def backend_name(self) -> str:
        return self._backend.name

    # -- Key construction --------------------------------------------------

    def text_key(self, text: str, *, transformer_fingerprint: str, encoding: str) -> CacheKey:
        return CacheKey(
            kind="text",
            content_hash=content_hash(text.encode("utf-8")),
            transformer_version=transformer_fingerprint,
            gateway_version=self._gateway_version,
            options_fingerprint=self._options_fingerprint,
            encoding=encoding,
        )

    def document_key(
        self,
        data: bytes,
        *,
        service_version: str,
        media_type: str | None,
        filename: str | None,
        encoding: str,
    ) -> CacheKey:
        return CacheKey(
            kind="document",
            content_hash=content_hash(data),
            transformer_version=service_version,
            gateway_version=self._gateway_version,
            options_fingerprint=self._options_fingerprint,
            encoding=encoding,
            # Media type and filename steer format detection without being part of the
            # bytes, so two identical files declared differently must not share a slot.
            qualifier=f"{media_type or ''}|{filename or ''}",
        )

    # -- Read / write ------------------------------------------------------

    def get(self, key: CacheKey) -> CachedTransformation | None:
        """Look up a cached result. Returns ``None`` on miss, corruption, or failure."""
        if not self._enabled:
            return None
        raw = self._backend.get(key.digest())
        if raw is None:
            self._stats.record_miss()
            return None
        entry = CachedTransformation.from_bytes(raw)
        if entry is None:
            # A value was present but unusable — corrupt, truncated, or an old schema.
            # Drop it so a poisoned key does not fail every future lookup, and count it
            # as a miss so the transformation runs.
            self._stats.record_corrupted()
            self._stats.record_miss()
            self._backend.delete(key.digest())
            return None
        self._stats.record_hit()
        return entry

    def put(self, key: CacheKey, entry: CachedTransformation) -> None:
        """Store a completed transformation. Best-effort; a failure is only counted."""
        if not self._enabled:
            return
        try:
            raw = entry.to_bytes()
        except (TypeError, ValueError):  # pragma: no cover — content is always JSON-safe
            self._stats.record_error()
            return
        stored = self._backend.set(key.digest(), raw, ttl_seconds=self._ttl_seconds or None)
        if stored:
            self._stats.record_store()
        else:
            self._stats.record_error()

    # -- Introspection -----------------------------------------------------

    def stats(self) -> CacheStatsSnapshot:
        return self._stats.snapshot()

    def probe(self) -> bool:
        return self._backend.probe()

    def close(self) -> None:
        self._backend.close()
