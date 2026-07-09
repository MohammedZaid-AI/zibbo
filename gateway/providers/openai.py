"""OpenAI provider.

The entire OpenAI integration is this file, and it is short. That is the measure
of whether the abstraction in ``base.py`` is right: everything specific to OpenAI
is *only* how it authenticates.

Note what is absent. There is no per-endpoint code. ``chat/completions``,
``embeddings``, ``models``, ``moderations``, ``images/generations``, ``files`` and
everything OpenAI ships next are proxied by the same catch-all path, because the
gateway never needs to understand a payload it does not modify.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from gateway.providers.base import Provider

if TYPE_CHECKING:
    import httpx


class OpenAIProvider(Provider):
    """Proxies the OpenAI REST API (and any API that mimics it)."""

    name: ClassVar[str] = "openai"

    def authenticate(self, headers: httpx.Headers) -> None:
        """Forward the caller's credential; fall back to a configured one.

        The caller's key winning is what makes the drop-in promise true: an app
        that already sends ``Authorization: Bearer sk-...`` keeps billing to its
        own account, and the gateway needs no credential at all. The configured
        key exists for callers that have none — an internal service, a browser
        client that must not hold a provider key.

        When neither is present we send nothing, and OpenAI replies with its own
        401 in its own envelope. That is more useful than a 401 we invent.
        """
        if "authorization" in headers:
            return
        if self._api_key is not None:
            headers["authorization"] = f"Bearer {self._api_key.get_secret_value()}"
