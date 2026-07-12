# Plugin architecture

Zibbo is two products with a clean seam between them.

* **The gateway** is the engine. It proxies providers, optimizes requests, extracts
  documents, caches transformations, and keeps in-memory analytics. It exposes a small
  local control API under `/internal/*`.
* **The plugins** are the UX. One for Claude Code, one for Codex. They surface status,
  stats, and controls *inside the editor*, and they start the gateway and guide setup.
  They contain **no optimization logic** — none, by design. Every number a plugin shows
  comes from the gateway.

Between them sits the `zibbo` CLI: a stdlib-only terminal client that both plugins shell
out to. One brain, one CLI, two thin skins.

```
Claude Code  ─┐                        ┌── /internal/status
Codex        ─┤── zibbo CLI ── HTTP ───┤── /internal/stats
(your shell) ─┘   (stdlib)             ├── /internal/benchmark
                                       ├── /internal/enable · /disable
                                       └── /internal/doctor · /logs · /version
                                              │
                                       Zibbo gateway (the engine)
```

## The one hard constraint

**A plugin cannot sit on the network path between the editor and the model API.** Claude
Code and Codex plugins are commands, skills, hooks, and MCP servers running in the agent
layer — not a network proxy. So a plugin cannot transparently optimize the editor's own
traffic.

The only supported way to route an editor through Zibbo is to point its model endpoint at
the gateway:

* **Claude Code** — `ANTHROPIC_BASE_URL=http://localhost:8000/anthropic` (plus an
  `ANTHROPIC_API_KEY`), set before launch or in `~/.claude/settings.json` `env`.
* **Codex** — a `model_providers` entry in `~/.codex/config.toml` pointing at
  `http://localhost:8000/v1`.

The plugins **automate what the host API allows** (starting the gateway, showing status,
running controls) and **guide the user** for the base-URL step, which no plugin API can do
for the current session. This is a deliberate choice over unsupported interception.

## Plugin lifecycle

**Claude Code** — on `SessionStart`, a hook runs `zibbo start`: it starts the gateway if
it is not already running (idempotent, instant when it is) and prints the status banner.
If the `zibbo` CLI is absent, it prints an install hint instead of failing. Thereafter the
`/zibbo` command dispatches to the CLI.

**Codex** — the `zibbo` skill loads at session start (per Codex's plugin model). It has no
long-running lifecycle; it runs a `zibbo` command when the user asks about Zibbo.

## Gateway discovery

The CLI never assumes a URL. It resolves, in order:

1. An explicit `--url`.
2. `$ZIBBO_GATEWAY_URL`.
3. A probe of `127.0.0.1:8000`, `:8080`, `:8123` — the first that answers
   `/internal/version` wins.

This supports custom ports, custom hosts, and multiple gateway instances (point
`ZIBBO_GATEWAY_URL` at the one you want).

## The command system

The whole plugin surface is one dispatch command plus one hook (Claude Code) or one skill
(Codex). Both call `zibbo <subcommand>` and display the output verbatim; the model is not
asked to compute or summarize numbers.

| Surface | Invocation | Runs |
|---|---|---|
| Claude Code | `/zibbo [subcommand]` | `zibbo <subcommand>` (default `status`) |
| Claude Code | SessionStart hook | `zibbo start` |
| Codex | "what's my Zibbo status?" etc. | the matching `zibbo` command |
| Any shell | `zibbo <subcommand>` | direct |

Subcommands: `status`, `stats`, `benchmark`, `doctor`, `logs`, `enable`, `disable`,
`version`, `start`.

## Public internal API

Consumed only by the CLI/plugins. Every response is deployment metadata or an aggregate
count — **no endpoint returns request content**. Bound to loopback by default (see
Security).

| Method | Path | Purpose |
|---|---|---|
| GET | `/internal/status` | Name, version, uptime, optimization/cache/documents flags, providers, transformers |
| GET | `/internal/stats` | Today and all-time: requests, tokens saved, reduction %, cache hit rate, top transformer, avg latency, estimated cost |
| GET | `/internal/version` | Gateway version + internal API version |
| GET | `/internal/cache` | Transformation-cache counters |
| GET | `/internal/plugins` | Discovered gateway transformer plugins |
| GET | `/internal/logs?limit=N` | Recent optimization events (metadata only) |
| POST | `/internal/benchmark` | Replay `{content?, model?}` through the pipeline; no upstream call. Omit `content` to replay the last request's stored metadata |
| POST | `/internal/enable` | Turn optimization on (runtime) |
| POST | `/internal/disable` | Turn optimization off (runtime) |
| POST | `/internal/doctor` | Diagnostics with actionable fixes |

The internal API version is `1`, reported by `/internal/version`. The CLI tolerates
additive changes; a breaking change bumps this number.

## Security

The internal API controls the deployment, so it is **loopback-only by default**. A request
from any non-loopback address gets a `404` — not a `403` — so the endpoints do not even
advertise their existence off-box.

Remote access is opt-in and authenticated:

* `ZIBBO_INTERNAL_API_ALLOW_REMOTE=true` opens the API to non-loopback clients.
* `ZIBBO_INTERNAL_API_TOKEN=…` is then **required**; requests must send
  `Authorization: Bearer <token>` (or `X-Zibbo-Token`). Missing/invalid → `401`. Enabling
  remote access without a token is a misconfiguration and returns `503`.

The proxy routes (`/v1/*`, `/anthropic/*`) are unaffected by this guard.

## Analytics

The stats the plugin shows come from an **in-memory** analytics engine: it folds each
request's `TransformationReport` into today and all-time aggregates. It holds only
metadata — counts, sizes, durations, transformer names — never a byte of a prompt. It
resets on restart. **Persisted analytics is separate and not built here**; this engine is
what makes the plugin useful without it.

## Folder structure

```
zibbo/                                  (the repository / marketplace)
  .claude-plugin/marketplace.json       Claude Code marketplace manifest
  .codex-plugin/marketplace.json        Codex marketplace manifest
  plugins/
    claude-code/
      .claude-plugin/plugin.json        plugin manifest
      commands/zibbo.md                 the /zibbo dispatch command
      hooks/hooks.json                  SessionStart -> zibbo start
      README.md
    codex/
      .codex-plugin/plugin.json         plugin manifest
      skills/zibbo/SKILL.md             the zibbo skill
      README.md
  gateway/
    cli.py                              the `zibbo` CLI (stdlib only)
    analytics/                          in-memory analytics engine
    runtime.py                          the live optimization kill switch
    api/routes/internal.py              the /internal/* API
```

## Installation

**Claude Code**

```
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo
pip install zibbo          # the gateway + `zibbo` CLI the plugin drives
```

**Codex**

```
codex plugin marketplace add MohammedZaid-AI/zibbo
codex plugin add zibbo@zibbo
pip install zibbo
```

(The `zibbo/zibbo` shorthand in the original vision resolves to whatever org hosts the
repo; today that is `MohammedZaid-AI/zibbo`.)

## Future integrations

The same shape — a thin skin over the `zibbo` CLI over the internal API — extends to the
other targets named in the vision: OpenCode, Gemini CLI, Aider, Continue, Cline, Roo Code.
Each needs only its host's command/skill/hook manifest; none needs new gateway logic. A
host without a plugin API still gets the `zibbo` CLI and the manual base-URL setup.
