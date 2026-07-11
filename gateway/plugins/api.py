"""The plugin API. This module is the contract with plugin authors.

Everything a plugin needs is importable from ``gateway.plugins``. Nothing else in
the gateway is public to a plugin, and the gateway never imports a plugin — it is
handed one by a discovery source.

Two design decisions are worth reading before writing a plugin.

**Capabilities are enforced, not documented.** A transformer that is not
deterministic and idempotent would poison the response cache and make a replayed
request differ from the original. The manager therefore *refuses to load* a plugin
that does not declare both. ``LOSSY`` is refused unless the deployment opts in.
Declaring a capability you do not have is the one lie the gateway cannot catch.

**A plugin may contribute a sniffer.** A CSV transformer without CSV detection
never runs: the detector would classify the content as text and route it elsewhere.
So a plugin ships the sniffer that finds its content type alongside the transformer
that handles it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Final

from gateway.plugins.errors import PluginValidationError, PluginVersionError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from gateway.optimizers.base import Transformer
    from gateway.optimizers.detector import Sniffer
    from gateway.optimizers.models import ContentType

PLUGIN_API_VERSION: Final = "1.0"
"""The plugin API version this gateway implements.

Compatibility rule, deliberately simple enough to reason about without a
dependency: a plugin is compatible when its **major** version equals the gateway's
and its **minor** version is less than or equal to the gateway's. A plugin written
against 1.0 runs on a 1.3 gateway; one written against 1.4 does not, because it may
use API the gateway lacks. A 2.x plugin never runs on a 1.x gateway.
"""

ENTRY_POINT_GROUP: Final = "zibbo.transformers"

_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE: Final = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$")
_API_VERSION_RE: Final = re.compile(r"^(\d+)\.(\d+)$")

MIN_PRIORITY: Final = 0
MAX_PRIORITY: Final = 1000


class Capability(StrEnum):
    """What a plugin promises about its transformer.

    The first two are mandatory. The pipeline's correctness, the Phase 8 cache, and
    the idempotency guarantee all rest on them.
    """

    DETERMINISTIC = "deterministic"
    """The same input always yields the same output, in every process."""

    IDEMPOTENT = "idempotent"
    """``T(T(x)) == T(x)`` whenever the transformer can handle its own output."""

    LOSSY = "lossy"
    """Removes information a caller might miss. Refused unless the deployment
    sets ``ZIBBO_PLUGINS_ALLOW_LOSSY=true``."""

    EXPERIMENTAL = "experimental"
    """Loads normally, but is logged at startup so operators know it is there."""

    PROVIDES_SNIFFER = "provides_sniffer"
    """Contributes content detection as well as transformation."""


REQUIRED_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {Capability.DETERMINISTIC, Capability.IDEMPOTENT}
)


def parse_api_version(value: str) -> tuple[int, int]:
    match = _API_VERSION_RE.match(value)
    if match is None:
        raise PluginVersionError(f"api_version {value!r} is not 'major.minor'")
    return int(match.group(1)), int(match.group(2))


def check_api_compatibility(plugin_api_version: str, gateway_api_version: str) -> None:
    """Raise :class:`PluginVersionError` if the plugin cannot run on this gateway."""
    plugin_major, plugin_minor = parse_api_version(plugin_api_version)
    gateway_major, gateway_minor = parse_api_version(gateway_api_version)

    if plugin_major != gateway_major:
        raise PluginVersionError(
            f"plugin targets API {plugin_api_version}, gateway implements "
            f"{gateway_api_version}: major versions must match"
        )
    if plugin_minor > gateway_minor:
        raise PluginVersionError(
            f"plugin targets API {plugin_api_version}, gateway implements "
            f"{gateway_api_version}: the gateway is too old for this plugin"
        )


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Everything the gateway knows about a plugin before it runs any of its code."""

    name: str
    version: str
    author: str
    description: str
    content_types: frozenset[ContentType]
    priority: int
    api_version: str = PLUGIN_API_VERSION
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset(REQUIRED_CAPABILITIES)
    )
    homepage: str | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PluginValidationError(
                f"plugin name {self.name!r} must be lowercase alphanumeric "
                "with '.', '_' or '-' (max 64 characters)"
            )
        if not _VERSION_RE.match(self.version):
            raise PluginValidationError(f"plugin version {self.version!r} is not semver-like")
        if not self.content_types:
            raise PluginValidationError(f"plugin {self.name!r} declares no content types")
        if not MIN_PRIORITY <= self.priority <= MAX_PRIORITY:
            raise PluginValidationError(
                f"plugin {self.name!r} priority {self.priority} is outside "
                f"[{MIN_PRIORITY}, {MAX_PRIORITY}]"
            )
        parse_api_version(self.api_version)

        missing = REQUIRED_CAPABILITIES - self.capabilities
        if missing:
            declared = ", ".join(sorted(capability.value for capability in missing))
            raise PluginValidationError(
                f"plugin {self.name!r} must declare {declared}: the pipeline's cache "
                "and idempotency guarantees depend on it"
            )

    @property
    def is_lossy(self) -> bool:
        return Capability.LOSSY in self.capabilities

    @property
    def is_experimental(self) -> bool:
        return Capability.EXPERIMENTAL in self.capabilities

    def summary(self) -> dict[str, Any]:
        """Metadata as plain data, for logs and the introspection endpoint."""
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "content_types": sorted(content.value for content in self.content_types),
            "priority": self.priority,
            "api_version": self.api_version,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "homepage": self.homepage,
        }


@dataclass(frozen=True, slots=True)
class PluginContext:
    """What the gateway hands a plugin at construction time.

    Deliberately not ``Settings``. A plugin gets its own configuration and the API
    version it is running against, and nothing else — it cannot read the gateway's
    credentials or reach into its internals.
    """

    api_version: str
    config: Mapping[str, Any]


class TransformerPlugin(ABC):
    """The base class every plugin implements.

    See ``gateway.plugins.sdk.simple_plugin`` for the zero-boilerplate path; this
    class is for plugins that need to do real work at construction time.
    """

    metadata: ClassVar[PluginMetadata]

    @abstractmethod
    def create_transformer(self, context: PluginContext) -> Transformer:
        """Build the transformer. Called once, at load time.

        Raising here fails this plugin and no other.
        """

    def create_sniffers(self, context: PluginContext) -> Sequence[Sniffer]:
        """Content detectors this plugin contributes. Usually one, often none.

        A transformer for a content type the detector cannot recognize will never
        run, so a plugin introducing a new ``ContentType`` must ship its sniffer.
        """
        del context
        return ()


def validate_transformer(metadata: PluginMetadata, transformer: Transformer) -> None:
    """The transformer must agree with the metadata that advertised it.

    Otherwise the registry would sort by one priority while the operator read
    another, and ``/internal/plugins`` would describe a transformer that does not
    exist.
    """
    if transformer.name != metadata.name:
        raise PluginValidationError(
            f"plugin {metadata.name!r} produced a transformer named {transformer.name!r}"
        )
    if transformer.priority != metadata.priority:
        raise PluginValidationError(
            f"plugin {metadata.name!r} declares priority {metadata.priority} but its "
            f"transformer uses {transformer.priority}"
        )
    if set(transformer.content_types) != set(metadata.content_types):
        raise PluginValidationError(
            f"plugin {metadata.name!r} declares content types "
            f"{sorted(c.value for c in metadata.content_types)} but its transformer "
            f"handles {sorted(c.value for c in transformer.content_types)}"
        )
