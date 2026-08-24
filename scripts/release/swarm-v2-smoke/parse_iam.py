#!/usr/bin/env python3
"""Validate Cloud Run IAM policy JSON for the Swarm V2 smoke preflight.

Reads the policy from a FILE ARGUMENT — never stdin. The previous smoke's
IAM preflight failed because a piped `gcloud ... --format=json` and a
`python3 - <<'PY'` heredoc competed for the same stdin; this parser makes
that failure mode structurally impossible.

Usage:
    parse_iam.py <policy.json> --required-invoker EMAIL [--forbid-public]

Exit codes: 0 policy satisfied; 1 violation; 2 usage/parse error.
"""

from __future__ import annotations

import argparse
import json
import sys


def check(policy: dict, required_invoker: str, forbid_public: bool) -> list[str]:
    problems: list[str] = []
    bindings = policy.get("bindings", []) or []
    invokers: set[str] = set()
    for binding in bindings:
        if binding.get("role") == "roles/run.invoker":
            invokers.update(binding.get("members", []) or [])
    wanted = f"serviceAccount:{required_invoker}"
    if wanted not in invokers:
        problems.append(f"roles/run.invoker is missing {wanted}")
    if forbid_public:
        for member in ("allUsers", "allAuthenticatedUsers"):
            if member in invokers:
                problems.append(f"roles/run.invoker must not include {member}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("policy_file")
    parser.add_argument("--required-invoker", required=True)
    parser.add_argument("--forbid-public", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        with open(args.policy_file, encoding="utf-8") as handle:
            policy = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot parse {args.policy_file}: {type(exc).__name__}", file=sys.stderr)
        return 2
    problems = check(policy, args.required_invoker, args.forbid_public)
    for problem in problems:
        print(f"IAM VIOLATION: {problem}")
    if not problems:
        print("IAM policy OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
