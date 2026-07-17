"""The `zibbo` CLI: pure rendering, discovery, and argument parsing.

The HTTP path is exercised live in development; here we cover the logic that does not
need a socket — the renderers (which the plugins ultimately display) and discovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway import cli

_STATUS = {
    "name": "zibbo",
    "version": "0.1.0",
    "environment": "development",
    "uptime_seconds": 125.0,
    "optimization_enabled": True,
    "pipeline_active": True,
    "cache_enabled": True,
    "cache_backend": "memory",
    "documents_enabled": True,
    "transformers": ["html", "json", "text"],
    "providers": [
        {"name": "openai", "prefix": "/v1"},
        {"name": "anthropic", "prefix": "/anthropic"},
    ],
}

_STATS = {
    "date": "2026-07-12",
    "today": {
        "requests": 10,
        "optimized": 8,
        "skipped": 2,
        "tokens_before": 1000,
        "tokens_after": 300,
        "tokens_saved": 700,
        "token_reduction_pct": 70.0,
        "bytes_saved": 2048,
        "cache_hit_rate": 0.5,
        "transformations": 8,
        "avg_latency_ms": 2.5,
        "top_transformer": {"name": "html", "count": 5, "tokens_saved": 500},
        "estimated_cost_usd": None,
    },
    "all_time": {
        "requests": 100,
        "optimized": 80,
        "skipped": 20,
        "tokens_before": 10000,
        "tokens_after": 3000,
        "tokens_saved": 7000,
        "token_reduction_pct": 70.0,
        "bytes_saved": 20480,
        "cache_hit_rate": 0.6,
        "transformations": 80,
        "avg_latency_ms": 2.2,
        "top_transformer": {"name": "html", "count": 50, "tokens_saved": 5000},
        "estimated_cost_usd": None,
    },
}


def test_render_status_lists_flags_and_providers() -> None:
    out = cli.render_status(_STATUS)
    assert "Gateway running" in out
    assert "Cache enabled (memory)" in out
    assert "Transformation pipeline active" in out
    assert "openai provider detected" in out
    assert "anthropic provider detected" in out
    assert "html, json, text" in out


def test_render_stats_shows_savings_and_top_transformer() -> None:
    out = cli.render_stats(_STATS)
    assert "700" in out  # tokens saved today
    assert "70.0%" in out
    assert "Top transformer: html" in out
    # No configured cost rate -> the hint, not a dollar figure.
    assert "ZIBBO_ANALYTICS_COST_PER_MILLION_TOKENS" in out


def test_render_stats_shows_cost_when_configured() -> None:
    stats = json.loads(json.dumps(_STATS))
    stats["today"]["estimated_cost_usd"] = 0.0021
    assert "$0.0021" in cli.render_stats(stats)


def test_render_benchmark_provided_and_none() -> None:
    provided = cli.render_benchmark(
        {
            "source": "provided",
            "content_type": "html",
            "original_tokens": 100,
            "optimized_tokens": 20,
            "reduction_pct": 80.0,
            "transformers": ["converted_to_markdown"],
            "cache_used": False,
            "processing_time_ms": 1.2,
            "note": None,
        }
    )
    assert "80.0%" in provided
    assert "converted_to_markdown" in provided

    nothing = cli.render_benchmark({"source": "none", "note": "Nothing yet."})
    assert nothing == "Nothing yet."


def test_render_doctor_marks_fixes() -> None:
    out = cli.render_doctor(
        {
            "healthy": False,
            "checks": [
                {"name": "gateway", "status": "ok", "detail": "running", "fix": None},
                {
                    "name": "cache",
                    "status": "fail",
                    "detail": "redis unreachable",
                    "fix": "start redis",
                },
            ],
        }
    )
    assert "gateway" in out
    assert "start redis" in out
    assert "Problems found" in out


def test_render_logs_is_metadata_only() -> None:
    out = cli.render_logs(
        {
            "count": 1,
            "events": [
                {
                    "timestamp": 1_700_000_000.0,
                    "provider": "openai",
                    "endpoint": "chat/completions",
                    "applied": True,
                    "skip_reason": None,
                    "content_types": ["html"],
                    "transformers": ["html"],
                    "tokens_saved": 500,
                    "cache_status": "hit",
                    "execution_time_ms": 2.0,
                }
            ],
        }
    )
    assert "chat/completions" in out
    assert "500" in out
    assert "cache=hit" in out


def test_render_explain_humanizes_steps() -> None:
    out = cli.render_explain(
        {
            "events": [
                {
                    "applied": True,
                    "endpoint": "messages",
                    "content_types": ["html"],
                    "transformers": ["html"],
                    "steps": [
                        "removed_scripts",
                        "removed_ads_and_banners",
                        "converted_to_markdown",
                    ],
                    "tokens_before": 52183,
                    "tokens_after": 18924,
                    "tokens_saved": 33259,
                    "cache_status": "miss",
                    "execution_time_ms": 3.1,
                    "skip_reason": None,
                }
            ]
        }
    )
    assert "52,183" in out
    assert "63.7%" in out
    assert "Removed scripts" in out
    assert "Converted HTML to Markdown" in out
    assert "MISS" in out


def test_render_explain_with_no_history() -> None:
    assert "No requests optimized yet" in cli.render_explain({"events": []})


def test_humanize_step_generic_and_special() -> None:
    assert cli.humanize_step("minified_json") == "Minified JSON"
    assert cli.humanize_step("format_pdf") == "Extracted PDF"
    assert cli.humanize_step("collapsed_blank_lines") == "Collapsed blank lines"
    assert cli.humanize_step("removed_duplicate_blocks") == "Removed duplicate instruction block"
    assert cli.humanize_step("removed_duplicate_list_items") == "Removed duplicate instructions"


def test_toggle_helpers_target_prompt_feature() -> None:
    assert cli._normalize_feature("prompt") == "prompt"
    assert cli._normalize_feature("PROMPT") == "prompt"
    assert cli._normalize_feature("nonsense") is None
    assert cli._normalize_feature(None) is None
    assert cli._toggle_path("enable", "prompt") == "/internal/enable?feature=prompt"
    assert cli._toggle_path("disable", None) == "/internal/disable"


def test_render_toggle_distinguishes_global_and_prompt() -> None:
    both_on = {"optimization_enabled": True, "prompt_optimization_enabled": True}
    assert cli._render_toggle(both_on, "prompt") == "Prompt optimization enabled."
    assert cli._render_toggle(both_on, None) == "Optimization enabled."
    prompt_off = {"optimization_enabled": True, "prompt_optimization_enabled": False}
    assert cli._render_toggle(prompt_off, "prompt") == "Prompt optimization disabled."


def test_parser_enable_disable_accept_feature() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["enable", "prompt"]).feature == "prompt"
    assert parser.parse_args(["disable", "prompt"]).feature == "prompt"
    assert parser.parse_args(["enable"]).feature is None


def test_discover_prefers_explicit_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.discover("http://host:9000", None).base_url == "http://host:9000"

    monkeypatch.setenv("ZIBBO_GATEWAY_URL", "http://from-env:1234")
    assert cli.discover(None, None).base_url == "http://from-env:1234"


def test_parser_accepts_every_subcommand() -> None:
    parser = cli.build_parser()
    for command in (
        "status",
        "banner",
        "claude",
        "stats",
        "doctor",
        "enable",
        "disable",
        "logs",
        "explain",
        "version",
        "start",
    ):
        assert parser.parse_args([command]).command == command
    args = parser.parse_args(["benchmark", "--content", "hi", "--model", "gpt-4o"])
    assert args.content == "hi"
    assert args.model == "gpt-4o"


def test_parser_accepts_plugin_actions() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["plugin"]).command == "plugin"
    assert parser.parse_args(["plugin"]).action == "status"  # default
    for action in ("status", "verify", "sync", "dev"):
        assert parser.parse_args(["plugin", action]).action == action
    assert parser.parse_args(["plugin", "sync", "--plugin-dir", "/x"]).plugin_dir == "/x"


def test_bare_invocation_has_no_subcommand() -> None:
    # A bare `zibbo` parses with command=None; main() maps that to the dashboard, so the
    # plugin's `!`zibbo $ARGUMENTS`` with no argument works.
    assert cli.build_parser().parse_args([]).command is None


def test_banner_accepts_start_flag() -> None:
    args = cli.build_parser().parse_args(["banner", "--start"])
    assert args.command == "banner"
    assert args.start is True


def test_session_start_hook_is_exec_form_without_shell() -> None:
    root = Path(__file__).resolve().parent.parent
    hooks = json.loads(
        (root / "plugins" / "claude-code" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    entry = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["type"] == "command"
    # Exec form: a bare executable plus an args vector — no shell string for a checker to
    # reject and no bash dependency on any platform.
    assert entry["command"] == "zibbo"
    assert entry["args"] == ["banner", "--start"]
    blob = entry["command"] + " " + " ".join(entry["args"])
    for meta in ("${", "$(", "&&", "||", "`", ";", "|", ">"):
        assert meta not in blob


def test_slash_command_uses_documented_args_without_expansion() -> None:
    root = Path(__file__).resolve().parent.parent
    text = (root / "plugins" / "claude-code" / "commands" / "zibbo.md").read_text(encoding="utf-8")
    # None of the constructs Claude Code's permission checker rejects.
    for forbidden in ("${", "$(", "&&", "||", 'sh "', "command -v", "CLAUDE_PLUGIN_ROOT"):
        assert forbidden not in text
    # The documented argument mechanism, plus a matching allow rule.
    assert "!`zibbo $ARGUMENTS`" in text
    assert "Bash(zibbo *)" in text


def test_marketplace_and_plugin_manifests_are_valid() -> None:
    root = Path(__file__).resolve().parent.parent
    for manifest in (
        root / ".claude-plugin" / "marketplace.json",
        root / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json",
        root / "plugins" / "claude-code" / "hooks" / "hooks.json",
        root / ".codex-plugin" / "marketplace.json",
        root / "plugins" / "codex" / ".codex-plugin" / "plugin.json",
    ):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# -- Phase 11 / 13: activation banner, dashboard (configured vs observed) -----

_CFG_SUBSCRIPTION = {"present": True, "label": "Claude subscription (OAuth login)"}
_OBS_API_KEY = {"present": True, "label": "Claude API key"}
_SUBSCRIPTION_AUTH = {"present": True, "label": "Claude subscription (OAuth login)"}


def _routing(*, configured: bool, observed: bool | None) -> dict[str, object]:
    return {
        "configured": configured,
        "observed": observed,
        "expected_base_url": "http://localhost:8000/anthropic",
        "reason": "ANTHROPIC_BASE_URL is not set",
    }


def _auth(
    *, configured: dict[str, object], observed: dict[str, object] | None
) -> dict[str, object]:
    return {"configured": configured, "observed": observed}


def _stats(**kw: object) -> dict[str, object]:
    base = {
        "requests": 182,
        "token_reduction_pct": 31.0,
        "cache_hit_rate": 0.84,
        "estimated_cost_usd": 3.91,
    }
    base.update(kw)
    return base


def test_render_banner_active_when_routing_observed() -> None:
    out = cli.render_banner(
        {
            "gateway": {"running": True, "version": "0.1.0"},
            "auth": _auth(configured=_CFG_SUBSCRIPTION, observed=None),
            "routing": _routing(configured=True, observed=True),
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
        }
    )
    assert "Zibbo Active" in out
    assert "Through Zibbo" in out
    assert "/zibbo" in out


def test_render_banner_one_step_left_never_mentions_api_keys() -> None:
    out = cli.render_banner(
        {
            "gateway": {"running": True, "version": "0.1.0"},
            "auth": _auth(configured=_CFG_SUBSCRIPTION, observed=None),
            "routing": _routing(configured=False, observed=False),
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
        }
    )
    assert "one step left" in out
    assert "authenticated correctly" in out
    assert "export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic" in out
    assert "API key" not in out  # a subscription user is never told to make one


def test_render_banner_gateway_down() -> None:
    out = cli.render_banner(
        {
            "gateway": None,
            "auth": _auth(configured=_CFG_SUBSCRIPTION, observed=None),
            "routing": _routing(configured=False, observed=None),
        }
    )
    assert "not running" in out.lower()
    assert "zibbo start" in out


def test_render_dashboard_shows_savings_and_both_routing_views() -> None:
    out = cli.render_dashboard(
        {
            "gateway": {"version": "0.1.0", "environment": "development"},
            "provider": "Anthropic",
            "auth": _auth(configured=_CFG_SUBSCRIPTION, observed=_OBS_API_KEY),
            "routing": _routing(configured=True, observed=True),
            "stats": _stats(),
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
        }
    )
    assert "Anthropic" in out
    assert "182" in out
    assert "31.0%" in out
    assert "84.0%" in out
    assert "$3.91" in out
    assert "Healthy" in out
    # Both views are rendered.
    assert "Configured" in out
    assert "Observed" in out


def test_dashboard_prefers_observed_reality_over_env_intent() -> None:
    # THE regression: the gateway has served Anthropic traffic (observed routing + auth),
    # but ANTHROPIC_BASE_URL is not visible in this shell (configured=False). The old code
    # showed "Not routed" / "Not authenticated" and "Needs attention" — internally
    # inconsistent with 36 processed requests. Reality must win.
    out = cli.render_dashboard(
        {
            "gateway": {"version": "0.1.0", "environment": "development"},
            "provider": "Anthropic",
            "auth": _auth(configured={"present": False, "label": ""}, observed=_OBS_API_KEY),
            "routing": _routing(configured=False, observed=True),
            "stats": _stats(requests=36, cache_hit_rate=0.93),
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
        }
    )
    assert "Healthy" in out  # not "Needs attention"
    assert "Needs attention" not in out
    assert "Active" in out  # observed routing
    assert "Claude API key" in out  # observed auth, not "not authenticated"
    # It exposes the mismatch instead of nagging to set the env var.
    assert "Traffic is flowing through Zibbo" in out
    assert "The only missing step is routing" not in out


def _dashboard(transformers: list[str]) -> str:
    return cli.render_dashboard(
        {
            "gateway": {"version": "0.1.0", "environment": "development"},
            "provider": "Anthropic",
            "auth": _auth(configured=_CFG_SUBSCRIPTION, observed=_OBS_API_KEY),
            "routing": _routing(configured=True, observed=True),
            "stats": _stats(),
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
            "transformers": transformers,
        }
    )


def test_dashboard_lists_registered_transformers_and_marks_prompt_disabled() -> None:
    # Prompt off: the registry the gateway reports has no `prompt`, so the dashboard
    # shows it explicitly as disabled rather than silently omitting it.
    off = _dashboard(["html", "json", "text"])
    assert "Transformers" in off
    for name in ("html", "json", "text"):
        assert name in off
    assert "prompt (disabled)" in off


def test_dashboard_shows_prompt_active_when_registered() -> None:
    # Prompt on: it is in the registry the gateway reports, so it renders as active with
    # no "disabled" marker. This is what proves the list mirrors the runtime registry.
    on = _dashboard(["html", "json", "prompt", "text"])
    assert "prompt" in on
    assert "prompt (disabled)" not in on


class _FakeDoctorClient:
    """A stand-in for the HTTP client so `_build_doctor_checks` can run without a socket.

    Returns a chosen `/internal/status` and refuses `/internal/claude` (its checks are not
    under test), exactly as an unreachable-claude gateway would.
    """

    base_url = "http://127.0.0.1:8000"

    def __init__(self, status: dict[str, object]) -> None:
        self._status = status

    def reachable(self) -> bool:
        return True

    def get(self, path: str) -> object:
        if path == "/internal/status":
            return self._status
        raise cli.GatewayError("not available")


def _prompt_doctor_check(*, prompt_on: bool, transformers: list[str]) -> dict[str, object]:
    status = {**_STATUS, "prompt_optimization_enabled": prompt_on, "transformers": transformers}
    checks = cli._build_doctor_checks(_FakeDoctorClient(status))  # type: ignore[arg-type]
    return next(check for check in checks if check["name"] == "prompt optimizer")


def test_doctor_prompt_check_reflects_registry_and_names_the_env_var() -> None:
    off = _prompt_doctor_check(prompt_on=False, transformers=["html", "json", "text"])
    assert off["status"] == "warn"
    assert off["detail"] == "disabled"
    # Requirement: the reason must name the actual cause, and the fix both enable methods.
    assert "ZIBBO_PROMPT_OPTIMIZATION=false" in str(off["reason"])
    assert "ZIBBO_PROMPT_OPTIMIZATION=true" in str(off["fix"])
    assert "zibbo enable prompt" in str(off["fix"])

    on = _prompt_doctor_check(prompt_on=True, transformers=["html", "json", "prompt", "text"])
    assert on["status"] == "ok"
    assert on["detail"] == "enabled"


def test_render_doctor_shows_problem_reason_fix() -> None:
    out = cli.render_doctor(
        {
            "healthy": False,
            "checks": [
                {"name": "gateway", "status": "ok", "detail": "running 0.1.0"},
                {
                    "name": "routing",
                    "status": "warn",
                    "detail": "not routed through Zibbo",
                    "problem": "this session's Claude traffic bypasses Zibbo",
                    "reason": "ANTHROPIC_BASE_URL is not set",
                    "fix": "export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic",
                },
            ],
        }
    )
    assert "problem:" in out
    assert "reason:" in out
    assert "fix:" in out
    assert "Working, with warnings" in out  # a warn is not a failure


def test_render_claude_is_metadata_only() -> None:
    out = cli.render_claude(
        {
            "gateway_running": True,
            "gateway_version": "0.1.0",
            "internal_api_version": "1",
            "optimization_enabled": True,
            "cache_enabled": True,
            "cache_backend": "memory",
            "anthropic_route": "/anthropic",
            "routing_observed": True,
            "anthropic_requests_observed": 36,
            "authentication": {
                "present": False,
                "label": "Not authenticated",
                "method": "none",
                "detail": "",
            },
            "observed_authentication": {
                "present": True,
                "label": "Claude API key",
                "method": "api_key",
                "detail": "observed on forwarded Anthropic requests",
            },
        }
    )
    assert "/anthropic" in out
    assert "36 Anthropic requests" in out
    assert "Claude API key" in out  # observed reality shown even when env says none


def test_routing_help_never_mentions_api_keys() -> None:
    text = "\n".join(
        cli._routing_help(
            _SUBSCRIPTION_AUTH,
            {"routed": False, "expected_base_url": "http://localhost:8000/anthropic", "reason": ""},
        )
    )
    assert "api key" not in text.lower()
    assert "export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic" in text
