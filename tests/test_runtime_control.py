"""The runtime kill switch, and that the policy reads it live."""

from __future__ import annotations

from gateway.optimizers import build_provider_policy
from gateway.optimizers.models import TransformationRequest
from gateway.providers import OpenAIProvider
from gateway.runtime import RuntimeControl
from tests.conftest import build_settings


def _chat_request() -> TransformationRequest:
    return TransformationRequest(
        method="POST",
        path="chat/completions",
        content_type="application/json",
        body=b"{}",
    )


def test_control_starts_from_seed_and_flips() -> None:
    control = RuntimeControl(optimization_enabled=True)
    assert control.optimization_enabled is True
    assert control.set_optimization_enabled(False) is False
    assert control.optimization_enabled is False
    assert control.set_optimization_enabled(True) is True


def test_policy_reads_the_control_live() -> None:
    settings = build_settings()
    control = RuntimeControl(optimization_enabled=True)
    provider = OpenAIProvider(base_url="https://api.openai.com/v1", api_key=None)
    policy = build_provider_policy(settings, provider.endpoint_policy, control)
    request = _chat_request()

    assert policy.decide(request).optimize is True

    # Flip after the policy was built — no rebuild — and the very next decision changes.
    control.set_optimization_enabled(False)
    decision = policy.decide(request)
    assert decision.optimize is False
    assert decision.reason is not None
    assert decision.reason.value == "optimization_disabled"

    control.set_optimization_enabled(True)
    assert policy.decide(request).optimize is True


def test_policy_without_a_control_falls_back_to_settings() -> None:
    provider = OpenAIProvider(base_url="https://api.openai.com/v1", api_key=None)
    disabled = build_provider_policy(
        build_settings(optimization_enabled=False), provider.endpoint_policy
    )
    assert disabled.decide(_chat_request()).optimize is False

    enabled = build_provider_policy(
        build_settings(optimization_enabled=True), provider.endpoint_policy
    )
    assert enabled.decide(_chat_request()).optimize is True
