"""
Action execution layer, shared verbatim by the discovery agent and the
replay engine. This is deliberate: if discovery and replay used
different code to click a button, "recorded once, works forever" would
be a lie -- replay would be exercising a different code path than the
one that was actually validated during discovery.

Locator resolution tries the primary strategy, then each fallback in
order, raising LocatorResolutionError only if all of them fail to
resolve to exactly one visible element.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from playwright.sync_api import Page, Locator as PWLocator, TimeoutError as PWTimeoutError

from artifacts.schema import ActionType, Locator, Step


class LocatorResolutionError(Exception):
    def __init__(self, locator: Locator, tried: list[str]):
        self.locator = locator
        self.tried = tried
        super().__init__(
            f"Could not resolve locator (strategy={locator.strategy}, value={locator.value!r}); "
            f"tried: {tried}"
        )


class StepExecutionError(Exception):
    def __init__(self, step_id: str, expected: str, observed: str):
        self.step_id = step_id
        self.expected = expected
        self.observed = observed
        super().__init__(f"[{step_id}] expected {expected!r}, observed {observed!r}")


def _resolve_single(page: Page, loc: Locator, timeout_ms: int) -> Optional[PWLocator]:
    try:
        if loc.strategy.value == "role_name":
            pw_loc = page.get_by_role(loc.role or "button", name=loc.value, exact=False)
        elif loc.strategy.value == "placeholder":
            pw_loc = page.get_by_placeholder(loc.value, exact=False)
        elif loc.strategy.value == "label_text":
            pw_loc = page.get_by_label(loc.value, exact=False)
        elif loc.strategy.value == "text_content":
            pw_loc = page.get_by_text(loc.value, exact=False)
        elif loc.strategy.value == "test_id":
            pw_loc = page.get_by_test_id(loc.value)
        elif loc.strategy.value == "css":
            pw_loc = page.locator(loc.value)
        elif loc.strategy.value == "table_row_cell":
            # `has_text` matches on the row's full (recursive) text content,
            # so on nested-table legacy markup a big outer <tr> that simply
            # *wraps* the real data row also matches -- and since it's the
            # ancestor, it comes first in document order. Taking `.first`
            # here silently resolved to that wrapper instead of the actual
            # row (verified against this app's own member_detail.html: an
            # outer layout <tr> contains the whole page and thus contains
            # "Savings" too). Disambiguate by picking the SMALLEST matching
            # row (fewest <td> descendants) -- the most specific match,
            # which is how a human reads "find the row for Savings" in a
            # nested legacy table.
            rows = page.locator("tr", has_text=loc.value)
            n = rows.count()
            if n == 0:
                return None
            best_cells = None
            best_count = None
            for i in range(n):
                candidate_cells = rows.nth(i).locator("td")
                c = candidate_cells.count()
                if c == 0:
                    continue
                if best_count is None or c < best_count:
                    best_cells = candidate_cells
                    best_count = c
            if best_cells is None:
                return None
            idx = loc.nth_cell if loc.nth_cell is not None else -1
            real_idx = idx if idx >= 0 else best_count + idx
            if not (0 <= real_idx < best_count):
                return None
            pw_loc = best_cells.nth(real_idx)
        else:
            return None

        pw_loc.first.wait_for(state="visible", timeout=timeout_ms)
        if pw_loc.count() >= 1:
            return pw_loc.first
        return None
    except PWTimeoutError:
        return None
    except Exception:
        return None


def resolve_locator(page: Page, loc: Locator, timeout_ms: int = 5000) -> PWLocator:
    tried = [f"{loc.strategy.value}:{loc.value}"]
    result = _resolve_single(page, loc, timeout_ms)
    if result is not None:
        return result
    for fb in loc.fallbacks:
        tried.append(f"{fb.strategy.value}:{fb.value}")
        result = _resolve_single(page, fb, timeout_ms)
        if result is not None:
            return result
    raise LocatorResolutionError(loc, tried)


def render_template(template: str, params: dict[str, Any]) -> str:
    out = template
    for k, v in params.items():
        out = out.replace(f"{{{{{k}}}}}", str(v))
    return out


@dataclass
class StepOutcome:
    step_id: str
    extracted_value: Optional[str] = None
    checkpoint_ok: Optional[bool] = None


def execute_step(page: Page, step: Step, params: dict[str, Any], base_url: str) -> StepOutcome:
    """
    Executes a single recorded step against the live page. Raises
    LocatorResolutionError / StepExecutionError on failure; the caller
    (replay engine or discovery loop) is responsible for classifying
    that into the outcome taxonomy (business outcome vs. recoverable
    vs. hard failure).
    """
    action = step.action

    if action == ActionType.NAVIGATE:
        target = render_template(step.value_template or "/", params)
        url = target if target.startswith("http") else base_url.rstrip("/") + "/" + target.lstrip("/")
        page.goto(url, timeout=step.timeout_ms)

    elif action == ActionType.FILL:
        loc = resolve_locator(page, step.locator, step.timeout_ms)
        value = render_template(step.value_template or "", params)
        loc.fill(value, timeout=step.timeout_ms)

    elif action == ActionType.CLICK:
        loc = resolve_locator(page, step.locator, step.timeout_ms)
        loc.click(timeout=step.timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=step.timeout_ms)

    elif action == ActionType.SELECT_OPTION:
        loc = resolve_locator(page, step.locator, step.timeout_ms)
        value = render_template(step.value_template or "", params)
        loc.select_option(label=value, timeout=step.timeout_ms)

    elif action == ActionType.WAIT_FOR:
        resolve_locator(page, step.locator, step.timeout_ms)

    elif action == ActionType.EXTRACT:
        loc = resolve_locator(page, step.locator, step.timeout_ms)
        attr = step.extract_attribute or "text"
        if attr == "text":
            value = loc.inner_text(timeout=step.timeout_ms).strip()
        elif attr == "value":
            value = loc.input_value(timeout=step.timeout_ms).strip()
        else:
            value = (loc.get_attribute(attr, timeout=step.timeout_ms) or "").strip()
        outcome = StepOutcome(step_id=step.step_id, extracted_value=value)
        _apply_checkpoint(page, step, outcome)
        return outcome

    elif action == ActionType.ASSERT_CHECKPOINT:
        pass  # checkpoint applied below for every action type

    outcome = StepOutcome(step_id=step.step_id)
    _apply_checkpoint(page, step, outcome)
    return outcome


def _apply_checkpoint(page: Page, step: Step, outcome: StepOutcome) -> None:
    if step.checkpoint is None:
        return
    try:
        resolve_locator(page, step.checkpoint, step.timeout_ms)
        outcome.checkpoint_ok = True
    except LocatorResolutionError:
        outcome.checkpoint_ok = False
        raise StepExecutionError(
            step.step_id,
            expected=f"checkpoint present: {step.checkpoint.strategy.value}:{step.checkpoint.value}",
            observed="checkpoint locator did not resolve",
        )
