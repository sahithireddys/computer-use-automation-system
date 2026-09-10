"""
engine/executor.py tests: locator resolution (including the
table_row_cell strategy) and step execution against the real mock app.
"""
from __future__ import annotations

import pytest

from artifacts.schema import ActionType, Locator, LocatorStrategy, Step
from engine.executor import (
    LocatorResolutionError, execute_step, render_template, resolve_locator,
)


def test_render_template_substitutes_params():
    assert render_template("member/{{member_id}}/open-account", {"member_id": "12345"}) == \
        "member/12345/open-account"


def test_render_template_leaves_unknown_placeholders_untouched():
    assert render_template("{{unknown}}", {}) == "{{unknown}}"


def test_role_name_locator_resolves_search_button(page, base_url):
    page.goto(base_url + "/")
    loc = Locator(strategy=LocatorStrategy.ROLE_NAME, role="button", value="Look Up Member")
    resolved = resolve_locator(page, loc)
    assert resolved.count() == 1


def test_locator_resolution_error_when_nothing_matches(page, base_url):
    page.goto(base_url + "/")
    loc = Locator(strategy=LocatorStrategy.ROLE_NAME, role="button", value="Nonexistent Button")
    with pytest.raises(LocatorResolutionError):
        resolve_locator(page, loc)


def test_locator_falls_back_when_primary_strategy_fails(page, base_url):
    page.goto(base_url + "/")
    loc = Locator(
        strategy=LocatorStrategy.ROLE_NAME, role="button", value="Nonexistent Button",
        fallbacks=[Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="Look Up Member")],
    )
    resolved = resolve_locator(page, loc)
    assert resolved.count() == 1


def test_table_row_cell_reads_rightmost_column(page, base_url):
    page.goto(base_url + "/search?member_id=12345")
    loc = Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Savings", nth_cell=-1)
    resolved = resolve_locator(page, loc)
    assert resolved.inner_text().strip() == "$1842.30"


def test_table_row_cell_positive_index_reads_account_type_column(page, base_url):
    page.goto(base_url + "/search?member_id=12345")
    loc = Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Savings", nth_cell=0)
    resolved = resolve_locator(page, loc)
    assert resolved.inner_text().strip() == "Savings"


def test_table_row_cell_no_matching_row_raises(page, base_url):
    page.goto(base_url + "/search?member_id=12345")
    loc = Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Checking", nth_cell=-1)
    with pytest.raises(LocatorResolutionError):
        resolve_locator(page, loc)


def test_execute_step_fill_and_extract_full_lookup_flow(page, base_url):
    nav = Step(step_id="s0", action=ActionType.NAVIGATE, description="go", value_template="/")
    execute_step(page, nav, {}, base_url)

    fill = Step(
        step_id="s1", action=ActionType.FILL, description="fill",
        locator=Locator(strategy=LocatorStrategy.PLACEHOLDER, value="Enter member ID"),
        value_template="{{member_id}}",
    )
    execute_step(page, fill, {"member_id": "12345"}, base_url)

    click = Step(
        step_id="s2", action=ActionType.CLICK, description="submit",
        locator=Locator(strategy=LocatorStrategy.ROLE_NAME, role="button", value="Look Up Member"),
    )
    execute_step(page, click, {}, base_url)

    extract = Step(
        step_id="s3", action=ActionType.EXTRACT, description="read balance",
        locator=Locator(strategy=LocatorStrategy.TABLE_ROW_CELL, value="Savings", nth_cell=-1),
        extract_as="savings_balance",
    )
    outcome = execute_step(page, extract, {}, base_url)
    assert outcome.extracted_value == "$1842.30"


def test_step_checkpoint_failure_raises_step_execution_error(page, base_url):
    from engine.executor import StepExecutionError

    click = Step(
        step_id="s0", action=ActionType.NAVIGATE, description="go", value_template="/",
        checkpoint=Locator(strategy=LocatorStrategy.TEXT_CONTENT, value="This text does not exist"),
    )
    with pytest.raises(StepExecutionError):
        execute_step(page, click, {}, base_url)
