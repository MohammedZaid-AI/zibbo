"""Unit tests for the three transformers.

The recurring theme: prove that noise is removed *and* that meaning is not.
"""

from __future__ import annotations

import json

import pytest

from gateway.optimizers.models import ContentType, Detection
from gateway.optimizers.options import HtmlOptions, JsonOptions, TextOptions
from gateway.optimizers.transformers import HtmlTransformer, JsonTransformer, TextTransformer

HTML_DETECTION = Detection(ContentType.HTML, 1.0, "test")
TEXT_DETECTION = Detection(ContentType.TEXT, 1.0, "test")


def _html(content: str, **kwargs: object) -> str:
    transformer = HtmlTransformer(HtmlOptions(**kwargs))  # type: ignore[arg-type]
    return transformer.transform(content, HTML_DETECTION).content


def _json_out(content: str, **kwargs: object) -> str:
    from gateway.optimizers.detector import JsonSniffer

    detection = JsonSniffer().sniff(content)
    assert detection is not None
    return JsonTransformer(JsonOptions(**kwargs)).transform(content, detection).content  # type: ignore[arg-type]


def _text(content: str, **kwargs: object) -> str:
    return TextTransformer(TextOptions(**kwargs)).transform(content, TEXT_DETECTION).content  # type: ignore[arg-type]


# ===========================================================================
# HTML
# ===========================================================================


@pytest.mark.parametrize(
    ("markup", "removed"),
    [
        ("<p>keep</p><script>evil()</script>", "evil"),
        ("<p>keep</p><style>.a{color:red}</style>", "color:red"),
        ("<p>keep</p><svg><path d='M0'/></svg>", "M0"),
        ("<p>keep</p><nav><a href='/'>Home</a></nav>", "Home"),
        ("<p>keep</p><footer>Copyright 2026</footer>", "Copyright"),
        ("<p>keep</p><aside>Related</aside>", "Related"),
        ("<p>keep</p><iframe src='x'>frame</iframe>", "frame"),
        ("<p>keep</p><noscript>enable js</noscript>", "enable js"),
    ],
)
def test_structural_noise_is_removed(markup: str, removed: str) -> None:
    output = _html(markup)
    assert "keep" in output
    assert removed not in output


@pytest.mark.parametrize(
    "markup",
    [
        "<div class='cookie-banner'>Accept cookies</div><p>keep</p>",
        "<div class='gdpr-consent'>Accept cookies</div><p>keep</p>",
        "<div id='cookie_notice'>Accept cookies</div><p>keep</p>",
        "<div role='dialog'>Accept cookies</div><p>keep</p>",
    ],
)
def test_cookie_banners_are_removed(markup: str) -> None:
    output = _html(markup)
    assert "Accept cookies" not in output
    assert "keep" in output


@pytest.mark.parametrize(
    "markup",
    [
        "<div class='ad-slot'>BUY</div><p>keep</p>",
        "<div class='advertisement'>BUY</div><p>keep</p>",
        "<div id='adsense-top'>BUY</div><p>keep</p>",
        "<div class='sponsored-content'>BUY</div><p>keep</p>",
    ],
)
def test_advertisements_are_removed(markup: str) -> None:
    output = _html(markup)
    assert "BUY" not in output
    assert "keep" in output


@pytest.mark.parametrize(
    "markup",
    [
        "<span style='display:none'>ghost</span><p>keep</p>",
        "<span style='visibility: hidden'>ghost</span><p>keep</p>",
        "<span hidden>ghost</span><p>keep</p>",
        "<span aria-hidden='true'>ghost</span><p>keep</p>",
        "<input type='hidden' value='ghost'><p>keep</p>",
    ],
)
def test_hidden_elements_are_removed(markup: str) -> None:
    output = _html(markup)
    assert "ghost" not in output
    assert "keep" in output


def test_word_boundaries_prevent_false_positives() -> None:
    """`ad` must not match `download`, `header`, `gradient`, or `loading`."""
    markup = (
        "<div class='download-section'><p>Download the file</p></div>"
        "<div class='gradient'><p>Gradient info</p></div>"
        "<div class='loading-state'><p>Loading data</p></div>"
    )
    output = _html(markup)
    assert "Download the file" in output
    assert "Gradient info" in output
    assert "Loading data" in output


def test_header_inside_an_article_is_a_byline_not_chrome() -> None:
    markup = (
        "<header><a href='/'>Site nav</a></header>"
        "<article><header><h1>Title</h1><p>By Jane</p></header><p>Body</p></article>"
    )
    output = _html(markup)
    assert "Site nav" not in output
    assert "By Jane" in output
    assert "Title" in output


# -- Markdown conversion ---------------------------------------------------


def test_headings_become_hashes() -> None:
    output = _html("<h1>One</h1><h2>Two</h2><h3>Three</h3>")
    assert output == "# One\n\n## Two\n\n### Three"


def test_paragraphs_are_separated_by_blank_lines() -> None:
    assert _html("<p>a</p><p>b</p>") == "a\n\nb"


def test_whitespace_within_a_paragraph_is_collapsed() -> None:
    assert _html("<p>a   b\n\n\tc</p>") == "a b c"


def test_unordered_lists() -> None:
    assert _html("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"


def test_ordered_lists_are_numbered() -> None:
    assert _html("<ol><li>a</li><li>b</li></ol>") == "1. a\n2. b"


def test_nested_lists_are_indented() -> None:
    output = _html("<ul><li>a<ul><li>b</li></ul></li></ul>")
    assert output == "- a\n  - b"


def test_tables_become_markdown_tables() -> None:
    output = _html("<table><tr><th>H1</th><th>H2</th></tr><tr><td>a</td><td>b</td></tr></table>")
    assert output == "| H1 | H2 |\n|---|---|\n| a | b |"


def test_ragged_tables_are_padded() -> None:
    output = _html("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>")
    assert output.splitlines()[-1] == "| 1 |  |"


def test_pipes_in_cells_are_escaped() -> None:
    output = _html("<table><tr><td>a|b</td></tr></table>")
    assert r"a\|b" in output


def test_code_blocks_are_fenced() -> None:
    assert _html("<pre><code>x = 1\ny = 2</code></pre>") == "```\nx = 1\ny = 2\n```"


def test_code_indentation_is_preserved() -> None:
    """Collapsing whitespace inside <pre> would change what the code means."""
    output = _html("<pre>def f():\n    return 1</pre>")
    assert "    return 1" in output


def test_inline_emphasis_and_code() -> None:
    output = _html("<p><strong>b</strong> <em>i</em> <code>c</code></p>")
    assert output == "**b** *i* `c`"


def test_links_are_preserved_by_default() -> None:
    assert _html("<p><a href='https://x.test/a'>text</a></p>") == "[text](https://x.test/a)"


def test_links_can_be_reduced_to_their_text() -> None:
    assert _html("<p><a href='https://x.test/a'>text</a></p>", preserve_links=False) == "text"


@pytest.mark.parametrize("href", ["#anchor", "javascript:alert(1)", "/relative"])
def test_valueless_hrefs_are_dropped_but_text_survives(href: str) -> None:
    assert _html(f"<p><a href='{href}'>text</a></p>") == "text"


def test_images_keep_alt_and_src() -> None:
    assert (
        _html("<p><img src='https://x.test/a.png' alt='A cat'></p>")
        == "![A cat](https://x.test/a.png)"
    )


def test_data_uri_images_drop_the_payload_and_keep_the_alt() -> None:
    huge = "data:image/png;base64," + "A" * 5000
    output = _html(f"<p><img src='{huge}' alt='A cat'></p>")
    assert output == "A cat"
    assert "base64" not in output


def test_br_becomes_a_newline() -> None:
    assert _html("<p>a<br>b</p>") == "a\nb"


def test_blockquotes_are_prefixed() -> None:
    assert _html("<blockquote><p>quoted</p></blockquote>") == "> quoted"


def test_horizontal_rules_survive() -> None:
    assert _html("<p>a</p><hr><p>b</p>") == "a\n\n---\n\nb"


def test_document_title_is_preserved_when_there_is_no_h1() -> None:
    output = _html("<html><head><title>Doc</title></head><body><p>x</p></body></html>")
    assert output == "# Doc\n\nx"


def test_document_title_is_not_duplicated_when_an_h1_exists() -> None:
    output = _html("<html><head><title>Doc</title></head><body><h1>Doc</h1><p>x</p></body></html>")
    assert output == "# Doc\n\nx"


def test_entities_are_decoded_once() -> None:
    assert _html("<p>a &amp; b &lt; c</p>") == "a & b < c"


def test_unicode_survives() -> None:
    assert _html("<p>héllo — 世界</p>") == "héllo — 世界"


def test_nul_bytes_cannot_forge_a_line_break() -> None:
    """NUL is the internal sentinel for <br>. Content must not be able to inject one."""
    assert "\n" not in _html("<p>a\x00b</p>")


def test_unparseable_html_is_returned_untouched() -> None:
    transformer = HtmlTransformer()
    assert transformer.transform("", HTML_DETECTION).content == ""


def test_markdown_input_falls_through_to_text_normalization() -> None:
    """The guard that makes idempotency exact."""
    markdown = "# Title\n\nSome text.\n\n- a\n- b"
    assert _html(markdown) == markdown


def test_html_transform_reports_the_steps_it_took() -> None:
    output = HtmlTransformer().transform("<p>x</p><script>y()</script>", HTML_DETECTION)
    assert "removed_scripts" in output.steps
    assert "converted_to_markdown" in output.steps


# ===========================================================================
# JSON
# ===========================================================================


def test_pretty_printed_json_is_minified() -> None:
    assert _json_out('{\n  "a": 1,\n  "b": [1, 2]\n}') == '{"a":1,"b":[1,2]}'


def test_key_order_is_preserved() -> None:
    assert _json_out('{"z": 1, "a": 2, "m": 3}') == '{"z":1,"a":2,"m":3}'


def test_values_are_never_altered() -> None:
    source = '{"n": 1.5, "s": "  spaced  ", "b": true, "nul": null, "neg": -0.0}'
    assert json.loads(_json_out(source)) == json.loads(source)


def test_non_ascii_is_emitted_directly_not_escaped() -> None:
    """`é` is one token; `\\u00e9` is several."""
    assert _json_out('{"a": "é"}') == '{"a":"é"}'


def test_duplicate_keys_collapse_to_the_last_and_are_reported() -> None:
    from gateway.optimizers.detector import JsonSniffer

    source = '{"a": 1, "a": 2}'
    detection = JsonSniffer().sniff(source)
    assert detection is not None
    output = JsonTransformer().transform(source, detection)
    assert output.content == '{"a":2}'
    assert "collapsed_duplicate_keys" in output.steps


def test_empty_containers_are_kept_by_default() -> None:
    """`{"tools": []}` does not mean the same thing as `{}`."""
    assert _json_out('{"tools": [], "meta": {}}') == '{"tools":[],"meta":{}}'


def test_empty_containers_can_be_removed_on_request() -> None:
    assert _json_out('{"a": 1, "b": [], "c": {}}', remove_empty_containers=True) == '{"a":1}'


def test_empty_container_removal_is_recursive_bottom_up() -> None:
    assert _json_out('{"a": {"b": []}}', remove_empty_containers=True) == "{}"


def test_already_minified_json_reports_no_change() -> None:
    from gateway.optimizers.detector import JsonSniffer

    source = '{"a":1}'
    detection = JsonSniffer().sniff(source)
    assert detection is not None
    assert JsonTransformer().transform(source, detection).steps == ()


def test_json_transformer_reuses_the_detectors_parse() -> None:
    """No `parsed` on the detection would mean a second, redundant parse."""
    detection = Detection(ContentType.JSON, 1.0, "test", parsed={"a": 1})
    assert JsonTransformer().transform("ignored", detection).content == '{"a":1}'


def test_malformed_json_is_returned_untouched() -> None:
    detection = Detection(ContentType.JSON, 1.0, "test")
    assert JsonTransformer().transform("{bad", detection).content == "{bad"


# ===========================================================================
# Plain text
# ===========================================================================


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("a   \nb", "a\nb"),
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("\n\n  a  \n\n", "a"),
        ("a\t \nb", "a\nb"),
    ],
)
def test_safe_normalizations(source: str, expected: str) -> None:
    assert _text(source) == expected


def test_consecutive_duplicate_paragraphs_are_removed() -> None:
    assert _text("same\n\nsame\n\nother") == "same\n\nother"


def test_non_consecutive_duplicates_are_kept() -> None:
    """A phrase recurring later in a document is content, not boilerplate."""
    assert _text("a\n\nb\n\na") == "a\n\nb\n\na"


def test_paragraph_dedupe_can_be_disabled() -> None:
    assert _text("same\n\nsame", dedupe_consecutive_paragraphs=False) == "same\n\nsame"


def test_inline_whitespace_is_preserved_by_default() -> None:
    """Indentation is meaning in code; alignment is meaning in tables."""
    assert _text("def f():\n    return 1") == "def f():\n    return 1"


def test_inline_whitespace_can_be_collapsed_on_request() -> None:
    assert _text("a    b", collapse_inline_whitespace=True) == "a b"


def test_single_blank_line_between_paragraphs_is_kept() -> None:
    assert _text("a\n\nb") == "a\n\nb"


def test_unchanged_text_reports_no_steps() -> None:
    assert TextTransformer().transform("a\n\nb", TEXT_DETECTION).steps == ()


def test_text_transformer_never_changes_words() -> None:
    source = "The quick brown fox.\n\nJumped over the lazy dog."
    assert _text(source) == source
