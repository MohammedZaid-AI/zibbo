"""The single source of truth for the gateway's location and identity.

These lock the contract every subsystem depends on: port precedence (env > .env >
default), the client-facing base URL, and the gateway identity protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway import endpoint
from gateway.config import ENV_PREFIX, Settings


def test_default_port_when_nothing_configured() -> None:
    res = endpoint.resolve_port({}, dotenv_path=Path("does-not-exist.env"))
    assert res.port == endpoint.DEFAULT_PORT
    assert res.source == "default"
    assert res.configured is False


def test_env_var_takes_precedence(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ZIBBO_PORT=9100\n", encoding="utf-8")
    res = endpoint.resolve_port({"ZIBBO_PORT": "8500"}, dotenv_path=dotenv)
    assert (res.port, res.source, res.configured) == (8500, "ZIBBO_PORT", True)


def test_dotenv_used_when_env_absent(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    # export prefix, surrounding quotes, comments, and other keys must all be handled.
    dotenv.write_text('# port\nexport ZIBBO_PORT="9100"\nOTHER=x\n', encoding="utf-8")
    res = endpoint.resolve_port({}, dotenv_path=dotenv)
    assert (res.port, res.source, res.configured) == (9100, ".env", True)


def test_invalid_port_falls_through_to_default(tmp_path: Path) -> None:
    # Out-of-range env and non-numeric .env both fall through to the built-in default.
    dotenv = tmp_path / ".env"
    dotenv.write_text("ZIBBO_PORT=notaport\n", encoding="utf-8")
    res = endpoint.resolve_port({"ZIBBO_PORT": "70000"}, dotenv_path=dotenv)
    assert res.port == endpoint.DEFAULT_PORT
    assert res.configured is False


def test_default_base_url_reflects_resolved_port(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ZIBBO_PORT=9100\n", encoding="utf-8")
    assert endpoint.default_base_url({}, dotenv_path=dotenv) == "http://127.0.0.1:9100"


def test_missing_dotenv_is_not_an_error() -> None:
    res = endpoint.resolve_port({}, dotenv_path=Path("/no/such/.env"))
    assert res.port == endpoint.DEFAULT_PORT


def test_gateway_and_cli_share_the_port_default() -> None:
    # The whole point of the module: the server's Settings default and the CLI resolver
    # must be the same value, so they cannot drift.
    assert Settings().port == endpoint.DEFAULT_PORT


def test_settings_reads_the_same_env_var_name() -> None:
    # Settings resolves ZIBBO_PORT via env_prefix + field name; endpoint hardcodes the name.
    # If either changes, this catches the divergence.
    assert f"{ENV_PREFIX}PORT" == endpoint.ENV_PORT.upper()


@pytest.mark.parametrize(
    ("env_port", "dotenv_line"),
    [
        (None, None),  # neither → both must land on the default
        (None, "ZIBBO_PORT=9100"),  # .env only
        (None, 'export ZIBBO_PORT="9100"'),  # .env with export + quotes
        (None, "ZIBBO_PORT=9100  # inline comment"),  # dotenv strips the comment
        (None, "ZIBBO_PORT = 9100"),  # spaces around =
        ("8500", "ZIBBO_PORT=9100"),  # env var wins over .env for both
    ],
)
def test_endpoint_and_settings_agree_on_the_same_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_port: str | None,
    dotenv_line: str | None,
) -> None:
    """The one guarantee that matters: the CLI resolver and the server's Settings produce
    the SAME port from the same environment and .env. Two behaviours (endpoint degrades,
    Settings validates) — but never a divergent interpretation on valid input. This fails
    the instant endpoint's parser and pydantic's parser disagree.
    """
    monkeypatch.chdir(tmp_path)  # both read ".env" relative to CWD
    monkeypatch.delenv("ZIBBO_PORT", raising=False)
    if env_port is not None:
        monkeypatch.setenv("ZIBBO_PORT", env_port)
    if dotenv_line is not None:
        (tmp_path / ".env").write_text(dotenv_line + "\n", encoding="utf-8")

    endpoint_port = endpoint.resolve_port().port
    settings_port = Settings().port
    assert endpoint_port == settings_port


def test_identity_accepts_explicit_service() -> None:
    assert endpoint.is_gateway_identity({"service": "zibbo", "gateway_version": "0.1.1"})


def test_identity_back_compat_on_internal_api_version() -> None:
    # A gateway predating the `service` field still identifies by the version key.
    assert endpoint.is_gateway_identity({"internal_api_version": "1"})


def test_identity_rejects_foreign_and_malformed() -> None:
    assert endpoint.is_gateway_identity({"hello": "other api"}) is False
    assert endpoint.is_gateway_identity({"service": "grafana"}) is False
    assert endpoint.is_gateway_identity("not a dict") is False
    assert endpoint.is_gateway_identity(None) is False
