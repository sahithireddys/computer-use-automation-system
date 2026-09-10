"""
replay/engine.py tests: outcome classification for both capabilities,
against the real mock app (no mocked Playwright/HTTP) -- the same
guarantee the assignment asks for (deterministic replay, real error
taxonomy), just pointed at a disposable test port and evidence dir.
"""
from __future__ import annotations

from artifacts.schema import OutcomeType
from engine.operator_console import run_operator_console
from evidence.logger import EvidenceLogger, new_run_id
from guardrails.policy import default_policy_for
from replay.engine import ReplayEngine
from tests.factories import member_balance_lookup_artifact, open_sub_account_artifact


def _engine_and_logger(artifact, kind="replay"):
    policy = default_policy_for(artifact.target.base_url)
    return ReplayEngine(artifact=artifact, policy=policy), EvidenceLogger(new_run_id(kind), kind=kind)


# -- member-balance-lookup ---------------------------------------------------

def test_success_path_extracts_balance(page, base_url, isolated_evidence_root):
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(page, {"member_id": "12345"}, logger=logger)
    assert result.status == OutcomeType.SUCCESS
    assert result.outputs["savings_balance"] == "$1842.30"
    # the evidence run_id must match the actual evidence directory used
    assert result.run_id == logger.run_id
    assert (logger.dir / "result.json").exists()


def test_member_not_found_is_business_outcome_not_failure(page, base_url, isolated_evidence_root):
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(page, {"member_id": "00099"}, logger=logger)
    assert result.status == OutcomeType.BUSINESS_OUTCOME
    assert result.business_outcome_name == "member_not_found"
    assert result.outputs == {"found": False}


def test_restricted_member_is_permission_denied_business_outcome(page, base_url, isolated_evidence_root):
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(page, {"member_id": "99999"}, logger=logger)
    assert result.status == OutcomeType.BUSINESS_OUTCOME
    assert result.business_outcome_name == "permission_denied"


def test_recurring_session_timeout_exhausts_recovery_budget_and_escalates(
    page, base_url, isolated_evidence_root
):
    # member_id 00000 triggers the session-timeout interstitial on every
    # search unconditionally (see mock_app/app.py) -- the recovery loop
    # restarts from step 1 each time, and since it recurs forever this
    # must exhaust max_recovery_attempts and escalate rather than loop
    # forever or silently fail.
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    escalated = []

    def on_escalation(pg, lg, reason, context):
        escalated.append((reason, context))

    result = engine.run(page, {"member_id": "00000"}, logger=logger, on_escalation=on_escalation)
    assert result.status == OutcomeType.ESCALATED
    assert result.escalation_reason == "recovery_budget_exhausted"
    assert escalated and escalated[0][0] == "recovery_budget_exhausted"


def test_undeclared_interstitial_hard_failure_then_human_resume_succeeds(
    page, base_url, isolated_evidence_root
):
    # member_id 55555 hits an interstitial the artifact never declared
    # (verify_identity.html) -- not in RECOVERABLE_INTERSTITIALS, so the
    # extract step hard-fails. A human operator (scripted here for
    # reproducibility) clicks past it on the SAME page/session, and the
    # engine must retry the failed step once and succeed.
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)

    def on_escalation(pg, lg, reason, context):
        assert reason == "step_failed_hard"
        assert context["step_id"] == "s3"
        run_operator_console(pg, lg, reason, context, commands=["click Proceed to Record"])

    result = engine.run(page, {"member_id": "55555"}, logger=logger, on_escalation=on_escalation)
    assert result.status == OutcomeType.SUCCESS
    assert result.outputs["savings_balance"] == "$9730.50"


def test_hard_failure_without_escalation_handler_surfaces_failure_detail(
    page, base_url, isolated_evidence_root
):
    artifact = member_balance_lookup_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(page, {"member_id": "55555"}, logger=logger, on_escalation=None)
    assert result.status == OutcomeType.HARD_FAILURE
    assert result.failure is not None
    assert result.failure.step_id == "s3"


def test_missing_required_param_raises():
    import pytest as _pytest
    artifact = member_balance_lookup_artifact("http://127.0.0.1:5099")
    policy = default_policy_for(artifact.target.base_url)
    engine = ReplayEngine(artifact=artifact, policy=policy)
    with _pytest.raises(ValueError):
        engine._validate_params({})


# -- open-sub-account (IRREVERSIBLE) -----------------------------------------

def test_open_sub_account_below_minimum_deposit_is_business_outcome(
    page, base_url, isolated_evidence_root
):
    artifact = open_sub_account_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(
        page,
        {"member_id": "12346", "account_type": "Savings", "initial_deposit": "2.00"},
        logger=logger,
    )
    assert result.status == OutcomeType.BUSINESS_OUTCOME
    assert result.business_outcome_name == "invalid_deposit_amount"
    assert result.outputs["opened"] is False


def test_open_sub_account_full_irreversible_success(page, base_url, isolated_evidence_root):
    artifact = open_sub_account_artifact(base_url)
    engine, logger = _engine_and_logger(artifact)
    result = engine.run(
        page,
        {"member_id": "12346", "account_type": "Checking", "initial_deposit": "40.00"},
        logger=logger,
    )
    assert result.status == OutcomeType.SUCCESS
    assert result.outputs["new_account_id"].startswith("CHE-12346-")
