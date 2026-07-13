"""Developer tooling for the Claude Code plugin — locate, verify, and refresh the installed
copy so it matches this repository. Stdlib only, so ``zibbo plugin`` runs anywhere.

Why this exists (verified against the Claude Code plugin docs):

    Claude Code *copies* a marketplace plugin into a per-version cache at
    ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`` and uses the plugin's
    ``version`` as the cache key — "users only receive updates when you bump this field …
    Pushing new commits without bumping it has no effect." So a fixed version plus edited
    files leaves the *installed* copy stale while the repository is correct. That mismatch
    is exactly what shipped the old ``!`command -v zibbo … ${ARGUMENTS:-status}` `` to the
    runtime after the repo had already been fixed.

Two supported ways out, both surfaced by ``zibbo plugin``:
    * Development: ``claude --plugin-dir <repo>/plugins/claude-code`` overrides the installed
      copy for the session (``zibbo plugin dev`` prints this). No cache, no version bump.
    * Release: bump ``version`` and ``/plugin update`` (``zibbo plugin sync`` refreshes the
      already-installed cache content in place so you can test without a full reinstall).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

PLUGIN_NAME = "zibbo"

# The command file must use the documented argument mechanism and nothing the permission
# checker rejects. These are the exact contracts the runtime is verified against.
REQUIRED_COMMAND_SNIPPET = "!`zibbo $ARGUMENTS`"
FORBIDDEN_COMMAND_SNIPPETS = (
    "command -v",
    "${ARGUMENTS",
    "${CLAUDE_PLUGIN_ROOT}",
    "&&",
    "||",
    "$(",
)
# Shell metacharacters that must never appear in the exec-form hook invocation.
FORBIDDEN_HOOK_SNIPPETS = ("&&", "||", "${", "$(", "command -v", 'sh "')

# Files whose content is executed by Claude Code — what ``sync`` refreshes. Note
# ``.claude-plugin/plugin.json`` is intentionally excluded so a content refresh never
# rewrites the cache directory's version label.
SYNC_PATHS = ("commands", "hooks", "README.md")


# -- Locating the repository plugin and the installed copies ------------------


def repo_plugin_dir(explicit: str | None = None) -> Path:
    """The repository's ``plugins/claude-code`` directory.

    ``explicit`` (``--plugin-dir``) wins. Otherwise derive it from this package's location,
    which works in an editable checkout. Raises if it cannot be found.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
    else:
        # gateway/plugin_dev.py -> gateway -> repo root -> plugins/claude-code
        path = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
    if not (path / ".claude-plugin" / "plugin.json").is_file():
        raise FileNotFoundError(
            f"no plugin manifest under {path}. Run from a Zibbo checkout, "
            "or pass --plugin-dir <path to plugins/claude-code>."
        )
    return path


def claude_plugins_home() -> Path:
    return Path.home() / ".claude" / "plugins"


def installed_cache_dirs(name: str = PLUGIN_NAME) -> list[Path]:
    """Every per-version cache directory Claude Code has for ``name``, newest path last."""
    root = claude_plugins_home() / "cache" / name / name
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def marketplace_clone_dir(name: str = PLUGIN_NAME) -> Path | None:
    """The marketplace git clone's plugin dir, if present."""
    candidate = claude_plugins_home() / "marketplaces" / name / "plugins" / "claude-code"
    return candidate if candidate.is_dir() else None


def read_plugin_version(plugin_dir: Path) -> str | None:
    """Read ``version`` from a plugin directory's manifest, or None if unreadable."""
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


# -- Verification (fails loudly on stale content) ----------------------------


def verify_command_text(text: str) -> list[str]:
    """Problems with a ``commands/zibbo.md``: missing the required form, or any forbidden
    shell expansion the Claude Code permission checker would reject."""
    problems: list[str] = []
    if REQUIRED_COMMAND_SNIPPET not in text:
        problems.append(f"commands/zibbo.md is missing {REQUIRED_COMMAND_SNIPPET!r}")
    for bad in FORBIDDEN_COMMAND_SNIPPETS:
        if bad in text:
            problems.append(f"commands/zibbo.md contains forbidden shell syntax {bad!r}")
    return problems


def verify_hooks_text(text: str) -> list[str]:
    """Problems with a ``hooks/hooks.json``: not exec form, or containing shell syntax."""
    problems: list[str] = []
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"hooks/hooks.json is not valid JSON: {exc}"]
    try:
        entry = data["hooks"]["SessionStart"][0]["hooks"][0]
    except (KeyError, IndexError, TypeError):
        return ["hooks/hooks.json has no SessionStart command hook"]
    if entry.get("type") != "command":
        problems.append("hooks SessionStart entry is not a command hook")
    if entry.get("command") != "zibbo":
        problems.append('hooks command must be the exec-form executable "zibbo"')
    if not isinstance(entry.get("args"), list):
        problems.append('hooks must pass an "args" vector (exec form), not a shell string')
    blob = json.dumps(data)
    for bad in FORBIDDEN_HOOK_SNIPPETS:
        if bad in blob:
            problems.append(f"hooks/hooks.json contains forbidden shell syntax {bad!r}")
    return problems


def verify_plugin_dir(plugin_dir: Path) -> list[str]:
    """Every problem with a plugin directory's executed files. Empty means clean."""
    problems: list[str] = []
    command = plugin_dir / "commands" / "zibbo.md"
    hooks = plugin_dir / "hooks" / "hooks.json"
    if not command.is_file():
        problems.append("missing commands/zibbo.md")
    else:
        problems += verify_command_text(command.read_text(encoding="utf-8"))
    if not hooks.is_file():
        problems.append("missing hooks/hooks.json")
    else:
        problems += verify_hooks_text(hooks.read_text(encoding="utf-8"))
    return problems


# -- Version bump and content sync -------------------------------------------


def set_plugin_version(plugin_dir: Path, marketplace_json: Path, version: str) -> None:
    """Bump the plugin version in both manifests, keeping them in lockstep.

    Claude Code keys its cache on this field, so a release must change it here (plugin
    manifest) and in the marketplace entry that advertises it.
    """
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = version
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")

    market = json.loads(marketplace_json.read_text(encoding="utf-8"))
    if isinstance(market.get("metadata"), dict):
        market["metadata"]["version"] = version
    for plugin in market.get("plugins", []):
        if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
            plugin["version"] = version
    marketplace_json.write_text(json.dumps(market, indent=2) + "\n", encoding="utf-8", newline="\n")


def sync_plugin(repo_dir: Path, target_dir: Path) -> list[str]:
    """Copy the executed files (commands/, hooks/, README) from repo into an installed copy.

    Refreshes content in place so the running plugin matches the repository without a full
    reinstall. The manifest is left untouched, so the cache directory keeps its version
    label. Returns the list of relative paths written.
    """
    written: list[str] = []
    for name in SYNC_PATHS:
        source = repo_dir / name
        if not source.exists():
            continue
        destination = target_dir / name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        written.append(name)
    return written
