"""Exact Stage C cap/posture verification for BOTH runtime surfaces.

Compares every cap in STAGE_C_CAPS (the single source of expected values in
stage-c-env.sh) against the live env of the worker job AND the API service,
immediately before run creation, and every provider limit in
STAGE_C_WORKER_PROVIDER_LIMITS against the Worker ONLY. Fails on any
missing, changed, or unexpected value — an extra budget/cap variable that
is not in the expected set also fails, so nothing can be loosened out of
band; an unexpected MILO_PROVIDER_* variable on the Worker fails unless
pinned, and ANY MILO_PROVIDER_* variable on the API fails (provider
scheduling belongs to the Worker alone). Also verifies both surfaces run
the pinned release images (the production-image blocker: Stage C must
deploy the exact STAGE_C_RELEASE_SHA runtime images before enabling any
execution surface), the exact flag posture, and the provider-secret
posture: the Worker holds KIMI_API_KEY only as a Secret Manager binding
(never a literal value) and the API holds neither KIMI_API_KEY nor
MOONSHOT_API_KEY in any form. Error messages name variables only — secret
VALUES are never printed.

Usage:
  python3 verify_caps.py --worker-json <file> --api-json <file>

  <file>s are the outputs of
    gcloud run jobs describe <worker> --format=json
    gcloud run services describe <api> --format=json

Env (all exported by stage-c-env.sh): STAGE_C_CAPS,
STAGE_C_WORKER_PROVIDER_LIMITS, STAGE_C_REGISTRY, STAGE_C_RELEASE_SHA.
Exit 0 only if every check passes.
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

# Provider scheduling configuration is WORKER-ONLY: every MILO_PROVIDER_*
# variable on the Worker must appear in STAGE_C_WORKER_PROVIDER_LIMITS with
# the exact pinned value, and none may exist on the API at all.
PROVIDER_PREFIX = "MILO_PROVIDER_"

# The exact seven settings of the pinned Attempt 7 operating envelope
# (deliberately below the operator-confirmed Kimi Tier 2 ceiling). A
# STAGE_C_WORKER_PROVIDER_LIMITS missing any of them fails closed.
REQUIRED_PROVIDER_LIMIT_KEYS = (
    "MILO_PROVIDER_MAX_CONCURRENCY",
    "MILO_PROVIDER_RPM_LIMIT",
    "MILO_PROVIDER_TPM_LIMIT",
    "MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES",
    "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS",
    "MILO_PROVIDER_BACKOFF_BASE_SECONDS",
    "MILO_PROVIDER_BACKOFF_MAX_SECONDS",
)

# Every provider-key alias the Worker accepts (see kill-switch.sh).
PROVIDER_SECRET_ALIASES = ("KIMI_API_KEY", "MOONSHOT_API_KEY")


def container_of(spec: dict, kind: str) -> dict:
    if kind == "worker":
        return spec["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    return spec["spec"]["template"]["spec"]["containers"][0]


def env_of(container: dict) -> tuple[dict[str, str], set[str]]:
    env = {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}
    secret_refs = {e["name"] for e in container.get("env", []) if "valueFrom" in e}
    return env, secret_refs


def parse_pairs(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def expected_caps() -> dict[str, str]:
    return parse_pairs(os.environ.get("STAGE_C_CAPS", ""))


def expected_provider_limits() -> dict[str, str]:
    return parse_pairs(os.environ.get("STAGE_C_WORKER_PROVIDER_LIMITS", ""))


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


def check_provider_limits(worker_env: dict[str, str], api_env: dict[str, str], api_secrets: set[str], limits: dict[str, str], problems: list[str]) -> None:
    # Every pinned provider limit exact on the Worker; missing or changed
    # values fail closed.
    for key, expected in limits.items():
        actual = worker_env.get(key)
        if actual is None:
            problems.append(f"worker: provider limit {key} is MISSING (expected {expected!r})")
        elif actual != expected:
            problems.append(f"worker: provider limit {key}={actual!r} differs from pinned {expected!r}")
    # Any unexpected MILO_PROVIDER_* variable on the Worker fails unless
    # pinned (name only — values are never assumed printable).
    for key in sorted(worker_env):
        if key.startswith(PROVIDER_PREFIX) and key not in limits:
            problems.append(f"worker: unexpected provider variable {key} is not pinned in STAGE_C_WORKER_PROVIDER_LIMITS")
    # ANY MILO_PROVIDER_* variable on the API fails: provider scheduling
    # configuration belongs to the Worker alone.
    for key in sorted(set(api_env) | api_secrets):
        if key.startswith(PROVIDER_PREFIX):
            problems.append(f"api: provider variable {key} must NEVER be set on the API service")


def check_provider_secret_posture(worker_env: dict[str, str], worker_secrets: set[str], api_env: dict[str, str], api_secrets: set[str], problems: list[str]) -> None:
    # Worker holds the provider key ONLY as a Secret Manager binding during
    # the enabled posture — never as a literal env value. The API holds it
    # in no form. Names only; secret VALUES are never read or printed.
    if "KIMI_API_KEY" not in worker_secrets:
        problems.append("worker: provider key is not bound")
    for alias in PROVIDER_SECRET_ALIASES:
        if alias in worker_env:
            problems.append(f"worker: {alias} is present as a LITERAL env value — the provider key may only be a secret binding")
        if alias in api_secrets:
            problems.append(f"api: provider secret {alias} must NEVER be bound to the API service")
        if alias in api_env:
            problems.append(f"api: {alias} is present as a literal env value — the API must NEVER hold the provider key")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-json", required=True)
    parser.add_argument("--api-json", required=True)
    args = parser.parse_args()

    caps = expected_caps()
    problems: list[str] = []
    if not caps:
        problems.append("STAGE_C_CAPS is empty — no expected cap values to verify against; failing closed")

    provider_limits = expected_provider_limits()
    if not provider_limits:
        problems.append("STAGE_C_WORKER_PROVIDER_LIMITS is empty — no pinned provider envelope to verify against; failing closed")
    else:
        missing_limit_keys = [k for k in REQUIRED_PROVIDER_LIMIT_KEYS if k not in provider_limits]
        if missing_limit_keys:
            problems.append(f"STAGE_C_WORKER_PROVIDER_LIMITS is missing pinned setting(s) {missing_limit_keys} — failing closed")

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

    # Production-image blocker: both surfaces MUST run the pinned release
    # images (the exact STAGE_C_RELEASE_SHA runtime commit) before any
    # execution surface is enabled.
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

    # Provider operating envelope: exact on the Worker, absent on the API.
    if provider_limits:
        check_provider_limits(worker_env, api_env, api_secrets, provider_limits, problems)

    # Provider-secret posture: worker binding only, no literals anywhere,
    # nothing on the API.
    check_provider_secret_posture(worker_env, worker_secrets, api_env, api_secrets, problems)

    # Flag posture: worker is the sole paid enforcement point.
    if worker_env.get("MILO_ENABLE_PAID_EXECUTION") != ENABLED:
        problems.append("worker: paid execution flag is not enabled (run 03-enable-stage-c.md first)")
    if api_env.get("MILO_ENABLE_PAID_EXECUTION") != DISABLED:
        problems.append("api: MILO_ENABLE_PAID_EXECUTION must stay false — the API never holds the paid flag")
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
    print(
        f"OK: all {len(caps)} caps exact on worker+api, "
        f"all {len(provider_limits)} provider limits exact on worker only, "
        f"release images {release_sha[:12]}… deployed, flag posture correct"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
