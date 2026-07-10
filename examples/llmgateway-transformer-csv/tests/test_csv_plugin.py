"""Tests a plugin author would write. Copy this file as a starting point."""

from __future__ import annotations

import pytest
from llmgateway_transformer_csv import PLUGIN, CsvSniffer, CsvTransformer

from gateway.optimizers.models import ContentType, Detection
from gateway.plugins import PLUGIN_API_VERSION, Capability, PluginContext

CONTEXT = PluginContext(api_version=PLUGIN_API_VERSION, config={})
CSV = "name,unused,city\nAda,,London\n\nGrace,,New York\n"
EXPECTED = "| name | city |\n|---|---|\n| Ada | London |\n| Grace | New York |"


def _detect(content: str) -> Detection:
    detection = CsvSniffer().sniff(content)
    assert detection is not None, "sniffer did not recognize the content"
    return detection


# -- Metadata --------------------------------------------------------------


def test_metadata_is_well_formed() -> None:
    metadata = PLUGIN.metadata
    assert metadata.name == "csv"
    assert metadata.priority == 30
    assert metadata.content_types == frozenset({ContentType.CSV})
    assert Capability.DETERMINISTIC in metadata.capabilities
    assert Capability.IDEMPOTENT in metadata.capabilities
    assert Capability.PROVIDES_SNIFFER in metadata.capabilities


def test_the_transformer_agrees_with_the_metadata() -> None:
    """`simple_plugin` stamps these on, so they cannot drift."""
    transformer = PLUGIN.create_transformer(CONTEXT)
    assert transformer.name == PLUGIN.metadata.name
    assert transformer.priority == PLUGIN.metadata.priority
    assert transformer.content_types == PLUGIN.metadata.content_types


def test_the_plugin_provides_its_sniffer() -> None:
    (sniffer,) = PLUGIN.create_sniffers(CONTEXT)
    assert sniffer.name == "csv-columns"


# -- Detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["a,b\n1,2", "a\tb\n1\t2", "a;b\n1;2", "name,city\nAda,London\nGrace,New York"],
)
def test_tables_are_detected(content: str) -> None:
    assert CsvSniffer().sniff(content) is not None


@pytest.mark.parametrize(
    "content",
    [
        "Hello, world",  # one line
        "Hello, world.\nThis is prose, mostly.\nNo table, here, at all.",  # ragged
        "single column\nno delimiter",
        "",
    ],
)
def test_prose_is_not_detected(content: str) -> None:
    assert CsvSniffer().sniff(content) is None


def test_a_markdown_table_is_never_detected_as_csv() -> None:
    """Otherwise the transformer would re-parse its own output into nonsense."""
    assert CsvSniffer().sniff(EXPECTED) is None


# -- Transformation --------------------------------------------------------


def test_csv_becomes_a_markdown_table() -> None:
    transformer = CsvTransformer()
    output = transformer.transform(CSV, _detect(CSV))
    assert output.content == EXPECTED
    assert "converted_to_markdown_table" in output.steps
    assert "removed_empty_columns" in output.steps
    assert "removed_empty_rows" in output.steps


def test_tabs_are_supported() -> None:
    content = "a\tb\n1\t2"
    output = CsvTransformer().transform(content, _detect(content))
    assert output.content == "| a | b |\n|---|---|\n| 1 | 2 |"


def test_pipes_in_cells_are_escaped() -> None:
    content = "a,b\nx|y,2"
    output = CsvTransformer().transform(content, _detect(content))
    assert r"x\|y" in output.content


def test_a_column_with_data_is_never_dropped() -> None:
    content = "a,b\n1,2"
    output = CsvTransformer().transform(content, _detect(content))
    assert "removed_empty_columns" not in output.steps
    assert "| 1 | 2 |" in output.content


def test_malformed_input_is_forwarded_untouched() -> None:
    detection = Detection(ContentType.CSV, 1.0, "test")
    assert CsvTransformer().transform("only one line", detection).content == "only one line"


# -- The two invariants the gateway enforces -------------------------------


def test_transformation_is_deterministic() -> None:
    transformer = CsvTransformer()
    first = transformer.transform(CSV, _detect(CSV)).content
    second = transformer.transform(CSV, _detect(CSV)).content
    assert first == second


def test_transformation_is_idempotent_through_detection() -> None:
    """The output is a Markdown table, which the sniffer must refuse."""
    once = CsvTransformer().transform(CSV, _detect(CSV)).content
    assert CsvSniffer().sniff(once) is None, "the second pass would re-transform"
