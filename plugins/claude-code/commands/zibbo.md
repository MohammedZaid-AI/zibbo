---
description: Zibbo gateway — status, stats, benchmark, doctor, logs, enable/disable
argument-hint: "[status|stats|benchmark|doctor|logs|enable|disable|start]"
allowed-tools: Bash(sh:*), Bash(zibbo:*), Bash(python:*), Bash(python3:*), Bash(command:*)
---

## Zibbo

The output below is from the local Zibbo gateway's CLI. The gateway does all the work;
this command only displays it. The helper script resolves the CLI whether it is installed
as `zibbo` on your PATH or only as an importable module in a virtualenv.

!`sh "${CLAUDE_PLUGIN_ROOT}/scripts/zibbo.sh" ${ARGUMENTS:-status}`

Present the text above to the user verbatim, in a fenced code block. Do not summarize,
re-order, or invent numbers. If it reports the gateway is not reachable, tell the user to
run `/zibbo start`. If the CLI itself was not found, point them at the install command in
the output. If they ask a follow-up, you may run another subcommand (`status`, `stats`,
`benchmark`, `doctor`, `logs`, `enable`, `disable`, `start`).

Reminder to surface only when the gateway is **not** yet receiving this editor's traffic:
Zibbo optimizes requests only when Claude Code's model endpoint is pointed at it. Set
`ANTHROPIC_BASE_URL=http://localhost:8000/anthropic` (and an `ANTHROPIC_API_KEY`) **before**
launching Claude Code — a plugin cannot change the current session's environment.
