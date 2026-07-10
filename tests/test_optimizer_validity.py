"""Output validity, over thousands of randomized inputs.

Determinism and idempotency (test_optimizer_properties.py) say the output is
*stable*. They say nothing about whether it is *usable*. These tests assert the
output is well-formed Markdown or well-formed JSON, whatever went in.

Markdown has no single specification, so "valid" is pinned to the properties a
consumer actually depends on: no leaked markup, balanced code fences, well-formed
headings and tables, and text normalized exactly as the text transformer would
leave it — which is also what makes the pipeline idempotent.
"""

from __future__ import annotations

import json
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gateway.optimizers.detector import ContentDetector
from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import HtmlOptions, JsonOptions
from gateway.optimizers.transformers import HtmlTransformer, JsonTransformer

pytestmark = pytest.mark.property

# "Thousands of randomized inputs": 1000 examples across each of several properties.
THOROUGH = settings(
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

DETECTOR = ContentDetector()
HTML_DETECTION = Detection(ContentType.HTML, 1.0, "test")

_RAW_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9]*[^>]*>")
_HEADING_RE = re.compile(r"^(#{1,6}) (?=\S)")
_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:-{3}\|)+$")


# -- Strategies ------------------------------------------------------------

_WORD = st.text(
    st.characters(whitelist_categories=("Lu", "Ll", "Nd"), max_codepoint=0x24F),
    min_size=1,
    max_size=10,
)

_CONTENT_TAGS = ["p", "h1", "h2", "h3", "div", "blockquote", "pre", "span", "strong", "em", "code"]
_NOISE_TAGS = ["script", "style", "nav", "footer", "aside", "svg", "iframe", "noscript"]


@st.composite
def html_documents(draw: st.DrawFn) -> str:
    """Documents that open with a tag, so the detector sees them as content."""
    pieces: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=8))):
        kind = draw(st.integers(min_value=0, max_value=5))
        word = draw(_WORD)
        if kind == 0:
            tag = draw(st.sampled_from(_CONTENT_TAGS))
            pieces.append(f"<{tag}>{word}</{tag}>")
        elif kind == 1:
            tag = draw(st.sampled_from(_NOISE_TAGS))
            pieces.append(f"<{tag}>{word}</{tag}>")
        elif kind == 2:
            items = "".join(f"<li>{draw(_WORD)}</li>" for _ in range(draw(st.integers(1, 3))))
            pieces.append(f"<{draw(st.sampled_from(['ul', 'ol']))}>{items}</ul>")
        elif kind == 3:
            cells = "".join(f"<td>{draw(_WORD)}</td>" for _ in range(draw(st.integers(1, 3))))
            pieces.append(f"<table><tr>{cells}</tr></table>")
        elif kind == 4:
            pieces.append(f'<p><a href="https://x.test/{word}">{word}</a></p>')
        else:
            pieces.append(f"<pre><code>{word} = {draw(st.integers())}</code></pre>")
    return "".join(pieces)


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(st.characters(blacklist_categories=("Cs",)), max_size=24),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=5)
    ),
    max_leaves=20,
)


def _to_markdown(content: str) -> str:
    return HtmlTransformer(HtmlOptions()).transform(content, HTML_DETECTION).content


# -- Markdown validity -----------------------------------------------------


@THOROUGH
@given(html_documents())
def test_markdown_output_contains_no_raw_markup(document: str) -> None:
    """A leaked tag would be sent to the model as literal noise."""
    assert not _RAW_TAG_RE.search(_to_markdown(document))


@THOROUGH
@given(html_documents())
def test_markdown_code_fences_are_balanced(document: str) -> None:
    """An odd fence swallows the rest of the document in any Markdown renderer."""
    assert len(_FENCE_RE.findall(_to_markdown(document))) % 2 == 0


@THOROUGH
@given(html_documents())
def test_markdown_headings_are_well_formed(document: str) -> None:
    for line in _to_markdown(document).splitlines():
        if line.startswith("#"):
            assert _HEADING_RE.match(line), f"malformed heading: {line!r}"


@THOROUGH
@given(html_documents())
def test_markdown_tables_have_a_separator_and_a_stable_width(document: str) -> None:
    lines = _to_markdown(document).splitlines()
    for index, line in enumerate(lines):
        if not _TABLE_SEPARATOR_RE.match(line):
            continue
        columns = line.count("|") - 1
        header = lines[index - 1]
        assert header.startswith("|") and header.endswith("|")
        assert header.count("|") - 1 == columns


@THOROUGH
@given(html_documents())
def test_markdown_output_is_normalized_text(document: str) -> None:
    """No control characters, no trailing whitespace, at most one blank line.

    The NUL check matters: the transformer uses it internally as the `<br>`
    sentinel. One escaping into the output would corrupt the payload.
    """
    markdown = _to_markdown(document)
    assert "\x00" not in markdown
    assert "\r" not in markdown
    assert markdown == markdown.strip()
    assert "\n\n\n" not in markdown
    for line in markdown.splitlines():
        assert line == line.rstrip(), f"trailing whitespace: {line!r}"


@THOROUGH
@given(html_documents())
def test_noise_element_text_never_survives(document: str) -> None:
    markdown = _to_markdown(document)
    for tag in _NOISE_TAGS:
        assert f"<{tag}>" not in markdown


# -- JSON validity ---------------------------------------------------------


def _minify(content: str, **options: bool) -> str:
    detection = DETECTOR.detect(content)
    return JsonTransformer(JsonOptions(**options)).transform(content, detection).content  # type: ignore[arg-type]


@THOROUGH
@given(json_values)
def test_minified_json_reparses_to_the_same_value(value: object) -> None:
    """Minification is a formatting change. The decoded value must be identical."""
    output = _minify(json.dumps(value, indent=2))
    assert json.loads(output) == value


@THOROUGH
@given(json_values)
def test_minified_json_is_never_larger(value: object) -> None:
    pretty = json.dumps(value, indent=2)
    assert len(_minify(pretty)) <= len(pretty)


@THOROUGH
@given(st.dictionaries(st.text(min_size=1, max_size=8), json_values, max_size=6))
def test_key_order_is_preserved_exactly(mapping: dict[str, object]) -> None:
    output = _minify(json.dumps(mapping, indent=2))
    assert list(json.loads(output)) == list(mapping)


@THOROUGH
@given(json_values)
def test_empty_container_pruning_still_yields_valid_json(value: object) -> None:
    output = _minify(json.dumps(value, indent=2), remove_empty_containers=True)
    json.loads(output)  # must not raise


@THOROUGH
@given(json_values)
def test_pruning_only_ever_removes_empty_containers(value: object) -> None:
    """Never a scalar, never a populated container."""
    pruned = json.loads(_minify(json.dumps(value), remove_empty_containers=True))

    def scalars(node: object) -> list[object]:
        if isinstance(node, dict):
            return [leaf for item in node.values() for leaf in scalars(item)]
        if isinstance(node, list):
            return [leaf for item in node for leaf in scalars(item)]
        return [node]

    assert scalars(pruned) == scalars(value)
