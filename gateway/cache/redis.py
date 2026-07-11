"""A Redis-backed byte store, shared across replicas and surviving restarts.

Redis is optional. The ``redis`` package is imported lazily, so a deployment that does
not use this backend need not install it. And the backend is defensive to a fault:
*every* Redis call is wrapped, and any failure — the package missing, the server down,
a timeout, a connection reset — degrades to a miss or a no-op. A cache is an
optimization; its unavailability must slow the gateway, never break it.

The synchronous client is deliberate. See ``backend.py``: the cache is consulted on the
same (often worker) thread as the transformation it guards, so a blocking round-trip
does not stall the event loop for the large payloads that dominate cache value.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

from gateway.cache.backend import CacheBackend
from gateway.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


class RedisCacheBackend(CacheBackend):
    """A namespaced byte store on Redis, with graceful degradation."""

    name: ClassVar[str] = "redis"

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "zibbo:xform:",
        socket_timeout: float = 0.5,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._socket_timeout = socket_timeout
        self._client: Any | None = None
        self._unavailable = False  # latched once construction of a client fails hard

    def _redis(self) -> Any | None:
        """Return a connected client, or ``None`` if Redis cannot be used.

        The client is built once, lazily. ``redis-py`` connects on first command, not
        on construction, so a bad URL or a down server surfaces at ``get``/``set`` and
        is handled there, not here.
        """
        if self._client is not None:
            return self._client
        if self._unavailable:
            return None
        try:
            import redis
        except ImportError:
            logger.warning("redis_package_missing", detail="pip install redis to use the cache")
            self._unavailable = True
            return None
        self._client = redis.Redis.from_url(
            self._url,
            socket_timeout=self._socket_timeout,
            socket_connect_timeout=self._socket_timeout,
        )
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _guard(self, op: str, action: Callable[[Any], Any]) -> Any | None:
        client = self._redis()
        if client is None:
            return None
        try:
            return action(client)
        except Exception as exc:  # noqa: BLE001 — any Redis failure degrades to a miss
            logger.warning("redis_cache_unavailable", operation=op, cause=type(exc).__name__)
            return None

    def get(self, key: str) -> bytes | None:
        result = self._guard("get", lambda c: c.get(self._key(key)))
        return result if isinstance(result, bytes) else None

    def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> bool:
        namespaced = self._key(key)
        if ttl_seconds:
            stored = self._guard("setex", lambda c: c.setex(namespaced, ttl_seconds, value))
        else:
            stored = self._guard("set", lambda c: c.set(namespaced, value))
        return bool(stored)

    def delete(self, key: str) -> None:
        self._guard("delete", lambda c: c.delete(self._key(key)))

    def clear(self) -> None:
        """Delete only this namespace's keys, never the whole database."""

        def _scan_delete(client: Any) -> None:
            for found in client.scan_iter(match=f"{self._prefix}*"):
                client.delete(found)

        self._guard("clear", _scan_delete)

    def probe(self) -> bool:
        return bool(self._guard("ping", lambda c: c.ping()))

    def close(self) -> None:
        if self._client is not None:
            # Closing a broken client must not raise; a failed close is not our problem.
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None
