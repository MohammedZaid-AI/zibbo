"""The error envelope.

Callers switch to Zibbo by changing one URL. If our failures don't look
like the provider's failures, their error handling silently stops working — so
the envelope shape is part of the public contract and is pinned here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel

from gateway.config import Settings, get_settings
from gateway.errors import BadRequestError, UpstreamTimeoutError
from gateway.main import create_app

pytestmark = pytest.mark.integration

ENVELOPE_KEYS = {"message", "type", "param", "code", "request_id"}


class _EchoBody(BaseModel):
    name: str
    count: int


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    """The standard app plus routes that fail in each way we care about."""
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings

    @application.get("/_test/upstream-timeout")
    async def _upstream_timeout() -> None:
        raise UpstreamTimeoutError("openai did not respond", context={"provider": "openai"})

    @application.get("/_test/bad-request")
    async def _bad_request() -> None:
        raise BadRequestError("model is required", param="model", code="missing_parameter")

    @application.get("/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("connection string: postgres://user:hunter2@db")

    @application.post("/_test/echo")
    async def _echo(body: _EchoBody) -> _EchoBody:
        return body

    yield application
    application.dependency_overrides.clear()


async def test_gateway_error_maps_to_its_status_and_type(client: AsyncClient) -> None:
    response = await client.get("/_test/upstream-timeout")
    error = response.json()["error"]

    assert response.status_code == 504
    assert error["type"] == "upstream_error"
    assert error["code"] == "upstream_timeout"
    assert error["message"] == "openai did not respond"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_client_error_carries_param_and_code(client: AsyncClient) -> None:
    response = await client.get("/_test/bad-request")
    error = response.json()["error"]

    assert response.status_code == 400
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "missing_parameter"
    assert error["param"] == "model"


async def test_unhandled_exception_does_not_leak_internals(client: AsyncClient) -> None:
    response = await client.get("/_test/boom")
    body = response.json()

    assert response.status_code == 500
    assert body["error"]["type"] == "api_error"
    assert body["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "postgres://" not in response.text


async def test_unhandled_exception_still_carries_a_request_id(client: AsyncClient) -> None:
    """The 500 is rendered above our middleware; the id must survive that path."""
    response = await client.get("/_test/boom")

    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


async def test_request_id_header_is_not_duplicated(client: AsyncClient) -> None:
    response = await client.get("/_test/upstream-timeout")

    assert "," not in response.headers["x-request-id"]


async def test_unknown_route_uses_the_envelope(client: AsyncClient) -> None:
    # Not under /v1: that prefix is the OpenAI proxy's catch-all and never 404s here.
    response = await client.get("/nope")
    error = response.json()["error"]

    assert response.status_code == 404
    assert error["type"] == "not_found_error"
    assert set(error) == ENVELOPE_KEYS


async def test_method_not_allowed_uses_the_envelope(client: AsyncClient) -> None:
    response = await client.post("/health/live")

    assert response.status_code == 405
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_validation_error_names_the_offending_field(client: AsyncClient) -> None:
    response = await client.post("/_test/echo", json={"name": "a", "count": "not-a-number"})
    error = response.json()["error"]

    assert response.status_code == 422
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_parameter"
    assert error["param"] == "count"
    assert error["details"], "the full pydantic error list is preserved for debugging"


async def test_validation_error_reports_a_missing_field(client: AsyncClient) -> None:
    response = await client.post("/_test/echo", json={"name": "a"})

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "count"


async def test_errors_never_use_fastapis_detail_key(client: AsyncClient) -> None:
    """FastAPI's default {"detail": ...} would break provider SDK error parsing."""
    for path, method in (
        ("/nope", "GET"),
        ("/_test/bad-request", "GET"),
        ("/_test/boom", "GET"),
    ):
        response = await client.request(method, path)
        assert "detail" not in response.json()
        assert "error" in response.json()
