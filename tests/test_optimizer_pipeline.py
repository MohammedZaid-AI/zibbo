"""The pipeline: policy, extraction, detection, transformation, reporting."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway.config import Settings
from gateway.optimizers import build_pipeline, build_provider_policy
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.models import SkipReason, TransformationRequest
from gateway.providers.openai import OPENAI_ENDPOINTS
from gateway.providers.schemas import (
    AnthropicMessagesAdapter,
    ChatCompletionsAdapter,
    OpenAIAssistantsAdapter,
    ResponsesAdapter,
    openai_adapters,
)
from gateway.tokenizers import HeuristicTokenCounter, TokenCounterFactory
from tests.conftest import build_settings

HTML_SNIPPET = (
    "<html><head><title>T</title><script>x()</script></head>"
    "<body><nav>Home</nav><h1>Title</h1><p>Body   text</p></body></html>"
)


class _BoundPipeline:
    """A pipeline bound to a provider's policy and adapters, so tests keep calling
    ``.transform(request)`` after the pipeline became provider-agnostic."""

    def __init__(self, settings: Settings) -> None:
        self._pipeline = build_pipeline(settings, TokenCounterFactory.from_settings(settings))
        self._policy = build_provider_policy(settings, OPENAI_ENDPOINTS)
        self._adapters = AdapterRegistry(openai_adapters())

    @property
    def registry(self) -> Any:
        return self._pipeline._registry

    async def transform(self, request: TransformationRequest) -> Any:
        return await self._pipeline.transform(request, policy=self._policy, adapters=self._adapters)


def _pipeline(**overrides: object) -> _BoundPipeline:
    return _BoundPipeline(build_settings(**overrides))


def _chat_body(content: Any, **extra: Any) -> bytes:
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}], **extra}
    return json.dumps(payload).encode()


def _request(body: bytes, path: str = "chat/completions") -> TransformationRequest:
    return TransformationRequest("POST", path, "application/json", body)


# -- The happy path --------------------------------------------------------


async def test_html_in_a_chat_message_is_converted_to_markdown() -> None:
    report = await _pipeline().transform(_request(_chat_body(HTML_SNIPPET)))

    assert report.applied
    content = json.loads(report.body)["messages"][0]["content"]
    assert content == "# Title\n\nBody text"
    assert "script" not in content


async def test_the_report_measures_everything() -> None:
    report = await _pipeline().transform(_request(_chat_body(HTML_SNIPPET)))

    assert report.tokens_saved > 0
    assert report.bytes_saved > 0
    assert 0 < report.token_reduction_pct <= 100
    assert report.execution_time_ms >= 0
    assert report.transformers_used == ("html",)
    assert report.content_types_detected == ("html",)

    (result,) = report.results
    assert result.origin == "messages[0].content"
    assert result.original_token_count > result.transformed_token_count
    assert result.tokens_saved == result.original_token_count - result.transformed_token_count
    assert "converted_to_markdown" in result.transformations_applied


async def test_json_pasted_into_a_message_is_minified() -> None:
    pretty = json.dumps({"users": [{"id": 1, "name": "Ada"}]}, indent=4)
    report = await _pipeline().transform(_request(_chat_body(pretty)))

    assert report.applied
    assert json.loads(report.body)["messages"][0]["content"] == '{"users":[{"id":1,"name":"Ada"}]}'


async def test_prose_is_normalized_not_rewritten() -> None:
    report = await _pipeline().transform(_request(_chat_body("Hello   \n\n\n\nworld")))

    assert report.applied
    assert json.loads(report.body)["messages"][0]["content"] == "Hello\n\nworld"


async def test_each_message_is_detected_independently() -> None:
    body = json.dumps(
        {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": HTML_SNIPPET},
                {"role": "user", "content": '{"a":   1}'},
            ],
        }
    ).encode()

    report = await _pipeline().transform(_request(body))

    assert report.applied
    assert set(report.content_types_detected) == {"text", "html", "json"}


# -- Transparency: unchanged means untouched -------------------------------


async def test_an_unchanged_body_is_forwarded_byte_for_byte() -> None:
    """The Phase 2 guarantee survives. No re-serialization of an optimal request."""
    body = _chat_body("Say hello.")
    report = await _pipeline().transform(_request(body))

    assert not report.applied
    assert report.skip_reason is SkipReason.NOT_MODIFIED
    assert report.body is body


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (_request(b"", "chat/completions"), SkipReason.EMPTY_BODY),
        (_request(_chat_body("x"), "files"), SkipReason.ENDPOINT_NOT_ELIGIBLE),
        (_request(b"{not json"), SkipReason.MALFORMED_PAYLOAD),
        (_request(b"[1,2,3]"), SkipReason.MALFORMED_PAYLOAD),
        (_request(json.dumps({"model": "m"}).encode()), SkipReason.NO_SEGMENTS),
        (_request(json.dumps({"messages": []}).encode()), SkipReason.NO_SEGMENTS),
    ],
)
async def test_skips_forward_the_original_body(
    request_: TransformationRequest, reason: SkipReason
) -> None:
    report = await _pipeline().transform(request_)

    assert not report.applied
    assert report.skip_reason is reason
    assert report.body == request_.body


async def test_an_allowed_path_with_no_adapter_is_skipped() -> None:
    """`assistants` is allowed by policy, but a path with no adapter cannot be walked."""
    pipeline = _pipeline()
    report = await pipeline.transform(_request(_chat_body("x"), "threads/abc/runs"))
    assert report.skip_reason in (SkipReason.NO_SEGMENTS, SkipReason.NOT_MODIFIED)


async def test_the_kill_switch_disables_everything() -> None:
    report = await _pipeline(optimization_enabled=False).transform(
        _request(_chat_body(HTML_SNIPPET))
    )

    assert not report.applied
    assert report.skip_reason is SkipReason.DISABLED
    assert report.body == _chat_body(HTML_SNIPPET)


# -- Idempotency -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        HTML_SNIPPET,
        json.dumps({"a": [1, 2], "b": {"c": None}}, indent=2),
        "text   with\r\n\r\n\r\n\r\nnoise",
        "same\n\nsame\n\ndifferent",
    ],
)
async def test_running_the_pipeline_twice_changes_nothing_the_second_time(content: str) -> None:
    pipeline = _pipeline()
    first = await pipeline.transform(_request(_chat_body(content)))
    second = await pipeline.transform(_request(first.body))

    assert second.body == first.body
    assert not second.applied
    assert second.skip_reason is SkipReason.NOT_MODIFIED


# -- Multimodal and structured content -------------------------------------


async def test_text_parts_are_optimized_and_image_parts_are_not() -> None:
    parts = [
        {"type": "text", "text": HTML_SNIPPET},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    report = await _pipeline().transform(_request(_chat_body(parts)))

    content = json.loads(report.body)["messages"][0]["content"]
    assert content[0]["text"] == "# Title\n\nBody text"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


async def test_tool_calls_and_other_fields_are_untouched() -> None:
    body = json.dumps(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "  spaced  "}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "temperature": 0.7,
            "stream": True,
        }
    ).encode()

    report = await _pipeline().transform(_request(body))
    payload = json.loads(report.body)

    assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert payload["temperature"] == 0.7
    assert payload["stream"] is True


async def test_a_transformation_that_grows_the_content_is_reverted() -> None:
    """Optimization must never cost more than it saves.

    A transformer can legitimately grow its input — Markdown table syntax outweighs
    CSV commas on a narrow table. The pipeline discards such a result rather than
    forwarding a bigger prompt than it received.
    """
    pipeline = _pipeline()

    class Inflater:
        name = "inflater"
        priority = 0

        def can_handle(self, content: str, detection: object) -> bool:
            return True

        def transform(self, content: str, detection: object) -> Any:
            from gateway.optimizers.models import TransformOutput

            return TransformOutput(content + " padding words added here", ("inflated",))

    pipeline.registry._transformers.insert(0, Inflater())  # type: ignore[attr-defined, arg-type]

    body = _chat_body("hello")
    report = await pipeline.transform(_request(body))

    assert not report.applied
    assert report.skip_reason is SkipReason.NOT_MODIFIED
    assert report.body is body, "the original bytes must be forwarded"


async def test_a_transformation_that_keeps_the_token_count_still_applies() -> None:
    """Only a strict increase is a regression; equal tokens with fewer bytes is a win."""
    report = await _pipeline().transform(_request(_chat_body("trailing   \n\n\n\nspace")))

    assert report.applied
    assert report.tokens_saved >= 0
    assert report.bytes_saved > 0


async def test_a_broken_transformer_does_not_break_the_request() -> None:
    pipeline = _pipeline()

    class Exploding:
        name = "boom"
        priority = 0

        def can_handle(self, content: str, detection: object) -> bool:
            return True

        def transform(self, content: str, detection: object) -> object:
            raise RuntimeError("kaboom")

    pipeline.registry._transformers.insert(0, Exploding())  # type: ignore[attr-defined, arg-type]

    body = _chat_body(HTML_SNIPPET)
    report = await pipeline.transform(_request(body))

    assert not report.applied
    assert report.body == body


# -- Extraction adapters ---------------------------------------------------


def test_chat_adapter_finds_string_and_part_content() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "a"},
            {"role": "user", "content": [{"type": "text", "text": "b"}]},
            {"role": "assistant", "content": None},
            "not-a-dict",
        ]
    }
    segments = list(ChatCompletionsAdapter().extract(payload))  # type: ignore[arg-type]
    assert [segment.text for segment in segments] == ["a", "b"]
    assert [segment.origin for segment in segments] == [
        "messages[0].content",
        "messages[1].content[0].text",
    ]


def test_segments_write_back_in_place() -> None:
    payload = {"messages": [{"role": "user", "content": "a"}]}
    (segment,) = list(ChatCompletionsAdapter().extract(payload))  # type: ignore[arg-type]
    segment.replace("b")
    assert payload["messages"][0]["content"] == "b"  # type: ignore[index]


def test_responses_adapter_handles_string_and_list_input() -> None:
    payload = {"instructions": "sys", "input": "hello"}
    assert [s.text for s in ResponsesAdapter().extract(payload)] == ["sys", "hello"]

    payload = {"input": [{"content": [{"type": "input_text", "text": "x"}]}]}
    assert [s.text for s in ResponsesAdapter().extract(payload)] == ["x"]


def test_assistants_adapter_finds_instructions() -> None:
    payload = {"instructions": "a", "additional_instructions": "b"}
    assert [s.text for s in OpenAIAssistantsAdapter().extract(payload)] == ["a", "b"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("chat/completions", "openai.chat.completions"),
        ("/chat/completions", "openai.chat.completions"),
        ("responses", "openai.responses"),
        ("assistants", "openai.assistants"),
        ("threads/x/messages", "openai.assistants"),
        ("embeddings", None),
    ],
)
def test_openai_adapter_routing(path: str, expected: str | None) -> None:
    adapter = AdapterRegistry(openai_adapters()).for_path(path)
    assert (adapter.name if adapter else None) == expected


def test_anthropic_adapter_extracts_system_and_messages() -> None:
    """The distinguishing shape: a top-level `system` string, plus content blocks."""
    payload = {
        "system": "You are a careful assistant.",
        "messages": [
            {"role": "user", "content": "plain string"},
            {"role": "user", "content": [{"type": "text", "text": "block text"}]},
        ],
    }
    texts = [s.text for s in AnthropicMessagesAdapter().extract(payload)]
    assert texts == ["You are a careful assistant.", "plain string", "block text"]


def test_anthropic_adapter_handles_system_as_a_block_list() -> None:
    payload = {"system": [{"type": "text", "text": "cached system"}], "messages": []}
    assert [s.text for s in AnthropicMessagesAdapter().extract(payload)] == ["cached system"]


# -- Token counting --------------------------------------------------------


def test_heuristic_counter_is_deterministic_and_monotonic() -> None:
    counter = HeuristicTokenCounter()
    assert counter.count("") == 0
    assert counter.count("hello") == counter.count("hello")
    assert counter.count("hello world") > counter.count("hello")
    assert counter.exact is False


def test_factory_falls_back_when_tiktoken_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CDN outage must degrade accuracy, never fail a request."""
    import gateway.tokenizers.registry as registry_module

    def boom(name: str) -> None:
        raise registry_module.TiktokenUnavailableError(name)

    monkeypatch.setattr(registry_module, "TiktokenCounter", boom)
    counter = TokenCounterFactory().for_model("gpt-4o-mini")

    assert counter.name == "heuristic"
    assert counter.count("hello world") > 0
