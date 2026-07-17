"""Lifecycle management for ``zibbo start`` / ``zibbo stop``.

The risky logic — merging and restoring Claude Code's settings, backups, rollback, and the
PID file — lives in ``gateway.lifecycle`` precisely so it can be tested here against a
``tmp_path`` with no gateway and no socket. Every requirement scenario has a test.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from gateway import lifecycle

GATEWAY = "http://127.0.0.1:8000"
ROUTED = "http://127.0.0.1:8000/anthropic"  # lifecycle.desired_base_url(GATEWAY)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def _write(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read(target: Path) -> dict:
    return json.loads(target.read_text(encoding="utf-8"))


def test_desired_base_url_appends_the_anthropic_prefix() -> None:
    assert lifecycle.desired_base_url(GATEWAY) == ROUTED
    assert lifecycle.desired_base_url(GATEWAY + "/") == ROUTED


# -- start ------------------------------------------------------------------


def _state(target: Path) -> dict:
    return json.loads(lifecycle.backup_path(target).read_text(encoding="utf-8"))


def test_start_with_no_settings_file_creates_it(target: Path) -> None:
    result = lifecycle.configure_routing(ROUTED, target=target)
    assert result.created_file is True
    assert _read(target) == {"env": {"ANTHROPIC_BASE_URL": ROUTED}}
    # A state file records that there was no prior file — so stop knows to remove the var.
    state = _state(target)
    assert state["original_existed"] is False
    assert state["original"] is None


def test_start_with_existing_settings_merges_and_preserves_keys(target: Path) -> None:
    _write(target, {"theme": "dark", "permissions": {"allow": ["Bash"]}, "experimental": True})
    result = lifecycle.configure_routing(ROUTED, target=target)

    data = _read(target)
    assert data["theme"] == "dark"  # unrelated keys untouched
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["experimental"] is True
    assert data["env"]["ANTHROPIC_BASE_URL"] == ROUTED
    assert result.backed_up is True  # the file existed, so it was backed up


def test_start_with_existing_env_preserves_other_env_vars(target: Path) -> None:
    _write(target, {"env": {"FOO": "bar", "CLAUDE_CODE_USE_BEDROCK": "1"}})
    lifecycle.configure_routing(ROUTED, target=target)

    env = _read(target)["env"]
    assert env["FOO"] == "bar"  # other env vars survive
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_BASE_URL"] == ROUTED


def test_start_with_existing_base_url_records_previous(target: Path) -> None:
    _write(target, {"env": {"ANTHROPIC_BASE_URL": "https://proxy.internal"}})
    result = lifecycle.configure_routing(ROUTED, target=target)

    assert result.previous_base_url == "https://proxy.internal"
    assert _read(target)["env"]["ANTHROPIC_BASE_URL"] == ROUTED
    # The state file holds the true pre-Zibbo value for stop to restore.
    assert _state(target)["original"]["env"]["ANTHROPIC_BASE_URL"] == "https://proxy.internal"


def test_backup_is_created_before_modification(target: Path) -> None:
    original = {"env": {"ANTHROPIC_BASE_URL": "https://old"}, "theme": "light"}
    _write(target, original)
    lifecycle.configure_routing(ROUTED, target=target)
    # The state file preserves the full document as it was BEFORE the change.
    assert _state(target)["original"] == original


# -- stop -------------------------------------------------------------------


def test_stop_restores_previous_value(target: Path) -> None:
    _write(target, {"env": {"ANTHROPIC_BASE_URL": "https://proxy.internal"}, "theme": "dark"})
    lifecycle.configure_routing(ROUTED, target=target)
    result = lifecycle.restore_routing(target=target)

    assert result.restored_to == "https://proxy.internal"
    data = _read(target)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "https://proxy.internal"  # restored
    assert data["theme"] == "dark"
    assert not lifecycle.backup_path(target).exists()  # backup cleaned up


def test_stop_removes_only_zibbo_changes(target: Path) -> None:
    # No prior ANTHROPIC_BASE_URL: stop must remove exactly the variable Zibbo added and
    # leave everything else — including other env vars — in place.
    _write(target, {"env": {"FOO": "bar"}, "permissions": {"allow": ["Read"]}})
    lifecycle.configure_routing(ROUTED, target=target)
    result = lifecycle.restore_routing(target=target)

    assert result.removed is True
    data = _read(target)
    assert "ANTHROPIC_BASE_URL" not in data["env"]
    assert data["env"]["FOO"] == "bar"  # other env vars untouched
    assert data["permissions"] == {"allow": ["Read"]}


def test_merge_then_restore_leaves_unrelated_keys_intact(target: Path) -> None:
    original = {
        "theme": "dark",
        "permissions": {"allow": ["Bash", "Read"]},
        "experimental": {"flag": True},
        "env": {"OTHER": "keep"},
    }
    _write(target, original)
    lifecycle.configure_routing(ROUTED, target=target)
    lifecycle.restore_routing(target=target)

    # Full round-trip returns to the original (only ANTHROPIC_BASE_URL was ever touched).
    assert _read(target) == original


# -- rollback / failure modes -----------------------------------------------


def test_invalid_json_is_refused_without_modification(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ this is not: valid json ", encoding="utf-8")
    with pytest.raises(lifecycle.SettingsError):
        lifecycle.configure_routing(ROUTED, target=target)
    # The unparseable file is left exactly as-is, and no backup was written.
    assert target.read_text(encoding="utf-8") == "{ this is not: valid json "
    assert not lifecycle.backup_path(target).exists()


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file perms")
def test_read_only_settings_rolls_back(target: Path) -> None:
    original = {"env": {"ANTHROPIC_BASE_URL": "https://old"}, "theme": "dark"}
    _write(target, original)
    os.chmod(target, 0o444)
    if os.access(target, os.W_OK):  # some platforms/filesystems ignore the read-only bit
        os.chmod(target, 0o644)
        pytest.skip("filesystem does not enforce read-only")
    try:
        with pytest.raises(lifecycle.SettingsError):
            lifecycle.configure_routing(ROUTED, target=target)
        # Original content is intact after the failed, rolled-back write.
        os.chmod(target, 0o644)
        assert _read(target) == original
        assert not lifecycle.backup_path(target).exists()  # rollback removed our backup
    finally:
        os.chmod(target, 0o644)


# -- idempotence ------------------------------------------------------------


def test_repeated_start_is_idempotent_and_preserves_original(target: Path) -> None:
    _write(target, {"env": {"ANTHROPIC_BASE_URL": "https://original"}})
    lifecycle.configure_routing(ROUTED, target=target)
    second = lifecycle.configure_routing(ROUTED, target=target)

    assert second.already_routed is True
    assert _read(target)["env"]["ANTHROPIC_BASE_URL"] == ROUTED
    # The state still holds the ORIGINAL value, not the gateway URL from the first run.
    assert _state(target)["original"]["env"]["ANTHROPIC_BASE_URL"] == "https://original"

    # And a stop after repeated starts restores the true original.
    lifecycle.restore_routing(target=target)
    assert _read(target)["env"]["ANTHROPIC_BASE_URL"] == "https://original"


def test_repeated_stop_is_a_safe_no_op(target: Path) -> None:
    _write(target, {"env": {"ANTHROPIC_BASE_URL": "https://original"}})
    lifecycle.configure_routing(ROUTED, target=target)
    lifecycle.restore_routing(target=target)
    second = lifecycle.restore_routing(target=target)  # nothing left to do

    assert second.changed is False
    assert _read(target)["env"]["ANTHROPIC_BASE_URL"] == "https://original"


def test_stop_with_no_settings_file_does_nothing(target: Path) -> None:
    result = lifecycle.restore_routing(target=target)
    assert result.changed is False
    assert not target.exists()


# -- PID file / process ownership -------------------------------------------


def test_pidfile_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    lifecycle.write_pidfile(4321, GATEWAY, path=path)
    info = lifecycle.read_pidfile(path=path)
    assert info == {"pid": 4321, "url": GATEWAY}
    lifecycle.clear_pidfile(path=path)
    assert lifecycle.read_pidfile(path=path) is None


def test_stop_gateway_leaves_an_unowned_process_alone(tmp_path: Path) -> None:
    # No PID file, but something is answering: it was started another way -> do not kill.
    path = tmp_path / "gateway.json"
    result = lifecycle.stop_gateway(lambda: True, path=path)
    assert result.outcome == "not_owned"


def test_stop_gateway_reports_not_running_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    result = lifecycle.stop_gateway(lambda: False, path=path)
    assert result.outcome == "not_running"


def test_stop_gateway_terminates_a_real_child_process(tmp_path: Path) -> None:
    # A live process we "own": stop must terminate it and confirm via the reachability probe.
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    path = tmp_path / "gateway.json"
    lifecycle.write_pidfile(proc.pid, GATEWAY, path=path)

    # Reachable until the process is actually gone.
    result = lifecycle.stop_gateway(lambda: proc.poll() is None, path=path)

    assert result.outcome == "stopped"
    assert proc.wait(timeout=5) is not None  # the child really exited
    assert lifecycle.read_pidfile(path=path) is None  # pid file cleared
