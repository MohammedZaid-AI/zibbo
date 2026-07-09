"""Application assembly: docs exposure, CORS, lifespan state."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.config import Environment
from gateway.main import create_app
from tests.conftest import build_settings

pytestmark = pytest.mark.integration


async def test_lifespan_populates_state() -> None:
    settings = build_settings()
    app = create_app(settings)

    assert not hasattr(app.state, "health")

    async with app.router.lifespan_context(app):
        assert app.state.settings is settings
        assert app.state.health is not None
        assert app.state.started_at > 0


async def test_openapi_is_served_outside_production() -> None:
    app = create_app(build_settings(environment=Environment.DEVELOPMENT))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.get("/docs")).status_code == 200


async def test_openapi_is_hidden_in_production() -> None:
    """Schema disclosure is free reconnaissance; production must not serve it."""
    app = create_app(build_settings(environment=Environment.PRODUCTION))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/health/live")).status_code == 200


async def test_cors_headers_are_returned_for_allowed_origins() -> None:
    app = create_app(build_settings(cors_allow_origins=["http://dash.test"]))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        response = await client.get("/health/live", headers={"Origin": "http://dash.test"})

    assert response.headers["access-control-allow-origin"] == "http://dash.test"
    # The dashboard reads the request id off the response, so it must be exposed.
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


async def test_cors_middleware_is_absent_when_no_origins_configured() -> None:
    app = create_app(build_settings(cors_allow_origins=[]))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        response = await client.get("/health/live", headers={"Origin": "http://dash.test"})

    assert "access-control-allow-origin" not in response.headers


async def test_preflight_request_carries_a_request_id() -> None:
    """RequestContextMiddleware sits outside CORS, so even preflights are traceable."""
    app = create_app(build_settings(cors_allow_origins=["http://dash.test"]))
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://gateway.test") as client,
    ):
        response = await client.options(
            "/health/live",
            headers={
                "Origin": "http://dash.test",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"]
