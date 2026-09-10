"""
Structured evidence logging, shared by discovery and replay runs.

Every run gets its own directory under evidence/runs/<run_id>/ containing:
  - events.jsonl   -- one structured event per line (what happened, why)
  - screenshots/   -- PNG on each step and always on failure/escalation
  - result.json    -- final ReplayResult / discovery summary

Sensitive values are redacted via guardrails.policy.PolicyEngine before
ever touching disk, per the "never persist secrets or raw PII" requirement.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from guardrails.policy import PolicyEngine

EVIDENCE_ROOT = Path(__file__).resolve().parent / "runs"


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"


class EvidenceLogger:
    def __init__(self, run_id: str, kind: str):
        """
        kind: "discovery" | "replay"
        """
        self.run_id = run_id
        self.kind = kind
        self.dir = EVIDENCE_ROOT / run_id
        self.screenshots_dir = self.dir / "screenshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.dir / "events.jsonl"
        self._step_counter = 0
        self.log_event("run_started", {"kind": kind, "run_id": run_id})

    # -- core logging -----------------------------------------------------

    def _redact_payload(self, payload: dict) -> dict:
        redacted = {}
        for k, v in payload.items():
            if isinstance(v, str):
                redacted[k] = PolicyEngine.redact(k, v)
            elif isinstance(v, dict):
                redacted[k] = self._redact_payload(v)
            else:
                redacted[k] = v
        return redacted

    def log_event(self, event_type: str, payload: Optional[dict] = None) -> None:
        record = {
            "ts": time.time(),
            "run_id": self.run_id,
            "event": event_type,
            "payload": self._redact_payload(payload or {}),
        }
        with open(self._events_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    # -- convenience wrappers ----------------------------------------------

    def log_step(self, step_id: str, action: str, detail: dict) -> None:
        self._step_counter += 1
        self.log_event("step", {"step_id": step_id, "action": action, **detail})

    def log_decision(self, reasoning_summary: str, chosen_action: dict) -> None:
        """
        For the LLM-driven discovery loop: record *what* was decided and a
        short reasoning summary, deliberately decoupled from the full raw
        model transcript (which is not part of the durable evidence -- see
        REPORT.md section 2 on why the artifact/evidence stay separate from
        the transcript).
        """
        self.log_event(
            "agent_decision",
            {"reasoning_summary": reasoning_summary, "chosen_action": chosen_action},
        )

    def log_outcome(self, status: str, detail: dict) -> None:
        self.log_event("outcome", {"status": status, **detail})

    def log_escalation(self, reason: str, context: dict) -> None:
        self.log_event("escalation", {"reason": reason, **context})

    def log_human_action(self, description: str) -> None:
        self.log_event("human_action", {"description": description})

    def screenshot_path(self, label: str) -> Path:
        self._step_counter += 1
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return self.screenshots_dir / f"{self._step_counter:03d}_{safe_label}.png"

    def write_result(self, result_dict: dict) -> Path:
        path = self.dir / "result.json"
        with open(path, "w") as f:
            json.dump(result_dict, f, indent=2, default=str)
        return path

    def relative(self, path: Path) -> str:
        return str(path.relative_to(EVIDENCE_ROOT.parent.parent))
