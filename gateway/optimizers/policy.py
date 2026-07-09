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
from typing import TYPE_CHECKING, ClassVar, Final

from gateway.optimizers.models import SkipReason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gateway.config import Settings
    from gateway.optimizers.models import TransformationRequest


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
    """A global kill switch. First rule, so it always wins."""

    name: ClassVar[str] = "optimization-enabled"

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        del request
        if not self._enabled:
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


class EndpointRule(PolicyRule):
    """An allowlist. Never abstains.

    Denied endpoints are listed explicitly as well, even though the allowlist
    already excludes them, so the intent survives someone widening the allowlist.
    """

    name: ClassVar[str] = "endpoint"

    ALLOWED: Final[frozenset[str]] = frozenset({"chat/completions", "responses", "assistants"})
    ALLOWED_PREFIXES: Final[tuple[str, ...]] = ("threads/",)

    # Corrupting any of these would be catastrophic and silent.
    DENIED_PREFIXES: Final[tuple[str, ...]] = (
        "files",
        "uploads",
        "audio/",
        "images/",
        "fine_tuning/",
        "batches",
        "embeddings",
        "moderations",
    )

    def evaluate(self, request: TransformationRequest) -> PolicyDecision | None:
        path = request.path.strip("/").lower()

        for prefix in self.DENIED_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return PolicyDecision(False, self.name, SkipReason.ENDPOINT_NOT_ELIGIBLE)

        if path in self.ALLOWED or path.startswith(self.ALLOWED_PREFIXES):
            return _ALLOW

        return PolicyDecision(False, self.name, SkipReason.ENDPOINT_NOT_ELIGIBLE)


class PolicyEngine:
    """Evaluates rules in order; the first decision wins."""

    def __init__(self, rules: Sequence[PolicyRule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_settings(cls, settings: Settings) -> PolicyEngine:
        return cls(
            [
                OptimizationEnabledRule(enabled=settings.optimization_enabled),
                MethodRule(),
                BodySizeRule(max_bytes=settings.optimization_max_body_bytes),
                ContentTypeRule(),
                EndpointRule(),
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
