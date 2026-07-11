"""The cache service: keys, serialization, invalidation, corruption, statistics.

These test the guarantees that make the cache safe to consult blindly — a key that
commits to everything affecting output, a read that self-heals past corruption, and a
failure that is counted rather than raised.
"""

from __future__ import annotations

from gateway.cache import options_fingerprint
from gateway.cache.memory import InMemoryCacheBackend
from gateway.cache.models import (
    CACHE_SCHEMA_VERSION,
    CachedTransformation,
    CacheKey,
    content_hash,
)
from gateway.cache.service import TransformationCache
from tests.conftest import build_settings


def _entry(content: str = "output", steps: tuple[str, ...] = ("x",)) -> CachedTransformation:
    return CachedTransformation(
        transformation_name="html",
        transformer_version="v1",
        content_type="html",
        transformed_content=content,
        original_size_bytes=100,
        transformed_size_bytes=len(content),
        original_token_count=50,
        transformed_token_count=10,
        steps=steps,
        execution_time_ms=1.5,
    )


def _cache(backend: InMemoryCacheBackend | None = None, **kw: object) -> TransformationCache:
    return TransformationCache(
        backend or InMemoryCacheBackend(),
        gateway_version="0.1.0",
        options_fingerprint="opt",
        **kw,  # type: ignore[arg-type]
    )


# -- Content hashing --------------------------------------------------------


def test_content_hash_is_sha256_and_deterministic() -> None:
    assert content_hash(b"hello") == content_hash(b"hello")
    assert len(content_hash(b"hello")) == 64  # sha256 hex
    assert content_hash(b"hello") != content_hash(b"world")


# -- Keys -------------------------------------------------------------------


def test_key_digest_is_stable() -> None:
    key = CacheKey("text", "abc", "tv", "gv", "opt", "enc")
    assert key.digest() == key.digest()


def test_every_component_changes_the_digest() -> None:
    base = CacheKey("text", "abc", "tv", "gv", "opt", "enc", "q")
    variants = [
        CacheKey("document", "abc", "tv", "gv", "opt", "enc", "q"),
        CacheKey("text", "XXX", "tv", "gv", "opt", "enc", "q"),
        CacheKey("text", "abc", "XX", "gv", "opt", "enc", "q"),
        CacheKey("text", "abc", "tv", "XX", "opt", "enc", "q"),
        CacheKey("text", "abc", "tv", "gv", "XXX", "enc", "q"),
        CacheKey("text", "abc", "tv", "gv", "opt", "XXX", "q"),
        CacheKey("text", "abc", "tv", "gv", "opt", "enc", "X"),
    ]
    digests = {base.digest()} | {v.digest() for v in variants}
    assert len(digests) == len(variants) + 1  # all distinct


def test_text_and_document_keys_never_collide() -> None:
    cache = _cache()
    text = cache.text_key("data", transformer_fingerprint="f", encoding="enc")
    doc = cache.document_key(
        b"data", service_version="f", media_type=None, filename=None, encoding="enc"
    )
    assert text.digest() != doc.digest()


def test_document_key_folds_in_media_type_and_filename() -> None:
    cache = _cache()
    a = cache.document_key(
        b"d", service_version="v", media_type="application/pdf", filename="a.pdf", encoding="e"
    )
    b = cache.document_key(
        b"d", service_version="v", media_type="text/csv", filename="a.csv", encoding="e"
    )
    assert a.digest() != b.digest()


# -- Serialization round-trip ----------------------------------------------


def test_entry_survives_a_round_trip() -> None:
    entry = _entry()
    restored = CachedTransformation.from_bytes(entry.to_bytes())
    assert restored == entry


def test_a_truncated_entry_deserializes_to_none() -> None:
    raw = _entry().to_bytes()
    assert CachedTransformation.from_bytes(raw[: len(raw) // 2]) is None


def test_a_stale_schema_deserializes_to_none() -> None:
    raw = (
        _entry()
        .to_bytes()
        .replace(
            f'"schema":{CACHE_SCHEMA_VERSION}'.encode(),
            b'"schema":999',
        )
    )
    assert CachedTransformation.from_bytes(raw) is None


def test_garbage_deserializes_to_none() -> None:
    assert CachedTransformation.from_bytes(b"\x00\x01not json") is None
    assert CachedTransformation.from_bytes(b"[]") is None


# -- get / put --------------------------------------------------------------


def test_put_then_get_returns_the_entry() -> None:
    cache = _cache()
    key = cache.text_key("in", transformer_fingerprint="f", encoding="e")
    cache.put(key, _entry())
    got = cache.get(key)
    assert got is not None
    assert got.transformed_content == "output"


def test_a_disabled_cache_never_stores_or_returns() -> None:
    cache = _cache(enabled=False)
    key = cache.text_key("in", transformer_fingerprint="f", encoding="e")
    cache.put(key, _entry())
    assert cache.get(key) is None
    assert cache.stats().stores == 0


def test_a_corrupt_stored_value_is_a_miss_and_is_evicted() -> None:
    backend = InMemoryCacheBackend()
    cache = _cache(backend)
    key = cache.text_key("in", transformer_fingerprint="f", encoding="e")
    backend.set(key.digest(), b"corrupt not-json")

    assert cache.get(key) is None
    stats = cache.stats()
    assert stats.corrupted == 1
    assert stats.misses == 1
    # The poisoned key is dropped so it cannot fail every future lookup.
    assert backend.get(key.digest()) is None


# -- Invalidation -----------------------------------------------------------


def test_a_different_transformer_fingerprint_misses() -> None:
    backend = InMemoryCacheBackend()
    cache = _cache(backend)
    stored = cache.text_key("in", transformer_fingerprint="v1", encoding="e")
    cache.put(stored, _entry())

    bumped = cache.text_key("in", transformer_fingerprint="v2", encoding="e")
    assert cache.get(bumped) is None  # a version bump retires the old entry


def test_options_fingerprint_changes_with_a_relevant_setting() -> None:
    base = options_fingerprint(build_settings())
    flipped = options_fingerprint(build_settings(html_preserve_links=False))
    assert base != flipped


def test_options_fingerprint_ignores_irrelevant_settings() -> None:
    """Endpoint eligibility and size caps do not change a transformation's output."""
    base = options_fingerprint(build_settings())
    same = options_fingerprint(build_settings(optimization_max_body_bytes=1234))
    assert base == same


# -- Stats ------------------------------------------------------------------


def test_stats_count_hits_and_misses() -> None:
    cache = _cache()
    key = cache.text_key("in", transformer_fingerprint="f", encoding="e")
    assert cache.get(key) is None  # miss
    cache.put(key, _entry())
    assert cache.get(key) is not None  # hit

    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.stores == 1
    assert stats.lookups == 2
    assert stats.hit_rate == 0.5


def test_hit_rate_is_zero_with_no_lookups() -> None:
    assert _cache().stats().hit_rate == 0.0
