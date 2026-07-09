"""Finding the text worth optimizing inside a provider's request schema.

A chat request is JSON, but minifying the *envelope* saves nothing — the tokens
are in ``messages[i].content``, where a user has pasted a scraped web page or a
pretty-printed API response. So the pipeline does not transform the body; it
transforms the segments an adapter points it at.

Segments hold a direct reference to their container and key, so writing a result
back is an assignment rather than a second walk of the tree. Nothing is copied.

Adding an endpoint means adding an adapter. Adding a provider (Anthropic, Phase 6)
means adding an adapter. Neither touches the pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@dataclass(frozen=True, slots=True)
class Segment:
    """One piece of optimizable text, and where to put its replacement."""

    container: dict[str, Any] | list[Any]
    key: str | int
    text: str
    origin: str
    """Human-readable path, for logs. Never contains user content."""

    def replace(self, content: str) -> None:
        self.container[self.key] = content  # type: ignore[index]


class PayloadAdapter(ABC):
    """Knows the shape of one endpoint's request body."""

    name: ClassVar[str]

    @abstractmethod
    def matches(self, path: str) -> bool:
        """Whether this adapter understands the endpoint at ``path``."""

    @abstractmethod
    def extract(self, payload: dict[str, Any]) -> Iterator[Segment]:
        """Yield every optimizable text segment, in document order."""


def _text_parts(
    content: list[Any], origin: str, *, text_keys: tuple[str, ...]
) -> Iterator[Segment]:
    """Walk a multimodal content array, yielding only its text parts.

    Image and audio parts are skipped: they are not text, and the ``image_url``
    of a base64 data URI would be destroyed by a text transformer.
    """
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        for key in text_keys:
            value = part.get(key)
            if isinstance(value, str) and value:
                yield Segment(part, key, value, f"{origin}[{index}].{key}")


class ChatCompletionsAdapter(PayloadAdapter):
    """``POST /v1/chat/completions``.

    ``content`` is either a string or an array of typed parts. Both are handled;
    tool call arguments and function results are left alone, because they are
    machine-generated and a caller may parse them byte-exactly.
    """

    name: ClassVar[str] = "chat.completions"

    _TEXT_PART_KEYS: ClassVar[tuple[str, ...]] = ("text",)

    def matches(self, path: str) -> bool:
        return path.strip("/").lower() == "chat/completions"

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment]:
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
                yield from _text_parts(content, origin, text_keys=self._TEXT_PART_KEYS)


class ResponsesAdapter(PayloadAdapter):
    """``POST /v1/responses``. ``input`` is a string or a list of typed items."""

    name: ClassVar[str] = "responses"

    _TEXT_PART_KEYS: ClassVar[tuple[str, ...]] = ("text",)

    def matches(self, path: str) -> bool:
        return path.strip("/").lower() == "responses"

    def extract(self, payload: dict[str, Any]) -> Iterator[Segment]:
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
                    yield from _text_parts(
                        content, f"{origin}.content", text_keys=self._TEXT_PART_KEYS
                    )


class AssistantsAdapter(PayloadAdapter):
    """``POST /v1/assistants`` and ``POST /v1/threads/...``: instructions and messages."""

    name: ClassVar[str] = "assistants"

    _INSTRUCTION_KEYS: ClassVar[tuple[str, ...]] = (
        "instructions",
        "additional_instructions",
    )

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


class AdapterRegistry:
    """Finds the adapter for an endpoint."""

    def __init__(self, adapters: Sequence[PayloadAdapter] | None = None) -> None:
        self._adapters = tuple(adapters) if adapters is not None else default_adapters()

    def for_path(self, path: str) -> PayloadAdapter | None:
        for adapter in self._adapters:
            if adapter.matches(path):
                return adapter
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)


def default_adapters() -> tuple[PayloadAdapter, ...]:
    return (ChatCompletionsAdapter(), ResponsesAdapter(), AssistantsAdapter())
