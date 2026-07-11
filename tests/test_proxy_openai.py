"""The transparency contract.

Phase 2 makes exactly one promise: change your ``base_url`` and nothing else
changes. Every test here is a way that promise could break.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.responses import StreamingResponse

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import OpenAIProvider, ProxyService
from tests.conftest import build_settings
from tests.mocks.openai_upstream import (
    CHAT_COMPLETION_BODY,
    ERROR_BODY_400,
    ERROR_BODY_429,
    MODEL_BAD_REQUEST,
    MODEL_RATE_LIMITED,
    MODELS_BODY,
    SSE_BODY,
    SSE_CHUNKS,
    UpstreamRecorder,
    create_upstream_app,
)

pytestmark = pytest.mark.integration

UPSTREAM_BASE_URL = "http://upstream.test/v1"

CHAT_REQUEST = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say hello."}],
    "temperature": 0.7,
}


# -- Fixtures --------------------------------------------------------------


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
async def upstream_client(upstream: UpstreamRecorder) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_upstream_app(upstream))
    async with AsyncClient(transport=transport, base_url="http://upstream.test") as client:
        yield client


@pytest.fixture
def proxy_settings() -> Settings:
    return build_settings(openai_base_url=UPSTREAM_BASE_URL)


@pytest.fixture
async def gateway_app(
    proxy_settings: Settings, upstream_client: AsyncClient
) -> AsyncIterator[FastAPI]:
    app = create_app(proxy_settings)
    app.dependency_overrides[get_settings] = lambda: proxy_settings
    # Swap the real connection pool for one that speaks to the mock upstream, through
    # the dependency the route actually resolves rather than by poking app.state.
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    async with app.router.lifespan_context(app):
        yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(gateway_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=gateway_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as http_client:
        yield http_client


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer sk-caller-key"}


# -- Body transparency -----------------------------------------------------


async def test_response_body_is_byte_for_byte_identical(client: AsyncClient) -> None:
    """The gateway must not re-serialize JSON: key order and spacing are preserved."""
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert response.status_code == 200
    assert response.content == CHAT_COMPLETION_BODY


async def test_request_body_reaches_upstream_unchanged(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    raw = b'{"model": "gpt-4o-mini", "messages": [], "extra_unknown_field": 1}'
    await client.post(
        "/v1/chat/completions",
        content=raw,
        headers={**_auth(), "Content-Type": "application/json"},
    )

    assert upstream.last.body == raw


async def test_unknown_endpoints_are_proxied(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """A catch-all route means endpoints OpenAI ships tomorrow work today."""
    response = await client.post(
        "/v1/some/future/endpoint", json={"anything": True}, headers=_auth()
    )

    assert response.status_code == 200
    assert upstream.last.path == "/v1/some/future/endpoint"


async def test_query_parameters_survive(client: AsyncClient, upstream: UpstreamRecorder) -> None:
    response = await client.get("/v1/models?limit=2&after=gpt-4", headers=_auth())

    assert response.status_code == 200
    assert response.content == MODELS_BODY
    assert upstream.last.query == "limit=2&after=gpt-4"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
async def test_all_methods_are_proxied(
    client: AsyncClient, upstream: UpstreamRecorder, method: str
) -> None:
    response = await client.request(method, "/v1/files/file-abc", headers=_auth())

    assert response.status_code == 200
    assert upstream.last.method == method


async def test_binary_bodies_pass_through(client: AsyncClient, upstream: UpstreamRecorder) -> None:
    """File uploads are multipart, not JSON; stream detection must not choke."""
    blob = bytes(range(256))
    response = await client.post(
        "/v1/files",
        content=blob,
        headers={**_auth(), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 200
    assert upstream.last.body == blob


async def test_malformed_json_body_is_forwarded_not_rejected(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """The gateway does not validate payloads. Let the provider reject them."""
    await client.post(
        "/v1/chat/completions",
        content=b"{not valid json",
        headers={**_auth(), "Content-Type": "application/json"},
    )

    assert upstream.last.body == b"{not valid json"


# -- Authentication --------------------------------------------------------


async def test_caller_credential_is_forwarded_verbatim(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert upstream.last.headers["authorization"] == "Bearer sk-caller-key"


async def test_configured_key_is_injected_when_caller_sends_none(
    upstream: UpstreamRecorder, upstream_client: AsyncClient
) -> None:
    settings = build_settings(
        openai_base_url=UPSTREAM_BASE_URL, openai_api_key=SecretStr("sk-gateway-key")
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            await http.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert upstream.last.headers["authorization"] == "Bearer sk-gateway-key"


async def test_caller_credential_beats_the_configured_one(
    upstream: UpstreamRecorder, upstream_client: AsyncClient
) -> None:
    """Otherwise an app that switched base_url would silently bill our account."""
    settings = build_settings(
        openai_base_url=UPSTREAM_BASE_URL, openai_api_key=SecretStr("sk-gateway-key")
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            await http.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert upstream.last.headers["authorization"] == "Bearer sk-caller-key"


async def test_no_credential_anywhere_sends_no_authorization_header(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=CHAT_REQUEST)

    assert "authorization" not in upstream.last.headers


# -- Request headers -------------------------------------------------------


async def test_openai_specific_headers_are_forwarded(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={
            **_auth(),
            "OpenAI-Organization": "org-caller",
            "OpenAI-Project": "proj-caller",
            "OpenAI-Beta": "assistants=v2",
        },
    )

    assert upstream.last.headers["openai-organization"] == "org-caller"
    assert upstream.last.headers["openai-project"] == "proj-caller"
    assert upstream.last.headers["openai-beta"] == "assistants=v2"


@pytest.mark.parametrize("header", ["connection", "transfer-encoding", "proxy-authorization"])
async def test_hop_by_hop_headers_are_not_forwarded(
    client: AsyncClient, upstream: UpstreamRecorder, header: str
) -> None:
    """RFC 9110 §7.6.1. Relaying these onto a new connection is a protocol error."""
    await client.post(
        "/v1/chat/completions",
        json=CHAT_REQUEST,
        headers={**_auth(), header: "some-value"},
    )

    assert upstream.last.headers.get(header) != "some-value"


async def test_host_header_targets_the_provider_not_the_gateway(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert upstream.last.headers["host"] == "upstream.test"


async def test_content_length_matches_the_forwarded_body(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert int(upstream.last.headers["content-length"]) == len(upstream.last.body)


# -- Response headers ------------------------------------------------------


async def test_rate_limit_headers_reach_the_caller(client: AsyncClient) -> None:
    """SDKs read these to schedule retries. Dropping them breaks backoff."""
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert response.headers["x-ratelimit-remaining-requests"] == "9999"
    assert response.headers["x-ratelimit-limit-tokens"] == "30000"
    assert response.headers["x-ratelimit-reset-requests"] == "6ms"


async def test_openai_response_headers_reach_the_caller(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert response.headers["openai-organization"] == "org-testing"
    assert response.headers["openai-processing-ms"] == "243"
    assert response.headers["openai-version"] == "2020-10-01"


async def test_content_type_is_preserved(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert response.headers["content-type"] == "application/json"


async def test_upstream_request_id_wins_over_the_gateways(client: AsyncClient) -> None:
    """`x-request-id` is what an SDK surfaces in its errors, so it must be OpenAI's."""
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert response.headers["x-request-id"] == "upstream-req-9f8e7d"
    assert response.headers["x-zibbo-request-id"].startswith("req_")


async def test_gateway_request_id_is_present_on_non_proxied_routes(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.headers["x-request-id"] == response.headers["x-zibbo-request-id"]


async def test_content_length_is_recomputed_not_relayed(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert int(response.headers["content-length"]) == len(CHAT_COMPLETION_BODY)


async def test_no_duplicate_content_length_or_encoding(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert len(response.headers.get_list("content-length")) == 1
    assert "content-encoding" not in response.headers


# -- Upstream errors are relayed, not reinterpreted -------------------------


async def test_upstream_400_body_is_relayed_verbatim(client: AsyncClient) -> None:
    """OpenAI's error envelope is already the one its SDK parses. Do not rewrap it."""
    response = await client.post(
        "/v1/chat/completions",
        json={**CHAT_REQUEST, "model": MODEL_BAD_REQUEST},
        headers=_auth(),
    )

    assert response.status_code == 400
    assert response.content == ERROR_BODY_400


async def test_upstream_429_preserves_retry_signalling(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={**CHAT_REQUEST, "model": MODEL_RATE_LIMITED},
        headers=_auth(),
    )

    assert response.status_code == 429
    assert response.content == ERROR_BODY_429
    assert response.headers["retry-after"] == "20"
    assert response.headers["x-should-retry"] == "true"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


async def test_upstream_500_is_relayed_as_500(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={**CHAT_REQUEST, "model": "trigger-500"},
        headers=_auth(),
    )

    assert response.status_code == 500
    assert b"server had an error" in response.content


# -- Streaming -------------------------------------------------------------


async def test_streaming_body_is_byte_for_byte_identical(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}, headers=_auth()
    )

    assert response.status_code == 200
    assert response.content == SSE_BODY


async def test_streaming_response_declares_event_stream(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}, headers=_auth()
    )

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert "content-length" not in response.headers
    assert response.headers["x-accel-buffering"] == "no"


class _RecordingStream(httpx.AsyncByteStream):
    """An upstream body that reports how much of it has actually been produced."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.produced: list[bytes] = []

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.produced.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        return None


async def test_streaming_does_not_buffer_the_upstream_body() -> None:
    """The gateway must relay chunk N without having read chunk N+1.

    Asserted against `ProxyService` rather than through the ASGI client because
    httpx's ASGITransport concatenates response body parts before handing them
    back, which would mask buffering no matter how the gateway behaved.
    """
    chunks = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]
    upstream_body = _RecordingStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=upstream_body
        )

    async with AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await ProxyService(http).forward(
            provider=OpenAIProvider(base_url=UPSTREAM_BASE_URL),
            method="POST",
            path="chat/completions",
            query="",
            headers={"content-type": "application/json"},
            body=b'{"model": "gpt-4o-mini", "stream": true}',
        )

        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator

        first = await anext(iterator)  # type: ignore[arg-type]
        assert first == chunks[0]
        assert upstream_body.produced == [chunks[0]], "upstream was drained ahead of the consumer"

        remaining = [chunk async for chunk in iterator]

    assert [first, *remaining] == chunks


async def test_streaming_end_to_end_delivers_every_frame(client: AsyncClient) -> None:
    async with client.stream(
        "POST", "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}, headers=_auth()
    ) as response:
        received = b"".join([chunk async for chunk in response.aiter_raw()])

    assert received == SSE_BODY
    assert received.startswith(b"data: ")


async def test_streaming_preserves_sse_framing(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}, headers=_auth()
    )

    frames = [frame for frame in response.text.split("\n\n") if frame]
    assert len(frames) == len(SSE_CHUNKS)
    assert frames[-1] == "data: [DONE]"
    first = json.loads(frames[0].removeprefix("data: "))
    assert first["choices"][0]["delta"]["role"] == "assistant"


async def test_streaming_rate_limit_headers_survive(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_REQUEST, "stream": True}, headers=_auth()
    )

    assert response.headers["x-ratelimit-remaining-tokens"] == "29979"
    assert response.headers["x-request-id"] == "upstream-req-9f8e7d"


async def test_stream_rejected_upstream_returns_json_not_sse(client: AsyncClient) -> None:
    """A stream that fails before it opens must look like an ordinary error,
    because the SDK has not yet switched into SSE parsing mode."""
    response = await client.post(
        "/v1/chat/completions",
        json={**CHAT_REQUEST, "model": MODEL_BAD_REQUEST, "stream": True},
        headers=_auth(),
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.content == ERROR_BODY_400


async def test_stream_false_takes_the_buffered_path(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_REQUEST, "stream": False}, headers=_auth()
    )

    assert response.content == CHAT_COMPLETION_BODY
    assert response.headers["content-type"] == "application/json"


# -- Transport failures become gateway errors ------------------------------


async def _app_with_failing_upstream(exc: Exception) -> FastAPI:
    def raise_error(request: httpx.Request) -> httpx.Response:
        raise exc

    settings = build_settings(openai_base_url=UPSTREAM_BASE_URL)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    broken = AsyncClient(transport=httpx.MockTransport(raise_error))
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(broken)
    return app


@pytest.mark.parametrize("stream", [False, True])
async def test_connect_failure_becomes_502(stream: bool) -> None:
    app = await _app_with_failing_upstream(httpx.ConnectError("refused"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post(
                "/v1/chat/completions", json={**CHAT_REQUEST, "stream": stream}, headers=_auth()
            )

    error = response.json()["error"]
    assert response.status_code == 502
    assert error["type"] == "upstream_error"
    assert error["code"] == "upstream_error"
    assert error["request_id"].startswith("req_")


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_timeout_becomes_504(stream: bool) -> None:
    app = await _app_with_failing_upstream(httpx.ReadTimeout("too slow"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post(
                "/v1/chat/completions", json={**CHAT_REQUEST, "stream": stream}, headers=_auth()
            )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


async def test_transport_failure_does_not_leak_internals() -> None:
    app = await _app_with_failing_upstream(httpx.ConnectError("dial tcp 10.0.0.1:443"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post("/v1/chat/completions", json=CHAT_REQUEST, headers=_auth())

    assert "10.0.0.1" not in response.text
