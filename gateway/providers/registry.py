"""Provider lookup.

Built once during startup and read-only thereafter, so a missing provider is a
configuration bug that surfaces at boot rather than a 500 under load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.errors import ConfigurationError
from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.providers.base import Provider

logger = get_logger(__name__)


class ProviderRegistry:
    """Maps a provider name to its configured instance."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        if provider.name in self._providers:
            raise ConfigurationError(f"provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider
        logger.debug("provider_registered", provider=provider.name, base_url=provider.base_url)

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ConfigurationError(f"provider {name!r} is not registered") from exc

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    def __contains__(self, name: object) -> bool:
        return name in self._providers
