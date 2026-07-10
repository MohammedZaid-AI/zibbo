# The transformation pipeline

The gateway does not know how optimization works. It makes one call:

```python
report = await pipeline.transform(request)
```

Everything else — what the content is, which transformer handles it, what it saved —
is decided inside, and reported back as data.

```
request  ->  PolicyEngine      may this be touched at all?
         ->  PayloadAdapter    where is the optimizable text in this schema?
         ->  ContentDetector   what is each segment, really?
         ->  TransformerRegistry  who handles that?
         ->  Transformer       rewrite it
         ->  TransformationReport  body to forward + every measurement
         ->  ProxyService
```

## Why the layers are where they are

**Policy is separate from transformation.** "Is this endpoint eligible" is a
business rule that changes with the product; "how do I clean HTML" is an algorithm.
Fused, every new endpoint would edit the transformation code.

**The pipeline transforms *segments*, not bodies.** Minifying a chat request's JSON
envelope saves nothing. The tokens are in `messages[i].content`, where a user pasted
a scraped page. A `PayloadAdapter` knows where that text lives in a given endpoint's
schema; the pipeline knows nothing about `messages`.

**Transformers only return text plus the names of the steps they applied.** Timing,
byte counts and token counts are computed by the pipeline, once, identically for all
of them. A transformer cannot forget to report tokens saved, cannot measure them
differently from its neighbour, and gains nothing to change when a metric is added.
This is what "the dashboard requires zero changes to the transformers" means in code.

**Detection is body inspection first.** The declared `Content-Type` is a hint. A
scraped page pasted into a chat message arrives inside `application/json` and is
still HTML.

## Adding a transformer

One module under `gateway/optimizers/transformers/`, one line in
`build_transformer_registry`. Nothing else in the gateway changes.

```python
class CsvTransformer(Transformer):
    name = "csv"
    priority = 30                       # lower runs first; specific beats general
    content_types = frozenset({ContentType.CSV})

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        ...
        return TransformOutput(markdown_table, ("converted_to_markdown",))
```

If it needs a new content type, add a `Sniffer` to `detector.py`. The detector itself
does not change. `MagicBytesSniffer` already exists for exactly this: Phase 7 extends
a table of file signatures.

The same seam takes PII masking, content hashing and deduplication: they are
transformers or policy rules, not gateway changes.

## The two invariants

Every transformer is **deterministic** — the same input yields the same output in
every process — and **idempotent**: `T(T(x)) == T(x)`. Both are asserted with
property-based tests over generated inputs.

Idempotency is not decoration. Phase 8 caches on a hash of the transformed body; a
transformer that produced two different outputs for one input would silently poison
that cache. And a request replayed through two gateway hops must not be transformed
twice into something different.

Making it hold exactly took one deliberate design choice. The HTML transformer emits
Markdown, and Markdown is not HTML — so:

* `HtmlSniffer` requires **two closing tags** before it calls something HTML.
  Markdown has none, so the HTML transformer's output is never re-detected as HTML.
* The HTML transformer finishes by running its Markdown through the *same normalizer*
  the text transformer uses. So when the second pass routes that Markdown to the text
  transformer, it is already in the exact form that transformer would produce.

The two-closing-tag rule also protects meaning. A user asking *"what does
`<p>hello</p>` do?"* is writing prose **about** markup. Treat it as markup and their
question is silently rewritten to "what does hello do?". One closing tag is prose;
a document has many.

## What each transformer does, and refuses to do

### HTML

Removes: `script`, `style`, `noscript`, `svg`, `canvas`, `iframe`, embedded media,
`head`; `nav`, `aside`, `footer`, `menu`, `dialog`; a `header` that is not inside an
`article`; ARIA landmark roles (`navigation`, `banner`, `dialog`, `contentinfo`, …);
elements whose `class` or `id` matches ad/cookie/consent/banner/sidebar/social/promo
patterns; hidden elements (`hidden`, `aria-hidden`, `display:none`,
`visibility:hidden`, `opacity:0`, `input[type=hidden]`).

Preserves and converts to Markdown: headings, paragraphs, ordered and unordered
lists with nesting, tables, code blocks with their indentation, blockquotes,
horizontal rules, inline emphasis and code, links, image references, and the document
`<title>` when there is no `<h1>`.

Noise-class matching is anchored on `-`, `_` and space, so `ad` never matches
`download`, `gradient` or `loading`.

**Links and images are kept by default.** A URL is content. Dropping it saves tokens
by destroying information, which is the one thing this pipeline promises never to do.
Set `LLMGATEWAY_HTML_PRESERVE_LINKS=false` if you disagree. `data:` URIs are the sole
exception: they are payload, not reference, and can be megabytes of base64. The `alt`
text survives.

### JSON

Removes pretty-printer whitespace and emits non-ASCII directly instead of as
`\uXXXX` escapes (`é` is one token; `é` is several). Key order is preserved.
Values are never touched.

**Duplicate keys.** JSON permits them; `json.loads` keeps the last and discards the
rest. The gateway *inherits* that behaviour rather than choosing it, and the detector
counts duplicates as it parses so the collapse is reported instead of silent. Every
JSON parser downstream would resolve the ambiguity the same way.

**Empty containers are kept by default.** `{"tools": []}` tells an API "no tools",
which is not what `{}` says. `LLMGATEWAY_JSON_REMOVE_EMPTY_CONTAINERS=true` opts in.

### Plain text

Normalizes line endings, strips trailing whitespace, collapses runs of blank lines to
one, and removes a paragraph identical to the one immediately before it. Consecutive
only — a phrase recurring later in a document is content, not boilerplate.

**Runs of spaces inside a line are not collapsed by default.** Indentation is meaning
in code; alignment is meaning in Markdown tables. `LLMGATEWAY_TEXT_COLLAPSE_INLINE_WHITESPACE=true`
enables it for deployments that know their content is prose.

## Policy

| Optimized | Never optimized |
|---|---|
| `chat/completions` | `files`, `uploads` |
| `responses` | `audio/*`, `images/*` |
| `assistants`, `threads/*` | `fine_tuning/*`, `batches` |
| | `embeddings`, `moderations` |

Only `POST`, only `application/json`, only bodies under
`LLMGATEWAY_OPTIMIZATION_MAX_BODY_BYTES` (8 MB).

The endpoint rule is an **allowlist that never abstains**. An endpoint OpenAI ships
tomorrow is proxied but not optimized until someone allows it explicitly. Getting
this backwards means silently corrupting a fine-tuning upload.

Rules are evaluated in order and the first to decide wins, so the kill switch
(`LLMGATEWAY_OPTIMIZATION_ENABLED=false`) precedes everything. Adding a rule — per
tenant, per model, per size — is one class.

## Optimization never costs more than it saves

A transformer can legitimately grow its input. Markdown table syntax outweighs CSV
commas on a narrow table; a short HTML fragment can gain more syntax than it sheds.

So the pipeline counts tokens before and after, and **discards any transformation
that increases the count**, forwarding the original. The worst a transformer can do
to a bill is nothing.

This lives in the pipeline rather than in each transformer because it is a property
of the product, not of any one format — and because a transformer cannot be trusted
to police itself. Reverted transformations are logged at `DEBUG` as
`transformation_reverted`.

## Transparency

If nothing changed, the **original bytes** are forwarded. Not a re-serialization of
them. A request whose content was already optimal crosses the gateway byte-for-byte,
exactly as it did in Phase 2.

Every response says what happened:

| Header | Meaning |
|---|---|
| `x-llmgateway-optimization: applied` | The body was rewritten |
| `x-llmgateway-optimization: skipped:<reason>` | Why it was not |
| `x-llmgateway-tokens-saved: 110` | Tokens removed, when applied |

## Performance

The body is parsed once. The detector's JSON parse is carried on the `Detection` and
reused by the JSON transformer rather than repeated. Segments hold a direct reference
to their container, so writing a result back is an assignment, not a second tree walk.

Above `LLMGATEWAY_OPTIMIZATION_OFFLOAD_THRESHOLD_BYTES` (128 KB) the work moves to a
worker thread. Parsing a multi-megabyte HTML document is tens of milliseconds of pure
CPU, and holding the event loop stalls every other in-flight request, including
active streams.

Measured throughput is roughly 3 MB/s for HTML and 5 MB/s for JSON and text; a 40 KB
document costs ~12 ms. See `python -m benchmarks.run`.

## Token counting

`TokenCounterFactory.for_model()` returns an exact tiktoken counter when the encoding
is available, and a deterministic heuristic counter when it is not.

This matters more than it looks. **tiktoken downloads its BPE files over the network
on first use.** A cold container that cannot reach the CDN would otherwise stall or
fail on its first request. So:

* The Docker image pre-warms the cache at build time (`TIKTOKEN_CACHE_DIR=/opt/tiktoken`).
  The runtime never reaches for the network; verified with `--network none`.
* Startup pre-warms in a worker thread, and logs `exact_token_counting`.
* A load failure is logged once and degrades to the heuristic counter. Reduction
  *percentages* stay sound, because both sides are measured the same way. Absolute
  token counts do not, and the analytics layer must say so.

## Logging

Every transformation emits one structured record: transformers used, content types,
segment count, tokens before and after, bytes before and after, execution time. The
request id arrives automatically from the logging contextvars.

**No user content is ever logged.** Only metadata.

```json
{"event": "optimization_applied", "transformers": ["html"], "content_types": ["html"],
 "segments": 1, "tokens_before": 122, "tokens_after": 12, "tokens_saved": 110,
 "token_reduction_pct": 90.16, "bytes_before": 449, "bytes_after": 113,
 "execution_time_ms": 1.083, "request_id": "req_5edb5c60c3bd4e99b4cf9725985ba94e"}
```
