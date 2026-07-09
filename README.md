# LLMGateway

A drop-in API gateway that sits between your application and its LLM provider.

Point your SDK's `base_url` at LLMGateway and it deterministically optimizes every
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

Phase 1 of 8 — foundations. The gateway currently serves health endpoints; the
proxy and optimizers land in Phases 2 and 3.

| Phase | Scope | State |
|---|---|---|
| 1 | App skeleton, config, logging, errors, health, Docker | done |
| 2 | OpenAI-compatible passthrough | next |
| 3 | HTML / JSON / text optimizers | |
| 4 | Token + cost analytics (Postgres) | |
| 5 | Next.js dashboard | |
| 6 | Anthropic-compatible endpoint | |
| 7 | PDF / DOCX / CSV optimizers | |
| 8 | Redis cache, streaming, benchmarks | |

## Quick start

### Docker (recommended)

```bash
docker compose up --build
```

Postgres and Redis start alongside the gateway; neither is used yet, but the
readiness endpoint will begin probing them as later phases wire them in.

### Local

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
python -m gateway               # http://localhost:8000
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Service summary: version, environment, uptime |
| `GET` | `/health/live` | Liveness. Touches no dependency, never fails while the process runs |
| `GET` | `/health/ready` | Readiness. Probes every dependency; `503` if any is unhealthy |
| `GET` | `/docs` | OpenAPI UI (disabled in production) |

Liveness and readiness are deliberately separate: an orchestrator should restart a
pod that fails liveness, but only pull a pod out of the load balancer when it fails
readiness. A gateway whose database blips should stop taking traffic, not be killed.

## Configuration

Every setting is an environment variable prefixed with `LLMGATEWAY_`, mapping onto a
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
ruff check . && ruff format --check .
mypy gateway
```

## Architecture

```
gateway/
  api/          HTTP surface — routers, dependencies, wire schemas
  providers/    Upstream clients (OpenAI, Anthropic)
  middleware/   Request context, timing, access log
  optimizers/   Deterministic content optimizers, one module per format
  analytics/    Token and cost accounting
  tokenizers/   Per-provider token counting
  storage/      Postgres repositories, Redis cache
  utils/        Shared helpers
  config.py     Typed settings from the environment
  errors.py     Exception hierarchy and the wire error format
  health.py     Pluggable health-check registry
  main.py       Application factory
```

Each optimizer is isolated behind a common protocol: supporting a new format means
adding one module, not editing the pipeline.
