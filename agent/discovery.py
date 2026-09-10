"""
The discovery agent loop (assignment section 3.1).

Flow: observe (accessibility-tree snapshot) -> ask Claude to pick exactly
one tool call -> execute it against the live page via the *same* executor
module replay will later use -> record what happened as a Step -> repeat
until the model calls finish_success / finish_business_outcome / escalate,
or a stopping condition (max steps/timeout) is hit.

The raw model transcript (full messages list) is kept only in memory for
the duration of the run and written to evidence as a summarized decision
log (see EvidenceLogger.log_decision) -- NOT persisted verbatim, and never
becomes part of the artifact. This is the "decoupled from the raw model
transcript" requirement (3.2): the artifact is our own structured record
of what actually happened on the page, derived from -- but not equal to
-- the conversation that produced it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.sync_api import Page, sync_playwright

from agent.anthropic_client import call_claude, AnthropicError
from agent.tools import TOOL_DEFINITIONS
from artifacts.schema import (
    ActionType, BusinessOutcome, CapabilityArtifact, InputParam, Locator,
    LocatorStrategy, OutputField, ParamType, RiskLevel, Step, TargetApp,
)
from engine.executor import execute_step, LocatorResolutionError, StepExecutionError
from engine.observer import snapshot_page
from evidence.logger import EvidenceLogger, new_run_id
from guardrails.policy import PolicyEngine, PolicyViolation, default_policy_for


SYSTEM_PROMPT = """You are an automation discovery agent for a bank/credit-union \
back-office web application. Your job is to accomplish the stated goal by \
observing the page (given to you as a list of interactive elements with \
[index] role='...' name='...', plus visible text) and calling exactly one \
tool per turn to act.

Rules:
- You may only act within the target application; you have no other tools.
- Reference elements ONLY by the [index] shown in the most recent observation \
  -- indices are reassigned every turn, so always use the latest snapshot.
- To read tabular data (e.g. a balance shown in a report-style table), use \
  extract_table_cell with the visible row label text, not element indices \
  (table cells are not addressable as interactive elements).
- If you determine the goal cannot be completed because of a legitimate \
  business condition described in the UI (e.g. a "not found" or "access \
  denied" message), call finish_business_outcome -- this is a normal, \
  expected result, not a failure.
- If you get stuck (e.g. an unrecognized error, or you're not confident \
  enough to safely proceed), call escalate with a clear reason rather than \
  guessing.
- When you have the information/state the goal asked for, call finish_success \
  with a short piece of visible page text that confirms you actually reached \
  the right state (this becomes the automated checkpoint for future replays).
"""


@dataclass
class DiscoveryResult:
    status: str  # "success" | "business_outcome" | "escalated" | "stopped"
    artifact: Optional[CapabilityArtifact]
    run_id: str
    outputs: dict[str, Any] = field(default_factory=dict)
    business_outcome: Optional[str] = None
    escalation_reason: Optional[str] = None


class DiscoveryAgent:
    def __init__(
        self,
        capability_id: str,
        goal: str,
        base_url: str,
        input_params: dict[str, str],
        expected_outputs: list[str],
        max_steps: int = 25,
        headless: bool = True,
    ):
        self.capability_id = capability_id
        self.goal = goal
        self.base_url = base_url.rstrip("/")
        self.input_params = input_params
        self.expected_outputs = expected_outputs
        self.max_steps = max_steps
        self.headless = headless
        self.policy = default_policy_for(base_url)

    def run(self) -> DiscoveryResult:
        run_id = new_run_id("discovery")
        logger = EvidenceLogger(run_id, kind="discovery")
        logger.log_event("goal", {"goal": self.goal, "params": self.input_params,
                                   "expected_outputs": self.expected_outputs})

        recorded_steps: list[Step] = []
        outputs: dict[str, Any] = {}
        messages: list[dict[str, Any]] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            entry_url = self.base_url + "/"
            self.policy.check_url(entry_url)
            page.goto(entry_url)
            recorded_steps.append(
                Step(step_id="s0", action=ActionType.NAVIGATE,
                     description="Navigate to entry point", value_template="/")
            )
            logger.log_step("s0", "navigate", {"url": entry_url})

            result = self._loop(page, logger, messages, recorded_steps, outputs, run_id)
            browser.close()
            return result

    # -- main loop ------------------------------------------------------

    def _loop(self, page: Page, logger: EvidenceLogger, messages, recorded_steps,
               outputs, run_id) -> DiscoveryResult:
        for turn in range(1, self.max_steps + 1):
            try:
                self.policy.check_step_budget(turn)
            except PolicyViolation as e:
                logger.log_escalation("max_steps_exceeded", {"detail": str(e)})
                return DiscoveryResult(status="escalated", artifact=None, run_id=run_id,
                                        escalation_reason="max_steps_exceeded")

            obs_text, elements = snapshot_page(page)
            self._screenshot(page, logger, f"turn{turn:02d}")

            user_content = obs_text if turn > 1 else (
                f"GOAL: {self.goal}\n"
                f"Known input values you may need (reference by name if you fill a field "
                f"that clearly corresponds to one): {self.input_params}\n\n" + obs_text
            )
            messages.append({"role": "user", "content": user_content})

            try:
                response = call_claude(SYSTEM_PROMPT, messages, TOOL_DEFINITIONS)
            except AnthropicError as e:
                logger.log_event("llm_error", {"error": str(e)})
                return DiscoveryResult(status="escalated", artifact=None, run_id=run_id,
                                        escalation_reason=f"llm_error: {e}")

            assistant_blocks = response.get("content", [])
            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_use = next((b for b in assistant_blocks if b.get("type") == "tool_use"), None)
            reasoning_text = " ".join(
                b.get("text", "") for b in assistant_blocks if b.get("type") == "text"
            )
            if tool_use is None:
                logger.log_event("no_tool_call", {"reasoning": reasoning_text[:500]})
                continue

            logger.log_decision(reasoning_text[:500], {"tool": tool_use["name"], "input": tool_use["input"]})

            outcome = self._handle_tool_call(
                page, logger, tool_use, elements, recorded_steps, outputs
            )
            if outcome is not None:
                # A terminal outcome (success / business_outcome / escalate) was reached.
                _, terminal_result = outcome
                return terminal_result

            # Report the tool result back to the model so it can decide next.
            tool_result_text, is_error = self._last_tool_result
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": tool_result_text,
                    "is_error": is_error,
                }],
            })

        logger.log_event("stopped", {"reason": "max_steps_reached"})
        return DiscoveryResult(status="stopped", artifact=None, run_id=run_id,
                                escalation_reason="max_steps_reached")

    # -- tool dispatch ----------------------------------------------------

    def _handle_tool_call(self, page, logger, tool_use, elements, recorded_steps, outputs):
        name = tool_use["name"]
        args = tool_use["input"]
        self._last_tool_result = ("", False)

        try:
            if name == "navigate":
                path = args["path"]
                url = path if path.startswith("http") else self.base_url + "/" + path.lstrip("/")
                self.policy.check_url(url)
                page.goto(url)
                step = Step(step_id=f"s{len(recorded_steps)}", action=ActionType.NAVIGATE,
                            description=f"Navigate to {path}", value_template=path)
                recorded_steps.append(step)
                logger.log_step(step.step_id, "navigate", {"path": path})
                self._last_tool_result = (f"Navigated to {page.url}", False)

            elif name in ("fill", "click", "select_option"):
                idx = args["element_index"]
                if idx < 0 or idx >= len(elements):
                    self._last_tool_result = (f"Invalid element_index {idx}", True)
                    return None
                el = elements[idx]
                loc, fallback = self._locator_for(el)

                if name == "fill":
                    value = args["value"]
                    templated, tval = self._maybe_template(value)
                    page.get_by_role(el.role, name=el.name, exact=False).first.fill(value)
                    step = Step(step_id=f"s{len(recorded_steps)}", action=ActionType.FILL,
                                description=f"Fill {el.role} '{el.name}'", locator=loc,
                                value_template=tval)
                    recorded_steps.append(step)
                    logger.log_step(step.step_id, "fill", {"target": el.name})
                    self._last_tool_result = ("Filled.", False)

                elif name == "click":
                    risk = RiskLevel.IRREVERSIBLE if "confirm" in el.name.lower() else (
                        RiskLevel.REVERSIBLE if el.role in ("button",) else RiskLevel.SAFE
                    )
                    page.get_by_role(el.role, name=el.name, exact=False).first.click()
                    page.wait_for_load_state("domcontentloaded")
                    step = Step(step_id=f"s{len(recorded_steps)}", action=ActionType.CLICK,
                                description=f"Click {el.role} '{el.name}'", locator=loc,
                                risk_level=risk)
                    recorded_steps.append(step)
                    logger.log_step(step.step_id, "click", {"target": el.name, "risk": risk.value})
                    self._last_tool_result = (f"Clicked. New URL: {page.url}", False)

                else:  # select_option
                    option = args["option_label"]
                    page.get_by_role(el.role, name=el.name, exact=False).first.select_option(label=option)
                    step = Step(step_id=f"s{len(recorded_steps)}", action=ActionType.SELECT_OPTION,
                                description=f"Select '{option}' in {el.name}", locator=loc,
                                value_template=option)
                    recorded_steps.append(step)
                    logger.log_step(step.step_id, "select_option", {"target": el.name, "option": option})
                    self._last_tool_result = ("Selected.", False)

            elif name == "extract_table_cell":
                row_label = args["row_label"]
                output_name = args["output_name"]
                nth_cell = args.get("nth_cell", -1)
                loc = Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value=row_label, nth_cell=nth_cell)
                step = Step(step_id=f"s{len(recorded_steps)}", action=ActionType.EXTRACT,
                            description=f"Extract cell from row '{row_label}'", locator=loc,
                            extract_as=output_name)
                exec_outcome = execute_step(page, step, {}, self.base_url)
                recorded_steps.append(step)
                outputs[output_name] = exec_outcome.extracted_value
                logger.log_step(step.step_id, "extract", {"row_label": row_label,
                                                            "value": exec_outcome.extracted_value})
                self._last_tool_result = (f"Extracted: {exec_outcome.extracted_value}", False)

            elif name == "finish_success":
                checkpoint_text = args["success_checkpoint_text"]
                missing = [o for o in self.expected_outputs if o not in outputs]
                if missing:
                    self._last_tool_result = (
                        f"Cannot finish yet -- missing declared outputs: {missing}. "
                        f"Extract them first.", True,
                    )
                    return None
                logger.log_outcome("success", {"outputs": outputs, "checkpoint_text": checkpoint_text})
                artifact = self._build_artifact(recorded_steps, outputs, checkpoint_text)
                return ("success", DiscoveryResult(
                    status="success", artifact=artifact, run_id=logger.run_id,
                    outputs=outputs,
                ))

            elif name == "finish_business_outcome":
                bo_name = args["name"]
                description = args["description"]
                detection_text = args.get("detection_text", description)
                logger.log_outcome("business_outcome", {"name": bo_name, "description": description})
                artifact = self._build_artifact(
                    recorded_steps, outputs, success_checkpoint_text=None,
                    business_outcome=BusinessOutcome(
                        name=bo_name, description=description,
                        detection=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value=detection_text),
                        outputs={"found": False} if "not_found" in bo_name else {},
                    ),
                )
                return ("business_outcome", DiscoveryResult(
                    status="business_outcome", artifact=artifact, run_id=logger.run_id,
                    business_outcome=bo_name,
                ))

            elif name == "escalate":
                reason = args["reason"]
                logger.log_escalation(reason, {"turn_context": "discovery_stuck"})
                return ("escalate", DiscoveryResult(
                    status="escalated", artifact=None, run_id=logger.run_id,
                    escalation_reason=reason,
                ))

            else:
                self._last_tool_result = (f"Unknown tool {name}", True)

        except PolicyViolation as e:
            logger.log_event("policy_violation", {"reason": e.reason, "detail": e.detail})
            self._last_tool_result = (f"Blocked by policy: {e}", True)
        except (LocatorResolutionError, StepExecutionError) as e:
            self._last_tool_result = (f"Action failed: {e}", True)
        except Exception as e:  # noqa: BLE001 - surfaced to the model as a tool error, not swallowed
            self._last_tool_result = (f"Unexpected error: {e}", True)

        return None

    # -- helpers ------------------------------------------------------------

    def _locator_for(self, el) -> tuple[Locator, Optional[Locator]]:
        fallback = None
        if el.role in ("button", "link"):
            fallback = Locator(strategy=LocatorStrategy.TEXT_CONTENT, value=el.name)
        elif el.role in ("textbox", "searchbox"):
            fallback = Locator(strategy=LocatorStrategy.PLACEHOLDER, value=el.name)
        loc = Locator(
            strategy=LocatorStrategy.ROLE_NAME, role=el.role, value=el.name,
            fallbacks=[fallback] if fallback else [],
            robustness_note=f"Resolved via accessibility role+name during discovery; "
                             f"falls back to {fallback.strategy.value if fallback else 'none'}.",
        )
        return loc, fallback

    def _maybe_template(self, value: str) -> tuple[bool, str]:
        for pname, pval in self.input_params.items():
            if str(pval) == str(value):
                return True, "{{" + pname + "}}"
        return False, value

    def _screenshot(self, page, logger, label):
        try:
            path = logger.screenshot_path(label)
            page.screenshot(path=str(path))
        except Exception:
            pass

    def _build_artifact(self, steps, outputs, success_checkpoint_text,
                          business_outcome: Optional[BusinessOutcome] = None) -> CapabilityArtifact:
        input_parameters = [
            InputParam(name=k, type=ParamType.STRING, description=f"Input parameter '{k}'", example=str(v))
            for k, v in self.input_params.items()
        ]
        output_schema = [
            OutputField(name=k, type=ParamType.STRING, description=f"Extracted field '{k}'")
            for k in self.expected_outputs
        ]
        checkpoint = Locator(
            strategy=LocatorStrategy.TEXT_CONTENT,
            value=success_checkpoint_text or (business_outcome.detection.value if business_outcome else "OK"),
        )
        max_risk = RiskLevel.SAFE
        for s in steps:
            order = [RiskLevel.SAFE, RiskLevel.REVERSIBLE, RiskLevel.IRREVERSIBLE]
            if order.index(s.risk_level) > order.index(max_risk):
                max_risk = s.risk_level

        return CapabilityArtifact(
            capability_id=self.capability_id,
            name=self.capability_id.replace("-", " ").title(),
            description=f"Discovered capability for goal: {self.goal}",
            target=TargetApp(app_id="cu-servicer", base_url=self.base_url),
            goal_statement=self.goal,
            input_parameters=input_parameters,
            output_schema=output_schema,
            steps=steps,
            business_outcomes=[business_outcome] if business_outcome else [],
            success_checkpoint=checkpoint,
            allowed_domains=[e.domain for e in self.policy.config.allowed_domains],
            max_step_risk=max_risk,
            created_from_run_id=None,  # filled in by caller once evidence run_id is known
        )
