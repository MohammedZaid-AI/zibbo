"""The policy engine: *may* this request be transformed?

Separate from the pipeline on purpose. "Is this endpoint eligible" is a business
rule that changes with the product; "how do I clean HTML" is an algorithm. Mixing
them would mean every new endpoint touched the transformation code.

Rules are consulted in order and the first to return a decision wins. A rule that
returns ``None`` abstains. The engine's default, if every rule abstains, is to
optimize — but the endpoint rule never abstains, so nothing is optimized by
accident. That is the important property: an endpoint added to OpenAI tomorrow is
*not* optimized until someone allows it explicitly, because getting this wrong
means corrupting a fine-tuning upload.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from gateway.optimizers.models import SkipReason

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from gateway.config import Settings
    from gateway.optimizers.models import TransformationRequest
    from gateway.runtime import RuntimeControl


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    optimize: bool
    rule: str
    reason: SkipReason | None = None


_ALLOW = PolicyDecision(optimize=True, rule="allow")


class PolicyRule(ABC):
    """Abstains by returning ``None``."""

    name: ClassVar[str]

    @abstractmethod
    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None: ...


class OptimizationEnabledRule(PolicyRule):
    """A global kill switch. First rule, so it always wins.

    Reads a live callable rather than a captured bool: the plugin's enable/disable
    commands flip the runtime control at request time, and the change must take effect
    on the next request without rebuilding every provider's policy.
    """

    name: ClassVar[str] = "optimization-enabled"

    def __init__(self, is_enabled: Callable[[], bool]) -> None:
        self._is_enabled = is_enabled

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        del request
        if not self._is_enabled():
            return PolicyDecision(False, self.name, SkipReason.DISABLED)
        return None


class MethodRule(PolicyRule):
    """Only request bodies are optimizable, and only POST carries one here."""

    name: ClassVar[str] = "method"

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        if request.method.upper() != "POST":
            return PolicyDecision(False, self.name, SkipReason.METHOD_NOT_ELIGIBLE)
        return None


class BodySizeRule(PolicyRule):
    """Empty bodies have nothing to do; enormous ones are not worth the memory."""

    name: ClassVar[str] = "body-size"

    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        if not request.body:
            return PolicyDecision(False, self.name, SkipReason.EMPTY_BODY)
        if len(request.body) > self._max_bytes:
            return PolicyDecision(False, self.name, SkipReason.BODY_TOO_LARGE)
        return None


class ContentTypeRule(PolicyRule):
    """Only JSON request bodies. Multipart uploads and raw binary are opaque."""

    name: ClassVar[str] = "content-type"

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        essence = request.content_type.split(";", 1)[0].strip().lower()
        if essence != "application/json":
            return PolicyDecision(False, self.name, SkipReason.CONTENT_TYPE_NOT_ELIGIBLE)
        return None


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    """Which of a provider's endpoints carry optimizable prose.

    **Data, supplied by the provider.** The gateway core cannot know that OpenAI
    calls it ``chat/completions`` and Anthropic calls it ``messages``, and it must
    not learn: that knowledge belongs in the provider module and nowhere else.

    Denied entries are listed explicitly even though the allowlist already excludes
    them, so the intent survives someone widening the allowlist later.
    """

    allowed: frozenset[str] = frozenset()
    allowed_prefixes: tuple[str, ...] = ()
    allowed_suffixes: tuple[str, ...] = ()
    """For providers that encode the operation in the path, like Gemini's
    ``models/gemini-2.0-flash:generateContent``."""

    denied_prefixes: tuple[str, ...] = ()

    def permits(self, path: str) -> bool:
        normalized = path.strip("/").lower()

        for prefix in self.denied_prefixes:
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                return False

        if normalized in self.allowed:
            return True
        if self.allowed_prefixes and normalized.startswith(self.allowed_prefixes):
            return True
        return bool(self.allowed_suffixes and normalized.endswith(self.allowed_suffixes))


class EndpointRule(PolicyRule):
    """An allowlist. Never abstains."""

    name: ClassVar[str] = "endpoint"

    def __init__(self, policy: EndpointPolicy) -> None:
        self._policy = policy

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        if self._policy.permits(request.path):
            return _ALLOW
        return PolicyDecision(False, self.name, SkipReason.ENDPOINT_NOT_ELIGIBLE)


class PolicyEngine:
    """Evaluates rules in order; the first decision wins."""

    def __init__(self, rules: Sequence[PolicyRule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        endpoint_policy: EndpointPolicy,
        control: RuntimeControl | None = None,
    ) -> PolicyEngine:
        # The runtime control is the live source of truth for the kill switch, seeded
        # from settings at startup. When absent (unit tests build a policy in
        # isolation), fall back to the static setting.
        static_enabled = settings.optimization_enabled

        def enabled_check() -> bool:
            return control.optimization_enabled if control is not None else static_enabled

        return cls(
            [
                OptimizationEnabledRule(enabled_check),
                MethodRule(),
                BodySizeRule(max_bytes=settings.optimization_max_body_bytes),
                ContentTypeRule(),
                EndpointRule(endpoint_policy),
            ]
        )

    def decide(self, request: TransformationRequest) -> PolicyDecision:
        for rule in self._rules:
            decision = rule.evaluate(request)
            if decision is not None:
                return decision
        return _ALLOW

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self._rules)
