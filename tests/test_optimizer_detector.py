"""Content detection.

The load-bearing claim: the body wins over the header. A user pastes a scraped web
page into a chat message; nothing in the transport says "HTML", and the detector
must work it out anyway.
"""

from __future__ import annotations

import pytest

from gateway.optimizers.detector import (
    ContentDetector,
    HtmlSniffer,
    JsonSniffer,
    MagicBytesSniffer,
    PlainTextSniffer,
    PromptSniffer,
    XmlSniffer,
    content_type_from_mime,
)
from gateway.optimizers.models import ContentType


@pytest.fixture
def detector() -> ContentDetector:
    return ContentDetector()


# -- The point of the whole module -----------------------------------------


def test_html_is_detected_despite_a_wrong_content_type(detector: ContentDetector) -> None:
    detection = detector.detect(
        "<div class='x'><p>Hello</p></div>", declared_mime="application/json"
    )
    assert detection.content_type is ContentType.HTML


def test_json_is_detected_despite_a_wrong_content_type(detector: ContentDetector) -> None:
    detection = detector.detect('{"a": 1}', declared_mime="text/html")
    assert detection.content_type is ContentType.JSON


def test_declared_type_is_used_when_the_body_is_inconclusive(detector: ContentDetector) -> None:
    """Prose sniffs only as low-confidence text, so the header breaks the tie."""
    detection = detector.detect("just some words", declared_mime="text/csv")
    assert detection.content_type is ContentType.CSV
    assert detection.source == "content-type-header"


def test_plain_text_is_the_fallback(detector: ContentDetector) -> None:
    detection = detector.detect("just some words")
    assert detection.content_type is ContentType.TEXT


# -- HTML sniffing ---------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "<!DOCTYPE html><html></html>",
        "<html><body><p>hi</p></body></html>",
        "<div><span>x</span></div>",
        "<table><tr><td>1</td></tr></table>",
        "<p>one</p><p>two</p>",
    ],
)
def test_html_is_recognized(detector: ContentDetector, content: str) -> None:
    assert detector.detect(content).content_type is ContentType.HTML


@pytest.mark.parametrize(
    "content",
    [
        "# Heading\n\nA paragraph with **bold** text.\n\n- item\n- item",
        "Use the <div> element to group content.",  # one tag, discussed not markup
        "a < b and c > d",
        "| col | col |\n|---|---|\n| 1 | 2 |",
        "```\ncode\n```",
    ],
)
def test_markdown_and_prose_are_not_html(detector: ContentDetector, content: str) -> None:
    """Critical: the HTML transformer emits Markdown. If Markdown were re-detected
    as HTML, running the pipeline twice would not be a no-op."""
    assert detector.detect(content).content_type is not ContentType.HTML


@pytest.mark.parametrize(
    "content",
    [
        "What does <p>hello</p> do in HTML?",
        "The <br> tag inserts a line break.",
        "Wrap it in a <div> and you're done.",
    ],
)
def test_prose_discussing_markup_is_not_treated_as_markup(
    detector: ContentDetector, content: str
) -> None:
    """A single closing tag is someone writing *about* HTML.

    Detecting this as HTML would rewrite the user's question into "What does hello
    do in HTML?" — silently destroying their meaning. That is the failure this
    pipeline exists to avoid.
    """
    assert detector.detect(content).content_type is not ContentType.HTML


def test_doctype_alone_is_enough() -> None:
    detection = HtmlSniffer().sniff("<!doctype html>")
    assert detection is not None
    assert detection.confidence >= 0.95


def test_a_document_element_alone_is_enough() -> None:
    detection = HtmlSniffer().sniff("<html><p>hi")
    assert detection is not None
    assert detection.content_type is ContentType.HTML


def test_a_single_unclosed_tag_is_not_enough() -> None:
    assert HtmlSniffer().sniff("<div>") is None


def test_two_closing_tags_are_enough() -> None:
    detection = HtmlSniffer().sniff("<p>one</p><p>two</p>")
    assert detection is not None
    assert detection.content_type is ContentType.HTML


def test_confidence_grows_with_tag_variety() -> None:
    few = HtmlSniffer().sniff("<div><p>x</p></div>")
    many = HtmlSniffer().sniff(
        "<section><div><p><a href='#'>x</a></p><ul><li>y</li></ul></div></section>"
    )
    assert few is not None
    assert many is not None
    assert many.confidence > few.confidence


# -- JSON sniffing ---------------------------------------------------------


def test_json_sniffer_carries_the_parse_forward() -> None:
    """Reused by the JSON transformer so a large payload is parsed exactly once."""
    detection = JsonSniffer().sniff('{"a": [1, 2]}')
    assert detection is not None
    assert detection.parsed == {"a": [1, 2]}


def test_json_sniffer_counts_duplicate_keys() -> None:
    detection = JsonSniffer().sniff('{"a": 1, "a": 2, "b": 3}')
    assert detection is not None
    assert detection.metadata["duplicate_keys"] == 1
    assert detection.parsed == {"a": 2, "b": 3}, "last occurrence wins, as json.loads does"


@pytest.mark.parametrize(
    "content",
    ["{not json", "", "  ", "plain text", "{'single': 'quotes'}", "{", "123", '"a string"'],
)
def test_json_sniffer_rejects_non_objects(content: str) -> None:
    assert JsonSniffer().sniff(content) is None


def test_json_arrays_are_detected() -> None:
    detection = JsonSniffer().sniff("[1, 2, 3]")
    assert detection is not None
    assert detection.parsed == [1, 2, 3]


def test_prose_never_reaches_the_json_parser() -> None:
    """The cheap first-character gate matters: prose must not be handed to json.loads."""
    assert JsonSniffer().sniff("a" * 10_000) is None


# -- Other sniffers --------------------------------------------------------


def test_xml_declaration_is_detected() -> None:
    detection = XmlSniffer().sniff('<?xml version="1.0"?><root/>')
    assert detection is not None
    assert detection.content_type is ContentType.XML


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("%PDF-1.7", ContentType.PDF),
        ("PK\x03\x04", ContentType.DOCX),
        ("\x89PNG\r\n", ContentType.IMAGE),
        ("\xff\xd8\xff", ContentType.IMAGE),
    ],
)
def test_magic_bytes_are_recognized(prefix: str, expected: ContentType) -> None:
    """Wired up now so Phase 7 only extends a table."""
    detection = MagicBytesSniffer().sniff(prefix + "rest of file")
    assert detection is not None
    assert detection.content_type is expected


def test_plain_text_sniffer_always_matches_but_loses() -> None:
    detection = PlainTextSniffer().sniff("anything")
    assert detection is not None
    assert detection.confidence < 0.7


# -- MIME mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("application/json", ContentType.JSON),
        ("application/json; charset=utf-8", ContentType.JSON),
        ("TEXT/HTML", ContentType.HTML),
        ("image/png", ContentType.IMAGE),
        ("image/anything", ContentType.IMAGE),
        ("multipart/form-data; boundary=x", ContentType.BINARY),
        ("application/pdf", ContentType.PDF),
        ("", None),
        (None, None),
        ("application/vnd.unknown", None),
    ],
)
def test_mime_mapping(mime: str | None, expected: ContentType | None) -> None:
    assert content_type_from_mime(mime) is expected


def test_detector_is_extensible_via_sniffers() -> None:
    """Phase 7 adds a sniffer; the detector itself does not change."""

    class AlwaysCsv:
        name = "always-csv"

        def sniff(self, content: str):
            from gateway.optimizers.models import Detection

            return Detection(ContentType.CSV, 1.0, self.name)

    detector = ContentDetector([AlwaysCsv(), PlainTextSniffer()])
    assert detector.detect("<html><body><p>x</p></body></html>").content_type is ContentType.CSV


def test_prompt_sniffer_refuses_a_stack_trace_with_repeated_frames() -> None:
    """Regression: a recursion traceback (identical frames) must never be classified PROMPT.

    The prompt optimizer collapses byte-identical blocks; misclassifying a trace let it
    remove stack frames, corrupting the trace and breaking the documented guarantee that
    stack traces are forwarded verbatim. A traceback header is now refused outright.
    """
    sniffer = PromptSniffer(min_chars=1, min_duplicate_ratio=0.15)
    recursion = (
        "Traceback (most recent call last):\n\n"
        + "\n\n".join('  File "app.py", line 5, in recurse\n    recurse()' for _ in range(6))
        + "\n\nRecursionError: maximum recursion depth exceeded"
    )
    assert sniffer.sniff(recursion) is None
