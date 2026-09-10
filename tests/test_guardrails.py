"""guardrails/policy.py tests: allowlist enforcement and redaction."""
from __future__ import annotations

import pytest

from artifacts.schema import ActionType, RiskLevel
from guardrails.policy import (
    AllowlistEntry, PolicyConfig, PolicyEngine, PolicyViolation, default_policy_for,
)


def _policy(domain="127.0.0.1", prefixes=("/",)):
    return PolicyEngine(PolicyConfig(allowed_domains=[AllowlistEntry(domain=domain, path_prefixes=list(prefixes))]))


def test_url_within_allowlist_passes():
    policy = _policy()
    policy.check_url("http://127.0.0.1:5055/search?member_id=1")  # should not raise


def test_url_outside_domain_allowlist_blocked():
    policy = _policy()
    with pytest.raises(PolicyViolation):
        policy.check_url("http://evil.example.com/search")


def test_url_outside_path_prefix_blocked():
    policy = _policy(prefixes=["/search"])
    with pytest.raises(PolicyViolation):
        policy.check_url("http://127.0.0.1/member/1/open-account")


def test_subdomain_matches_allowlisted_domain():
    policy = _policy(domain="example.com")
    policy.check_url("http://api.example.com/")  # should not raise


def test_disallowed_action_type_blocked():
    config = PolicyConfig(allowed_domains=[AllowlistEntry(domain="127.0.0.1")],
                           allowed_actions={ActionType.NAVIGATE})
    policy = PolicyEngine(config)
    with pytest.raises(PolicyViolation):
        policy.check_action(ActionType.CLICK)


def test_step_budget_enforced():
    policy = _policy()
    policy.check_step_budget(policy.config.max_steps)  # exactly at budget: fine
    with pytest.raises(PolicyViolation):
        policy.check_step_budget(policy.config.max_steps + 1)


def test_requires_confirmation_matches_risk_level():
    policy = _policy()
    assert policy.requires_confirmation(RiskLevel.SAFE) is False
    assert policy.requires_confirmation(RiskLevel.REVERSIBLE) is False
    assert policy.requires_confirmation(RiskLevel.IRREVERSIBLE) is True


def test_is_sensitive_field_matches_known_fragments():
    assert PolicyEngine.is_sensitive_field("account_number") is True
    assert PolicyEngine.is_sensitive_field("ssn") is True
    assert PolicyEngine.is_sensitive_field("member_id") is False


def test_redact_masks_sensitive_values_but_keeps_shape():
    redacted = PolicyEngine.redact("api_key", "sk-1234567890")
    assert redacted != "sk-1234567890"
    assert redacted.startswith("sk")
    assert redacted.endswith("90")
    assert "*" in redacted


def test_redact_leaves_non_sensitive_values_untouched():
    assert PolicyEngine.redact("member_id", "12345") == "12345"


def test_redact_dict_applies_per_field():
    out = PolicyEngine.redact_dict({"member_id": "12345", "password": "hunter2222"})
    assert out["member_id"] == "12345"
    assert out["password"] != "hunter2222"


def test_default_policy_for_scopes_to_target_host():
    policy = default_policy_for("http://127.0.0.1:5055/")
    policy.check_url("http://127.0.0.1:5055/anything")
    with pytest.raises(PolicyViolation):
        policy.check_url("http://other-host/anything")
