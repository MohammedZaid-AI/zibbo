"""Anthropic through the gateway: routing, auth, optimization, errors, streaming.

The point of these tests is that the *same gateway* serves a second provider whose
every convention differs from OpenAI's — a different credential header, a different
request schema, a different error envelope, a different stream event format — with no
provider logic in the core.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.responses import StreamingResponse

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import AnthropicProvider, ProxyService
from tests.conftest import build_settings
from tests.mocks import anthropic_upstream as mock
from tests.mocks.anthropic_upstream import UpstreamRecorder, create_upstream_app

pytestmark = pytest.mark.integration

# The Anthropic SDK adds `/v1` itself, so the gateway prefix is `/anthropic` and the
# upstream base is the origin. A caller posts to `/anthropic/v1/messages`.
UPSTREAM = "http://anthropic.test"
PREFIX = "/anthropic/v1"

NOISY_HTML = (
    "<!DOCTYPE html><html><head><script>t()</script></head><body>"
    "<nav class='navbar'>Home</nav><main><h1>Guide</h1><p>Body   text.</p></main>"
    "<footer>(c) 2026</footer></body></html>"
)


def _message(content: object, *, system: object = None, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "claude-sonnet-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": content}],
        **extra,
    }
    if system is not None:
        body["system"] = system
    return body


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
async def upstream_client(upstream: UpstreamRecorder) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_upstream_app(upstream))
    async with AsyncClient(transport=transport, base_url="http://anthropic.test") as client:
        yield client


def _settings(**overrides: object) -> Settings:
    return build_settings(
        openai_enabled=False,
        anthropic_enabled=True,
        anthropic_base_url=UPSTREAM,
        **overrides,
    )


@pytest.fixture
async def client(upstream_client: AsyncClient) -> AsyncIterator[AsyncClient]:
    settings = _settings(anthropic_api_key=SecretStr("sk-ant-configured"))
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            yield http
    app.dependency_overrides.clear()


# -- Routing and body relay -------------------------------------------------


async def test_a_message_is_proxied_and_relayed_verbatim(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages", json=_message("Hello"), headers={"x-api-key": "sk-caller"}
    )

    assert response.status_code == 200
    assert response.content == mock.MESSAGE_BODY


async def test_the_path_maps_onto_the_anthropic_origin(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(f"{PREFIX}/messages", json=_message("Hello"), headers={"x-api-key": "k"})
    assert upstream.last.path == "/v1/messages"


async def test_response_headers_reach_the_caller(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages", json=_message("Hello"), headers={"x-api-key": "k"}
    )
    assert response.headers["anthropic-ratelimit-requests-remaining"] == "4999"
    assert response.headers["request-id"] == "req_anthropic_upstream_01"


# -- Authentication (x-api-key, not bearer) ---------------------------------


async def test_the_configured_key_is_injected_as_x_api_key(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(f"{PREFIX}/messages", json=_message("Hi"))
    assert upstream.last.headers["x-api-key"] == "sk-ant-configured"
    assert "authorization" not in upstream.last.headers


async def test_a_caller_x_api_key_wins_over_the_configured_one(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(f"{PREFIX}/messages", json=_message("Hi"), headers={"x-api-key": "sk-caller"})
    assert upstream.last.headers["x-api-key"] == "sk-caller"


async def test_a_caller_oauth_bearer_is_left_alone(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """Anthropic accepts an OAuth bearer token. A caller using one must not also get
    our x-api-key bolted on, or Anthropic would see two credentials."""
    await client.post(
        f"{PREFIX}/messages", json=_message("Hi"), headers={"Authorization": "Bearer oauth-token"}
    )
    assert upstream.last.headers["authorization"] == "Bearer oauth-token"
    assert "x-api-key" not in upstream.last.headers


async def test_the_anthropic_version_header_is_added(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(f"{PREFIX}/messages", json=_message("Hi"), headers={"x-api-key": "k"})
    assert upstream.last.headers["anthropic-version"] == "2023-06-01"


async def test_a_caller_version_header_is_not_overwritten(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(
        f"{PREFIX}/messages",
        json=_message("Hi"),
        headers={"x-api-key": "k", "anthropic-version": "2024-10-01"},
    )
    assert upstream.last.headers["anthropic-version"] == "2024-10-01"


# -- Optimization: the Anthropic schema -------------------------------------


async def test_html_in_a_message_is_converted(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(f"{PREFIX}/messages", json=_message(NOISY_HTML), headers={"x-api-key": "k"})

    content = upstream.last.json()["messages"][0]["content"]
    assert content == "# Guide\n\nBody text."
    assert "script" not in content


async def test_the_system_prompt_is_optimized(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """`system` is top-level in Anthropic and often the largest block. Missing it
    would forfeit most of the saving."""
    await client.post(
        f"{PREFIX}/messages",
        json=_message("thanks", system=NOISY_HTML),
        headers={"x-api-key": "k"},
    )

    assert upstream.last.json()["system"] == "# Guide\n\nBody text."


async def test_content_blocks_are_optimized(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    blocks = [{"type": "text", "text": NOISY_HTML}]
    await client.post(f"{PREFIX}/messages", json=_message(blocks), headers={"x-api-key": "k"})

    assert upstream.last.json()["messages"][0]["content"][0]["text"] == "# Guide\n\nBody text."


async def test_optimization_headers_are_reported(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages", json=_message(NOISY_HTML), headers={"x-api-key": "k"}
    )
    assert response.headers["x-llmgateway-optimization"] == "applied"
    assert int(response.headers["x-llmgateway-tokens-saved"]) > 0


async def test_a_clean_message_crosses_untouched(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    raw = json.dumps(_message("Just a plain question.")).encode()
    await client.post(
        f"{PREFIX}/messages",
        content=raw,
        headers={"x-api-key": "k", "content-type": "application/json"},
    )
    assert upstream.last.body == raw


# -- Error mapping ----------------------------------------------------------


async def test_a_native_anthropic_error_is_relayed_verbatim(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages",
        json=_message("x", model=mock.MODEL_BAD_REQUEST),
        headers={"x-api-key": "k"},
    )
    assert response.status_code == 400
    assert response.content == mock.ERROR_BODY_400
    assert response.json()["type"] == "error"


async def test_a_rate_limit_preserves_retry_after(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages",
        json=_message("x", model=mock.MODEL_RATE_LIMITED),
        headers={"x-api-key": "k"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


async def test_a_gateway_failure_uses_the_anthropic_error_envelope(
    upstream_client: AsyncClient,
) -> None:
    """The distinguishing test. A 502 the gateway authored must be Anthropic-shaped,
    or an Anthropic SDK pointed at the gateway breaks on exactly the failures that
    matter — not OpenAI's `{"error": {...}}` but Anthropic's `{"type": "error", ...}`."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=httpx.MockTransport(refuse))
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post(
                f"{PREFIX}/messages", json=_message("x"), headers={"x-api-key": "k"}
            )

    body = response.json()
    assert response.status_code == 502
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"  # Anthropic's catch-all, not "upstream_error"
    assert "error" not in body or "message" in body["error"]
    assert body["request_id"].startswith("req_")


# -- Streaming (Anthropic's typed events) -----------------------------------


async def test_a_stream_is_relayed_byte_for_byte(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages", json=_message("Hi", stream=True), headers={"x-api-key": "k"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == mock.SSE_BODY


async def test_the_stream_event_framing_survives(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/messages", json=_message("Hi", stream=True), headers={"x-api-key": "k"}
    )
    text = response.text
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text


async def test_a_broken_anthropic_stream_emits_an_anthropic_error_frame() -> None:
    """The mid-stream error frame must be Anthropic-shaped too."""
    events = list(mock.SSE_EVENTS[:2])

    class BreakingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for event in events:
                yield event
            raise httpx.ReadError("connection reset")

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BreakingStream()
        )

    provider = AnthropicProvider(base_url=UPSTREAM)
    async with AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await ProxyService(http).forward(
            provider=provider,
            method="POST",
            path="messages",
            query="",
            headers={"content-type": "application/json"},
            body=b'{"model":"claude-sonnet-5","stream":true,"messages":[]}',
        )
        assert isinstance(response, StreamingResponse)
        chunks = [chunk async for chunk in response.body_iterator]

    final = json.loads(chunks[-1].removeprefix(b"data: ").strip())  # type: ignore[union-attr]
    assert final["type"] == "error"
    assert final["error"]["type"] == "api_error"
