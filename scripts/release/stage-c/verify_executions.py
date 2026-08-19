"""Exact Worker-execution baseline gate for Stage C (Attempt 7).

Reads the structured JSON of
  gcloud run jobs executions list --job=<worker> --format=json
from stdin and verifies an EXACT execution posture instead of a fragile
line count:

  - the total number of executions equals --expected-total exactly
    (Attempt 7: 2 historical terminal executions before the run, 3 after
    the one authorized launch — a one-execution increment over the pinned
    baseline, never merely "some execution exists");
  - every execution is verifiably TERMINAL (a non-empty completionTime or
    a Completed condition with status True/False — the same rule the kill
    switch uses);
  - zero active/nonterminal executions. A missing, null or malformed
    status is treated fail-safe as nonterminal/unverifiable and FAILS the
    gate; it is never assumed terminal.

Historical terminal executions are evidence: they are never cancelled,
deleted or hidden by this gate. Listing or parsing failures exit non-zero
(fail closed). Prints one structured JSON verdict line; never prints
secret values. Exit 0 only when every check passes.

Usage:
  gcloud run jobs executions list ... --format=json \
    | python3 verify_executions.py --expected-total 2
"""

from __future__ import annotations

import argparse
import json
import sys


def is_terminal(execution: object) -> bool:
    """True only when the execution's status PROVES a terminal state."""
    if not isinstance(execution, dict):
        return False
    status = execution.get("status")
    if not isinstance(status, dict):
        return False
    if status.get("completionTime"):
        return True
    for condition in status.get("conditions") or []:
        if (
            isinstance(condition, dict)
            and condition.get("type") == "Completed"
            and condition.get("status") in ("True", "False")
        ):
            return True
    return False


def execution_name(execution: object) -> str:
    if isinstance(execution, dict):
        metadata = execution.get("metadata")
        if isinstance(metadata, dict) and metadata.get("name"):
            return str(metadata["name"])
    return "<unnamed>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-total",
        required=True,
        type=int,
        help="exact number of executions that must exist, all terminal",
    )
    args = parser.parse_args()
    if args.expected_total < 0:
        print(json.dumps({
            "stage_c_execution_gate": "BLOCKED",
            "ok": False,
            "reason": f"--expected-total {args.expected_total} is invalid — failing closed",
        }))
        return 1

    try:
        executions = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({
            "stage_c_execution_gate": "BLOCKED",
            "ok": False,
            "reason": "execution listing was not valid JSON — failing closed",
        }))
        return 1
    if not isinstance(executions, list):
        print(json.dumps({
            "stage_c_execution_gate": "BLOCKED",
            "ok": False,
            "reason": "execution listing had an unexpected shape — failing closed",
        }))
        return 1

    terminal = [e for e in executions if is_terminal(e)]
    nonterminal = [e for e in executions if not is_terminal(e)]
    problems: list[str] = []
    if len(executions) != args.expected_total:
        problems.append(
            f"{len(executions)} execution(s) exist, expected exactly {args.expected_total} — "
            "the pinned historical baseline plus authorized increment does not match"
        )
    if nonterminal:
        names = sorted(execution_name(e) for e in nonterminal)
        problems.append(
            f"{len(nonterminal)} active/unverifiable (nonterminal) execution(s): {names} — "
            "missing/null/malformed status is treated as nonterminal, fail-safe"
        )

    print(json.dumps({
        "stage_c_execution_gate": "verify",
        "expected_total": args.expected_total,
        "total": len(executions),
        "terminal": len(terminal),
        "nonterminal": len(nonterminal),
        "executions": sorted(execution_name(e) for e in executions),
        "problems": problems,
        "ok": not problems,
    }))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
