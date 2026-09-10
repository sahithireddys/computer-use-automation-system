#!/usr/bin/env python3
"""
CLI entry point: run the real LLM discovery agent against the mock app and,
on success (or a discovered business outcome), save the resulting capability
artifact under capabilities/<capability-id>.json.

Requires ANTHROPIC_API_KEY to be exported in your own shell -- this script
never accepts the key as an argument or prints it. See /README.md and
/HANDOFF.md for why (never paste API keys into a chat/session transcript).

Usage:
    python scripts/run_agent.py \
        --goal "Look up member 12345's savings balance" \
        --capability-id member-balance-lookup \
        --base-url http://127.0.0.1:5055 \
        --param member_id=12345 \
        --output savings_balance
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running this script directly (`python scripts/run_agent.py ...`)
# without installing the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.discovery import DiscoveryAgent

CAPABILITIES_DIR = Path(__file__).resolve().parent.parent / "capabilities"


def parse_params(pairs: list[str]) -> dict[str, str]:
    params = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param must be KEY=VALUE, got: {pair!r}")
        k, v = pair.split("=", 1)
        params[k] = v
    return params


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--goal", required=True, help="Natural-language goal for the discovery agent.")
    ap.add_argument("--capability-id", required=True, help="Slug to save the artifact under, e.g. 'member-balance-lookup'.")
    ap.add_argument("--base-url", default="http://127.0.0.1:5055")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                     help="Input parameter passed to the discovery agent; may be repeated.")
    ap.add_argument("--output", action="append", default=[], dest="outputs", metavar="NAME",
                     help="Declared output field the agent must extract before finishing; may be repeated.")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--headed", action="store_true", help="Show the browser window (default: headless).")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY is not set in this shell.\n"
            "Export it yourself before running this script, e.g.:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...\n"
            "Do not paste it into a chat session -- run this command directly in your terminal.",
            file=sys.stderr,
        )
        return 2

    params = parse_params(args.param)
    agent = DiscoveryAgent(
        capability_id=args.capability_id,
        goal=args.goal,
        base_url=args.base_url,
        input_params=params,
        expected_outputs=args.outputs,
        max_steps=args.max_steps,
        headless=not args.headed,
    )
    result = agent.run()

    print(f"status: {result.status}")
    print(f"run_id: {result.run_id}")
    print(f"evidence: evidence/runs/{result.run_id}")

    if result.status not in ("success", "business_outcome"):
        print(f"escalation_reason: {result.escalation_reason}", file=sys.stderr)
        return 1

    if result.outputs:
        print(f"outputs: {json.dumps(result.outputs, indent=2)}")

    assert result.artifact is not None
    result.artifact.created_from_run_id = result.run_id
    CAPABILITIES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAPABILITIES_DIR / f"{args.capability_id}.json"
    out_path.write_text(result.artifact.model_dump_json(indent=2))
    print(f"saved capability artifact: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
