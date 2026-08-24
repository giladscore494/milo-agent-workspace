#!/usr/bin/env python3
"""Resolve and verify the ACTUAL serving Cloud Run revision.

The service template alone is never authoritative: a safe template can
coexist with an unsafe revision still receiving traffic. The controller
therefore resolves ``status.latestReadyRevisionName``, describes exactly
that revision, and requires it to receive 100% of traffic before any
environment contract is evaluated against it.

Like every parser in this directory, inputs are FILE ARGUMENTS — never
stdin.

Usage:
    parse_serving_state.py resolve <service.json>
        Prints status.latestReadyRevisionName. Exit 2 when absent.
    parse_serving_state.py verify <service.json> <revision.json>
        Asserts the described revision IS the latest ready revision and
        that it receives 100% of the service's traffic.

Exit codes: 0 verified; 1 violation; 2 usage/parse error.
"""

from __future__ import annotations

import json
import sys


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def resolve(service: dict) -> str:
    return str((service.get("status") or {}).get("latestReadyRevisionName") or "")


def verify(service: dict, revision: dict) -> list[str]:
    problems: list[str] = []
    ready = resolve(service)
    if not ready:
        return ["service has no latestReadyRevisionName"]
    described = str((revision.get("metadata") or {}).get("name") or "")
    if described != ready:
        problems.append(
            f"described revision {described or '<unnamed>'!r} is not the latest ready revision {ready!r}")
    traffic = (service.get("status") or {}).get("traffic") or []
    try:
        ready_percent = sum(
            int(item.get("percent") or 0) for item in traffic
            if item.get("revisionName") == ready
        )
    except (TypeError, ValueError):
        return problems + ["traffic assignment is malformed"]
    if ready_percent != 100:
        problems.append(
            f"latest ready revision {ready!r} receives {ready_percent}% of traffic, expected 100%")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "resolve" and len(argv) == 2:
        try:
            service = _load(argv[1])
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot parse {argv[1]}: {type(exc).__name__}", file=sys.stderr)
            return 2
        ready = resolve(service)
        if not ready:
            print("service has no latestReadyRevisionName", file=sys.stderr)
            return 2
        print(ready)
        return 0
    if len(argv) == 3 and argv[0] == "verify":
        try:
            service = _load(argv[1])
            revision = _load(argv[2])
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot parse inputs: {type(exc).__name__}", file=sys.stderr)
            return 2
        problems = verify(service, revision)
        for problem in problems:
            print(f"SERVING VIOLATION: {problem}")
        if not problems:
            print(f"serving revision OK: {resolve(service)} is ready and receives 100% of traffic")
        return 1 if problems else 0
    print("usage: parse_serving_state.py resolve <service.json> | verify <service.json> <revision.json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
