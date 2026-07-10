"""Anthropic.

The second real provider, and the one that proves the abstraction. Everything that
differs from OpenAI is declared here as data, not scattered through the core:

* **Authentication** is ``x-api-key``, a bare key, not a bearer token. An OAuth
  ``Authorization`` header also counts as a caller credential, so a caller using one
  does not additionally get our ``x-api-key``.
* **A version header**, ``anthropic-version``, is mandatory. The caller's own value
  is never overwritten.
* **The error envelope** is Anthropic's ``{"type": "error", "error": {...}}``, so a
  gateway-authored 502 is something the Anthropic SDK can parse and raise from.
* **The request schema** has a top-level ``system`` field and ``content`` blocks —
  handled by ``AnthropicMessagesAdapter``.

Streaming needs no special handling: Anthropic uses SSE ``data:`` framing, and its
stream carries typed events (``message_start``, ``content_block_delta``, …) that the
gateway relays untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from gateway.optimizers.policy import EndpointPolicy
from gateway.providers.auth import ApiKeyHeaderAuth
from gateway.providers.base import Provider
from gateway.providers.schemas import anthropic_adapters

if TYPE_CHECKING:
    from pydantic import SecretStr

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# The Anthropic SDK adds `/v1`, so the path the gateway sees is `v1/messages`. Match
# by suffix rather than pinning the version, so a future `v2/messages` still works.
ANTHROPIC_ENDPOINTS = EndpointPolicy(
    allowed_suffixes=("messages",),
    denied_prefixes=("v1/files", "v1/models", "v1/messages/batches"),
)


class AnthropicErrorEnvelope:
    """Anthropic's error shape: ``{"type": "error", "error": {"type", "message"}}``.

    The Anthropic SDK reads ``error.type`` to choose its exception class, so a
    gateway-authored failure has to name a type the SDK recognizes. ``api_error`` is
    Anthropic's catch-all and the honest label for "the gateway could not reach
    Anthropic". The gateway's request id rides along under a namespaced key rather
    than displacing anything Anthropic defines.
    """

    _GATEWAY_TO_ANTHROPIC: ClassVar[dict[str, str]] = {
        "upstream_error": "api_error",
        "upstream_timeout": "api_error",
        "service_unavailable_error": "overloaded_error",
        "invalid_request_error": "invalid_request_error",
        "not_found_error": "not_found_error",
        "api_error": "api_error",
    }

    def render(
        self,
        *,
        message: str,
        error_type: str,
        code: str | None,
        param: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        del param
        error: dict[str, Any] = {
            "type": self._GATEWAY_TO_ANTHROPIC.get(error_type, "api_error"),
            "message": message,
        }
        if code:
            error["gateway_code"] = code
        payload: dict[str, Any] = {"type": "error", "error": error}
        if request_id:
            payload["request_id"] = request_id
        return payload


class AnthropicProvider(Provider):
    """Proxies the Anthropic Messages API."""

    name = "anthropic"
    # `x-api-key` for the configured key; an OAuth `authorization` from the caller
    # also counts, so we never double up on a caller who authenticates that way.
    auth: ClassVar[ApiKeyHeaderAuth] = ApiKeyHeaderAuth("x-api-key", also_accept=("authorization",))

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None = None,
        version: str = DEFAULT_ANTHROPIC_VERSION,
    ) -> None:
        # Instance-level so a deployment can pin a version without a subclass.
        self.default_headers = {"anthropic-version": version}
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            endpoint_policy=ANTHROPIC_ENDPOINTS,
            adapters=anthropic_adapters(),
            error_envelope=AnthropicErrorEnvelope(),
        )
