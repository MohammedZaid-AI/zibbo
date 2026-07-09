# OpenAI API compatibility

LLMGateway aims to be indistinguishable from the upstream provider to any client
that changes only its `base_url`. This document records every place where it is
*not*, and why.

As of Phase 3 the gateway **optimizes request bodies** on a small allowlist of
endpoints. Response bodies are still never modified. See
[OPTIMIZATION.md](OPTIMIZATION.md) for the full design, and difference 11 below for
what that means for compatibility.

## Verified compatible

Exercised in `tests/test_proxy_openai.py` and, using the official `openai` Python
SDK as the client, in `tests/test_openai_sdk_compat.py`.

- **Every endpoint under `/v1`.** The proxy is a catch-all, so `chat/completions`,
  `embeddings`, `models`, `moderations`, `files`, and endpoints that do not exist
  yet all work. The gateway does not model payloads it does not modify.
- **All methods**: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`. Query strings included.
- **Response bodies are byte-identical**, never re-serialized. JSON key order and
  whitespace survive.
- **Streaming (`stream: true`)** relays SSE frames as they arrive. Time-to-first-token
  measured at 26 ms through the gateway versus 27 ms direct to upstream.
- **Upstream errors pass through untouched** — status code, body, and headers.
  OpenAI's error envelope already is what its SDK parses, so the gateway must not
  rewrap it. `openai.BadRequestError`, `RateLimitError`, and `InternalServerError`
  all raise as they would without the gateway.
- **Retry and diagnostic headers survive**: `x-ratelimit-*`, `retry-after`,
  `x-should-retry`, `openai-organization`, `openai-processing-ms`, `openai-version`.
  SDK backoff depends on these.
- **Request headers pass through**, including `OpenAI-Beta`, `OpenAI-Organization`,
  `OpenAI-Project`, and any header invented after this code was written. The policy
  is a denylist, not an allowlist.
- **Authentication is transparent.** The caller's `Authorization` header reaches
  OpenAI unmodified, so requests bill the caller's account, not the gateway's.

## Intentional differences

### 1. `x-request-id` is the provider's; the gateway's id has its own header

A proxied response carries **upstream's** `x-request-id`. The gateway's own
correlation id is always in `x-llmgateway-request-id`.

`openai.APIError.request_id` reads `x-request-id`, and that value is what OpenAI
support asks for. Overwriting it would make every gateway user's support ticket
unanswerable. On routes the gateway serves itself (`/health`, 404s), `x-request-id`
is the gateway's, since no upstream claimed it.

### 2. Two headers are added

`x-llmgateway-request-id` on every response, and `x-process-time` (milliseconds to
response headers). Streaming responses also get `x-accel-buffering: no`, which stops
nginx from buffering an SSE body and silently destroying incremental delivery.

### 3. `Content-Encoding` is terminated at the gateway

Response bodies are decompressed by the gateway and forwarded uncompressed;
`Content-Encoding` and the upstream `Content-Length` are not relayed. Payload bytes
are unchanged — only the transport framing is. Clients decompress transparently, so
this is invisible above the HTTP layer.

### 4. Hop-by-hop headers are dropped

`Connection`, `Keep-Alive`, `Transfer-Encoding`, `TE`, `Trailer`, `Upgrade`,
`Proxy-Authenticate`, `Proxy-Authorization`, `Proxy-Connection`. Required by
RFC 9110 §7.6.1: they describe one connection and must not be relayed onto another.
`Date` and `Server` are also dropped, because our own server emits them.

### 5. Gateway-originated errors have types OpenAI never returns

When the provider cannot be reached at all, there is no upstream response to relay,
so the gateway synthesizes one in OpenAI's envelope:

| Condition | Status | `error.type` | `error.code` |
|---|---|---|---|
| Connection refused, DNS failure, TLS error | `502` | `upstream_error` | `upstream_error` |
| Connect or read timeout | `504` | `upstream_error` | `upstream_timeout` |

SDKs treat 502 and 504 as retryable, so default retry behaviour is correct. A client
that switches on `error.type` will not recognize `upstream_error`; there is no
alternative, since the condition itself has no upstream analogue.

### 6. Redirects are not followed

A 3xx from the provider is handed to the caller, which is what a transparent proxy
should do. The SDK decides.

### 7. `Expect: 100-continue` is not honoured

The header is stripped. The gateway reads the full request body before contacting
upstream, so the continue handshake has nothing to gate.

### 8. Request bodies are fully buffered

The gateway reads the entire request body into memory before forwarding. For chat
completions this is negligible. For very large `POST /v1/files` uploads it means
memory proportional to file size, and the upload cannot begin until it is complete.

This is not laziness — Phase 3's optimizers must see the whole body to rewrite it,
so a streamed request body could not be optimized anyway. Revisit if large file
uploads become a real workload; the fix is to bypass optimization and stream the
body for endpoints that never carry optimizable content.

### 9. Percent-encoded path segments are normalized

The path is decoded by the router and re-encoded when the upstream URL is built. A
path containing `%2F` would reach OpenAI as `/`. No current OpenAI endpoint uses
encoded separators.

### 10. HTTP/2 is not used upstream

The gateway speaks HTTP/1.1 to the provider. No semantic difference; a connection
pool amortizes handshakes.

### 11. Request bodies are optimized on eligible endpoints

On `POST` to `chat/completions`, `responses`, `assistants` and `threads/*`, with a
JSON body, the text inside message content is rewritten before forwarding: HTML
becomes Markdown, pretty-printed JSON is minified, prose is normalized. Everything
else in the payload — `model`, `tools`, `temperature`, `stream`, tool call arguments,
image parts — is untouched.

This is the product. It is nevertheless a difference from the upstream API, and it
has consequences worth stating plainly:

* **The provider does not receive the bytes the caller sent** on those endpoints.
  If a caller needs byte-exact prompt delivery, set `LLMGATEWAY_OPTIMIZATION_ENABLED=false`
  and the gateway reverts to the Phase 2 pure passthrough.
* **A request whose content is already optimal crosses byte-for-byte.** Optimization
  only re-serializes the body when a segment actually changed.
* **Content is never summarized, reordered, or invented.** Only structural noise is
  removed. Transformations are deterministic and idempotent.
* **Prompt caching.** Providers cache on an exact prefix match. Because
  transformation is deterministic, the same input still yields the same optimized
  prefix, so caching keeps working — but a prefix cached *before* the gateway was
  introduced will miss once.
* **Token counts in the response `usage` field are the provider's**, and reflect the
  optimized prompt. That is the point: it is the smaller number you are billed for.
* File uploads, audio, images, fine-tuning, batches, embeddings and moderations are
  **never** optimized.

Two response headers are added: `x-llmgateway-optimization` (`applied` or
`skipped:<reason>`) and `x-llmgateway-tokens-saved`.

### 12. Percent-encoded path segments, revisited

Unchanged from Phase 2, but worth repeating alongside the optimizer: the gateway
decodes and re-encodes the path. No current OpenAI endpoint uses encoded separators.

## Not yet implemented

- Anthropic-compatible endpoint (Phase 6).
- Persisted token and cost analytics (Phase 4). Savings are measured and logged per
  request, but not stored.
- PDF, DOCX and CSV optimizers (Phase 7).
- Response caching (Phase 8). Every request reaches the provider.
