<div align="center">

<img src="docs/assets/avatar.png" alt="Zibbo" width="140" height="140" />

#Zibbo

**Cut the token cost of your AI coding assistant — losslessly, on your own machine.**

[![CI](https://github.com/MohammedZaid-AI/zibbo/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammedZaid-AI/zibbo/actions/workflows/ci.yml)
[![no LLM in the loop](https://img.shields.io/badge/LLM_in_the_loop-none-blue)](#trust)
[![deterministic](https://img.shields.io/badge/transformations-deterministic-black)](#trust)
[![license](https://img.shields.io/badge/license-Apache_2.0-black)](LICENSE)

</div>

---

## What is Zibbo?

AI coding assistants send whole HTML pages, pretty-printed JSON, stack traces,
and PDFs to the model. Most of that is scripts, nav bars, indentation, and
base64 — tokens you pay for that carry no information.

Zibbo is a local proxy that strips this structural noise from each request
before forwarding it to your provider. HTML becomes Markdown, JSON is minified,
documents are extracted to text. The meaning is preserved; the token count drops.

## Why Zibbo?

- **Runs locally.** Traffic is optimized on your machine and forwarded on your
  own API key — requests bill your account, nothing is proxied through a third party.
- **Lossless by design.** Structural noise is removed; content is never
  summarized, paraphrased, or reordered.
- **No LLM in the loop.** The optimization path uses deterministic rules, not a
  second model — so there's no extra latency, cost, or nondeterminism.
- **Drop-in.** Point your assistant's base URL at Zibbo and keep working. It
  supports Claude Code and any OpenAI-compatible client.

## How it works

```
   ┌────────────┐      ┌─────────────────────────────┐      ┌───────────┐
   │ Claude Code│      │            Zibbo             │      │  Provider │
   │  / Codex   │ ───► │  detect → transform → verify │ ───► │ Anthropic │
   │  / any SDK │      │  (HTML, JSON, logs, docs)    │      │  / OpenAI │
   └────────────┘      └─────────────────────────────┘      └───────────┘
```

Zibbo is a transparent proxy: it optimizes the request body, forwards it on your
credentials, and streams the provider's response straight back untouched. If a
request wouldn't get smaller, the original bytes are forwarded unchanged.

## Features

- 🔒 &nbsp;Runs locally on your own API key
- 🧮 &nbsp;Deterministic — the same input always produces the same output
- 🚫 &nbsp;No second model in the optimization path
- 🔁 &nbsp;Transparent proxy — responses stream back unmodified
- 📄 &nbsp;HTML → Markdown, JSON minified, PDFs and DOCX extracted
- 🧩 &nbsp;Works with Claude Code and OpenAI-compatible clients
- 🛡️ &nbsp;Never rewrites source code and never enlarges a request
- ⚡ &nbsp;One command to connect, one to disconnect

## Installation

**Claude Code plugin**

```text
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo
```

**Standalone CLI** (any assistant)

```bash
pipx install git+https://github.com/MohammedZaid-AI/zibbo
```

## Connect

`zibbo connect` starts the local gateway and routes Claude Code through it — no
settings file to edit by hand.

```bash
zibbo connect
```

```text
✓ Gateway running (http://127.0.0.1:8000)
✓ Claude Code configured  (~/.claude/settings.json)

  Restart Claude Code for routing to take effect.
```

It writes `ANTHROPIC_BASE_URL` into your **global** Claude Code settings, which
both the CLI and the VS Code extension read regardless of the open folder. Use
`--project` to scope routing to the current workspace, and `zibbo disconnect` to
undo it.

For any other assistant, set its base URL to Zibbo — `…/v1` for OpenAI-style
clients, `…/anthropic` for Anthropic-style. Your API key is forwarded untouched.

> **Not routed after connecting?** Restart your assistant (settings are read at
> launch), then check `zibbo status`. Still off? Run `zibbo doctor --fix`.

## Supported Content

| Content | What Zibbo does |
|---|---|
| **HTML** pages & docs | strips scripts, styles, nav, and ads; converts to Markdown |
| **JSON** responses | removes pretty-print whitespace and escapes |
| **PDF / DOCX** attachments | extracts embedded text from the base64 payload |
| **CSV / XML** | converts to compact Markdown or text |
| **Logs & prose** | trims trailing whitespace and redundant blank lines |
| **Source code** | left untouched — never reformatted or reindented |

## Inspect Optimizations

Every response reports what Zibbo did, in headers:

```text
x-zibbo-optimization: applied
x-zibbo-tokens-saved: 709
```

`zibbo explain` breaks down the last request:

```text
$ zibbo explain

Last request

  Content type:      HTML
  Original tokens:   939
  Optimized tokens:  230

  Transformations applied:
    ✓ Removed scripts
    ✓ Removed navigation
    ✓ Converted HTML to Markdown
```

### Optional: prompt de-duplication

Long, hand-written prompts often repeat themselves — an instruction pasted
twice, a `Requirements:` section copied and edited. Zibbo can remove that exact
redundancy without paraphrasing, reordering, or touching code. Off by default:

```bash
zibbo enable prompt      # live, no restart
zibbo disable prompt
```

Details: [docs/PROMPT_OPTIMIZATION.md](docs/PROMPT_OPTIMIZATION.md).

## Commands

```bash
zibbo connect     # start the gateway and route your assistant through it
zibbo disconnect  # stop routing (the gateway keeps running)
zibbo status      # what is running, what is routed, current endpoint
zibbo stats       # tokens saved, today and all-time
zibbo explain     # what the last request had removed, and why
zibbo doctor      # diagnose setup problems; each check names its fix
zibbo stop        # stop the gateway and restore your assistant's endpoint
```

Inside Claude Code the same views are `/zibbo:zibbo`, `/zibbo:stats`, and
`/zibbo:doctor`.

## Trust

Four guarantees, each covered by property tests in the suite:

- **Deterministic** — no model, no randomness. Output is a pure function of input.
- **Idempotent** — running the pipeline twice changes nothing.
- **Never-grow** — a transformation that produces more tokens is discarded and
  the original forwarded.
- **Content-blind logging** — no request body enters a log line or a statistic.

## Architecture

A request is classified by its content (not its declared type), routed to the
transformer that handles it, transformed, checked against the never-grow
guarantee, cached, and forwarded. Providers, transformers, caching, and plugins
are pluggable. Full internals: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

[Apache-2.0](LICENSE).
