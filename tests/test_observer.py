"""
engine/observer.py tests -- regression coverage for the aria_snapshot-based
rewrite. The original implementation called the now-removed
`page.accessibility.snapshot()` (see engine/observer.py's module docstring
and HANDOFF.md); these tests exercise the replacement against the real
mock app so a future Playwright upgrade that changes aria_snapshot's
output format fails loudly here instead of silently breaking discovery.
"""
from __future__ import annotations

from engine.observer import snapshot_page


def test_snapshot_lists_textbox_and_button_on_home_page(page, base_url):
    page.goto(base_url + "/")
    text, elements = snapshot_page(page)
    by_role = {(e.role, e.name) for e in elements}
    assert ("textbox", "Enter member ID") in by_role
    assert ("button", "Look Up Member") in by_role
    assert "Interactive elements:" in text


def test_snapshot_lists_links_on_member_detail_page(page, base_url):
    page.goto(base_url + "/search?member_id=12345")
    _, elements = snapshot_page(page)
    names = {e.name for e in elements}
    assert "Open a new sub-account for this member" in names
    assert "Back to search" in names


def test_snapshot_excludes_unlabeled_select(page, base_url):
    # Documents a known limitation (see REPORT.md Cuts): the account_type
    # <select> has no accessible name in this legacy markup, so it is
    # correctly excluded from the LLM-facing element list -- an LLM
    # discovery run could not select_option on it through this observer.
    # This is why open-sub-account is hand-authored rather than discovered.
    page.goto(base_url + "/member/12345/open-account")
    _, elements = snapshot_page(page)
    assert not any(e.role == "combobox" for e in elements)
    names = {e.name for e in elements}
    assert "e.g. 25.00" in names  # placeholder-derived name still present
    assert "Continue" in names


def test_snapshot_respects_max_elements(page, base_url):
    page.goto(base_url + "/")
    _, elements = snapshot_page(page, max_elements=1)
    assert len(elements) <= 1
