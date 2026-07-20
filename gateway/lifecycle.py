"""Lifecycle management for ``zibbo start`` / ``zibbo stop``.

This is the product feature behind the two commands: starting the gateway *and* pointing
Claude Code at it, then reversing both — so a user never edits ``settings.local.json`` by
hand. It is deliberately stdlib-only (like ``cli`` and ``claude_env``) so the CLI keeps
importing without the gateway's dependencies, and every risky operation lives here rather
than in ``cli`` so it is unit-testable against a ``tmp_path`` with no socket.

Two responsibilities, kept separate:

* **Routing config** — merge/restore ``ANTHROPIC_BASE_URL`` in Claude Code's project-local
  settings, transactionally, with a verbatim backup that doubles as the record of the
  pre-Zibbo value. Everything else in the file is preserved byte-for-key.
* **Process state** — a PID file so ``stop`` can terminate the gateway *this tool started*,
  and never a gateway someone launched another way.

Provider-agnostic except for the one sanctioned constant: the environment variable Claude
Code reads for its base URL. Nothing else here knows about Anthropic.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from gateway.claude_env import ANTHROPIC_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable

Scope = Literal["user", "project"]

# The single Claude-specific assumption the requirements permit. Kept as one named constant
# so the provider-agnostic boundary is auditable at a glance.
ENV_BASE_URL = "ANTHROPIC_BASE_URL"
DIRECT_ENDPOINT = "https://api.anthropic.com"

_BACKUP_SUFFIX = ".zibbo.bak"
_TMP_SUFFIX = ".zibbo.tmp"


class SettingsError(Exception):
    """A settings file could not be read or written safely. Carries a user-facing message."""


# -- Locating the settings file ----------------------------------------------


def user_settings_target() -> Path:
    """Global Claude Code settings — ``~/.claude/settings.json``.

    Read by both the CLI and the VS Code extension regardless of the working directory or
    which folder the editor has open, so routing set here reaches the extension reliably.
    This is the default onboarding scope.
    """
    return Path.home() / ".claude" / "settings.json"


def project_settings_target(cwd: Path | None = None) -> Path:
    """Project-local Claude Code settings — ``<cwd>/.claude/settings.local.json``.

    The highest-precedence writable layer, but only in effect when the editor's open
    workspace *is* this folder — so it is opt-in (``--project``), not the default.
    """
    cwd = Path.cwd() if cwd is None else cwd
    return cwd / ".claude" / "settings.local.json"


def settings_target(scope: Scope = "user", *, cwd: Path | None = None) -> Path:
    """The settings file for ``scope``: ``user`` (global) or ``project`` (workspace-local)."""
    return user_settings_target() if scope == "user" else project_settings_target(cwd)


def backup_path(target: Path) -> Path:
    return target.with_name(target.name + _BACKUP_SUFFIX)


def desired_base_url(gateway_base_url: str) -> str:
    """The value ``ANTHROPIC_BASE_URL`` must hold to route through this gateway."""
    return gateway_base_url.rstrip("/") + ANTHROPIC_PREFIX


# -- Reading / writing JSON safely -------------------------------------------


def _load(path: Path) -> tuple[dict[str, object], bool]:
    """Return ``(data, existed)``. Missing file -> ``({}, False)``. Invalid JSON raises.

    We never silently discard an unparseable file — clobbering hand-written settings is the
    one thing this module must not do, so a parse failure is surfaced, not swallowed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    stripped = raw.strip()
    if not stripped:
        return {}, True
    try:
        data = json.loads(stripped)
    except ValueError as exc:
        raise SettingsError(
            f"{path} is not valid JSON ({exc}); fix or remove it, then retry"
        ) from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object; refusing to modify it")
    return data, True


def _atomic_write(path: Path, data: dict[str, object]) -> None:
    """Write ``data`` as pretty JSON via a temp file + rename, so a failed write never
    leaves a half-written settings file. Raises ``OSError`` on a read-only target."""
    # A read-only target is refused on every platform. POSIX ``rename`` would replace a
    # read-only *file* regardless (only the directory's write bit matters), so this explicit
    # check is what makes "don't modify a read-only settings file" hold on Linux/macOS too,
    # not just Windows (where ``os.replace`` already fails on a read-only destination).
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"{path} is read-only")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _env_block(data: dict[str, object]) -> dict[str, object]:
    env = data.get("env")
    return dict(env) if isinstance(env, dict) else {}


# -- Backup / state file -----------------------------------------------------
#
# The backup is a small structured record, not a raw copy, so it can represent the one case
# a copy cannot: "there was no settings file before Zibbo." Its *presence* is the single
# source of truth for "routing is active", which is what makes stop idempotent — a second
# stop finds no state file and touches nothing. It still carries the full original document,
# so a human (or a manual recovery) can restore the pre-Zibbo file from ``original``.


def _write_state(path: Path, *, existed: bool, original: dict[str, object] | None) -> None:
    state: dict[str, object] = {"zibbo_state": 1, "original_existed": existed, "original": original}
    _atomic_write(path, state)


def _read_state(path: Path) -> dict[str, object] | None:
    """The Zibbo state at ``path``, or ``None`` if absent/unreadable/not ours."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("zibbo_state") == 1:
        return data
    return None


# -- Configure (start) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigureResult:
    settings_path: Path
    previous_base_url: str | None  # what ANTHROPIC_BASE_URL held before this call, if set
    created_file: bool  # the settings file did not exist and we created it
    backed_up: bool  # a fresh verbatim backup was written this call
    already_routed: bool  # it already pointed at this gateway; nothing changed


def configure_routing(
    base_url: str, *, scope: Scope = "user", target: Path | None = None
) -> ConfigureResult:
    """Point Claude Code at ``base_url`` by merging ``ANTHROPIC_BASE_URL`` into settings.

    Writes the ``user`` (global) settings by default, or ``project`` (workspace-local) when
    asked. Transactional: a verbatim backup is taken before the first modification, and any
    write failure restores the prior on-disk state before raising. Only the one env key is
    touched — every other setting and env var is preserved.
    """
    target = settings_target(scope) if target is None else target
    data, existed = _load(target)  # raises SettingsError on invalid JSON — no change made

    env = _env_block(data)
    previous = env.get(ENV_BASE_URL)
    previous_str = previous if isinstance(previous, str) else None

    if previous_str == base_url:
        # Already routed here. Leave the file (and any existing state) exactly as-is.
        return ConfigureResult(
            target, previous_str, created_file=False, backed_up=False, already_routed=True
        )

    # Record the pre-Zibbo state once. If it already exists (a prior start), keep it: it
    # holds the true original, which re-recording the now-modified file would lose.
    backup = backup_path(target)
    made_backup = False
    if not backup.exists():
        try:
            _write_state(backup, existed=existed, original=data if existed else None)
            made_backup = True
        except OSError as exc:
            raise SettingsError(f"cannot write backup {backup}: {exc}") from exc
    elif _read_state(backup) is None:
        raise SettingsError(f"{backup} exists and was not created by Zibbo; move it aside")

    env[ENV_BASE_URL] = base_url
    new_data = dict(data)
    new_data["env"] = env
    try:
        _atomic_write(target, new_data)
    except OSError as exc:
        # The write is atomic (temp + rename), so a failure leaves ``target`` untouched —
        # rollback only has to drop the state file this call created.
        if made_backup:
            backup.unlink(missing_ok=True)
        raise SettingsError(f"cannot write {target}: {exc}") from exc

    return ConfigureResult(
        target, previous_str, created_file=not existed, backed_up=made_backup, already_routed=False
    )


# -- Restore (stop) ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoreResult:
    settings_path: Path
    restored_to: str | None  # the previous value we put back, if any
    removed: bool  # we removed the Zibbo-added variable (no previous value existed)
    changed: bool  # the file was actually modified


def restore_routing(*, scope: Scope = "user", target: Path | None = None) -> RestoreResult:
    """Reverse :func:`configure_routing`: put back the previous ``ANTHROPIC_BASE_URL`` (read
    from the backup), or remove only the variable Zibbo added. No other setting is touched.

    Surgical on purpose — it edits just the one env key in the *current* file rather than
    restoring the whole backup, so settings the user changed while routing was active survive.
    """
    target = settings_target(scope) if target is None else target
    backup = backup_path(target)
    state = _read_state(backup)
    if state is None:
        # No Zibbo state ⇒ not routed by us (or already restored). Touch nothing.
        return RestoreResult(target, restored_to=None, removed=False, changed=False)

    data, existed = _load(target)  # raises SettingsError on invalid JSON

    # The pre-Zibbo value lives in the recorded original document, if the file existed then.
    previous: str | None = None
    original = state.get("original")
    if isinstance(original, dict):
        recorded = _env_block(original).get(ENV_BASE_URL)
        previous = recorded if isinstance(recorded, str) else None

    changed = False
    env_obj = data.get("env")
    if existed and isinstance(env_obj, dict) and ENV_BASE_URL in env_obj:
        env = dict(env_obj)
        if previous is not None:
            env[ENV_BASE_URL] = previous  # restore the user's own endpoint
        else:
            del env[ENV_BASE_URL]  # remove only the variable Zibbo added
        new_data = dict(data)
        new_data["env"] = env
        try:
            _atomic_write(target, new_data)
        except OSError as exc:
            raise SettingsError(f"cannot write {target}: {exc}") from exc
        changed = True

    # State consumed; removing it keeps the invariant "state exists ⇔ routed by Zibbo".
    backup.unlink(missing_ok=True)
    return RestoreResult(
        target, restored_to=previous, removed=(previous is None and changed), changed=changed
    )


def persisted_base_url(*, scope: Scope = "user", target: Path | None = None) -> str | None:
    """The ``ANTHROPIC_BASE_URL`` written in the ``scope`` settings file, if any.

    Reflects the persisted routing intent (what Claude Code will use next launch). Returns
    ``None`` on a missing/invalid/unset file — status must never raise.
    """
    target = settings_target(scope) if target is None else target
    try:
        data, _ = _load(target)
    except SettingsError:
        return None
    value = _env_block(data).get(ENV_BASE_URL)
    return value if isinstance(value, str) else None


def effective_persisted_base_url() -> str | None:
    """The ``ANTHROPIC_BASE_URL`` Claude Code will actually use, checking both scopes.

    Project-local settings override user settings in Claude Code's precedence, so a value
    there wins; otherwise the global user value applies. Used by ``zibbo status`` so it
    reports routing correctly no matter which scope onboarding used.
    """
    return persisted_base_url(scope="project") or persisted_base_url(scope="user")


# -- Gateway process state (PID file) ----------------------------------------


def pidfile_path() -> Path:
    """Where the PID of a gateway *we started* is recorded. Home-scoped: one gateway per
    user, independent of the project directory ``start`` ran in."""
    return Path.home() / ".zibbo" / "gateway.json"


def startup_log_path() -> Path:
    """Where a gateway *we start* writes its stdout/stderr, so ``zibbo start`` can show a
    startup crash instead of a bare timeout. Truncated on each start; sits by the PID file."""
    return Path.home() / ".zibbo" / "gateway.log"


def write_pidfile(pid: int, base_url: str, *, path: Path | None = None) -> None:
    path = pidfile_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "url": base_url}), encoding="utf-8")
    except OSError:
        pass  # best-effort: a missing PID file only means stop can't auto-kill, never a crash


def read_pidfile(*, path: Path | None = None) -> dict[str, object] | None:
    path = pidfile_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def clear_pidfile(*, path: Path | None = None) -> None:
    path = pidfile_path() if path is None else path
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _terminate(pid: int) -> None:
    """Ask the process to exit. Cross-platform, best-effort — a gone process is not an error."""
    try:
        if os.name == "nt":
            import subprocess

            # taskkill resolves from the Windows System32 PATH; argv is fixed + integer pid.
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(pid), "/F", "/T"],  # noqa: S607
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


@dataclass(frozen=True, slots=True)
class StopResult:
    outcome: str  # "stopped" | "not_owned" | "not_running" | "stop_timeout"
    pid: int | None = None


def stop_gateway(is_reachable: Callable[[], bool], *, path: Path | None = None) -> StopResult:
    """Stop the gateway this tool started, confirming via reachability rather than PID
    liveness (robust across platforms). A gateway with no PID file was started another way
    (uvicorn, docker, another shell) and is deliberately left alone."""
    info = read_pidfile(path=path)
    if info is None:
        # Nothing we own. If something is answering, it isn't ours to kill.
        return StopResult("not_owned" if is_reachable() else "not_running")

    pid = info["pid"]
    assert isinstance(pid, int)  # noqa: S101 — read_pidfile only returns an int pid

    # If nothing is answering, the gateway already stopped. Do NOT signal the recorded PID:
    # the OS may have recycled it for an unrelated process. Drop the stale PID file instead.
    if not is_reachable():
        clear_pidfile(path=path)
        return StopResult("not_running", pid=pid)

    _terminate(pid)
    for _ in range(30):  # up to ~6s for the port to close
        if not is_reachable():
            break
        time.sleep(0.2)
    clear_pidfile(path=path)
    return StopResult("stopped" if not is_reachable() else "stop_timeout", pid=pid)
