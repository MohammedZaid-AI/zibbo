"""The plugin SDK: the short path from a transformer to a plugin.

Writing a plugin should be writing a transformer, plus a description of it. The
gateway needs the metadata *before* it constructs anything — to check the API
version, to enforce capabilities, to decide whether it is disabled — so the two
cannot simply be read off the class at load time.

``simple_plugin`` closes the gap. Declare the facts once, and it stamps ``name``,
``priority`` and ``content_types`` onto the transformer class for you:

    class CsvTransformer(Transformer):
        def transform(self, content, detection): ...

    PLUGIN = simple_plugin(
        transformer=CsvTransformer,
        name="csv",
        version="1.0.0",
        author="you@example.com",
        description="CSV to compact Markdown tables.",
        content_types={ContentType.CSV},
        priority=30,
        sniffers=[CsvSniffer],
    )

That is the whole plugin. For anything needing real work at construction time —
reading ``context.config``, opening a model file — subclass ``TransformerPlugin``
directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from gateway.optimizers.base import Transformer
from gateway.plugins.api import (
    PLUGIN_API_VERSION,
    Capability,
    PluginContext,
    PluginMetadata,
    TransformerPlugin,
)
from gateway.plugins.errors import PluginValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from gateway.optimizers.detector import Sniffer
    from gateway.optimizers.models import ContentType

__all__ = ["simple_plugin"]

SnifferFactory = "Callable[[], Sniffer]"


def _stamp(
    transformer_class: type[Transformer],
    *,
    name: str,
    priority: int,
    content_types: frozenset[ContentType],
) -> None:
    """Fill in the class attributes the metadata already states.

    Only when absent. A transformer that declares its own must agree with the
    metadata, and ``validate_transformer`` will reject it if it does not — better a
    load failure than a registry sorted by one number and documented with another.
    """
    if getattr(transformer_class, "name", None) is None:
        transformer_class.name = name
    if getattr(transformer_class, "priority", None) is None:
        transformer_class.priority = priority
    if getattr(transformer_class, "content_types", None) is None:
        transformer_class.content_types = content_types


def simple_plugin(
    *,
    transformer: type[Transformer],
    name: str,
    version: str,
    author: str,
    description: str,
    content_types: Iterable[ContentType],
    priority: int,
    sniffers: Sequence[type[Sniffer] | Callable[[], Sniffer]] = (),
    capabilities: Iterable[Capability] | None = None,
    api_version: str = PLUGIN_API_VERSION,
    homepage: str | None = None,
    factory: Callable[[PluginContext], Transformer] | None = None,
) -> TransformerPlugin:
    """Build a ``TransformerPlugin`` from a transformer class and its description.

    ``capabilities`` defaults to ``{DETERMINISTIC, IDEMPOTENT}`` — the two the
    gateway requires — plus ``PROVIDES_SNIFFER`` when sniffers are supplied. Pass
    it explicitly to add ``LOSSY`` or ``EXPERIMENTAL``.

    ``factory`` overrides construction when the transformer needs ``context.config``.
    """
    if not isinstance(transformer, type) or not issubclass(transformer, Transformer):
        raise PluginValidationError(f"{transformer!r} is not a Transformer subclass")

    types = frozenset(content_types)
    _stamp(transformer, name=name, priority=priority, content_types=types)

    declared = set(capabilities) if capabilities is not None else set()
    if capabilities is None:
        declared = {Capability.DETERMINISTIC, Capability.IDEMPOTENT}
    if sniffers:
        declared.add(Capability.PROVIDES_SNIFFER)

    plugin_metadata = PluginMetadata(
        name=name,
        version=version,
        author=author,
        description=description,
        content_types=types,
        priority=priority,
        api_version=api_version,
        capabilities=frozenset(declared),
        homepage=homepage,
    )

    class _SimplePlugin(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = plugin_metadata

        def create_transformer(self, context: PluginContext) -> Transformer:
            if factory is not None:
                return factory(context)
            return transformer()

        def create_sniffers(self, context: PluginContext) -> Sequence[Sniffer]:
            del context
            return tuple(_instantiate(sniffer) for sniffer in sniffers)

    _SimplePlugin.__name__ = f"{transformer.__name__}Plugin"
    _SimplePlugin.__qualname__ = _SimplePlugin.__name__
    return _SimplePlugin()


def _instantiate(sniffer: Any) -> Sniffer:
    instance: Sniffer = sniffer() if isinstance(sniffer, type) else sniffer
    return instance
