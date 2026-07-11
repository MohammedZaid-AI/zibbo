# Architecture

LLMGateway sits between an application and an LLM provider. The application changes
one string — its `base_url` — and everything else keeps working, while the requests
that cross the gateway get smaller.

Two ideas carry the design:

* **Transparency is the default, optimization is the exception.** A request that
  cannot be safely improved is forwarded byte-for-byte. Everything the gateway does
  beyond that is opt-out.
* **Nothing in the middle knows what is on either side.** The proxy does not know
  HTML exists. The transformers do not know HTTP exists. The provider does not know
  about sockets.

## Request lifecycle

```
      client
        │  POST /v1/chat/completions
        ▼
┌───────────────────────────────────────────────────────────┐
│ ServerErrorMiddleware        catches anything unhandled   │
│ ┌───────────────────────────────────────────────────────┐ │
│ │ RequestContextMiddleware   request id, timing, access │ │
│ │ ┌───────────────────────────────────────────────────┐ │ │
│ │ │ CORSMiddleware                                    │ │ │
│ │ │ ┌───────────────────────────────────────────────┐ │ │ │
│ │ │ │ ExceptionMiddleware   GatewayError -> envelope│ │ │ │
│ │ │ │ ┌───────────────────────────────────────────┐ │ │ │ │
│ │ │ │ │ proxy route                               │ │ │ │ │
│ │ │ │ │   1. read body                            │ │ │ │ │
│ │ │ │ │   2. pipeline.transform(request)          │ │ │ │ │
│ │ │ │ │   3. proxy.forward(provider, body)        │ │ │ │ │
│ │ │ │ └───────────────────────────────────────────┘ │ │ │ │
│ │ │ └───────────────────────────────────────────────┘ │ │ │
│ │ └───────────────────────────────────────────────────┘ │ │
│ └───────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
        │
        ▼  provider
```

Middleware order is load-bearing. `RequestContextMiddleware` wraps CORS so even a
preflight response carries a request id, and it is raw ASGI rather than
`BaseHTTPMiddleware` because the latter buffers responses through an anyio stream
and would destroy streaming.

Three details that are easy to get wrong:

**The request id.** A caller's `X-Request-ID` is honoured if it is printable and
short; otherwise one is generated. On a *proxied* response, `x-request-id` carries
the **provider's** id — that is what an SDK surfaces in exceptions and what provider
support asks for — and the gateway's own id moves to `x-llmgateway-request-id`.

**Context variables, not parameters.** The route binds `provider`, `endpoint`, and
the optimization metrics to structlog's contextvars. Every log line emitted while
handling that request, including the access log written by middleware that knows
nothing about optimization, carries them.

**Errors.** Anything raised below is converted to OpenAI's error envelope. A failure
*with* an HTTP response (a provider 429) is relayed verbatim, because its body is
already what the caller's SDK parses. Only failures with no HTTP response —
connection refused, timeout — become gateway-authored errors.

## The pipeline

```
TransformationRequest
        │
        ▼
   PolicyEngine ─────── no ──▶ forward original bytes, record why
        │ yes
        ▼
   parse body once
        │
        ▼
   PayloadAdapter  ──▶ segments: (container, key, text, origin)
        │
        ▼   for each segment
   ContentDetector ──▶ Detection(type, confidence, parsed)
        │
        ▼
   TransformerRegistry ──▶ Transformer | None
        │
        ▼
   Transformer.transform ──▶ TransformOutput(content, steps)
        │
        ▼
   pipeline measures bytes, tokens, time
        │
        ▼
   TransformationReport ──▶ ProxyService
```

Policy is separate from transformation because "is this endpoint eligible" is a
business rule that changes with the product, while "how do I clean HTML" is an
algorithm. Fused, every new endpoint would edit transformation code.

The pipeline transforms **segments, not bodies**. Minifying a chat request's JSON
envelope saves nothing; the tokens are in `messages[i].content`, where a user pasted
a scraped page. An adapter knows where text lives in a schema. The pipeline does not
know that `messages` exists.

Segments hold a direct reference to their container and key, so writing a result back
is an assignment rather than a second walk of the tree. The body is parsed once; the
detector's JSON parse rides along on the `Detection` and is reused by the JSON
transformer instead of being repeated.

Above `optimization_offload_threshold_bytes` (128 KB) the whole synchronous core runs
in a worker thread. Parsing a multi-megabyte document is tens of milliseconds of pure
CPU, and holding the event loop stalls every other in-flight request, including
active streams.

A segment may also be a **document**: a base64 PDF, DOCX, CSV or XML in a provider
`document`/`file` block. The pipeline hands it to a `DocumentService`, which decodes,
detects the real format from its bytes, extracts Markdown, and — only if that is
cheaper — rewrites the block. Every document format is an isolated extractor; the
pipeline knows only the service. See [DOCUMENT_TRANSFORMERS.md](DOCUMENT_TRANSFORMERS.md).

If nothing changed, the **original bytes** are forwarded — not a re-serialization of
them.

## Transformer architecture

A transformer answers two questions and performs one action:

```python
class Transformer(ABC):
    name: ClassVar[str]
    priority: ClassVar[int]            # lower wins; specific beats general
    content_types: ClassVar[frozenset[ContentType]]

    def can_handle(self, content: str, detection: Detection) -> bool: ...
    def transform(self, content: str, detection: Detection) -> TransformOutput: ...
```

`TransformOutput` is text plus the names of the steps applied. **Nothing else.** No
timing, no byte counts, no token counts — the pipeline derives every metric, once,
identically for all transformers. A transformer cannot forget to report tokens saved
or measure them differently from its neighbour, and adding a metric is one change in
`models.py` rather than one per transformer. That is what makes the dashboard work
that Phase 5 will build require zero transformer changes.

Two invariants, enforced by property-based tests over thousands of generated inputs:

* **Deterministic.** `T(x)` is the same in every process. The transformation cache
  keys on a hash of the input; a non-deterministic transformer would poison it.
* **Idempotent.** `T(T(x)) == T(x)`. A request replayed through two gateway hops must
  not be transformed twice into something different.

Idempotency holds *exactly*, by construction rather than by luck. The HTML transformer
emits Markdown, and Markdown is not HTML: `HtmlSniffer` requires closing tags, which
Markdown has none of, so the output is never re-detected as HTML. And the HTML
transformer finishes by running its Markdown through the *same normalizer* the text
transformer uses, so the second pass is a no-op.

Transformers also never raise into the request. A transformer that throws is logged,
its segment is skipped, and the original content is forwarded.

And a transformer cannot make things worse. The pipeline counts tokens on both sides
and discards any transformation that increased them — a property of the product, not
of any one format, and not something a transformer can be trusted to police itself.

## Plugins

A transformer need not live in the gateway. `gateway/plugins/` discovers transformers
from Python entry points, a local directory, or explicit configuration, constructs
them behind per-plugin error handling, and attaches them to the same registry and
detector the builtins use.

The invariant: **a broken plugin never prevents the gateway from starting, and never
affects another plugin.** Every stage — import, construct, validate, register — is
wrapped per plugin, and a failure is recorded with the stage that failed. Otherwise a
`pip install` of a third-party package could take production offline.

Plugins may contribute a `Sniffer` as well as a `Transformer`, because a transformer
for a content type the detector cannot recognize would never run. Capabilities
(`deterministic`, `idempotent`) are *enforced*: a plugin that does not declare them
does not load, since the pipeline's cache and idempotency guarantees depend on them.

See [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md).

## Transformation cache

Optimization is deterministic, so transforming the same content twice is waste.
`gateway/cache/` does it once: hash the input, look it up, and on a hit reuse the stored
result instead of re-extracting and re-counting. The pipeline consults one object,
`TransformationCache`, and the gateway core is unaware the cache exists.

The design turns on two decisions. First, the **cache key** commits to everything that
determines a transformation's output — the content hash, a fingerprint of the applicable
transformers and their versions, the gateway version, the options in force, and the
tokenizer encoding (because token counts are cached too). Invalidation is therefore not
an operation but a consequence: change any of these and the key changes, so a stale
result can never be served. Second, a **backend is just a byte store** with per-key
expiry, which is what makes in-memory, Redis, and a future Postgres or S3 store
interchangeable.

Safety mirrors the rest of the gateway. A failed or partial transformation is never
cached — only completed, deterministic outputs. A corrupt stored entry is treated as a
miss, dropped, and recomputed. A backend failure (Redis down) degrades to a miss rather
than an error: **a cache outage slows the gateway, it never breaks it.** Provider
responses are never cached; only the gateway's own pre-processing is reused.

See [CACHE.md](CACHE.md).

## Content detection

The declared `Content-Type` is a hint, not evidence. A scraped web page pasted into a
chat message arrives inside `application/json` and is still HTML.

Detection is a list of `Sniffer`s; the highest-confidence verdict at or above 0.7
wins, otherwise the declared type breaks the tie, otherwise plain text. Adding PDF,
DOCX or CSV detection is a new sniffer, not an edit to the detector — `MagicBytesSniffer`
already exists as the extension point.

The HTML sniffer is the subtle one. Counting tags cannot distinguish markup *used as
content* from markup *being talked about*: a generated encyclopedia article has a
markup density of 0.155 and the sentence "To make a paragraph use `<p>text</p>`, to
bold use `<b>text</b>`" has 0.119. No threshold separates them. What does separate
them is where the markup starts, so:

1. Fenced and inline code regions are removed first — markup quoted there is a
   question or a documentation example, never content.
2. A doctype or `<html>`/`<body>`/`<head>` settles it.
3. Otherwise the content must **open with a tag** and close at least two, or be more
   than half markup by character count.

Everything else is prose, and prose gets normalized rather than rewritten. The failure
this avoids is silent: treat "what does `<p>hello</p>` do?" as HTML and the user's
question becomes "what does hello do?".

## Provider abstraction

```python
class Provider(ABC):
    name: ClassVar[str]

    def upstream_url(self, path: str, query: str) -> httpx.URL: ...
    def authenticate(self, headers: httpx.Headers) -> None: ...      # the only abstract one
    def extra_headers(self) -> Mapping[str, str]: ...
    def wants_stream(self, payload, path) -> bool: ...
```

`Provider` **translates**: where a path lives upstream, how to authenticate, which
endpoints carry optimizable prose, where that prose sits in the body, what shape its
errors take, how it frames a mid-stream error. `ProxyService` **transports**: the
connection pool, the buffered and streaming response paths, header preservation.

The gateway core holds **no** provider-specific logic. A provider expresses every
difference as data it hands the base class — an `AuthStrategy`, an `EndpointPolicy`,
a set of `PayloadAdapter`s, an `ErrorEnvelope`. So OpenAI and Anthropic — different
credential header, different request schema, different error shape, different stream
events — share the same transport, policy engine, pipeline and route.

Providers are selected by **route prefix**: `/v1` is OpenAI, `/anthropic` is
Anthropic, `/{groq,mistral,ollama}/v1` are OpenAI-compatible backends. A caller picks
one by the URL its SDK points at. Full rationale, and the auth/streaming/error
contracts, are in [PROVIDERS.md](PROVIDERS.md).

Header policy is a **denylist** (hop-by-hop, RFC 9110 §7.6.1) rather than an
allowlist, so a `x-ratelimit-*` dimension OpenAI invents next year reaches the caller
without a code change. `httpx.Headers` is used rather than a dict so repeated headers
survive.

### Streaming ownership

`_StreamRelay` is an explicit iterator, not an async generator, and that is deliberate.
A generator's `finally` only runs if the generator was *started*; a caller who
disconnects between the response headers and the first chunk would leak an upstream
connection — slowly, invisibly, until the pool starved. `_StreamRelay.aclose()` closes
the response whether iteration began or not, is idempotent, and is also attached as a
Starlette `BackgroundTask`.

When the upstream breaks mid-stream, headers are already sent and no HTTP error can be
returned. Stopping silently is the worst outcome: the caller's SDK sees a clean
end-of-stream and hands back a truncated answer as if it were complete. Instead a final
SSE frame carrying `error` is emitted, which the OpenAI SDK's stream decoder turns back
into an exception.

## Policy engine

Rules are consulted in order; the first to return a decision wins; a rule that returns
`None` abstains.

| Rule | Decides |
|---|---|
| `OptimizationEnabledRule` | The kill switch. First, so it always wins. |
| `MethodRule` | Only `POST` carries an optimizable body. |
| `BodySizeRule` | Empty, or above the cap. |
| `ContentTypeRule` | Only `application/json`. |
| `EndpointRule` | An allowlist. **Never abstains.** |

The endpoint rule never abstaining is the important property. An endpoint OpenAI ships
tomorrow is proxied but **not** optimized until someone allows it explicitly, because
getting that backwards means silently corrupting a fine-tuning upload.

Adding a rule — per tenant, per model, per size, per time of day — is one class and one
line in `PolicyEngine.from_settings`.

## Observability

One structured record per request, metadata only. Never a byte of user content.

```json
{"event": "request_completed", "request_id": "req_5edb...", "provider": "openai",
 "endpoint": "/chat/completions", "method": "POST", "status_code": 200,
 "duration_ms": 18.4, "request_bytes": 4914, "response_bytes": 289,
 "optimization_applied": true, "transformers": ["html"],
 "tokens_before": 122, "tokens_after": 12, "tokens_saved": 110}
```

Credentials never reach a log: the configured key is a `SecretStr`, the caller's
`Authorization` header is forwarded but never recorded, and `tests/test_security.py`
captures real log output and greps it to prove both.

## Failure model

| Condition | Result |
|---|---|
| Provider returns 4xx/5xx | Relayed verbatim: status, body, headers |
| Connection refused, DNS, TLS | `502` `upstream_error` in OpenAI's envelope |
| Connect / read / pool timeout | `504` `upstream_timeout` |
| Stream breaks after headers | Final SSE frame carrying `error` |
| Client disconnects mid-stream | Upstream connection closed immediately |
| Transformer raises | Logged; segment skipped; original content forwarded |
| Body above the size cap | Proxied unoptimized |
| Unhandled exception | `500` `internal_error`, no internals disclosed |
