"""Finding plugins.

Three sources, in descending precedence:

1. **Explicit configuration** (``ZIBBO_PLUGINS_LOAD``). An operator naming a
   module wins over anything installed, which is what makes an override possible.
2. **A local directory** (``ZIBBO_PLUGINS_DIR``). For development, and for
   deployments that vendor plugins rather than package them.
3. **Python entry points** in the ``zibbo.transformers`` group. The normal
   path: ``pip install zibbo-transformer-csv`` and it is found.

Discovery is **lazy**. A source yields a candidate carrying a *callable* that
imports the plugin, never the plugin itself. Importing third-party code is exactly
where things explode, and it must explode inside the manager's per-plugin error
handling rather than while we are still enumerating.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from gateway.logging import get_logger
from gateway.plugins.errors import PluginLoadError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

logger = get_logger(__name__)

PLUGIN_ATTRIBUTE = "PLUGIN"
"""The module attribute a directory plugin exposes: a ``TransformerPlugin`` instance,
or a zero-argument callable returning one."""

PLUGIN_FACTORY = "get_plugin"


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """A plugin we know how to load but have not loaded."""

    name: str
    """Provisional. The authoritative name is ``plugin.metadata.name``."""

    source: str
    origin: str
    """Where it came from, verbatim, for the operator to grep."""

    load: Callable[[], Any]


class PluginSource(ABC):
    name: ClassVar[str]

    @abstractmethod
    def discover(self) -> Iterator[PluginCandidate]: ...


def _resolve_attribute(module: Any, origin: str) -> Any:
    """Pull the plugin object out of an imported module."""
    for attribute in (PLUGIN_ATTRIBUTE, PLUGIN_FACTORY):
        candidate = getattr(module, attribute, None)
        if candidate is None:
            continue
        return candidate() if callable(candidate) and attribute == PLUGIN_FACTORY else candidate
    raise PluginLoadError(f"{origin} defines neither {PLUGIN_ATTRIBUTE!r} nor {PLUGIN_FACTORY!r}")


class EntryPointSource(PluginSource):
    """Installed packages advertising themselves in ``pyproject.toml``.

    ```toml
    [project.entry-points."zibbo.transformers"]
    csv = "zibbo_transformer_csv:PLUGIN"
    ```
    """

    name: ClassVar[str] = "entry-point"

    def __init__(self, group: str) -> None:
        self._group = group

    def discover(self) -> Iterator[PluginCandidate]:
        try:
            entry_points = importlib.metadata.entry_points(group=self._group)
        except Exception as exc:  # noqa: BLE001 — a broken dist must not stop discovery
            logger.warning("plugin_entry_point_scan_failed", group=self._group, cause=str(exc))
            return

        for entry_point in entry_points:
            yield PluginCandidate(
                name=entry_point.name,
                source=self.name,
                origin=f"{self._group}:{entry_point.name} -> {entry_point.value}",
                load=entry_point.load,
            )


class DirectorySource(PluginSource):
    """``*.py`` files and packages under a directory.

    This executes arbitrary code from the filesystem, so it only runs when an
    operator has explicitly set ``ZIBBO_PLUGINS_DIR``. There is no default.
    """

    name: ClassVar[str] = "directory"

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def discover(self) -> Iterator[PluginCandidate]:
        if not self._directory.is_dir():
            logger.warning("plugin_directory_missing", directory=str(self._directory))
            return

        for path in sorted(self._directory.iterdir()):
            if path.name.startswith(("_", ".")):
                continue
            if path.is_dir() and (path / "__init__.py").exists():
                module_path = path / "__init__.py"
            elif path.suffix == ".py":
                module_path = path
            else:
                continue

            yield PluginCandidate(
                name=path.stem,
                source=self.name,
                origin=str(module_path),
                load=self._loader(path.stem, module_path),
            )

    @staticmethod
    def _loader(stem: str, module_path: Path) -> Callable[[], Any]:
        def load() -> Any:
            module_name = f"zibbo_plugin_{stem}"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                raise PluginLoadError(f"cannot import {module_path}")
            module = importlib.util.module_from_spec(spec)
            # Registered before execution so the module can import itself by name,
            # and removed again if execution fails so a retry is not poisoned.
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            return _resolve_attribute(module, str(module_path))

        return load


class ExplicitSource(PluginSource):
    """Dotted paths from configuration: ``package.module`` or ``package.module:ATTR``."""

    name: ClassVar[str] = "explicit"

    def __init__(self, targets: Sequence[str]) -> None:
        self._targets = tuple(targets)

    def discover(self) -> Iterator[PluginCandidate]:
        for target in self._targets:
            yield PluginCandidate(
                name=target.rsplit(":", 1)[0].rsplit(".", 1)[-1],
                source=self.name,
                origin=target,
                load=self._loader(target),
            )

    @staticmethod
    def _loader(target: str) -> Callable[[], Any]:
        def load() -> Any:
            module_name, _, attribute = target.partition(":")
            module = importlib.import_module(module_name)
            if attribute:
                try:
                    return getattr(module, attribute)
                except AttributeError as exc:
                    raise PluginLoadError(f"{module_name} has no attribute {attribute!r}") from exc
            return _resolve_attribute(module, target)

        return load


def build_sources(
    *,
    entry_point_group: str,
    directory: Path | None,
    explicit: Sequence[str],
) -> tuple[PluginSource, ...]:
    """Sources in precedence order. The first to claim a name keeps it."""
    sources: list[PluginSource] = []
    if explicit:
        sources.append(ExplicitSource(explicit))
    if directory is not None:
        sources.append(DirectorySource(directory))
    if entry_point_group:
        sources.append(EntryPointSource(entry_point_group))
    return tuple(sources)
