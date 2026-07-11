"""Binary format detection.

The property under test: the *bytes* decide, and a lying media type cannot override
them. That is what stops a caller from getting a PDF parsed as CSV, or — worse — a
raw binary treated as text.
"""

from __future__ import annotations

import pytest

from gateway.documents import DocumentFormat, detect_format
from tests.mocks.documents import make_docx, make_pdf


def test_pdf_is_detected_by_magic_bytes() -> None:
    pdf = make_pdf([[("Hello", 12)]])
    assert detect_format(pdf) is DocumentFormat.PDF


def test_docx_is_detected_by_peeking_inside_the_zip() -> None:
    """DOCX/XLSX/PPTX share the ZIP signature; only the members tell them apart."""
    docx = make_docx([("Heading 1", "Title")])
    assert detect_format(docx) is DocumentFormat.DOCX


def test_a_lying_media_type_cannot_override_the_bytes() -> None:
    pdf = make_pdf([[("Real PDF", 12)]])
    assert detect_format(pdf, media_type="text/csv") is DocumentFormat.PDF


def test_a_lying_extension_cannot_override_the_bytes() -> None:
    docx = make_docx([("Normal", "text")])
    assert detect_format(docx, filename="report.pdf") is DocumentFormat.DOCX


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b'<?xml version="1.0"?><r/>', DocumentFormat.XML),
        (b"\xef\xbb\xbf<?xml version='1.0'?><r/>", DocumentFormat.XML),
        (b"<!DOCTYPE html><html></html>", DocumentFormat.HTML),
        (b"<html><body>x</body></html>", DocumentFormat.HTML),
    ],
)
def test_textual_formats_declare_themselves(head: bytes, expected: DocumentFormat) -> None:
    assert detect_format(head) is expected


def test_csv_needs_a_media_type_hint() -> None:
    data = b"name,age\nAda,36"
    assert detect_format(data) is DocumentFormat.TEXT  # textual, but not self-declaring
    assert detect_format(data, media_type="text/csv") is DocumentFormat.CSV


def test_extension_is_the_last_resort() -> None:
    data = b"col1,col2\n1,2"
    assert detect_format(data, filename="data.csv") is DocumentFormat.CSV


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00\x01\x02\x03", b"\xff\xd8\xff\xe0garbage"],  # empty, NULs, JPEG
)
def test_binary_junk_is_not_mistaken_for_a_document(data: bytes) -> None:
    assert detect_format(data) is DocumentFormat.UNKNOWN


def test_legacy_ole_documents_are_recognized_but_unsupported() -> None:
    """A .doc/.xls (OLE) is identifiable but not something we extract; UNKNOWN is safe."""
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    assert detect_format(ole) is DocumentFormat.UNKNOWN


def test_a_non_ooxml_zip_is_not_called_text() -> None:
    """A plain ZIP has the PK signature but no word/ member. It must stay binary."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    assert detect_format(buffer.getvalue()) is DocumentFormat.UNKNOWN


def test_a_truncated_zip_does_not_crash_detection() -> None:
    assert detect_format(b"PK\x03\x04truncated") is DocumentFormat.UNKNOWN
