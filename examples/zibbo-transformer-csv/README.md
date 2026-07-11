# zibbo-transformer-csv

A reference [Zibbo](../../README.md) plugin. Converts CSV and TSV tables inside
prompts into compact Markdown tables, dropping empty rows and columns.

It is here to be read. The whole plugin is one file, and the parts that matter are
the entry point in `pyproject.toml` and the `PLUGIN` object at the bottom of
`src/zibbo_transformer_csv/__init__.py`.

## Install

```bash
pip install -e examples/zibbo-transformer-csv
```

That is the entire integration. The gateway discovers the plugin through the
`zibbo.transformers` entry point on its next start:

```bash
curl localhost:8000/internal/plugins
```

## What it does

```
name,unused,city
Ada,,London

Grace,,New York
```

becomes

```
| name | city |
|---|---|
| Ada | London |
| Grace | New York |
```

The `unused` column is dropped because no data row fills it. The blank line is
dropped. `name` survives because it labels a column that has values.

## What it ships

**A sniffer.** A CSV transformer without CSV detection never runs — the detector
would call the content plain text and route it to the text transformer. So the
plugin brings `CsvSniffer`, which looks for several lines that agree on how many
delimiters they contain, and declares `Capability.PROVIDES_SNIFFER`.

`CsvSniffer` refuses content that already contains a Markdown table separator row.
Without that guard the transformer would eventually re-detect its own output as CSV
— every row of a Markdown table can carry the same number of commas — and re-parse
it into nonsense. Idempotency has to be defended in *detection*, not in the
transformer, because by the time the transformer runs it cannot tell.

**A transformer.** `simple_plugin` stamps `name`, `priority` and `content_types` onto
it from the metadata, so those facts are declared exactly once.

## Priority

`30`: after HTML (10) and JSON (20), before plain text (100). A JSON array of arrays
is JSON, and JSON should claim it first.

## Testing

```bash
pip install -e . && pytest tests/
```

See [PLUGIN_DEVELOPMENT.md](../../docs/PLUGIN_DEVELOPMENT.md).
