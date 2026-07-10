"""Several providers under one gateway.

Route-prefix selection: the caller picks a provider by the URL its SDK points at.
These tests assert the prefixes route to the right upstream, that each provider keeps
its own auth and error shape, and that configuration decides which providers exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from gateway.api.deps import get_proxy_service
from gateway.config import Settings, get_settings
from gateway.main import build_providers, create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings

pytestmark = pytest.mark.integration


class _Router(httpx.AsyncBaseTransport):
    """Records which upstream host each forwarded request reached."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, dict[str, str]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(
            (str(request.url), request.method, {k.lower(): v for k, v in request.headers.items()})
        )
        return httpx.Response(200, json={"ok": True})

    @property
    def last_url(self) -> str:
        return self.seen[-1][0]

    @property
    def last_headers(self) -> dict[str, str]:
        return self.seen[-1][2]


def _multi_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "openai_enabled": True,
        "openai_base_url": "http://openai.test/v1",
        "openai_api_key": SecretStr("sk-openai"),
        "anthropic_enabled": True,
        "anthropic_base_url": "http://anthropic.test",
        "anthropic_api_key": SecretStr("sk-ant"),
        "groq_base_url": "http://groq.test/openai/v1",
        "groq_api_key": SecretStr("sk-groq"),
        "ollama_base_url": "http://localhost:11434/v1",
    }
    defaults.update(overrides)
    return build_settings(**defaults)


@pytest.fixture
def transport() -> _Router:
    return _Router()


@pytest.fixture
async def client(transport: _Router) -> AsyncIterator[AsyncClient]:
    settings = _multi_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(
        AsyncClient(transport=transport)
    )
    async with app.router.lifespan_context(app):
        asgi = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=asgi, base_url="http://gateway.test") as http:
            yield http
    app.dependency_overrides.clear()


# -- Every configured provider mounts --------------------------------------


def test_all_configured_providers_are_built() -> None:
    mounted = build_providers(_multi_settings())
    by_name = {entry.provider.name: entry.prefix for entry in mounted}

    assert by_name == {
        "openai": "/v1",
        "anthropic": "/anthropic",
        "groq": "/groq/v1",
        "ollama": "/ollama/v1",
    }


def test_a_compatible_provider_is_absent_without_a_base_url() -> None:
    names = {entry.provider.name for entry in build_providers(_multi_settings(groq_base_url=None))}
    assert "groq" not in names
    assert "openai" in names and "anthropic" in names


def test_a_disabled_core_provider_is_absent() -> None:
    names = {
        entry.provider.name for entry in build_providers(_multi_settings(openai_enabled=False))
    }
    assert "openai" not in names
    assert "anthropic" in names


# -- The prefix selects the upstream ----------------------------------------


@pytest.mark.parametrize(
    ("prefix", "path", "expected_host"),
    [
        ("/v1", "chat/completions", "openai.test"),
        ("/anthropic", "v1/messages", "anthropic.test"),
        ("/groq/v1", "chat/completions", "groq.test"),
        ("/ollama/v1", "chat/completions", "localhost"),
    ],
)
async def test_the_prefix_routes_to_the_right_upstream(
    client: AsyncClient, transport: _Router, prefix: str, path: str, expected_host: str
) -> None:
    await client.post(f"{prefix}/{path}", json={"model": "m", "messages": []})
    assert httpx.URL(transport.last_url).host == expected_host


async def test_each_provider_authenticates_in_its_own_way(
    client: AsyncClient, transport: _Router
) -> None:
    await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert transport.last_headers["authorization"] == "Bearer sk-openai"

    await client.post("/anthropic/v1/messages", json={"model": "m", "messages": []})
    assert transport.last_headers["x-api-key"] == "sk-ant"
    assert "authorization" not in transport.last_headers
    assert httpx.URL(transport.last_url).path == "/v1/messages"

    await client.post("/groq/v1/chat/completions", json={"model": "m", "messages": []})
    assert transport.last_headers["authorization"] == "Bearer sk-groq"


async def test_ollama_sends_no_credential_when_none_configured(
    client: AsyncClient, transport: _Router
) -> None:
    await client.post("/ollama/v1/chat/completions", json={"model": "m", "messages": []})
    assert "authorization" not in transport.last_headers


async def test_a_caller_key_is_forwarded_to_a_compatible_provider(
    client: AsyncClient, transport: _Router
) -> None:
    await client.post(
        "/ollama/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer caller-key"},
    )
    assert transport.last_headers["authorization"] == "Bearer caller-key"


# -- Configuration guards ---------------------------------------------------


def test_two_providers_cannot_share_a_prefix() -> None:
    with pytest.raises(ValidationError, match="both mount at"):
        build_settings(anthropic_prefix="/v1")  # collides with openai's /v1


def test_provider_registry_lists_every_provider(client: AsyncClient) -> None:
    app: FastAPI = client._transport.app  # type: ignore[union-attr, attr-defined]
    assert set(app.state.providers.names) == {"openai", "anthropic", "groq", "ollama"}
