"""The /internal/cache introspection endpoint."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_cache_status_reports_configuration(client: AsyncClient) -> None:
    response = await client.get("/internal/cache")
    assert response.status_code == 200

    body = response.json()
    assert body["enabled"] is True
    assert body["backend"] == "memory"
    assert body["hits"] == 0
    assert body["misses"] == 0
    assert body["hit_rate"] == 0.0


async def test_cache_status_is_metadata_only(client: AsyncClient) -> None:
    """The endpoint exposes counters, never cached content."""
    body = (await client.get("/internal/cache")).json()
    assert set(body) == {
        "enabled",
        "backend",
        "hits",
        "misses",
        "stores",
        "errors",
        "corrupted",
        "lookups",
        "hit_rate",
    }
