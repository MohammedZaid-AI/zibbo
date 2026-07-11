# Plugin development

A plugin adds a content format to Zibbo without changing the gateway. Install
the package, restart, and the gateway finds it.

Everything a plugin needs is importable from `gateway.plugins`. The gateway never
imports your package — it discovers it, constructs it behind per-plugin error
handling, and registers what it produces.

## Creating a plugin

A plugin ships two things:

* a **transformer** that rewrites content, and
* usually a **sniffer** that recognizes the content in the first place.

The sniffer is not optional in practice. The detector routes segments by what they
*are*, so a CSV transformer with no CSV detection never runs: the content would be
classified as plain text and normalized instead.

```python
# src/zibbo_transformer_csv/__init__.py
import csv
import io
from typing import ClassVar

from gateway.plugins import (
    Capability, ContentType, Detection, Transformer, TransformOutput, simple_plugin,
)


class CsvSniffer:
    name: ClassVar[str] = "csv-columns"

    def sniff(self, content: str) -> Detection | None:
        lines = [line for line in content.splitlines() if line.strip()][:20]
        if len(lines) < 2:
            return None
        counts = {line.count(",") for line in lines}
        if len(counts) == 1 and counts.pop() >= 1:
            return Detection(ContentType.CSV, 0.85, self.name, metadata={"delimiter": ","})
        return None


class CsvTransformer(Transformer):
    def transform(self, content: str, detection: Detection) -> TransformOutput:
        rows = [r for r in csv.reader(io.StringIO(content)) if any(c.strip() for c in r)]
        if len(rows) < 2:
            return TransformOutput(content, ())          # nothing to do
        header, *body = rows
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        return TransformOutput("\n".join(lines), ("converted_to_markdown_table",))


PLUGIN = simple_plugin(
    transformer=CsvTransformer,
    sniffers=[CsvSniffer],
    name="csv",
    version="1.0.0",
    author="you@example.com",
    description="CSV to compact Markdown tables.",
    content_types={ContentType.CSV},
    priority=30,
)
```

`simple_plugin` stamps `name`, `priority` and `content_types` onto the transformer
class from the metadata, so each fact is declared once. If you set them yourself they
must match, or the plugin is rejected at load — a registry sorted by one priority
while the operator reads another is worse than a failure.

For a plugin that needs real work at construction time — reading `context.config`,
loading a model file — subclass `TransformerPlugin` directly and implement
`create_transformer(context)`.

The complete, working version of the above is in
[`examples/zibbo-transformer-csv/`](../examples/zibbo-transformer-csv/).

## The contract

`transform` must be:

**Deterministic.** Same input, same output, every process. The Phase 8 response cache
keys on the transformed body; non-determinism would poison it.

**Idempotent.** `T(T(x)) == T(x)` whenever your transformer can handle its own output.
Usually the output is a *different* content type — CSV becomes Markdown — so the
detector routes the second pass elsewhere and the property holds trivially. **Check
that it really does.** The CSV example nearly shipped a bug: every row of a Markdown
table can carry the same number of commas, so its own sniffer would have recognized
its output as CSV and re-parsed it into nonsense. Idempotency had to be defended in
*detection*, because by the time the transformer runs it cannot tell.

**Total.** Never raise. Malformed input is forwarded untouched. (If you do raise, the
pipeline logs it and skips the segment — a safety net, not a design.)

**Meaning-preserving.** Remove structural noise. Never summarize, reorder, infer, or
invent. When in doubt, keep the content and make the removal a config flag.

An empty `steps` tuple means "I changed nothing", and the pipeline then forwards the
original bytes rather than a re-serialization. Return it whenever output equals input.

**You do not need to check that you saved tokens.** The pipeline counts tokens before
and after, and if a transformation *grew* the content it discards the result and
forwards the original. That is a property of the product, not of your transformer.
Markdown table syntax outweighs CSV commas on a narrow table, and the gateway simply
reverts. You will see `transformation_reverted` in the debug log.

## Capabilities

Declared in metadata, and **enforced**, not documented.

| Capability | Meaning |
|---|---|
| `deterministic` | **Required.** Same input, same output. |
| `idempotent` | **Required.** `T(T(x)) == T(x)`. |
| `provides_sniffer` | Required if `create_sniffers` returns anything. |
| `lossy` | Removes information a caller might miss. Refused unless `ZIBBO_PLUGINS_ALLOW_LOSSY=true`. |
| `experimental` | Loads, but is logged at startup so operators know. |

A plugin that does not declare `deterministic` and `idempotent` **does not load**. The
pipeline's guarantees rest on them, and there is no way for the gateway to check them
at runtime — declaring them is a promise, and the one lie it cannot catch.

## Priority

Lower runs first. The registry picks the first transformer whose `can_handle` accepts
the content, so a specific transformer must outrank a general one.

| | |
|---|---|
| `10` | `html` (builtin) |
| `20` | `json` (builtin) |
| `30` | `csv` (example plugin) |
| `100` | `text` (builtin) — the fallback |

Range is `0`–`1000`. Pick a number, leave gaps, and remember `text` will claim
anything you leave unhandled.

## Packaging

One line in `pyproject.toml` is the whole discovery mechanism:

```toml
[project.entry-points."zibbo.transformers"]
csv = "zibbo_transformer_csv:PLUGIN"
```

The value is `module:ATTRIBUTE`, where the attribute is a `TransformerPlugin`
instance (a class or a `get_plugin()` factory also work).

Do **not** pin the gateway as a dependency. Compatibility is governed by
`api_version` in your metadata, not by a version range on the package.

```bash
pip install -e .
curl localhost:8000/internal/plugins        # should list your plugin as "enabled"
```

## Version compatibility

The gateway implements plugin API `1.0`. Your plugin declares the API version it was
written against; the default is the version of `gateway.plugins` you built with.

A plugin is compatible when its **major** version equals the gateway's and its
**minor** is less than or equal to the gateway's:

| Plugin | Gateway | |
|---|---|---|
| `1.0` | `1.3` | loads — written against older API the gateway still has |
| `1.3` | `1.3` | loads |
| `1.4` | `1.0` | refused — needs API this gateway lacks |
| `2.0` | `1.0` | refused — different major |

A refused plugin is recorded as `failed` with stage `version`. Nothing else breaks.

## Publishing

Name the package `zibbo-transformer-<format>`. Ship the entry point, a README,
and the plugin's own tests. Nothing else about publishing is special — it is an
ordinary Python package on PyPI.

## Testing

Copy [`examples/zibbo-transformer-csv/tests/test_csv_plugin.py`](../examples/zibbo-transformer-csv/tests/test_csv_plugin.py).
It covers what matters:

```python
def test_metadata_is_well_formed():
    assert PLUGIN.metadata.name == "csv"

def test_the_transformer_agrees_with_the_metadata():
    t = PLUGIN.create_transformer(PluginContext(api_version=PLUGIN_API_VERSION, config={}))
    assert t.priority == PLUGIN.metadata.priority

def test_transformation_is_deterministic():
    assert transform(CSV) == transform(CSV)

def test_transformation_is_idempotent_through_detection():
    once = transform(CSV)
    assert CsvSniffer().sniff(once) is None   # the second pass must not re-transform

def test_prose_is_not_detected():
    assert CsvSniffer().sniff("Hello, world") is None   # a comma is not a table
```

Test what you must **not** transform at least as hard as what you must. Over-eager
detection is how a plugin silently corrupts a user's prompt.

To exercise a plugin inside the real pipeline:

```python
registry = build_transformer_registry(OptimizerOptions())
detector = ContentDetector()
manager = PluginManager([StaticSource(csv=PLUGIN)])
manager.load()
manager.attach(registry, detector)
pipeline = build_pipeline(settings, counters, registry=registry, detector=detector)
```

## Configuration

| Variable | Effect |
|---|---|
| `ZIBBO_PLUGINS_ENABLED` | Master switch. `true` by default. |
| `ZIBBO_PLUGINS_ENTRY_POINT_GROUP` | Defaults to `zibbo.transformers`. Set empty to skip entry points. |
| `ZIBBO_PLUGINS_DIR` | Load `*.py` files and packages from a directory. **Executes arbitrary code**; no default. |
| `ZIBBO_PLUGINS_LOAD` | `module:ATTR` targets, comma-separated. Highest precedence. |
| `ZIBBO_PLUGINS_DISABLED` | Names to load but not attach. |
| `ZIBBO_PLUGINS_ALLOW_LOSSY` | Permit `lossy` plugins. |
| `ZIBBO_PLUGINS_CONFIG` | JSON object keyed by plugin name; each plugin sees only its own entry. |

Precedence: explicit configuration, then directory, then entry points. The first
source to claim a name keeps it, so an operator can override an installed plugin.

## Debugging

**Start here.**

```bash
curl localhost:8000/internal/plugins
```

Every plugin appears, including the ones that failed, with the stage that failed and
the exception that caused it:

```json
{
  "name": "wrong_version", "state": "failed",
  "source": "directory", "origin": "/opt/plugins/wrong_version.py",
  "stage": "version",
  "error": "PluginVersionError: plugin targets API 9.9, gateway implements 1.0: ..."
}
```

| Stage | What went wrong |
|---|---|
| `import` | Your module raised, or exposes neither `PLUGIN` nor `get_plugin`. |
| `version` | `api_version` is incompatible. |
| `capability` | Declares `lossy` in a deployment that forbids it, or missing a required capability. |
| `validate` | Metadata is malformed, or the transformer disagrees with it. |
| `duplicate` | Another plugin already claimed the name. |
| `construct` | `create_transformer` raised. |
| `register` | The transformer's name collides with a builtin (`html`, `json`, `text`). |

**A broken plugin never stops the gateway.** It is recorded, logged, and skipped.
Every other plugin loads. That is the one guarantee the plugin system makes to
operators, and `tests/test_plugins.py` asserts it against a real application startup.

**My plugin loaded but never runs.** Almost always detection. The registry only asks
your transformer about content the detector labelled with your `ContentType`. Check
`sniff` in isolation, and remember the highest-confidence sniffer wins — a JSON array
of arrays is JSON, not CSV.

**My plugin runs but nothing changes.** The pipeline reverts a transformation that
increases the token count. Look for `transformation_reverted` at `DEBUG`.

Startup logs the whole picture:

```json
{"event": "plugins_loaded", "enabled": ["csv"], "disabled": [], "failed": ["explodes"], "total": 2}
```
