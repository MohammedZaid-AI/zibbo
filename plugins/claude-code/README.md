# Zibbo for Claude Code

The companion plugin for [Zibbo](https://github.com/MohammedZaid-AI/zibbo), the
deterministic token-optimization gateway. **The plugin is UX only** — every byte of
optimization happens in the gateway. This package adds a `/zibbo` command and a
session-start banner; it shells out to the `zibbo` CLI and never re-implements gateway
logic.

## Install

```
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo
```

Then install the gateway itself (the engine the plugin talks to):

```
pip install zibbo        # provides the `zibbo` CLI and the gateway
```

## Route Claude Code through Zibbo

The plugin cannot rewrite the current session's network traffic — that is not something
any Claude Code plugin can do. To have your requests optimized, point Claude Code's model
endpoint at the gateway **before launching it**:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic
export ANTHROPIC_API_KEY=sk-ant-...
claude
```

Or persist it in `~/.claude/settings.json` under `"env"`. See the gateway's
[PLUGIN_ARCHITECTURE.md](https://github.com/MohammedZaid-AI/zibbo/blob/main/docs/PLUGIN_ARCHITECTURE.md).

## Commands

| Command | What it shows |
|---|---|
| `/zibbo` or `/zibbo status` | Gateway status, providers, cache, uptime |
| `/zibbo stats` | Requests, cache hit rate, tokens saved, top transformer, latency |
| `/zibbo benchmark` | Replay the last request's metadata through the pipeline (no upstream call) |
| `/zibbo doctor` | Diagnostics with actionable fixes |
| `/zibbo logs` | Recent optimization activity (metadata only — never prompt contents) |
| `/zibbo enable` / `/zibbo disable` | Turn transformations on/off at runtime |
| `/zibbo start` | Start the gateway if it is not already running |

## What happens on session start

A `SessionStart` hook runs `zibbo start`: it starts the gateway if it is not already
running (idempotent — instant when it is) and prints the status banner. If the `zibbo`
CLI is not installed, it prints a one-line install hint instead of failing.

## Discovery

The CLI finds the gateway at `$ZIBBO_GATEWAY_URL`, then by probing `127.0.0.1:8000`,
`:8080`, `:8123`. Set `ZIBBO_GATEWAY_URL` to point at a custom port or host.
