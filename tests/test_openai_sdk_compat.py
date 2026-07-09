"""Drive the gateway with the official OpenAI SDK.

Byte-level assertions prove the gateway relays the wire format. They do not prove
the *SDK* is happy — that depends on headers it inspects, status codes it maps to
exception types, and the SSE framing its stream parser expects.

So these tests construct a real ``AsyncOpenAI`` client whose only non-default
argument is ``base_url`` pointing at the gateway, exactly as the README instructs,
and then exercise it. If the drop-in promise is ever broken, this file fails.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import openai
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings
from tests.mocks.openai_upstream import (
    MODEL_BAD_REQUEST,
    MODEL_RATE_LIMITED,
    UpstreamRecorder,
    create_upstream_app,
)

pytestmark = [pytest.mark.integration, pytest.mark.compat]

UPSTREAM_BASE_URL = "http://upstream.test/v1"
MESSAGES = [{"role": "user", "content": "Say hello."}]


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
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(upstream_client)
    async with app.router.lifespan_context(app):
        yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def sdk(gateway_app: FastAPI) -> AsyncIterator[AsyncOpenAI]:
    """A stock SDK client. The only thing changed is the base URL."""
    transport = ASGITransport(app=gateway_app)
    async with AsyncClient(transport=transport) as http_client:
        yield AsyncOpenAI(
            api_key="sk-caller-key",
            base_url="http://gateway.test/v1",
            http_client=http_client,
            max_retries=0,
        )


# -- The happy paths the SDK must parse ------------------------------------


async def test_sdk_parses_a_chat_completion(sdk: AsyncOpenAI) -> None:
    completion = await sdk.chat.completions.create(model="gpt-4o-mini", messages=MESSAGES)  # type: ignore[arg-type]

    assert completion.id == "chatcmpl-abc123"
    assert completion.model == "gpt-4o-mini-2024-07-18"
    assert completion.choices[0].message.content == "Hello there."
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 12


async def test_sdk_consumes_a_stream(sdk: AsyncOpenAI) -> None:
    """Exercises the SDK's own SSE parser against our relayed frames."""
    deltas: list[str] = []
    stream = await sdk.chat.completions.create(
        model="gpt-4o-mini",
        messages=MESSAGES,  # type: ignore[arg-type]
        stream=True,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content
        if content:
            deltas.append(content)

    assert "".join(deltas) == "Hello there."


async def test_sdk_sees_the_final_finish_reason(sdk: AsyncOpenAI) -> None:
    finish_reasons = []
    stream = await sdk.chat.completions.create(
        model="gpt-4o-mini",
        messages=MESSAGES,  # type: ignore[arg-type]
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices[0].finish_reason:
            finish_reasons.append(chunk.choices[0].finish_reason)

    assert finish_reasons == ["stop"]


async def test_sdk_lists_models(sdk: AsyncOpenAI) -> None:
    models = await sdk.models.list()

    assert [model.id for model in models.data] == ["gpt-4o-mini"]


async def test_sdk_forwards_its_credential(sdk: AsyncOpenAI, upstream: UpstreamRecorder) -> None:
    await sdk.chat.completions.create(model="gpt-4o-mini", messages=MESSAGES)  # type: ignore[arg-type]

    assert upstream.last.headers["authorization"] == "Bearer sk-caller-key"


# -- The error paths the SDK must map to its own exception types -----------


async def test_sdk_raises_bad_request_error(sdk: AsyncOpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as caught:
        await sdk.chat.completions.create(model=MODEL_BAD_REQUEST, messages=MESSAGES)  # type: ignore[arg-type]

    assert caught.value.status_code == 400
    assert "temperature" in str(caught.value)


async def test_sdk_reads_the_upstream_request_id_from_an_error(sdk: AsyncOpenAI) -> None:
    """`APIError.request_id` is what a user pastes into a provider support ticket,
    so it has to be the provider's id, not the gateway's."""
    with pytest.raises(openai.BadRequestError) as caught:
        await sdk.chat.completions.create(model=MODEL_BAD_REQUEST, messages=MESSAGES)  # type: ignore[arg-type]

    assert caught.value.request_id == "upstream-req-9f8e7d"


async def test_sdk_raises_rate_limit_error(sdk: AsyncOpenAI) -> None:
    with pytest.raises(openai.RateLimitError) as caught:
        await sdk.chat.completions.create(model=MODEL_RATE_LIMITED, messages=MESSAGES)  # type: ignore[arg-type]

    assert caught.value.status_code == 429
    assert caught.value.response.headers["retry-after"] == "20"


async def test_sdk_raises_internal_server_error(sdk: AsyncOpenAI) -> None:
    with pytest.raises(openai.InternalServerError):
        await sdk.chat.completions.create(model="trigger-500", messages=MESSAGES)  # type: ignore[arg-type]


async def test_sdk_error_when_stream_is_rejected(sdk: AsyncOpenAI) -> None:
    """The SDK must get a JSON error, not a half-open event stream."""
    with pytest.raises(openai.BadRequestError):
        await sdk.chat.completions.create(
            model=MODEL_BAD_REQUEST,
            messages=MESSAGES,  # type: ignore[arg-type]
            stream=True,
        )


async def test_sdk_can_read_rate_limit_headers_on_success(sdk: AsyncOpenAI) -> None:
    """`with_raw_response` exposes headers; retry logic and cost dashboards use them."""
    raw = await sdk.chat.completions.with_raw_response.create(
        model="gpt-4o-mini",
        messages=MESSAGES,  # type: ignore[arg-type]
    )

    assert raw.headers["x-ratelimit-remaining-requests"] == "9999"
    assert raw.headers["openai-processing-ms"] == "243"
    assert raw.parse().id == "chatcmpl-abc123"
