# Report: Computer-Use Automation System

## Architecture

The system has four pieces, meant to be read in this order because each
one depends on the last:

1. **Perception** (`engine/observer.py`) turns a live Playwright `Page`
   into a compact text description an LLM can reason over: a list of
   interactive elements as `role` + accessible `name` pairs, plus a
   truncated snippet of visible text for error banners/confirmation copy
   that don't live in an "interesting" ARIA role. It deliberately does
   *not* use screenshots+coordinates as the primary signal, and it
   deliberately does *not* use raw CSS selectors -- both break easily on
   a legacy, table-based, no-test-id surface, and neither survives a move
   to a desktop application. Role+accessible-name does: the same
   perception model applies to native apps via OS accessibility APIs
   (UIA on Windows, AT-SPI on Linux), so the agent loop and the artifact
   schema wouldn't need to change if the "surface" were ever a desktop app
   instead of a browser.

2. **Discovery** (`agent/discovery.py`) is the one-time, LLM-in-the-loop
   phase: observe -> ask Claude to choose exactly one tool call -> execute
   it -> record what happened as a typed `Step` -> repeat until the model
   calls `finish_success`, `finish_business_outcome`, or `escalate`. The
   model only ever sees the latest observation and a small fixed tool
   surface (`agent/tools.py`); it cannot invent actions outside that
   surface, and every action it takes is immediately policy-checked (see
   Safety) before it touches the page.

3. **The capability artifact** (`artifacts/schema.py`) is what discovery
   produces and what replay consumes -- see the Artifact schema section.

4. **Replay** (`replay/engine.py`) is the deterministic, no-LLM production
   path. It walks the artifact's steps, executing each one via
   `engine/executor.py`.

The critical architectural decision holding this together: **discovery
and replay call the exact same `engine/executor.py` code** to resolve
locators and perform actions. If they used different code paths, "record
once, replay forever" would be a lie -- replay would be exercising logic
that discovery never actually validated. Concretely, `execute_step()` and
`resolve_locator()` are imported by both `agent/discovery.py`'s tool
dispatch and `replay/engine.py`'s step loop.

```
 discovery (LLM, one-time)              replay (deterministic, every run)
 ┌───────────────────┐                  ┌───────────────────┐
 │ observer.py        │                 │ (no perception     │
 │  (accessibility    │                 │  needed -- steps   │
 │   tree -> text)     │                 │  are already known)│
 └─────────┬──────────┘                 └─────────┬──────────┘
           │ tool call chosen by Claude             │ next recorded Step
           ▼                                        ▼
     ┌─────────────────────  engine/executor.py  ─────────────────────┐
     │        resolve_locator()  +  execute_step()  (SHARED)           │
     └─────────┬─────────────────────────────────────────┬────────────┘
               │ recorded as                             │ classified by
               ▼                                         ▼
     artifacts/schema.py: Step                  replay/engine.py's outcome
     (accumulates into a                          taxonomy (success / business
      CapabilityArtifact)                         outcome / recoverable /
                                                   hard failure / escalated)
```

Cutting across both paths: `guardrails/policy.py` (the same `PolicyEngine`
instance shape is consulted by both discovery and replay -- what the
system is allowed to do is defined once) and `evidence/logger.py`
(structured, redacted logging of what happened, for both).

## Artifact schema

`artifacts/schema.py` (pydantic v2) is the contract between discovery, the
replay engine, and whatever caller invokes a capability by name with typed
args. Four design principles drove it:

1. **Decoupled from the raw model transcript.** The artifact records
   *what to do and how to verify it*, not *why the model chose it*. The
   full Claude conversation is evidence (`EvidenceLogger.log_decision`
   writes a short reasoning summary, not the raw messages list) -- never
   part of the durable contract. This matters because a transcript is
   non-deterministic scaffolding; the artifact needs to be a stable thing
   a completely different process (replay, with no model involved) can
   execute unambiguously forever.

2. **Every locator carries a strategy + ordered fallbacks, not a bare
   selector.** `LocatorStrategy` is explicitly ordered from most to least
   robust on a legacy, no-test-id surface: `role_name` (preferred --
   survives markup/DOM restructuring as long as the visible semantics of
   a control don't change) down through `placeholder`, `label_text`,
   `text_content`, `test_id` (only when the surface actually has one), a
   `table_row_cell` strategy purpose-built for legacy report-style tables
   ("find the row labeled X, read its Nth column" -- mirrors how a human
   operator actually reads these pages), and finally `css` as a documented
   last resort. Each `Locator` also carries an optional `robustness_note`
   explaining *why* this strategy was chosen and what would break it --
   this is not a comment for humans only, it's a first-class schema field,
   because "why is this locator considered stable" is exactly the
   judgment a legacy-UI automation system needs to be able to show its
   work on. The hand-authored `open-sub-account` artifact leans on this
   directly: its `<select>` has no accessible name at all (verified
   against a live render, not assumed -- see Cuts), so `role_name` isn't
   viable and CSS-on-a-stable-attribute is used instead, with the
   reasoning recorded in `robustness_note` rather than left implicit.

3. **Steps distinguish safe vs. risky/irreversible actions
   (`RiskLevel`).** This lets the guardrail layer treat them differently
   without re-deriving that judgment from scratch at replay time -- see
   Safety.

4. **Business outcomes are declared in the schema, not discovered as
   exceptions at replay time.** "Member not found" or "access denied" are
   named, first-class `BusinessOutcome` entries the artifact author (the
   discovery agent, or a human author) is expected to have already
   encountered and documented, each with its own detection locator and
   fixed/derived outputs. This is what lets replay tell the difference
   between "the application gave a legitimate answer" and "something
   actually broke" (see next section).

The schema also separates a capability's `input_parameters` /
`output_schema` (typed, named, what a caller sees) from its `steps`
(internal recipe) -- a caller invokes `member-balance-lookup` with
`member_id` and gets back `savings_balance`; it never needs to know the
capability takes four Playwright actions to get there, or that step 3 uses
a `table_row_cell` locator instead of a `role_name` one.

## Determinism & error handling

`replay/engine.py` classifies every run into exactly one of five outcomes,
by design (see its module docstring for the full rationale):

- **SUCCESS** -- all steps executed, the declared `success_checkpoint`
  resolved, and every declared output was produced.
- **BUSINESS_OUTCOME** -- one of the artifact's declared
  `business_outcomes` matched instead. Treated as a legitimate answer, not
  a failure -- e.g. member-not-found and permission-denied both return
  `BUSINESS_OUTCOME` with `{"found": false, ...}`, never `HARD_FAILURE`.
- **RECOVERABLE** -- a small, explicit, *documented* list
  (`RECOVERABLE_INTERSTITIALS`) of known transient conditions, checked
  proactively before every step. Currently one entry: the session-timeout
  interstitial, whose only safe recovery is restarting the recorded flow
  from step 1 (a timed-out session invalidates whatever the flow had
  already done, e.g. a filled form) -- this is deliberately *not* a
  generic "retry on any error" loop, per the assignment's own warning that
  blind retries aren't real error handling. Each recovery consumes budget
  (`max_recovery_attempts`, default 2); if the same interstitial recurs
  past budget, the run escalates as `recovery_budget_exhausted` rather
  than looping forever.
- **HARD_FAILURE** -- a step's locator (with all its fallbacks) could not
  be resolved, or a step's checkpoint failed, and no declared business
  outcome matched either. Returns full debug detail (`StepFailureDetail`:
  step id, what was expected, what was observed).
- **ESCALATED** -- the run was hard-blocked by policy, or a hard failure
  occurred and a human was brought in (see next section). If the human
  fixes the page state, the *same failed step* is retried once; if that
  retry also fails, the run still finishes as `HARD_FAILURE` rather than
  silently succeeding or looping.

**This taxonomy was verified against the live mock app, not just
designed on paper** -- and doing so caught two real bugs, which is worth
reporting plainly rather than glossing over:

- `ReplayEngine.run()` generated a *new* `run_id` for the `ReplayResult`
  even when the caller supplied its own `EvidenceLogger` (which already
  had its own `run_id`), so `result.run_id` and the actual evidence
  directory on disk could silently diverge. Fixed by deriving `run_id`
  from the logger that's actually used.
- The `table_row_cell` locator strategy matched `page.locator("tr",
  has_text=...)` and took `.first` -- but on this app's nested-table
  legacy markup, an *outer* layout `<tr>` wraps the entire page content
  and therefore also contains the target text (e.g. "Savings") as a
  substring, and it comes first in document order. `.first` silently
  resolved to that wrapper row instead of the actual data row. It
  happened to still extract the right value when reading the *last*
  cell (`nth_cell=-1`, what both shipped capabilities use) purely by
  coincidence of document order, but broke for `nth_cell=0`. Fixed by
  picking the *smallest* matching row (fewest `<td>` descendants) instead
  of the first one -- the most specific match, which is how a human
  actually reads "find the row for Savings" in a nested legacy table.
  Both bugs are covered by regression tests (`tests/test_replay_engine.py`,
  `tests/test_executor.py`) so a future change can't silently reintroduce
  either.

A third latent bug was caught the same way, before it could waste real
LLM budget: `engine/observer.py` originally called
`page.accessibility.snapshot()`, which no longer exists as of the
Playwright version this project actually installs (`>=1.45` resolved to
1.62 locally; the old accessibility-tree API was removed upstream). This
would have crashed the discovery agent's very first observation. It was
caught by inspecting a real page before spending any API budget, and
`snapshot_page()` was rewritten against `Locator.aria_snapshot()` (current
Playwright's supported equivalent) with regression tests
(`tests/test_observer.py`) pinning its (role, name) output against the
real app.

**The real discovery run itself surfaced a fourth, more fundamental gap --
this one in the artifact it produced, not in the engine.** The one LLM
discovery session run against this system was given the goal "look up
member 12345's savings balance," and it only ever searched that one
(found, active) member. Nothing forced it to also try a not-found or
restricted ID, so the resulting `capabilities/member-balance-lookup.json`
came back with `business_outcomes: []` and a `success_checkpoint` of
`"Savings SAV-12345-01 $1842.30"` -- the *literal balance string for that
one member*, not a stable page marker. Replaying it against member
`00099` (not-found) or `99999` (restricted) therefore hard-failed at the
extract step and escalated to a human operator, instead of classifying
as the `member_not_found` / `permission_denied` business outcomes those
pages actually represent -- not because replay's classification logic
was wrong, but because the artifact never declared those outcomes for it
to check against.

This is exactly the situation `CapabilityArtifact.status` (`"draft"` /
`"approved"`) exists for: discovery produces a draft, and a draft is not
assumed correct or complete on its own -- it's reviewed before being
promoted. Here, a human reviewer (with prior manual knowledge of this
app's exact detection text, from the same testing that verified the
engine originally) added the two missing `BusinessOutcome` declarations,
replaced the member-specific checkpoint with the stable `"Member Record"`
marker every successful lookup actually shows, and bumped `version` to 2
before flipping `status` to `"approved"` -- the same schema fields
discovery would have populated itself had its one exploratory run
happened to also cover those paths. No engine or schema code changed to
fix this; `evidence/runs/discovery-20260910T212811Z-b4ba4d` (the real
discovery session) and the paired before/after replay runs for member
`00099` against artifact versions 1 and 2
(`replay-20260910T213212Z-466aa0` hard-failing, then
`replay-20260910T213418Z-cbd8ad` correctly classifying as
`member_not_found`) document the before/after.

The broader lesson: a single discovery run only ever covers the paths it
happened to take. Declaring `business_outcomes` and a checkpoint
correctly for paths *never observed* is not something discovery can be
expected to get right on the first try -- draft-status review by someone
who has separately verified the target app's actual behavior (as
happened here) is the intended safety net, not an afterthought.

## Heterogeneity & multi-tenant

Two mechanisms make this reusable across different legacy vendor
apps/tenants without changing the engine:

- `TargetApp.app_id` + `allowed_domains` scope a capability to a specific
  vendor product/tenant instance. A different credit union running the
  *same* vendor product (same `app_id`, different `base_url`) can reuse an
  artifact's steps as-is if its markup matches; a genuinely different
  product needs its own discovery run (or hand-authored artifact) and gets
  its own `capability_id`/`app_id` -- the schema doesn't try to paper over
  a real UI difference with a single "universal" recipe, which would be
  the kind of blind-retry-style false robustness the assignment
  specifically warns against.
- **Locator fallback chains** absorb small, expected drift within the same
  vendor app (a button's exact wording changing, markup being
  restructured) without needing a full re-discovery: `role_name` primary
  with a `text_content` fallback survives a DOM restructure as long as the
  visible label is unchanged; a `placeholder` fallback survives a
  `<label>` being added or removed later. This is why every locator is a
  primary-plus-ordered-fallbacks structure rather than a single string.
- The perception layer's choice of accessibility role+name (not
  screenshots, not CSS) is itself the multi-*surface* heterogeneity
  answer: the same signal is available from a browser DOM today and from
  OS accessibility APIs on a native desktop back-office client tomorrow,
  without changing `artifacts/schema.py` or the agent loop -- only
  `engine/observer.py`'s snapshot source and `engine/executor.py`'s
  action-execution backend would need new implementations.

What's *not* built (see Cuts): actually pointing this at a second, real
vendor app or a native desktop surface. The multi-tenant story above is
the schema/architecture supporting it, exercised here only against one
mock app.

## Escalation & handoff

`engine/operator_console.py` is a bare textual REPL that acts on the
**same live `Page` object** the automation was just using -- not a fresh
browser session, not a simulated action log. This is the load-bearing
design decision for this section: "hand control to a human" only means
something if the human is looking at and acting on the actual browser
state the automation left behind (whatever was filled in, whatever page
it's stuck on), not a reconstruction.

Control-transfer sequence:

1. Replay hits a condition requiring a human (`step_failed_hard`, or
   `recovery_budget_exhausted`) and calls `on_escalation(page, logger,
   reason, context)`.
2. Before control actually changes hands, the reason, context, and a
   screenshot are logged as an `intervention_request` event -- so there's
   a durable record of what was asked of the human even if they never
   respond.
3. `run_operator_console()` takes over: a small command set (`click
   <text>`, `fill <field> <value>`, `screenshot`, `resume`) executes
   directly against `page`. Every command is logged
   (`EvidenceLogger.log_human_action`).
4. Typing `resume` (or, for scripted/unattended runs, exhausting a
   pre-supplied `commands=[...]` list) ends the console and returns
   control to the caller.
5. For a **hard failure**, the caller retries the *one step that just
   failed*, once, on the assumption the human fixed whatever blocked it --
   not the whole flow from scratch. For **recovery-budget-exhausted**, the
   caller does not retry automatically at all (the recovery loop already
   proved retrying doesn't help on its own); the human's actions are
   logged and the run still finishes `ESCALATED`, on the theory that a
   condition that has already exhausted its own recovery budget needs a
   human decision about what to do next, not another silent attempt.

The console is textual/role-and-text-based specifically so the demo works
headless (no display needed in CI/sandboxes) -- the assignment allows
mocking the operator *UI*, as long as the handoff mechanism and
control-transfer model are real, and a screenshot-based visual console
would not run headless. `commands=[...]` (used throughout
`evidence/runs/`) exists for exactly that: reproducible escalation/handoff
evidence without a human at a keyboard during grading, while the same
code path (`_dispatch`) is what a real human's interactive `input()` loop
calls.

This path is verified end-to-end against the mock app's deliberately
undeclared "verify identity" interstitial (member `55555`): the artifact
never saw this screen during (simulated) discovery, so replay hard-fails
at the balance-extraction step; escalation hands the same page to the
operator console; a scripted `click Proceed to Record` clears the
interstitial; the retried extraction step succeeds. See
`evidence/runs/` and `tests/test_replay_engine.py`'s
`test_undeclared_interstitial_hard_failure_then_human_resume_succeeds`.

## Safety

- **Allowlist enforcement, identically for discovery and replay.**
  `guardrails/policy.py`'s `PolicyEngine` is domain + path-prefix based
  (`check_url`) plus an explicit action-type allowlist (`check_action`).
  Anything outside it is a hard `PolicyViolation`, not a warning --
  surfaced as `HARD_FAILURE` at replay time, or fed back to the model as a
  blocked tool result during discovery. The same `PolicyEngine` shape is
  constructed from the artifact's own `allowed_domains` at replay time, so
  a capability cannot be replayed against a domain it wasn't discovered/
  authored against.
- **Risk-gated confirmation, using the target app's own UI.**
  `RiskLevel.SAFE` and `REVERSIBLE` steps proceed automatically.
  `IRREVERSIBLE` steps (e.g. `open-sub-account`'s final confirm click) are
  *not* gated by an automation-invented confirmation dialog -- the system
  deliberately requires the *target app's own* confirmation screen to be
  part of the recorded flow (see `open-sub-account`'s step `s3` navigating
  to the confirm screen, then `s4` as the actual irreversible click) and
  logs a `risky_step_auto_proceeding` event with an explicit note that the
  in-app screen is the real control gate. This mirrors how the mock app
  itself already models "confirm before opening an account," rather than
  bolting on a second, redundant confirmation layer the system would have
  to trust itself.
- **Redaction before anything touches disk.** `PolicyEngine.redact` /
  `redact_dict` mask values whose *field name* matches a sensitive-name
  heuristic (`ssn`, `password`, `api_key`, `account_number`, `pin`, etc.)
  before they're written to an artifact or an evidence log --
  `EvidenceLogger._redact_payload` applies this to every logged event
  automatically, recursively through nested dicts. This is a heuristic on
  field *names*, not content-scanning; see Cuts for the corresponding
  limitation.
- **Budget limits.** `PolicyConfig.max_steps` / `max_seconds` bound both a
  single replay run (`check_step_budget`) and the discovery loop's own
  turn counter, so neither can run away indefinitely on unexpected page
  behavior.
- **The LLM API key is never handled by this system's own code paths in a
  way that could leak it.** `agent/anthropic_client.py` reads
  `ANTHROPIC_API_KEY` only from the environment, `scripts/run_agent.py`
  refuses to run without it already set (rather than accepting it as a
  CLI arg, which would land in shell history), and nothing in this
  codebase logs request headers.

## Cuts

Documented, deliberate scope reductions -- not oversights:

- **Only one capability was produced by a real LLM discovery run.** The
  assignment requires demonstrating genuine LLM-driven discovery once;
  spending a second expensive session on `open-sub-account` (an
  irreversible, multi-field, confirmation-gated flow) would have
  exercised the same discovery machinery a second time for comparatively
  little additional signal. Instead, `open-sub-account` was
  **hand-authored** directly against the schema, specifically to exercise
  code paths a read-only lookup can't: the risk/guardrail-confirmation
  path, a declared validation-error business outcome, and a full
  irreversible replay through the target app's own confirm screen. Its
  locator choices were grounded in an actual inspection of the live page's
  accessibility tree (documented inline in the artifact-building script),
  not guessed.
- **The discovery agent's perception layer can't act on an unlabeled form
  control.** `engine/observer.py` only surfaces elements with a non-empty
  accessible name (by design -- an unnamed element is not something an
  LLM can meaningfully refer back to by description). The
  `open-sub-account` form's `<select name="account_type">` has no
  associated `<label>` and gets no accessible name at all (verified
  live), so an LLM-driven discovery run could not have filled it out
  through today's observer -- one more reason that capability is
  hand-authored rather than discovered. A production version of this
  system would likely need the perception layer to fall back to
  positional/structural descriptions (e.g. "the 2nd combobox, in the row
  labeled 'Account Type'") for exactly this class of legacy form control.
- **Redaction is field-name-based, not content-based.** A sensitive value
  stored under an innocuously-named field (or embedded inside a larger
  string) would not be caught by `PolicyEngine.redact`. A real deployment
  would want content-pattern matching (SSN/card-number shaped strings,
  etc.) as a second layer, not just name heuristics.
- **No real second vendor app or desktop surface.** The heterogeneity
  story (Section 4) is architectural -- accessibility-tree perception and
  a portable schema -- but only ever exercised against the one mock app in
  this repo. Nothing here has actually been pointed at a second product or
  a native (non-browser) surface.
- **`RECOVERABLE_INTERSTITIALS` has exactly one entry.** It's built as an
  extensible, explicit list by design, but only the session-timeout case
  is implemented/tested. A production system would accumulate more
  entries over time as new known-transient conditions are identified --
  deliberately never as a generic catch-all retry.
- **No CI pipeline.** `tests/` is real pytest coverage (43 tests: schema
  construction/validation, executor locator resolution including
  `table_row_cell`, replay engine outcome classification for both
  capabilities, guardrail allowlist/redaction, and observer regression
  coverage) that must be run manually; nothing here wires it into GitHub
  Actions or similar.
- **Single in-process mock app, single browser, no concurrency.** Nothing
  in this system was tested against concurrent replay runs against the
  same target app instance (e.g. two capabilities racing against the
  mock app's in-memory, lock-guarded `data.py`). The lock in
  `mock_app/data.py` protects the mock's own data structure, but the
  engine/replay layer itself has no notion of concurrent-run isolation.
