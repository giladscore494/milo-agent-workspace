#!/usr/bin/env python3
"""Validate the deployed Swarm V2 worker/API contract from a gcloud JSON dump.

Reads the JSON from a FILE ARGUMENT, never from stdin: the previous smoke
attempt failed because piped JSON and a Python heredoc competed for stdin.
This parser has exactly one input channel and no interactive reads.

Usage:
    parse_env_contract.py worker <describe.json> [--smoke-active] [--service-account EMAIL]
    parse_env_contract.py api    <describe.json> [--smoke-active] [--service-account EMAIL]

The input may be a Cloud Run SERVICE describe, a JOB describe, or a
REVISION describe: the API contract is verified against the actual
serving revision (resolved by the controller via latestReadyRevisionName),
never against the service template alone.

Exit codes: 0 contract satisfied; 1 contract violation; 2 usage/parse error.
Values are compared, never printed for secret-backed variables.
"""

from __future__ import annotations

import json
import sys

# Expected non-secret variable VALUES for the production Swarm V2 smoke.
# tests/test_release_tooling_swarm_smoke.py cross-checks every name here
# against the names the backend actually reads (BudgetConfig.ENV_KEYS,
# ProviderLimitsConfig.ENV_KEYS and the worker's os.getenv calls), so this
# contract cannot silently drift from the code.
WORKER_EXPECTED = {
    "ENVIRONMENT": "production",
    "MILO_MAX_MODEL_CALLS_PER_RUN": "200",
    "MILO_MAX_COST_PER_RUN": "3.00",
    "MILO_MAX_ESTIMATED_COST_PER_RUN": "4.00",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN": "900000",
    "MILO_MAX_RUN_DURATION_SECONDS": "3300",
    "MILO_MAX_RETRIES": "15",
    "MILO_MAX_CONCURRENT_RUNS_PER_USER": "1",
    "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT": "1",
    "MILO_PROVIDER_MAX_CONCURRENCY": "8",
    "MILO_SWARM_MAX_ACTIVE_WORKERS": "8",
    "MILO_COMMANDER_MODEL": "kimi-k2.6",
    "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k2.6",
    "MILO_SWARM_WORKER_MODEL": "kimi-k2.6",
    "MILO_MODEL_BASE_URL": "https://api.moonshot.ai/v1",
    "JOB_LAUNCHER": "disabled",
}
API_EXPECTED = {
    "ENVIRONMENT": "production",
    "CLOUD_RUN_WORKER_JOB": "milo-agent-worker",
    "GCP_PROJECT_ID": "big-cabinet-457321-t7",
    "GCP_REGION": "us-central1",
    "JOB_LAUNCHER": "disabled",
    "MILO_MAX_CONCURRENT_RUNS_PER_USER": "1",
    "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT": "1",
}
# Execution flags stay OFF at rest. During the authorized smoke window the
# operator temporarily enables exactly the smoke set; --smoke-active checks
# that posture instead.
FLAGS_AT_REST = {
    "MILO_ENABLE_RUN_CREATION": "false",
    "MILO_ENABLE_PROPOSAL_MUTATIONS": "false",
    "MILO_ENABLE_PROPOSAL_READS": "false",
    "MILO_ENABLE_RUN_CANCELLATION": "false",
    "MILO_ENABLE_EXECUTION_CONTROL": "false",
    "MILO_ENABLE_PAID_EXECUTION": "false",
}
WORKER_FLAGS_SMOKE = {**FLAGS_AT_REST, "MILO_ENABLE_PAID_EXECUTION": "true"}
API_FLAGS_SMOKE = {**FLAGS_AT_REST, "MILO_ENABLE_RUN_CREATION": "true",
                   "MILO_ENABLE_RUN_CANCELLATION": "true"}
PROVIDER_KEY_NAMES = ("KIMI_API_KEY", "MOONSHOT_API_KEY")
SECRET_BACKED = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                 "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")


def _resolved_spec(document: dict) -> dict:
    spec = document.get("spec", {})
    if "containers" in spec:  # Cloud Run Revision: containers live directly on spec
        return spec
    spec = spec.get("template", {}).get("spec", {})
    if "template" in spec:  # Cloud Run Job: one more template layer
        spec = spec.get("template", {}).get("spec", {})
    return spec


def _containers(document: dict) -> list[dict]:
    return _resolved_spec(document).get("containers", []) or []


def _service_account(document: dict) -> str:
    return str(_resolved_spec(document).get("serviceAccountName") or "")


def check(role: str, document: dict, smoke_active: bool,
          expected_service_account: str | None = None) -> list[str]:
    problems: list[str] = []
    containers = _containers(document)
    if len(containers) != 1:
        return [f"expected exactly one container, found {len(containers)}"]
    env = containers[0].get("env", []) or []
    plain = {e["name"]: e.get("value") for e in env if "value" in e}
    secret = {e["name"] for e in env if "valueFrom" in e}

    expected = dict(WORKER_EXPECTED if role == "worker" else API_EXPECTED)
    if role == "worker":
        expected.update(WORKER_FLAGS_SMOKE if smoke_active else FLAGS_AT_REST)
    else:
        expected.update(API_FLAGS_SMOKE if smoke_active else FLAGS_AT_REST)
    for name, value in sorted(expected.items()):
        if name not in plain:
            problems.append(f"missing environment variable: {name}")
        elif plain[name] != value:
            problems.append(f"{name} is {plain[name]!r}, expected {value!r}")

    for name in SECRET_BACKED:
        if name not in secret:
            problems.append(f"{name} must be bound from Secret Manager")
        if name in plain:
            problems.append(f"{name} must never be a plain value")

    for name in PROVIDER_KEY_NAMES:
        if name in plain:
            problems.append(f"{name} must never be a plain value")
        if role == "api" and name in secret:
            problems.append(f"{name} must never be bound to the API service")
        if role == "worker":
            if smoke_active and name == "KIMI_API_KEY" and name not in secret:
                problems.append("KIMI_API_KEY must be secret-bound to the worker during the smoke window")
            if not smoke_active and name in secret:
                problems.append(f"{name} must be unbound from the worker at rest")

    # Worker-mutation surfaces are unusable without a verified service
    # identity boundary: execution control may never be enabled without a
    # concrete worker audience and a non-wildcard identity allowlist
    # (mirrors backend/production_config.py rule 5).
    if plain.get("MILO_ENABLE_EXECUTION_CONTROL") == "true":
        audience = (plain.get("MILO_WORKER_AUDIENCE") or "").strip()
        allowlist = (plain.get("MILO_APPROVED_WORKER_IDENTITIES") or "").strip()
        if not audience or audience == "*":
            problems.append("MILO_ENABLE_EXECUTION_CONTROL requires a concrete MILO_WORKER_AUDIENCE")
        entries = [item.strip() for item in allowlist.split(",") if item.strip()]
        if not entries or any(entry == "*" or "@" not in entry for entry in entries):
            problems.append("MILO_ENABLE_EXECUTION_CONTROL requires an explicit, non-wildcard MILO_APPROVED_WORKER_IDENTITIES allowlist")

    if expected_service_account:
        actual = _service_account(document)
        if actual != expected_service_account:
            problems.append(
                f"runtime service account is {actual or '<unset>'!r}, expected {expected_service_account!r}")
    return problems


def main(argv: list[str]) -> int:
    smoke_active = "--smoke-active" in argv
    expected_service_account = None
    args: list[str] = []
    skip_next = False
    for index, item in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if item == "--smoke-active":
            continue
        if item == "--service-account":
            if index + 1 >= len(argv):
                print("--service-account requires an email argument", file=sys.stderr)
                return 2
            expected_service_account = argv[index + 1]
            skip_next = True
            continue
        args.append(item)
    if len(args) != 2 or args[0] not in {"worker", "api"}:
        print("usage: parse_env_contract.py worker|api <describe.json> [--smoke-active] [--service-account EMAIL]", file=sys.stderr)
        return 2
    try:
        with open(args[1], encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot parse {args[1]}: {type(exc).__name__}", file=sys.stderr)
        return 2
    problems = check(args[0], document, smoke_active, expected_service_account)
    for problem in problems:
        print(f"CONTRACT VIOLATION [{args[0]}]: {problem}")
    if not problems:
        print(f"contract OK [{args[0]}] ({'smoke-active' if smoke_active else 'at-rest'} posture)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
