"""Drive the gateway with the official Anthropic SDK.

The mirror of ``test_openai_sdk_compat.py``, for the second provider. Byte assertions
prove the wire format; this proves the *Anthropic SDK* is happy — its own event-stream
parser, its own error classes, its own header handling — when its only non-default
argument is ``base_url`` pointing at the gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import anthropic
import pytest
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings
from tests.mocks.anthropic_upstream import (
    MODEL_BAD_REQUEST,
    MODEL_RATE_LIMITED,
    UpstreamRecorder,
    create_upstream_app,
)

pytestmark = [pytest.mark.integration, pytest.mark.compat]

# The SDK adds `/v1`; the gateway prefix is `/anthropic` and the upstream is the origin.
UPSTREAM = "http://anthropic.test"
MESSAGES = [{"role": "user", "content": "Say hello."}]


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
async def upstream_client(upstream: UpstreamRecorder) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_upstream_app(upstream))
    async with AsyncClient(transport=transport, base_url="http://anthropic.test") as client:
        yield client


@pytest.fixture
def proxy_settings() -> Settings:
    return build_settings(openai_enabled=False, anthropic_enabled=True, anthropic_base_url=UPSTREAM)


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
async def sdk(gateway_app: FastAPI) -> AsyncIterator[AsyncAnthropic]:
    """A stock Anthropic client. The only change is the base URL."""
    transport = ASGITransport(app=gateway_app)
    async with AsyncClient(transport=transport) as http_client:
        yield AsyncAnthropic(
            api_key="sk-ant-caller",
            base_url="http://gateway.test/anthropic",
            http_client=http_client,
            max_retries=0,
        )


# -- Happy paths ------------------------------------------------------------


async def test_sdk_parses_a_message(sdk: AsyncAnthropic) -> None:
    message = await sdk.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=MESSAGES,  # type: ignore[arg-type]
    )
    assert message.id == "msg_bench01"
    assert message.role == "assistant"
    assert message.content[0].text == "Hello there."  # type: ignore[union-attr]
    assert message.stop_reason == "end_turn"
    assert message.usage.input_tokens == 12


async def test_sdk_consumes_a_stream(sdk: AsyncAnthropic) -> None:
    """Exercises the Anthropic SDK's own event-stream parser against relayed events."""
    text = ""
    async with sdk.messages.stream(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=MESSAGES,  # type: ignore[arg-type]
    ) as stream:
        async for event in stream.text_stream:
            text += event
    assert text == "Hello there."


async def test_sdk_gets_the_final_message_from_a_stream(sdk: AsyncAnthropic) -> None:
    async with sdk.messages.stream(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=MESSAGES,  # type: ignore[arg-type]
    ) as stream:
        final = await stream.get_final_message()
    assert final.content[0].text == "Hello there."  # type: ignore[union-attr]
    assert final.stop_reason == "end_turn"


async def test_sdk_forwards_its_credential(sdk: AsyncAnthropic, upstream: UpstreamRecorder) -> None:
    await sdk.messages.create(model="claude-sonnet-5", max_tokens=8, messages=MESSAGES)  # type: ignore[arg-type]
    assert upstream.last.headers["x-api-key"] == "sk-ant-caller"


async def test_sdk_sends_the_system_prompt(sdk: AsyncAnthropic, upstream: UpstreamRecorder) -> None:
    await sdk.messages.create(
        model="claude-sonnet-5",
        max_tokens=8,
        system="You are terse.",
        messages=MESSAGES,  # type: ignore[arg-type]
    )
    assert upstream.last.json()["system"] == "You are terse."


# -- Error classes ----------------------------------------------------------


async def test_sdk_raises_bad_request(sdk: AsyncAnthropic) -> None:
    with pytest.raises(anthropic.BadRequestError) as caught:
        await sdk.messages.create(model=MODEL_BAD_REQUEST, max_tokens=8, messages=MESSAGES)  # type: ignore[arg-type]
    assert caught.value.status_code == 400
    assert "max_tokens" in str(caught.value)


async def test_sdk_raises_rate_limit(sdk: AsyncAnthropic) -> None:
    with pytest.raises(anthropic.RateLimitError) as caught:
        await sdk.messages.create(model=MODEL_RATE_LIMITED, max_tokens=8, messages=MESSAGES)  # type: ignore[arg-type]
    assert caught.value.status_code == 429


async def test_sdk_can_read_rate_limit_headers(sdk: AsyncAnthropic) -> None:
    response = await sdk.messages.with_raw_response.create(
        model="claude-sonnet-5",
        max_tokens=8,
        messages=MESSAGES,  # type: ignore[arg-type]
    )
    assert response.headers["anthropic-ratelimit-requests-remaining"] == "4999"
    assert response.parse().id == "msg_bench01"
