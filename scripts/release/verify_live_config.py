"""Verify LIVE Cloud Run service/job configuration against production policy.

Reads the actual `gcloud run services describe --format json` and
`gcloud run jobs describe --format json` output (LIVE state, not a manifest)
and checks that every required environment variable, Secret Manager reference,
execution flag, budget cap and identity holds the EXACT expected value.

Emits one `STATUS|name|detail` line per finding on stdout (STATUS is
PASS/WARN/BLOCKED/MANUAL/NOT_APPLICABLE). It NEVER prints a secret value — only
env-var NAMES, secret-reference NAMES/versions and non-secret values.

Fail-closed rules (a value that is absent / empty / an alternative
representation is BLOCKED, never accepted as "disabled"):
  - execution flags must be a PLAIN env var equal to exactly "false";
  - ENVIRONMENT / JOB_LAUNCHER / audiences / identities must equal exactly
    their expected value;
  - ALLOWED_CORS_ORIGINS must normalize to exactly the approved origin set;
  - budgets must be a plain numeric env var strictly > 0 (and <= an optional
    Stage-A maximum);
  - each secret reference must point at the exact expected resource, and the
    Redis reference must pin the exact expected numeric version (never latest).

Exit status is always 0; verdicts travel in the findings.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXECUTION_FLAGS = [
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
]
BUDGETS = [
    "MILO_MAX_COST_PER_RUN",
    "MILO_DAILY_USER_BUDGET",
    "MILO_DAILY_PROJECT_BUDGET",
    "MILO_MAX_MODEL_CALLS_PER_RUN",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN",
    "MILO_MAX_RUN_DURATION_SECONDS",
]
# Optional Stage-A upper bounds; a configured budget above these is a red flag.
BUDGET_MAX = {
    "MILO_MAX_COST_PER_RUN": 25.0,
    "MILO_DAILY_USER_BUDGET": 100.0,
    "MILO_DAILY_PROJECT_BUDGET": 500.0,
    "MILO_MAX_MODEL_CALLS_PER_RUN": 500.0,
    "MILO_MAX_TOTAL_TOKENS_PER_RUN": 5_000_000.0,
    "MILO_MAX_RUN_DURATION_SECONDS": 7200.0,
}

_FINDINGS: list[str] = []


def emit(status: str, name: str, detail: str) -> None:
    detail = detail.replace("\n", " ").replace("|", "/")
    _FINDINGS.append(f"{status}|{name}|{detail}")


def _containers(describe: dict) -> list[dict]:
    spec = (((describe or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    if "containers" in spec:
        return spec.get("containers") or []
    inner = (spec.get("template") or {}).get("spec") or {}
    return inner.get("containers") or []


def _service_account(describe: dict) -> str:
    spec = (((describe or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    if "serviceAccountName" in spec:
        return spec.get("serviceAccountName") or ""
    inner = (spec.get("template") or {}).get("spec") or {}
    return inner.get("serviceAccountName") or ""


def _env_maps(describe: dict):
    """Return (plain, secret_ref) where secret_ref[name] = (resource, version)."""
    plain: dict[str, str] = {}
    secret_ref: dict[str, tuple] = {}
    for container in _containers(describe):
        for entry in container.get("env") or []:
            name = entry.get("name")
            if not name:
                continue
            if "value" in entry and entry.get("value") is not None:
                plain[name] = str(entry.get("value"))
            vf = entry.get("valueFrom") or {}
            skr = vf.get("secretKeyRef") or {}
            if skr.get("name"):
                secret_ref[name] = (str(skr.get("name")), str(skr.get("key") or ""))
    return plain, secret_ref


def _norm_set(value: str) -> set:
    return {p.strip() for p in value.split(",") if p.strip()}


def check_exact_plain(label: str, plain: dict, name: str, expected: str) -> None:
    if name not in plain:
        emit("BLOCKED", f"live:{label}:{name}", f"{name} is not configured as a plain env var on the live {label} (expected exactly '{expected}')")
    elif plain[name] != expected:
        emit("BLOCKED", f"live:{label}:{name}", f"{name} is '{plain[name]}', expected exactly '{expected}'")
    else:
        emit("PASS", f"live:{label}:{name}", f"{name} equals '{expected}'")


def check_exact_set(label: str, plain: dict, name: str, expected: str) -> None:
    if name not in plain:
        emit("BLOCKED", f"live:{label}:{name}", f"{name} is not configured on the live {label} (expected exactly '{expected}')")
        return
    got = _norm_set(plain[name])
    want = _norm_set(expected)
    if got != want:
        emit("BLOCKED", f"live:{label}:{name}", f"{name} normalizes to {sorted(got)}, expected exactly {sorted(want)}")
    else:
        emit("PASS", f"live:{label}:{name}", f"{name} matches the expected value set exactly")


def check_flag_false(label: str, plain: dict, secret_ref: dict, flag: str) -> None:
    if flag in secret_ref:
        emit("BLOCKED", f"live:{label}:flag:{flag}", f"execution flag {flag} is a secret reference, not a plain 'false' env var")
        return
    if flag not in plain:
        emit("BLOCKED", f"live:{label}:flag:{flag}", f"execution flag {flag} is missing; production must set it to exactly 'false'")
        return
    val = plain[flag]
    if val != "false":
        emit("BLOCKED", f"live:{label}:flag:{flag}", f"execution flag {flag} is '{val}', must be exactly 'false' (not empty/0/no/off/true)")
    else:
        emit("PASS", f"live:{label}:flag:{flag}", f"{flag} is exactly 'false'")


def check_budget(label: str, plain: dict, secret_ref: dict, budget: str) -> None:
    if budget in secret_ref:
        emit("BLOCKED", f"live:{label}:budget:{budget}", f"{budget} is a secret reference, not a plain numeric env var")
        return
    if budget not in plain:
        emit("BLOCKED", f"live:{label}:budget:{budget}", f"budget cap {budget} is missing from the live {label}")
        return
    raw = plain[budget].strip()
    try:
        num = float(raw)
    except ValueError:
        emit("BLOCKED", f"live:{label}:budget:{budget}", f"{budget}='{raw}' is not numeric")
        return
    if num <= 0:
        emit("BLOCKED", f"live:{label}:budget:{budget}", f"{budget}={raw} must be strictly greater than zero")
        return
    cap = BUDGET_MAX.get(budget)
    if cap is not None and num > cap:
        emit("BLOCKED", f"live:{label}:budget:{budget}", f"{budget}={raw} exceeds the Stage-A maximum {cap}")
        return
    emit("PASS", f"live:{label}:budget:{budget}", f"{budget}={raw} (nonzero, within Stage-A bounds)")


def check_secret_ref(label: str, secret_ref: dict, env_name: str, resource: str, version: str = "") -> None:
    if env_name not in secret_ref:
        emit("BLOCKED", f"live:{label}:secret-ref:{env_name}", f"{env_name} is not wired to a Secret Manager reference on the live {label}")
        return
    got_res, got_ver = secret_ref[env_name]
    if got_res != resource:
        emit("BLOCKED", f"live:{label}:secret-ref:{env_name}", f"{env_name} references secret '{got_res}', expected '{resource}'")
        return
    if version:
        if got_ver != version:
            emit("BLOCKED", f"live:{label}:secret-ref:{env_name}", f"{env_name} pins version '{got_ver}', expected exact version '{version}' (never 'latest')")
            return
        emit("PASS", f"live:{label}:secret-ref:{env_name}", f"{env_name} references {resource} pinned at version {version}")
    else:
        emit("PASS", f"live:{label}:secret-ref:{env_name}", f"{env_name} references Secret Manager resource {resource}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service-json", required=True)
    p.add_argument("--job-json", required=True)
    p.add_argument("--expected-api-sa", required=True)
    p.add_argument("--expected-worker-sa", required=True)
    p.add_argument("--expected-gateway-sa", required=True)
    p.add_argument("--expected-environment", default="production")
    p.add_argument("--expected-production-origin", required=True)
    p.add_argument("--expected-api-url", required=True)
    p.add_argument("--supabase-url-secret", required=True)
    p.add_argument("--supabase-key-secret", required=True)
    p.add_argument("--provider-secret", required=True)
    p.add_argument("--redis-secret", required=True)
    p.add_argument("--redis-secret-version", default="", help="exact numeric Redis secret version to require (never latest)")
    p.add_argument("--expected-redis-url", default="")
    args = p.parse_args()

    def load(path: str):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    svc = load(args.service_json)
    job = load(args.job_json)

    # ---- API service ----
    if svc is None:
        emit("BLOCKED", "live:api", "could not read a valid Cloud Run API service description (fail closed; not accepted as configured)")
    else:
        plain, secret_ref = _env_maps(svc)
        sa = _service_account(svc)
        if sa != args.expected_api_sa:
            emit("BLOCKED", "live:api:sa", f"API runs as '{sa or '(none)'}', expected exactly '{args.expected_api_sa}'")
        else:
            emit("PASS", "live:api:sa", f"API runtime identity is exactly {sa}")
        check_exact_plain("api", plain, "ENVIRONMENT", args.expected_environment)
        check_exact_set("api", plain, "ALLOWED_CORS_ORIGINS", args.expected_production_origin)
        check_exact_plain("api", plain, "MILO_GATEWAY_AUDIENCE", args.expected_api_url)
        check_exact_set("api", plain, "MILO_APPROVED_GATEWAY_IDENTITIES", args.expected_gateway_sa)
        check_exact_set("api", plain, "MILO_APPROVED_WORKER_IDENTITIES", args.expected_worker_sa)
        check_exact_plain("api", plain, "JOB_LAUNCHER", "disabled")
        check_exact_plain("api", plain, "GATEWAY_ALLOW_EXECUTION_ROUTES", "false")
        if args.expected_redis_url:
            check_exact_plain("api", plain, "UPSTASH_REDIS_REST_URL", args.expected_redis_url)
        for flag in EXECUTION_FLAGS:
            check_flag_false("api", plain, secret_ref, flag)
        for budget in BUDGETS:
            check_budget("api", plain, secret_ref, budget)
        check_secret_ref("api", secret_ref, "SUPABASE_URL", args.supabase_url_secret)
        check_secret_ref("api", secret_ref, "SUPABASE_SECRET_KEY", args.supabase_key_secret)
        check_secret_ref("api", secret_ref, "UPSTASH_REDIS_REST_TOKEN", args.redis_secret, args.redis_secret_version)

    # ---- Worker job ----
    if job is None:
        emit("BLOCKED", "live:worker", "could not read a valid Cloud Run worker job description (fail closed)")
    else:
        plain, secret_ref = _env_maps(job)
        sa = _service_account(job)
        if sa != args.expected_worker_sa:
            emit("BLOCKED", "live:worker:sa", f"worker runs as '{sa or '(none)'}', expected exactly '{args.expected_worker_sa}'")
        else:
            emit("PASS", "live:worker:sa", f"worker runtime identity is exactly {sa}")
        check_exact_plain("worker", plain, "ENVIRONMENT", args.expected_environment)
        check_exact_plain("worker", plain, "MILO_WORKER_AUDIENCE", args.expected_api_url)
        check_exact_set("worker", plain, "MILO_APPROVED_WORKER_IDENTITIES", args.expected_worker_sa)
        if args.expected_redis_url:
            check_exact_plain("worker", plain, "UPSTASH_REDIS_REST_URL", args.expected_redis_url)
        for flag in EXECUTION_FLAGS:
            check_flag_false("worker", plain, secret_ref, flag)
        for budget in BUDGETS:
            check_budget("worker", plain, secret_ref, budget)
        check_secret_ref("worker", secret_ref, "SUPABASE_URL", args.supabase_url_secret)
        check_secret_ref("worker", secret_ref, "SUPABASE_SECRET_KEY", args.supabase_key_secret)
        check_secret_ref("worker", secret_ref, "KIMI_API_KEY", args.provider_secret)
        check_secret_ref("worker", secret_ref, "UPSTASH_REDIS_REST_TOKEN", args.redis_secret, args.redis_secret_version)

    if svc is not None and job is not None:
        api_sa = _service_account(svc)
        worker_sa = _service_account(job)
        if api_sa and worker_sa and api_sa == worker_sa:
            emit("BLOCKED", "live:shared-identity", "live API service and worker job share a runtime identity; they must be distinct")
        elif api_sa and worker_sa:
            emit("PASS", "live:distinct-identity", "live API and worker run as distinct service accounts")

    for line in _FINDINGS:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
