"""Optimization observed from outside the gateway.

What the upstream provider actually receives, and what the caller gets told about it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings
from tests.mocks.openai_upstream import UpstreamRecorder, create_upstream_app

pytestmark = pytest.mark.integration

UPSTREAM_BASE_URL = "http://upstream.test/v1"

NOISY_HTML = (
    "<!DOCTYPE html><html><head><title>Guide</title>"
    "<script>tracker.init()</script><style>.x{color:red}</style></head><body>"
    "<nav class='navbar'><a href='/'>Home</a></nav>"
    "<div class='cookie-consent'>Accept all cookies</div>"
    "<div class='ad-slot'>Buy now!</div>"
    "<main><h1>Installing</h1><p>Run   the   command.</p>"
    "<ul><li>One</li><li>Two</li></ul></main>"
    "<footer>Copyright 2026</footer></body></html>"
)


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
    app = _app(build_settings(openai_base_url=UPSTREAM_BASE_URL), upstream_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            yield http
    app.dependency_overrides.clear()


def _chat(content: str, **extra: object) -> dict[str, object]:
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}], **extra}


# -- What upstream receives ------------------------------------------------


async def test_upstream_receives_markdown_not_html(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=_chat(NOISY_HTML))

    content = upstream.last.json()["messages"][0]["content"]
    assert content == "# Installing\n\nRun the command.\n\n- One\n- Two"
    for noise in ("tracker.init", "color:red", "Accept all cookies", "Buy now!", "Copyright"):
        assert noise not in content


async def test_upstream_receives_a_smaller_body(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    original = json.dumps(_chat(NOISY_HTML)).encode()
    await client.post("/v1/chat/completions", json=_chat(NOISY_HTML))

    assert len(upstream.last.body) < len(original) // 2


async def test_content_length_matches_the_optimized_body(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    """Forwarding the original Content-Length after rewriting would corrupt the request."""
    await client.post("/v1/chat/completions", json=_chat(NOISY_HTML))

    assert int(upstream.last.headers["content-length"]) == len(upstream.last.body)


async def test_other_request_fields_are_preserved(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post("/v1/chat/completions", json=_chat(NOISY_HTML, temperature=0.3, stream=False))

    payload = upstream.last.json()
    assert payload["model"] == "gpt-4o-mini"
    assert payload["temperature"] == 0.3
    assert payload["messages"][0]["role"] == "user"


async def test_streaming_requests_are_optimized_too(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    response = await client.post("/v1/chat/completions", json=_chat(NOISY_HTML, stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert upstream.last.json()["stream"] is True
    assert "tracker.init" not in upstream.last.json()["messages"][0]["content"]


# -- Transparency preserved where it must be -------------------------------


async def test_an_already_clean_request_crosses_byte_for_byte(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    raw = b'{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hello."}]}'
    await client.post(
        "/v1/chat/completions", content=raw, headers={"Content-Type": "application/json"}
    )

    assert upstream.last.body == raw


async def test_file_uploads_are_never_touched(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    blob = bytes(range(256))
    await client.post(
        "/v1/files", content=blob, headers={"Content-Type": "application/octet-stream"}
    )

    assert upstream.last.body == blob


async def test_embeddings_are_not_optimized(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    body = json.dumps({"model": "text-embedding-3-small", "input": "  spaced  text  "}).encode()
    await client.post("/v1/embeddings", content=body, headers={"Content-Type": "application/json"})

    assert upstream.last.body == body


# -- What the caller is told -----------------------------------------------


async def test_optimization_headers_report_the_saving(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=_chat(NOISY_HTML))

    assert response.headers["x-llmgateway-optimization"] == "applied"
    assert int(response.headers["x-llmgateway-tokens-saved"]) > 0


async def test_skipped_requests_say_why(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json=_chat("Say hello."))

    assert response.headers["x-llmgateway-optimization"] == "skipped:content_already_optimal"


async def test_denied_endpoints_say_why(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/embeddings", content=b"{}", headers={"Content-Type": "application/json"}
    )

    assert response.headers["x-llmgateway-optimization"] == "skipped:endpoint_not_eligible"


# -- Behaviour under configuration -----------------------------------------


async def test_the_kill_switch_restores_pure_passthrough(
    upstream: UpstreamRecorder, upstream_client: AsyncClient
) -> None:
    settings = build_settings(openai_base_url=UPSTREAM_BASE_URL, optimization_enabled=False)
    app = _app(settings, upstream_client)
    body = json.dumps(_chat(NOISY_HTML)).encode()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            await http.post(
                "/v1/chat/completions", content=body, headers={"Content-Type": "application/json"}
            )

    assert upstream.last.body == body


async def test_large_bodies_are_offloaded_and_still_correct(
    upstream: UpstreamRecorder, upstream_client: AsyncClient
) -> None:
    """Above the threshold the work runs in a worker thread. Same answer, off the loop."""
    settings = build_settings(
        openai_base_url=UPSTREAM_BASE_URL, optimization_offload_threshold_bytes=1
    )
    app = _app(settings, upstream_client)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.post("/v1/chat/completions", json=_chat(NOISY_HTML))

    assert response.status_code == 200
    assert upstream.last.json()["messages"][0]["content"].startswith("# Installing")


async def test_repeated_request_headers_reach_upstream(
    client: AsyncClient, upstream: UpstreamRecorder
) -> None:
    await client.post(
        "/v1/chat/completions",
        json=_chat("hello"),
        headers=httpx.Headers([("accept", "application/json"), ("accept", "text/event-stream")]),
    )

    assert upstream.last.header_values("accept") == ["application/json", "text/event-stream"]
