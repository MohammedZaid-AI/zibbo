"""OpenAI, and every provider that speaks its dialect.

The OpenAI integration is short because the base class already does the work. What
lives here is only what is specific to OpenAI: which endpoints carry optimizable
prose, and where in their bodies that prose sits.

``OpenAICompatibleProvider`` is the same thing with the endpoint allowlist stripped
down to ``chat/completions``, for Groq, Mistral and Ollama. They implement OpenAI's
chat API but not its Assistants or Responses surfaces, and pointing an allowlist at
an endpoint a provider does not have would at best waste a parse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from gateway.optimizers.policy import EndpointPolicy
from gateway.providers.auth import BearerAuth
from gateway.providers.base import Provider
from gateway.providers.schemas import ChatCompletionsAdapter, openai_adapters

if TYPE_CHECKING:
    from pydantic import SecretStr

# Endpoints whose bodies are prose worth optimizing.
OPENAI_ENDPOINTS = EndpointPolicy(
    allowed=frozenset({"chat/completions", "responses", "assistants"}),
    allowed_prefixes=("threads/",),
    # Corrupting any of these would be catastrophic and silent, so they are denied
    # explicitly even though the allowlist already excludes them.
    denied_prefixes=(
        "files",
        "uploads",
        "audio/",
        "images/",
        "fine_tuning/",
        "batches",
        "embeddings",
        "moderations",
    ),
)

# The chat endpoint only. Groq/Mistral/Ollama expose nothing else optimizable.
OPENAI_COMPATIBLE_ENDPOINTS = EndpointPolicy(allowed=frozenset({"chat/completions"}))


class OpenAIProvider(Provider):
    """Proxies the OpenAI REST API.

    Authentication is the OpenAI-compatible default: the caller's ``Authorization``
    header wins, and a configured key is the fallback for callers that have none.
    That is what makes the drop-in promise true — an app already sending
    ``Authorization: Bearer sk-...`` keeps billing to its own account.
    """

    name = "openai"
    auth: ClassVar[BearerAuth] = BearerAuth()

    def __init__(self, *, base_url: str, api_key: SecretStr | None = None) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            endpoint_policy=OPENAI_ENDPOINTS,
            adapters=openai_adapters(),
        )


class OpenAICompatibleProvider(Provider):
    """A provider that implements OpenAI's chat API but not the rest of its surface.

    Groq, Mistral and Ollama. Distinguished by ``name`` and ``base_url``; behaviour
    is identical to OpenAI's chat endpoint. Anything they add beyond it is still
    proxied — just never optimized until an adapter is written for it.
    """

    auth: ClassVar[BearerAuth] = BearerAuth()

    def __init__(self, *, name: str, base_url: str, api_key: SecretStr | None = None) -> None:
        self.name = name  # instance-level: several of these coexist under one class
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            endpoint_policy=OPENAI_COMPATIBLE_ENDPOINTS,
            adapters=(ChatCompletionsAdapter(),),
        )
