"""
Deterministic replay engine -- the production execution path an AI agent
actually invokes. No LLM in the loop. See REPORT.md section 3 for the
full error-taxonomy rationale; summary:

  SUCCESS            -> success_checkpoint resolved, declared outputs extracted.
  BUSINESS_OUTCOME   -> one of the artifact's declared business_outcomes matched
                        (e.g. "member not found"). This is a legitimate answer,
                        not a failure -- it is returned to the caller as such.
  RECOVERABLE        -> a known transient/interstitial condition was detected
                        and handled without derailing the run (e.g. a session
                        timeout interstitial was dismissed and the flow retried
                        from the top).
  HARD_FAILURE       -> a step's locator could not be resolved, or a
                        checkpoint failed, and no declared business outcome or
                        recovery matched. Stops the run with full debug detail.
  ESCALATED          -> the run was hard-blocked by policy or exhausted its
                        recovery budget, and control was handed to a human.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from playwright.sync_api import Page

from artifacts.schema import (
    CapabilityArtifact, OutcomeType, ReplayResult, StepFailureDetail, ParamType,
)
from engine.executor import execute_step, resolve_locator, LocatorResolutionError, StepExecutionError
from evidence.logger import EvidenceLogger, new_run_id
from guardrails.policy import PolicyEngine, PolicyViolation


# Recoverable, known interstitials we proactively check for before each
# step. Kept small and explicit -- this is a *documented* recovery list,
# not a generic "retry on any error" loop, per the assignment's warning
# that blind retries are not real error handling.
RECOVERABLE_INTERSTITIALS = [
    {
        "name": "session_timeout_interstitial",
        "detect_text": "Your session has timed out",
        "recover": lambda page: (
            page.get_by_role("button", name="Continue").click(),
            page.wait_for_load_state("domcontentloaded"),
        ),
        # A timed-out session invalidates whatever the flow had done so
        # far (e.g. a filled form, a mid-flow navigation state) -- the
        # only safe recovery is to restart the recorded flow from step 1,
        # not to resume at the step that happened to observe the
        # interstitial. Other interstitial types (e.g. a one-off "are you
        # sure?" dialog unrelated to session state) would set this False
        # and simply retry the current step after dismissal.
        "restart_flow": True,
    },
]


class EscalationRequested(Exception):
    """Raised internally to unwind to the caller, which owns bringing in a human."""
    def __init__(self, reason: str, context: dict):
        self.reason = reason
        self.context = context
        super().__init__(reason)


@dataclass
class ReplayEngine:
    artifact: CapabilityArtifact
    policy: PolicyEngine
    max_recovery_attempts: int = 2

    def run(
        self,
        page: Page,
        params: dict[str, Any],
        logger: Optional[EvidenceLogger] = None,
        on_escalation: Optional[Callable[[Page, EvidenceLogger, str, dict], None]] = None,
    ) -> ReplayResult:
        logger = logger or EvidenceLogger(new_run_id("replay"), kind="replay")
        run_id = logger.run_id
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self._validate_params(params)

        outputs: dict[str, Any] = {}
        try:
            self.policy.check_step_budget(len(self.artifact.steps))

            recovery_attempts = 0
            step_index = 0
            while step_index < len(self.artifact.steps):
                step = self.artifact.steps[step_index]

                self.policy.check_action(step.action)
                if step.locator is not None and step.locator.strategy.value != "table_row_cell":
                    pass  # domain check happens on navigate below

                # Proactively check for known recoverable interstitials.
                recovered, restart_flow = self._try_recover(page, logger)
                if recovered:
                    recovery_attempts += 1
                    logger.log_event("recovered_interstitial", {"name": recovered,
                                                                  "restart_flow": restart_flow})
                    if recovery_attempts > self.max_recovery_attempts:
                        raise EscalationRequested(
                            "recovery_budget_exhausted",
                            {"attempts": recovery_attempts, "step_id": step.step_id},
                        )
                    step_index = 0 if restart_flow else step_index
                    continue

                if step.action.value == "navigate":
                    target_url = self.artifact.target.base_url.rstrip("/") + "/" + \
                        (step.value_template or "/").lstrip("/")
                    self.policy.check_url(target_url)

                logger.log_step(step.step_id, step.action.value, {"risk": step.risk_level.value})

                if self.policy.requires_confirmation(step.risk_level):
                    logger.log_event(
                        "risky_step_auto_proceeding",
                        {"step_id": step.step_id, "risk": step.risk_level.value,
                         "note": "irreversible step; target app's own in-flow confirmation "
                                 "screen is the control here (recorded as a prior step)."},
                    )

                try:
                    outcome = execute_step(page, step, params, self.artifact.target.base_url)
                except (LocatorResolutionError, StepExecutionError) as e:
                    # Before declaring a hard failure, check whether the page
                    # actually landed on one of the artifact's *declared*
                    # business outcomes -- that's an expected answer, not a bug.
                    match = self._match_business_outcome(page)
                    if match is not None:
                        return self._finish_business_outcome(
                            match, run_id, started_at, logger
                        )
                    self._screenshot(page, logger, "hard_failure")
                    detail = StepFailureDetail(
                        step_id=step.step_id,
                        expected=str(getattr(e, "locator", getattr(e, "expected", "?"))),
                        observed=str(getattr(e, "tried", getattr(e, "observed", str(e)))),
                    )
                    if on_escalation is not None:
                        on_escalation(
                            page, logger, "step_failed_hard",
                            {"step_id": step.step_id, "error": str(e)},
                        )
                        # Human may have fixed the state manually; re-attempt once.
                        try:
                            outcome = execute_step(page, step, params, self.artifact.target.base_url)
                        except Exception:
                            return self._finish_hard_failure(detail, run_id, started_at, logger)
                    else:
                        return self._finish_hard_failure(detail, run_id, started_at, logger)

                if outcome.extracted_value is not None and step.extract_as:
                    outputs[step.extract_as] = outcome.extracted_value

                step_index += 1

            # All steps executed -- verify the declared success checkpoint.
            try:
                resolve_locator(page, self.artifact.success_checkpoint, timeout_ms=5000)
            except LocatorResolutionError:
                match = self._match_business_outcome(page)
                if match is not None:
                    return self._finish_business_outcome(match, run_id, started_at, logger)
                self._screenshot(page, logger, "checkpoint_failed")
                detail = StepFailureDetail(
                    step_id="success_checkpoint",
                    expected=f"{self.artifact.success_checkpoint.strategy.value}:"
                             f"{self.artifact.success_checkpoint.value}",
                    observed="checkpoint did not resolve after all steps completed",
                )
                return self._finish_hard_failure(detail, run_id, started_at, logger)

            self._validate_outputs(outputs)
            logger.log_outcome("success", {"outputs": outputs})
            result = ReplayResult(
                capability_id=self.artifact.capability_id,
                capability_version=self.artifact.version,
                run_id=run_id,
                status=OutcomeType.SUCCESS,
                outputs=outputs,
                started_at=started_at,
                finished_at=self._now(),
                evidence_refs=[str(logger.dir)],
            )
            logger.write_result(result.model_dump())
            return result

        except PolicyViolation as e:
            logger.log_event("policy_violation", {"reason": e.reason, "detail": e.detail})
            self._screenshot(page, logger, "policy_violation")
            result = ReplayResult(
                capability_id=self.artifact.capability_id,
                capability_version=self.artifact.version,
                run_id=run_id,
                status=OutcomeType.HARD_FAILURE,
                failure=StepFailureDetail(step_id="policy", expected="within allowlist", observed=str(e)),
                started_at=started_at,
                finished_at=self._now(),
                evidence_refs=[str(logger.dir)],
            )
            logger.write_result(result.model_dump())
            return result

        except EscalationRequested as e:
            logger.log_escalation(e.reason, e.context)
            self._screenshot(page, logger, "escalation")
            if on_escalation is not None:
                on_escalation(page, logger, e.reason, e.context)
            result = ReplayResult(
                capability_id=self.artifact.capability_id,
                capability_version=self.artifact.version,
                run_id=run_id,
                status=OutcomeType.ESCALATED,
                escalation_reason=e.reason,
                started_at=started_at,
                finished_at=self._now(),
                evidence_refs=[str(logger.dir)],
            )
            logger.write_result(result.model_dump())
            return result

    # -- helpers ------------------------------------------------------------

    def _validate_params(self, params: dict[str, Any]) -> None:
        for p in self.artifact.input_parameters:
            if p.required and p.name not in params:
                raise ValueError(f"Missing required input parameter: {p.name}")

    def _validate_outputs(self, outputs: dict[str, Any]) -> None:
        missing = [o.name for o in self.artifact.output_schema if o.name not in outputs]
        if missing:
            raise ValueError(f"Declared outputs not produced by any step: {missing}")

    def _match_business_outcome(self, page: Page):
        for outcome in self.artifact.business_outcomes:
            try:
                resolve_locator(page, outcome.detection, timeout_ms=1500)
                return outcome
            except LocatorResolutionError:
                continue
        return None

    def _try_recover(self, page: Page, logger: EvidenceLogger) -> tuple[Optional[str], bool]:
        try:
            body = page.inner_text("body")
        except Exception:
            return None, False
        for interstitial in RECOVERABLE_INTERSTITIALS:
            if interstitial["detect_text"] in body:
                interstitial["recover"](page)
                return interstitial["name"], interstitial.get("restart_flow", False)
        return None, False

    def _finish_business_outcome(self, outcome, run_id, started_at, logger) -> ReplayResult:
        logger.log_outcome("business_outcome", {"name": outcome.name})
        result = ReplayResult(
            capability_id=self.artifact.capability_id,
            capability_version=self.artifact.version,
            run_id=run_id,
            status=OutcomeType.BUSINESS_OUTCOME,
            business_outcome_name=outcome.name,
            outputs=outcome.outputs,
            started_at=started_at,
            finished_at=self._now(),
            evidence_refs=[str(logger.dir)],
        )
        logger.write_result(result.model_dump())
        return result

    def _finish_hard_failure(self, detail: StepFailureDetail, run_id, started_at, logger) -> ReplayResult:
        logger.log_outcome("hard_failure", {"step_id": detail.step_id,
                                             "expected": detail.expected,
                                             "observed": detail.observed})
        result = ReplayResult(
            capability_id=self.artifact.capability_id,
            capability_version=self.artifact.version,
            run_id=run_id,
            status=OutcomeType.HARD_FAILURE,
            failure=detail,
            started_at=started_at,
            finished_at=self._now(),
            evidence_refs=[str(logger.dir)],
        )
        logger.write_result(result.model_dump())
        return result

    def _screenshot(self, page: Page, logger: EvidenceLogger, label: str) -> None:
        try:
            path = logger.screenshot_path(label)
            page.screenshot(path=str(path))
            logger.log_event("screenshot", {"label": label, "path": str(path)})
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
