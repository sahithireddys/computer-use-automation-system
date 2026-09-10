"""
Guardrail / policy layer.

Enforced identically during discovery (LLM-driven) and replay
(deterministic) -- the same PolicyEngine instance is consulted by both,
so "what the agent is allowed to do" is defined exactly once.

Design decisions (see /REPORT.md section 6):
  - Allowlist is domain+path-prefix based, plus an explicit action-type
    allowlist. Anything outside it is a hard block, not a warning.
  - Risk handling: SAFE actions proceed automatically. REVERSIBLE actions
    proceed but are logged prominently. IRREVERSIBLE actions require an
    explicit confirmation step to have occurred in-app (we don't invent
    our own confirmation UI -- we require the *target app's own*
    confirmation screen/click to be part of the recorded flow) AND are
    always eligible for human escalation if the agent is not highly
    confident. This mirrors how the mock app itself models "confirm
    before opening an account."
  - Redaction: never write full PII/secrets into artifacts or logs.
    Values considered sensitive (based on parameter name heuristics) are
    truncated/masked before they touch disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
import re

from artifacts.schema import ActionType, RiskLevel


@dataclass
class AllowlistEntry:
    domain: str
    path_prefixes: list[str] = field(default_factory=lambda: ["/"])


@dataclass
class PolicyConfig:
    allowed_domains: list[AllowlistEntry]
    allowed_actions: set[ActionType] = field(
        default_factory=lambda: {
            ActionType.NAVIGATE,
            ActionType.FILL,
            ActionType.CLICK,
            ActionType.SELECT_OPTION,
            ActionType.WAIT_FOR,
            ActionType.EXTRACT,
            ActionType.ASSERT_CHECKPOINT,
        }
    )
    # Risk levels that may proceed without a human confirmation step.
    auto_approve_risk: set[RiskLevel] = field(
        default_factory=lambda: {RiskLevel.SAFE, RiskLevel.REVERSIBLE}
    )
    max_steps: int = 40
    max_seconds: int = 180


class PolicyViolation(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# Heuristic set of parameter/field name fragments treated as sensitive.
_SENSITIVE_NAME_FRAGMENTS = (
    "ssn", "social_security", "password", "secret", "token", "api_key",
    "credit_card", "card_number", "cvv", "dob", "date_of_birth",
    "account_number", "pin",
)


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        for entry in self.config.allowed_domains:
            if host == entry.domain or host.endswith(f".{entry.domain}"):
                if any(parsed.path.startswith(p) for p in entry.path_prefixes):
                    return
        raise PolicyViolation(
            "domain_not_allowlisted", f"{url} is outside the configured allowlist"
        )

    def check_action(self, action: ActionType) -> None:
        if action not in self.config.allowed_actions:
            raise PolicyViolation("action_type_not_allowed", str(action))

    def requires_confirmation(self, risk_level: RiskLevel) -> bool:
        return risk_level not in self.config.auto_approve_risk

    def check_step_budget(self, step_count: int) -> None:
        if step_count > self.config.max_steps:
            raise PolicyViolation(
                "max_steps_exceeded", f"{step_count} > {self.config.max_steps}"
            )

    @staticmethod
    def is_sensitive_field(field_name: str) -> bool:
        lowered = field_name.lower()
        return any(frag in lowered for frag in _SENSITIVE_NAME_FRAGMENTS)

    @classmethod
    def redact(cls, field_name: str, value: str) -> str:
        """Redact a value for logging/artifact storage if its field name looks sensitive."""
        if not cls.is_sensitive_field(field_name):
            return value
        if not value:
            return value
        if len(value) <= 4:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 4) + value[-2:]

    @staticmethod
    def redact_dict(d: dict) -> dict:
        return {k: PolicyEngine.redact(k, str(v)) for k, v in d.items()}


def default_policy_for(base_url: str) -> PolicyEngine:
    """Convenience: a policy scoped to a single target app's own domain."""
    host = urlparse(base_url).hostname or base_url
    return PolicyEngine(PolicyConfig(allowed_domains=[AllowlistEntry(domain=host)]))
