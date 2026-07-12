# Zibbo for Codex

The companion plugin for [Zibbo](https://github.com/MohammedZaid-AI/zibbo), the
deterministic token-optimization gateway. **UX only** — all optimization happens in the
gateway. This package ships one skill that drives the `zibbo` CLI.

## Install

```
codex plugin marketplace add MohammedZaid-AI/zibbo
codex plugin add zibbo@zibbo
```

Then install the gateway:

```
pip install zibbo        # provides the `zibbo` CLI and the gateway
```

Start a new Codex session so the skill loads.

## Route Codex through Zibbo

The skill cannot rewrite Codex's traffic. Point Codex's model provider base URL at the
gateway's OpenAI-compatible route in `~/.codex/config.toml`:

```toml
# Example: send an OpenAI-compatible provider through Zibbo
[model_providers.zibbo]
name = "zibbo"
base_url = "http://localhost:8000/v1"
```

Then select that provider and restart Codex. See the gateway's
[PLUGIN_ARCHITECTURE.md](https://github.com/MohammedZaid-AI/zibbo/blob/main/docs/PLUGIN_ARCHITECTURE.md).

## Use it

Ask Codex things like "what's my Zibbo status?", "how many tokens has Zibbo saved?", or
"run Zibbo diagnostics". The skill runs the matching `zibbo` command and shows the result.
