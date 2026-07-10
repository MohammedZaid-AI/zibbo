"""The policy engine decides what may be touched.

Getting this wrong is the worst failure mode in the product: silently corrupting a
fine-tuning file or an audio upload. So the endpoint rule is an allowlist that
never abstains.
"""

from __future__ import annotations

import pytest

from gateway.optimizers.models import SkipReason, TransformationRequest
from gateway.optimizers.policy import (
    BodySizeRule,
    ContentTypeRule,
    EndpointRule,
    MethodRule,
    OptimizationEnabledRule,
    PolicyEngine,
)
from gateway.optimizers.registry import TransformerRegistry
from gateway.providers.openai import OPENAI_ENDPOINTS
from tests.conftest import build_settings


def _request(
    path: str = "chat/completions",
    method: str = "POST",
    content_type: str = "application/json",
    body: bytes = b'{"a":1}',
) -> TransformationRequest:
    return TransformationRequest(method=method, path=path, content_type=content_type, body=body)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine.from_settings(build_settings(), OPENAI_ENDPOINTS)


# -- Allowed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["chat/completions", "/chat/completions", "responses", "assistants", "threads/abc/messages"],
)
def test_optimizable_endpoints_are_allowed(engine: PolicyEngine, path: str) -> None:
    assert engine.decide(_request(path=path)).optimize is True


# -- Denied ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "files",
        "files/file-abc",
        "uploads",
        "audio/transcriptions",
        "audio/speech",
        "images/generations",
        "images/edits",
        "fine_tuning/jobs",
        "batches",
        "embeddings",
        "moderations",
    ],
)
def test_binary_and_non_prose_endpoints_are_never_optimized(
    engine: PolicyEngine, path: str
) -> None:
    decision = engine.decide(_request(path=path))
    assert decision.optimize is False
    assert decision.reason is SkipReason.ENDPOINT_NOT_ELIGIBLE


def test_unknown_endpoints_default_to_not_optimizing(engine: PolicyEngine) -> None:
    """An endpoint OpenAI ships tomorrow is proxied, never optimized, until allowed."""
    decision = engine.decide(_request(path="some/brand/new/endpoint"))
    assert decision.optimize is False
    assert decision.reason is SkipReason.ENDPOINT_NOT_ELIGIBLE


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_only_post_carries_an_optimizable_body(engine: PolicyEngine, method: str) -> None:
    decision = engine.decide(_request(method=method))
    assert decision.reason is SkipReason.METHOD_NOT_ELIGIBLE


@pytest.mark.parametrize(
    "content_type",
    ["multipart/form-data; boundary=x", "application/octet-stream", "audio/mpeg", ""],
)
def test_only_json_bodies_are_optimized(engine: PolicyEngine, content_type: str) -> None:
    decision = engine.decide(_request(content_type=content_type))
    assert decision.reason is SkipReason.CONTENT_TYPE_NOT_ELIGIBLE


def test_json_content_type_with_charset_is_accepted(engine: PolicyEngine) -> None:
    assert engine.decide(_request(content_type="application/json; charset=utf-8")).optimize


def test_empty_bodies_are_skipped(engine: PolicyEngine) -> None:
    assert engine.decide(_request(body=b"")).reason is SkipReason.EMPTY_BODY


def test_oversized_bodies_are_skipped() -> None:
    engine = PolicyEngine.from_settings(
        build_settings(optimization_max_body_bytes=10), OPENAI_ENDPOINTS
    )
    assert engine.decide(_request(body=b"x" * 11)).reason is SkipReason.BODY_TOO_LARGE


def test_the_kill_switch_wins_over_everything() -> None:
    engine = PolicyEngine.from_settings(
        build_settings(optimization_enabled=False), OPENAI_ENDPOINTS
    )
    decision = engine.decide(_request())
    assert decision.optimize is False
    assert decision.reason is SkipReason.DISABLED


# -- Rule mechanics --------------------------------------------------------


def test_rules_abstain_by_returning_none() -> None:
    assert MethodRule().evaluate(_request(method="POST")) is None
    assert ContentTypeRule().evaluate(_request()) is None
    assert BodySizeRule(max_bytes=100).evaluate(_request()) is None
    assert OptimizationEnabledRule(enabled=True).evaluate(_request()) is None


def test_the_endpoint_rule_never_abstains() -> None:
    """Every other rule may abstain; this one must always decide."""
    rule = EndpointRule(OPENAI_ENDPOINTS)
    assert rule.evaluate(_request(path="chat/completions")) is not None
    assert rule.evaluate(_request(path="anything/else")) is not None


def test_the_first_rule_to_decide_wins() -> None:
    """Order is the contract: the kill switch is checked before the endpoint."""
    engine = PolicyEngine.from_settings(
        build_settings(optimization_enabled=False), OPENAI_ENDPOINTS
    )
    assert engine.decide(_request(path="files")).reason is SkipReason.DISABLED


def test_engine_is_extensible_with_a_custom_rule() -> None:
    from gateway.optimizers.policy import PolicyDecision, PolicyRule

    class BlockOneModel(PolicyRule):
        name = "block-tenant"

        def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
            if b"do-not-touch" in request.body:
                return PolicyDecision(False, self.name, SkipReason.DISABLED)
            return None

    engine = PolicyEngine([BlockOneModel(), EndpointRule(OPENAI_ENDPOINTS)])
    assert engine.decide(_request(body=b'{"m":"do-not-touch"}')).optimize is False
    assert engine.decide(_request(body=b'{"m":"fine"}')).optimize is True


def test_transformer_registry_rejects_duplicates() -> None:
    from gateway.errors import ConfigurationError
    from gateway.optimizers.transformers import TextTransformer

    registry = TransformerRegistry()
    registry.register(TextTransformer())
    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(TextTransformer())


def test_transformer_selection_respects_priority() -> None:
    """HTML must outrank plain text, or every HTML page would be normalized as prose."""
    from gateway.optimizers import build_transformer_registry
    from gateway.optimizers.models import ContentType, Detection
    from gateway.optimizers.options import OptimizerOptions

    registry = build_transformer_registry(OptimizerOptions())
    assert registry.names == ("html", "json", "text")

    html = registry.select("<p>x</p>", Detection(ContentType.HTML, 1.0, "t"))
    assert html is not None
    assert html.name == "html"


def test_no_transformer_claims_binary_content() -> None:
    from gateway.optimizers import build_transformer_registry
    from gateway.optimizers.models import ContentType, Detection
    from gateway.optimizers.options import OptimizerOptions

    registry = build_transformer_registry(OptimizerOptions())
    assert registry.select("x", Detection(ContentType.PDF, 1.0, "t")) is None
    assert registry.select("x", Detection(ContentType.IMAGE, 1.0, "t")) is None
    assert registry.select("x", Detection(ContentType.BINARY, 1.0, "t")) is None
