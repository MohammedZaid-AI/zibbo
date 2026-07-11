"""Transformation-cache introspection.

Read-only. Answers the operator's question — "is the cache doing anything?" — with the
hit rate and the raw counters behind it. Mounted under ``/internal`` because it
describes the deployment, not the API.
"""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api.deps import CacheDep
from gateway.api.schemas.cache import CacheStatusResponse

router = APIRouter(prefix="/internal", tags=["cache"])


@router.get("/cache", response_model=CacheStatusResponse, summary="Transformation cache")
async def cache_status(cache: CacheDep) -> CacheStatusResponse:
    stats = cache.stats()
    return CacheStatusResponse(
        enabled=cache.enabled,
        backend=cache.backend_name,
        hits=stats.hits,
        misses=stats.misses,
        stores=stats.stores,
        errors=stats.errors,
        corrupted=stats.corrupted,
        lookups=stats.lookups,
        hit_rate=stats.hit_rate,
    )
