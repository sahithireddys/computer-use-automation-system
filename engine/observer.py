"""
Perception layer: turns a live Playwright Page into a compact textual
representation an LLM can reason over.

Uses the accessibility tree rather than raw HTML/screenshots as the
primary signal. Rationale (REPORT.md section 4, "surface abstraction"):
  - The accessibility tree is available uniformly across modern web,
    legacy server-rendered web (as long as elements have a computed
    accessible name -- which we get here from placeholder/text content
    even without ARIA authoring), AND native desktop apps (via OS
    accessibility APIs, e.g. UIA/AT-SPI) -- making this the one
    perception strategy that survives a swap from "browser" to "desktop"
    as the surface, without changing the agent loop or artifact schema.
  - It is far more compact and stable than a screenshot+coordinates
    approach when the UI is a legacy, table-heavy layout: coordinates
    shift with the table's content, but role+name does not.

We still capture a screenshot on every step as *evidence* (for humans
debugging a run), but the *decision* signal fed to the LLM is text.

Implementation note: Playwright's old `page.accessibility.snapshot()`
API (what an earlier version of this module used) was removed upstream
-- as of Playwright 1.62 (the version this project actually runs on,
per requirements.txt's `>=1.45`) calling it raises AttributeError. This
was caught locally, before spending any LLM budget, by inspecting a real
page's tree (see HANDOFF.md). The current replacement,
`Locator.aria_snapshot()`, returns the same role+name information Playwright
itself uses to resolve `get_by_role(...)` (so a captured (role, name) pair
is guaranteed resolvable later), just serialized as an indented YAML-like
string instead of a nested dict. `_parse_aria_snapshot_line` below parses
that format -- a small, deliberately narrow parser (documented limits
below), not a general YAML parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page


@dataclass
class InteractiveElement:
    role: str
    name: str
    index: int  # stable-for-this-snapshot ordinal, used to disambiguate same role+name

    def as_line(self) -> str:
        return f"[{self.index}] role={self.role!r} name={self.name!r}"


# Roles we surface to the LLM as candidate interaction targets. Kept
# narrow deliberately -- flooding the model with every AX node (rows,
# generic groups, etc.) on a legacy table-based page produces noise that
# degrades decision quality far more than it helps.
_INTERESTING_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "searchbox", "heading",
}

# Matches one already-unwrapped aria_snapshot node body, e.g.:
#   button "Continue"
#   textbox "e.g. 25.00"
#   combobox
# Deliberately does not attempt to parse nested nodes on the same line,
# escaped-quote edge cases beyond a simple \" , or YAML flow-collections --
# aria_snapshot's own output for the plain, table-based markup this system
# targets doesn't produce those, and this is a perception heuristic (the
# executor's own locator resolution, not this parser, is the source of
# truth at replay time).
_NODE_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+"((?:[^"\\]|\\.)*)")?')


def _unwrap_yaml_scalar(body: str) -> str:
    """
    aria_snapshot wraps a node in single quotes (instead of the plain
    `role "name":` form) whenever `name` itself contains a colon, e.g.:
        'row "Account Type: -- select --"':
    Strip that outer quoting (and any trailing ':' introducing children)
    down to the plain `role "name"` form the regex above expects.
    """
    if body.startswith("'"):
        if body.endswith("':"):
            return body[1:-2]
        if body.endswith("'"):
            return body[1:-1]
        return body[1:]
    if body.endswith(":"):
        return body[:-1]
    return body


def _parse_aria_snapshot_line(raw_line: str) -> Optional[tuple[str, str]]:
    stripped = raw_line.strip()
    if not stripped.startswith("-"):
        return None
    body = _unwrap_yaml_scalar(stripped[1:].strip())
    m = _NODE_RE.match(body)
    if not m:
        return None
    role = m.group(1)
    name = (m.group(2) or "").replace('\\"', '"').strip()
    return role, name


def snapshot_page(page: Page, max_elements: int = 60) -> tuple[str, list[InteractiveElement]]:
    """
    Returns (text_for_llm, elements) where `elements` lets the caller map
    an LLM-chosen index back to a concrete accessibility node description.
    """
    yaml_snapshot = page.locator("body").aria_snapshot()
    elements: list[InteractiveElement] = []

    for raw_line in yaml_snapshot.splitlines():
        parsed = _parse_aria_snapshot_line(raw_line)
        if parsed is None:
            continue
        role, name = parsed
        if role in _INTERESTING_ROLES and name:
            elements.append(InteractiveElement(role=role, name=name, index=len(elements)))
        if len(elements) >= max_elements:
            break

    lines = [f"URL: {page.url}", f"Title: {page.title()}", "", "Interactive elements:"]
    lines += [e.as_line() for e in elements] or ["(none detected)"]

    # A small amount of raw visible text helps the model read confirmation
    # copy / error banners that aren't inside an "interesting" role (e.g.
    # a plain <b> error message on this legacy app).
    try:
        body_text = page.inner_text("body")
        snippet = " ".join(body_text.split())[:800]
        lines += ["", "Visible text (truncated):", snippet]
    except Exception:
        pass

    return "\n".join(lines), elements
