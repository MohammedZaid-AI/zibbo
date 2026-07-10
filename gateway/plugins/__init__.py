"""The plugin system.

A plugin author imports from **this module and nothing else**. The gateway, in
turn, never imports a plugin: it is handed one by a discovery source, constructs
it behind per-plugin error handling, and registers the result.

    from gateway.plugins import Capability, ContentType, Transformer, simple_plugin

Read ``docs/PLUGIN_DEVELOPMENT.md`` before writing one.
"""

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, Detection, TransformOutput
from gateway.plugins.api import (
    ENTRY_POINT_GROUP,
    PLUGIN_API_VERSION,
    REQUIRED_CAPABILITIES,
    Capability,
    PluginContext,
    PluginMetadata,
    TransformerPlugin,
    check_api_compatibility,
)
from gateway.plugins.discovery import (
    DirectorySource,
    EntryPointSource,
    ExplicitSource,
    PluginCandidate,
    PluginSource,
    build_sources,
)
from gateway.plugins.errors import (
    DuplicatePluginError,
    PluginCapabilityError,
    PluginError,
    PluginLoadError,
    PluginValidationError,
    PluginVersionError,
)
from gateway.plugins.manager import PluginManager, PluginRecord, PluginReport, PluginState
from gateway.plugins.sdk import simple_plugin

__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_API_VERSION",
    "REQUIRED_CAPABILITIES",
    "Capability",
    "ContentType",
    "Detection",
    "DirectorySource",
    "DuplicatePluginError",
    "EntryPointSource",
    "ExplicitSource",
    "PluginCandidate",
    "PluginCapabilityError",
    "PluginContext",
    "PluginError",
    "PluginLoadError",
    "PluginManager",
    "PluginMetadata",
    "PluginRecord",
    "PluginReport",
    "PluginSource",
    "PluginState",
    "PluginValidationError",
    "PluginVersionError",
    "TransformOutput",
    "Transformer",
    "TransformerPlugin",
    "build_sources",
    "check_api_compatibility",
    "simple_plugin",
]
