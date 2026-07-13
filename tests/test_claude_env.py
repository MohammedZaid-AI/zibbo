"""Claude Code environment detection: authentication mode and routing, no secrets.

The detector is the keystone of the first-class Claude Code experience — every banner,
dashboard, and doctor check reads from it. These tests pin the full auth matrix (the spec's
Pro / Max / Team / OAuth / API key / bearer / apiKeyHelper / cloud-provider cases), the
routing logic, and the one non-negotiable: a credential value never leaves the module.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.claude_env import (
    detect_auth,
    detect_routing,
    read_claude_settings,
)

_SECRET = "sk-ant-SUPER-SECRET-VALUE-do-not-leak"


# -- Authentication ----------------------------------------------------------


def test_subscription_login_is_the_default_inside_claude_code() -> None:
    # Pro / Max / Team / Enterprise all look identical here: an OAuth login Claude Code
    # manages, with nothing in the environment. The tier is deliberately not inferred.
    auth = detect_auth({"CLAUDECODE": "1"})
    assert auth.method == "subscription"
    assert auth.present is True
    assert auth.is_api_key is False
    assert "OAuth" in auth.label


def test_no_auth_outside_claude_code_is_reported_as_none() -> None:
    auth = detect_auth({})
    assert auth.method == "none"
    assert auth.present is False
    assert auth.is_api_key is False


def test_oauth_token_env() -> None:
    auth = detect_auth({"CLAUDE_CODE_OAUTH_TOKEN": _SECRET})
    assert auth.method == "oauth_token"
    assert auth.present is True
    assert auth.is_api_key is False


def test_api_key_is_the_only_method_that_flags_is_api_key() -> None:
    auth = detect_auth({"ANTHROPIC_API_KEY": _SECRET})
    assert auth.method == "api_key"
    assert auth.is_api_key is True


def test_auth_token_takes_precedence_over_api_key() -> None:
    auth = detect_auth({"ANTHROPIC_AUTH_TOKEN": _SECRET, "ANTHROPIC_API_KEY": _SECRET})
    assert auth.method == "auth_token"
    assert "api_key" in auth.also_present


def test_api_key_helper_from_settings() -> None:
    auth = detect_auth({}, {"apiKeyHelper": "/usr/local/bin/get-key.sh"})
    assert auth.method == "api_key_helper"
    assert auth.present is True
    assert auth.is_api_key is False


def test_cloud_providers_win_outright() -> None:
    assert detect_auth({"CLAUDE_CODE_USE_BEDROCK": "1"}).method == "bedrock"
    assert detect_auth({"CLAUDE_CODE_USE_VERTEX": "1"}).method == "vertex"
    assert detect_auth({"CLAUDE_CODE_USE_FOUNDRY": "1"}).method == "foundry"


def test_cloud_toggle_is_off_for_falsey_values() -> None:
    # A toggle set to 0/false/empty must not be read as enabled.
    assert detect_auth({"CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDECODE": "1"}).method == "subscription"
    assert detect_auth({"CLAUDE_CODE_USE_VERTEX": "false"}).method == "none"


def test_no_secret_ever_leaves_the_detector() -> None:
    # Every auth signal carries the same secret; it must appear in none of the output.
    env = {
        "ANTHROPIC_API_KEY": _SECRET,
        "ANTHROPIC_AUTH_TOKEN": _SECRET,
        "CLAUDE_CODE_OAUTH_TOKEN": _SECRET,
    }
    auth = detect_auth(env, {"apiKeyHelper": _SECRET})
    blob = f"{auth!r} {auth.label} {auth.detail} {auth.also_present}"
    assert _SECRET not in blob


# -- Routing -----------------------------------------------------------------


def test_routing_not_set() -> None:
    info = detect_routing({}, "http://127.0.0.1:8000")
    assert info.routed is False
    assert "not set" in info.reason
    assert info.expected_base_url == "http://127.0.0.1:8000/anthropic"


def test_routing_active_with_localhost_alias() -> None:
    # localhost and 127.0.0.1 are the same host.
    info = detect_routing(
        {"ANTHROPIC_BASE_URL": "http://localhost:8000/anthropic"}, "http://127.0.0.1:8000"
    )
    assert info.routed is True


def test_routing_points_elsewhere() -> None:
    info = detect_routing(
        {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, "http://127.0.0.1:8000"
    )
    assert info.routed is False
    assert "points at" in info.reason


def test_routing_reaches_gateway_but_wrong_path() -> None:
    info = detect_routing(
        {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8000/v1"}, "http://127.0.0.1:8000"
    )
    assert info.routed is False
    assert "/anthropic" in info.reason


def test_routing_base_url_is_echoed_but_is_not_a_secret() -> None:
    info = detect_routing(
        {"ANTHROPIC_BASE_URL": "http://localhost:8000/anthropic"}, "http://127.0.0.1:8000"
    )
    assert info.base_url == "http://localhost:8000/anthropic"


# -- Settings ----------------------------------------------------------------


def test_read_claude_settings_merges_and_survives_bad_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "x", "model": "sonnet"}), encoding="utf-8"
    )
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{ this is not json", encoding="utf-8")

    settings = read_claude_settings(cwd=project, home=home)
    assert "apiKeyHelper" in settings  # from user settings
    assert settings["model"] == "sonnet"  # malformed project file was skipped, not fatal
