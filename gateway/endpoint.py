"""Single source of truth for the gateway's network location and identity.

Stdlib-only, so the dependency-free ``zibbo`` CLI and the gateway itself share exactly one
resolver for the port, the base URL, the service identity, and the environment-variable
names. Change a default or an env-var name here and every subsystem — CLI discovery, the
spawned server, ``Settings``, routing detection — agrees automatically. There is no second
copy to drift.

Port precedence mirrors pydantic-settings' precedence for the gateway process, so the CLI
resolves the *same* port the gateway binds:

    an environment variable  >  a ``.env`` file  >  the built-in default

The ``.env`` step is what closes the historical drift: the gateway read ``.env`` (via
pydantic) while the CLI read only ``os.environ``, so a ``.env``-configured port left the CLI
probing the default and reporting a phantom "no gateway".

Client host vs. server host is deliberately *not* unified: the server binds broadly
(``0.0.0.0`` inside a container) while a client always connects to loopback. Those are two
correct-but-different values, not a single source, so only the client-facing default lives
here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# -- Identity ----------------------------------------------------------------

SERVICE_NAME = "zibbo"
"""The gateway's protocol identity. Fixed, unlike the user-configurable ``app_name`` — the
CLI matches on this to tell a real gateway from an unrelated HTTP service on the same port."""

# -- Network defaults --------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
"""Where a *client* connects. The server may bind wider (see module docstring)."""

DEFAULT_PORT = 8000

DEFAULT_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

# -- Environment variable names ----------------------------------------------

ENV_PORT = "ZIBBO_PORT"
ENV_GATEWAY_URL = "ZIBBO_GATEWAY_URL"
ENV_RELOAD = "ZIBBO_RELOAD"

_DEFAULT_DOTENV = Path(".env")


@dataclass(frozen=True, slots=True)
class PortResolution:
    """A resolved port and where it came from — so diagnostics can name the source."""

    port: int
    source: str  # human-readable: "ZIBBO_PORT", ".env", or "default"
    configured: bool  # True when env or .env supplied it (i.e. not the built-in default)


def _dotenv_value(key: str, dotenv_path: Path) -> str | None:
    """The value of ``key`` in a ``.env`` file, using python-dotenv — the *same* parser
    pydantic-settings uses for the gateway — so the CLI's interpretation of ``.env`` can never
    diverge from what the gateway actually binds.

    python-dotenv arrives as a dependency of pydantic-settings, so it is present exactly when
    the gateway is installed — which is the only situation in which a ``.env``-configured port
    matters (with no gateway installed there is nothing to bind it). If it is somehow absent,
    ``.env`` is skipped rather than reinterpreted by a second, drift-prone parser.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    try:
        return dotenv_values(dotenv_path).get(key)
    except OSError:
        return None


def _valid_port(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if raw.isdigit():
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    return None


def resolve_port(
    env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None
) -> PortResolution:
    """Resolve the gateway port with env > ``.env`` > default precedence."""
    env = os.environ if env is None else env
    dotenv_path = _DEFAULT_DOTENV if dotenv_path is None else dotenv_path

    from_env = _valid_port(env.get(ENV_PORT))
    if from_env is not None:
        return PortResolution(from_env, ENV_PORT, configured=True)

    from_dotenv = _valid_port(_dotenv_value(ENV_PORT, dotenv_path))
    if from_dotenv is not None:
        return PortResolution(from_dotenv, ".env", configured=True)

    return PortResolution(DEFAULT_PORT, "default", configured=False)


def default_base_url(
    env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None
) -> str:
    """The gateway base URL implied by the resolved port (never the explicit override).

    ``ZIBBO_GATEWAY_URL`` is a *client-side* override for "I know exactly where it is" and is
    layered on top by discovery — it is intentionally not consulted here, so this function
    always reflects where a locally-spawned gateway will actually listen.
    """
    return f"http://{DEFAULT_HOST}:{resolve_port(env, dotenv_path=dotenv_path).port}"


def is_gateway_identity(payload: object) -> bool:
    """Whether a ``/internal/version`` payload proves the responder is a Zibbo gateway.

    Authoritative signal: the explicit ``service`` field equals :data:`SERVICE_NAME`.

    TEMPORARY back-compat: a gateway built before the ``service`` field still identifies by
    carrying ``internal_api_version``. This bridges only the straddle-upgrade window — a new
    CLI talking to an old gateway *process* still running — which closes when that gateway is
    restarted. Since CLI and gateway ship from the same repo, that window is short.

    REMOVAL PLAN: delete the ``internal_api_version`` branch the next time
    ``INTERNAL_API_VERSION`` is incremented (in gateway/api/schemas/internal.py). By then every
    supported gateway emits ``service``, and the API-version bump is the natural, non-silent
    trigger — leaving the fallback past that point would keep the old fragile heuristic alive
    for no reason. A test asserts this fallback exists; it will fail loudly if removed early.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("service") == SERVICE_NAME:
        return True
    return "internal_api_version" in payload
