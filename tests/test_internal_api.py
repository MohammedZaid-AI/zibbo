"""The /internal/* control API and its loopback guard."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.config import get_settings
from gateway.main import create_app
from tests.conftest import build_settings


async def _client(settings, *, client_addr: tuple[str, int]) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    # Routes resolve settings through DI, not the module cache — mirror the app fixture.
    app.dependency_overrides[get_settings] = lambda: settings
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False, client=client_addr)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            yield http


@pytest.mark.integration
async def test_status_reports_the_deployment(client: AsyncClient) -> None:
    body = (await client.get("/internal/status")).json()
    assert body["name"] == "zibbo"
    assert body["optimization_enabled"] is True
    assert body["pipeline_active"] is True
    assert "cache_backend" in body
    assert isinstance(body["providers"], list)
    assert any(p["name"] == "openai" for p in body["providers"])


@pytest.mark.integration
async def test_version_and_stats_and_logs(client: AsyncClient) -> None:
    version = (await client.get("/internal/version")).json()
    assert version["app_name"] == "zibbo"
    assert version["internal_api_version"] == "1"

    stats = (await client.get("/internal/stats")).json()
    assert stats["today"]["requests"] == 0
    assert stats["all_time"]["tokens_saved"] == 0

    logs = (await client.get("/internal/logs")).json()
    assert logs["count"] == 0
    assert logs["events"] == []


@pytest.mark.integration
async def test_enable_disable_roundtrip(client: AsyncClient) -> None:
    assert (await client.post("/internal/disable")).json() == {"optimization_enabled": False}
    assert (await client.get("/internal/status")).json()["optimization_enabled"] is False
    assert (await client.post("/internal/enable")).json() == {"optimization_enabled": True}
    assert (await client.get("/internal/status")).json()["optimization_enabled"] is True


@pytest.mark.integration
async def test_benchmark_provided_content_runs_the_pipeline(client: AsyncClient) -> None:
    html = "<html><body><h1>Title</h1><script>junk()</script><p>Real text.</p></body></html>"
    body = (await client.post("/internal/benchmark", json={"content": html})).json()
    assert body["source"] == "provided"
    assert body["content_type"] == "html"
    assert body["original_tokens"] > body["optimized_tokens"]
    assert body["reduction_pct"] > 0
    assert "converted_to_markdown" in body["transformers"]


@pytest.mark.integration
async def test_benchmark_with_no_prior_request_says_so(client: AsyncClient) -> None:
    body = (await client.post("/internal/benchmark", json={})).json()
    assert body["source"] == "none"
    assert body["note"] is not None


@pytest.mark.integration
async def test_doctor_reports_healthy(client: AsyncClient) -> None:
    body = (await client.post("/internal/doctor")).json()
    assert body["healthy"] is True
    names = {check["name"] for check in body["checks"]}
    assert {"gateway", "optimization", "cache", "providers"} <= names


@pytest.mark.integration
async def test_doctor_warns_when_optimization_is_off(client: AsyncClient) -> None:
    await client.post("/internal/disable")
    body = (await client.post("/internal/doctor")).json()
    opt = next(check for check in body["checks"] if check["name"] == "optimization")
    assert opt["status"] == "warn"
    assert opt["fix"] is not None


@pytest.mark.integration
async def test_claude_activation_status(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the environment so the classification is deterministic regardless of the runner.
    for higher in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        monkeypatch.delenv(higher, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETVALUE-should-never-leak")
    response = await client.get("/internal/claude")
    body = response.json()
    assert body["gateway_running"] is True
    assert body["internal_api_version"] == "1"
    assert body["routing_observed"] is False  # no Anthropic traffic in this test run
    assert body["anthropic_requests_observed"] == 0
    assert body["authentication"]["method"] == "api_key"
    assert body["authentication"]["present"] is True
    # Nothing observed on the wire yet.
    assert body["observed_authentication"]["present"] is False
    # The classification leaves; the credential never does.
    assert "SECRETVALUE" not in response.text


@pytest.mark.integration
async def test_claude_observes_authenticated_anthropic_traffic(
    client: AsyncClient, app: FastAPI
) -> None:
    """Reality over intent: once authenticated Anthropic traffic is recorded, the endpoint
    reports it as observed — independent of any environment variable."""
    from gateway.analytics.models import OptimizationEvent

    app.state.analytics.record(
        OptimizationEvent(
            timestamp=0.0,
            provider="anthropic",
            endpoint="v1/messages",
            applied=False,
            skip_reason="content_already_optimal",
            content_types=(),
            transformers=(),
            tokens_before=0,
            tokens_after=0,
            bytes_before=0,
            bytes_after=0,
            cache_hits=0,
            cache_lookups=0,
            execution_time_ms=0.0,
            auth_method="oauth_token",
        )
    )
    body = (await client.get("/internal/claude")).json()
    assert body["anthropic_requests_observed"] >= 1
    assert body["observed_authentication"]["present"] is True
    assert body["observed_authentication"]["method"] == "oauth_token"
    # routing_observed is true whenever the Anthropic route is mounted and traffic seen.
    assert body["routing_observed"] is (body["anthropic_route"] is not None)


# -- The loopback guard ------------------------------------------------------


@pytest.mark.integration
async def test_remote_client_gets_404_by_default() -> None:
    settings = build_settings()
    async for http in _client(settings, client_addr=("203.0.113.7", 4444)):
        assert (await http.get("/internal/status")).status_code == 404
        # A normal proxied route is unaffected by the internal guard.
        assert (await http.get("/health/live")).status_code == 200


@pytest.mark.integration
async def test_remote_allowed_requires_a_token() -> None:
    settings = build_settings(internal_api_allow_remote=True, internal_api_token="s3cret")
    async for http in _client(settings, client_addr=("203.0.113.7", 4444)):
        assert (await http.get("/internal/status")).status_code == 401
        ok = await http.get("/internal/status", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        bad = await http.get("/internal/status", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401


@pytest.mark.integration
async def test_remote_allowed_without_token_configured_is_misconfig() -> None:
    settings = build_settings(internal_api_allow_remote=True)
    async for http in _client(settings, client_addr=("203.0.113.7", 4444)):
        assert (await http.get("/internal/status")).status_code == 503
