"""Property-based invariants for the prompt optimizer.

Thousands of randomized prompts, each asserting the guarantees that make "same intent,
fewer tokens, no AI in the loop" a fact rather than a slogan:

* **Deterministic** — ``T(x)`` is the same every time.
* **Idempotent** — ``T(T(x)) == T(x)``.
* **Never grows** — the output is never longer than the input.
* **No reordering, no invention** — every non-blank output line, stripped, is a line of
  the input in the same order (a subsequence). This single property rules out paraphrase,
  synonym replacement, reordering, and any newly introduced text at once.
* **Code is untouched** — the interior of a fenced code block is preserved byte-for-byte.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import PromptOptions
from gateway.optimizers.transformers.prompt import PromptTransformer

pytestmark = pytest.mark.property

SETTINGS = settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

DETECTION = Detection(ContentType.PROMPT, 1.0, "test")
TRANSFORMER = PromptTransformer(PromptOptions(enabled=True))


def _t(content: str) -> str:
    return TRANSFORMER.transform(content, DETECTION).content


# -- Strategies ------------------------------------------------------------
#
# Lines are drawn as whole units from small pools, so a "line" is atomic: the generator
# never produces a line with two sentences that the sentence-deduper could merge, which
# keeps the subsequence property exact while still exercising block and list dedup hard.

_HEADINGS = ["Requirements:", "Rules:", "Constraints:", "Context:", "## Notes", "# Task"]
_BULLETS = [f"- item {c}" for c in "abcdef"] + [f"1. step {n}" for n in range(4)]
_PARAS = [f"Sentence number {n} stands alone" for n in range(8)]
_UNITS = _HEADINGS + _BULLETS + _PARAS


@st.composite
def prompt_text(draw: st.DrawFn) -> str:
    """Assemble a prompt from repeated headings, bullets and paragraphs, with blank
    lines scattered in — the shape of a real, over-copied coding prompt."""
    lines: list[str] = []
    count = draw(st.integers(min_value=1, max_value=40))
    for _ in range(count):
        if draw(st.integers(0, 4)) == 0:
            lines.append("")  # blank line
        else:
            lines.append(draw(st.sampled_from(_UNITS)))
    # Optionally splice in a fenced code block with unique, dedup-tempting content.
    if draw(st.booleans()):
        at = draw(st.integers(0, len(lines)))
        fence = ["```", "code_line = 1", "code_line = 1", "code_line = 2", "```"]
        lines[at:at] = fence
    return "\n".join(lines)


def _stripped_nonblank(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(needle in it for needle in needles)


_FENCE_BLOCKS = re.compile(r"```.*?```", re.DOTALL)


def _code_interiors(text: str) -> list[str]:
    return _FENCE_BLOCKS.findall(text)


# -- Properties ------------------------------------------------------------


@SETTINGS
@given(prompt_text())
def test_deterministic(content: str) -> None:
    assert _t(content) == _t(content)


@SETTINGS
@given(prompt_text())
def test_idempotent(content: str) -> None:
    once = _t(content)
    assert _t(once) == once


@SETTINGS
@given(prompt_text())
def test_never_grows(content: str) -> None:
    assert len(_t(content)) <= len(content)


@SETTINGS
@given(prompt_text())
def test_no_reordering_or_invention(content: str) -> None:
    """Every output line is an input line, in order: no paraphrase, no reorder, no new text."""
    assert _is_subsequence(_stripped_nonblank(_t(content)), _stripped_nonblank(content))


@SETTINGS
@given(prompt_text())
def test_code_blocks_untouched(content: str) -> None:
    """A surviving fenced block keeps its exact bytes; the deduper never edits code."""
    out = _t(content)
    input_blocks = _code_interiors(content)
    for block in _code_interiors(out):
        assert block in input_blocks


@SETTINGS
@given(prompt_text())
def test_output_re_detects_and_transforms_stably(content: str) -> None:
    """Feeding the output back through is a no-op — the pipeline fixed point holds."""
    once = _t(content)
    twice = _t(once)
    thrice = _t(twice)
    assert once == twice == thrice
