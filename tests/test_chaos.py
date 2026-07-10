"""Failure injection.

Everything a provider or a network can do to us, done deliberately, through the
whole ASGI stack. Two assertions recur: the caller gets an OpenAI-compatible error,
and the gateway is still healthy afterwards.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.api.deps import get_proxy_service
from gateway.config import get_settings
from gateway.main import create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings

pytestmark = pytest.mark.integration

UPSTREAM = "http://upstream.test/v1"
CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

ENVELOPE_KEYS = {"message", "type", "param", "code", "request_id"}


def _failing_app(exc: Exception) -> FastAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _app_with(httpx.MockTransport(handler))


def _responding_app(responder: Callable[[httpx.Request], httpx.Response]) -> FastAPI:
    return _app_with(httpx.MockTransport(responder))


def _app_with(transport: httpx.BaseTransport) -> FastAPI:
    settings = build_settings(openai_base_url=UPSTREAM)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=transport)  # type: ignore[arg-type]
    )
    return app


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            yield http


# -- Transport-level chaos: no HTTP response ever arrives -------------------


@pytest.mark.parametrize(
    ("name", "error", "status", "code"),
    [
        (
            "dns failure",
            httpx.ConnectError("[Errno -2] Name or service not known"),
            502,
            "upstream_error",
        ),
        ("connection refused", httpx.ConnectError("Connection refused"), 502, "upstream_error"),
        ("connection reset", httpx.ReadError("Connection reset by peer"), 502, "upstream_error"),
        ("broken pipe", httpx.WriteError("Broken pipe"), 502, "upstream_error"),
        ("protocol violation", httpx.RemoteProtocolError("bad chunk"), 502, "upstream_error"),
        ("connect timeout", httpx.ConnectTimeout("timed out"), 504, "upstream_timeout"),
        ("read timeout", httpx.ReadTimeout("timed out"), 504, "upstream_timeout"),
        ("pool timeout", httpx.PoolTimeout("no free connection"), 504, "upstream_timeout"),
    ],
)
async def test_transport_failures_become_openai_shaped_errors(
    name: str, error: Exception, status: int, code: str
) -> None:
    app = _failing_app(error)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    body = response.json()
    assert response.status_code == status, name
    assert set(body["error"]) == ENVELOPE_KEYS
    assert body["error"]["code"] == code
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["request_id"].startswith("req_")


async def test_tls_failure_becomes_a_502() -> None:
    """An expired or untrusted certificate is a connect error, not a crash."""
    app = _failing_app(httpx.ConnectError(ssl.SSLCertVerificationError("certificate expired")))
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


async def test_transport_failures_never_leak_internals() -> None:
    app = _failing_app(httpx.ConnectError("dial tcp 10.0.0.7:443: connect: no route"))
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert "10.0.0.7" not in response.text
    assert "dial tcp" not in response.text


async def test_the_gateway_survives_a_transport_failure() -> None:
    """One bad request must not take the process with it."""
    app = _failing_app(httpx.ConnectError("refused"))
    async for client in _client(app):
        for _ in range(5):
            assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 502
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/health/live")).status_code == 200


# -- HTTP-level chaos: the provider answers, badly --------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504])
async def test_upstream_error_statuses_are_relayed_untouched(status: int) -> None:
    """The provider's own envelope is what its SDK parses. Never rewrap it."""
    body = b'{"error": {"message": "upstream said so", "type": "invalid_request_error"}}'

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == status
    assert response.content == body


async def test_a_429_preserves_every_retry_signal() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b'{"error": {"message": "slow down"}}',
            headers={
                "content-type": "application/json",
                "retry-after": "17",
                "x-should-retry": "true",
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "17s",
            },
        )

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"
    assert response.headers["x-should-retry"] == "true"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


async def test_an_empty_upstream_body_is_relayed_not_invented() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"")

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 500
    assert response.content == b""


async def test_a_non_json_upstream_error_is_relayed_verbatim() -> None:
    """Cloudflare returns HTML. Do not pretend it is JSON."""
    html = b"<html><body><h1>502 Bad Gateway</h1></body></html>"

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=html, headers={"content-type": "text/html"})

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 502
    assert response.content == html
    assert response.headers["content-type"] == "text/html"


async def test_a_stream_rejected_upstream_returns_json_not_sse() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b'{"error": {"message": "rate limited"}}',
            headers={"content-type": "application/json", "retry-after": "5"},
        )

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post("/v1/chat/completions", json={**CHAT, "stream": True})

    assert response.status_code == 429
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "5"


# -- Malformed requests -----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"", b"[]", b'{"messages": "not-a-list"}', b"\xff\xfe\x00", b"null"],
)
async def test_malformed_request_bodies_are_forwarded_not_rejected(body: bytes) -> None:
    """The gateway validates nothing. The provider owns that decision."""
    seen: list[bytes] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

    app = _responding_app(responder)
    async for client in _client(app):
        response = await client.post(
            "/v1/chat/completions", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == 200
    assert seen[0] == body


async def test_an_enormous_body_is_forwarded_unoptimized() -> None:
    """Graceful degradation: above the limit the gateway proxies instead of parsing."""
    huge = b'{"model":"m","messages":[{"role":"user","content":"' + b"x" * 200 + b'"}]}'
    settings = build_settings(openai_base_url=UPSTREAM, optimization_max_body_bytes=50)
    seen: list[bytes] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=httpx.MockTransport(responder))
    )
    async for client in _client(app):
        response = await client.post(
            "/v1/chat/completions", content=huge, headers={"content-type": "application/json"}
        )

    assert response.status_code == 200
    assert seen[0] == huge
    assert response.headers["x-llmgateway-optimization"] == "skipped:body_too_large"
