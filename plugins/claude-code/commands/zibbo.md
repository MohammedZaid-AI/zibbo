---
description: Zibbo — activation dashboard (also /zibbo:stats, /zibbo:doctor, /zibbo:explain …)
argument-hint: "[status|stats|doctor|explain|benchmark|logs|enable|disable|start]"
allowed-tools: Bash(zibbo), Bash(zibbo *)
---

## Zibbo

The output below is from the local Zibbo gateway's CLI. The gateway does all the work;
this command only displays it. With no argument it shows the activation dashboard.

!`zibbo $ARGUMENTS`

Present the output above verbatim, in a fenced code block. Do not summarize, re-order, or
invent numbers. If it reports the gateway is not reachable, run `/zibbo:start`. If the
`zibbo` command was not found, tell the user to install it so Claude Code can see it:
`pipx install git+https://github.com/MohammedZaid-AI/zibbo`.

There are also per-action commands, so the user need not remember argument syntax:
`/zibbo:stats`, `/zibbo:explain`, `/zibbo:doctor`, `/zibbo:benchmark`, `/zibbo:logs`,
`/zibbo:enable`, `/zibbo:disable`, `/zibbo:claude`, `/zibbo:start`.

Reminder to surface only when the gateway is **not** yet receiving this editor's traffic:
Zibbo optimizes requests only when Claude Code's model endpoint is pointed at it. Set
`ANTHROPIC_BASE_URL=http://localhost:8000/anthropic` **before** launching Claude Code — a
plugin cannot change the current session's environment. Log in to Claude the way you always
do; a Claude subscription needs no API key.
