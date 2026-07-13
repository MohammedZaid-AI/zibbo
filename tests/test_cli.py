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


# -- Phase 11: activation banner, dashboard, composite doctor ----------------

_SUBSCRIPTION_AUTH = {
    "present": True,
    "label": "Claude subscription (OAuth login)",
    "detail": "OAuth login managed by Claude Code",
    "method": "subscription",
    "is_api_key": False,
}


def test_render_banner_active_when_routed() -> None:
    out = cli.render_banner(
        {
            "gateway": {"running": True, "version": "0.1.0"},
            "auth": _SUBSCRIPTION_AUTH,
            "routing": {
                "routed": True,
                "reason": "ok",
                "expected_base_url": "http://localhost:8000/anthropic",
            },
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
        }
    )
    assert "Zibbo Active" in out
    assert "Through Zibbo" in out
    assert "Claude subscription (OAuth login)" in out
    assert "/zibbo" in out


def test_render_banner_one_step_left_never_mentions_api_keys() -> None:
    out = cli.render_banner(
        {
            "gateway": {"running": True, "version": "0.1.0"},
            "auth": _SUBSCRIPTION_AUTH,
            "routing": {
                "routed": False,
                "reason": "ANTHROPIC_BASE_URL is not set",
                "expected_base_url": "http://localhost:8000/anthropic",
            },
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
            "auth": _SUBSCRIPTION_AUTH,
            "routing": {
                "routed": False,
                "reason": "",
                "expected_base_url": "http://localhost:8000/anthropic",
            },
        }
    )
    assert "not running" in out.lower()
    assert "zibbo start" in out


def test_render_dashboard_shows_savings_and_health() -> None:
    out = cli.render_dashboard(
        {
            "gateway": {"version": "0.1.0", "environment": "development"},
            "provider": "Anthropic",
            "auth": _SUBSCRIPTION_AUTH,
            "routing": {
                "routed": True,
                "reason": "",
                "expected_base_url": "http://localhost:8000/anthropic",
            },
            "stats": {
                "requests": 182,
                "token_reduction_pct": 31.0,
                "cache_hit_rate": 0.84,
                "estimated_cost_usd": 3.91,
            },
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
            "healthy": True,
        }
    )
    assert "Anthropic" in out
    assert "182" in out
    assert "31.0%" in out
    assert "84.0%" in out
    assert "$3.91" in out
    assert "Healthy" in out


def test_render_dashboard_nudges_routing_and_hints_savings_setup() -> None:
    out = cli.render_dashboard(
        {
            "gateway": {"version": "0.1.0", "environment": "development"},
            "provider": "openai",
            "auth": _SUBSCRIPTION_AUTH,
            "routing": {
                "routed": False,
                "reason": "not set",
                "expected_base_url": "http://localhost:8000/anthropic",
            },
            "stats": {
                "requests": 0,
                "token_reduction_pct": 0.0,
                "cache_hit_rate": 0.0,
                "estimated_cost_usd": None,
            },
            "optimization_enabled": True,
            "cache": {"enabled": True, "backend": "memory"},
            "healthy": False,
        }
    )
    assert "Needs attention" in out
    assert "export ANTHROPIC_BASE_URL" in out
    assert "ZIBBO_ANALYTICS_COST_PER_MILLION_TOKENS" in out


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
            "routing_observed": False,
            "authentication": {
                "present": True,
                "label": "Claude subscription (OAuth login)",
                "method": "subscription",
                "detail": "",
            },
        }
    )
    assert "/anthropic" in out
    assert "Claude subscription (OAuth login)" in out


def test_routing_help_never_mentions_api_keys() -> None:
    text = "\n".join(
        cli._routing_help(
            _SUBSCRIPTION_AUTH,
            {"routed": False, "expected_base_url": "http://localhost:8000/anthropic", "reason": ""},
        )
    )
    assert "api key" not in text.lower()
    assert "export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic" in text
