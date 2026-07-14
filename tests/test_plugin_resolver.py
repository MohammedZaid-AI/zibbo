"""The bin/zibbo resolver shim — the shared helper that lets the /zibbo command and the
SessionStart hook find the CLI without a global ``zibbo`` on PATH.

Each resolution step is exercised by putting fake executables on a controlled PATH and
asserting which one the shim ultimately runs. The shim is POSIX ``sh``; these tests need a
``sh`` (Git Bash on Windows), and skip cleanly when one is not available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "plugins" / "claude-code" / "bin" / "zibbo"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh available")

# Enough of the real system for grep/dirname; the fakes we add always sort first.
_SYSTEM_PATH = "/usr/bin:/bin"


def _fake(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/usr/bin/env sh\n" + body, encoding="utf-8", newline="\n")
    script.chmod(0o755)


def _run(shim: Path, path: str, *args: str) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    return subprocess.run(  # noqa: S603 — fixed sh + repo shim path, test-only
        [SH, str(shim), *args],
        env={"PATH": path},
        capture_output=True,
        text=True,
        check=False,
    )


# -- Resolution order --------------------------------------------------------


def test_1_prefers_a_real_zibbo_on_path(tmp_path: Path) -> None:
    real = tmp_path / "bin"
    _fake(real, "zibbo", 'echo "USED:real $*"\n')  # no shim marker -> a "real" zibbo
    result = _run(SHIM, f"{real}:{_SYSTEM_PATH}", "status", "x")
    assert result.stdout.strip() == "USED:real status x"


def test_2_falls_back_to_py_module(tmp_path: Path) -> None:
    binn = tmp_path / "bin"
    # `py -c "import gateway"` succeeds; `py -m gateway.cli ...` echoes a marker.
    _fake(binn, "py", 'if [ "$1" = "-c" ]; then exit 0; fi\necho "USED:py $*"\n')
    result = _run(SHIM, f"{binn}:{_SYSTEM_PATH}", "stats")
    assert result.stdout.strip() == "USED:py -m gateway.cli stats"


def test_3_falls_back_to_python_module(tmp_path: Path) -> None:
    binn = tmp_path / "bin"
    _fake(binn, "python", 'if [ "$1" = "-c" ]; then exit 0; fi\necho "USED:python $*"\n')
    result = _run(SHIM, f"{binn}:{_SYSTEM_PATH}", "doctor")
    assert result.stdout.strip() == "USED:python -m gateway.cli doctor"


def test_4_falls_back_to_python3_module(tmp_path: Path) -> None:
    binn = tmp_path / "bin"
    _fake(binn, "python3", 'if [ "$1" = "-c" ]; then exit 0; fi\necho "USED:python3 $*"\n')
    result = _run(SHIM, f"{binn}:{_SYSTEM_PATH}", "logs")
    assert result.stdout.strip() == "USED:python3 -m gateway.cli logs"


def test_5_uses_repo_checkout_when_python_lacks_gateway(tmp_path: Path) -> None:
    # The real repo shim: <repo>/plugins/claude-code/bin/zibbo, with <repo>/gateway present.
    # A python that CANNOT import gateway is skipped by steps 2-4, then run under step 5 with
    # PYTHONPATH pointed at the repo.
    binn = tmp_path / "bin"
    _fake(
        binn,
        "python",
        'if [ "$1" = "-c" ]; then exit 1; fi\necho "USED:repo PYTHONPATH=$PYTHONPATH"\n',
    )
    result = _run(SHIM, f"{binn}:{_SYSTEM_PATH}", "status")
    assert result.stdout.startswith("USED:repo PYTHONPATH=")
    # PYTHONPATH points at the repo (path is POSIX-formatted under Git Bash, so match the
    # distinctive trailing directory rather than the native Windows spelling).
    assert REPO_ROOT.name in result.stdout


def test_6_prints_install_instructions_when_nothing_resolves(tmp_path: Path) -> None:
    # Copy the shim somewhere with no repo above it and no interpreter on PATH.
    lone = tmp_path / "isolated" / "zibbo"
    lone.parent.mkdir(parents=True)
    shutil.copy2(SHIM, lone)
    tools = tmp_path / "tools"  # only grep/dirname, no zibbo/py/python
    for real_tool in ("grep", "dirname", "cd"):
        found = shutil.which(real_tool)
        if found:
            _fake(tools, real_tool, f'exec "{found}" "$@"\n')
    result = _run(lone, f"{tools}:{_SYSTEM_PATH}", "status")
    assert result.returncode == 127
    assert "Zibbo CLI not found" in result.stderr


def test_shim_is_self_excluding_by_marker() -> None:
    # The recursion guard: the shim carries a marker so the PATH search never selects a shim.
    assert "ZIBBO_RESOLVER_SHIM" in SHIM.read_text(encoding="utf-8")
