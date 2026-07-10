"""Plugin failures.

Every one of these is caught by the manager and recorded against the offending
plugin. None of them ever reaches the application startup path: a gateway that
refuses to boot because a third-party package raised on import is a gateway that
hands its availability to code it does not control.
"""

from __future__ import annotations

from gateway.errors import GatewayError


class PluginError(GatewayError):
    """Base class for anything a plugin can do wrong."""

    code = "plugin_error"


class PluginLoadError(PluginError):
    """The plugin could not be imported, constructed, or instantiated."""

    code = "plugin_load_error"


class PluginValidationError(PluginError):
    """The plugin loaded but does not satisfy the plugin contract."""

    code = "plugin_validation_error"


class PluginVersionError(PluginError):
    """The plugin targets an incompatible gateway plugin API."""

    code = "plugin_version_error"


class DuplicatePluginError(PluginError):
    """Two plugins claim the same name. The first one found wins."""

    code = "duplicate_plugin"


class PluginCapabilityError(PluginError):
    """The plugin declares a capability this deployment does not permit."""

    code = "plugin_capability_error"
