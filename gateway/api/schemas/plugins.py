"""Wire schemas for plugin introspection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from gateway.plugins import PluginRecord, PluginState


class PluginSummary(BaseModel):
    """One plugin, whatever became of it."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: PluginState
    source: str
    origin: str

    version: str | None = None
    author: str | None = None
    description: str | None = None
    homepage: str | None = None
    content_types: list[str] = Field(default_factory=list)
    priority: int | None = None
    api_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    load_time_ms: float = 0.0
    error: str | None = None
    stage: str | None = None

    @classmethod
    def from_record(cls, record: PluginRecord) -> PluginSummary:
        return cls.model_validate(record.summary())


class PluginListResponse(BaseModel):
    """The plugin system's state, for operators and for debugging."""

    model_config = ConfigDict(frozen=True)

    api_version: str
    total: int
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    plugins: list[PluginSummary] = Field(default_factory=list)
