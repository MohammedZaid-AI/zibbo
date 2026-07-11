"""Deterministic transformation cache.

Transformation is a pure function of its input, the transformers in play, the gateway
version and the active options — so transforming the same content twice is wasted work.
This subsystem does it once and reuses the result: hash the original content, look it
up, and on a miss transform, store, and forward.

It caches only deterministic transformation *outputs* — extracted Markdown, cleaned
text, and the token/byte measurements that go with them. It never caches a failed or
partial transformation, and never a provider response. Those are the safety lines the
whole design is built to hold; see docs/CACHE.md.

The gateway core does not know the cache exists. The pipeline consults one object,
``TransformationCache``, and every backend hides behind one small byte-store interface,
so in-memory, Redis, and a future Postgres or S3 store are interchangeable.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from gateway import __version__
from gateway.cache.backend import CacheBackend
from gateway.cache.memory import InMemoryCacheBackend
from gateway.cache.models import (
    CachedTransformation,
    CacheKey,
    CacheStats,
    CacheStatsSnapshot,
    content_hash,
)
from gateway.cache.redis import RedisCacheBackend
from gateway.cache.service import TransformationCache
from gateway.config import CacheBackend as CacheBackendKind
from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.config import Settings

logger = get_logger(__name__)

__all__ = [
    "CacheBackend",
    "CacheKey",
    "CacheStats",
    "CacheStatsSnapshot",
    "CachedTransformation",
    "InMemoryCacheBackend",
    "RedisCacheBackend",
    "TransformationCache",
    "build_transformation_cache",
    "content_hash",
    "options_fingerprint",
]


def options_fingerprint(settings: Settings) -> str:
    """A digest of every setting that can change a transformation's output.

    Changing any of these must retire the cache — a request cached under
    ``preserve_links`` is wrong once links are being dropped. Settings that affect only
    *whether* a request is optimized (endpoint eligibility, size caps) are excluded:
    they never change the output of a transformation that does run.
    """
    material = {
        "text_collapse_inline_whitespace": settings.text_collapse_inline_whitespace,
        "text_dedupe_consecutive_paragraphs": settings.text_dedupe_consecutive_paragraphs,
        "json_remove_empty_containers": settings.json_remove_empty_containers,
        "html_preserve_links": settings.html_preserve_links,
        "html_preserve_images": settings.html_preserve_images,
        "documents_enabled": settings.documents_enabled,
        "documents_disabled_formats": sorted(settings.documents_disabled_formats),
        "min_segment_chars": settings.optimization_min_segment_chars,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _build_backend(settings: Settings) -> CacheBackend:
    if settings.cache_backend is CacheBackendKind.REDIS:
        if settings.redis_url:
            return RedisCacheBackend(settings.redis_url, prefix=settings.cache_redis_prefix)
        # Asked for Redis with no URL: fall back rather than refuse to start, and say so.
        logger.warning("cache_redis_no_url", detail="falling back to in-memory cache")
    return InMemoryCacheBackend(
        max_entries=settings.cache_max_entries,
        max_bytes=settings.cache_max_bytes,
    )


def build_transformation_cache(settings: Settings) -> TransformationCache:
    """Assemble the cache from configuration. Always returns an object.

    When ``cache_enabled`` is false the cache is a fully-wired no-op: every lookup
    misses and every store is dropped, so the pipeline needs no special case.
    """
    return TransformationCache(
        _build_backend(settings),
        gateway_version=__version__,
        options_fingerprint=options_fingerprint(settings),
        enabled=settings.cache_enabled,
        ttl_seconds=settings.cache_ttl_seconds,
    )
