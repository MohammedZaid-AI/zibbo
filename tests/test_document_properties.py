"""Property-based invariants for document extraction.

The safety property is the one that matters most: **no input, however hostile,
makes an extractor raise.** The pipeline reads a raised exception as a crash, not as
"leave it alone", so an extractor that can be made to throw is a request that can be
made to fail. These tests fire thousands of random byte strings at every extractor
and assert it always returns cleanly.
"""

from __future__ import annotations

import csv
import io

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gateway.documents import DocumentFormat, build_document_registry, detect_format
from gateway.documents.convert import csv_to_markdown, xml_to_markdown

pytestmark = pytest.mark.property

THOROUGH = settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

REGISTRY = build_document_registry()
ALL_FORMATS = [f for f in DocumentFormat if f is not DocumentFormat.UNKNOWN]


# -- Nothing crashes --------------------------------------------------------


@THOROUGH
@given(st.binary(max_size=2048), st.sampled_from(ALL_FORMATS))
def test_no_random_bytes_ever_make_an_extractor_raise(data: bytes, fmt: DocumentFormat) -> None:
    extractor = REGISTRY.for_format(fmt)
    if extractor is None:
        return
    result = extractor.extract(data, fmt)  # must not raise
    assert result.markdown is None or isinstance(result.markdown, str)


@THOROUGH
@given(st.binary(max_size=4096), st.text(max_size=40), st.text(max_size=40))
def test_detection_never_raises(data: bytes, media_type: str, filename: str) -> None:
    fmt = detect_format(data, media_type=media_type, filename=filename)
    assert isinstance(fmt, DocumentFormat)


# -- CSV converter ----------------------------------------------------------

# Cells exclude the characters the converter treats structurally: the candidate
# delimiters (a cell containing ';' would make the sniffer pick ';'), quotes, and
# the pipe used for Markdown escaping. Delimiter ambiguity in single-column data is
# a documented limitation, tested separately, not a preservation failure.
_cell = st.text(
    st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters=",;\t|\"'\\"),
    max_size=12,
)
_table = st.lists(st.lists(_cell, min_size=1, max_size=5), min_size=2, max_size=8)


@st.composite
def csv_text(draw: st.DrawFn) -> str:
    rows = draw(_table)
    width = len(rows[0])
    rows = [row + [""] * (width - len(row)) for row in rows]  # rectangular
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


@THOROUGH
@given(csv_text())
def test_csv_conversion_is_deterministic(text: str) -> None:
    assert csv_to_markdown(text) == csv_to_markdown(text)


@THOROUGH
@given(csv_text())
def test_csv_values_are_preserved(text: str) -> None:
    """Every non-empty cell value survives into the output, escaped but unaltered.

    This is the "never change data values" guarantee. Combined with determinism and
    the never-larger property, it pins the converter to *reformatting* rather than
    editing: nothing a row contained is dropped or rewritten.
    """
    markdown = csv_to_markdown(text)
    if markdown is None:
        return
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows:
        for cell in row:
            value = cell.strip()
            if value:
                escaped = value.replace("|", r"\|")
                assert escaped in markdown, f"value {value!r} was lost"


# -- XML converter ----------------------------------------------------------

_xml_name = st.text("abcdefghijklmnop", min_size=1, max_size=6)
_xml_value = st.text(st.characters(whitelist_categories=("Lu", "Ll", "Nd")), max_size=12)


@st.composite
def xml_text(draw: st.DrawFn) -> str:
    depth = draw(st.integers(min_value=1, max_value=4))

    def element(level: int) -> str:
        tag = draw(_xml_name)
        if level >= depth:
            return f"<{tag}>{draw(_xml_value)}</{tag}>"
        children = "".join(element(level + 1) for _ in range(draw(st.integers(1, 3))))
        return f"<{tag}>{children}</{tag}>"

    return f"<?xml version='1.0'?>{element(0)}"


@THOROUGH
@given(xml_text())
def test_xml_conversion_is_deterministic(text: str) -> None:
    assert xml_to_markdown(text) == xml_to_markdown(text)


@THOROUGH
@given(xml_text())
def test_xml_conversion_produces_no_raw_tags(text: str) -> None:
    markdown = xml_to_markdown(text)
    if markdown is None:
        return
    # The whole point is to strip the angle-bracket noise.
    assert "</" not in markdown


@THOROUGH
@given(st.binary(max_size=1024))
def test_xml_converter_never_raises_on_bytes_pretending_to_be_xml(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    result = xml_to_markdown(text)
    assert result is None or isinstance(result, str)
