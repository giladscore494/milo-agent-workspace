#!/usr/bin/env python3
"""Parse Cloud Run job execution JSON dumps for the smoke controller.

Like every parser in this directory it reads a FILE ARGUMENT, never stdin.

Usage:
    parse_executions.py active-count <executions-list.json>
        Prints the number of executions with no terminal Completed condition.
    parse_executions.py verdict <execution-describe.json>
        Prints one of: succeeded | failed:<succeeded>:<failed> | running

Exit codes: 0 parsed; 2 usage/parse error.
"""

from __future__ import annotations

import json
import sys


def _conditions(doc: dict) -> dict[str, str]:
    return {c.get("type"): c.get("status")
            for c in doc.get("status", {}).get("conditions", []) or []}


def active_count(rows: list) -> int:
    active = 0
    for row in rows or []:
        if _conditions(row).get("Completed") not in {"True", "False"}:
            active += 1
    return active


def verdict(doc: dict) -> str:
    conditions = _conditions(doc)
    succeeded = doc.get("status", {}).get("succeededCount", 0) or 0
    failed = doc.get("status", {}).get("failedCount", 0) or 0
    if conditions.get("Completed") == "True":
        return "succeeded"
    if conditions.get("Completed") == "False":
        return f"failed:{succeeded}:{failed}"
    return "running"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"active-count", "verdict"}:
        print("usage: parse_executions.py active-count|verdict <file.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot parse {argv[1]}: {type(exc).__name__}", file=sys.stderr)
        return 2
    if argv[0] == "active-count":
        if not isinstance(document, list):
            print("active-count expects a JSON array", file=sys.stderr)
            return 2
        print(active_count(document))
    else:
        print(verdict(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
