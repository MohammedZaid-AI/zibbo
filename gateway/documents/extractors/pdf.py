"""PDF -> Markdown, via pdfplumber.

The goals from the brief: headings, paragraphs, tables and lists, in reading order,
without the header/footer that repeats on every page. All of it deterministic — the
same PDF yields the same Markdown, with no model in the loop.

The three judgement calls, and how each is made without guessing:

* **Headings** are lines whose characters are meaningfully larger than the document's
  body text. The body size is the modal character height; a line a quarter larger is
  a heading, and its level follows how much larger.
* **Tables** come from pdfplumber's ruling-line detection, emitted before the page's
  prose so a table is not spliced into a sentence.
* **Running headers and footers** are lines that recur near the top or bottom of many
  pages. A line appearing on more than half the pages, in the margin band, is chrome.

An encrypted or corrupt PDF makes pdfplumber raise; the base class catches it and the
document is forwarded untouched.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, ClassVar, Final

from gateway.documents.base import DocumentExtractor
from gateway.documents.models import DocumentFormat

_HEADING_RATIO: Final = 1.25  # a line this much taller than body text is a heading
_H1_RATIO: Final = 1.8
_H2_RATIO: Final = 1.45
_MARGIN_FRACTION: Final = 0.12  # top/bottom band a running header/footer lives in
_MIN_PAGES_FOR_CHROME: Final = 3
_CHROME_PAGE_FRACTION: Final = 0.5


class PdfExtractor(DocumentExtractor):
    name: ClassVar[str] = "pdf"
    formats: ClassVar[frozenset[DocumentFormat]] = frozenset({DocumentFormat.PDF})

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        import io

        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if not pdf.pages:
                return None

            body_size = _body_font_size(pdf)
            chrome = _running_chrome(pdf)

            blocks: list[str] = []
            for page in pdf.pages:
                blocks.extend(_page_blocks(page, body_size, chrome))

        markdown = "\n\n".join(block for block in blocks if block.strip())
        return _collapse_blank_lines(markdown) or None


# -- Font-size baseline -----------------------------------------------------


def _body_font_size(pdf: Any) -> float:
    """The modal character height across the first few pages — the body text size."""
    sizes: Counter[int] = Counter()
    for page in pdf.pages[:5]:
        for char in page.chars:
            size = char.get("size")
            if size:
                sizes[round(size)] += 1
    if not sizes:
        return 0.0
    return float(sizes.most_common(1)[0][0])


# -- Running headers/footers ------------------------------------------------


def _running_chrome(pdf: Any) -> frozenset[str]:
    """Lines that recur in the top/bottom margin across many pages."""
    if len(pdf.pages) < _MIN_PAGES_FOR_CHROME:
        return frozenset()

    margin_lines: Counter[str] = Counter()
    for page in pdf.pages:
        height = page.height or 1
        top_band = height * _MARGIN_FRACTION
        bottom_band = height * (1 - _MARGIN_FRACTION)
        seen: set[str] = set()
        # Real word coordinates, the same ones the renderer uses, so the detector and
        # the renderer never disagree about whether a line is in the margin.
        for text, top in _lines_with_top(page):
            if (top < top_band or top > bottom_band) and (normalized := _normalize(text)):
                seen.add(normalized)
        margin_lines.update(seen)

    threshold = len(pdf.pages) * _CHROME_PAGE_FRACTION
    return frozenset(line for line, count in margin_lines.items() if count > threshold)


_PAGE_NUMBER_DIGITS = str.maketrans("", "", "0123456789")


def _normalize(line: str) -> str:
    """Fold page numbers away so 'Page 3' and 'Page 4' count as the same header."""
    return " ".join(line.translate(_PAGE_NUMBER_DIGITS).split()).strip().lower()


# -- Per-page rendering -----------------------------------------------------


def _page_blocks(page: Any, body_size: float, chrome: frozenset[str]) -> list[str]:
    blocks: list[str] = []

    for table in page.extract_tables() or []:
        rendered = _table_to_markdown(table)
        if rendered:
            blocks.append(rendered)

    lines = _lines_with_size(page)
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    for text, size in lines:
        if _normalize(text) in chrome:
            continue
        heading_level = _heading_level(size, body_size)
        if heading_level:
            flush()
            blocks.append(f"{'#' * heading_level} {text}")
        elif _is_list_item(text):
            flush()
            blocks.append(f"- {_strip_bullet(text)}")
        else:
            paragraph.append(text)
    flush()
    return blocks


def _heading_level(size: float, body_size: float) -> int:
    if body_size <= 0 or size < body_size * _HEADING_RATIO:
        return 0
    if size >= body_size * _H1_RATIO:
        return 1
    if size >= body_size * _H2_RATIO:
        return 2
    return 3


_BULLETS: Final = ("•", "‣", "◦", "-", "*", "–")  # noqa: RUF001 — real bullet glyphs, not typos


def _is_list_item(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(_BULLETS) and len(stripped) > 2


def _strip_bullet(text: str) -> str:
    stripped = text.lstrip()
    for bullet in _BULLETS:
        if stripped.startswith(bullet):
            return stripped[len(bullet) :].strip()
    return stripped


# -- pdfplumber helpers -----------------------------------------------------


def _lines_with_size(page: Any) -> list[tuple[str, float]]:
    """Group characters into lines, each paired with its dominant character height."""
    words = page.extract_words(extra_attrs=["size"], use_text_flow=True)
    lines: dict[float, list[dict[str, Any]]] = {}
    for word in words:
        # Bucket by vertical position so words on the same visual line group together.
        key = round(float(word["top"]) / 3) * 3
        lines.setdefault(key, []).append(word)

    result: list[tuple[str, float]] = []
    for top in sorted(lines):
        row = sorted(lines[top], key=lambda w: float(w["x0"]))
        text = " ".join(str(w["text"]) for w in row).strip()
        if not text:
            continue
        sizes = [float(w["size"]) for w in row if w.get("size")]
        result.append((text, statistics.median(sizes) if sizes else 0.0))
    return result


def _lines_with_top(page: Any) -> list[tuple[str, float]]:
    """Lines paired with their real ``top`` coordinate on the page."""
    words = page.extract_words(use_text_flow=True)
    lines: dict[float, list[dict[str, Any]]] = {}
    for word in words:
        key = round(float(word["top"]) / 3) * 3
        lines.setdefault(key, []).append(word)

    result: list[tuple[str, float]] = []
    for top in sorted(lines):
        row = sorted(lines[top], key=lambda w: float(w["x0"]))
        text = " ".join(str(w["text"]) for w in row).strip()
        if text:
            result.append((text, top))
    return result


def _clean_cell(cell: str | None) -> str:
    return (cell or "").strip().replace("|", r"\|").replace("\n", " ")


def _table_to_markdown(table: list[list[str | None]]) -> str:
    rows = [[_clean_cell(cell) for cell in row] for row in table]
    rows = [row for row in rows if any(row)]
    if len(rows) < 1:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _collapse_blank_lines(text: str) -> str:
    out: list[str] = []
    blank = False
    for line in text.split("\n"):
        if line.strip():
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()
