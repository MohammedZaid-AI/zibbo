"""Loading, validating, and attaching plugins.

The single invariant: **one broken plugin must never prevent the gateway from
starting, and must never affect another plugin.** Every stage — import,
construction, validation, registration — is wrapped per plugin. A failure is
recorded against that plugin and load continues.

``load()`` never raises. Nothing in this module raises into the startup path. If
that is ever not true, a `pip install` of a third-party package can take a
production gateway offline, and the plugin system is a liability rather than a
feature.

The lifecycle is explicit:

    discovered -> loaded -> enabled       (attached to registry and detector)
                     |          ^
                     v          v
                  failed     disabled     (unregistered; may be re-enabled)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from gateway.logging import get_logger
from gateway.plugins.api import (
    PLUGIN_API_VERSION,
    Capability,
    PluginContext,
    PluginMetadata,
    TransformerPlugin,
    check_api_compatibility,
    validate_transformer,
)
from gateway.plugins.errors import (
    DuplicatePluginError,
    PluginCapabilityError,
    PluginLoadError,
    PluginValidationError,
    PluginVersionError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gateway.config import Settings
    from gateway.optimizers.base import Transformer
    from gateway.optimizers.detector import ContentDetector, Sniffer
    from gateway.optimizers.registry import TransformerRegistry
    from gateway.plugins.discovery import PluginSource

logger = get_logger(__name__)


class PluginState(StrEnum):
    LOADED = "loaded"
    """Constructed and validated, but not yet attached to the pipeline."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(slots=True)
class PluginRecord:
    """What became of one plugin. Always present, whatever went wrong."""

    name: str
    source: str
    origin: str
    state: PluginState
    metadata: PluginMetadata | None = None
    transformer: Transformer | None = None
    sniffers: tuple[Sniffer, ...] = ()
    error: str | None = None
    stage: str | None = None
    """Which step failed: ``import``, ``validate``, ``construct``, ``register``."""

    load_time_ms: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "source": self.source,
            "origin": self.origin,
            "error": self.error,
            "stage": self.stage,
            "load_time_ms": self.load_time_ms,
            **(self.metadata.summary() if self.metadata else {}),
        }


@dataclass(frozen=True, slots=True)
class PluginReport:
    """The outcome of a load pass, for logging and introspection."""

    records: tuple[PluginRecord, ...] = ()

    def _named(self, state: PluginState) -> tuple[str, ...]:
        return tuple(r.name for r in self.records if r.state is state)

    @property
    def enabled(self) -> tuple[str, ...]:
        return self._named(PluginState.ENABLED)

    @property
    def loaded(self) -> tuple[str, ...]:
        return self._named(PluginState.LOADED)

    @property
    def disabled(self) -> tuple[str, ...]:
        return self._named(PluginState.DISABLED)

    @property
    def failed(self) -> tuple[PluginRecord, ...]:
        return tuple(r for r in self.records if r.state is PluginState.FAILED)

    @property
    def healthy(self) -> bool:
        return not self.failed


class PluginManager:
    """Owns plugin lifecycle and the registries they attach to."""

    def __init__(
        self,
        sources: Sequence[PluginSource],
        *,
        api_version: str = PLUGIN_API_VERSION,
        disabled: frozenset[str] = frozenset(),
        allow_lossy: bool = False,
        config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._api_version = api_version
        self._disabled = disabled
        self._allow_lossy = allow_lossy
        self._config = config or {}
        self._records: dict[str, PluginRecord] = {}
        self._registry: TransformerRegistry | None = None
        self._detector: ContentDetector | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> PluginManager:
        from gateway.plugins.discovery import build_sources

        if not settings.plugins_enabled:
            return cls(())

        return cls(
            build_sources(
                entry_point_group=settings.plugins_entry_point_group,
                directory=settings.plugins_dir,
                explicit=settings.plugins_load,
            ),
            disabled=frozenset(settings.plugins_disabled),
            allow_lossy=settings.plugins_allow_lossy,
            config=settings.plugins_config,
        )

    # -- Loading -----------------------------------------------------------

    def load(self) -> PluginReport:
        """Discover and construct every plugin. Never raises."""
        for source in self._sources:
            try:
                candidates = list(source.discover())
            except Exception as exc:  # noqa: BLE001 — a broken source is not fatal
                logger.warning("plugin_source_failed", source=source.name, cause=str(exc))
                continue

            for candidate in candidates:
                record = self._load_one(candidate)
                # Keyed on the authoritative name, which is only known after load.
                self._records.setdefault(record.name, record)

        report = self.report()
        self._log(report)
        return report

    def _load_one(self, candidate: Any) -> PluginRecord:
        started = time.perf_counter()
        record = PluginRecord(
            name=candidate.name,
            source=candidate.source,
            origin=candidate.origin,
            state=PluginState.FAILED,
        )

        try:
            plugin = self._import(candidate)
            metadata = self._validate_metadata(plugin, candidate.origin)
            record.name = metadata.name
            record.metadata = metadata

            if metadata.name in self._records:
                raise DuplicatePluginError(
                    f"plugin {metadata.name!r} is already provided by "
                    f"{self._records[metadata.name].origin}"
                )

            transformer, sniffers = self._construct(plugin, metadata)
        except Exception as exc:  # noqa: BLE001 — isolation is the entire point
            record.error = f"{type(exc).__name__}: {exc}"
            record.stage = getattr(exc, "stage", None) or _stage_of(exc)
            record.load_time_ms = _elapsed_ms(started)
            logger.warning(
                "plugin_load_failed",
                plugin=record.name,
                source=record.source,
                origin=record.origin,
                stage=record.stage,
                cause=record.error,
            )
            return record

        record.transformer = transformer
        record.sniffers = tuple(sniffers)
        record.load_time_ms = _elapsed_ms(started)
        record.state = PluginState.DISABLED if record.name in self._disabled else PluginState.LOADED
        return record

    def _import(self, candidate: Any) -> TransformerPlugin:
        try:
            plugin = candidate.load()
        except Exception as exc:
            raise PluginLoadError(f"importing {candidate.origin}: {exc}") from exc

        # A class rather than an instance is a common mistake; accept it.
        if isinstance(plugin, type) and issubclass(plugin, TransformerPlugin):
            plugin = plugin()
        if not isinstance(plugin, TransformerPlugin):
            raise PluginValidationError(
                f"{candidate.origin} is a {type(plugin).__name__}, not a TransformerPlugin"
            )
        return plugin

    def _validate_metadata(self, plugin: TransformerPlugin, origin: str) -> PluginMetadata:
        metadata = getattr(plugin, "metadata", None)
        if not isinstance(metadata, PluginMetadata):
            raise PluginValidationError(f"{origin} has no PluginMetadata")

        check_api_compatibility(metadata.api_version, self._api_version)

        if metadata.is_lossy and not self._allow_lossy:
            raise PluginCapabilityError(
                f"plugin {metadata.name!r} declares itself lossy; set "
                "LLMGATEWAY_PLUGINS_ALLOW_LOSSY=true to permit it"
            )
        return metadata

    def _construct(
        self, plugin: TransformerPlugin, metadata: PluginMetadata
    ) -> tuple[Transformer, Sequence[Sniffer]]:
        context = PluginContext(
            api_version=self._api_version,
            config=self._config.get(metadata.name, {}),
        )
        transformer = plugin.create_transformer(context)
        validate_transformer(metadata, transformer)

        sniffers = tuple(plugin.create_sniffers(context))
        if sniffers and Capability.PROVIDES_SNIFFER not in metadata.capabilities:
            raise PluginValidationError(
                f"plugin {metadata.name!r} returns sniffers but does not declare "
                "the 'provides_sniffer' capability"
            )
        return transformer, sniffers

    # -- Attaching ---------------------------------------------------------

    def attach(self, registry: TransformerRegistry, detector: ContentDetector) -> PluginReport:
        """Register every loaded plugin. A registration failure isolates too."""
        self._registry = registry
        self._detector = detector

        for record in self._records.values():
            if record.state is PluginState.LOADED:
                self._register(record)

        report = self.report()
        self._log(report)
        return report

    def _register(self, record: PluginRecord) -> None:
        if self._registry is None or self._detector is None:
            raise PluginLoadError("plugin manager is not attached")
        if record.transformer is None:
            return

        # Roll back only what *this* call actually registered. Unregistering by name
        # would be catastrophic on the commonest failure: a plugin colliding with a
        # builtin would delete the builtin on its way out.
        transformer_registered = False
        sniffers_registered: list[str] = []

        try:
            self._registry.register(record.transformer)
            transformer_registered = True
            for sniffer in record.sniffers:
                self._detector.add_sniffer(sniffer)
                sniffers_registered.append(sniffer.name)
        except Exception as exc:  # noqa: BLE001
            if transformer_registered:
                self._registry.unregister(record.name)
            for name in sniffers_registered:
                self._detector.remove_sniffer(name)

            record.state = PluginState.FAILED
            record.stage = "register"
            record.error = f"{type(exc).__name__}: {exc}"
            logger.warning("plugin_register_failed", plugin=record.name, cause=record.error)
            return

        record.state = PluginState.ENABLED

    def _unregister(self, record: PluginRecord) -> None:
        if self._registry is None or self._detector is None:
            return
        self._registry.unregister(record.name)
        for sniffer in record.sniffers:
            self._detector.remove_sniffer(sniffer.name)

    # -- Lifecycle ---------------------------------------------------------

    def enable(self, name: str) -> PluginRecord:
        record = self._get(name)
        if record.state is PluginState.ENABLED:
            return record
        if record.state is PluginState.FAILED:
            raise PluginLoadError(f"plugin {name!r} failed to load and cannot be enabled")
        record.state = PluginState.LOADED
        self._register(record)
        logger.info("plugin_enabled", plugin=name, state=record.state.value)
        return record

    def disable(self, name: str) -> PluginRecord:
        record = self._get(name)
        if record.state is PluginState.ENABLED:
            self._unregister(record)
        record.state = PluginState.DISABLED
        logger.info("plugin_disabled", plugin=name)
        return record

    def unload(self, name: str) -> None:
        """Detach and forget. Reloading requires a fresh ``load()``."""
        record = self._get(name)
        if record.state is PluginState.ENABLED:
            self._unregister(record)
        del self._records[name]
        logger.info("plugin_unloaded", plugin=name)

    # -- Introspection -----------------------------------------------------

    def _get(self, name: str) -> PluginRecord:
        try:
            return self._records[name]
        except KeyError as exc:
            raise PluginLoadError(f"plugin {name!r} is not known") from exc

    def report(self) -> PluginReport:
        return PluginReport(tuple(self._records.values()))

    @property
    def records(self) -> tuple[PluginRecord, ...]:
        return tuple(self._records.values())

    def _log(self, report: PluginReport) -> None:
        for record in report.records:
            if record.metadata and record.metadata.is_experimental:
                logger.warning("plugin_experimental", plugin=record.name)
        logger.info(
            "plugins_loaded",
            enabled=report.enabled,
            disabled=report.disabled,
            failed=tuple(record.name for record in report.failed),
            total=len(report.records),
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _stage_of(exc: Exception) -> str:
    if isinstance(exc, PluginLoadError):
        return "import"
    if isinstance(exc, DuplicatePluginError):
        return "duplicate"
    if isinstance(exc, PluginVersionError):
        return "version"
    if isinstance(exc, PluginCapabilityError):
        return "capability"
    if isinstance(exc, PluginValidationError):
        return "validate"
    return "construct"
