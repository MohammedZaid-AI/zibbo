<div align="center">
<img src="../../docs/assets/avatar.png" alt="Zibbo" width="120" height="120" />
</div>

# Zibbo for Claude Code

The companion plugin for [Zibbo](https://github.com/MohammedZaid-AI/zibbo), the
deterministic token-optimization gateway. **The plugin is UX only** — every byte of
optimization happens in the gateway. This package adds `/zibbo:*` commands and a
session-start banner; it shells out to the `zibbo` CLI and never re-implements gateway
logic.

## Two things have to be true

Enabling the plugin is step one of two. For Zibbo to actually save you tokens:

1. **The `zibbo` CLI must be reachable** from the shell Claude Code runs hooks in — so the
   plugin can start the gateway and read its stats.
2. **This Claude Code session must be routed through the gateway** — because a plugin
   *cannot* rewrite the running session's traffic. That is a Claude Code limitation, not a
   bug. See [Route Claude Code through Zibbo](#route-claude-code-through-zibbo).

If only (1) is true, `/zibbo:zibbo` works and the gateway runs, but nothing gets optimized. The
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
> command above, or `pip install -e .` inside a clone for development. The hook and command
> call `zibbo` directly (no shell), so it **must be on your `PATH`** — pipx guarantees that.
> A `zibbo` living only inside a project virtualenv is invisible unless that venv is active.

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

  ✓ Gateway        Running (0.1.1)
  ✓ Authentication Claude subscription (OAuth login)
  ✓ Routing        Through Zibbo
  ✓ Optimization   Enabled
  ✓ Cache          Ready (memory)

  Try  /zibbo:zibbo  (dashboard)  ·  /zibbo:stats  ·  /zibbo:doctor
```

If routing is the only thing missing, the banner tells you exactly how to fix it — and
never mentions API keys:

```
🟡 Zibbo - one step left

  ✓ Gateway        Running (0.1.1)
  ✓ Authentication Claude subscription (OAuth login)
  ✗ Routing        Not routed
  ...

  Claude Code is authenticated correctly.
  The only missing step is routing. Run:

    export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic

  Then restart Claude Code.
```

## Commands

Claude Code namespaces every plugin command as `/<plugin>:<command>`, so all Zibbo commands
begin with `/zibbo:`. Type `/zibbo` and the autocomplete lists them all — nothing to memorize.

| Command | What it shows |
|---|---|
| `/zibbo:zibbo` | The activation dashboard: gateway, provider, authentication, routing, requests today, average reduction, cache hit rate, estimated savings, overall status |
| `/zibbo:doctor` | Diagnostics — gateway, auth, routing, port, plugin, env vars, version, connectivity, cache, transformers, provider — each failed check with problem, reason, exact fix |
| `/zibbo:stats` | Requests, cache hit rate, tokens saved, top transformer, latency |
| `/zibbo:explain` | Why the last request's tokens were reduced |
| `/zibbo:benchmark` | Replay the last request's metadata through the pipeline (no upstream call) |
| `/zibbo:logs` | Recent optimization activity (metadata only — never prompt contents) |
| `/zibbo:claude` | Claude Code activation status from the gateway's point of view |
| `/zibbo:enable` / `/zibbo:disable` | Turn transformations on/off at runtime |
| `/zibbo:connect` | Start the gateway and route Claude Code through it — recommended onboarding (global by default; `--project` for this workspace only) |
| `/zibbo:disconnect` | Stop routing Claude Code through Zibbo (the gateway keeps running) |
| `/zibbo:start` | Start the gateway and route Claude Code through it |
| `/zibbo:stop` | Stop the gateway and restore your assistant's endpoint |

The dashboard command also accepts an argument, so `/zibbo:zibbo stats` is equivalent to
`/zibbo:stats`.

## Lifecycle — what happens, and when

| Event | What the plugin does |
|---|---|
| **Plugin enabled** | Registers the `/zibbo:*` commands and the `SessionStart` hook. No code runs yet. |
| **Session start / resume** | The hook runs `zibbo banner --start` in [exec form](#why-no-shell-scripts): it starts the gateway (instant if already running), then prints the activation banner — gateway, auth, routing, optimization, cache. |
| **You run `/zibbo:stats` (etc.)** | The command runs `` !`zibbo stats $ARGUMENTS` `` against the gateway's local HTTP API. `/zibbo:zibbo` shows the dashboard. |
| **Gateway unreachable** | The banner/command says the gateway is not running and how to start it. |
| **`zibbo` not found** | Install it on PATH with pipx (below). |

Claude Code does **not** provide session-end, tool-invocation, or per-request hooks that a
UX plugin like this could use to optimize traffic — optimization is the gateway's job, and
routing is set at launch (above). The plugin deliberately uses only the `SessionStart` hook
and one slash command, both officially supported.

### Why no shell scripts

The hook and command invoke the `zibbo` executable **directly**, never through a shell:

- The **hook** uses hooks [exec form](https://code.claude.com/docs/en/hooks) —
  `{"type":"command","command":"zibbo","args":["banner","--start"]}`. Claude Code spawns
  the executable with that argument vector and **no shell**, so there is no variable
  expansion, no `&&`/`||`, and no bash dependency on any platform.
- The **command** uses the documented `$ARGUMENTS` substitution and a matching
  `allowed-tools: Bash(zibbo *)` rule, so the injected command is a literal `zibbo …` with
  nothing for the permission checker to reject.

Earlier versions shelled out (`sh "${CLAUDE_PLUGIN_ROOT}/…" ${ARGUMENTS:-status}`). Claude
Code's permission checker rejects a `` !`command` `` that contains shell expansion, and the
`sh` wrapper needed Git Bash. Both are gone — so `zibbo` must simply be on your `PATH`,
which is exactly what pipx guarantees.

## Debugging

Set `ZIBBO_DEBUG=1` before launching Claude Code (e.g. in `~/.claude/settings.json` `"env"`).
Then the CLI logs each step to stderr — the command dispatched, gateway discovery, every
HTTP request, and where things stop:

```
[zibbo:debug] command: banner
[zibbo:debug] discovery: probing http://127.0.0.1:8000
[zibbo:debug] GET http://127.0.0.1:8000/internal/version -> 200
```

If nothing seems to happen after enabling the plugin, run `/zibbo:doctor` — it reports
exactly which check failed and how to fix it.

## Discovery

The CLI finds the gateway at `$ZIBBO_GATEWAY_URL`, then by probing `127.0.0.1:8000`,
`:8080`, `:8123`. Set `ZIBBO_GATEWAY_URL` to point at a custom port or host.

## How the plugin finds the CLI

The plugin does **not** assume a global `zibbo`. It ships a resolver shim at
[`bin/zibbo`](bin/zibbo); Claude Code adds a plugin's `bin/` to the Bash tool's PATH while
the plugin is enabled, so `` !`zibbo $ARGUMENTS` `` (the command) and exec-form `zibbo` (the
hook) both reach the shim. One shared helper, so command and hook behave identically. The
shim resolves in order:

1. a real `zibbo` on PATH (e.g. a pipx install)
2. `py -m gateway.cli` (Windows, if it can import `gateway`)
3. `python -m gateway.cli`
4. `python3 -m gateway.cli`
5. the repository checkout the shim ships in (`PYTHONPATH=<repo>`)
6. otherwise, print install instructions

So a `pipx install` works, and so does a plain checkout where `python` can import `gateway`
— no global `zibbo` required. The shim is an ordinary script, so its logic never touches the
slash-command permission checker; the command stays `` !`zibbo $ARGUMENTS` `` and the hook
stays exec form.

## Developing the Claude Code plugin

Claude Code does **not** run this repository directly. It installs a **copy**, and the
copy is keyed by the plugin's `version`:

```
  Repository                you edit here
  plugins/claude-code/
        │  git push
        ▼
  Marketplace (GitHub)      MohammedZaid-AI/zibbo
        │  /plugin marketplace update
        ▼
  Marketplace clone         ~/.claude/plugins/marketplaces/zibbo/
        │  /plugin install / update  (only if version changed)
        ▼
  Version cache             ~/.claude/plugins/cache/zibbo/zibbo/<version>/
        │
        ▼
  Running plugin            ← what actually executes
```

Per the [Claude Code docs](https://code.claude.com/docs/en/plugins-reference#version-management):
*"users only receive updates when you bump this field … Pushing new commits without bumping
it has no effect, and `/plugin update` reports 'already at the latest version'."* So if you
edit files without bumping `version`, the running plugin stays stale.

### While iterating — load the repo directly (no cache)

The supported dev workflow bypasses the marketplace and cache entirely:

```bash
claude --plugin-dir "/path/to/zibbo/plugins/claude-code"
```

This overrides the installed marketplace plugin for that session. After editing hooks or
commands, run `/reload-plugins`. Print the exact command for your checkout with:

```bash
zibbo plugin dev
```

### Checking and refreshing an installed copy

```bash
zibbo plugin status   # compare installed vs repo; list any stale executed files
zibbo plugin verify   # fail loudly if the repo files contain shell expansion
zibbo plugin sync     # copy repo commands/, hooks/, README into the installed cache
```

`zibbo plugin sync` refreshes the **content** of the already-installed cache in place, so
you can test a fix without a full reinstall. It does not change the cache's version label.

### Cutting a release (so real users get the update)

1. Bump `version` in **both** `plugins/claude-code/.claude-plugin/plugin.json` and the
   marketplace entry in `.claude-plugin/marketplace.json` (keep them equal).
2. `git push`.
3. Users run `/plugin marketplace update zibbo` then `/plugin update zibbo@zibbo`.

Because the cache is version-keyed, the bump is what makes `/plugin update` fetch the new
files instead of reporting "already at the latest version."
