# Computer-Use Automation System

A backend integration layer that lets an AI agent operate legacy
back-office bank/credit-union UIs that have no API: an LLM discovers how
to accomplish a goal once by driving a real browser, the successful run
becomes a typed **capability artifact**, and that artifact is then
**replayed deterministically** (no LLM) in production -- with an explicit
error taxonomy and a human-escalation path for when it gets stuck.

See [`REPORT.md`](REPORT.md) for the design writeup (architecture, schema,
determinism/error handling, multi-tenant story, escalation, safety, and
scope cuts) and [`HANDOFF.md`](HANDOFF.md) for the full history of
decisions made while building this.

## Requirements

- Python 3.10+
- A modern browser download via Playwright (installed below; no separate
  Chrome/Chromium install needed)
- An `ANTHROPIC_API_KEY` -- only if you want to run the real LLM discovery
  step yourself (everything else -- the mock app, deterministic replay,
  the test suite -- works without one)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Start the mock back-office app (leave running in its own terminal):

```bash
python -m mock_app.app        # serves http://127.0.0.1:5055
```

Smoke-test the schema against real pydantic:

```bash
python3 -c "from artifacts.schema import CapabilityArtifact; print('pydantic OK')"
```

Run the test suite (spins up its own instance of the mock app on a
separate port automatically -- no need for the terminal above just for
this):

```bash
pytest tests/ -v
```

## Demo commands

All commands below assume the mock app is running at
`http://127.0.0.1:5055` (see Setup) and `.venv` is activated.

### 1. Deterministic replay -- `member-balance-lookup` (SAFE, read-only)

This capability's artifact was produced by a real LLM discovery run (see
section 3 below), then reviewed by a human and promoted from `draft` to
`approved` (version 2) -- the initial discovery run only ever searched a
found/active member, so it never declared business outcomes for
not-found/restricted records; those were added during review, not by
re-running discovery. See `REPORT.md`'s Determinism & error handling
section for the full story. `capabilities/member-balance-lookup.json`
already exists in this repo, so these all run as-is:

```bash
# Happy path
python scripts/replay.py --capability capabilities/member-balance-lookup.json \
    --param member_id=12345

# Business outcome: no such member (not a failure -- a declared answer)
python scripts/replay.py --capability capabilities/member-balance-lookup.json \
    --param member_id=00099

# Business outcome: permission denied (restricted record)
python scripts/replay.py --capability capabilities/member-balance-lookup.json \
    --param member_id=99999

# Recoverable interstitial that recurs until it exhausts its recovery
# budget and escalates (member 00000 always bounces to a timeout screen)
python scripts/replay.py --capability capabilities/member-balance-lookup.json \
    --param member_id=00000

# Hard failure -> human escalation -> operator takes the SAME live
# session -> resume -> success. Member 55555 hits an undeclared
# "verify identity" interstitial the artifact never saw during discovery.
# --escalation-commands scripts the human's console input for a
# reproducible demo (omit it to get an interactive operator prompt instead):
python scripts/replay.py --capability capabilities/member-balance-lookup.json \
    --param member_id=55555 --escalation-commands 'click Proceed to Record'
```

### 2. Deterministic replay -- `open-sub-account` (IRREVERSIBLE)

Hand-authored (see `REPORT.md`'s Cuts section for why), already saved at
`capabilities/open-sub-account.json`:

```bash
# Business outcome: validation error surfaced by the target app itself
python scripts/replay.py --capability capabilities/open-sub-account.json \
    --param member_id=12346 --param account_type=Savings --param initial_deposit=2.00

# Full irreversible replay: goes through the app's own confirmation screen
# and actually opens the account
python scripts/replay.py --capability capabilities/open-sub-account.json \
    --param member_id=12346 --param account_type=Checking --param initial_deposit=40.00
```

### 3. Real LLM discovery run -- produces `member-balance-lookup.json`

Requires your own Anthropic API key, exported in your own shell (never
paste it into a chat/session transcript):

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # your key, your shell, not this file
python scripts/run_agent.py \
    --goal "Look up member 12345's savings balance" \
    --capability-id member-balance-lookup \
    --base-url http://127.0.0.1:5055 \
    --param member_id=12345 \
    --output savings_balance
```

On success this writes `capabilities/member-balance-lookup.json` as a
fresh `status: "draft"`, `version: 1` artifact covering only the path this
one run happened to take. Before trusting it for paths beyond that (e.g.
not-found/restricted member IDs it never searched), review it like any
other draft -- see the note in section 1 above and REPORT.md's
Determinism & error handling section.

Every command above writes structured evidence (`events.jsonl`,
screenshots, `result.json`) under `evidence/runs/<run_id>/`. See
`evidence/` for saved examples from prior runs.

## Project layout

```
agent/          discovery agent loop + Anthropic client + tool schemas
artifacts/      the capability artifact pydantic schema (the contract)
capabilities/   saved capability artifacts (JSON)
engine/         perception (observer), execution (executor), operator console
guardrails/     policy engine: allowlist, risk gating, redaction
replay/         deterministic replay engine + error taxonomy
mock_app/       the legacy target application used for discovery/replay/tests
evidence/       structured logs/screenshots/results per run
scripts/        CLI entry points (run_agent.py, replay.py)
tests/          pytest suite
```
