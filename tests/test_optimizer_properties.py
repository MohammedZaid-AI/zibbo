"""Property-based invariants.

Two properties hold for every transformer, on every input:

* **Determinism.** ``T(x)`` is the same on every call. Non-determinism would make
  the cache in Phase 8 unsound and the analytics unreproducible.
* **Idempotency.** ``T(T(x)) == T(x)`` whenever ``T`` can handle its own output.

The conditional in the idempotency statement is not a weasel. The HTML transformer
emits Markdown, which is not HTML — feeding Markdown back to it would be asking a
different question. What must hold, and is asserted separately, is that the whole
*pipeline* is idempotent: the detector routes the Markdown to the text transformer,
which is a no-op on it, because the HTML transformer finished by running its output
through that very normalizer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gateway.optimizers import build_transformer_registry
from gateway.optimizers.detector import ContentDetector
from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import HtmlOptions, JsonOptions, OptimizerOptions, TextOptions
from gateway.optimizers.registry import TransformerRegistry
from gateway.optimizers.transformers import HtmlTransformer, JsonTransformer, TextTransformer

pytestmark = pytest.mark.property

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

DETECTOR = ContentDetector()
REGISTRY: TransformerRegistry = build_transformer_registry(OptimizerOptions())


def _transform(transformer: Any, content: str, content_type: ContentType) -> str:
    detection = DETECTOR.detect(content)
    if detection.content_type is not content_type:
        detection = Detection(content_type, 1.0, "forced", parsed=detection.parsed)
    return transformer.transform(content, detection).content


# -- Strategies ------------------------------------------------------------

# `<` is excluded: it would let generated prose accidentally look like markup.
# Surrogates and NUL are excluded because they cannot appear in decoded HTTP text.
_TEXT_CHARS = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters="<\x00",
)
text_strategy = st.text(_TEXT_CHARS, max_size=400)

json_strategy = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(_TEXT_CHARS, max_size=20),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(_TEXT_CHARS, min_size=1, max_size=8), children, max_size=4)
    ),
    max_leaves=15,
)

_INLINE = st.sampled_from(["b", "strong", "em", "i", "code", "span", "a"])
_BLOCK = st.sampled_from(["p", "div", "h1", "h2", "blockquote", "li", "td"])
_NOISE = st.sampled_from(["script", "style", "nav", "footer", "aside", "svg"])

_SAFE_WORDS = st.text(
    st.characters(whitelist_categories=("Lu", "Ll", "Nd"), max_codepoint=0x24F),
    min_size=1,
    max_size=12,
)


@st.composite
def html_strategy(draw: st.DrawFn) -> str:
    """Small documents mixing content, inline markup, and noise."""
    pieces: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        kind = draw(st.integers(min_value=0, max_value=2))
        word = draw(_SAFE_WORDS)
        if kind == 0:
            tag = draw(_BLOCK)
            pieces.append(f"<{tag}>{word}</{tag}>")
        elif kind == 1:
            outer, inner = draw(_BLOCK), draw(_INLINE)
            pieces.append(f"<{outer}><{inner}>{word}</{inner}></{outer}>")
        else:
            tag = draw(_NOISE)
            pieces.append(f"<{tag}>{word}</{tag}>")
    return "".join(pieces)


# -- Determinism -----------------------------------------------------------


@SETTINGS
@given(text_strategy)
def test_text_transform_is_deterministic(content: str) -> None:
    assert _transform(TextTransformer(), content, ContentType.TEXT) == _transform(
        TextTransformer(), content, ContentType.TEXT
    )


@SETTINGS
@given(json_strategy)
def test_json_transform_is_deterministic(value: object) -> None:
    content = json.dumps(value, indent=2)
    first = _transform(JsonTransformer(), content, ContentType.JSON)
    second = _transform(JsonTransformer(), content, ContentType.JSON)
    assert first == second


@SETTINGS
@given(html_strategy())
def test_html_transform_is_deterministic(content: str) -> None:
    assert _transform(HtmlTransformer(), content, ContentType.HTML) == _transform(
        HtmlTransformer(), content, ContentType.HTML
    )


# -- Idempotency -----------------------------------------------------------


@SETTINGS
@given(text_strategy)
def test_text_transform_is_idempotent(content: str) -> None:
    once = _transform(TextTransformer(), content, ContentType.TEXT)
    twice = _transform(TextTransformer(), once, ContentType.TEXT)
    assert twice == once


@SETTINGS
@given(json_strategy)
def test_json_transform_is_idempotent(value: object) -> None:
    content = json.dumps(value, indent=2)
    once = _transform(JsonTransformer(), content, ContentType.JSON)
    twice = _transform(JsonTransformer(), once, ContentType.JSON)
    assert twice == once


@SETTINGS
@given(json_strategy)
def test_json_empty_container_pruning_is_idempotent(value: object) -> None:
    transformer = JsonTransformer(JsonOptions(remove_empty_containers=True))
    content = json.dumps(value, indent=2)
    once = _transform(transformer, content, ContentType.JSON)
    twice = _transform(transformer, once, ContentType.JSON)
    assert twice == once


@SETTINGS
@given(html_strategy())
def test_html_transform_is_idempotent(content: str) -> None:
    once = _transform(HtmlTransformer(), content, ContentType.HTML)
    twice = _transform(HtmlTransformer(), once, ContentType.HTML)
    assert twice == once


@SETTINGS
@given(text_strategy)
def test_inline_whitespace_collapsing_is_idempotent(content: str) -> None:
    transformer = TextTransformer(TextOptions(collapse_inline_whitespace=True))
    once = _transform(transformer, content, ContentType.TEXT)
    twice = _transform(transformer, once, ContentType.TEXT)
    assert twice == once


@SETTINGS
@given(html_strategy())
def test_link_stripping_is_idempotent(content: str) -> None:
    transformer = HtmlTransformer(HtmlOptions(preserve_links=False, preserve_images=False))
    once = _transform(transformer, content, ContentType.HTML)
    twice = _transform(transformer, once, ContentType.HTML)
    assert twice == once


# -- Registry-level idempotency: detection included ------------------------


def _through_registry(content: str) -> str:
    detection = DETECTOR.detect(content)
    transformer = REGISTRY.select(content, detection)
    if transformer is None:
        return content
    return transformer.transform(content, detection).content


@SETTINGS
@given(
    st.one_of(text_strategy, html_strategy(), json_strategy.map(lambda v: json.dumps(v, indent=2)))
)
def test_detect_then_transform_is_idempotent(content: str) -> None:
    """The real pipeline path: the second pass re-detects the first pass's output.

    This is where HTML -> Markdown -> text must settle. If the detector classified
    Markdown as HTML, or the HTML transformer did not finish with text
    normalization, this would fail.
    """
    once = _through_registry(content)
    twice = _through_registry(once)
    assert twice == once


# -- Semantic preservation -------------------------------------------------


@SETTINGS
@given(json_strategy)
def test_json_transform_never_changes_the_value(value: object) -> None:
    """Minification is a formatting change. The decoded value must be identical."""
    content = json.dumps(value, indent=2)
    output = _transform(JsonTransformer(), content, ContentType.JSON)
    assert json.loads(output) == value


@SETTINGS
@given(_SAFE_WORDS, _SAFE_WORDS)
def test_html_transform_never_invents_or_drops_words(first: str, second: str) -> None:
    """Content words survive; the noise element's words do not."""
    content = f"<p>{first}</p><script>{second}</script>"
    output = _transform(HtmlTransformer(), content, ContentType.HTML)
    assert output == first


@SETTINGS
@given(text_strategy)
def test_text_transform_never_grows_the_content(content: str) -> None:
    output = _transform(TextTransformer(), content, ContentType.TEXT)
    assert len(output) <= len(content)
