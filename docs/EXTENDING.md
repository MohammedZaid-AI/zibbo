# Extending Zibbo

Four extension points. None of them require touching the gateway.

| I want to… | Add a… | Touches |
|---|---|---|
| Support a new content format | `Transformer` (+ a `Sniffer` if the type is new) | one module, one line |
| Ship that format as its own package | a **plugin** | nothing — see [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md) |
| Support a new LLM provider | `Provider` | one module, one line |
| Change what may be optimized | `PolicyRule` | one class, one line |

The transformer below can live inside the gateway or inside a separate distributable
package. The code is identical; a plugin only adds a `PLUGIN` object describing it and
an entry point in `pyproject.toml`.

## A new transformer, in under 50 lines

Here is a complete, working CSV transformer. It converts a spreadsheet dump into a
compact Markdown table and drops columns that carry no data. It is 30 lines.

```python
# gateway/optimizers/transformers/csv.py
import csv
import io
from typing import ClassVar

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, Detection, TransformOutput


class CsvTransformer(Transformer):
    name: ClassVar[str] = "csv"
    priority: ClassVar[int] = 30                      # after html(10)/json(20), before text(100)
    content_types: ClassVar[frozenset[ContentType]] = frozenset({ContentType.CSV})

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        try:
            rows = [r for r in csv.reader(io.StringIO(content)) if any(c.strip() for c in r)]
        except csv.Error:
            return TransformOutput(content, ())       # unparseable: forward untouched
        if len(rows) < 2:
            return TransformOutput(content, ())

        header, *body = rows
        width = max(len(row) for row in rows)
        # Drop a column only when every *data* row leaves it empty. A header alone
        # is a label for nothing.
        keep = [i for i in range(width) if any(len(r) > i and r[i].strip() for r in body)]
        if not keep:
            return TransformOutput(content, ())

        def cells(row: list[str]) -> list[str]:
            return [row[i].strip().replace("|", r"\|") if len(row) > i else "" for i in keep]

        lines = ["| " + " | ".join(cells(header)) + " |", "|" + "---|" * len(keep)]
        lines.extend("| " + " | ".join(cells(row)) + " |" for row in body)

        markdown = "\n".join(lines)
        if markdown == content:
            return TransformOutput(content, ())       # already optimal: no re-serialization
        return TransformOutput(markdown, ("converted_to_markdown_table", "removed_empty_columns"))
```

Register it:

```python
# gateway/optimizers/__init__.py
def build_transformer_registry(options: OptimizerOptions) -> TransformerRegistry:
    registry = TransformerRegistry()
    registry.register(HtmlTransformer(options.html))
    registry.register(JsonTransformer(options.json))
    registry.register(CsvTransformer())               # <- the only other line
    registry.register(TextTransformer(options.text))
    return registry
```

That is the whole change. The pipeline now detects CSV in a chat message, routes it
here, times it, counts its tokens before and after, records which steps ran, reports
the saving in `x-zibbo-tokens-saved`, and logs the metadata. You wrote none of
that, and Phase 5's dashboard will show your transformer without knowing it exists.

Given `name,unused,city` / `Ada,,London` / `Grace,,New York`:

```
| name | city |
|---|---|
| Ada | London |
| Grace | New York |
```

### The contract you are signing

`transform` must be:

* **Deterministic.** Same input, same output, in every process. Phase 8 caches on a
  hash of the output; non-determinism would poison the cache.
* **Idempotent.** `T(T(x)) == T(x)` whenever your transformer can handle its own
  output. If your output is a different content type — as CSV → Markdown is — the
  detector will route the second pass elsewhere, and idempotency is asserted at the
  pipeline level instead.
* **Total.** Never raise. Malformed input is forwarded untouched, not rejected. (If
  you do raise, the pipeline logs it and skips the segment — but that is a safety
  net, not a design.)
* **Meaning-preserving.** Remove structural noise. Never summarize, reorder, infer,
  or invent. When in doubt, keep the content and make the removal a config flag —
  see `html_preserve_links`.

An empty `steps` tuple means "I changed nothing", and the pipeline then forwards the
original bytes rather than a re-serialization of them. Return it whenever your output
equals your input.

### Registering a new content type

If `ContentType` does not yet have your format, add it to the enum, then teach the
detector to recognize it with a `Sniffer`:

```python
class CsvSniffer:
    name: ClassVar[str] = "csv-delimiters"

    def sniff(self, content: str) -> Detection | None:
        lines = [line for line in content.splitlines() if line.strip()]
        if len(lines) >= 2 and all("," in line for line in lines):
            return Detection(ContentType.CSV, 0.8, self.name)
        return None
```

Append it to `default_sniffers()`. The detector itself does not change. Confidence at
or above `0.7` beats the declared `Content-Type`; below that it only breaks a tie.

For binary formats — PDF, DOCX, images — `MagicBytesSniffer` already exists and takes
a new file signature as a table entry.

### Testing it

Every transformer gets the same three tests for free by adding it to the strategies
in `tests/test_optimizer_properties.py` and `tests/test_optimizer_validity.py`:
determinism, idempotency, and output validity over a thousand generated inputs. Add
unit tests for the specific noise you remove and, more importantly, for the content
you must **not** remove.

## A new provider

`Provider` translates; `ProxyService` transports. You inherit streaming, header
preservation, connection pooling and error mapping.

```python
# gateway/providers/anthropic.py
class AnthropicProvider(Provider):
    name: ClassVar[str] = "anthropic"

    def authenticate(self, headers: httpx.Headers) -> None:
        if "x-api-key" in headers:
            return                                     # the caller's key always wins
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key.get_secret_value()

    def extra_headers(self) -> Mapping[str, str]:
        return {"anthropic-version": "2023-06-01"}
```

Register it and mount a router:

```python
registry.register(AnthropicProvider(base_url=..., api_key=...))
api_router.include_router(create_proxy_router(provider_name="anthropic", prefix="/anthropic/v1"))
```

Override `wants_stream` if the provider signals streaming somewhere other than a
`{"stream": true}` body field — Gemini puts it in the URL. Override `upstream_url` if
the credential goes in the query string.

To optimize that provider's payloads too, add a `PayloadAdapter` that knows where the
text lives in its request schema. Without one the requests are proxied, never
optimized — which is the safe default.

## A new policy rule

```python
class BlockLargeTenantRule(PolicyRule):
    name: ClassVar[str] = "tenant-optout"

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        if request.path == "chat/completions" and b'"x-no-optimize"' in request.body:
            return PolicyDecision(False, self.name, SkipReason.DISABLED)
        return None                                    # abstain: let later rules decide
```

Add it to `PolicyEngine.from_settings`. Rules run in order and the first decision
wins, so placement is the whole semantics: before `EndpointRule` to override the
allowlist, after it to refine.

Returning `None` abstains. Only `EndpointRule` never abstains, and that is deliberate:
an endpoint nobody has explicitly allowed is proxied, never optimized.

## What not to do

**Do not import a transformer from the gateway.** Ask the registry. `pipeline.py` has
no idea HTML exists, and it should stay that way.

**Do not measure your own performance.** Return `TransformOutput` and let the pipeline
time you. If two transformers measured themselves, they would measure differently.

**Do not use an LLM to optimize.** Optimization is deterministic, or the cache is
unsound, the analytics are unreproducible, and the cost saving is spent on the model
doing the saving.

**Do not log user content.** Sizes, counts, timings, names. Nothing else.
`tests/test_security.py` captures real log output and greps it for prompt text.
