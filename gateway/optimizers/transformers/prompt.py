"""Deterministic prompt de-duplication.

AI coding assistants — Claude Code, Codex, Gemini CLI, Continue, Cline, Roo Code,
Aider — get long, human-written prompts that repeat themselves: the same instruction
pasted twice, a Requirements section copied and edited, the same bullet under two
copies of the same heading. This transformer removes that **exact** redundancy and
nothing else.

What it removes, all keep-first so ordering never changes:

* exact-duplicate blocks (paragraphs, byte-identical fenced code blocks, byte-identical
  Markdown tables, an isolated repeated heading such as a second ``Requirements:``);
* exact-duplicate list items **within the same section** — items under a repeated
  heading are folded into the first occurrence, but an identical bullet under a
  *different* heading is left alone, because there it means something different;
* a sentence immediately repeated inside a paragraph.

It then runs the shared text normalizer, so its output is a fixed point of the plain
text transformer and ``pipeline(pipeline(x)) == pipeline(x)`` holds exactly.

What it will **never** do, by construction rather than by policy: paraphrase, replace a
word with a synonym, reorder, correct grammar or spelling, shorten an explanation,
touch the inside of a code fence or inline code, or remove an example, a stack trace or
an error message. Every operation is exact-string removal of a later duplicate. There is
no model in the loop; the same input yields the same output on every machine, forever.

The semantic collapses one might *wish* for — recognizing that "don't touch auth" and
"don't change login" say the same thing — are deliberately out of scope. They require
inferring intent, which is the one thing a deterministic, no-AI transformer must not do.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, TransformOutput
from gateway.optimizers.options import PromptOptions
from gateway.optimizers.transformers.text import normalize_text

if TYPE_CHECKING:
    from gateway.optimizers.models import Detection

# A fence opens/closes a code region. Markdown allows ``` or ~~~, optionally indented.
_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>```+|~~~+)")
# List items: unordered (-, *, +) and ordered (1. / 1)). A marker must be followed by
# text, so a bare "-" rule line or "* * *" separator is not treated as a list item.
_BULLET_RE = re.compile(r"^\s*[-*+]\s+\S")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+\S")
# Headings that open a section: Markdown ATX (## Foo) and short label lines (Requirements:).
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_LABEL_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _/&-]{0,40}:\s*$")
# Sentence boundary: a terminator followed by whitespace. Conservative on purpose.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

STEP_DUPLICATE_LIST_ITEMS = "removed_duplicate_list_items"
STEP_DUPLICATE_BLOCKS = "removed_duplicate_blocks"
STEP_DUPLICATE_SENTENCES = "removed_duplicate_sentences"


def _is_fence(line: str) -> bool:
    return _FENCE_RE.match(line) is not None


def _is_heading(line: str) -> bool:
    return bool(_ATX_HEADING_RE.match(line) or _LABEL_HEADING_RE.match(line))


def _is_list_item(line: str) -> bool:
    return bool(_BULLET_RE.match(line) or _NUMBERED_RE.match(line))


def _dedupe_list_items(lines: list[str]) -> tuple[list[str], bool]:
    """Drop a list item that already appeared under the *same* heading. Keep first.

    Section-scoped is the safety property: two identical bullets under one repeated
    ``Requirements:`` heading are the same instruction and the copy is dropped, but an
    identical bullet under a different heading is a different instruction and is kept.
    Lines inside a fenced code block are never touched.
    """
    seen: dict[str, set[str]] = {}
    current_heading = ""
    in_fence = False
    kept: list[str] = []
    removed = False

    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            kept.append(line)
            continue
        if in_fence:
            kept.append(line)
            continue

        if _is_heading(line):
            current_heading = line.strip()
            kept.append(line)
            continue

        if _is_list_item(line):
            key = line.strip()
            bucket = seen.setdefault(current_heading, set())
            if key in bucket:
                removed = True
                continue
            bucket.add(key)

        kept.append(line)

    return kept, removed


def _split_blocks(lines: list[str]) -> list[list[str]]:
    """Group lines into blocks separated by blank lines. A fenced code block is one
    block regardless of the blank lines inside it, so it is compared and kept as an
    atomic unit and its interior is never split apart."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_fence = False

    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            current.append(line)
            continue
        if in_fence:
            current.append(line)
            continue
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)

    if current:
        blocks.append(current)
    return blocks


def _block_key(block: list[str]) -> str:
    """Identity of a block for duplicate detection: trailing whitespace on each line is
    noise, everything else is significant (indentation and alignment are meaning)."""
    return "\n".join(line.rstrip() for line in block)


def _is_list_only(block: list[str]) -> bool:
    """A block whose non-blank lines are all list items. Its de-duplication is the
    section-scoped list deduper's job, not the block deduper's — otherwise two identical
    bullets under *different* headings would be collapsed, changing meaning."""
    non_blank = [line for line in block if line.strip()]
    return bool(non_blank) and all(_is_list_item(line) for line in non_blank)


def _dedupe_blocks(lines: list[str]) -> tuple[list[str], bool]:
    """Remove a block byte-identical to an earlier one. Keep first.

    List-only blocks are left to the section-scoped list deduper; a fenced code block is
    compared whole (so a byte-identical code fence is removed, but its interior is never
    inspected line by line).
    """
    blocks = _split_blocks(lines)
    seen: set[str] = set()
    seen_headings: set[str] = set()
    kept: list[list[str]] = []
    removed = False

    for block in blocks:
        non_blank = [line for line in block if line.strip()]

        # A block that is nothing but a heading which already opened an earlier block is a
        # repeated section header left bare after its items were de-duplicated (the second
        # `Requirements:` once its bullets are gone). Drop it — keep-first — so a duplicated
        # section collapses completely rather than leaving a dangling heading behind.
        if len(non_blank) == 1 and _is_heading(non_blank[0]):
            heading = non_blank[0].strip()
            if heading in seen_headings:
                removed = True
                continue
            seen_headings.add(heading)
            kept.append(block)
            continue

        if _is_list_only(block):
            kept.append(block)
            continue
        key = _block_key(block)
        if key in seen:
            removed = True
            continue
        seen.add(key)
        # A block that opens with a heading claims that heading, so a later bare repeat of
        # it (left behind by list-item de-duplication) is recognized as a duplicate.
        if non_blank and _is_heading(non_blank[0]):
            seen_headings.add(non_blank[0].strip())
        kept.append(block)

    if not removed:
        return lines, False
    # Reassemble with a single blank line between blocks; the final normalize pass owns
    # blank-line policy, so producing exactly one here keeps the result a fixed point.
    out: list[str] = []
    for index, block in enumerate(kept):
        if index:
            out.append("")
        out.extend(block)
    return out, True


def _dedupe_consecutive_sentences(lines: list[str]) -> tuple[list[str], bool]:
    """Within a paragraph, drop a sentence identical to the one immediately before it.

    Skipped for any line holding inline code (backticks) or fenced regions: splitting on
    ``. `` there could fall inside a code span, and the transformer must never risk a
    code edit to save a sentence. Consecutive-only, so a phrase that recurs elsewhere in
    the prompt is untouched.
    """
    in_fence = False
    changed = False
    out: list[str] = []

    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or "`" in line or _is_list_item(line) or _is_heading(line):
            out.append(line)
            continue

        sentences = _SENTENCE_SPLIT_RE.split(line)
        if len(sentences) < 2:
            out.append(line)
            continue

        deduped: list[str] = []
        for sentence in sentences:
            if deduped and sentence and sentence == deduped[-1]:
                changed = True
                continue
            deduped.append(sentence)
        out.append(" ".join(deduped) if changed else line)

    return out, changed


class PromptTransformer(Transformer):
    """Removes exact redundancy from a long human-written prompt. Nothing semantic."""

    name: ClassVar[str] = "prompt"
    # Ahead of the plain-text transformer (100): a prompt is a more specific kind of
    # text, so when the detector classifies content PROMPT this must win selection.
    priority: ClassVar[int] = 90
    content_types: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PROMPT})

    def __init__(self, options: PromptOptions | None = None) -> None:
        self._options = options or PromptOptions()

    @staticmethod
    def _deduplicate(lines: list[str], steps: list[str]) -> list[str]:
        """Run the removal passes to a fixed point.

        The passes interact: block de-duplication can drop a heading that list-item
        de-duplication relies on to scope sections, which would let a *second* call remove
        a bullet the first call kept — breaking idempotency. Iterating until a whole round
        removes nothing closes that gap by construction, and terminates because every
        effective round strictly shrinks the content. ``steps`` records each kind of
        removal once, in the order it first happened, for a readable explain view.
        """
        seen_steps: set[str] = set()
        while True:
            lines, removed_items = _dedupe_list_items(lines)
            lines, removed_blocks = _dedupe_blocks(lines)
            lines, removed_sentences = _dedupe_consecutive_sentences(lines)
            for did, name in (
                (removed_items, STEP_DUPLICATE_LIST_ITEMS),
                (removed_blocks, STEP_DUPLICATE_BLOCKS),
                (removed_sentences, STEP_DUPLICATE_SENTENCES),
            ):
                if did and name not in seen_steps:
                    seen_steps.add(name)
                    steps.append(name)
            if not (removed_items or removed_blocks or removed_sentences):
                return lines

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        del detection
        # Normalize line endings up front so block/line splitting is stable; the shared
        # normalizer at the end owns every other whitespace decision.
        working = content.replace("\r\n", "\n").replace("\r", "\n")
        lines = working.split("\n")
        steps: list[str] = []

        # `enabled` gates the de-duplication only. When off, this behaves as the plain
        # text normalizer would — the feature is dormant, not destructive. In the running
        # gateway the transformer is registered only when enabled, so this guard is a
        # defensive belt against direct construction.
        if self._options.enabled:
            lines = self._deduplicate(lines, steps)

        deduped = "\n".join(lines)
        normalized, norm_steps = normalize_text(deduped, self._options.text)
        steps.extend(norm_steps)

        if normalized == content:
            return TransformOutput(content, ())
        return TransformOutput(normalized, tuple(steps))
