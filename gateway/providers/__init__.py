"""Upstream provider clients.

``Provider`` translates (URL, auth, streaming detection); ``ProxyService``
transports. Supporting a new provider means adding one ``Provider`` subclass —
Anthropic in Phase 6, and Gemini, Groq, Mistral or Ollama after it.
"""

from gateway.providers.base import Provider, UpstreamRequest, parse_json_object
from gateway.providers.openai import OpenAIProvider
from gateway.providers.proxy import ProxyService
from gateway.providers.registry import ProviderRegistry

__all__ = [
    "OpenAIProvider",
    "Provider",
    "ProviderRegistry",
    "ProxyService",
    "UpstreamRequest",
    "parse_json_object",
]
