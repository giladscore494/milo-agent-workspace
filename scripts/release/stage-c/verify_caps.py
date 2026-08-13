"""Exact Stage C cap/posture verification for BOTH runtime surfaces.

Compares every cap in STAGE_C_CAPS (the single source of expected values in
stage-c-env.sh) against the live env of the worker job AND the API service,
immediately before run creation. Fails on any missing, changed, or
unexpected value — an extra budget/cap variable that is not in the expected
set also fails, so nothing can be loosened out of band. Also verifies both
surfaces run the signed-off release images (the production-image blocker:
Stage C must deploy the 30b05bc release images before enabling any
execution surface) and the exact flag posture.

Usage:
  python3 verify_caps.py --worker-json <file> --api-json <file>

  <file>s are the outputs of
    gcloud run jobs describe <worker> --format=json
    gcloud run services describe <api> --format=json

Env (all exported by stage-c-env.sh): STAGE_C_CAPS, STAGE_C_REGISTRY,
STAGE_C_RELEASE_SHA. Exit 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ENABLED = "true"  # expected value of a deliberately operator-enabled flag
DISABLED = "false"

# Every variable that participates in budget/cap enforcement. Any live env
# key matching these prefixes must appear in STAGE_C_CAPS with the exact
# expected value — unknown extras are treated as tampering, not tolerated.
CAP_PREFIXES = ("MILO_MAX_", "MILO_DAILY_", "MILO_ESTIMATED_COST")


def container_of(spec: dict, kind: str) -> dict:
    if kind == "worker":
        return spec["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    return spec["spec"]["template"]["spec"]["containers"][0]


def env_of(container: dict) -> tuple[dict[str, str], set[str]]:
    env = {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}
    secret_refs = {e["name"] for e in container.get("env", []) if "valueFrom" in e}
    return env, secret_refs


def expected_caps() -> dict[str, str]:
    caps: dict[str, str] = {}
    for pair in os.environ.get("STAGE_C_CAPS", "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            caps[key.strip()] = value.strip()
    return caps


def check_caps(surface: str, env: dict[str, str], caps: dict[str, str], problems: list[str]) -> None:
    for key, expected in caps.items():
        actual = env.get(key)
        if actual is None:
            problems.append(f"{surface}: cap {key} is MISSING (expected {expected!r})")
        elif actual != expected:
            problems.append(f"{surface}: cap {key}={actual!r} differs from expected {expected!r}")
    for key in sorted(env):
        if key.startswith(CAP_PREFIXES) and key not in caps:
            problems.append(f"{surface}: unexpected budget/cap variable {key}={env[key]!r} not in STAGE_C_CAPS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-json", required=True)
    parser.add_argument("--api-json", required=True)
    args = parser.parse_args()

    caps = expected_caps()
    problems: list[str] = []
    if not caps:
        problems.append("STAGE_C_CAPS is empty — no expected cap values to verify against; failing closed")

    registry = os.environ.get("STAGE_C_REGISTRY", "")
    release_sha = os.environ.get("STAGE_C_RELEASE_SHA", "")
    if not registry or not release_sha:
        problems.append("STAGE_C_REGISTRY / STAGE_C_RELEASE_SHA not set — cannot verify release images; failing closed")

    with open(args.worker_json, encoding="utf-8") as fh:
        worker = container_of(json.load(fh), "worker")
    with open(args.api_json, encoding="utf-8") as fh:
        api = container_of(json.load(fh), "api")

    worker_env, worker_secrets = env_of(worker)
    api_env, api_secrets = env_of(api)

    # Production-image blocker: both surfaces MUST run the signed-off
    # release images before any execution surface is enabled.
    if registry and release_sha:
        expected_worker_image = f"{registry}/worker:{release_sha}"
        expected_api_image = f"{registry}/api:{release_sha}"
        if worker.get("image") != expected_worker_image:
            problems.append(f"worker: image {worker.get('image')!r} is not the signed-off release {expected_worker_image!r}")
        if api.get("image") != expected_api_image:
            problems.append(f"api: image {api.get('image')!r} is not the signed-off release {expected_api_image!r}")

    # Exact cap values on BOTH surfaces.
    check_caps("worker", worker_env, caps, problems)
    check_caps("api", api_env, caps, problems)

    # Flag posture: worker is the sole paid enforcement point.
    if worker_env.get("MILO_ENABLE_PAID_EXECUTION") != ENABLED:
        problems.append("worker: paid execution flag is not enabled (run 03-enable-stage-c.md first)")
    if "KIMI_API_KEY" not in worker_secrets:
        problems.append("worker: provider key is not bound")
    if api_env.get("MILO_ENABLE_PAID_EXECUTION") != DISABLED:
        problems.append("api: MILO_ENABLE_PAID_EXECUTION must stay false — the API never holds the paid flag")
    if "KIMI_API_KEY" in api_secrets or "MOONSHOT_API_KEY" in api_secrets:
        problems.append("api: provider key must NEVER be bound to the API service")
    if api_env.get("MILO_ENABLE_RUN_CREATION") != ENABLED:
        problems.append("api: run creation flag is not enabled")
    if api_env.get("JOB_LAUNCHER") != "cloud_run":
        problems.append(f"api: JOB_LAUNCHER={api_env.get('JOB_LAUNCHER')!r}, expected 'cloud_run'")
    for flag in ("MILO_ENABLE_PROPOSAL_MUTATIONS", "MILO_ENABLE_PROPOSAL_READS", "MILO_ENABLE_RUN_CANCELLATION", "MILO_ENABLE_EXECUTION_CONTROL"):
        if api_env.get(flag) != DISABLED:
            problems.append(f"api: {flag}={api_env.get(flag)!r}, expected 'false'")

    if problems:
        print("STAGE C CAP VERIFICATION FAILED — do NOT create the run:")
        for problem in problems:
            print(f"  - {problem}")
        print("Fix the posture (03-enable-stage-c.md) or run kill-switch.sh, then re-verify.")
        return 1
    print(f"OK: all {len(caps)} caps exact on worker+api, release images {release_sha[:12]}… deployed, flag posture correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
