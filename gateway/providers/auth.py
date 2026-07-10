"""Provider authentication, as a strategy rather than a method override.

Every provider answers the same two questions differently:

* Where does the credential go? ``Authorization: Bearer``, ``x-api-key``,
  ``x-goog-api-key``.
* Did the caller already supply one?

Composing a strategy instead of overriding ``authenticate`` per provider means the
transparency rule — **the caller's credential always wins** — is written once and
cannot be forgotten by the next provider module. That rule is what lets an existing
application switch to the gateway by changing one URL and keep billing its own
account.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    import httpx
    from pydantic import SecretStr


class AuthStrategy(ABC):
    """Places a credential on an outbound request."""

    name: ClassVar[str]

    @abstractmethod
    def credential_headers(self) -> tuple[str, ...]:
        """Headers whose presence means the caller supplied their own credential."""

    @abstractmethod
    def _inject(self, headers: httpx.Headers, api_key: SecretStr) -> None: ...

    def apply(self, headers: httpx.Headers, api_key: SecretStr | None) -> None:
        """Attach the configured credential, unless the caller brought their own.

        When neither exists we send nothing and let the provider answer with its own
        401. That is more useful than a 401 we invent, and it is the behaviour an
        SDK's error handling already expects.
        """
        if self.has_caller_credential(headers):
            return
        if api_key is not None:
            self._inject(headers, api_key)

    def has_caller_credential(self, headers: httpx.Headers) -> bool:
        return any(header in headers for header in self.credential_headers())


class BearerAuth(AuthStrategy):
    """``Authorization: Bearer <key>``. OpenAI, Groq, Mistral, Ollama."""

    name: ClassVar[str] = "bearer"

    def credential_headers(self) -> tuple[str, ...]:
        return ("authorization",)

    def _inject(self, headers: httpx.Headers, api_key: SecretStr) -> None:
        headers["authorization"] = f"Bearer {api_key.get_secret_value()}"


class ApiKeyHeaderAuth(AuthStrategy):
    """A bare key in a named header. Anthropic (``x-api-key``), Gemini (``x-goog-api-key``).

    ``also_accept`` lists headers that also count as a caller-supplied credential.
    Anthropic accepts an OAuth bearer token as well as an API key, and a caller who
    sends one must not additionally have our ``x-api-key`` bolted on.
    """

    name: ClassVar[str] = "api-key-header"

    def __init__(self, header: str, *, also_accept: tuple[str, ...] = ()) -> None:
        self._header = header.lower()
        self._also_accept = tuple(item.lower() for item in also_accept)

    @property
    def header(self) -> str:
        return self._header

    def credential_headers(self) -> tuple[str, ...]:
        return (self._header, *self._also_accept)

    def _inject(self, headers: httpx.Headers, api_key: SecretStr) -> None:
        headers[self._header] = api_key.get_secret_value()


class NoAuth(AuthStrategy):
    """Send nothing. A local Ollama with no key configured, for instance."""

    name: ClassVar[str] = "none"

    def credential_headers(self) -> tuple[str, ...]:
        return ()

    def _inject(self, headers: httpx.Headers, api_key: SecretStr) -> None:
        return
