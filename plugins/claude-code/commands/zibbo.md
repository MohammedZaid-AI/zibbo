---
description: Zibbo gateway — status, stats, benchmark, doctor, logs, enable/disable
argument-hint: "[status|stats|benchmark|doctor|logs|enable|disable|start]"
allowed-tools: Bash(zibbo:*), Bash(command:*)
---

## Zibbo

The output below is from the local Zibbo gateway's `zibbo` CLI. The gateway does all the
work; this command only displays it.

!`command -v zibbo >/dev/null 2>&1 && zibbo ${ARGUMENTS:-status} 2>&1 || echo "Zibbo CLI not found. Install the gateway:  pip install zibbo   then:  zibbo start"`

Present the text above to the user verbatim, in a fenced code block. Do not summarize,
re-order, or invent numbers. If it reports the gateway is not reachable, tell the user to
run `/zibbo start`. If they ask a follow-up, you may run another `zibbo …` subcommand
(`status`, `stats`, `benchmark`, `doctor`, `logs`, `enable`, `disable`, `start`).

Reminder to surface only when the gateway is **not** yet receiving this editor's traffic:
Zibbo optimizes requests only when Claude Code's model endpoint is pointed at it. Set
`ANTHROPIC_BASE_URL=http://localhost:8000/anthropic` (and an `ANTHROPIC_API_KEY`) before
launching Claude Code. The plugin cannot change the current session's environment.
