"""Finding the text worth optimizing inside a request body.

A chat request is JSON, but minifying the *envelope* saves nothing — the tokens
are in the message content, where a user has pasted a scraped web page or a
pretty-printed API response. So the pipeline does not transform the body; it
transforms the segments an adapter points it at.

**This module contains no provider knowledge.** It cannot know that OpenAI calls
the field ``messages`` and Gemini calls it ``contents``, and it must not learn:
concrete adapters live in ``gateway.providers.schemas`` and are handed to the
pipeline by the provider that owns them.

Segments hold a direct reference to their container and key, so writing a result
back is an assignment rather than a second walk of the tree. Nothing is copied.
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


def text_parts(content: list[Any], origin: str, *, text_keys: tuple[str, ...]) -> Iterator[Segment]:
    """Walk a multimodal content array, yielding only its text parts.

    Image and audio parts are skipped: they are not text, and the base64 payload of
    an inline image would be destroyed by a text transformer.
    """
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        for key in text_keys:
            value = part.get(key)
            if isinstance(value, str) and value:
                yield Segment(part, key, value, f"{origin}[{index}].{key}")


class AdapterRegistry:
    """Finds the adapter for an endpoint. One registry per provider."""

    def __init__(self, adapters: Sequence[PayloadAdapter] = ()) -> None:
        self._adapters = tuple(adapters)

    def for_path(self, path: str) -> PayloadAdapter | None:
        for adapter in self._adapters:
            if adapter.matches(path):
                return adapter
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)
