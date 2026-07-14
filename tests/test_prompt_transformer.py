"""Unit tests for the deterministic prompt optimizer.

The theme, as with the other transformers: prove exact redundancy is removed *and* that
meaning, ordering, code, and examples are not. The one worked example from the Phase 12
spec that is achievable without inferring intent — a repeated ``Requirements:`` section —
is pinned exactly; the semantic collapses in the spec's other examples are deliberately
out of scope (they would require an LLM) and are asserted *not* to happen.
"""

from __future__ import annotations

import pytest

from gateway.optimizers.detector import ContentDetector, PromptSniffer
from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import PromptOptions
from gateway.optimizers.transformers.prompt import PromptTransformer

PROMPT_DETECTION = Detection(ContentType.PROMPT, 1.0, "test")


def _prompt(content: str, **kwargs: object) -> str:
    transformer = PromptTransformer(PromptOptions(enabled=True, **kwargs))  # type: ignore[arg-type]
    return transformer.transform(content, PROMPT_DETECTION).content


def _steps(content: str) -> tuple[str, ...]:
    return PromptTransformer(PromptOptions(enabled=True)).transform(content, PROMPT_DETECTION).steps


# ===========================================================================
# The achievable spec example: a repeated Requirements section
# ===========================================================================


def test_repeated_requirements_section_is_folded() -> None:
    source = (
        "Fix this bug.\n\n"
        "Requirements:\n\n"
        "- Don't modify authentication.\n"
        "- Don't modify CSS.\n"
        "- Return complete files.\n\n"
        "Requirements:\n\n"
        "- Don't modify CSS.\n"
        "- Return complete files."
    )
    expected = (
        "Fix this bug.\n\n"
        "Requirements:\n\n"
        "- Don't modify authentication.\n"
        "- Don't modify CSS.\n"
        "- Return complete files."
    )
    assert _prompt(source) == expected


def test_exact_duplicate_paragraph_removed() -> None:
    source = "Refactor the parser.\n\nRefactor the parser.\n\nThen add tests."
    assert _prompt(source) == "Refactor the parser.\n\nThen add tests."


def test_exact_duplicate_bullets_under_same_heading_removed() -> None:
    source = (
        "Rules:\n\n- Keep tests green.\n- Keep tests green.\n- Do not touch the schema."
    )
    assert _prompt(source) == "Rules:\n\n- Keep tests green.\n- Do not touch the schema."


# ===========================================================================
# What it must never do
# ===========================================================================


def test_identical_bullets_under_different_headings_are_kept() -> None:
    """Section-scoped: the same words mean different things under different headings."""
    source = (
        "Frontend tasks:\n\n- Add a button.\n\nBackend tasks:\n\n- Add a button."
    )
    assert _prompt(source) == source


def test_no_semantic_collapse_of_similar_but_distinct_lines() -> None:
    """Spec examples 1-3 want this collapsed; a deterministic engine must not."""
    source = (
        "Don't modify authentication.\n"
        "Don't touch auth.\n"
        "Don't change login.\n"
        "Authentication must remain exactly the same."
    )
    # No two lines are byte-identical, so nothing is removed.
    assert _prompt(source) == source
    assert _steps(source) == ()


def test_code_fence_interior_is_never_modified() -> None:
    source = (
        "Do this.\n\n"
        "```python\n"
        "x = 1\n"
        "x = 1\n"  # identical lines inside code are NOT de-duplicated
        "y = 2\n"
        "```\n\n"
        "Do this."
    )
    out = _prompt(source)
    assert "x = 1\nx = 1\ny = 2" in out  # code body byte-identical
    assert out.count("Do this.") == 1  # the duplicate prose line outside code went


def test_byte_identical_code_fences_are_deduplicated() -> None:
    fence = "```python\nprint('hi')\n```"
    source = f"Example:\n\n{fence}\n\n{fence}"
    out = _prompt(source)
    assert out.count("print('hi')") == 1


def test_stack_trace_is_preserved() -> None:
    trace = (
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in <module>\n'
        "    raise ValueError('x')\n"
        "ValueError: x"
    )
    source = f"Fix this.\n\n{trace}\n\nFix this."
    out = _prompt(source)
    assert trace in out  # the trace block survives byte-for-byte
    assert out.count("Fix this.") == 1  # only the duplicate instruction is removed


def test_numbered_list_is_left_alone_when_distinct() -> None:
    source = "Steps:\n\n1. Clone the repo.\n2. Install deps.\n3. Run tests."
    assert _prompt(source) == source


# ===========================================================================
# Invariants
# ===========================================================================


@pytest.mark.parametrize(
    "source",
    [
        "Requirements:\n\n- a\n- b\n\nRequirements:\n\n- a\n- b",
        "Para.\n\nPara.\n\nOther.",
        "Rules:\n\n- x\n- x\n- y",
        "Just one line with no redundancy at all.",
    ],
)
def test_idempotent(source: str) -> None:
    once = _prompt(source)
    twice = _prompt(once)
    assert once == twice


@pytest.mark.parametrize(
    "source",
    [
        "Requirements:\n\n- a\n- b\n\nRequirements:\n\n- a\n- b",
        "Para.\n\nPara.\n\nOther.",
    ],
)
def test_never_grows(source: str) -> None:
    assert len(_prompt(source)) <= len(source)


def test_disabled_options_still_normalize_only() -> None:
    """With enabled=False the transformer is a plain normalizer: no de-duplication."""
    transformer = PromptTransformer(PromptOptions(enabled=False))
    source = "Requirements:\n\n- a\n- a"
    # It still normalizes, but the block/list dedup steps must be absent.
    out = transformer.transform(source, PROMPT_DETECTION)
    assert "removed_duplicate_list_items" not in out.steps
    assert "removed_duplicate_blocks" not in out.steps


def test_unchanged_content_returns_original_and_no_steps() -> None:
    source = "A single clean instruction."
    result = PromptTransformer(PromptOptions(enabled=True)).transform(source, PROMPT_DETECTION)
    assert result.content == source
    assert result.steps == ()


# ===========================================================================
# Detection
# ===========================================================================


def _detector() -> ContentDetector:
    detector = ContentDetector()
    detector.add_sniffer(PromptSniffer(min_chars=1500, min_duplicate_ratio=0.15))
    return detector


def _long_duplicate_prompt() -> str:
    block = "Requirements:\n\n- Do not modify authentication.\n- Return complete files.\n\n"
    return "Fix the checkout bug.\n\n" + block * 24


def test_long_duplicate_prose_is_detected_as_prompt() -> None:
    detection = _detector().detect(_long_duplicate_prompt())
    assert detection.content_type is ContentType.PROMPT


def test_short_prompt_is_not_detected() -> None:
    detection = _detector().detect("Requirements:\n\n- a\n- a")
    assert detection.content_type is not ContentType.PROMPT


def test_low_duplicate_prose_is_not_detected() -> None:
    unique = "\n\n".join(f"Paragraph number {i} says something different." for i in range(80))
    assert _detector().detect(unique).content_type is not ContentType.PROMPT


def test_code_paste_is_not_detected_as_prompt() -> None:
    code = (
        "def handler(event):\n    return event\n\nclass Service:\n    pass\n" * 40
    )
    assert _detector().detect(code).content_type is not ContentType.PROMPT


def test_json_still_wins_over_prompt() -> None:
    import json

    payload = json.dumps({"items": [{"id": i, "id2": i} for i in range(200)]}, indent=2)
    assert _detector().detect(payload).content_type is ContentType.JSON


def test_disabled_detector_has_no_prompt_sniffer() -> None:
    """A plain detector never classifies PROMPT — the feature is opt-in."""
    assert ContentDetector().detect(_long_duplicate_prompt()).content_type is ContentType.TEXT
