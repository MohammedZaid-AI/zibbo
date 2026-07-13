# Zibbo for Claude Code

The companion plugin for [Zibbo](https://github.com/MohammedZaid-AI/zibbo), the
deterministic token-optimization gateway. **The plugin is UX only** — every byte of
optimization happens in the gateway. This package adds a `/zibbo` command and a
session-start banner; it shells out to the `zibbo` CLI and never re-implements gateway
logic.

## Two things have to be true

Enabling the plugin is step one of two. For Zibbo to actually save you tokens:

1. **The `zibbo` CLI must be reachable** from the shell Claude Code runs hooks in — so the
   plugin can start the gateway and read its stats.
2. **This Claude Code session must be routed through the gateway** — because a plugin
   *cannot* rewrite the running session's traffic. That is a Claude Code limitation, not a
   bug. See [Route Claude Code through Zibbo](#route-claude-code-through-zibbo).

If only (1) is true, `/zibbo` works and the gateway runs, but nothing gets optimized. The
session-start banner tells you which of the two is missing.

## Setup — the whole thing

```bash
# 1. Install the gateway so its CLI is on your PATH (pipx keeps it global + isolated)
pipx install git+https://github.com/MohammedZaid-AI/zibbo

# 2. Install the plugin, inside Claude Code
#    /plugin marketplace add MohammedZaid-AI/zibbo
#    /plugin install zibbo@zibbo

# 3. Route Claude Code through the gateway, then launch and log in normally
export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic
claude
```

That's it. **Log in to Claude the way you always do** — Claude Pro, Max, Team, Enterprise,
or a Console API key. Zibbo forwards whatever authentication Claude Code selected and never
touches it.

> **Already on a Claude subscription? You do not need an API key.** Zibbo never asks you to
> create one. If you log in with Claude Pro/Max/Team, that login keeps working through the
> gateway unchanged.

> The package is not on PyPI yet, so `pip install zibbo` will **not** work — use the pipx
> command above, or `pip install -e .` inside a clone for development. The plugin also works
> if `zibbo` is not on PATH but the package is importable by your `python` — it falls back
> to `python -m gateway.cli` automatically.

Prefer to make it permanent? Put `ANTHROPIC_BASE_URL` in `~/.claude/settings.json` under
`"env"` and every session is routed automatically.

## Authentication — every mode Claude Code supports

Authentication belongs entirely to Claude Code. Zibbo only observes *which* mode is active
(never the secret) so it can show you a green check. Supported and detected:

| Your login | What Zibbo shows |
|---|---|
| Claude Pro / Max / Team / Enterprise (OAuth login) | `Claude subscription (OAuth login)` |
| `CLAUDE_CODE_OAUTH_TOKEN` | `Claude OAuth token` |
| `ANTHROPIC_API_KEY` (Console) | `Claude API key` |
| `ANTHROPIC_AUTH_TOKEN` | `Claude auth token` |
| `apiKeyHelper` in settings.json | `Claude API key (via apiKeyHelper)` |
| Amazon Bedrock / Google Vertex / Microsoft Foundry | `Amazon Bedrock` / `Google Vertex AI` / `Microsoft Foundry` |

Zibbo classifies auth by environment-variable name and non-secret shape only. It never
reads, stores, or logs a credential value. (Subscription tier — Pro vs Max vs Team — is not
exposed to the environment, so all subscription logins show the same label.)

## What you see at startup

Every session begins with the activation banner:

```
🟢 Zibbo Active

  ✓ Gateway        Running (0.1.0)
  ✓ Authentication Claude subscription (OAuth login)
  ✓ Routing        Through Zibbo
  ✓ Optimization   Enabled
  ✓ Cache          Ready (memory)

  Type  /zibbo  for details.
```

If routing is the only thing missing, the banner tells you exactly how to fix it — and
never mentions API keys:

```
🟡 Zibbo - one step left

  ✓ Gateway        Running (0.1.0)
  ✓ Authentication Claude subscription (OAuth login)
  ✗ Routing        Not routed
  ...

  Claude Code is authenticated correctly.
  The only missing step is routing. Run:

    export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic

  Then restart Claude Code.
```

## Commands

| Command | What it shows |
|---|---|
| `/zibbo` or `/zibbo status` | The activation dashboard: gateway, provider, authentication, routing, requests today, average reduction, cache hit rate, estimated savings, overall status |
| `/zibbo doctor` | Diagnostics — gateway, auth, routing, port, plugin, env vars, version, connectivity, cache, transformers, provider — each failed check with problem, reason, exact fix |
| `/zibbo stats` | Requests, cache hit rate, tokens saved, top transformer, latency |
| `/zibbo explain` | Why the last request's tokens were reduced |
| `/zibbo benchmark` | Replay the last request's metadata through the pipeline (no upstream call) |
| `/zibbo logs` | Recent optimization activity (metadata only — never prompt contents) |
| `/zibbo enable` / `/zibbo disable` | Turn transformations on/off at runtime |
| `/zibbo start` | Start the gateway if it is not already running |

## Lifecycle — what happens, and when

| Event | What the plugin does |
|---|---|
| **Plugin enabled** | Registers the `/zibbo` command and the `SessionStart` hook. No code runs yet. |
| **Session start / resume** | The hook runs [`scripts/session-start.sh`](scripts/session-start.sh): resolves the CLI, runs `zibbo start` (instant if already running), then prints the activation banner (gateway, auth, routing, optimization, cache) via `zibbo banner`. |
| **You run `/zibbo …`** | [`scripts/zibbo.sh`](scripts/zibbo.sh) resolves the CLI and runs the subcommand against the gateway's local HTTP API. |
| **Gateway unreachable** | Commands print `no gateway at …` and suggest `/zibbo start`. |
| **CLI not found** | Every surface prints the pipx install hint instead of failing silently. |

Claude Code does **not** provide session-end, tool-invocation, or per-request hooks that a
UX plugin like this could use to optimize traffic — optimization is the gateway's job, and
routing is set at launch (above). The plugin deliberately uses only the `SessionStart` hook
and slash commands, both officially supported.

## Debugging

Set `ZIBBO_DEBUG=1` before launching Claude Code (e.g. in `~/.claude/settings.json` `"env"`).
Then the hook and CLI log each step to stderr — CLI resolution, gateway discovery, every
HTTP request, and where initialization stops:

```
[zibbo:debug] SessionStart hook firing (source=…/plugins/claude-code)
[zibbo:debug] resolved CLI: python -m gateway.cli
[zibbo:debug] discovery: probing http://127.0.0.1:8000
[zibbo:debug] GET http://127.0.0.1:8000/internal/version -> 200
```

If nothing seems to happen after enabling the plugin, run `/zibbo doctor` — it reports
exactly which check failed and how to fix it.

## Discovery

The CLI finds the gateway at `$ZIBBO_GATEWAY_URL`, then by probing `127.0.0.1:8000`,
`:8080`, `:8123`. Set `ZIBBO_GATEWAY_URL` to point at a custom port or host.
