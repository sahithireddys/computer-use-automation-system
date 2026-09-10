# Handoff: interface.ai Computer-Use Automation System

This is a take-home assignment for interface.ai (design doc:
`Assignment_A___Computer-Use_Automation_System.pdf`, not included here --
ask the user if you need the original text). This file exists so whoever
picks up the project next (human or Claude Code) has full context without
re-deriving decisions already made and verified.

## What this system is

A backend integration layer that lets an AI agent operate legacy
back-office bank/credit-union UIs that have no API:
1. An LLM ("computer use") drives a real browser to accomplish a goal the
   first time -- **discovery**.
2. The successful run is turned into a typed, versioned, reusable
   **capability artifact** (JSON, pydantic schema in `artifacts/schema.py`).
3. The artifact is **replayed deterministically** (no LLM) in production,
   with an explicit error taxonomy: success / business outcome /
   recoverable / hard failure / escalated.
4. When stuck, the system **escalates to a human**, who takes control of
   the *same live browser session* (not a fresh one) via a bare text
   console, then hands control back.

See `/README.md` for setup + exact demo commands, `/REPORT.md` for the
full design writeup (7 required sections).

## Decisions already made (don't re-litigate without reason)

- **Target app**: a local mock "bank back-office" Flask app (`mock_app/`)
  -- server-rendered, table-based, no test IDs, deliberately legacy. Runs
  at `http://127.0.0.1:5055`.
- **Perception**: accessibility tree (role + accessible name), not raw
  CSS selectors or screenshot+coordinates. See `engine/observer.py`.
- **Execution**: `engine/executor.py` is used *identically* by discovery
  and replay -- same code resolves locators and performs actions in both
  paths.
- **Schema**: pydantic v2 (`artifacts/schema.py`), real (not shimmed) --
  confirmed working in this environment.
- **LLM client**: raw `requests` calls to the Anthropic Messages API
  (`agent/anthropic_client.py`). Model id read from `ANTHROPIC_MODEL` env
  var, default now `claude-sonnet-5` (updated from a stale
  `claude-sonnet-4-5` -- **if this default 404s again by the time you're
  reading this, check https://docs.claude.com for the current id**).
- **Two capabilities, asymmetric treatment** (documented in REPORT.md's
  Cuts section, not an oversight): the spec only requires ONE genuine LLM
  discovery run.
  - `member-balance-lookup` (SAFE, read-only) -- **the real discovery
    run**, via `scripts/run_agent.py`. **Not yet run** -- needs the user's
    own `ANTHROPIC_API_KEY` (see Next Steps #1).
  - `open-sub-account` (IRREVERSIBLE, multi-field form + confirmation
    step) -- **hand-authored**, saved at
    `capabilities/open-sub-account.json`. Locator choices for its
    unlabeled `<select>` and bare-`<span>` extract target were grounded in
    an actual inspection of the live page's `aria_snapshot()` output, not
    guessed (see the artifact's own `robustness_note` fields, and
    REPORT.md section 2).
- **Escalation console** (`engine/operator_console.py`): a bare textual
  REPL acting on the *same* `Page` object. Supports a `commands=[...]`
  scripted list for reproducible evidence generation.

## Three real bugs were found and fixed this session (via testing, before
## they could waste LLM budget or ship silently broken)

1. **`engine/observer.py` called a removed Playwright API.**
   `page.accessibility.snapshot()` no longer exists as of Playwright 1.62
   (what `requirements.txt`'s `playwright>=1.45` actually resolves to
   locally) -- would have crashed the discovery agent's very first
   observation. Caught by inspecting a real page *before* spending any
   API budget. Rewritten against `Locator.aria_snapshot()` (current
   Playwright's supported equivalent); regression-tested in
   `tests/test_observer.py` against the real mock app.
2. **`replay/engine.py`'s `ReplayEngine.run()` generated a fresh `run_id`
   for the `ReplayResult` even when the caller supplied its own
   `EvidenceLogger`** (which already had a real `run_id` and a real
   directory on disk) -- so `result.run_id` and the actual evidence
   location could silently diverge. Fixed: `run_id` is now always derived
   from the logger actually in use.
3. **`engine/executor.py`'s `table_row_cell` locator strategy matched the
   *first* `<tr>` containing the target text, not the most specific
   one.** On this app's nested-table legacy markup, an outer layout
   `<tr>` wraps the entire page and therefore also contains e.g.
   "Savings" as a text substring, and it's first in document order --
   so `.first` silently resolved to that ancestor wrapper instead of the
   real data row. It happened to still work when reading the *last* cell
   of a row (`nth_cell=-1`, what both shipped capabilities use) purely by
   document-order coincidence, but broke for `nth_cell=0`. Fixed to pick
   the row with the fewest `<td>` descendants among matches (the most
   specific one) instead of the first one in document order. See
   REPORT.md's Determinism & error handling section for the full story;
   regression-tested in `tests/test_executor.py`.

All 43 tests in `tests/` pass after these fixes (`pytest tests/ -v`).

## What is verified working (real pytest suite + curated /evidence/ runs,
## not just ad hoc scripts anymore)

- `tests/` (43 tests): schema construction/validation
  (`test_schema.py`), executor locator resolution including
  `table_row_cell` (`test_executor.py`), observer regression coverage
  (`test_observer.py`), guardrail allowlist/redaction
  (`test_guardrails.py`), and full replay-engine outcome classification
  for **both** capabilities (`test_replay_engine.py`): SUCCESS,
  BUSINESS_OUTCOME (not-found, permission-denied, and
  `open-sub-account`'s validation-error), RECOVERABLE ->
  recovery-budget-exhausted -> ESCALATED, HARD_FAILURE ->
  escalation -> human resume -> SUCCESS, and a full IRREVERSIBLE
  `open-sub-account` replay through the app's own confirm screen.
- `evidence/runs/` holds 7 curated, clean (post-bugfix) demonstration
  runs covering all of the above via `scripts/replay.py` directly (see
  README.md section "Demo commands" for the exact commands that produced
  each one).
- `scripts/replay.py` and `scripts/run_agent.py` (Next Steps' old #5) are
  written and working.

## What is NOT yet done

1. **Run the real LLM discovery** for `member-balance-lookup` (needs the
   user's own `ANTHROPIC_API_KEY`, exported in their own shell --
   `scripts/run_agent.py` refuses to run without it and never accepts it
   as a CLI arg; never paste it into a chat). Once run, this produces
   `capabilities/member-balance-lookup.json`, which the replay demo
   commands in `README.md` section 1 then need to actually execute
   (currently that section's commands will fail with "file not found"
   until this artifact exists).
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   python scripts/run_agent.py --goal "Look up member 12345's savings balance" \
       --capability-id member-balance-lookup --base-url http://127.0.0.1:5055 \
       --param member_id=12345 --output savings_balance
   ```
   If it succeeds, also capture a couple of replay runs against the
   resulting artifact into `evidence/runs/` (success + at least one
   business-outcome/error case) to round out the evidence set with a
   *real* discovery-produced artifact, not just the test-only equivalent
   used for the demonstrations captured so far.
2. Git init, confirm `.gitignore` keeps secrets/caches out (updated this
   session -- evidence/runs/* is now intentionally tracked, see the
   comment in `.gitignore`), first commit, push to a **public** GitHub
   repo, email the link per spec section 11. **Not started** -- needs the
   user's go-ahead on the GitHub account/repo name and confirmation before
   any push (publishing is an outward-facing action).

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# terminal 1
python -m mock_app.app        # serves http://127.0.0.1:5055

# terminal 2 -- smoke test the executor/replay engine still work with real pydantic
python3 -c "from artifacts.schema import CapabilityArtifact; print('pydantic OK')"

# terminal 2 (same) -- run the test suite (spins up its own mock_app instance
# on a separate port automatically)
pytest tests/ -v
```
