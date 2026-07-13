"""Detect how Claude Code is authenticated, and whether this session is routed through
Zibbo — from the environment, never from secrets.

This is the one Claude-Code-specific module in the gateway, and it is pure UX: it powers
the plugin's activation banner, ``zibbo status``, ``zibbo doctor``, and the
``/internal/claude`` endpoint. The optimization engine stays provider-agnostic and never
imports this.

Security contract — the whole point of the module:
    Every function classifies authentication by environment-variable *name* and by
    non-secret *shape*. It never returns, logs, stores, or derives a credential value.
    Only the *kind* of authentication ever leaves here. Authentication belongs entirely
    to Claude Code; Zibbo only observes which mode is in play.

Stdlib only, so the ``zibbo`` CLI — which runs inside Claude Code's own environment, where
these variables live — can import it without pulling in the gateway's dependencies.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

# -- Authentication ----------------------------------------------------------

# Environment variables and settings keys Claude Code documents for authentication. We key
# off names and cloud toggles only — never the values.
_ENV_API_KEY = "ANTHROPIC_API_KEY"
_ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"  # noqa: S105 — an env var name, not a secret
_ENV_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — an env var name, not a secret
_ENV_USE_BEDROCK = "CLAUDE_CODE_USE_BEDROCK"
_ENV_USE_VERTEX = "CLAUDE_CODE_USE_VERTEX"
_ENV_USE_FOUNDRY = "CLAUDE_CODE_USE_FOUNDRY"
_ENV_IN_CLAUDE_CODE = "CLAUDECODE"  # Claude Code sets this to "1" for its subprocesses.

# Human labels, keyed by the machine ``method``. Kept together so the CLI and the endpoint
# render identically.
_AUTH_LABELS = {
    "subscription": "Claude subscription (OAuth login)",
    "oauth_token": "Claude OAuth token",
    "api_key": "Claude API key",
    "auth_token": "Claude auth token",
    "api_key_helper": "Claude API key (via apiKeyHelper)",
    "bedrock": "Amazon Bedrock",
    "vertex": "Google Vertex AI",
    "foundry": "Microsoft Foundry",
    "none": "Not authenticated",
}


@dataclass(frozen=True, slots=True)
class AuthInfo:
    """The *kind* of authentication Claude Code is using. Never a credential value."""

    method: str  # machine key, one of _AUTH_LABELS
    present: bool  # is any Claude authentication configured?
    detail: str  # human explanation of the signal we matched (no secrets)
    also_present: tuple[str, ...] = ()  # other methods whose signals were also set

    @property
    def label(self) -> str:
        return _AUTH_LABELS.get(self.method, self.method)

    @property
    def is_api_key(self) -> bool:
        """True only when a raw Anthropic API key is the active credential.

        The gate for whether setup guidance may mention creating an API key. Subscription,
        OAuth, auth-token, and cloud-provider users must never be told to make one.
        """
        return self.method == "api_key"


def _truthy(value: str | None) -> bool:
    """A Claude Code cloud toggle is on for any value that is not empty/0/false/no."""
    return value is not None and value.strip().lower() not in ("", "0", "false", "no", "off")


def detect_auth(
    env: Mapping[str, str] | None = None, settings: Mapping[str, object] | None = None
) -> AuthInfo:
    """Classify Claude Code's active authentication from the environment and settings.

    Precedence follows how Claude Code resolves credentials: a cloud-provider transport
    wins outright; otherwise an explicit token or key; then ``apiKeyHelper``; then an
    OAuth token; and finally — inside a Claude Code session with nothing else set — an
    OAuth subscription login managed by Claude Code itself.
    """
    env = os.environ if env is None else env
    settings = {} if settings is None else settings

    signals: list[tuple[str, str]] = []  # (method, detail), highest precedence first
    if _truthy(env.get(_ENV_USE_BEDROCK)):
        signals.append(("bedrock", f"via {_ENV_USE_BEDROCK}"))
    if _truthy(env.get(_ENV_USE_VERTEX)):
        signals.append(("vertex", f"via {_ENV_USE_VERTEX}"))
    if _truthy(env.get(_ENV_USE_FOUNDRY)):
        signals.append(("foundry", f"via {_ENV_USE_FOUNDRY}"))
    if env.get(_ENV_AUTH_TOKEN):
        signals.append(("auth_token", f"via {_ENV_AUTH_TOKEN}"))
    if env.get(_ENV_API_KEY):
        signals.append(("api_key", f"via {_ENV_API_KEY}"))
    if "apiKeyHelper" in settings:
        signals.append(("api_key_helper", "via apiKeyHelper in settings.json"))
    if env.get(_ENV_OAUTH_TOKEN):
        signals.append(("oauth_token", f"via {_ENV_OAUTH_TOKEN}"))

    if signals:
        method, detail = signals[0]
        others = tuple(m for m, _ in signals[1:])
        return AuthInfo(method=method, present=True, detail=detail, also_present=others)

    # No explicit signal. Inside a Claude Code session this means the default: an OAuth
    # subscription login Claude Code manages in the OS keychain (Pro / Max / Team /
    # Enterprise all look identical here — the tier is not exposed to the environment).
    if _truthy(env.get(_ENV_IN_CLAUDE_CODE)):
        return AuthInfo(
            method="subscription",
            present=True,
            detail="OAuth login managed by Claude Code",
        )
    return AuthInfo(
        method="none",
        present=False,
        detail="no Claude credentials found in this environment",
    )


# -- Observed authentication (from forwarded request headers) ----------------


def observed_auth(method: str | None) -> AuthInfo:
    """Wrap an observed auth-method key (from :func:`classify_auth_header`) as AuthInfo.

    ``method`` is what was actually seen on forwarded Anthropic requests. ``None`` means
    no authenticated Anthropic traffic has been observed.
    """
    if method is None:
        return AuthInfo(
            method="none",
            present=False,
            detail="no authenticated Anthropic traffic observed",
        )
    return AuthInfo(
        method=method,
        present=True,
        detail="observed on forwarded Anthropic requests",
    )


def _header_ci(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that returns whether a header is set, safely.

    Used only to test presence and, for Authorization, the scheme — the credential value
    is never returned to callers or logged.
    """
    getter = getattr(headers, "get", None)
    if getter is not None:
        # Starlette/httpx Headers are already case-insensitive.
        value = headers.get(name)
        if value is not None:
            return value
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def classify_auth_header(headers: Mapping[str, str]) -> str | None:
    """Classify the *kind* of Anthropic credential on a request, from header names only.

    This is how the gateway *observes* authentication: Anthropic uses ``x-api-key`` for
    Console API keys and ``Authorization: Bearer`` for OAuth/subscription and auth tokens.
    We read only which header is present (and, for Authorization, that the scheme is
    Bearer) — never the credential value — so no secret is touched. Bearer is reported as
    ``oauth_token`` (the common Claude Code subscription case); the environment-side
    detector is what distinguishes OAuth from a custom auth token when it can. Returns a
    method key from :data:`_AUTH_LABELS`, or ``None`` when no credential header is present.
    """
    if _header_ci(headers, "x-api-key"):
        return "api_key"
    authz = _header_ci(headers, "authorization") or ""
    if authz[:7].lower() == "bearer " and authz[7:].strip():
        return "oauth_token"
    return None


# -- Routing -----------------------------------------------------------------

_ENV_BASE_URL = "ANTHROPIC_BASE_URL"
ANTHROPIC_PREFIX = "/anthropic"


@dataclass(frozen=True, slots=True)
class RoutingInfo:
    """Whether this session's Anthropic traffic is pointed at the Zibbo gateway."""

    routed: bool
    base_url: str | None  # ANTHROPIC_BASE_URL as set (a URL, never a secret)
    expected_base_url: str  # what it should be for routing to work
    reason: str


def _netloc(url: str) -> str:
    """Host:port, with localhost and 127.0.0.1 treated as the same host."""
    parts = urlsplit(url if "://" in url else f"//{url}")
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        host = "127.0.0.1"
    return f"{host}:{parts.port}" if parts.port else host


def detect_routing(
    env: Mapping[str, str] | None = None,
    gateway_base_url: str = "http://127.0.0.1:8000",
) -> RoutingInfo:
    """Compare ``ANTHROPIC_BASE_URL`` against the gateway to decide if we are in the path."""
    env = os.environ if env is None else env
    expected = gateway_base_url.rstrip("/") + ANTHROPIC_PREFIX
    base = env.get(_ENV_BASE_URL)

    if not base:
        return RoutingInfo(
            routed=False,
            base_url=None,
            expected_base_url=expected,
            reason=f"{_ENV_BASE_URL} is not set",
        )
    gateway_netloc = _netloc(gateway_base_url)
    if _netloc(base) != gateway_netloc:
        return RoutingInfo(
            routed=False,
            base_url=base,
            expected_base_url=expected,
            reason=f"{_ENV_BASE_URL} points at {base}, not the gateway",
        )
    if ANTHROPIC_PREFIX not in urlsplit(base).path:
        return RoutingInfo(
            routed=False,
            base_url=base,
            expected_base_url=expected,
            reason=f"{_ENV_BASE_URL} reaches the gateway but not its {ANTHROPIC_PREFIX} route",
        )
    return RoutingInfo(
        routed=True,
        base_url=base,
        expected_base_url=expected,
        reason="routed through the Zibbo gateway",
    )


# -- Settings ----------------------------------------------------------------


def read_claude_settings(cwd: Path | None = None, home: Path | None = None) -> dict[str, object]:
    """Best-effort merge of Claude Code's settings files, for ``apiKeyHelper`` detection.

    Reads only the settings JSON (never the credentials store). Project settings override
    user settings. Any missing or malformed file is skipped silently — this is advisory.
    """
    home = Path.home() if home is None else home
    cwd = Path.cwd() if cwd is None else cwd
    merged: dict[str, object] = {}
    candidates = [
        home / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged
