"""Plain-text normalization. Nothing semantic.

``normalize_text`` is exported because the HTML transformer finishes by running its
Markdown through it. Sharing one normalizer is what guarantees that feeding the
pipeline's output back into the pipeline is a no-op: the Markdown is already in the
exact form the text transformer would produce.

Deliberately *not* done here: collapsing runs of spaces inside a line. Indentation
is meaning in code blocks, and alignment is meaning in Markdown tables. It is
available behind ``TextOptions.collapse_inline_whitespace`` for callers who know
their content is prose.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, TransformOutput
from gateway.optimizers.options import TextOptions

if TYPE_CHECKING:
    from gateway.optimizers.models import Detection

_LINE_ENDINGS_RE = re.compile(r"\r\n|\r")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]{2,}")

STEP_LINE_ENDINGS = "normalized_line_endings"
STEP_TRAILING_WHITESPACE = "stripped_trailing_whitespace"
STEP_BLANK_LINES = "collapsed_blank_lines"
STEP_INLINE_WHITESPACE = "collapsed_inline_whitespace"
STEP_DUPLICATE_PARAGRAPHS = "removed_duplicate_paragraphs"


def _collapse_blank_lines(text: str, max_consecutive: int) -> str:
    """Reduce runs of blank lines to at most ``max_consecutive``."""
    pattern = re.compile(rf"\n{{{max_consecutive + 2},}}")
    return pattern.sub("\n" * (max_consecutive + 1), text)


def _dedupe_consecutive_paragraphs(text: str) -> str:
    """Drop a paragraph identical to the one immediately before it.

    Consecutive-only, so the repeated boilerplate that survives HTML stripping is
    removed while a legitimately recurring phrase elsewhere in the document is not.
    Idempotent: removing adjacent duplicates cannot create new adjacent duplicates
    that were not already there transitively.
    """
    paragraphs = text.split("\n\n")
    kept: list[str] = []
    for paragraph in paragraphs:
        if kept and paragraph.strip() and paragraph.strip() == kept[-1].strip():
            continue
        kept.append(paragraph)
    return "\n\n".join(kept)


def normalize_text(text: str, options: TextOptions) -> tuple[str, tuple[str, ...]]:
    """Normalize ``text``, returning it with the names of the steps that changed it."""
    steps: list[str] = []

    normalized = _LINE_ENDINGS_RE.sub("\n", text)
    if normalized != text:
        steps.append(STEP_LINE_ENDINGS)

    stripped = _TRAILING_SPACE_RE.sub("", normalized)
    if stripped != normalized:
        steps.append(STEP_TRAILING_WHITESPACE)
    normalized = stripped

    if options.collapse_inline_whitespace:
        collapsed = _INLINE_WHITESPACE_RE.sub(" ", normalized)
        if collapsed != normalized:
            steps.append(STEP_INLINE_WHITESPACE)
        normalized = collapsed

    collapsed = _collapse_blank_lines(normalized, options.max_consecutive_blank_lines)
    if collapsed != normalized:
        steps.append(STEP_BLANK_LINES)
    normalized = collapsed

    if options.dedupe_consecutive_paragraphs:
        deduped = _dedupe_consecutive_paragraphs(normalized)
        if deduped != normalized:
            steps.append(STEP_DUPLICATE_PARAGRAPHS)
        normalized = deduped

    # Leading/trailing blank space is never meaningful and never counted twice.
    trimmed = normalized.strip()
    if trimmed != normalized and not steps:
        steps.append(STEP_TRAILING_WHITESPACE)

    return trimmed, tuple(steps)


class TextTransformer(Transformer):
    """The fallback transformer: safe normalization, nothing else."""

    name: ClassVar[str] = "text"
    priority: ClassVar[int] = 100
    content_types: ClassVar[frozenset[ContentType]] = frozenset(
        {ContentType.TEXT, ContentType.UNKNOWN}
    )

    def __init__(self, options: TextOptions | None = None) -> None:
        self._options = options or TextOptions()

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        del detection
        normalized, steps = normalize_text(content, self._options)
        if normalized == content:
            return TransformOutput(content, ())
        return TransformOutput(normalized, steps)
