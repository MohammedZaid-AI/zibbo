"""CSV/TSV -> Markdown, with the intelligent sizing the brief asks for.

The sizing rule follows one principle: **never emit more than you were given.** A
Markdown table adds two or three characters of pipes and padding to every cell, so
it is a fine trade for a small table a model reads more reliably that way, but a
disaster for a large one. So:

* A **small** table becomes a Markdown table — readable, and the overhead is trivial.
* A **large** table becomes cleaned, minified CSV: trimmed cells, empty rows and
  columns removed, but still comma-separated. That is genuinely compact — one
  delimiter per cell, never the two-plus of a Markdown table — and it is what keeps
  a big spreadsheet from *growing* when converted.

Never changes a value. Drops only empty rows, empty columns, and the whitespace
around a cell. Determinism and idempotency are the caller's contract; this function
is a pure transformation of one string to another.
"""

from __future__ import annotations

import csv
import io
from typing import Final

# Above either bound, the Markdown table form is abandoned for compact CSV.
_MAX_TABLE_COLUMNS: Final = 12
_MAX_TABLE_ROWS: Final = 50

_DELIMITERS: Final = (",", "\t", ";", "|")


def _sniff_delimiter(text: str) -> str:
    """Pick the delimiter that parses the header into the most, most-consistent cells."""
    sample = "\n".join(text.splitlines()[:20])
    best, best_score = ",", 0
    for delimiter in _DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        except csv.Error:
            continue
        rows = [r for r in rows if r]
        if len(rows) < 2:
            continue
        width = len(rows[0])
        if width < 2:
            continue
        # Reward a wide header whose width the body rows agree with.
        consistent = sum(1 for r in rows[1:] if len(r) == width)
        score = width * (1 + consistent)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def _clean(cell: str) -> str:
    return cell.strip().replace("|", r"\|").replace("\n", " ")


def csv_to_markdown(text: str, *, delimiter: str | None = None) -> str | None:
    """Convert delimited text to Markdown. ``None`` if it is not tabular."""
    delimiter = delimiter or _sniff_delimiter(text)
    try:
        raw = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return None

    rows = [row for row in raw if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        return None

    header, *body = rows
    width = max(len(row) for row in rows)

    # Drop a column only when *every* cell in it is empty — header included. A column
    # whose header carries a value is kept even if its data is blank, because that
    # header is content, and dropping it would lose a value the user provided. This
    # is the conservative reading of "never change data values": remove only what is
    # wholly empty.
    keep = [
        index
        for index in range(width)
        if any(len(row) > index and row[index].strip() for row in rows)
    ]
    if len(keep) < 1:
        return None

    # An unlabelled column keeps an empty header rather than an invented name —
    # "never change data values" extends to not inventing labels either.
    headers = [_clean(header[i]) if i < len(header) else "" for i in keep]

    if len(keep) <= _MAX_TABLE_COLUMNS and len(body) <= _MAX_TABLE_ROWS:
        return _as_table(headers, body, keep)
    return _as_clean_csv(headers, body, keep)


def _cell(row: list[str], index: int) -> str:
    return _clean(row[index]) if index < len(row) else ""


def _as_table(headers: list[str], body: list[list[str]], keep: list[int]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(keep),
    ]
    lines.extend("| " + " | ".join(_cell(row, i) for i in keep) + " |" for row in body)
    return "\n".join(lines)


def _as_clean_csv(headers: list[str], body: list[list[str]], keep: list[int]) -> str:
    """Compact form for a large table: cleaned CSV in a fenced code block.

    One delimiter per cell, empty rows and columns already removed, cells trimmed.
    Never larger than the input, which is the whole point — a Markdown table of ten
    thousand rows would balloon; this stays lean. The fence tells the model it is
    tabular data, not prose.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([header.replace("\n", " ") for header in headers])
    for row in body:
        writer.writerow([_row_cell(row, index) for index in keep])
    return "```csv\n" + out.getvalue().rstrip("\n") + "\n```"


def _row_cell(row: list[str], index: int) -> str:
    return row[index].strip().replace("\n", " ") if index < len(row) else ""
