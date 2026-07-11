"""Security audit, asserted rather than asserted-to.

The credential tests capture real log output and search it, because a promise that
"we don't log keys" is worth nothing unless something fails when someone logs one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import OpenAIProvider, ProxyService
from tests.conftest import build_settings
from tests.mocks.openai_upstream import UpstreamRecorder, create_upstream_app

pytestmark = pytest.mark.integration

UPSTREAM = "http://upstream.test/v1"
SECRET_KEY = "sk-proj-SUPERSECRET1234567890"
SECRET_PROMPT = "my social security number is 123-45-6789"


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
async def upstream_client(upstream: UpstreamRecorder) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_upstream_app(upstream))
    async with AsyncClient(transport=transport, base_url="http://upstream.test") as client:
        yield client


def _app(settings: Settings, upstream_client: AsyncClient) -> FastAPI:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    return app


@pytest.fixture
async def client(upstream_client: AsyncClient) -> AsyncIterator[AsyncClient]:
    settings = build_settings(
        openai_base_url=UPSTREAM, openai_api_key=SecretStr(SECRET_KEY), log_level="DEBUG"
    )
    app = _app(settings, upstream_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            yield http
    app.dependency_overrides.clear()


@pytest.fixture
def captured_logs() -> AsyncIterator[list[str]]:
    """Everything written to the logging pipeline, as rendered text."""
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers, root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    yield records
    root.handlers, root.level = previous_handlers, previous_level


# -- Credentials never reach the logs --------------------------------------


async def test_the_configured_api_key_never_appears_in_logs(
    client: AsyncClient, captured_logs: list[str]
) -> None:
    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    blob = "\n".join(captured_logs)
    assert SECRET_KEY not in blob
    assert "sk-" not in blob


async def test_a_caller_supplied_api_key_never_appears_in_logs(
    client: AsyncClient, captured_logs: list[str]
) -> None:
    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": f"Bearer {SECRET_KEY}"},
    )

    assert SECRET_KEY not in "\n".join(captured_logs)


async def test_prompt_content_never_appears_in_logs(
    client: AsyncClient, captured_logs: list[str]
) -> None:
    """Metadata only. The whole product depends on this being true."""
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": f"<p>{SECRET_PROMPT}</p><p>x</p>"}],
        },
    )

    blob = "\n".join(captured_logs)
    assert SECRET_PROMPT not in blob
    assert "123-45-6789" not in blob
    assert "social security" not in blob


async def test_transformed_content_never_appears_in_logs(
    client: AsyncClient, captured_logs: list[str]
) -> None:
    """The *output* of a transformer is user content too."""
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "<h1>CONFIDENTIAL</h1><p>secret body</p>"}],
        },
    )

    blob = "\n".join(captured_logs)
    assert "CONFIDENTIAL" not in blob
    assert "secret body" not in blob


async def test_an_upstream_failure_does_not_log_the_authorization_header(
    captured_logs: list[str],
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    settings = build_settings(openai_base_url=UPSTREAM)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=httpx.MockTransport(boom))
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            await http.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": []},
                headers={"Authorization": f"Bearer {SECRET_KEY}"},
            )

    assert SECRET_KEY not in "\n".join(captured_logs)


# -- Credentials never reach a repr ----------------------------------------


def test_the_api_key_is_not_exposed_by_settings_repr() -> None:
    settings = build_settings(openai_api_key=SecretStr(SECRET_KEY))

    assert SECRET_KEY not in repr(settings)
    assert SECRET_KEY not in str(settings)
    assert SECRET_KEY not in str(settings.model_dump())


def test_the_api_key_is_not_exposed_by_provider_repr() -> None:
    provider = OpenAIProvider(base_url=UPSTREAM, api_key=SecretStr(SECRET_KEY))

    assert SECRET_KEY not in repr(provider)


# -- Header injection -------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "abc\r\nX-Injected: 1",
        "abc\nX-Injected: 1",
        "abc\rX-Injected: 1",
        "abc\x00def",
        "x" * 200,
        "",
    ],
)
async def test_a_hostile_request_id_is_replaced_not_echoed(
    client: AsyncClient, hostile: str
) -> None:
    """We echo `X-Request-ID` back. An unvalidated echo is a CRLF injection primitive."""
    response = await client.get("/health/live", headers={"X-Request-ID": hostile})

    returned = response.headers["x-request-id"]
    assert returned.startswith("req_")
    assert "X-Injected" not in str(response.headers)
    assert "\r" not in returned and "\n" not in returned


async def test_a_benign_request_id_is_still_honoured(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["x-request-id"] == "trace-abc-123"


async def test_a_client_cannot_forge_the_optimization_headers(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """The gateway owns these. A caller-supplied value must not survive."""
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        headers={"X-Zibbo-Tokens-Saved": "999999"},
    )

    assert response.headers.get("x-zibbo-tokens-saved") != "999999"


async def test_hop_by_hop_headers_are_not_relayed_upstream(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """`Proxy-Authorization` authenticates the caller to us, not to OpenAI."""
    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": []},
        headers={"Proxy-Authorization": "Basic c2VjcmV0", "Connection": "keep-alive"},
    )

    assert "proxy-authorization" not in upstream.last.headers


# -- Safe error handling ----------------------------------------------------


async def test_an_unhandled_exception_reveals_nothing(client: AsyncClient) -> None:
    """A 500 must never carry a traceback, a path, or a connection string."""
    app = client._transport.app  # type: ignore[union-attr, attr-defined]

    @app.get("/_boom")
    async def _boom() -> None:
        raise RuntimeError(f"db=postgres://user:{SECRET_KEY}@host/db")

    response = await client.get("/_boom")
    body = response.json()

    assert response.status_code == 500
    assert SECRET_KEY not in response.text
    assert "postgres://" not in response.text
    assert "Traceback" not in response.text
    assert body["error"]["code"] == "internal_error"


async def test_openapi_is_not_served_in_production() -> None:
    """Schema disclosure is free reconnaissance."""
    from gateway.config import Environment

    settings = build_settings(environment=Environment.PRODUCTION)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            assert (await http.get("/openapi.json")).status_code == 404


async def test_upstream_response_headers_cannot_smuggle_a_status(
    upstream_client: AsyncClient,
) -> None:
    """httpx parses upstream headers; a `\\r\\n` in a value cannot split the response."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-safe": "value"}, content=b"{}")

    settings = build_settings(openai_base_url=UPSTREAM)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=httpx.MockTransport(responder))
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post("/v1/chat/completions", json={"model": "m", "messages": []})

    assert response.status_code == 200
    assert response.headers["x-safe"] == "value"
