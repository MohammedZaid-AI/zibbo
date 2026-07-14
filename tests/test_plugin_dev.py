"""Developer tooling for the Claude Code plugin: version sync, and the guarantee that the
executed files never contain the shell expansion Claude Code's permission checker rejects.

The bug these guard against: the repository was fixed, but the installed cache kept running
the old ``!`command -v zibbo … ${ARGUMENTS:-status}` `` because the plugin ``version`` was
never bumped and the cache is version-keyed.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway import plugin_dev

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "claude-code"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# The exact stale files that shipped to the runtime before the fix.
_OLD_COMMAND = "!`command -v zibbo >/dev/null 2>&1 && zibbo ${ARGUMENTS:-status} 2>&1 || echo x`\n"
_OLD_HOOKS = json.dumps(
    {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "command -v zibbo && zibbo start || echo x",
                            "timeout": 15,
                        }
                    ]
                }
            ]
        }
    }
)


# -- The repository is clean and versioned -----------------------------------


def test_repo_plugin_dir_resolves_and_is_versioned() -> None:
    resolved = plugin_dev.repo_plugin_dir()
    assert resolved == PLUGIN_DIR
    version = plugin_dev.read_plugin_version(resolved)
    assert version is not None
    assert version.count(".") >= 1  # a semantic-ish version string


def test_repo_plugin_files_contain_no_shell_expansion() -> None:
    assert plugin_dev.verify_plugin_dir(PLUGIN_DIR) == []


def test_per_action_command_files_exist_and_are_clean() -> None:
    # Claude Code namespaces plugin commands as /zibbo:<file>, so each action ships as its
    # own command file (/zibbo:stats, /zibbo:doctor, …) — discoverable, none to memorize.
    commands = PLUGIN_DIR / "commands"
    expected = {
        "stats",
        "explain",
        "doctor",
        "benchmark",
        "logs",
        "enable",
        "disable",
        "claude",
        "start",
    }
    present = {path.stem for path in commands.glob("*.md")}
    assert expected <= present, f"missing per-action commands: {expected - present}"
    for action in expected:
        text = (commands / f"{action}.md").read_text(encoding="utf-8")
        assert f"!`zibbo {action} $ARGUMENTS`" in text  # wired to the right subcommand
        for bad in plugin_dev.FORBIDDEN_COMMAND_SNIPPETS:
            assert bad not in text  # no expansion the permission checker would reject


def test_manifests_agree_on_version() -> None:
    """A version bump must touch both manifests, or the marketplace advertises a stale one."""
    plugin_version = plugin_dev.read_plugin_version(PLUGIN_DIR)
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "zibbo")
    assert entry["version"] == plugin_version
    assert market["metadata"]["version"] == plugin_version


# -- Verification catches stale content --------------------------------------


def test_verify_command_text_flags_old_shell_expansion() -> None:
    problems = plugin_dev.verify_command_text(_OLD_COMMAND)
    joined = " ".join(problems)
    assert "command -v" in joined
    assert "${ARGUMENTS" in joined
    assert "&&" in joined
    assert "||" in joined
    assert "missing" in joined  # required !`zibbo $ARGUMENTS` absent


def test_verify_command_text_accepts_the_fixed_form() -> None:
    good = "## Zibbo\n\n!`zibbo $ARGUMENTS`\n\nPresent verbatim.\n"
    assert plugin_dev.verify_command_text(good) == []


def test_verify_hooks_text_flags_shell_form() -> None:
    problems = plugin_dev.verify_hooks_text(_OLD_HOOKS)
    joined = " ".join(problems)
    assert "exec-form" in joined  # command must be "zibbo"
    assert "args" in joined  # must pass an args vector
    assert "command -v" in joined


def test_verify_hooks_text_accepts_exec_form() -> None:
    good = json.dumps(
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "zibbo", "args": ["banner"]}]}
                ]
            }
        }
    )
    assert plugin_dev.verify_hooks_text(good) == []


# -- Version bump and sync ---------------------------------------------------


def test_set_plugin_version_bumps_both_manifests(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "zibbo", "version": "0.1.0"}), encoding="utf-8"
    )
    market = tmp_path / "marketplace.json"
    market.write_text(
        json.dumps(
            {
                "metadata": {"version": "0.1.0"},
                "plugins": [{"name": "zibbo", "version": "0.1.0"}],
            }
        ),
        encoding="utf-8",
    )

    plugin_dev.set_plugin_version(plugin_dir, market, "0.2.0")

    assert plugin_dev.read_plugin_version(plugin_dir) == "0.2.0"
    updated = json.loads(market.read_text(encoding="utf-8"))
    assert updated["metadata"]["version"] == "0.2.0"
    assert updated["plugins"][0]["version"] == "0.2.0"


def test_sync_refreshes_content_but_preserves_version_label(tmp_path: Path) -> None:
    # An "installed" copy with the old stale command and an older version label.
    installed = tmp_path / "cache" / "0.1.0"
    (installed / "commands").mkdir(parents=True)
    (installed / ".claude-plugin").mkdir(parents=True)
    (installed / "commands" / "zibbo.md").write_text(_OLD_COMMAND, encoding="utf-8")
    (installed / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "zibbo", "version": "0.1.0"}), encoding="utf-8"
    )
    assert plugin_dev.verify_plugin_dir(installed) != []  # stale before

    written = plugin_dev.sync_plugin(PLUGIN_DIR, installed)

    assert "commands" in written and "hooks" in written
    assert plugin_dev.verify_plugin_dir(installed) == []  # clean after
    # sync refreshes executed content, not the cache directory's version label.
    assert plugin_dev.read_plugin_version(installed) == "0.1.0"
