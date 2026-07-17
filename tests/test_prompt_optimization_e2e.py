"""Prompt optimization through the REAL proxy route — the path the benchmarks bypass.

The benchmarks (``zibbo benchmark``, ``python -m benchmarks.prompts``) drive
``pipeline.preview()``, which runs detection + selection + transform + cache but *skips*
``policy.decide``, the payload adapter, and segment extraction. So a green benchmark does
not prove the live path works. These tests drive an actual ``POST /anthropic/v1/messages``
end to end — proxy route -> adapter -> extraction -> pipeline -> analytics -> forwarding —
which is where prompt optimization is consumed in production.

The reproduction at the centre is the exact duplicated "Requirements" prompt from the bug
report: with the prompt optimizer on it must go through the ``prompt`` transformer, be
counted as optimized, and reach the upstream de-duplicated.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.api.deps import get_proxy_service, get_settings
from gateway.main import create_app
from gateway.providers import ProxyService
from tests.conftest import build_settings
from tests.mocks.anthropic_upstream import UpstreamRecorder, create_upstream_app

pytestmark = pytest.mark.integration

UPSTREAM = "http://upstream.test/v1"

# The exact prompt from the bug report: five identical "Requirements:" blocks.
DUPLICATED_REQUIREMENTS = "Build a FastAPI app.\n\n" + "\n\n".join(
    ["Requirements:\n- Use FastAPI\n- Use PostgreSQL\n- Use JWT"] * 5
)

# Duplicated "Requirements:" sections separated by different prose. The text transformer
# only removes *consecutive* duplicates, so it cannot touch these — only the prompt
# transformer can. Already whitespace-clean, so nothing else changes it.
NON_CONSECUTIVE = (
    "Build a FastAPI service for me.\n\n"
    "Requirements:\n- Use FastAPI\n- Use PostgreSQL\n- Use JWT\n\n"
    "Make it production ready with clean module boundaries and clear naming throughout.\n\n"
    "Requirements:\n- Use FastAPI\n- Use PostgreSQL\n- Use JWT\n\n"
    "Please include tests and a short README describing how to run everything locally.\n\n"
    "Requirements:\n- Use FastAPI\n- Use PostgreSQL\n- Use JWT"
)

NOISY_HTML = (
    "<!DOCTYPE html><html><head><title>Guide</title>"
    "<script>tracker.init()</script></head><body>"
    "<nav><a href='/'>Home</a></nav><main><h1>Install</h1><p>Run it.</p></main>"
    "<footer>Copyright</footer></body></html>"
)


@dataclass
class Gateway:
    http: AsyncClient
    upstream: UpstreamRecorder
    app: FastAPI

    async def send(self, content: Any, **extra: Any) -> Any:
        body = {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": content}]}
        body.update(extra)
        return await self.http.post("/anthropic/v1/messages", json=body)

    async def send_raw(self, raw: bytes) -> Any:
        return await self.http.post(
            "/anthropic/v1/messages", content=raw, headers={"content-type": "application/json"}
        )

    async def stats(self) -> dict[str, Any]:
        return (await self.http.get("/internal/stats")).json()["all_time"]  # type: ignore[no-any-return]

    async def last_event(self) -> dict[str, Any] | None:
        events = (await self.http.get("/internal/logs?limit=1")).json()["events"]
        return events[0] if events else None

    async def set_prompt(self, *, on: bool) -> dict[str, Any]:
        action = "enable" if on else "disable"
        return (await self.http.post(f"/internal/{action}?feature=prompt")).json()  # type: ignore[no-any-return]

    def upstream_content(self) -> str:
        return self.upstream.last.json()["messages"][0]["content"]  # type: ignore[no-any-return]


@asynccontextmanager
async def gateway(**overrides: Any) -> AsyncIterator[Gateway]:
    recorder = UpstreamRecorder()
    up = AsyncClient(
        transport=ASGITransport(app=create_upstream_app(recorder)),
        base_url="http://upstream.test",
    )
    settings = build_settings(anthropic_base_url=UPSTREAM, **overrides)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: ProxyService(up)
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://gw.test") as http:
                yield Gateway(http, recorder, app)
    finally:
        app.dependency_overrides.clear()
        await up.aclose()


# -- The reproduction: the exact duplicated "Requirements" prompt ------------


async def test_duplicated_requirements_prompt_is_prompt_optimized() -> None:
    """The bug report, end to end through the live proxy.

    PromptTransformer executes, Optimized == 1, Transformations == 1, tokens saved > 0,
    Top transformer == prompt — and the upstream receives the de-duplicated prompt.
    """
    async with gateway(prompt_optimization_enabled=True) as gw:
        resp = await gw.send(DUPLICATED_REQUIREMENTS)
        assert resp.status_code == 200
        assert resp.headers["x-zibbo-optimization"] == "applied"
        assert int(resp.headers["x-zibbo-tokens-saved"]) > 0

        stats = await gw.stats()
        assert stats["optimized"] == 1
        assert stats["transformations"] == 1
        assert stats["tokens_saved"] > 0
        assert stats["top_transformer"]["name"] == "prompt"

        # /zibbo:explain reads this event: it shows the prompt transformation and its steps.
        event = await gw.last_event()
        assert event is not None
        assert event["applied"] is True
        assert event["transformers"] == ["prompt"]
        assert event["content_types"] == ["prompt"]
        assert event["steps"], "explain must show which duplicate-removal steps ran"

        # The upstream actually received the de-duplicated prompt: five blocks folded to one.
        assert gw.upstream_content().count("Requirements:") == 1


# -- HTML still works through the proxy (nothing regressed) ------------------


async def test_html_optimization_still_works_through_the_proxy() -> None:
    async with gateway() as gw:  # prompt off by default; HTML is always on
        resp = await gw.send(NOISY_HTML)
        assert resp.headers["x-zibbo-optimization"] == "applied"

        stats = await gw.stats()
        assert stats["optimized"] == 1
        assert stats["top_transformer"]["name"] == "html"

        forwarded = gw.upstream_content()
        assert "tracker.init" not in forwarded
        assert "# Install" in forwarded


# -- Runtime enable/disable of the prompt optimizer --------------------------


async def test_prompt_optimization_toggles_at_runtime() -> None:
    """Non-consecutive duplicate sections: only the prompt optimizer can remove them, so
    the toggle's effect is visible in what the upstream receives — no restart."""
    async with gateway() as gw:  # boots with the prompt optimizer OFF
        # Off: the text transformer cannot remove non-consecutive duplicates.
        await gw.send(NON_CONSECUTIVE)
        assert gw.upstream_content().count("Requirements:") == 3

        toggled = await gw.set_prompt(on=True)
        assert toggled["prompt_optimization_enabled"] is True

        # On: the very next request is de-duplicated.
        await gw.send(NON_CONSECUTIVE)
        assert gw.upstream_content().count("Requirements:") == 1
        assert (await gw.stats())["top_transformer"]["name"] == "prompt"

        # Back off: duplicates survive again.
        await gw.set_prompt(on=False)
        await gw.send(NON_CONSECUTIVE)
        assert gw.upstream_content().count("Requirements:") == 3


# -- Observability: status reflects the live registry ------------------------


async def test_status_transformers_mirror_the_live_registry() -> None:
    """`/internal/status` (what `zibbo status`/`zibbo doctor` read) must report the actual
    registry, so enabling the prompt optimizer is visible without a restart. This is the
    guard against the observability gap: the reported list is the registry, not a constant."""
    async with gateway() as gw:  # prompt off by default
        status = (await gw.http.get("/internal/status")).json()
        assert status["transformers"] == list(gw.app.state.transformer_registry.names)
        assert "prompt" not in status["transformers"]
        assert status["prompt_optimization_enabled"] is False

        await gw.set_prompt(on=True)

        status = (await gw.http.get("/internal/status")).json()
        assert status["transformers"] == list(gw.app.state.transformer_registry.names)
        assert "prompt" in status["transformers"]
        assert status["prompt_optimization_enabled"] is True


# -- Cache correctness -------------------------------------------------------


async def test_a_repeated_prompt_is_served_from_cache() -> None:
    async with gateway(prompt_optimization_enabled=True, cache_enabled=True) as gw:
        first = await gw.send(DUPLICATED_REQUIREMENTS)
        assert first.headers.get("x-zibbo-cache") == "miss"
        second = await gw.send(DUPLICATED_REQUIREMENTS)
        assert second.headers.get("x-zibbo-cache") == "hit"
        # A cache hit still forwards the same de-duplicated prompt.
        assert gw.upstream_content().count("Requirements:") == 1


# -- Never-grow, enforced at the proxy boundary ------------------------------


async def test_never_grow_is_enforced_through_the_proxy() -> None:
    """A transformation that would grow the prompt is discarded and the original forwarded,
    even on the prompt path — the never-grow guarantee holds end to end."""
    async with gateway(prompt_optimization_enabled=True) as gw:

        class Inflater:
            name = "inflater"
            version = "1"
            priority = 0  # selected first
            content_types: frozenset[Any] = frozenset()

            def can_handle(self, content: str, detection: object) -> bool:
                return True

            def transform(self, content: str, detection: object) -> Any:
                from gateway.optimizers.models import TransformOutput

                return TransformOutput(content + (" pad" * 300), ("inflated",))

        gw.app.state.transformer_registry._transformers.insert(0, Inflater())

        raw = json.dumps(
            {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "hello world"}]}
        ).encode()
        resp = await gw.send_raw(raw)

        assert resp.status_code == 200
        assert gw.upstream.last.body == raw, "a grown transformation must forward the original"
        assert "pad pad" not in gw.upstream_content()


# -- The benchmark path and the live path agree ------------------------------


async def test_preview_and_live_transform_agree_on_the_prompt() -> None:
    """preview() (benchmark) and transform() (live) share the transform core, so their
    verdict on the same content must match — this is what the benchmark relies on."""
    async with gateway(prompt_optimization_enabled=True) as gw:
        pipeline = gw.app.state.pipeline
        preview = pipeline.preview(DUPLICATED_REQUIREMENTS)
        assert preview.detected_content_type.value == "prompt"
        assert preview.transformation_name == "prompt"

        await gw.send(DUPLICATED_REQUIREMENTS)
        event = await gw.last_event()
        assert event is not None
        assert event["transformers"] == ["prompt"]
