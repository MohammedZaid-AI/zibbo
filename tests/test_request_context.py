"""Request-id propagation and timing headers."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from gateway.utils.ids import REQUEST_ID_PREFIX, is_request_id, new_request_id

pytestmark = pytest.mark.integration


async def test_a_request_id_is_generated_and_returned(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    request_id = response.headers["x-request-id"]
    assert request_id.startswith(REQUEST_ID_PREFIX)
    assert len(request_id) == len(REQUEST_ID_PREFIX) + 32


async def test_client_supplied_request_id_is_honoured(client: AsyncClient) -> None:
    """Preserving the caller's id is what makes a trace span the gateway boundary."""
    response = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-123"})

    assert response.headers["x-request-id"] == "trace-abc-123"


async def test_unusable_client_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": ""})

    assert response.headers["x-request-id"].startswith(REQUEST_ID_PREFIX)


async def test_each_request_gets_a_distinct_id(client: AsyncClient) -> None:
    first = await client.get("/health/live")
    second = await client.get("/health/live")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_process_time_header_is_present_and_numeric(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert float(response.headers["x-process-time"]) >= 0


async def test_request_id_is_attached_to_error_responses(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["x-request-id"]
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_new_request_id_is_unique() -> None:
    assert len({new_request_id() for _ in range(1000)}) == 1000


@pytest.mark.parametrize(
    ("value", "expected"),
    [("req_abc", True), ("", False), ("x" * 129, False), ("bad\nvalue", False)],
)
def test_is_request_id_rejects_unusable_values(value: str, expected: bool) -> None:
    assert is_request_id(value) is expected
