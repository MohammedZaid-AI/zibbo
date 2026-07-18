# Changelog

All notable changes to Zibbo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Zibbo adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

First public release. A deterministic context optimization engine for AI coding assistants.

### Added

- **Optimization pipeline** — content-routed, provider-agnostic. Detects a request's content
  by inspecting the body (not the declared type), transforms it, verifies the never-grow
  guarantee, caches the result, and forwards it.
- **Transformers** — HTML → Markdown (scripts, styles, nav, ads stripped), JSON minification,
  whitespace normalization, and PDF/DOCX text extraction from base64 attachments.
- **Optional prompt de-duplication** — removes exact-duplicate instruction blocks, repeated
  sections, and duplicate bullets from long prompts. Off by default; toggle with
  `zibbo enable prompt`.
- **Providers** — OpenAI- and Anthropic-compatible endpoints; credentials forwarded untouched
  on your own account. Groq, Mistral, and Ollama supported as OpenAI-compatible.
- **`zibbo` CLI** — `start` / `stop` (starts the gateway and routes Claude Code through it,
  then restores it), `status`, `stats`, `explain`, `doctor`, `benchmark`, `enable`/`disable`.
- **Editor plugins** — Claude Code and Codex plugins that surface status, stats, and controls.
- **Transformation cache** — in-memory or Redis; deterministic outputs only. Degrades to
  "always miss" when the backend is unreachable, never to a request failure.
- **Guarantees** — deterministic, idempotent, never-grow, and content-blind logging, each
  enforced by property tests.

### Guarantees

- Source code is not reformatted or rewritten; inline whitespace is preserved.
- No LLM is used to optimize; the same input always produces the same output.
- Request contents are never stored — logs and statistics are metadata only.

[0.1.0]: https://github.com/MohammedZaid-AI/zibbo/releases/tag/v0.1.0
