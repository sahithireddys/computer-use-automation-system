"""
The Capability Artifact schema.

This is the contract between three parties:
  - the discovery agent (LLM), which produces it after a successful run
  - the replay engine, which executes it deterministically
  - the calling AI agent, which invokes it by name with typed args and
    expects a typed, predictable result back

Design principles (see /REPORT.md section 2 for full rationale):
  1. Decoupled from the raw model transcript. The artifact records *what
     to do and how to verify it*, not *why the model chose it*. The
     transcript is evidence (see evidence/), not part of the contract.
  2. Every locator carries a *strategy* + *fallbacks*, not a bare
     selector -- because on legacy surfaces a single CSS selector is
     the single most common source of silent replay breakage.
  3. Steps distinguish safe vs. risky/irreversible actions so the
     guardrail layer can treat them differently without re-deriving
     that judgment from scratch at replay time.
  4. Business outcomes are declared *in the schema*, not discovered as
     exceptions at replay time -- "member not found" is a first-class,
     named result the artifact author (the discovery agent) is expected
     to have already encountered and documented.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    FILL = "fill"
    CLICK = "click"
    SELECT_OPTION = "select_option"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT_CHECKPOINT = "assert_checkpoint"


class LocatorStrategy(str, Enum):
    """
    Ordered (in general) from most to least robust on a legacy,
    no-test-id surface. `role_name` (accessibility tree: role + accessible
    name) is preferred because it survives markup/DOM restructuring as
    long as the *visible semantics* of the control don't change -- which
    is exactly the "stable UI, occasional runtime weirdness" world this
    system targets. `css` is a documented last resort.
    """
    ROLE_NAME = "role_name"          # e.g. role=button, name="Continue"
    PLACEHOLDER = "placeholder"      # accessible name derived from placeholder
    LABEL_TEXT = "label_text"        # associated <label> text
    TEXT_CONTENT = "text_content"    # visible text match (links, buttons)
    TEST_ID = "test_id"              # only when the surface actually has one
    TABLE_ROW_CELL = "table_row_cell"  # row containing `value` text -> Nth cell in that row
    CSS = "css"                      # last resort; flagged as low-robustness


class RiskLevel(str, Enum):
    SAFE = "safe"          # read-only or trivially reversible (e.g. a search)
    REVERSIBLE = "reversible"   # changes state but can be undone (e.g. draft save)
    IRREVERSIBLE = "irreversible"  # e.g. opening an account, posting a transaction


class OutcomeType(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"    # legitimate non-success result
    RECOVERABLE = "recoverable"              # handled transient condition
    HARD_FAILURE = "hard_failure"            # stop and surface for debugging
    ESCALATED = "escalated"                  # handed off to a human


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


# --------------------------------------------------------------------------
# Locators & steps
# --------------------------------------------------------------------------

class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str = Field(..., description="The role name, placeholder text, label text, etc.")
    role: Optional[str] = Field(
        default=None, description="ARIA/accessibility role, required when strategy=role_name"
    )
    fallbacks: list["Locator"] = Field(
        default_factory=list,
        description="Ordered fallback locators tried if the primary fails to resolve "
        "to exactly one element. Documents the robustness reasoning explicitly "
        "rather than leaving it implicit in code.",
    )
    robustness_note: Optional[str] = Field(
        default=None,
        description="Why this locator was chosen / what would break it (e.g. "
        "'breaks if the button label is re-worded; does not depend on DOM position').",
    )
    nth_cell: Optional[int] = Field(
        default=-1,
        description="Only used with strategy=table_row_cell: which <td> in the matched "
        "<tr> to target. Negative indices count from the end (-1 = last cell). This "
        "mirrors how a human operator reads a legacy report table -- 'find the row for "
        "Savings, read the rightmost column' -- and survives column reordering better "
        "than a fixed CSS nth-child selector would if a column were inserted, though it "
        "still breaks if columns are reordered relative to each other.",
    )


class Step(BaseModel):
    step_id: str
    action: ActionType
    description: str = Field(..., description="Human-readable purpose of this step.")
    locator: Optional[Locator] = Field(
        default=None, description="Required for fill/click/select_option/wait_for/extract."
    )
    value_template: Optional[str] = Field(
        default=None,
        description="Literal value or a '{{param_name}}' template referencing an input "
        "parameter, used for fill/select_option/navigate.",
    )
    extract_as: Optional[str] = Field(
        default=None, description="If action=extract, the output field name this populates."
    )
    extract_attribute: Optional[str] = Field(
        default=None,
        description="What to pull from the located element: 'text' (default), "
        "'value', or an attribute name.",
    )
    risk_level: RiskLevel = RiskLevel.SAFE
    timeout_ms: int = 5000
    checkpoint: Optional[Locator] = Field(
        default=None,
        description="Optional locator that must resolve after this step for it to be "
        "considered successful (e.g. confirming a navigation actually landed).",
    )


class BusinessOutcome(BaseModel):
    """
    A named, legitimate non-success result the artifact author already
    knows can happen -- e.g. 'member not found'. Declaring these here
    means the replay engine does not have to guess whether an unexpected
    page state is a crash or an expected answer.
    """
    name: str
    description: str
    detection: Locator = Field(
        ..., description="Locator that, if present, confirms this outcome occurred."
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Fixed/derived outputs to return to the caller for this outcome "
        "(e.g. {'found': false}).",
    )


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


# --------------------------------------------------------------------------
# Top-level artifact
# --------------------------------------------------------------------------

class TargetApp(BaseModel):
    app_id: str = Field(..., description="Stable identifier for the vendor app/product, "
                         "e.g. 'cu-servicer'. Used for cross-tenant reuse (see REPORT.md 4).")
    base_url: str
    entry_path: str = "/"
    vendor_version: Optional[str] = Field(
        default=None, description="Detected/declared version of the vendor product, if known."
    )


class CapabilityArtifact(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    capability_id: str = Field(..., description="Stable slug, e.g. 'member-balance-lookup'.")
    name: str
    description: str
    version: int = Field(1, description="Bumped on every re-recording/edit of this capability.")

    target: TargetApp
    goal_statement: str = Field(..., description="The natural-language goal this was recorded from.")

    input_parameters: list[InputParam]
    output_schema: list[OutputField]
    steps: list[Step]
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    success_checkpoint: Locator = Field(
        ..., description="Locator that must resolve for the run to count as SUCCESS."
    )

    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Domains/routes this capability is permitted to touch; enforced by "
        "the guardrail layer independently of the recorded steps.",
    )
    max_step_risk: RiskLevel = Field(
        default=RiskLevel.SAFE,
        description="Highest risk level present in `steps`, cached here for quick "
        "policy checks without re-scanning steps.",
    )

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_from_run_id: Optional[str] = Field(
        default=None, description="Evidence run ID of the discovery session that produced this."
    )
    status: Literal["draft", "approved"] = "draft"

    @field_validator("steps")
    @classmethod
    def _must_have_steps(cls, v: list[Step]) -> list[Step]:
        if not v:
            raise ValueError("A capability artifact must have at least one step.")
        return v

    def compute_max_risk(self) -> RiskLevel:
        order = [RiskLevel.SAFE, RiskLevel.REVERSIBLE, RiskLevel.IRREVERSIBLE]
        return max((s.risk_level for s in self.steps), key=order.index, default=RiskLevel.SAFE)


# --------------------------------------------------------------------------
# Replay result contract
# --------------------------------------------------------------------------

class StepFailureDetail(BaseModel):
    step_id: str
    expected: str
    observed: str


class ReplayResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    capability_id: str
    capability_version: int
    run_id: str
    status: OutcomeType
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome_name: Optional[str] = None
    failure: Optional[StepFailureDetail] = None
    escalation_reason: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    evidence_refs: list[str] = Field(
        default_factory=list, description="Paths to logs/screenshots/traces for this run."
    )
