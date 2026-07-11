"""The extractors themselves: does each format become the Markdown it should?

And, at least as important, the safety half: a malformed, encrypted or corrupt
document must yield *nothing* and never raise, because the pipeline reads "nothing"
as "forward the original untouched".
"""

from __future__ import annotations

import pytest

from gateway.documents import DocumentFormat, build_document_registry
from gateway.documents.registry import DocumentExtractorRegistry
from tests.mocks.documents import make_docx, make_docx_with_hyperlink, make_pdf


@pytest.fixture(scope="module")
def registry() -> DocumentExtractorRegistry:
    return build_document_registry()


def _extract(registry: DocumentExtractorRegistry, data: bytes, fmt: DocumentFormat) -> str | None:
    extractor = registry.for_format(fmt)
    assert extractor is not None
    return extractor.extract(data, fmt).markdown


# ===========================================================================
# PDF
# ===========================================================================


def test_pdf_headings_and_paragraphs(registry: DocumentExtractorRegistry) -> None:
    pdf = make_pdf([[("Big Title", 24), ("A line of body text.", 11), ("Another line.", 11)]])
    markdown = _extract(registry, pdf, DocumentFormat.PDF)
    assert markdown is not None
    assert markdown.startswith("# Big Title")
    assert "A line of body text. Another line." in markdown


def test_pdf_preserves_reading_order_across_pages(registry: DocumentExtractorRegistry) -> None:
    pdf = make_pdf([[("First page text.", 11)], [("Second page text.", 11)]])
    markdown = _extract(registry, pdf, DocumentFormat.PDF)
    assert markdown is not None
    assert markdown.index("First page") < markdown.index("Second page")


def test_pdf_drops_running_headers_and_footers(registry: DocumentExtractorRegistry) -> None:
    """A line repeated in the margin of every page is chrome, not content."""
    pages = [
        [("CONFIDENTIAL REPORT", 9), (f"Body of page {n}.", 11), ("Page footer text", 9)]
        for n in range(1, 6)
    ]
    pdf = make_pdf(pages)
    markdown = _extract(registry, pdf, DocumentFormat.PDF)
    assert markdown is not None
    assert "Body of page 1." in markdown
    assert markdown.count("CONFIDENTIAL REPORT") == 0
    assert markdown.count("Page footer text") == 0


def test_an_encrypted_pdf_yields_nothing(registry: DocumentExtractorRegistry) -> None:
    """It must be a clean None, not an exception — the request still forwards."""
    pdf = make_pdf([[("Secret", 12)]], encrypt="owner-password")
    assert _extract(registry, pdf, DocumentFormat.PDF) is None


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.4 this is not really a pdf",
        b"%PDF-",
        b"not a pdf at all",
        b"",
    ],
)
def test_corrupt_pdf_yields_nothing_and_does_not_raise(
    registry: DocumentExtractorRegistry, data: bytes
) -> None:
    assert _extract(registry, data, DocumentFormat.PDF) is None


# ===========================================================================
# DOCX
# ===========================================================================


def test_docx_headings_paragraphs_and_lists(registry: DocumentExtractorRegistry) -> None:
    docx = make_docx(
        [
            ("Heading 1", "Report"),
            ("normal", "Introductory paragraph."),
            ("Heading 2", "Findings"),
            ("List Bullet", "First point"),
            ("List Bullet", "Second point"),
        ]
    )
    markdown = _extract(registry, docx, DocumentFormat.DOCX)
    assert markdown is not None
    assert "# Report" in markdown
    assert "## Findings" in markdown
    assert "- First point" in markdown
    assert "Introductory paragraph." in markdown


def test_docx_tables_appear_in_document_order(registry: DocumentExtractorRegistry) -> None:
    docx = make_docx(
        [("Heading 1", "Data"), ("normal", "Before the table.")],
        table=[["Metric", "Value"], ["Revenue", "100"]],
    )
    markdown = _extract(registry, docx, DocumentFormat.DOCX)
    assert markdown is not None
    assert markdown.index("Before the table.") < markdown.index("| Metric | Value |")
    assert "| Revenue | 100 |" in markdown


def test_docx_hyperlinks_become_markdown_links(registry: DocumentExtractorRegistry) -> None:
    docx = make_docx_with_hyperlink("the docs", "https://example.test/docs")
    markdown = _extract(registry, docx, DocumentFormat.DOCX)
    assert markdown is not None
    assert "[the docs](https://example.test/docs)" in markdown


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04 corrupt zip body",
        b"not a docx",
        b"",
    ],
)
def test_corrupt_docx_yields_nothing_and_does_not_raise(
    registry: DocumentExtractorRegistry, data: bytes
) -> None:
    assert _extract(registry, data, DocumentFormat.DOCX) is None


# ===========================================================================
# CSV
# ===========================================================================


def test_small_csv_becomes_a_markdown_table(registry: DocumentExtractorRegistry) -> None:
    markdown = _extract(registry, b"name,city\nAda,London\nGrace,Paris", DocumentFormat.CSV)
    assert markdown == "| name | city |\n|---|---|\n| Ada | London |\n| Grace | Paris |"


def test_large_csv_becomes_compact_fenced_csv(registry: DocumentExtractorRegistry) -> None:
    """A table too tall for a Markdown table stays as cleaned CSV — never larger."""
    rows = "\n".join(f"{n},value{n},extra{n}" for n in range(80))
    data = ("id,name,note\n" + rows).encode()
    markdown = _extract(registry, data, DocumentFormat.CSV)
    assert markdown is not None
    assert markdown.startswith("```csv\n")
    assert markdown.endswith("\n```")
    assert "| id | name | note |" not in markdown  # not the padded table form
    assert "id,name,note" in markdown
    assert "0,value0,extra0" in markdown
    # The compact form must not be larger than the input it replaced.
    assert len(markdown) <= len(data) + 20


def test_csv_drops_wholly_empty_rows_and_columns(registry: DocumentExtractorRegistry) -> None:
    """A fully empty column (no header, no data) is dropped; blank rows are dropped."""
    markdown = _extract(registry, b"a,,b\n1,,2\n\n3,,4", DocumentFormat.CSV)
    assert markdown == "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"


def test_csv_keeps_a_labelled_column_even_with_no_data(
    registry: DocumentExtractorRegistry,
) -> None:
    """A header is content: dropping it would lose a value the user provided."""
    markdown = _extract(registry, b"a,notes,b\n1,,2", DocumentFormat.CSV)
    assert markdown is not None
    assert "notes" in markdown


def test_csv_never_changes_a_value(registry: DocumentExtractorRegistry) -> None:
    markdown = _extract(
        registry, b"amount,note\n-0.50,paid in full\n1e3,scientific", DocumentFormat.CSV
    )
    assert markdown is not None
    assert "-0.50" in markdown
    assert "1e3" in markdown
    assert "paid in full" in markdown


def test_tsv_is_detected_by_delimiter(registry: DocumentExtractorRegistry) -> None:
    markdown = _extract(registry, b"a\tb\n1\t2", DocumentFormat.CSV)
    assert markdown == "| a | b |\n|---|---|\n| 1 | 2 |"


# ===========================================================================
# XML
# ===========================================================================


def test_xml_hierarchy_becomes_nested_markdown(registry: DocumentExtractorRegistry) -> None:
    data = b"<?xml version='1.0'?><catalog><book id='1'><title>Python</title></book></catalog>"
    markdown = _extract(registry, data, DocumentFormat.XML)
    assert markdown is not None
    assert "# catalog" in markdown
    assert "book" in markdown
    assert "id=1" in markdown
    assert "Python" in markdown


def test_xml_attributes_are_preserved(registry: DocumentExtractorRegistry) -> None:
    data = b"<item sku='ABC-123' price='9.99'>Widget</item>"
    markdown = _extract(registry, data, DocumentFormat.XML)
    assert markdown is not None
    assert "sku=ABC-123" in markdown
    assert "price=9.99" in markdown


def test_malformed_xml_is_recovered_not_rejected(registry: DocumentExtractorRegistry) -> None:
    """lxml's recovering parser salvages real-world exports that are not well-formed."""
    markdown = _extract(registry, b"<root><unclosed>text</root>", DocumentFormat.XML)
    assert markdown is not None
    assert "text" in markdown


def test_xml_entities_do_not_expand_external_resources(registry: DocumentExtractorRegistry) -> None:
    """A billion-laughs / XXE payload must not be resolved."""
    xxe = (
        b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><r>&xxe;</r>"
    )
    markdown = _extract(registry, xxe, DocumentFormat.XML)
    # Whatever comes out, it must not contain a resolved file.
    assert markdown is None or "root:" not in markdown


# ===========================================================================
# HTML, Markdown, plain text
# ===========================================================================


def test_html_document_becomes_markdown(registry: DocumentExtractorRegistry) -> None:
    data = b"<html><body><nav>menu</nav><h1>Title</h1><p>Body.</p></body></html>"
    markdown = _extract(registry, data, DocumentFormat.HTML)
    assert markdown is not None
    assert "# Title" in markdown
    assert "menu" not in markdown


def test_markdown_is_only_normalized(registry: DocumentExtractorRegistry) -> None:
    data = b"# Title\n\n\n\nSome   text.   \n\n\n"
    markdown = _extract(registry, data, DocumentFormat.MARKDOWN)
    assert markdown == "# Title\n\nSome   text."


def test_utf16_is_decoded(registry: DocumentExtractorRegistry) -> None:
    data = "Héllo wörld".encode("utf-16")
    markdown = _extract(registry, data, DocumentFormat.TEXT)
    assert markdown == "Héllo wörld"
