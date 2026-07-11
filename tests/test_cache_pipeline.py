"""The transformation cache through the whole pipeline.

The behaviour that matters end to end: identical content is transformed once and reused;
a version or option change retires the cache; a failed extraction is never cached; and
concurrent identical requests are safe. Reuse is proven, not assumed — after the first
run the transformer is swapped for one that would produce different output, so a second
run that still returns the original output can only have come from the cache.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from gateway.cache import build_transformation_cache
from gateway.config import Settings
from gateway.documents import build_document_service
from gateway.optimizers import build_pipeline, build_provider_policy
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.models import TransformationRequest
from gateway.providers.anthropic import ANTHROPIC_ENDPOINTS
from gateway.providers.openai import OPENAI_ENDPOINTS
from gateway.providers.schemas import anthropic_adapters, openai_adapters
from gateway.tokenizers import TokenCounterFactory
from tests.conftest import build_settings
from tests.mocks.documents import make_pdf, to_base64

NOISY_HTML = (
    "<html><head><style>.x{color:red}</style><script>evil()</script></head>"
    "<body><nav>menu menu menu</nav><article><h1>Title</h1>"
    "<p>The first paragraph has real content worth keeping.</p>"
    "<p>A second paragraph, also meaningful and worth several tokens.</p>"
    "</article><footer>copyright boilerplate</footer></body></html>"
)

PDF = make_pdf([[("Quarterly Results", 22), ("Revenue rose sharply this quarter.", 11)]])


class _Harness:
    """A pipeline wired to a real cache, plus its provider policy and adapters."""

    def __init__(self, adapters: Any, endpoints: Any, **overrides: object) -> None:
        settings: Settings = build_settings(**overrides)
        self.cache = build_transformation_cache(settings)
        self.registry = None
        self.documents = build_document_service(settings)
        self.pipeline = build_pipeline(
            settings,
            TokenCounterFactory.from_settings(settings),
            document_service=self.documents,
            cache=self.cache,
        )
        self.registry = self.pipeline._registry
        self._policy = build_provider_policy(settings, endpoints)
        self._adapters = AdapterRegistry(adapters)

    async def run(self, body: bytes, path: str) -> Any:
        return await self.pipeline.transform(
            TransformationRequest("POST", path, "application/json", body),
            policy=self._policy,
            adapters=self._adapters,
        )


def _openai(**overrides: object) -> _Harness:
    return _Harness(openai_adapters(), OPENAI_ENDPOINTS, **overrides)


def _anthropic(**overrides: object) -> _Harness:
    return _Harness(anthropic_adapters(), ANTHROPIC_ENDPOINTS, **overrides)


def _chat(content: str) -> bytes:
    return json.dumps(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}]}
    ).encode()


def _pdf_chat() -> bytes:
    return json.dumps(
        {
            "model": "claude-sonnet-5",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": to_base64(PDF),
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


# -- Text: cold miss, warm hit ---------------------------------------------


async def test_second_identical_text_request_is_a_cache_hit() -> None:
    harness = _openai()
    body = _chat(NOISY_HTML)

    first = await harness.run(body, "chat/completions")
    assert first.applied
    assert first.cache_status == "miss"
    assert not first.results[0].cache_hit

    second = await harness.run(body, "chat/completions")
    assert second.applied
    assert second.cache_status == "hit"
    assert second.results[0].cache_hit
    assert second.body == first.body  # identical optimized output


async def test_a_hit_bypasses_the_transformer_entirely() -> None:
    """Sabotage every transformer to raise; a hit must still return the cached output.

    The registry *fingerprint* is left untouched (same names and versions), so the key
    is unchanged — only the transformers' behaviour is broken. A cache miss would now
    call the broken transformer; a hit skips it. The output proves which happened.
    """
    harness = _openai()
    body = _chat(NOISY_HTML)
    first = await harness.run(body, "chat/completions")

    def _boom(_content: str, _detection: object) -> object:
        raise AssertionError("transformer must not run on a cache hit")

    for transformer in harness.registry.transformers:
        transformer.transform = _boom  # type: ignore[method-assign]

    second = await harness.run(body, "chat/completions")
    assert second.cache_status == "hit"
    assert second.body == first.body  # came from cache, not the (now-broken) transformer


async def test_cache_stats_track_lookups() -> None:
    harness = _openai()
    body = _chat(NOISY_HTML)
    await harness.run(body, "chat/completions")
    await harness.run(body, "chat/completions")

    stats = harness.cache.stats()
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.stores == 1


# -- Documents: cold miss, warm hit ----------------------------------------


async def test_second_identical_document_request_is_a_cache_hit() -> None:
    harness = _anthropic()
    body = _pdf_chat()

    first = await harness.run(body, "v1/messages")
    assert first.applied
    assert first.cache_status == "miss"

    second = await harness.run(body, "v1/messages")
    assert second.cache_status == "hit"
    assert second.body == first.body
    assert second.results[0].cache_hit


async def test_a_document_hit_avoids_re_extraction() -> None:
    """After caching, disabling the document service must not change the hit's output."""
    harness = _anthropic()
    body = _pdf_chat()
    first = await harness.run(body, "v1/messages")

    # Force the service to refuse work; a miss now would leave the block untouched.
    harness.documents._options = harness.documents._options.__class__(
        enabled=True, max_decoded_bytes=1
    )
    second = await harness.run(body, "v1/messages")
    assert second.cache_status == "hit"
    assert second.body == first.body


# -- Safety: failures are never cached -------------------------------------


async def test_a_failed_extraction_is_not_cached() -> None:
    corrupt = to_base64(b"%PDF-1.4 this is not a real pdf, extraction will fail")
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": corrupt,
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    harness = _anthropic()
    await harness.run(body, "v1/messages")
    await harness.run(body, "v1/messages")

    # A failed extraction stores nothing, so the second request is another miss.
    assert harness.cache.stats().stores == 0


# -- Invalidation -----------------------------------------------------------


async def test_disabled_cache_never_hits() -> None:
    harness = _openai(cache_enabled=False)
    body = _chat(NOISY_HTML)
    first = await harness.run(body, "chat/completions")
    second = await harness.run(body, "chat/completions")

    assert first.applied
    assert second.applied
    assert first.cache_status == "miss"
    assert second.cache_status == "miss"  # no hit, ever
    assert harness.cache.stats().hits == 0


async def test_an_option_change_across_processes_is_a_miss() -> None:
    """Two gateways with different options must not share cached output."""
    body = _chat(NOISY_HTML)
    keep = _openai(html_preserve_links=True)
    drop = _openai(html_preserve_links=False)
    # Share one backing store, as two replicas on one Redis would.
    drop.cache._backend = keep.cache._backend

    await keep.run(body, "chat/completions")
    result = await drop.run(body, "chat/completions")
    assert result.cache_status == "miss"  # different options_fingerprint


# -- Concurrency ------------------------------------------------------------


async def test_concurrent_identical_requests_are_consistent() -> None:
    """Race many identical requests through the worker-thread path; all agree."""
    # A 1-byte offload threshold forces every non-empty body onto a worker thread,
    # exercising the cache from multiple threads at once.
    harness = _openai(optimization_offload_threshold_bytes=1)
    body = _chat(NOISY_HTML)

    reports = await asyncio.gather(*(harness.run(body, "chat/completions") for _ in range(24)))

    bodies = {bytes(report.body) for report in reports}
    assert len(bodies) == 1  # every worker produced identical output
    stats = harness.cache.stats()
    assert stats.hits + stats.misses == 24
    assert stats.misses >= 1  # at least the first was cold
