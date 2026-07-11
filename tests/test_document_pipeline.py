"""Embedded documents through the whole pipeline, and the safety guarantees.

The chain under test: a base64 document block in a chat request is detected by the
adapter, extracted by the service, and — only if that is cheaper — substituted into
the body. On any failure the block is left exactly as it arrived.
"""

from __future__ import annotations

import json
from typing import Any

from gateway.config import Settings
from gateway.documents import build_document_service
from gateway.optimizers import build_pipeline, build_provider_policy
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.models import SkipReason, TransformationRequest
from gateway.providers.anthropic import ANTHROPIC_ENDPOINTS
from gateway.providers.openai import OPENAI_ENDPOINTS
from gateway.providers.schemas import anthropic_adapters, openai_adapters
from gateway.tokenizers import TokenCounterFactory
from tests.conftest import build_settings
from tests.mocks.documents import make_docx, make_pdf, to_base64, to_data_uri


class _Pipeline:
    """Binds the provider-agnostic pipeline to a provider's policy and adapters."""

    def __init__(self, adapters: Any, endpoints: Any, **overrides: object) -> None:
        settings: Settings = build_settings(**overrides)
        self._documents = build_document_service(settings)
        self._pipeline = build_pipeline(
            settings,
            TokenCounterFactory.from_settings(settings),
            document_service=self._documents,
        )
        self._policy = build_provider_policy(settings, endpoints)
        self._adapters = AdapterRegistry(adapters)

    async def run(self, body: bytes, path: str) -> Any:
        return await self._pipeline.transform(
            TransformationRequest("POST", path, "application/json", body),
            policy=self._policy,
            adapters=self._adapters,
        )


def _openai(**overrides: object) -> _Pipeline:
    return _Pipeline(openai_adapters(), OPENAI_ENDPOINTS, **overrides)


def _anthropic(**overrides: object) -> _Pipeline:
    return _Pipeline(anthropic_adapters(), ANTHROPIC_ENDPOINTS, **overrides)


PDF = make_pdf([[("Quarterly Results", 22), ("Revenue rose sharply this quarter.", 11)]])


# -- OpenAI file blocks -----------------------------------------------------


async def test_an_openai_file_block_is_extracted_and_substituted() -> None:
    body = json.dumps(
        {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize."},
                        {
                            "type": "file",
                            "file": {
                                "filename": "q.pdf",
                                "file_data": to_data_uri(PDF, "application/pdf"),
                            },
                        },
                    ],
                }
            ],
        }
    ).encode()

    report = await _openai().run(body, "chat/completions")

    assert report.applied
    content = json.loads(report.body)["messages"][0]["content"]
    assert content[1]["type"] == "text"
    assert content[1]["text"].startswith("# Quarterly Results")
    assert "file" not in content[1]
    assert report.token_reduction_pct > 50


# -- Anthropic document blocks ----------------------------------------------


async def test_an_anthropic_document_block_is_extracted() -> None:
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read this."},
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": to_base64(PDF),
                            },
                        },
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic().run(body, "v1/messages")

    assert report.applied
    content = json.loads(report.body)["messages"][0]["content"]
    assert content[1]["type"] == "text"
    assert "Quarterly Results" in content[1]["text"]


async def test_a_docx_document_block_is_extracted() -> None:
    docx = make_docx([("Heading 1", "Memo"), ("normal", "Please review the attached.")])
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
                            "source": {"type": "base64", "media_type": "", "data": to_base64(docx)},
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic().run(body, "v1/messages")

    assert report.applied
    assert "# Memo" in json.loads(report.body)["messages"][0]["content"][0]["text"]


async def test_cache_control_survives_substitution() -> None:
    """Rewriting the block must not drop a prompt-caching directive beside it."""
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
                                "data": to_base64(PDF),
                            },
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic().run(body, "v1/messages")
    block = json.loads(report.body)["messages"][0]["content"][0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}


# -- Safety: failures forward the original ----------------------------------


async def test_a_corrupt_document_leaves_the_block_untouched() -> None:
    corrupt = to_base64(b"%PDF-1.4 not really a pdf at all, this is garbage")
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

    report = await _anthropic().run(body, "v1/messages")

    assert not report.applied
    assert report.body == body
    block = json.loads(report.body)["messages"][0]["content"][0]
    assert block["type"] == "document"  # untouched


async def test_a_url_source_document_is_left_alone() -> None:
    """Only inline base64 is extracted; a URL reference is forwarded as-is."""
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
                            "source": {"type": "url", "url": "https://example.test/report.pdf"},
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic().run(body, "v1/messages")
    assert report.body == body


async def test_document_extraction_can_be_disabled() -> None:
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
                                "data": to_base64(PDF),
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic(documents_enabled=False).run(body, "v1/messages")
    assert report.body == body


async def test_an_oversized_document_is_left_alone() -> None:
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
                                "data": to_base64(PDF),
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic(
        documents_max_decoded_bytes=100, optimization_max_body_bytes=10_000_000
    ).run(body, "v1/messages")
    assert not report.applied


async def test_a_disabled_format_is_left_alone() -> None:
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
                                "data": to_base64(PDF),
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _anthropic(documents_disabled_formats=["pdf"]).run(body, "v1/messages")
    assert report.body == body


# -- Idempotency ------------------------------------------------------------


async def test_extraction_is_idempotent() -> None:
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
                                "data": to_base64(PDF),
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    pipeline = _anthropic()
    first = await pipeline.run(body, "v1/messages")
    second = await pipeline.run(first.body, "v1/messages")

    assert first.applied
    assert not second.applied
    assert second.skip_reason is SkipReason.NOT_MODIFIED


async def test_an_image_block_is_never_treated_as_a_document() -> None:
    """A PNG in an image block is not a document and must pass through untouched."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    body = json.dumps(
        {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": to_data_uri(png, "image/png")},
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = await _openai().run(body, "chat/completions")
    assert report.body == body
