"""A per-process, thread-safe LRU byte cache.

The default backend. It needs nothing external, which makes it the right choice for a
single replica and the obvious one for tests. Eviction is bounded on two axes — entry
count and total bytes — because a cache of extracted 100-page PDFs is bounded by
neither on its own: a few huge entries blow the byte budget, and many tiny ones blow
the count.

Every operation takes one lock. Contention is negligible: the critical section is a
dict operation, and the expensive work (transformation, serialization) happens outside
it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import ClassVar

from gateway.cache.backend import CacheBackend


@dataclass(slots=True)
class _Entry:
    value: bytes
    expires_at: float | None  # monotonic deadline, or None for no expiry


class InMemoryCacheBackend(CacheBackend):
    """Bounded LRU over ``bytes``, safe for concurrent access."""

    name: ClassVar[str] = "memory"

    def __init__(self, *, max_entries: int = 2048, max_bytes: int = 128_000_000) -> None:
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("max_entries and max_bytes must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0

    def get(self, key: str) -> bytes | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= now:
                self._discard(key)
                return None
            self._store.move_to_end(key)  # most-recently used
            return entry.value

    def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> bool:
        # A single entry larger than the whole budget can never be admitted; storing it
        # would evict everything else and still not fit.
        if len(value) > self._max_bytes:
            return False
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        with self._lock:
            if key in self._store:
                self._discard(key)
            self._store[key] = _Entry(value, expires_at)
            self._bytes += len(value)
            self._evict_to_fit()
        return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._discard(key)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._bytes = 0

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._store)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._bytes

    # -- internals; callers already hold the lock -------------------------

    def _discard(self, key: str) -> None:
        entry = self._store.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry.value)

    def _evict_to_fit(self) -> None:
        while self._store and (
            len(self._store) > self._max_entries or self._bytes > self._max_bytes
        ):
            oldest, entry = self._store.popitem(last=False)
            self._bytes -= len(entry.value)
            del oldest
