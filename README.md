# Zibbo

A drop-in API gateway that sits between your application and its LLM provider.

Point your SDK's `base_url` at Zibbo and it deterministically optimizes every
request before forwarding it upstream — stripping structural noise from HTML, PDFs,
spreadsheets, JSON blobs and email threads, and normalizing what remains to clean
Markdown. Fewer tokens, lower cost, cleaner context.

```diff
- base_url = "https://api.openai.com/v1"
+ base_url = "https://gateway.local/v1"
```

Nothing else changes.

## Principles

- **No LLM in the optimization path.** Optimization is deterministic, fast, and
  reproducible — the same input always yields the same output.
- **Content is never summarized or rewritten.** Only structural noise is removed.
  Meaning is preserved, byte for byte, where it matters.
- **Provider-compatible failures.** Errors use the provider's own envelope, so a
  caller's existing error handling keeps working.

## Status

Phase 9 of 9. The gateway proxies OpenAI and Anthropic transparently, streaming
included, deterministically optimizes eligible request payloads and embedded documents,
caches those transformations so identical content is processed only once, and ships an
editor plugin (Claude Code / Codex) and a `zibbo` CLI over a loopback control API.

| Phase | Scope | State |
|---|---|---|
| 1 | App skeleton, config, logging, errors, health, Docker | done |
| 2 | OpenAI-compatible proxy, provider layer, streaming | done |
| 3 | Transformation pipeline: HTML / JSON / text | done |
| 4 | Production hardening: chaos, streaming, security, benchmarks | done |
| 5 | Plugin architecture and transformation SDK | done |
| 6 | Multi-provider: OpenAI, Anthropic, Groq, Mistral, Ollama | done |
| 7 | Document transformers: PDF, DOCX, CSV, XML, HTML | done |
| 8 | Transformation cache: content-addressed, in-memory + Redis | done |
| 9 | Editor integration: `zibbo` CLI, internal API, Claude Code + Codex plugins | done |
| — | Token + cost analytics persistence (Postgres) | next |
| — | Next.js dashboard | |

Reference: [ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[PROVIDERS.md](docs/PROVIDERS.md) ·
[DOCUMENT_TRANSFORMERS.md](docs/DOCUMENT_TRANSFORMERS.md) ·
[CACHE.md](docs/CACHE.md) ·
[PLUGIN_ARCHITECTURE.md](docs/PLUGIN_ARCHITECTURE.md) ·
[EXTENDING.md](docs/EXTENDING.md) ·
[PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md) ·
[COMPATIBILITY.md](docs/COMPATIBILITY.md) ·
[OPTIMIZATION.md](docs/OPTIMIZATION.md)

## What it saves

Measured on generated corpora with `python -m benchmarks.run`, reproducible offline:

| Document | Bytes | Tokens | Token reduction |
|---|---|---|---|
| News article (ads, banners, nav) | 12.4 KB → 4.9 KB | 3,594 → 1,076 | **70.1%** |
| Documentation page | 14.0 KB → 7.2 KB | 4,302 → 1,837 | **57.3%** |
| Pretty-printed JSON API response | 41.1 KB → 19.2 KB | 9,569 → 5,708 | **40.4%** |
| Encyclopedia article | 42.0 KB → 32.4 KB | 11,101 → 7,649 | **31.1%** |
| Plain-text notes | 16.1 KB → 13.2 KB | 3,461 → 2,887 | **16.6%** |

## Quick start

### Docker (recommended)

```bash
docker compose up --build
```

Postgres and Redis start alongside the gateway. Redis backs the transformation cache
when `ZIBBO_CACHE_BACKEND=redis` (the default is in-memory, needing neither);
Postgres is reserved for the analytics phase.

### Local

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
python -m gateway               # http://localhost:8000
```

## Using it

Point any OpenAI client at the gateway. Nothing else changes — your key is
forwarded, so requests still bill your account.

```python
from openai import OpenAI

client = OpenAI(api_key="sk-...", base_url="http://localhost:8000/v1")

completion = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Say hello."}],
)

# Streaming works identically.
for chunk in client.chat.completions.create(model="gpt-4o-mini", messages=[...], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

Every `/v1` endpoint is proxied, not just chat completions — including ones OpenAI
has not shipped yet. Upstream errors, rate-limit headers, and request ids reach you
unchanged. The full list of intentional deviations is in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

Paste a web page into a message and the gateway strips the scripts, styles,
navigation, cookie banners, ads and hidden elements, converts what remains to clean
Markdown, and forwards that. Your headings, lists, tables and code blocks survive.
Nothing is summarized. Each response tells you what happened:

```
x-zibbo-optimization: applied
x-zibbo-tokens-saved: 110
```

Attach a PDF, DOCX, CSV or XML (as a provider `document`/`file` block) and the gateway
extracts it to clean Markdown before forwarding — 70–90% fewer tokens than the base64,
which the model reads better anyway. A document it cannot read is passed through
untouched. See [docs/DOCUMENT_TRANSFORMERS.md](docs/DOCUMENT_TRANSFORMERS.md).

Because optimization is deterministic, its results are cached: send the same document
or web page twice and the second request reuses the first's extraction instead of
redoing it — a warm 100-page PDF drops from seconds to under two milliseconds. The cache
is content-addressed (SHA-256), in-memory by default or Redis for a shared, multi-replica
store, and it caches only transformation outputs — never provider responses, never a
failed extraction. Each response says `x-zibbo-cache: hit|miss|partial`. See
[docs/CACHE.md](docs/CACHE.md).

Set `ZIBBO_OPTIMIZATION_ENABLED=false` for a pure passthrough. Design and
guarantees: [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md).

## Inside Claude Code and Codex

Zibbo ships an editor plugin and a `zibbo` CLI so the gateway lives where you code.

```
# Claude Code
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo

# then point the editor at the gateway (the plugin can't rewrite live traffic):
export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic
```

Inside Claude Code, `/zibbo`, `/zibbo stats`, `/zibbo doctor`, `/zibbo benchmark` show
savings and controls without leaving the session; a session-start hook starts the gateway
for you. From any shell, the same is `zibbo status`, `zibbo stats`, `zibbo doctor`. The
plugin is UX only — every number comes from the gateway's loopback control API. Full
design, including Codex setup and the internal API: [docs/PLUGIN_ARCHITECTURE.md](docs/PLUGIN_ARCHITECTURE.md).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| any | `/v1/*` | OpenAI-compatible proxy |
| any | `/anthropic/*` | Anthropic-compatible proxy |
| any | `/{groq,mistral,ollama}/v1/*` | OpenAI-compatible providers (when configured) |
| `GET` | `/internal/plugins` | Discovered plugins, including failures and why |
| `GET` | `/internal/cache` | Transformation-cache backend, counters, and hit rate |
| `GET` | `/health` | Service summary: version, environment, uptime |
| `GET` | `/health/live` | Liveness. Touches no dependency, never fails while the process runs |
| `GET` | `/health/ready` | Readiness. Probes every dependency; `503` if any is unhealthy |
| `GET` | `/docs` | OpenAPI UI (disabled in production) |

Liveness and readiness are deliberately separate: an orchestrator should restart a
pod that fails liveness, but only pull a pod out of the load balancer when it fails
readiness. A gateway whose database blips should stop taking traffic, not be killed.

## Configuration

Every setting is an environment variable prefixed with `ZIBBO_`, mapping onto a
field of `Settings` in [gateway/config.py](gateway/config.py). See
[.env.example](.env.example) for the full list.

Configuration is validated at startup. Staging and production refuse to boot with
`debug` enabled or a wildcard CORS origin.

## Errors

Failures use the OpenAI error envelope, always, including for 404s and validation
errors that never reach a provider:

```json
{
  "error": {
    "message": "openai did not respond",
    "type": "upstream_error",
    "param": null,
    "code": "upstream_timeout",
    "request_id": "req_9f2c1e4b8a7d4c3e9b0a1f2e3d4c5b6a"
  }
}
```

Every response carries `X-Request-ID` and `X-Process-Time`. Supply your own
`X-Request-ID` and the gateway will propagate it, so a trace spans the boundary
between your service and this one.

## Development

```bash
pytest                          # full suite
pytest -m integration           # ASGI-level tests only
pytest -m compat                # drives the gateway with the real OpenAI SDK
pytest -m property              # determinism, idempotency, output validity
ruff check . && ruff format --check .
mypy gateway
```

Benchmarks and the JavaScript compatibility suite need a running upstream:

```bash
python -m benchmarks.run                 # token reduction per content type
python -m benchmarks.documents           # PDF/DOCX/CSV/XML extraction
python -m benchmarks.cache               # cold vs warm transformation, CPU saved
python -m benchmarks.large_payload       # 1/5/10 MB: latency, memory, degradation

uvicorn benchmarks.upstream:app --port 8124 --no-access-log
ZIBBO_OPENAI_BASE_URL=http://127.0.0.1:8124/v1 uvicorn gateway.main:app --port 8123
python -m benchmarks.overhead --requests 600 --concurrency 1   # added latency
cd compat/openai-js && npm install && npm test                 # JS SDK
```

## Architecture

```
gateway/
  api/          HTTP surface — routers, dependencies, wire schemas
  providers/    base.py       Provider ABC: URL, auth, stream detection
                proxy.py      Transport: buffered + streaming relay
                headers.py    Hop-by-hop denylist
                openai.py     OpenAI (~10 lines of real logic)
                registry.py   Name -> provider lookup
  optimizers/   pipeline.py   The one call the gateway makes
                policy.py     May this request be transformed?
                detector.py   What is this content, really?
                extraction.py Where is the text in this schema?
                registry.py   Which transformer handles it?
                base.py       Transformer ABC
                models.py     Report, result, content types
                transformers/ html.py, json.py, text.py
  cache/        backend.py    Byte-store interface (memory, Redis, …)
                memory.py     Bounded thread-safe LRU
                redis.py      Shared store, degrades to a miss when down
                service.py    Keys, (de)serialization, stats, safety
  tokenizers/   tiktoken with an offline heuristic fallback
  middleware/   Request context, timing, access log
  analytics/    Token and cost accounting            (next)
  storage/      Postgres repositories                (next)
  utils/        Shared helpers
  config.py     Typed settings from the environment
  errors.py     Exception hierarchy and the wire error format
  health.py     Pluggable health-check registry
  main.py       Application factory
benchmarks/     Reproducible, offline optimization benchmarks
```

**Providers.** `Provider` translates (where a path lives upstream, how to
authenticate, whether a request wants a stream). `ProxyService` transports (sockets,
streaming, error mapping). Adding Anthropic, Gemini, Groq, Mistral or Ollama means
writing one `Provider` subclass — every provider inherits streaming and header
preservation.

**Optimizers.** The gateway never imports a transformer. It calls
`pipeline.transform(request)`; the registry selects a transformer by what the content
*is*. Adding PDF, DOCX, CSV, XML, PII masking or deduplication means adding one module
and one registration line. See [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md).

**Plugins.** A transformer can also live in its own package. `pip install
zibbo-transformer-csv` and the gateway discovers it through a Python entry point;
no gateway code changes. A broken plugin is recorded and skipped, never fatal.

```bash
pip install -e examples/zibbo-transformer-csv
curl localhost:8000/internal/plugins
```

See [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md).
