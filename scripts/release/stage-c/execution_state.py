"""Terminal-state verdict for ONE Cloud Run job execution (Stage C).

Reads the JSON of
  gcloud run jobs executions describe <name> --format=json
from stdin and prints exactly one verdict word:

  succeeded — the Completed condition is "True"
  failed    — the Completed condition is "False", OR the execution has a
              completionTime without a positive Completed condition
              (terminal but unproven success is treated as failure,
              fail-safe — the same terminal rule verify_executions.py
              and the kill switch use)
  running   — no terminal proof yet (still executing), AND on malformed
              or unparseable input, so callers keep waiting and their
              bounded timeout fails closed instead of trusting a guess

Used by 06-collect-evidence.sh, which launches probe executions with
--async so the execution NAME is captured before completion: a probe that
completes nonzero must never lose its name — the name is what lets the
gate retrieve the structured failure evidence from Cloud Logging.

Exit code is always 0 when a verdict was printed; the verdict word is the
interface. Never prints secret values.
"""

from __future__ import annotations

import json
import sys


def verdict(execution: object) -> str:
    if not isinstance(execution, dict):
        return "running"
    status = execution.get("status")
    if not isinstance(status, dict):
        return "running"
    for condition in status.get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == "Completed":
            if condition.get("status") == "True":
                return "succeeded"
            if condition.get("status") == "False":
                return "failed"
    if status.get("completionTime"):
        return "failed"
    return "running"


def main() -> int:
    try:
        execution = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("running")
        return 0
    print(verdict(execution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
