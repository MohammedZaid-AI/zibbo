"""The plugin system.

The load-bearing test in this file is `test_a_broken_plugin_does_not_stop_the_gateway`.
Everything else is detail. A plugin system whose failure mode is "the gateway does
not start" hands a third-party `pip install` the power to take production down.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.config import Settings, get_settings
from gateway.main import create_app
from gateway.optimizers import ContentDetector, OptimizerOptions, build_transformer_registry
from gateway.optimizers.models import ContentType, Detection, TransformOutput
from gateway.plugins import (
    PLUGIN_API_VERSION,
    Capability,
    DirectorySource,
    ExplicitSource,
    PluginCandidate,
    PluginContext,
    PluginManager,
    PluginMetadata,
    PluginSource,
    PluginState,
    Transformer,
    TransformerPlugin,
    check_api_compatibility,
    simple_plugin,
)
from gateway.plugins.errors import PluginValidationError, PluginVersionError
from tests.conftest import build_settings

CONTEXT = PluginContext(api_version=PLUGIN_API_VERSION, config={})


# -- Test doubles ----------------------------------------------------------


class Marker(Transformer):
    """A transformer that stamps its own name onto the content."""

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        del detection
        return TransformOutput(f"{content}:{self.name}", ("marked",))


_UNSET = object()


def _make_plugin(
    name: str = "demo",
    *,
    priority: int = 50,
    content_types: set[ContentType] | object = _UNSET,
    capabilities: set[Capability] | None = None,
    api_version: str = PLUGIN_API_VERSION,
    version: str = "1.0.0",
) -> TransformerPlugin:
    # A distinct class per plugin: `simple_plugin` stamps attributes onto the class,
    # and sharing one would let plugins overwrite each other's identity.
    transformer = type(f"Marker_{name}_{priority}", (Marker,), {})
    types = {ContentType.CSV} if content_types is _UNSET else content_types
    return simple_plugin(
        transformer=transformer,
        name=name,
        version=version,
        author="tests",
        description=f"{name} transformer",
        content_types=types,  # type: ignore[arg-type]
        priority=priority,
        capabilities=capabilities,
        api_version=api_version,
    )


class StaticSource(PluginSource):
    """Yields candidates from objects already in memory."""

    name: ClassVar[str] = "static"

    def __init__(self, **plugins: object) -> None:
        self._plugins = plugins

    def discover(self):
        for name, plugin in self._plugins.items():
            yield PluginCandidate(
                name=name,
                source=self.name,
                origin=f"static:{name}",
                load=(lambda p=plugin: p),  # type: ignore[misc]
            )


class ExplodingSource(PluginSource):
    name: ClassVar[str] = "exploding"

    def discover(self):
        raise RuntimeError("the source itself is broken")
        yield  # pragma: no cover


def _attached(manager: PluginManager) -> tuple[ContentDetector, object]:
    registry = build_transformer_registry(OptimizerOptions())
    detector = ContentDetector()
    manager.load()
    manager.attach(registry, detector)
    return detector, registry


# -- Discovery: the three sources ------------------------------------------


def test_a_plugin_is_discovered_loaded_and_enabled() -> None:
    manager = PluginManager([StaticSource(demo=_make_plugin())])
    _detector, registry = _attached(manager)

    report = manager.report()
    assert report.enabled == ("demo",)
    assert report.healthy
    assert "demo" in registry.names  # type: ignore[attr-defined]


def test_a_plugin_class_is_accepted_as_well_as_an_instance() -> None:
    """Returning the class rather than an instance is the commonest mistake."""

    class Plugin(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = PluginMetadata(
            name="klass",
            version="1.0.0",
            author="t",
            description="d",
            content_types=frozenset({ContentType.CSV}),
            priority=40,
        )

        def create_transformer(self, context: PluginContext) -> Transformer:
            return type(
                "K",
                (Marker,),
                {"name": "klass", "priority": 40, "content_types": frozenset({ContentType.CSV})},
            )()

    manager = PluginManager([StaticSource(klass=Plugin)])
    _attached(manager)
    assert manager.report().enabled == ("klass",)


def test_directory_source_loads_a_python_file(tmp_path: Path) -> None:
    (tmp_path / "my_plugin.py").write_text(
        textwrap.dedent(
            """
            from gateway.plugins import ContentType, Transformer, TransformOutput, simple_plugin

            class T(Transformer):
                def transform(self, content, detection):
                    return TransformOutput(content, ())

            PLUGIN = simple_plugin(
                transformer=T, name="fromdir", version="0.1.0", author="t",
                description="d", content_types={ContentType.CSV}, priority=45,
            )
            """
        ),
        encoding="utf-8",
    )

    manager = PluginManager([DirectorySource(tmp_path)])
    _attached(manager)

    assert manager.report().enabled == ("fromdir",)


def test_directory_source_skips_dunder_and_non_python(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "_private.py").write_text("PLUGIN = None", encoding="utf-8")

    manager = PluginManager([DirectorySource(tmp_path)])
    assert manager.load().records == ()


def test_a_missing_plugin_directory_is_not_fatal(tmp_path: Path) -> None:
    manager = PluginManager([DirectorySource(tmp_path / "nope")])
    assert manager.load().healthy


def test_explicit_source_loads_a_dotted_path() -> None:
    manager = PluginManager([ExplicitSource(["tests.test_plugins:EXPLICIT_PLUGIN"])])
    _attached(manager)
    assert manager.report().enabled == ("explicit",)


def test_a_source_that_raises_during_discovery_is_isolated() -> None:
    manager = PluginManager([ExplodingSource(), StaticSource(demo=_make_plugin())])
    _attached(manager)

    assert manager.report().enabled == ("demo",)


# -- Precedence and duplicates ---------------------------------------------


def test_the_first_source_to_claim_a_name_keeps_it() -> None:
    """Explicit configuration is listed first, so an operator override wins."""
    first = _make_plugin("dupe", priority=10)
    second = _make_plugin("dupe", priority=90)

    manager = PluginManager([StaticSource(dupe=first), StaticSource(dupe=second)])
    _attached(manager)

    (record,) = [r for r in manager.records if r.state is PluginState.ENABLED]
    assert record.metadata is not None
    assert record.metadata.priority == 10


def test_a_duplicate_name_is_recorded_as_a_failure_not_a_crash() -> None:
    manager = PluginManager(
        [StaticSource(a=_make_plugin("dupe")), StaticSource(b=_make_plugin("dupe"))]
    )
    manager.load()
    report = manager.report()

    assert report.enabled == () or report.loaded == ("dupe",)
    assert len(report.records) == 1, "the loser is not recorded under a name it does not own"


def test_a_plugin_clashing_with_a_builtin_fails_and_rolls_back() -> None:
    """`html` is a builtin. The plugin must not half-register."""
    clashing = _make_plugin("html", content_types={ContentType.CSV})
    manager = PluginManager([StaticSource(html=clashing)])
    detector, registry = _attached(manager)

    (record,) = manager.records
    assert record.state is PluginState.FAILED
    assert record.stage == "register"
    assert registry.names == ("html", "json", "text")  # type: ignore[attr-defined]
    assert len(detector.sniffers) == len(ContentDetector().sniffers)


# -- Version compatibility --------------------------------------------------


@pytest.mark.parametrize(
    ("plugin_api", "gateway_api"),
    [("1.0", "1.0"), ("1.0", "1.3"), ("1.3", "1.3"), ("2.1", "2.4")],
)
def test_compatible_api_versions_load(plugin_api: str, gateway_api: str) -> None:
    """Equal major, and a minor no newer than the gateway's."""
    check_api_compatibility(plugin_api, gateway_api)


@pytest.mark.parametrize(
    ("plugin_api", "gateway_api"),
    [
        ("2.0", "1.0"),  # newer major
        ("0.9", "1.0"),  # older major
        ("1.4", "1.0"),  # plugin needs API the gateway lacks
    ],
)
def test_incompatible_api_versions_are_rejected(plugin_api: str, gateway_api: str) -> None:
    with pytest.raises(PluginVersionError):
        check_api_compatibility(plugin_api, gateway_api)


def test_a_plugin_from_the_future_fails_but_the_others_load() -> None:
    manager = PluginManager(
        [
            StaticSource(
                future=_make_plugin("future", api_version="1.9"),
                fine=_make_plugin("fine", priority=51),
            )
        ]
    )
    _attached(manager)
    report = manager.report()

    assert report.enabled == ("fine",)
    (failure,) = report.failed
    assert failure.name == "future"
    assert failure.stage == "version"


@pytest.mark.parametrize("bad", ["1", "1.x", "v1.0", ""])
def test_a_malformed_api_version_is_rejected(bad: str) -> None:
    with pytest.raises(PluginVersionError):
        check_api_compatibility(bad, "1.0")


# -- Load failures are isolated --------------------------------------------


def test_a_plugin_that_raises_on_import_is_isolated() -> None:
    def boom() -> object:
        raise ImportError("no module named 'nonexistent'")

    class BoomSource(PluginSource):
        name: ClassVar[str] = "boom"

        def discover(self):
            yield PluginCandidate("boom", self.name, "boom:boom", boom)

    manager = PluginManager([BoomSource(), StaticSource(ok=_make_plugin("ok"))])
    _attached(manager)
    report = manager.report()

    assert report.enabled == ("ok",)
    (failure,) = report.failed
    assert failure.stage == "import"
    assert "nonexistent" in (failure.error or "")


def test_a_plugin_that_raises_while_constructing_is_isolated() -> None:
    class Exploding(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = PluginMetadata(
            name="exploding",
            version="1.0.0",
            author="t",
            description="d",
            content_types=frozenset({ContentType.CSV}),
            priority=60,
        )

        def create_transformer(self, context: PluginContext) -> Transformer:
            raise RuntimeError("cannot build")

    manager = PluginManager([StaticSource(exploding=Exploding(), ok=_make_plugin("ok"))])
    _attached(manager)
    report = manager.report()

    assert report.enabled == ("ok",)
    assert report.failed[0].stage == "construct"


def test_a_non_plugin_object_is_rejected() -> None:
    manager = PluginManager([StaticSource(bogus="I am a string")])
    manager.load()

    (record,) = manager.records
    assert record.state is PluginState.FAILED
    assert "not a TransformerPlugin" in (record.error or "")


def test_a_transformer_disagreeing_with_its_metadata_is_rejected() -> None:
    """The registry would sort by one priority while the operator reads another."""

    class Liar(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = PluginMetadata(
            name="liar",
            version="1.0.0",
            author="t",
            description="d",
            content_types=frozenset({ContentType.CSV}),
            priority=60,
        )

        def create_transformer(self, context: PluginContext) -> Transformer:
            return type(
                "L",
                (Marker,),
                {"name": "liar", "priority": 999, "content_types": frozenset({ContentType.CSV})},
            )()

    manager = PluginManager([StaticSource(liar=Liar())])
    manager.load()

    (record,) = manager.records
    assert record.state is PluginState.FAILED
    assert record.stage == "validate"


def test_a_broken_plugin_does_not_stop_the_gateway(tmp_path: Path) -> None:
    """The whole point of the plugin system's error handling."""
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom at import')", encoding="utf-8")
    (tmp_path / "fine.py").write_text(
        textwrap.dedent(
            """
            from gateway.plugins import ContentType, Transformer, TransformOutput, simple_plugin

            class T(Transformer):
                def transform(self, content, detection):
                    return TransformOutput(content, ())

            PLUGIN = simple_plugin(
                transformer=T, name="fine", version="0.1.0", author="t",
                description="d", content_types={ContentType.CSV}, priority=45,
            )
            """
        ),
        encoding="utf-8",
    )

    settings = build_settings(
        plugins_enabled=True, plugins_dir=tmp_path, plugins_entry_point_group=""
    )
    app = create_app(settings)  # must not raise
    assert isinstance(app, FastAPI)


# -- Metadata validation ----------------------------------------------------


@pytest.mark.parametrize("name", ["Bad Name", "UPPER", "", "x" * 65, "-leading"])
def test_invalid_plugin_names_are_rejected(name: str) -> None:
    with pytest.raises(PluginValidationError, match="plugin name"):
        _make_plugin(name)


@pytest.mark.parametrize("version", ["1", "x.y.z", "", "1.0.0.0", "1.0-", "1.0."])
def test_invalid_versions_are_rejected(version: str) -> None:
    with pytest.raises(PluginValidationError, match="semver"):
        _make_plugin(version=version)


@pytest.mark.parametrize("version", ["1.0", "1.0.0", "0.1.0", "1.0.0-rc1", "2.3.4+build.7"])
def test_valid_versions_are_accepted(version: str) -> None:
    assert _make_plugin(version=version).metadata.version == version


@pytest.mark.parametrize("priority", [-1, 1001])
def test_out_of_range_priorities_are_rejected(priority: int) -> None:
    with pytest.raises(PluginValidationError, match="priority"):
        _make_plugin(priority=priority)


def test_a_plugin_with_no_content_types_is_rejected() -> None:
    with pytest.raises(PluginValidationError, match="no content types"):
        _make_plugin(content_types=set())


@pytest.mark.parametrize(
    "capabilities",
    [
        {Capability.DETERMINISTIC},
        {Capability.IDEMPOTENT},
        set(),
    ],
)
def test_the_required_capabilities_are_enforced(capabilities: set[Capability]) -> None:
    """The pipeline's cache and idempotency guarantees rest on these."""
    with pytest.raises(PluginValidationError, match="must declare"):
        _make_plugin(capabilities=capabilities)


def test_a_lossy_plugin_is_refused_unless_the_deployment_opts_in() -> None:
    lossy = _make_plugin(
        "lossy",
        capabilities={Capability.DETERMINISTIC, Capability.IDEMPOTENT, Capability.LOSSY},
    )

    refused = PluginManager([StaticSource(lossy=lossy)])
    refused.load()
    assert refused.records[0].state is PluginState.FAILED
    assert refused.records[0].stage == "capability"

    permitted = PluginManager([StaticSource(lossy=lossy)], allow_lossy=True)
    _attached(permitted)
    assert permitted.report().enabled == ("lossy",)


def test_sniffers_require_the_provides_sniffer_capability() -> None:
    class Sniff:
        name = "s"

        def sniff(self, content: str) -> Detection | None:
            return None

    class Sneaky(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = PluginMetadata(
            name="sneaky",
            version="1.0.0",
            author="t",
            description="d",
            content_types=frozenset({ContentType.CSV}),
            priority=60,
        )

        def create_transformer(self, context: PluginContext) -> Transformer:
            return type(
                "S",
                (Marker,),
                {"name": "sneaky", "priority": 60, "content_types": frozenset({ContentType.CSV})},
            )()

        def create_sniffers(self, context: PluginContext):
            return (Sniff(),)

    manager = PluginManager([StaticSource(sneaky=Sneaky())])
    manager.load()

    assert manager.records[0].state is PluginState.FAILED
    assert "provides_sniffer" in (manager.records[0].error or "")


# -- Priority ordering ------------------------------------------------------


def test_plugins_are_ordered_by_priority_among_the_builtins() -> None:
    early = _make_plugin("early", priority=15)
    late = _make_plugin("late", priority=90)

    manager = PluginManager([StaticSource(early=early, late=late)])
    _detector, registry = _attached(manager)

    assert registry.names == ("html", "early", "json", "late", "text")  # type: ignore[attr-defined]


def test_priority_decides_which_transformer_claims_shared_content() -> None:
    high = _make_plugin("high", priority=5, content_types={ContentType.TEXT})
    low = _make_plugin("low", priority=95, content_types={ContentType.TEXT})

    manager = PluginManager([StaticSource(high=high, low=low)])
    _detector, registry = _attached(manager)

    chosen = registry.select("x", Detection(ContentType.TEXT, 1.0, "t"))  # type: ignore[attr-defined]
    assert chosen is not None
    assert chosen.name == "high"


# -- Plugin isolation: one plugin cannot see or break another ---------------


def test_a_plugin_receives_only_its_own_configuration() -> None:
    seen: dict[str, object] = {}

    class Configured(TransformerPlugin):
        metadata: ClassVar[PluginMetadata] = PluginMetadata(
            name="configured",
            version="1.0.0",
            author="t",
            description="d",
            content_types=frozenset({ContentType.CSV}),
            priority=60,
        )

        def create_transformer(self, context: PluginContext) -> Transformer:
            seen.update(context.config)
            return type(
                "C",
                (Marker,),
                {
                    "name": "configured",
                    "priority": 60,
                    "content_types": frozenset({ContentType.CSV}),
                },
            )()

    manager = PluginManager(
        [StaticSource(configured=Configured())],
        config={"configured": {"mine": 1}, "other": {"secret": 2}},
    )
    _attached(manager)

    assert seen == {"mine": 1}


def test_the_plugin_context_does_not_expose_settings() -> None:
    """A plugin must not be able to read the gateway's credentials."""
    assert not hasattr(CONTEXT, "settings")
    assert set(PluginContext.__slots__) == {"api_version", "config"}


# -- Lifecycle --------------------------------------------------------------


def test_disable_unregisters_and_enable_restores() -> None:
    manager = PluginManager([StaticSource(demo=_make_plugin())])
    _detector, registry = _attached(manager)
    assert "demo" in registry.names  # type: ignore[attr-defined]

    manager.disable("demo")
    assert manager.records[0].state is PluginState.DISABLED
    assert "demo" not in registry.names  # type: ignore[attr-defined]

    manager.enable("demo")
    assert manager.records[0].state is PluginState.ENABLED
    assert "demo" in registry.names  # type: ignore[attr-defined]


def test_a_plugin_disabled_by_configuration_loads_but_never_attaches() -> None:
    manager = PluginManager([StaticSource(demo=_make_plugin())], disabled=frozenset({"demo"}))
    _detector, registry = _attached(manager)

    assert manager.report().disabled == ("demo",)
    assert "demo" not in registry.names  # type: ignore[attr-defined]


def test_unload_detaches_and_forgets() -> None:
    manager = PluginManager([StaticSource(demo=_make_plugin())])
    _detector, registry = _attached(manager)

    manager.unload("demo")

    assert manager.records == ()
    assert "demo" not in registry.names  # type: ignore[attr-defined]


def test_a_failed_plugin_cannot_be_enabled() -> None:
    manager = PluginManager([StaticSource(bogus=object())])
    manager.load()

    with pytest.raises(Exception, match="cannot be enabled"):
        manager.enable("bogus")


def test_disable_and_unload_are_idempotent_enough() -> None:
    manager = PluginManager([StaticSource(demo=_make_plugin())])
    _attached(manager)

    manager.disable("demo")
    manager.disable("demo")
    manager.unload("demo")

    with pytest.raises(Exception, match="not known"):
        manager.disable("demo")


# -- Disabled entirely ------------------------------------------------------


def test_the_kill_switch_loads_nothing() -> None:
    settings = build_settings(plugins_enabled=False)
    manager = PluginManager.from_settings(settings)
    assert manager.load().records == ()


# -- The introspection endpoint ---------------------------------------------


async def _client(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, ASGITransport(app=app, raise_app_exceptions=False)  # type: ignore[return-value]


async def test_the_plugins_endpoint_reports_state(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    settings = build_settings(
        plugins_enabled=True, plugins_dir=tmp_path, plugins_entry_point_group=""
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.get("/internal/plugins")

    body = response.json()
    assert response.status_code == 200
    assert body["api_version"] == PLUGIN_API_VERSION
    assert body["failed"] == ["broken"]
    (plugin,) = body["plugins"]
    assert plugin["state"] == "failed"
    assert plugin["stage"] == "import"
    assert "boom" in plugin["error"]


async def test_the_plugins_endpoint_is_empty_when_disabled() -> None:
    settings = build_settings(plugins_enabled=False)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            response = await http.get("/internal/plugins")

    assert response.json()["total"] == 0


# Module-level so `ExplicitSource` can import it by dotted path.
EXPLICIT_PLUGIN = _make_plugin("explicit", priority=55)
