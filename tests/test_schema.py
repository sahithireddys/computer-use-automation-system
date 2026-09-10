"""Pydantic construction/validation tests for artifacts/schema.py."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from artifacts.schema import (
    ActionType, CapabilityArtifact, InputParam, Locator, LocatorStrategy,
    OutputField, ParamType, RiskLevel, Step, TargetApp,
)
from tests.factories import member_balance_lookup_artifact, open_sub_account_artifact


def test_valid_artifact_round_trips_through_json():
    artifact = member_balance_lookup_artifact("http://127.0.0.1:5099")
    dumped = artifact.model_dump_json()
    restored = CapabilityArtifact.model_validate_json(dumped)
    assert restored == artifact


def test_open_sub_account_artifact_is_valid_and_irreversible():
    artifact = open_sub_account_artifact("http://127.0.0.1:5099")
    assert artifact.max_step_risk == RiskLevel.IRREVERSIBLE
    assert artifact.compute_max_risk() == RiskLevel.IRREVERSIBLE


def test_artifact_requires_at_least_one_step():
    with pytest.raises(ValidationError):
        CapabilityArtifact(
            capability_id="empty",
            name="Empty",
            description="No steps.",
            target=TargetApp(app_id="cu-servicer", base_url="http://127.0.0.1:5099"),
            goal_statement="n/a",
            input_parameters=[],
            output_schema=[],
            steps=[],
            success_checkpoint=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="OK"),
        )


def test_table_row_cell_locator_requires_no_extra_fields_but_accepts_nth_cell():
    loc = Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Savings", nth_cell=-1)
    assert loc.nth_cell == -1
    assert loc.role is None


def test_role_name_locator_can_carry_fallbacks_and_robustness_note():
    loc = Locator(
        strategy=LocatorStrategy.ROLE_NAME, role="button", value="Continue",
        fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Continue")],
        robustness_note="Breaks if the label is re-worded.",
    )
    assert len(loc.fallbacks) == 1
    assert loc.fallbacks[0].strategy == LocatorStrategy.TEXT_CONTENT


def test_step_defaults_to_safe_risk_and_5s_timeout():
    step = Step(step_id="s0", action=ActionType.NAVIGATE, description="go", value_template="/")
    assert step.risk_level == RiskLevel.SAFE
    assert step.timeout_ms == 5000


def test_invalid_enum_value_rejected():
    with pytest.raises(ValidationError):
        InputParam(name="x", type="not-a-real-type", description="bad")


def test_output_field_requires_name_type_description():
    with pytest.raises(ValidationError):
        OutputField(type=ParamType.STRING, description="missing name")
