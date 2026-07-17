"""Upstream provider clients.

``Provider`` translates (URL, auth, streaming detection, endpoint policy, request
schema, error envelope); ``ProxyService`` transports (sockets, streaming, header
preservation). The gateway core holds no provider-specific logic — a provider
supplies its own as data. Adding Gemini, Groq, Mistral or Ollama is one subclass.
"""

from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.auth import ApiKeyHeaderAuth, AuthStrategy, BearerAuth, NoAuth
from gateway.providers.base import Provider, UpstreamRequest, parse_json_object
from gateway.providers.openai import OpenAICompatibleProvider, OpenAIProvider
from gateway.providers.proxy import ProxyMetrics, ProxyMetricsSnapshot, ProxyService
from gateway.providers.registry import ProviderRegistry

__all__ = [
    "AnthropicProvider",
    "ApiKeyHeaderAuth",
    "AuthStrategy",
    "BearerAuth",
    "NoAuth",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderRegistry",
    "ProxyMetrics",
    "ProxyMetricsSnapshot",
    "ProxyService",
    "UpstreamRequest",
    "parse_json_object",
]
