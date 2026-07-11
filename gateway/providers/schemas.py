"""Request-body schemas: where the optimizable text lives, per provider.

Each adapter knows the shape of one endpoint's request. This is the only place in
the codebase that knows OpenAI puts message text under ``messages[i].content`` and
Anthropic puts it under ``messages[i].content[j].text`` with a top-level ``system``
string. The pipeline consumes ``Segment``s and never learns any of it.

The rule every adapter obeys: yield **user-authored prose**, and nothing else.
Never tool-call arguments (machine-generated, and a caller may parse them
byte-exactly), never image or audio parts, never identifiers. Optimizing the wrong
field is how a gateway silently corrupts a request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from gateway.optimizers.extraction import (
    DocumentSegment,
    PayloadAdapter,
    Segment,
    document_parts,
    text_parts,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


class ChatCompletionsAdapter(PayloadAdapter):
    """``POST /chat/completions`` — OpenAI, Groq, Mistral, Ollama, and every
    OpenAI-compatible provider.

    ``content`` is either a string or an array of typed parts. Tool call arguments
    and function results are left alone: they are machine-generated.
    """

    name: ClassVar[str] = "openai.chat.completions"
    _TEXT_PART_KEYS: ClassVar[tuple[str, ...]] = ("text",)

    def matches(self, path: str) -> bool:
        return path.strip("/").lower() == "chat/completions"

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment | DocumentSegment]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            origin = f"messages[{index}].content"
            if isinstance(content, str) and content:
                yield Segment(message, "content", content, origin)
            elif isinstance(content, list):
                yield from text_parts(content, origin, text_keys=self._TEXT_PART_KEYS)
                yield from document_parts(content, origin)


class ResponsesAdapter(PayloadAdapter):
    """``POST /responses`` — OpenAI. ``input`` is a string or a list of typed items."""

    name: ClassVar[str] = "openai.responses"
    _TEXT_PART_KEYS: ClassVar[tuple[str, ...]] = ("text",)

    def matches(self, path: str) -> bool:
        return path.strip("/").lower() == "responses"

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment | DocumentSegment]:
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions:
            yield Segment(payload, "instructions", instructions, "instructions")

        payload_input = payload.get("input")
        if isinstance(payload_input, str) and payload_input:
            yield Segment(payload, "input", payload_input, "input")
        elif isinstance(payload_input, list):
            for index, item in enumerate(payload_input):
                if not isinstance(item, dict):
                    continue
                origin = f"input[{index}]"
                content = item.get("content")
                if isinstance(content, str) and content:
                    yield Segment(item, "content", content, f"{origin}.content")
                elif isinstance(content, list):
                    yield from text_parts(
                        content, f"{origin}.content", text_keys=self._TEXT_PART_KEYS
                    )
                    yield from document_parts(content, f"{origin}.content")


class OpenAIAssistantsAdapter(PayloadAdapter):
    """``POST /assistants`` and ``POST /threads/...`` — OpenAI."""

    name: ClassVar[str] = "openai.assistants"
    _INSTRUCTION_KEYS: ClassVar[tuple[str, ...]] = ("instructions", "additional_instructions")

    def matches(self, path: str) -> bool:
        normalized = path.strip("/").lower()
        return normalized == "assistants" or normalized.startswith("threads/")

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment]:
        for key in self._INSTRUCTION_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                yield Segment(payload, key, value, key)

        messages = payload.get("messages")
        if isinstance(messages, list):
            for index, message in enumerate(messages):
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield Segment(message, "content", content, f"messages[{index}].content")

        content = payload.get("content")
        if isinstance(content, str) and content:
            yield Segment(payload, "content", content, "content")


def openai_adapters() -> tuple[PayloadAdapter, ...]:
    return (ChatCompletionsAdapter(), ResponsesAdapter(), OpenAIAssistantsAdapter())


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicMessagesAdapter(PayloadAdapter):
    """``POST /messages`` — Anthropic.

    Two shapes differ from OpenAI and both matter:

    * ``system`` is a top-level field, not a message. It is often the largest block
      of prose in the request — a pasted style guide, a document to reason over — so
      missing it would forfeit most of the saving. It is a string, or (with prompt
      caching) a list of text blocks.
    * A message's ``content`` is a string or a list of blocks; text lives in the
      ``text`` field of ``type: "text"`` blocks. ``tool_use`` and ``tool_result``
      blocks are left untouched.
    """

    name: ClassVar[str] = "anthropic.messages"
    _TEXT_PART_KEYS: ClassVar[tuple[str, ...]] = ("text",)

    def matches(self, path: str) -> bool:
        # `v1/messages`, and version-agnostically any `.../messages` create call, but
        # not `messages/batches` (a batch operation, whose body is not a prompt).
        normalized = path.strip("/").lower()
        return normalized.endswith("messages") and not normalized.endswith("batches")

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment | DocumentSegment]:
        system = payload.get("system")
        if isinstance(system, str) and system:
            yield Segment(payload, "system", system, "system")
        elif isinstance(system, list):
            yield from text_parts(system, "system", text_keys=self._TEXT_PART_KEYS)

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            origin = f"messages[{index}].content"
            if isinstance(content, str) and content:
                yield Segment(message, "content", content, origin)
            elif isinstance(content, list):
                yield from text_parts(content, origin, text_keys=self._TEXT_PART_KEYS)
                yield from document_parts(content, origin)


def anthropic_adapters() -> tuple[PayloadAdapter, ...]:
    return (AnthropicMessagesAdapter(),)
