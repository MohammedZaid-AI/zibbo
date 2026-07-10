"""A reference LLMGateway plugin: CSV to compact Markdown tables.

Everything a plugin needs comes from ``gateway.plugins``. Nothing else in the
gateway is imported, and the gateway never imports this package — it is discovered
through the ``llmgateway.transformers`` entry point declared in ``pyproject.toml``.

The plugin ships two things:

* a **sniffer**, because a CSV transformer without CSV detection never runs — the
  detector would classify the content as plain text and route it elsewhere;
* a **transformer**, which does the work.

Why CSV is worth optimizing: a spreadsheet dump is mostly delimiters and empty
columns. The Markdown table says the same thing in fewer tokens, and a model reads
it more reliably than raw CSV.
"""

from __future__ import annotations

import csv
import io
import re
from typing import TYPE_CHECKING, ClassVar

from gateway.plugins import (
    Capability,
    ContentType,
    Detection,
    Transformer,
    TransformOutput,
    simple_plugin,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__version__ = "1.0.0"

STEP_CONVERTED = "converted_to_markdown_table"
STEP_REMOVED_EMPTY_COLUMNS = "removed_empty_columns"
STEP_REMOVED_EMPTY_ROWS = "removed_empty_rows"

_MIN_ROWS = 2
_MIN_COLUMNS = 2
_SAMPLE_LINES = 20

# This transformer's own output. Every cell of a Markdown table may happen to hold
# the same number of commas, in which case the sniffer below would recognize its own
# result as CSV and parse it into nonsense. Idempotency has to be defended here, in
# detection, because by the time the transformer runs it is too late to tell.
_MARKDOWN_TABLE_RE = re.compile(r"^\|(?:\s*-{3,}\s*\|)+$", re.MULTILINE)


class CsvSniffer:
    """Detects delimiter-separated tables.

    Conservative on purpose. A single line with a comma is a sentence; a table is
    several lines that agree on how many fields they have. Confidence stays below
    the JSON and HTML sniffers so that a CSV-shaped JSON array is never stolen.
    """

    name: ClassVar[str] = "csv-columns"

    def sniff(self, content: str) -> Detection | None:
        if _MARKDOWN_TABLE_RE.search(content):
            return None  # already a Markdown table; re-parsing it would corrupt it

        lines = [line for line in content.splitlines() if line.strip()][:_SAMPLE_LINES]
        if len(lines) < _MIN_ROWS:
            return None

        for delimiter in (",", "\t", ";"):
            counts = {line.count(delimiter) for line in lines}
            if len(counts) == 1 and (columns := counts.pop()) >= _MIN_COLUMNS - 1:
                # Every line has the same number of delimiters, and there is at
                # least one. That is a table, not prose.
                return Detection(
                    ContentType.CSV,
                    0.85,
                    self.name,
                    metadata={"delimiter": delimiter, "columns": columns + 1},
                )
        return None


class CsvTransformer(Transformer):
    """Rewrites a CSV table as a Markdown table.

    ``name``, ``priority`` and ``content_types`` are stamped on by ``simple_plugin``
    from the metadata below, so they are declared exactly once.
    """

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        delimiter = str(detection.metadata.get("delimiter", ","))

        try:
            raw = list(csv.reader(io.StringIO(content), delimiter=delimiter))
        except csv.Error:
            return TransformOutput(content, ())  # malformed: forward untouched

        rows = [row for row in raw if any(cell.strip() for cell in row)]
        if len(rows) < _MIN_ROWS:
            return TransformOutput(content, ())

        steps: list[str] = []
        if len(rows) != len(raw):
            steps.append(STEP_REMOVED_EMPTY_ROWS)

        header, *body = rows
        width = max(len(row) for row in rows)
        # A column is empty only when every *data* row leaves it empty. A header
        # with no values beneath it is a label for nothing.
        keep = [
            index
            for index in range(width)
            if any(len(row) > index and row[index].strip() for row in body)
        ]
        if not keep:
            return TransformOutput(content, ())
        if len(keep) != width:
            steps.append(STEP_REMOVED_EMPTY_COLUMNS)

        lines = [_row(header, keep), "|" + "---|" * len(keep)]
        lines.extend(_row(row, keep) for row in body)
        markdown = "\n".join(lines)

        if markdown == content:
            return TransformOutput(content, ())  # already optimal: no re-serialization

        steps.append(STEP_CONVERTED)
        return TransformOutput(markdown, tuple(steps))


def _row(row: Sequence[str], keep: Sequence[int]) -> str:
    cells = [row[index].strip().replace("|", r"\|") if len(row) > index else "" for index in keep]
    return "| " + " | ".join(cells) + " |"


PLUGIN = simple_plugin(
    transformer=CsvTransformer,
    sniffers=[CsvSniffer],
    name="csv",
    version=__version__,
    author="LLMGateway <plugins@llmgateway.dev>",
    description="Converts CSV and TSV tables to compact Markdown tables.",
    content_types={ContentType.CSV},
    # Lower runs first. After html(10) and json(20) so a JSON array of arrays is
    # never mistaken for a table; before text(100), which would only normalize it.
    priority=30,
    capabilities={
        Capability.DETERMINISTIC,
        Capability.IDEMPOTENT,
        Capability.PROVIDES_SNIFFER,
    },
    homepage="https://github.com/MohammedZaid-AI/Semantix",
)
