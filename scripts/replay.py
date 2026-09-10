#!/usr/bin/env python3
"""
CLI entry point: deterministic replay of a saved capability artifact.
No LLM involved -- this is the production execution path.

Usage:
    python scripts/replay.py --capability capabilities/member-balance-lookup.json \
        --param member_id=12345

    # Force a hard-failure path into human escalation, scripted for
    # reproducible /evidence/ generation (no human at the keyboard):
    python scripts/replay.py --capability capabilities/member-balance-lookup.json \
        --param member_id=55555 \
        --escalation-commands 'click Proceed to Record'

Exit codes: 0 on SUCCESS or BUSINESS_OUTCOME, 1 on RECOVERABLE-exhausted /
HARD_FAILURE / ESCALATED, 2 on a CLI/usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# Allow running this script directly (`python scripts/replay.py ...`) without
# installing the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from artifacts.schema import CapabilityArtifact, OutcomeType
from engine.operator_console import run_operator_console
from evidence.logger import EvidenceLogger, new_run_id
from guardrails.policy import AllowlistEntry, PolicyConfig, PolicyEngine
from replay.engine import ReplayEngine


def parse_params(pairs: list[str]) -> dict[str, str]:
    params = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param must be KEY=VALUE, got: {pair!r}")
        k, v = pair.split("=", 1)
        params[k] = v
    return params


def build_policy(artifact: CapabilityArtifact) -> PolicyEngine:
    domains = artifact.allowed_domains or [urlparse(artifact.target.base_url).hostname]
    return PolicyEngine(PolicyConfig(allowed_domains=[AllowlistEntry(domain=d) for d in domains]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capability", required=True, help="Path to a capability artifact JSON file.")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                     help="Input parameter for the capability; may be repeated.")
    ap.add_argument("--headed", action="store_true", help="Show the browser window (default: headless).")
    ap.add_argument("--max-recovery-attempts", type=int, default=2)
    ap.add_argument("--escalation-commands", action="append", default=None,
                     help="Scripted operator-console command for unattended escalation demos; "
                          "may be repeated in order. If omitted, escalation drops to an "
                          "interactive stdin console.")
    args = ap.parse_args()

    capability_path = Path(args.capability)
    if not capability_path.exists():
        print(f"error: capability file not found: {capability_path}", file=sys.stderr)
        return 2

    artifact = CapabilityArtifact.model_validate_json(capability_path.read_text())
    params = parse_params(args.param)
    policy = build_policy(artifact)
    engine = ReplayEngine(artifact=artifact, policy=policy, max_recovery_attempts=args.max_recovery_attempts)

    run_id = new_run_id("replay")
    logger = EvidenceLogger(run_id, kind="replay")
    logger.log_event("cli_invocation", {"capability": str(capability_path), "params": params})

    def on_escalation(page, logger, reason, context):
        run_operator_console(page, logger, reason, context, commands=args.escalation_commands)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        try:
            result = engine.run(page, params, logger=logger, on_escalation=on_escalation)
        finally:
            browser.close()

    print(json.dumps(result.model_dump(), indent=2, default=str))
    print(f"\nevidence: {logger.dir}", file=sys.stderr)

    if result.status in (OutcomeType.SUCCESS, OutcomeType.BUSINESS_OUTCOME):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
