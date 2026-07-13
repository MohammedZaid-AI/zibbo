---
name: zibbo
description: >-
  Inspect and control the local Zibbo token-optimization gateway. Use when the user asks
  about Zibbo status, token savings, cache hit rate, optimization stats, or wants to
  enable/disable optimization or diagnose the gateway.
---

# Zibbo gateway control

Zibbo is a local gateway that deterministically optimizes LLM requests (stripping
structural noise, extracting documents, caching transformations) to cut tokens. This
skill is UX only — it never performs optimization itself. It drives the `zibbo` CLI,
which talks to the running gateway's local HTTP API.

## How to respond

Run the appropriate command with your shell tool and show the output verbatim in a code
block. Do not invent numbers or summarize unless the user asks a follow-up.

| The user wants… | Run |
|---|---|
| Gateway status / "is Zibbo running" | `zibbo status` |
| Savings, cache hit rate, stats | `zibbo stats` |
| A pipeline benchmark of the last request | `zibbo benchmark` |
| Benchmark specific text | `zibbo benchmark --content "<text>"` |
| Diagnose problems | `zibbo doctor` |
| Recent activity (metadata only) | `zibbo logs` |
| Turn optimization on / off | `zibbo enable` / `zibbo disable` |
| Start the gateway | `zibbo start` |

If `zibbo` is not found, tell the user to install the gateway so the CLI is on their PATH:
`pipx install git+https://github.com/MohammedZaid-AI/zibbo` (the package is not on PyPI, so
plain `pip install zibbo` will not work). If the gateway is unreachable, tell them to run
`zibbo start`. As a fallback when `zibbo` is not on PATH but the package is importable,
`python -m gateway.cli <subcommand>` runs the same CLI.

## Routing Codex through Zibbo (manual, one-time)

The skill cannot rewrite Codex's network traffic. To have requests optimized, point
Codex's model provider base URL at the gateway (OpenAI-compatible route
`http://localhost:8000/v1`) in `~/.codex/config.toml`, then restart Codex. Guide the user
through this rather than attempting it silently.
