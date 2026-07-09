"""Transformer configuration, decoupled from ``Settings``.

Transformers take an options object, not the application settings. They stay
unit-testable without an environment, and a future per-tenant policy can hand a
different options object to the same transformer instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class TextOptions:
    """Only reversible-looking, meaning-preserving normalizations."""

    dedupe_consecutive_paragraphs: bool = True

    collapse_inline_whitespace: bool = False
    """Off by default. Runs of spaces carry meaning in code blocks and Markdown
    tables; collapsing them would rewrite content, not strip noise."""

    max_consecutive_blank_lines: int = 1


@dataclass(frozen=True, slots=True)
class JsonOptions:
    remove_empty_containers: bool = False
    """Off by default. ``{"tools": []}`` does not mean the same thing as ``{}``."""


@dataclass(frozen=True, slots=True)
class HtmlOptions:
    preserve_links: bool = True
    """URLs are content. Dropping them saves tokens by destroying information."""

    preserve_images: bool = True
    """Keeps ``![alt](src)``. ``data:`` URIs are always dropped — they are payload,
    not reference, and can be megabytes."""

    text: TextOptions = field(default_factory=TextOptions)


@dataclass(frozen=True, slots=True)
class OptimizerOptions:
    text: TextOptions = field(default_factory=TextOptions)
    json: JsonOptions = field(default_factory=JsonOptions)
    html: HtmlOptions = field(default_factory=HtmlOptions)
    min_segment_chars: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> OptimizerOptions:
        text = TextOptions(
            dedupe_consecutive_paragraphs=settings.text_dedupe_consecutive_paragraphs,
            collapse_inline_whitespace=settings.text_collapse_inline_whitespace,
        )
        return cls(
            text=text,
            json=JsonOptions(remove_empty_containers=settings.json_remove_empty_containers),
            html=HtmlOptions(
                preserve_links=settings.html_preserve_links,
                preserve_images=settings.html_preserve_images,
                # The HTML transformer finishes by running its Markdown through the
                # same normalizer the text transformer uses. Sharing the options is
                # what makes pipeline(pipeline(x)) == pipeline(x) hold.
                text=text,
            ),
            min_segment_chars=settings.optimization_min_segment_chars,
        )
