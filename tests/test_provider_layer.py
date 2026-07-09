"""Unit tests for the provider abstraction and the header policy."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from gateway.errors import ConfigurationError
from gateway.providers import OpenAIProvider, ProviderRegistry, parse_json_object
from gateway.providers.headers import filter_request_headers, filter_response_headers


def _provider(api_key: str | None = None) -> OpenAIProvider:
    return OpenAIProvider(
        base_url="https://api.openai.com/v1",
        api_key=SecretStr(api_key) if api_key else None,
    )


# -- URL construction ------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        (
            "https://api.openai.com/v1",
            "chat/completions",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://api.openai.com/v1/",
            "chat/completions",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://api.openai.com/v1",
            "/chat/completions",
            "https://api.openai.com/v1/chat/completions",
        ),
        ("http://localhost:11434/v1", "models", "http://localhost:11434/v1/models"),
    ],
)
def test_upstream_url_joins_without_doubling_slashes(base: str, path: str, expected: str) -> None:
    assert str(OpenAIProvider(base_url=base).upstream_url(path, "")) == expected


def test_query_string_is_appended() -> None:
    url = _provider().upstream_url("models", "limit=2&after=x")
    assert url.query == b"limit=2&after=x"


# -- Authentication --------------------------------------------------------


def test_configured_key_is_injected_when_absent() -> None:
    headers: dict[str, str] = {}
    _provider("sk-configured").authenticate(headers)
    assert headers["authorization"] == "Bearer sk-configured"


def test_caller_key_is_never_overwritten() -> None:
    headers = {"authorization": "Bearer sk-caller"}
    _provider("sk-configured").authenticate(headers)
    assert headers["authorization"] == "Bearer sk-caller"


def test_no_key_means_no_header() -> None:
    headers: dict[str, str] = {}
    _provider().authenticate(headers)
    assert "authorization" not in headers


def test_api_key_is_not_exposed_in_repr() -> None:
    """A SecretStr keeps the credential out of tracebacks and log dumps."""
    settings_key = SecretStr("sk-super-secret")
    assert "sk-super-secret" not in repr(settings_key)
    assert "sk-super-secret" not in str(settings_key)


# -- Stream detection ------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b'{"stream": true}', "application/json", True),
        (b'{"stream": false}', "application/json", False),
        (b"{}", "application/json", False),
        (b'{"stream": "true"}', "application/json", False),  # string, not bool
        (b'{"stream": 1}', "application/json", False),
        (b'{"stream": true}', "multipart/form-data", False),  # not JSON, not parsed
        (b"not json at all", "application/json", False),
        (b"", "application/json", False),
        (b'[{"stream": true}]', "application/json", False),  # array, not object
    ],
)
def test_stream_detection(body: bytes, content_type: str, expected: bool) -> None:
    request = _provider().build_request(
        method="POST",
        path="chat/completions",
        query="",
        headers={"content-type": content_type},
        content=body,
    )
    assert request.stream is expected


def test_charset_suffixed_content_type_still_parses() -> None:
    request = _provider().build_request(
        method="POST",
        path="chat/completions",
        query="",
        headers={"content-type": "application/json; charset=utf-8"},
        content=b'{"stream": true, "model": "gpt-4o"}',
    )
    assert request.stream is True
    assert request.model == "gpt-4o"


def test_body_is_never_mutated_during_translation() -> None:
    body = b'{"model": "gpt-4o", "messages": []}'
    request = _provider().build_request(
        method="POST", path="chat/completions", query="", headers={}, content=body
    )
    assert request.content is body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"model": "gpt-4o"}', "gpt-4o"),
        (b'{"model": 42}', None),
        (b"{}", None),
        (b"garbage", None),
    ],
)
def test_model_extraction_is_defensive(body: bytes, expected: str | None) -> None:
    request = _provider().build_request(
        method="POST",
        path="chat/completions",
        query="",
        headers={"content-type": "application/json"},
        content=body,
    )
    assert request.model == expected


def test_parse_json_object_rejects_non_objects() -> None:
    assert parse_json_object(b"[1, 2]", "application/json") is None
    assert parse_json_object(b'"a string"', "application/json") is None
    assert parse_json_object(b"\xff\xfe", "application/json") is None


# -- Header policy ---------------------------------------------------------


def test_request_hop_by_hop_headers_are_dropped() -> None:
    filtered = filter_request_headers(
        {
            "Authorization": "Bearer sk-x",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Proxy-Authorization": "Basic abc",
            "Host": "gateway.test",
            "Content-Length": "42",
            "Expect": "100-continue",
            "OpenAI-Beta": "assistants=v2",
        }
    )

    assert dict(filtered.multi_items()) == {
        "authorization": "Bearer sk-x",
        "openai-beta": "assistants=v2",
    }


def test_unknown_request_headers_are_forwarded() -> None:
    """Denylist, not allowlist: headers invented after this code was written work."""
    filtered = filter_request_headers({"X-Some-Future-Header": "value"})
    assert filtered["x-some-future-header"] == "value"


def test_repeated_request_headers_survive() -> None:
    """A dict would collapse these, changing the request the provider sees."""
    filtered = filter_request_headers(
        httpx.Headers([("accept", "application/json"), ("accept", "text/event-stream")])
    )

    assert filtered.get_list("accept") == ["application/json", "text/event-stream"]


def test_response_headers_drop_body_describing_and_hop_by_hop() -> None:
    headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-length": "123",
            "content-encoding": "gzip",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "date": "Thu, 01 Jan 2026 00:00:00 GMT",
            "server": "cloudflare",
            "x-request-id": "req-upstream",
            "x-ratelimit-remaining-requests": "9",
        }
    )

    relayed = dict(filter_response_headers(headers))

    assert relayed == {
        "content-type": "application/json",
        "x-request-id": "req-upstream",
        "x-ratelimit-remaining-requests": "9",
    }


def test_repeated_response_headers_survive() -> None:
    headers = httpx.Headers([("set-cookie", "a=1"), ("set-cookie", "b=2")])
    assert list(filter_response_headers(headers)) == [("set-cookie", "a=1"), ("set-cookie", "b=2")]


# -- Registry --------------------------------------------------------------


def test_registry_returns_the_registered_provider() -> None:
    registry = ProviderRegistry()
    provider = _provider()
    registry.register(provider)

    assert registry.get("openai") is provider
    assert "openai" in registry
    assert registry.names == ("openai",)


def test_registry_rejects_duplicate_registration() -> None:
    registry = ProviderRegistry()
    registry.register(_provider())
    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(_provider())


def test_registry_raises_for_an_unknown_provider() -> None:
    with pytest.raises(ConfigurationError, match="not registered"):
        ProviderRegistry().get("gemini")
