"""Detecting a document's real format from its bytes.

The order is the point, and it is the order the phase brief asks for: **magic bytes
first, media type second, extension last.** A caller's declared media type is a
hint an attacker or a careless client can lie about; the bytes cannot lie.

The hard case is Office formats. DOCX, XLSX and PPTX are all ZIP archives — they
share the ``PK\\x03\\x04`` signature — so the signature alone cannot tell them
apart. The archive has to be opened and the member names inspected: a DOCX contains
``word/``, an XLSX ``xl/``, a PPTX ``ppt/``. That peek is bounded and cheap.
"""

from __future__ import annotations

import io
import zipfile
from typing import Final

from gateway.documents.models import DocumentFormat
from gateway.logging import get_logger

logger = get_logger(__name__)

# Byte signatures that identify a format outright. Ordered longest-first so a more
# specific signature is tried before a prefix of it.
_MAGIC: Final[tuple[tuple[bytes, DocumentFormat], ...]] = (
    (b"%PDF-", DocumentFormat.PDF),
    (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        DocumentFormat.UNKNOWN,
    ),  # legacy .doc/.xls (OLE): unsupported
    (b"<?xml", DocumentFormat.XML),
    (b"\xef\xbb\xbf<?xml", DocumentFormat.XML),  # UTF-8 BOM + declaration
)

_ZIP_MAGIC: Final = b"PK\x03\x04"

# Distinguishing member prefixes inside an OOXML ZIP.
_OOXML_MEMBERS: Final[tuple[tuple[str, DocumentFormat], ...]] = (
    ("word/", DocumentFormat.DOCX),
    ("xl/", DocumentFormat.XLSX),
    ("ppt/", DocumentFormat.PPTX),
)

# Media types, used only when the bytes are inconclusive. The OOXML types share a
# long prefix, so they are built from it rather than written out at full width.
_OOXML: Final = "application/vnd.openxmlformats-officedocument."
_MEDIA_TYPES: Final[dict[str, DocumentFormat]] = {
    "application/pdf": DocumentFormat.PDF,
    f"{_OOXML}wordprocessingml.document": DocumentFormat.DOCX,
    f"{_OOXML}spreadsheetml.sheet": DocumentFormat.XLSX,
    f"{_OOXML}presentationml.presentation": DocumentFormat.PPTX,
    "text/csv": DocumentFormat.CSV,
    "application/xml": DocumentFormat.XML,
    "text/xml": DocumentFormat.XML,
    "text/html": DocumentFormat.HTML,
    "text/markdown": DocumentFormat.MARKDOWN,
    "text/plain": DocumentFormat.TEXT,
}

_EXTENSIONS: Final[dict[str, DocumentFormat]] = {
    "pdf": DocumentFormat.PDF,
    "docx": DocumentFormat.DOCX,
    "xlsx": DocumentFormat.XLSX,
    "pptx": DocumentFormat.PPTX,
    "csv": DocumentFormat.CSV,
    "tsv": DocumentFormat.CSV,
    "xml": DocumentFormat.XML,
    "html": DocumentFormat.HTML,
    "htm": DocumentFormat.HTML,
    "md": DocumentFormat.MARKDOWN,
    "markdown": DocumentFormat.MARKDOWN,
    "txt": DocumentFormat.TEXT,
    "text": DocumentFormat.TEXT,
}

# How far into a text document to look before deciding it is plain text.
_TEXT_SNIFF_BYTES: Final = 4096


def _essence(media_type: str | None) -> str:
    if not media_type:
        return ""
    return media_type.split(";", 1)[0].strip().lower()


def _classify_zip(data: bytes) -> DocumentFormat:
    """Open an OOXML ZIP and read its member names to tell DOCX/XLSX/PPTX apart."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return DocumentFormat.UNKNOWN

    for prefix, fmt in _OOXML_MEMBERS:
        if any(name.startswith(prefix) for name in names):
            return fmt
    return DocumentFormat.UNKNOWN


def _looks_textual(data: bytes) -> bool:
    """A crude but reliable binary/text split: real text decodes and has no NULs."""
    sample = data[:_TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _classify_text(data: bytes) -> DocumentFormat:
    """Distinguish the textual formats when only the bytes are available."""
    head = data[:_TEXT_SNIFF_BYTES].lstrip().lower()
    if head.startswith((b"<?xml", b"\xef\xbb\xbf<?xml")):
        return DocumentFormat.XML
    if head.startswith((b"<!doctype html", b"<html")):
        return DocumentFormat.HTML
    return DocumentFormat.UNKNOWN  # CSV/Markdown/plain need the media-type hint


def detect_format(
    data: bytes,
    *,
    media_type: str | None = None,
    filename: str | None = None,
) -> DocumentFormat:
    """Identify a document. Bytes win; media type and extension only break ties."""
    if not data:
        return DocumentFormat.UNKNOWN

    # 1. Magic bytes.
    for signature, fmt in _MAGIC:
        if data.startswith(signature):
            if fmt is DocumentFormat.UNKNOWN:
                return DocumentFormat.UNKNOWN  # recognized-but-unsupported (legacy OLE)
            return fmt
    if data.startswith(_ZIP_MAGIC):
        zip_format = _classify_zip(data)
        if zip_format is not DocumentFormat.UNKNOWN:
            return zip_format
        # A ZIP we do not recognize as OOXML: fall through to the hint, but never
        # call it text — it is binary.
        declared = _MEDIA_TYPES.get(_essence(media_type), DocumentFormat.UNKNOWN)
        return declared if declared in _OOXML_FORMATS else DocumentFormat.UNKNOWN

    # 2. Textual structure (XML/HTML declare themselves in their first bytes).
    if _looks_textual(data):
        structural = _classify_text(data)
        if structural is not DocumentFormat.UNKNOWN:
            return structural

        # 3. Media type, then extension, for the textual formats that do not.
        by_media = _MEDIA_TYPES.get(_essence(media_type))
        if by_media is not None:
            return by_media
        if filename:
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if extension in _EXTENSIONS:
                return _EXTENSIONS[extension]
        return DocumentFormat.TEXT  # textual but unclassified: still safe to normalize

    # Binary, no known signature. A media-type hint is the last resort.
    declared = _MEDIA_TYPES.get(_essence(media_type), DocumentFormat.UNKNOWN)
    if declared in _BINARY_FORMATS:
        return declared
    return DocumentFormat.UNKNOWN


_OOXML_FORMATS: Final = frozenset({DocumentFormat.DOCX, DocumentFormat.XLSX, DocumentFormat.PPTX})
_BINARY_FORMATS: Final = frozenset({DocumentFormat.PDF, *_OOXML_FORMATS})
