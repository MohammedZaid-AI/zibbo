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
class PromptOptions:
    """Deterministic prompt de-duplication. Off by default — a long prompt with genuine
    structure must never be reshaped without the operator asking for it."""

    enabled: bool = False
    """When false the PROMPT content type is never detected: long prose is handled by
    the plain-text transformer exactly as before."""

    min_chars: int = 240
    """Below this the content is left to the plain-text transformer. Low enough to admit a
    realistic coding prompt (about two copies of a short instruction block); the real guard
    against reshaping ordinary prose is ``min_duplicate_ratio``, not length."""

    min_duplicate_ratio: float = 0.15
    """Fraction of non-blank lines that must be exact duplicates before the content is
    classified PROMPT. Keeps ordinary prose out of the prompt path."""

    text: TextOptions = field(default_factory=TextOptions)
    """The prompt transformer finishes with the shared normalizer, so its output is a
    fixed point of the text transformer and the pipeline stays idempotent."""


@dataclass(frozen=True, slots=True)
class OptimizerOptions:
    text: TextOptions = field(default_factory=TextOptions)
    json: JsonOptions = field(default_factory=JsonOptions)
    html: HtmlOptions = field(default_factory=HtmlOptions)
    prompt: PromptOptions = field(default_factory=PromptOptions)
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
            prompt=PromptOptions(
                enabled=settings.prompt_optimization_enabled,
                min_chars=settings.prompt_optimization_min_chars,
                min_duplicate_ratio=settings.prompt_optimization_min_duplicate_ratio,
                text=text,
            ),
            min_segment_chars=settings.optimization_min_segment_chars,
        )
