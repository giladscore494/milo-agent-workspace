#!/usr/bin/env python3
"""Validate the durable positive-smoke result from sanitized REST JSON files.

Inputs are Supabase REST result arrays written to temporary files by the
controller. No credentials, provider payloads, raw errors or reasoning are
accepted or printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation


def _load_array(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("expected a JSON array of objects")
    return value


def verify(
    runs: list[dict],
    checkpoints: list[dict],
    *,
    run_id: str,
    expected_attempt: int,
    max_model_calls: int,
    max_actual_cost: Decimal,
) -> tuple[list[str], dict]:
    problems: list[str] = []
    if len(runs) != 1:
        return [f"expected exactly one run row, found {len(runs)}"], {}
    run = runs[0]
    if str(run.get("id") or "") != run_id:
        problems.append("run id does not match the authorized smoke run")
    if run.get("status") != "completed":
        problems.append(f"run status is {run.get('status')!r}, expected 'completed'")
    if run.get("attempt") != expected_attempt:
        problems.append(
            f"run attempt is {run.get('attempt')!r}, expected {expected_attempt}"
        )
    if not run.get("finished_at"):
        problems.append("completed run has no finished_at timestamp")

    usage = run.get("usage")
    if not isinstance(usage, dict):
        problems.append("run usage is missing or malformed")
        usage = {}
    model_calls = usage.get("model_calls")
    if not isinstance(model_calls, int) or isinstance(model_calls, bool):
        problems.append("usage.model_calls is missing or not an integer")
    elif not 1 <= model_calls <= max_model_calls:
        problems.append(
            f"usage.model_calls is {model_calls}, expected within 1..{max_model_calls}"
        )
    try:
        actual_cost = Decimal(str(usage.get("actual_cost")))
    except (InvalidOperation, TypeError, ValueError):
        actual_cost = Decimal("-1")
        problems.append("usage.actual_cost is missing or malformed")
    if actual_cost < 0:
        if not any("actual_cost" in item for item in problems):
            problems.append("usage.actual_cost must be non-negative")
    elif actual_cost > max_actual_cost:
        problems.append(
            f"usage.actual_cost exceeds the authorized cap {max_actual_cost}"
        )

    if len(checkpoints) != 1:
        problems.append(
            f"expected exactly one latest checkpoint row, found {len(checkpoints)}"
        )
        checkpoint = {}
    else:
        checkpoint = checkpoints[0]
        if str(checkpoint.get("run_id") or "") != run_id:
            problems.append("checkpoint run_id does not match the smoke run")
        if checkpoint.get("engine_version") != "swarm_v2.1":
            problems.append("checkpoint engine_version is incompatible")
        if checkpoint.get("workflow_key") != "swarm_v2":
            problems.append("checkpoint workflow_key is incompatible")
        if checkpoint.get("phase") != "swarm_v2":
            problems.append("checkpoint phase is incompatible")
        if checkpoint.get("attempt") not in (None, expected_attempt):
            problems.append("checkpoint attempt does not match the run attempt")

    summary = {
        "semantic_smoke": "pass" if not problems else "fail",
        "run_id": run_id,
        "status": run.get("status"),
        "attempt": run.get("attempt"),
        "model_calls": model_calls,
        "actual_cost": str(actual_cost) if actual_cost >= 0 else None,
        "engine_version": checkpoint.get("engine_version"),
        "workflow_key": checkpoint.get("workflow_key"),
    }
    return problems, summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_file")
    parser.add_argument("checkpoint_file")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-attempt", type=int, required=True)
    parser.add_argument("--max-model-calls", type=int, required=True)
    parser.add_argument("--max-actual-cost", type=Decimal, required=True)
    try:
        args = parser.parse_args(argv)
        runs = _load_array(args.run_file)
        checkpoints = _load_array(args.checkpoint_file)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"SEMANTIC SMOKE ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2
    problems, summary = verify(
        runs,
        checkpoints,
        run_id=args.run_id,
        expected_attempt=args.expected_attempt,
        max_model_calls=args.max_model_calls,
        max_actual_cost=args.max_actual_cost,
    )
    for problem in problems:
        print(f"SEMANTIC SMOKE VIOLATION: {problem}", file=sys.stderr)
    print(json.dumps(summary, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
