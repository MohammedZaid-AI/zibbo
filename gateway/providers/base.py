"""The Provider abstraction.

A provider *translates*. It knows where a path lives upstream, how the provider
authenticates, which of its endpoints carry optimizable prose and where that prose
sits in the request body, how it frames a mid-stream error, and what shape its
errors take. It knows nothing about sockets, retries, or ASGI —
:class:`gateway.providers.proxy.ProxyService` owns all of that.

That split is the whole point of the phase. The gateway core contains **no**
provider-specific logic: not the endpoint allowlist (a provider supplies an
``EndpointPolicy``), not the payload shape (a provider supplies ``PayloadAdapter``s),
not the error envelope (a provider supplies one). Adding Gemini, Groq, Mistral or
Ollama is one subclass and one registration.

``build_request`` is a template method: it fixes the *order* of translation — filter
headers, authenticate, add provider headers, resolve URL, detect stream — while the
provider supplies the pieces. A provider that only differs in authentication changes
one attribute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from gateway.errors import DEFAULT_ERROR_ENVELOPE, ErrorEnvelope
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.policy import EndpointPolicy
from gateway.providers.auth import AuthStrategy, BearerAuth
from gateway.providers.headers import filter_request_headers

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import SecretStr

    from gateway.optimizers.extraction import PayloadAdapter


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    """A fully translated request, ready for the transport layer."""

    method: str
    url: httpx.URL
    headers: httpx.Headers
    content: bytes
    stream: bool
    model: str | None = None
    """Extracted purely for logging and, from a later phase, analytics."""


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


class Provider:
    """Translates a gateway request into an upstream request.

    Concrete, not abstract: a provider is *configured*, not subclassed-to-fill-in.
    Behaviour comes from the auth strategy, endpoint policy, adapters and error
    envelope passed in, so the common path — auth, header filtering, stream
    detection — is written once here and cannot drift between providers. Subclasses
    exist only to bundle a provider's defaults; several providers share one.
    """

    #: Provider identity. Subclasses set a class-level default; providers that
    #: multiplex one class over several backends (OpenAI-compatible) set it per
    #: instance, which is why this is not a ``ClassVar``.
    name: str = ""

    #: How this provider authenticates. Bearer is the OpenAI-compatible default.
    auth: ClassVar[AuthStrategy] = BearerAuth()

    #: Headers the provider mandates on every request (e.g. ``anthropic-version``).
    #: A caller's own value is never overwritten. Not a ``ClassVar`` because a
    #: provider may pin the value per instance.
    default_headers: Mapping[str, str] = {}

    #: The stream sentinel this provider emits. OpenAI and Anthropic both use
    #: SSE ``data:`` framing, so the default suits every OpenAI-compatible provider.
    stream_media_type: ClassVar[str] = "text/event-stream"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None = None,
        endpoint_policy: EndpointPolicy | None = None,
        adapters: tuple[PayloadAdapter, ...] = (),
        error_envelope: ErrorEnvelope | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._endpoint_policy = endpoint_policy or EndpointPolicy()
        self._adapter_registry = AdapterRegistry(adapters)
        self._error_envelope = error_envelope or DEFAULT_ERROR_ENVELOPE

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def endpoint_policy(self) -> EndpointPolicy:
        return self._endpoint_policy

    @property
    def adapters(self) -> AdapterRegistry:
        return self._adapter_registry

    @property
    def error_envelope(self) -> ErrorEnvelope:
        return self._error_envelope

    # -- Translation steps -------------------------------------------------

    def upstream_url(self, path: str, query: str) -> httpx.URL:
        """Map a gateway path onto the provider's origin."""
        url = httpx.URL(f"{self._base_url}/{path.lstrip('/')}")
        if query:
            url = url.copy_with(query=query.encode("ascii"))
        return url

    def authenticate(self, headers: httpx.Headers) -> None:
        """Attach the configured credential, unless the caller supplied their own."""
        self.auth.apply(headers, self._api_key)

    def wants_stream(self, payload: Mapping[str, Any] | None, path: str) -> bool:
        """Whether the response will be a stream.

        Defaults to the OpenAI/Anthropic convention of ``{"stream": true}`` in the
        body. Providers that signal streaming through the URL (Gemini's
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

    def stream_error_frame(self, payload: dict[str, Any]) -> bytes:
        """Frame a gateway-authored error for a stream that broke after it opened.

        Both OpenAI and Anthropic parse an SSE ``data:`` line carrying ``error`` and
        raise from it, so the default serves every SSE provider. A provider whose
        stream framing differs overrides this.
        """
        return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"

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
        for key, value in self.default_headers.items():
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
