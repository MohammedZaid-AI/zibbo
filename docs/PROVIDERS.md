# Providers

Zibbo is provider-agnostic. OpenAI and Anthropic are implemented; Groq, Mistral
and Ollama work today as OpenAI-compatible providers; Gemini is a documented next
step. The gateway core contains **no** provider-specific logic — every difference
between providers is data a provider module supplies.

## Provider architecture

A request crosses two layers:

```
route (per provider, at its prefix)
   │  resolves its Provider by name; holds no provider logic
   ▼
Provider              translates: URL, auth, endpoints, schema, error shape, stream framing
   ▼
ProxyService          transports: connection pool, buffered + streaming relay, header policy
   ▼
upstream
```

`Provider` **translates**; `ProxyService` **transports**. Everything a provider does
differently is expressed as configuration on the provider object, not as a branch in
shared code:

| Concern | How a provider expresses it | OpenAI | Anthropic |
|---|---|---|---|
| Credential placement | `auth: AuthStrategy` | `Authorization: Bearer` | `x-api-key` |
| Mandatory headers | `default_headers` | — | `anthropic-version` |
| Optimizable endpoints | `EndpointPolicy` | `chat/completions`, `responses`, … | `…/messages` |
| Request schema | `PayloadAdapter`s | `messages[i].content` | `system` + content blocks |
| Error envelope | `ErrorEnvelope` | `{"error": {…}}` | `{"type": "error", …}` |
| Stream error framing | `stream_error_frame` | SSE `data:` | SSE `data:` |

The base `Provider` is **concrete, not abstract**. A provider is *configured*, not
subclassed-to-fill-in; subclasses exist only to bundle a provider's defaults, and one
class (`OpenAICompatibleProvider`) serves several backends.

## Adding a provider

`AnthropicProvider` in full is the template. In three parts:

**1. Authentication** — reuse a strategy or write one.

```python
auth = ApiKeyHeaderAuth("x-api-key", also_accept=("authorization",))
```

`BearerAuth`, `ApiKeyHeaderAuth` and `NoAuth` cover every provider here. The strategy
enforces the rule the gateway must never break — *the caller's credential always
wins* — so a new provider cannot forget it.

**2. Endpoint policy and request schema** — what may be optimized, and where the
prose lives.

```python
ANTHROPIC_ENDPOINTS = EndpointPolicy(
    allowed_suffixes=("messages",),
    denied_prefixes=("v1/files", "v1/models", "v1/messages/batches"),
)

class AnthropicMessagesAdapter(PayloadAdapter):
    def matches(self, path: str) -> bool:
        return path.strip("/").endswith("messages")
    def extract(self, payload):
        # yield the `system` string and each text content block
        ...
```

An endpoint with no adapter is proxied but never optimized — the safe default. A new
provider that only needs a chat endpoint can often reuse `ChatCompletionsAdapter`.

**3. Error envelope** — so a gateway-authored failure looks native.

```python
class AnthropicErrorEnvelope:
    def render(self, *, message, error_type, code, param, request_id):
        return {"type": "error", "error": {"type": "api_error", "message": message}, ...}
```

Then bundle the defaults and register:

```python
class AnthropicProvider(Provider):
    name = "anthropic"
    auth = ApiKeyHeaderAuth("x-api-key", also_accept=("authorization",))
    def __init__(self, *, base_url, api_key=None, version="2023-06-01"):
        self.default_headers = {"anthropic-version": version}
        super().__init__(base_url=base_url, api_key=api_key,
                         endpoint_policy=ANTHROPIC_ENDPOINTS,
                         adapters=anthropic_adapters(),
                         error_envelope=AnthropicErrorEnvelope())
```

`build_providers` in `gateway/main.py` is the one place providers are named. Add a
line there and the provider mounts.

### Gemini, the interesting case

Gemini is not OpenAI-shaped, and the abstraction was built with it in mind:

* Streaming is signalled in the **URL** (`:streamGenerateContent`), not the body —
  override `wants_stream`.
* The credential goes in a **query parameter** or `x-goog-api-key` — an `AuthStrategy`
  plus an `upstream_url` override.
* The schema is `contents[].parts[].text` — a new `PayloadAdapter`.

None of that touches the core. It is a provider module.

## Authentication

The caller's credential always wins; a configured key is the fallback for callers
that send none. This is what makes the drop-in promise true — an app already sending
its own key keeps billing to its own account, and the gateway needs no credential at
all. When neither is present the gateway sends nothing and lets the provider issue its
own 401, which an SDK's error handling already expects.

| Strategy | Header | Providers |
|---|---|---|
| `BearerAuth` | `Authorization: Bearer <key>` | OpenAI, Groq, Mistral, Ollama |
| `ApiKeyHeaderAuth` | a named header, bare key | Anthropic (`x-api-key`), Gemini (`x-goog-api-key`) |
| `NoAuth` | — | a local Ollama with no key |

`ApiKeyHeaderAuth` can *also accept* other headers as caller credentials — Anthropic
takes an OAuth bearer token as well as an API key, so a caller using OAuth does not
additionally get our `x-api-key` bolted on.

## Streaming

Normalized without being flattened. OpenAI and Anthropic both frame SSE as `data:`
lines, but their **event models differ** — OpenAI streams `chat.completion.chunk`
objects, Anthropic streams typed events (`message_start`, `content_block_delta`,
`message_stop`). The gateway relays those bytes untouched; it does not translate one
provider's stream into another's. Back-pressure and time-to-first-token are the
provider's, plus one proxy hop.

The one place the gateway adds bytes to a stream is a mid-stream upstream failure.
Because headers are already sent, no HTTP error is possible, and stopping silently
would let the SDK treat a truncated answer as complete. Instead a final error frame is
emitted, **framed by the provider** so its SDK raises from it — OpenAI-shaped for
OpenAI, Anthropic-shaped for Anthropic.

## Configuration and routing

Providers are selected by **route prefix**: the caller picks a provider by the URL its
SDK points at.

| Provider | Prefix | Point your SDK's `base_url` at |
|---|---|---|
| OpenAI | `/v1` | `http://gateway/v1` |
| Anthropic | `/anthropic` | `http://gateway/anthropic` |
| Groq | `/groq/v1` | `http://gateway/groq/v1` |
| Mistral | `/mistral/v1` | `http://gateway/mistral/v1` |
| Ollama | `/ollama/v1` | `http://gateway/ollama/v1` |

The prefixes differ because the SDKs construct URLs differently: the OpenAI SDK carries
`/v1` in its base URL and appends `/chat/completions`, while the Anthropic SDK takes an
origin and appends `/v1/messages`. Each prefix mirrors what that SDK expects, so the
promise holds: **change only `base_url`.** Every prefix is configurable, and two
providers sharing one is a startup error.

### Why route prefixes, and not the alternatives

* **Route prefix (chosen).** Explicit, debuggable — the URL says which provider — and
  it lets one gateway serve several. It mirrors each SDK's own URL construction, so a
  caller changes exactly one string. Cost: the caller must know which prefix, and the
  prefixes are not uniform (`/anthropic` vs `/ollama/v1`), because the SDKs are not.
* **Model-name routing** (infer the provider from `"model": "claude-…"`). Zero caller
  change, but it forces the gateway to parse and understand every body, breaks when
  two providers share a model name, and cannot express "the same model through a
  different account". Rejected: it trades transparency for a guess.
* **Host-based routing** (`api.openai.com` → gateway via DNS). The most transparent —
  the caller changes nothing but DNS — but it needs per-provider hostnames, TLS certs
  and DNS control, which is deployment-specific. Compatible with the prefix design and
  can be layered on later at the reverse proxy; not the gateway's job.

Enabling a provider: OpenAI and Anthropic are on by default (set `*_ENABLED=false` to
remove one). An OpenAI-compatible provider turns on when its base URL is set — there
is no universal default for Groq, Mistral or Ollama.

```bash
ZIBBO_ANTHROPIC_API_KEY=sk-ant-...        # fallback; caller's key still wins
ZIBBO_GROQ_BASE_URL=https://api.groq.com/openai/v1
ZIBBO_GROQ_API_KEY=gsk_...
ZIBBO_OLLAMA_BASE_URL=http://localhost:11434/v1   # no key needed
```

## Upstream TLS verification

The upstream client verifies every provider certificate against the **operating
system trust store** — the same anchors `curl`, the OS, and Claude Code itself use —
rather than only certifi's bundled public roots. Verification is never disabled;
`truststore` performs full certificate-chain and hostname checks (`CERT_REQUIRED`).

This is what lets the gateway run where the assistant already does. HTTPS-inspection
software — Avast Web Shield, Zscaler, Netskope, a corporate proxy — re-signs upstream
certificates with a CA it installs in the OS trust store. certifi does not know that
CA, so a certifi-only client rejects every handshake (`CERTIFICATE_VERIFY_FAILED`) and
every request 502s, even though the assistant on the same machine works. Trusting the
OS store means the gateway trusts exactly what the machine trusts.

If `truststore` is somehow unavailable, the client falls back to certifi verification
(still full validation, never `verify=False`).

**Deployment note.** On Linux, "OS trust store" is the system CA bundle
(`/etc/ssl/certs`), which `truststore` reads via OpenSSL — it ships no bundle of its
own there. The provided Docker image installs `ca-certificates` for this reason; a
self-built minimal image (distroless, `scratch`, stripped Alpine) must include a
system CA bundle, as any TLS client would require. Operators behind an inspecting
proxy add their CA to that store (`/usr/local/share/ca-certificates` +
`update-ca-certificates`) and the gateway trusts it — no code or config change.

## Compatibility guarantees

Verified with each provider's official SDK, whose only non-default argument is
`base_url` (`tests/test_openai_sdk_compat.py`, `tests/test_anthropic_sdk_compat.py`).

* **Request and response bodies are relayed byte-for-byte** unless optimization
  rewrote message content, and optimization only ever touches user-authored prose.
* **A provider's own errors are relayed verbatim** — status, body, headers. Only a
  failure with no upstream response becomes a gateway-authored error, and that error
  is rendered in *that provider's* envelope.
* **Each provider keeps its own auth, headers, error shape and stream format.** The
  gateway does not homogenize them.
* **The caller's credential always wins.**

Per-provider deviations are in [COMPATIBILITY.md](COMPATIBILITY.md). The intentional
differences there (added `x-zibbo-*` headers, request-body buffering, no retries,
the mid-stream error frame) apply to every provider.
