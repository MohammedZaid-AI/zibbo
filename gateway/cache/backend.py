"""The storage interface every backend implements.

A backend deals only in bytes keyed by a string. It knows nothing of transformations,
keys, or statistics — that is the service's job. Keeping the contract this small is
what lets in-memory, Redis, and a future Postgres or S3 backend be genuinely
interchangeable: each is a byte store with a TTL.

Two rules every backend upholds:

* **Never raise into the request path.** A failing store degrades to a miss, not an
  error. The service records the failure and the transformation simply runs.
* **Synchronous.** Transformation is CPU work the pipeline already offloads to a worker
  thread above a size threshold; the cache lookup rides the same thread. A blocking
  Redis round-trip therefore does not touch the event loop for the large payloads that
  matter most. See docs/CACHE.md for the trade-off on small payloads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class CacheBackend(ABC):
    """A byte store with per-key expiry."""

    name: ClassVar[str]

    @abstractmethod
    def get(self, key: str) -> bytes | None:
        """Return the stored bytes, or ``None`` on a miss or any backend failure."""

    @abstractmethod
    def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> bool:
        """Store ``value``. Return whether it was stored; never raise."""

    def delete(self, key: str) -> None:  # noqa: B027 — optional hook, no-op by default
        """Remove a key if present. Idempotent. Used by tests and invalidation."""

    def clear(self) -> None:  # noqa: B027 — optional hook, no-op by default
        """Drop everything this backend holds. Optional; used in tests."""

    def probe(self) -> bool:
        """Whether the backend is reachable. In-memory is always ``True``."""
        return True

    def close(self) -> None:  # noqa: B027 — optional hook, no-op by default
        """Release any held resources. Default: nothing to release."""
