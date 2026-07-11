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

import base64
import binascii
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

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


_DATA_URI_RE: Final = re.compile(
    r"^data:(?P<media>[\w.+-]+/[\w.+-]+)?(?:;[\w-]+=[\w.+-]+)*;base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DocumentSegment:
    """A base64-encoded document embedded in a request, and how to replace it.

    Distinct from :class:`Segment` because the payload is *bytes*, not text, and the
    replacement rewrites a whole content block (``{"type": "document", ...}``) into a
    text block rather than swapping one string for another. The pipeline decodes,
    extracts, and — only if that produced something cheaper — calls :meth:`replace`.
    """

    block: dict[str, Any]
    """The content-array element to rewrite in place."""

    data: bytes
    media_type: str | None
    filename: str | None
    origin: str
    original_text: str
    """The base64 payload as it sat in the request. A reference, not a copy, kept so
    the pipeline can count the tokens the provider would have been charged for it."""

    def replace(self, markdown: str) -> None:
        """Turn the document block into a plain text block carrying the Markdown.

        ``clear`` then re-populate, so provider-specific keys (``source``, ``file``,
        ``cache_control``) do not linger beside the new ``text``.
        """
        cache_control = self.block.get("cache_control")
        self.block.clear()
        self.block["type"] = "text"
        self.block["text"] = markdown
        if cache_control is not None:
            self.block["cache_control"] = cache_control


def _decode_base64(data: str) -> bytes | None:
    """Decode base64, tolerating whitespace and missing padding. ``None`` if invalid."""
    cleaned = "".join(data.split())
    if not cleaned:
        return None
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        return None


class PayloadAdapter(ABC):
    """Knows the shape of one endpoint's request body."""

    name: ClassVar[str]

    @abstractmethod
    def matches(self, path: str) -> bool:
        """Whether this adapter understands the endpoint at ``path``."""

    @abstractmethod
    def extract(self, payload: dict[str, Any]) -> Iterator[Segment | DocumentSegment]:
        """Yield every optimizable text and document segment, in document order."""


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


# Content-block shapes that carry an uploaded document, across providers.
_DOCUMENT_BLOCK_TYPES: Final = frozenset({"document", "file", "input_file"})


def document_parts(content: list[Any], origin: str) -> Iterator[DocumentSegment]:
    """Yield base64 documents embedded in a multimodal content array.

    Recognizes the shapes providers actually use:

    * Anthropic ``{"type": "document", "source": {"type": "base64",
      "media_type": ..., "data": ...}}``
    * OpenAI ``{"type": "file", "file": {"file_data": "data:...;base64,...",
      "filename": ...}}`` and the ``input_file`` variant.

    A block whose base64 does not decode, or whose source is a URL rather than
    inline data, is skipped — the gateway only rewrites what it can read.
    """
    for index, part in enumerate(content):
        if not isinstance(part, dict) or part.get("type") not in _DOCUMENT_BLOCK_TYPES:
            continue
        decoded = _document_from_block(part)
        if decoded is not None:
            data, media_type, filename, raw_text = decoded
            yield DocumentSegment(part, data, media_type, filename, f"{origin}[{index}]", raw_text)


def _document_from_block(
    block: dict[str, Any],
) -> tuple[bytes, str | None, str | None, str] | None:
    """Pull (bytes, media_type, filename, raw_base64) out of a block, if inline."""
    # Anthropic: nested `source` object.
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        raw = str(source.get("data", ""))
        data = _decode_base64(raw)
        if data is not None:
            media = source.get("media_type")
            return data, media if isinstance(media, str) else None, None, raw

    # OpenAI: nested `file` object with a data URI.
    file_obj = block.get("file")
    if isinstance(file_obj, dict):
        raw_name = file_obj.get("filename")
        filename = raw_name if isinstance(raw_name, str) else None
        file_data = file_obj.get("file_data")
        if isinstance(file_data, str):
            match = _DATA_URI_RE.match(file_data.strip())
            if match:
                data = _decode_base64(match.group("data"))
                if data is not None:
                    return data, match.group("media"), filename, file_data
    return None


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
