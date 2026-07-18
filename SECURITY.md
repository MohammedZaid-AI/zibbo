# Security Policy

## Supported versions

Zibbo is pre-1.0. Security fixes are applied to the latest `0.x` release only.

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue.

- Use GitHub's [private vulnerability reporting](https://github.com/MohammedZaid-AI/zibbo/security/advisories/new), or
- email the maintainer at the address on the GitHub profile.

Include a description, reproduction steps, affected version, and impact. You can expect an
acknowledgement within a few days. Please give a reasonable window to release a fix before
any public disclosure.

## Security model

Zibbo is a **local proxy** that sits between an AI coding assistant and the model provider.
A few properties are load-bearing for its security posture:

- **Credentials pass through untouched.** Zibbo forwards your `Authorization` / `x-api-key`
  header to the provider as received. It never stores, logs, or inspects the credential
  value — only the *kind* of authentication (for status display).
- **Request contents are never persisted.** Logs, statistics, and benchmark results contain
  only metadata — sizes, token counts, timings, transformer names. No request body ever
  enters a log line. This is enforced by tests.
- **The control API is loopback-only by default.** The `/internal/*` endpoints (used by the
  editor plugins) reject non-loopback clients unless `ZIBBO_INTERNAL_API_ALLOW_REMOTE=true`
  is set *and* a bearer token is configured.
- **No code execution from request content.** Zibbo parses and rewrites request bodies; it
  never executes them. Plugin loading executes local Python only from paths the operator
  explicitly configures.

## Out of scope

- The security of the upstream provider you forward to.
- Plugins or `plugins_dir` code you choose to load (that is local code you control).
- Exposing the gateway or its `/internal/*` API to an untrusted network (don't).
