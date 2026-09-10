"""
Test-only capability artifact builders.

`member_balance_lookup_artifact` mirrors what the real LLM discovery run
produces (see HANDOFF.md / scripts/run_agent.py) -- kept here, not under
/capabilities/, so tests don't depend on that real run having happened yet.
`open_sub_account_artifact` mirrors the hand-authored
capabilities/open-sub-account.json (reconstructed here rather than loaded
from disk so schema-validity is exercised independently of that file).
"""
from __future__ import annotations

from artifacts.schema import (
    ActionType, BusinessOutcome, CapabilityArtifact, InputParam, Locator,
    LocatorStrategy, OutputField, ParamType, RiskLevel, Step, TargetApp,
)


def member_balance_lookup_artifact(base_url: str) -> CapabilityArtifact:
    return CapabilityArtifact(
        capability_id="member-balance-lookup",
        name="Member Balance Lookup",
        description="Look up a credit-union member's savings balance by member ID.",
        target=TargetApp(app_id="cu-servicer", base_url=base_url, entry_path="/"),
        goal_statement="Look up the member's savings balance given a member ID.",
        input_parameters=[
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="The member ID to search for.", example="12345"),
        ],
        output_schema=[
            OutputField(name="savings_balance", type=ParamType.STRING,
                        description="The member's savings account balance, as displayed."),
        ],
        steps=[
            Step(step_id="s0", action=ActionType.NAVIGATE, description="Navigate to entry point",
                 value_template="/"),
            Step(step_id="s1", action=ActionType.FILL, description="Fill member ID search box",
                 locator=Locator(
                     strategy=LocatorStrategy.ROLE_NAME, role="textbox", value="Enter member ID",
                     fallbacks=[Locator(strategy=LocatorStrategy.PLACEHOLDER, value="Enter member ID")],
                 ),
                 value_template="{{member_id}}", risk_level=RiskLevel.SAFE),
            Step(step_id="s2", action=ActionType.CLICK, description="Submit member lookup",
                 locator=Locator(
                     strategy=LocatorStrategy.ROLE_NAME, role="button", value="Look Up Member",
                     fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Look Up Member")],
                 ),
                 risk_level=RiskLevel.SAFE),
            Step(step_id="s3", action=ActionType.EXTRACT, description="Read the Savings balance cell",
                 locator=Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Savings", nth_cell=-1),
                 extract_as="savings_balance", risk_level=RiskLevel.SAFE),
        ],
        business_outcomes=[
            BusinessOutcome(
                name="member_not_found", description="No record exists for the given member ID.",
                detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="No member record found"),
                outputs={"found": False},
            ),
            BusinessOutcome(
                name="permission_denied", description="The member record is restricted.",
                detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Access denied."),
                outputs={"found": False, "restricted": True},
            ),
        ],
        success_checkpoint=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Member Record"),
        allowed_domains=["127.0.0.1"],
        max_step_risk=RiskLevel.SAFE,
    )


def open_sub_account_artifact(base_url: str) -> CapabilityArtifact:
    return CapabilityArtifact(
        capability_id="open-sub-account",
        name="Open Sub Account",
        description="Open a new sub-account for an existing member. IRREVERSIBLE.",
        target=TargetApp(app_id="cu-servicer", base_url=base_url,
                          entry_path="/member/{member_id}/open-account"),
        goal_statement="Open a new sub-account of the given type for the given member.",
        input_parameters=[
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="Existing member ID.", example="12345"),
            InputParam(name="account_type", type=ParamType.STRING, required=True,
                       description="One of 'Savings', 'Checking', 'Money Market'.", example="Savings"),
            InputParam(name="initial_deposit", type=ParamType.STRING, required=True,
                       description="Initial deposit in dollars, e.g. '25.00'.", example="25.00"),
        ],
        output_schema=[
            OutputField(name="new_account_id", type=ParamType.STRING,
                        description="The newly created sub-account's ID."),
        ],
        steps=[
            Step(step_id="s0", action=ActionType.NAVIGATE,
                 description="Navigate to this member's open-sub-account form",
                 value_template="member/{{member_id}}/open-account", risk_level=RiskLevel.SAFE),
            Step(step_id="s1", action=ActionType.SELECT_OPTION,
                 description="Choose the sub-account type",
                 locator=Locator(strategy=LocatorStrategy.CSS, value="select[name='account_type']"),
                 value_template="{{account_type}}", risk_level=RiskLevel.REVERSIBLE),
            Step(step_id="s2", action=ActionType.FILL,
                 description="Fill the initial deposit amount",
                 locator=Locator(strategy=LocatorStrategy.PLACEHOLDER, value="e.g. 25.00"),
                 value_template="{{initial_deposit}}", risk_level=RiskLevel.REVERSIBLE),
            Step(step_id="s3", action=ActionType.CLICK,
                 description="Submit the form to reach the confirmation screen",
                 locator=Locator(
                     strategy=LocatorStrategy.ROLE_NAME, role="button", value="Continue",
                     fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Continue")],
                 ),
                 risk_level=RiskLevel.REVERSIBLE),
            Step(step_id="s4", action=ActionType.CLICK,
                 description="Confirm opening the sub-account -- irreversible",
                 locator=Locator(
                     strategy=LocatorStrategy.ROLE_NAME, role="button", value="Confirm & Open Account",
                     fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Confirm & Open Account")],
                 ),
                 risk_level=RiskLevel.IRREVERSIBLE),
            Step(step_id="s5", action=ActionType.EXTRACT,
                 description="Read the newly created account ID",
                 locator=Locator(strategy=LocatorStrategy.CSS, value="#new-account-id"),
                 extract_as="new_account_id", risk_level=RiskLevel.SAFE),
        ],
        business_outcomes=[
            BusinessOutcome(
                name="member_not_found", description="No record exists for the given member ID.",
                detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="No member record found"),
                outputs={"opened": False, "reason": "member_not_found"},
            ),
            BusinessOutcome(
                name="permission_denied", description="The member record is restricted.",
                detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Access denied."),
                outputs={"opened": False, "reason": "permission_denied"},
            ),
            BusinessOutcome(
                name="invalid_deposit_amount",
                description="Requested initial deposit is below the $5.00 minimum.",
                detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT,
                                   value="Initial deposit must be at least $5.00."),
                outputs={"opened": False, "reason": "deposit_below_minimum"},
            ),
        ],
        success_checkpoint=Locator(strategy=LocatorStrategy.TEXT_CONTENT,
                                    value="Sub-account opened successfully."),
        allowed_domains=["127.0.0.1"],
        max_step_risk=RiskLevel.IRREVERSIBLE,
    )
