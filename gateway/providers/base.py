"""The Provider abstraction.

A provider knows how to *translate*: where a path lives upstream, how to
authenticate, and how to tell that a request wants a stream. It knows nothing
about sockets, retries, or ASGI — :class:`gateway.providers.proxy.ProxyService`
owns all of that.

That split is the point. Adding Anthropic, Gemini, Groq, Mistral or Ollama means
writing one subclass of :class:`Provider` and registering it. No transport code,
no streaming code, no error-mapping code gets touched or duplicated.

``build_request`` is a template method: it fixes the *order* of translation
(filter headers, authenticate, add provider defaults, resolve URL, detect stream)
while leaving each step overridable. Subclasses that only differ in how they
authenticate override exactly one method.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from gateway.providers.headers import filter_request_headers

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    """A fully translated request, ready for the transport layer."""

    method: str
    url: httpx.URL
    headers: httpx.Headers
    content: bytes
    stream: bool
    model: str | None = None
    """Extracted purely for logging and, from Phase 4, analytics."""


def parse_json_object(content: bytes, content_type: str) -> dict[str, Any] | None:
    """Best-effort peek at a JSON body.

    Returns ``None`` for anything that is not a JSON object — multipart uploads,
    malformed bodies, bare arrays. Callers must treat that as "no information",
    never as an error: a body we cannot parse is still forwarded verbatim.
    """
    if not content or "json" not in content_type.lower():
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class Provider(ABC):
    """Translates a gateway request into an upstream request."""

    name: ClassVar[str]

    def __init__(self, *, base_url: str, api_key: SecretStr | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- Translation steps -------------------------------------------------

    def upstream_url(self, path: str, query: str) -> httpx.URL:
        """Map a gateway path onto the provider's origin."""
        url = httpx.URL(f"{self._base_url}/{path.lstrip('/')}")
        if query:
            url = url.copy_with(query=query.encode("ascii"))
        return url

    @abstractmethod
    def authenticate(self, headers: httpx.Headers) -> None:
        """Attach credentials, mutating ``headers`` in place.

        Must be a no-op when the caller already supplied their own credential:
        transparency means the caller's key, not ours, is what the provider sees.
        """

    def extra_headers(self) -> Mapping[str, str]:
        """Provider-mandated headers (e.g. Anthropic's ``anthropic-version``)."""
        return {}

    def wants_stream(self, payload: Mapping[str, Any] | None, path: str) -> bool:
        """Whether the response will be a stream.

        Defaults to the OpenAI convention of ``{"stream": true}`` in the body.
        Providers that signal streaming through the URL (Gemini's
        ``:streamGenerateContent``) override this.
        """
        del path
        if payload is None:
            return False
        return payload.get("stream") is True

    def extract_model(self, payload: Mapping[str, Any] | None) -> str | None:
        if not payload:
            return None
        model = payload.get("model")
        return model if isinstance(model, str) else None

    # -- Template method ---------------------------------------------------

    def build_request(
        self,
        *,
        method: str,
        path: str,
        query: str,
        headers: httpx.Headers | Mapping[str, str],
        content: bytes,
    ) -> UpstreamRequest:
        forwarded = filter_request_headers(headers)
        self.authenticate(forwarded)
        for key, value in self.extra_headers().items():
            forwarded.setdefault(key.lower(), value)

        payload = parse_json_object(content, forwarded.get("content-type", ""))

        return UpstreamRequest(
            method=method,
            url=self.upstream_url(path, query),
            headers=forwarded,
            content=content,
            stream=self.wants_stream(payload, path),
            model=self.extract_model(payload),
        )
