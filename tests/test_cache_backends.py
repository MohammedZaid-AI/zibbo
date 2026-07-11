"""The storage backends in isolation: LRU bounds, TTL, and graceful Redis failure.

A backend is a byte store. These tests hold it to exactly that contract — round-trip,
eviction on both axes, expiry, and (for Redis) that every conceivable failure degrades
to a miss rather than an exception.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from gateway.cache.memory import InMemoryCacheBackend
from gateway.cache.redis import RedisCacheBackend


class TestInMemoryBackend:
    def test_round_trips_a_value(self) -> None:
        cache = InMemoryCacheBackend()
        assert cache.set("k", b"value")
        assert cache.get("k") == b"value"

    def test_missing_key_is_none(self) -> None:
        assert InMemoryCacheBackend().get("absent") is None

    def test_delete_removes_and_frees_bytes(self) -> None:
        cache = InMemoryCacheBackend()
        cache.set("k", b"12345")
        cache.delete("k")
        assert cache.get("k") is None
        assert cache.total_bytes == 0

    def test_clear_empties_everything(self) -> None:
        cache = InMemoryCacheBackend()
        cache.set("a", b"1")
        cache.set("b", b"2")
        cache.clear()
        assert cache.entry_count == 0
        assert cache.total_bytes == 0

    def test_evicts_least_recently_used_over_entry_cap(self) -> None:
        cache = InMemoryCacheBackend(max_entries=2, max_bytes=10_000)
        cache.set("a", b"a")
        cache.set("b", b"b")
        cache.get("a")  # touch a, so b is now the LRU
        cache.set("c", b"c")
        assert cache.get("b") is None  # evicted
        assert cache.get("a") == b"a"
        assert cache.get("c") == b"c"

    def test_evicts_over_byte_cap(self) -> None:
        cache = InMemoryCacheBackend(max_entries=1000, max_bytes=10)
        cache.set("a", b"12345")
        cache.set("b", b"12345")
        cache.set("c", b"12345")  # would exceed 10 bytes, evicts oldest
        assert cache.total_bytes <= 10
        assert cache.get("a") is None

    def test_value_larger_than_budget_is_refused(self) -> None:
        cache = InMemoryCacheBackend(max_entries=10, max_bytes=4)
        assert not cache.set("k", b"12345")
        assert cache.get("k") is None

    def test_ttl_expires(self) -> None:
        cache = InMemoryCacheBackend()
        cache.set("k", b"v", ttl_seconds=1)
        assert cache.get("k") == b"v"
        # Advance past the deadline without sleeping a whole second.
        entry = cache._store["k"]
        entry.expires_at = time.monotonic() - 1
        assert cache.get("k") is None
        assert cache.entry_count == 0  # a lazy-expired entry is dropped on read

    def test_overwrite_updates_byte_accounting(self) -> None:
        cache = InMemoryCacheBackend()
        cache.set("k", b"12345")
        cache.set("k", b"1")
        assert cache.total_bytes == 1
        assert cache.get("k") == b"1"

    def test_rejects_nonpositive_bounds(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            InMemoryCacheBackend(max_entries=0)

    def test_concurrent_writes_keep_byte_count_consistent(self) -> None:
        """Many threads hammering the same keys must not corrupt the byte accounting."""
        cache = InMemoryCacheBackend(max_entries=50, max_bytes=1_000_000)

        def worker(n: int) -> None:
            for i in range(200):
                cache.set(f"k{(n + i) % 40}", b"x" * 10)
                cache.get(f"k{i % 40}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The invariant that must survive the race: reported bytes equal actual bytes.
        actual = sum(len(entry.value) for entry in cache._store.values())
        assert cache.total_bytes == actual
        assert cache.entry_count <= 50


class _FakeRedis:
    """A minimal in-process stand-in for a Redis client."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes) -> bool:
        self.store[key] = value
        return True

    def setex(self, key: str, ttl: int, value: bytes) -> bool:
        del ttl
        self.store[key] = value
        return True

    def delete(self, key: str) -> int:
        return int(self.store.pop(key, None) is not None)

    def scan_iter(self, match: str) -> list[str]:
        prefix = match.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


class _BrokenRedis:
    """Every operation fails, the way a down server does."""

    def __getattr__(self, _name: str) -> Any:
        def _raise(*_a: object, **_k: object) -> None:
            raise ConnectionError("redis is down")

        return _raise


def _backend_with(client: object) -> RedisCacheBackend:
    backend = RedisCacheBackend("redis://localhost:6379/0", prefix="t:")
    backend._client = client
    return backend


class TestRedisBackend:
    def test_round_trips_through_a_fake_client(self) -> None:
        backend = _backend_with(_FakeRedis())
        assert backend.set("k", b"v")
        assert backend.get("k") == b"v"

    def test_namespaces_keys_with_the_prefix(self) -> None:
        fake = _FakeRedis()
        _backend_with(fake).set("k", b"v")
        assert "t:k" in fake.store

    def test_clear_touches_only_the_namespace(self) -> None:
        fake = _FakeRedis()
        fake.store["other:x"] = b"keep"
        backend = _backend_with(fake)
        backend.set("mine", b"v")
        backend.clear()
        assert "other:x" in fake.store
        assert "t:mine" not in fake.store

    def test_a_down_server_degrades_get_to_a_miss(self) -> None:
        backend = _backend_with(_BrokenRedis())
        assert backend.get("k") is None  # no exception

    def test_a_down_server_degrades_set_to_no_op(self) -> None:
        backend = _backend_with(_BrokenRedis())
        assert backend.set("k", b"v") is False  # no exception

    def test_probe_is_false_when_unreachable(self) -> None:
        assert _backend_with(_BrokenRedis()).probe() is False

    def test_probe_is_true_when_reachable(self) -> None:
        assert _backend_with(_FakeRedis()).probe() is True

    def test_missing_redis_package_latches_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the package absent, the backend gives up once and stays a no-op."""
        backend = RedisCacheBackend("redis://localhost:6379/0")
        import builtins

        real_import = builtins.__import__

        def _no_redis(name: str, *args: object, **kwargs: object) -> Any:
            if name == "redis":
                raise ImportError("no redis")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_redis)
        assert backend.get("k") is None
        assert backend._unavailable is True
