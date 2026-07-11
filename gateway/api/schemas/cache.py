"""Wire schema for transformation-cache introspection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CacheStatusResponse(BaseModel):
    """The cache's configuration and running counters.

    Metadata only — how often the cache helped, never what it holds. The counters are
    process-lifetime totals; for a per-replica shared (Redis) cache they describe this
    replica's view of it.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool
    backend: str

    hits: int
    misses: int
    stores: int
    errors: int
    corrupted: int

    lookups: int
    hit_rate: float
