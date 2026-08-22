"""Stage C production DB probe (runs inside `stagec-db-probe` Cloud Run job).

Runs AS the API runtime service account with SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY bound from Secret Manager (that identity's
existing access — no new grants). Talks to PostgREST/auth-admin with the
Python standard library only, prints structured JSON evidence and NEVER
prints a secret value.

Modes (env STAGE_C_MODE):
  preflight — verify migration-dependent schema/RPC surface AND enforce
              (via a real server-side exact count) that the runs table
              holds exactly STAGE_C_EXPECTED_PRIOR_RUNS rows (the pinned
              live baseline — Attempt 6's run row) and zero rows for the
              current Stage C idempotency key; fails closed on any other
              value, on an unavailable count, and on missing/invalid
              baseline configuration
  setup     — idempotently create the operator test user, dedicated
              stage-c-smoke project, membership and conversation
  evidence  — executable acceptance gate for env STAGE_C_RUN_ID: exits
              non-zero unless EVERY acceptance criterion holds, including
              exactly one new authorized run over the pinned prior
              baseline (total = STAGE_C_EXPECTED_PRIOR_RUNS + 1) carrying
              the current idempotency key — historical runs never satisfy
              the new run's acceptance and are never deleted or hidden

Exit codes: 0 = PASS, 1 = a check failed (fail closed).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

BASE = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TEST_EMAIL = os.environ.get("STAGE_C_TEST_EMAIL", "stage-c-smoke@invalid.milo")
PROJECT_SLUG = "stage-c-smoke"

KILL_SWITCH_ACTION = (
    "Treat the smoke run as FAILED. RUN THE KILL SWITCH NOW: "
    "scripts/release/stage-c/kill-switch.sh — then restore posture with "
    "07-post-smoke-posture.sh and record the failure in STAGE_C_ACCEPTANCE.md."
)


def call(method: str, path: str, body: dict | list | None = None, headers: dict | None = None):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "apikey": KEY,
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")[:300]}


def parse_content_range_total(header: str | None) -> int | None:
    """Total row count from a PostgREST Content-Range header, else None.

    Accepts "0-0/17" and "*/0" forms. "0-0/*" (count not computed) returns
    None so callers fail closed instead of trusting a partial page length.
    """
    if not header or "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    if not total.isdigit():
        return None
    return int(total)


def count_exact(path_with_query: str) -> int | None:
    """Real server-side exact row count (Prefer: count=exact), else None."""
    request = urllib.request.Request(
        BASE + path_with_query,
        method="GET",
        headers={
            "apikey": KEY,
            "Authorization": f"Bearer {KEY}",
            "Prefer": "count=exact",
            "Range-Unit": "items",
            "Range": "0-0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return parse_content_range_total(response.headers.get("Content-Range"))
    except urllib.error.HTTPError as exc:
        # PostgREST answers 416 for an out-of-range page on some versions;
        # the Content-Range header still carries the exact total.
        if exc.code == 416:
            return parse_content_range_total(exc.headers.get("Content-Range"))
        return None


def fail(reason: str) -> None:
    print(json.dumps({"stage_c_probe": "BLOCKED", "reason": reason}))
    sys.exit(1)


def expected_prior_runs() -> int | None:
    """Pinned historical run baseline from the environment, else None.

    A missing, empty or non-integer STAGE_C_EXPECTED_PRIOR_RUNS returns
    None so callers fail closed — the gates must never fall back to
    assuming an empty system (Attempt 6's run row is real history).
    """
    raw = os.environ.get("STAGE_C_EXPECTED_PRIOR_RUNS")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


# Stage C RPC surface with the REQUIRED argument names of each function as
# defined by the release migrations (000400 for create_message_and_run_v2,
# 000600 for the guarded worker RPCs). Optional (defaulted) arguments are
# deliberately excluded so a migration adding an optional parameter does
# not fail the check, while a missing/renamed required argument does.
REQUIRED_RPC_ARGS: dict[str, set[str]] = {
    "create_message_and_run_v2": {
        "p_conversation_id", "p_content", "p_metadata",
        "p_requested_by", "p_idempotency_key", "p_request_fingerprint",
    },
    "transition_run_worker_guarded": {
        "p_run_id", "p_status", "p_expected_status",
        "p_worker_id", "p_attempt", "p_lease_token",
    },
    "heartbeat_run_guarded": {"p_run_id", "p_worker_id", "p_attempt", "p_lease_token"},
    "update_run_usage_guarded": {"p_run_id", "p_worker_id", "p_attempt", "p_lease_token", "p_usage"},
    "settle_model_call_budget_guarded": {
        "p_reservation_id", "p_actual_cost", "p_run_id",
        "p_worker_id", "p_attempt", "p_lease_token",
    },
}


def advertised_rpc_args(post_spec: dict) -> set[str] | None:
    """Argument names an RPC advertises in the PostgREST OpenAPI document.

    PostgREST (Swagger 2.0) lists an RPC's named arguments as the
    properties of its single in-body parameter schema. Returns None when
    that shape cannot be established — callers must fail closed on None,
    never assume.
    """
    parameters = post_spec.get("parameters")
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if isinstance(parameter, dict) and parameter.get("in") == "body":
            schema = parameter.get("schema")
            if not isinstance(schema, dict):
                return None
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                return None
            return set(properties)
    return None


def check_rpc_surface(checks: dict[str, str], problems: list[str]) -> None:
    """NON-MUTATING RPC existence/signature check via OpenAPI introspection.

    Never POSTs to an RPC: Stage C attempt 2 failed because the previous
    check POSTed `{}` and treated HTTP 404 as "function absent" — but
    PostgREST resolves functions by name AND argument keys, so an existing
    function with required parameters answers 404 (PGRST202) to an empty
    body. A mismatched-invocation 404 is NOT proof of absence, and probing
    by invoking mutating RPCs is unsafe by construction. Instead the
    service-role GET of the PostgREST root returns the OpenAPI document;
    each required RPC must be exposed as /rpc/<name> and advertise every
    required argument name. Any unavailable, malformed, unauthorized or
    ambiguous metadata fails closed.
    """
    status, spec = call("GET", "/rest/v1/")
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if status != 200 or not isinstance(paths, dict):
        for rpc in REQUIRED_RPC_ARGS:
            checks[f"rpc_{rpc}"] = "UNVERIFIED"
        problems.append(
            f"PostgREST OpenAPI introspection unavailable (HTTP {status}) — "
            "cannot establish the RPC surface; failing closed"
        )
        return
    for rpc, required_args in REQUIRED_RPC_ARGS.items():
        entry = paths.get(f"/rpc/{rpc}")
        post_spec = entry.get("post") if isinstance(entry, dict) else None
        if not isinstance(post_spec, dict):
            checks[f"rpc_{rpc}"] = "MISSING"
            problems.append(f"rpc_{rpc} is MISSING from the exposed RPC surface")
            continue
        advertised = advertised_rpc_args(post_spec)
        if advertised is None:
            checks[f"rpc_{rpc}"] = "UNVERIFIED"
            problems.append(
                f"rpc_{rpc}: argument metadata malformed/ambiguous — "
                "cannot verify the callable surface; failing closed"
            )
        elif not required_args <= advertised:
            missing = sorted(required_args - advertised)
            checks[f"rpc_{rpc}"] = "SIGNATURE_MISMATCH"
            problems.append(
                f"rpc_{rpc}: required argument(s) {missing} not advertised — "
                "the deployed signature differs from the release migrations"
            )
        else:
            checks[f"rpc_{rpc}"] = "present"


def preflight() -> None:
    checks: dict[str, str] = {}
    problems: list[str] = []
    # Migrations 012/000400/000300/000600: full RPC surface, verified
    # WITHOUT invoking anything (see check_rpc_surface).
    check_rpc_surface(checks, problems)
    # Migration 000600: attempt-aware reservation identity.
    status, _ = call("GET", "/rest/v1/model_call_budget_reservations?select=attempt&limit=1")
    checks["reservations_attempt_column"] = "present" if status == 200 else "MISSING"
    # Migration 012: lease tokens on runs.
    status, _ = call("GET", "/rest/v1/runs?select=lease_token,launch_state&limit=1")
    checks["runs_lease_columns"] = "present" if status == 200 else "MISSING"
    # Ledger table (013/000500).
    status, _ = call("GET", "/rest/v1/run_usage_ledger?select=id&limit=1")
    checks["run_usage_ledger"] = "present" if status == 200 else "MISSING"
    problems.extend(
        f"{k} is MISSING" for k, v in checks.items() if v == "MISSING" and not k.startswith("rpc_")
    )

    # Stage C precondition: the runs table must hold EXACTLY the pinned
    # live baseline (Attempt 7: the 1 prior Attempt 6 run row. Attempt 5
    # also occurred historically, but its row is absent from production
    # for an unverified reason and is NOT counted) — enforced with a real
    # server-side exact count, failing closed if the count OR the
    # baseline configuration is unavailable/invalid.
    expected_prior = expected_prior_runs()
    total_runs = count_exact("/rest/v1/runs?select=id")
    checks["existing_runs"] = "UNKNOWN" if total_runs is None else str(total_runs)
    checks["expected_prior_runs"] = "INVALID" if expected_prior is None else str(expected_prior)
    if expected_prior is None:
        problems.append("STAGE_C_EXPECTED_PRIOR_RUNS is missing or not a non-negative integer — failing closed")
    if total_runs is None:
        problems.append("exact run count unavailable (no Content-Range total) — failing closed")
    elif expected_prior is not None and total_runs != expected_prior:
        problems.append(
            f"runs table holds {total_runs} row(s), expected exactly the pinned prior baseline of {expected_prior} — "
            "an unexpected run may exist (or history was altered); refusing to create the authorized run"
        )

    # Zero pre-existing rows under the CURRENT (Attempt 7) idempotency key
    # — the key is fresh and unique, so any row under it means the
    # authorization was already consumed or the key was reused.
    idempotency_key = os.environ.get("STAGE_C_IDEMPOTENCY_KEY")
    if not idempotency_key:
        problems.append("STAGE_C_IDEMPOTENCY_KEY is missing — failing closed")
    else:
        stage_c_runs = count_exact(f"/rest/v1/runs?select=id&idempotency_key=eq.{idempotency_key}")
        checks["existing_stage_c_runs"] = "UNKNOWN" if stage_c_runs is None else str(stage_c_runs)
        if stage_c_runs is None:
            problems.append("exact Stage C run count unavailable — failing closed")
        elif stage_c_runs != 0:
            problems.append(f"{stage_c_runs} pre-existing run(s) with idempotency key {idempotency_key!r}")

    print(json.dumps({"stage_c_probe": "preflight", "checks": checks, "problems": problems, "ok": not problems}))
    if problems:
        sys.exit(1)


def setup() -> None:
    # 1. Operator-controlled test user (admin API; no password flow used).
    status, body = call("POST", "/auth/v1/admin/users", {"email": TEST_EMAIL, "email_confirm": True})
    if status in (200, 201):
        user_id = body["id"]
    else:
        status, body = call("GET", "/auth/v1/admin/users?per_page=1000")
        users = body.get("users", body) if isinstance(body, dict) else body
        matches = [u for u in users if u.get("email") == TEST_EMAIL]
        if not matches:
            fail(f"test user creation failed (HTTP {status}) and no existing user found")
        user_id = matches[0]["id"]
    # 2. Dedicated project (only the test user will be a member).
    status, body = call(
        "POST",
        "/rest/v1/projects",
        {
            "slug": PROJECT_SLUG,
            "name": "Stage C smoke",
            "description": "Operator-controlled Stage C smoke-test project",
            "workflow_key": "vehicle_catalog_v1",
            "configuration": {"stage": "stage-c"},
        },
        headers={"Prefer": "return=representation"},
    )
    if status == 201:
        project_id = body[0]["id"]
    else:
        status, rows = call("GET", f"/rest/v1/projects?slug=eq.{PROJECT_SLUG}&select=id")
        if status != 200 or not rows:
            fail(f"project create/lookup failed (HTTP {status})")
        project_id = rows[0]["id"]
    # 3. Membership (idempotent upsert).
    status, _ = call(
        "POST",
        "/rest/v1/project_members",
        {"project_id": project_id, "user_id": user_id, "role": "owner"},
        headers={"Prefer": "resolution=merge-duplicates"},
    )
    if status not in (200, 201):
        fail(f"membership upsert failed (HTTP {status})")
    # 4. Conversation (reuse if present).
    status, rows = call("GET", f"/rest/v1/conversations?project_id=eq.{project_id}&select=id&limit=1")
    if status == 200 and rows:
        conversation_id = rows[0]["id"]
    else:
        status, body = call(
            "POST",
            "/rest/v1/conversations",
            {"project_id": project_id, "title": "stage-c-smoke"},
            headers={"Prefer": "return=representation"},
        )
        if status != 201:
            fail(f"conversation creation failed (HTTP {status})")
        conversation_id = body[0]["id"]
    print(json.dumps({
        "stage_c_probe": "setup",
        "user_id": user_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "members": "test user only",
    }))


SECRET_MARKERS = ("sk-", "KIMI_API_KEY", "MOONSHOT_API_KEY", "service_role", "sb_secret")

# Cap values the evidence gate verifies against; provided via STAGE_C_CAPS
# (the exact string from stage-c-env.sh) so there is a single source of
# expected values. Missing keys fail the gate closed.
REQUIRED_CAP_KEYS = (
    "MILO_MAX_MODEL_CALLS_PER_RUN",
    "MILO_MAX_INPUT_TOKENS_PER_RUN",
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN",
    "MILO_MAX_COST_PER_RUN",
)

# Per-call rounding tolerance for the ledger↔reservation reconciliation.
# The ledger stores actual_cost as numeric(12,6) (migration 013), so each
# settled ledger row is the 6-decimal rounding of the UNROUNDED per-call
# cost the worker settled into its reservation row (plain numeric,
# migration 015). Half an ulp of that rounding is the largest legitimate
# per-row difference; anything larger is tampering or corruption. There is
# deliberately NO fixed aggregate tolerance: signed per-row rounding drift
# accumulates with the call count (production Attempt 7: 84 calls,
# per-row drift ≤ 0.0000005, aggregate signed drift 0.0000088), so an
# aggregate bound is either too loose for small runs or false-fails large
# ones — the reconciliation is one-to-one by call_seq instead.
PER_CALL_ROUNDING_TOLERANCE = Decimal("0.0000005")
# run.usage.actual_cost is the worker's float running total rounded ONCE
# to 6 decimals; compare it against the UNROUNDED reservation total
# quantized to the same 6 decimals, allowing one unit in the last place
# for the float-accumulation/rounding-mode boundary.
USAGE_TOTAL_TOLERANCE = Decimal("0.000001")


def to_decimal(value: object) -> Decimal | None:
    """Exact Decimal for a PostgREST-serialized numeric/float, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def fetch_rows(path: str, label: str, failures: list[str]) -> list[dict] | None:
    """GET a PostgREST collection, failing closed on anything but rows.

    A PostgREST error (missing column, bad filter, RLS denial, …) answers
    with a non-200 status and a JSON error OBJECT. Treating that dict as a
    row collection silently corrupts every downstream count and scan, so a
    non-200 status, a non-list body or a non-dict element records a
    fail-closed failure and returns None — never the error body.
    """
    status, body = call("GET", path)
    if status != 200 or not isinstance(body, list) or not all(isinstance(row, dict) for row in body):
        detail = ""
        if isinstance(body, dict):
            error_fields = {k: body[k] for k in ("code", "message", "hint") if k in body}
            if error_fields:
                detail = f" PostgREST error: {json.dumps(error_fields, default=str)[:300]}"
        failures.append(
            f"fail-closed: {label} query did not return a row list (HTTP {status}).{detail}"
        )
        return None
    return body


def reconcile_costs(reservations: list[dict], ledger: list[dict], usage_cost: Decimal | None, failures: list[str]) -> dict:
    """Decimal-based, call_seq-aware one-to-one cost reconciliation.

    Matches every settled/overage reservation row to exactly one settled
    ledger row by call_seq and verifies each pair within the 6-decimal
    per-row rounding tolerance. Rejects duplicate, missing and unmatched
    call_seq values on either side and any per-row difference beyond the
    rounding half-ulp. run.usage.actual_cost is compared against the
    UNROUNDED reservation total (the same running total the worker
    snapshots), never against the per-row-rounded ledger sum. Appends
    failures in place; returns the Decimal sums for evidence output and
    for the strict cap checks.
    """
    reservation_costs: dict[int, Decimal] = {}
    for row in reservations:
        if row.get("status") not in ("settled", "overage"):
            continue
        seq = row.get("call_seq")
        if not isinstance(seq, int):
            failures.append(f"reservation row has invalid call_seq {seq!r} — cannot reconcile; failing closed")
            continue
        if seq in reservation_costs:
            failures.append(f"duplicate reservation for call_seq {seq} — reconciliation is ambiguous")
            continue
        cost = to_decimal(row.get("actual_cost"))
        if cost is None:
            failures.append(f"settled reservation call_seq {seq} has no numeric actual_cost — failing closed")
            continue
        reservation_costs[seq] = cost

    ledger_costs: dict[int, Decimal] = {}
    for row in ledger:
        if row.get("decision") != "settled":
            continue
        seq = row.get("call_seq")
        if not isinstance(seq, int):
            failures.append(f"settled ledger row has invalid call_seq {seq!r} — cannot reconcile; failing closed")
            continue
        if seq in ledger_costs:
            failures.append(f"duplicate settled ledger row for call_seq {seq} — reconciliation is ambiguous")
            continue
        # A zero-cost call may legitimately ledger actual_cost as NULL;
        # normalizing to 0 keeps it comparable while any nonzero
        # reservation cost still exceeds the tolerance and fails.
        ledger_costs[seq] = to_decimal(row.get("actual_cost")) or Decimal(0)

    missing_in_ledger = sorted(set(reservation_costs) - set(ledger_costs))
    if missing_in_ledger:
        failures.append(f"settled reservation call_seq(s) missing a settled ledger row: {missing_in_ledger[:20]}")
    missing_in_reservations = sorted(set(ledger_costs) - set(reservation_costs))
    if missing_in_reservations:
        failures.append(f"settled ledger call_seq(s) without a settled reservation: {missing_in_reservations[:20]}")

    for seq in sorted(set(reservation_costs) & set(ledger_costs)):
        drift = ledger_costs[seq] - reservation_costs[seq]
        if abs(drift) > PER_CALL_ROUNDING_TOLERANCE:
            failures.append(
                f"call_seq {seq}: ledger actual_cost {ledger_costs[seq]} differs from reservation "
                f"actual_cost {reservation_costs[seq]} by {drift} — beyond the 6-decimal per-row "
                f"rounding tolerance {PER_CALL_ROUNDING_TOLERANCE}"
            )

    reservation_total = sum(reservation_costs.values(), Decimal(0))
    ledger_total = sum(ledger_costs.values(), Decimal(0))
    if usage_cost is None:
        failures.append("run.usage actual_cost is missing or not numeric — failing closed")
    else:
        expected_usage = reservation_total.quantize(Decimal("0.000001"))
        if abs(usage_cost - expected_usage) > USAGE_TOTAL_TOLERANCE:
            failures.append(
                f"run.usage actual_cost {usage_cost} != unrounded reservation total {reservation_total} "
                f"(rounded {expected_usage}, tolerance {USAGE_TOTAL_TOLERANCE})"
            )
    return {
        "matched_calls": len(set(reservation_costs) & set(ledger_costs)),
        "reservation_total_unrounded": reservation_total,
        "ledger_total": ledger_total,
    }


def expected_caps() -> dict[str, str]:
    caps: dict[str, str] = {}
    for pair in os.environ.get("STAGE_C_CAPS", "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            caps[key.strip()] = value.strip()
    return caps


def evidence() -> None:
    """Executable acceptance gate — exits non-zero if ANY criterion fails.

    Verifies exactly one new authorized run over the pinned prior baseline
    (total = STAGE_C_EXPECTED_PRIOR_RUNS + 1) and that the ONE requested
    run carries the current idempotency key — the historical Attempt 6
    row is counted only as baseline and can never satisfy the new run's
    acceptance. Two criteria live in 06-collect-evidence.sh because they
    need gcloud or the gateway identity: the worker execution total
    (pinned baseline + 1, all terminal) and the post-completion idempotent
    replay; the shell gate enforces both.
    """
    run_id = os.environ["STAGE_C_RUN_ID"]
    expected_terminal = {
        s.strip()
        for s in os.environ.get("STAGE_C_EXPECTED_TERMINAL_STATES", "completed").split(",")
        if s.strip()
    }
    expected_idempotency = os.environ.get("STAGE_C_IDEMPOTENCY_KEY")
    caps = expected_caps()
    failures: list[str] = []
    out: dict = {"stage_c_probe": "evidence", "run_id": run_id}

    missing_caps = [k for k in REQUIRED_CAP_KEYS if k not in caps]
    if missing_caps:
        failures.append(f"fail-closed: expected cap values missing from STAGE_C_CAPS: {missing_caps}")

    # Run row (lease token excluded from output).
    rows = fetch_rows(
        f"/rest/v1/runs?id=eq.{run_id}"
        "&select=id,status,attempt,worker_id,launch_state,started_at,finished_at,"
        "last_heartbeat_at,lease_expires_at,usage,error,requested_by,idempotency_key",
        "runs",
        failures,
    )
    if not rows:
        failures.append("run row not found")
        print(json.dumps({"stage_c_probe": "evidence", "ok": False, "failures": failures, "operator_action": KILL_SWITCH_ACTION}))
        sys.exit(1)
    run = rows[0]
    out["run"] = run

    # Exactly one new authorized run over the pinned prior baseline — real
    # exact counts, fail closed if unknown or if the baseline configuration
    # is missing/invalid. The gates never delete or hide historical rows
    # to make counts easier; the increment itself is the proof.
    prior_baseline = expected_prior_runs()
    out["expected_prior_runs"] = prior_baseline
    if prior_baseline is None:
        failures.append("STAGE_C_EXPECTED_PRIOR_RUNS is missing or not a non-negative integer — failing closed")
    total_runs = count_exact("/rest/v1/runs?select=id")
    out["total_runs"] = total_runs
    if total_runs is None:
        failures.append("exact run count unavailable — failing closed")
    elif prior_baseline is not None and total_runs != prior_baseline + 1:
        failures.append(
            f"total_runs={total_runs}, expected exactly {prior_baseline + 1} "
            f"(the pinned prior baseline of {prior_baseline} plus exactly one new authorized run)"
        )
    if not expected_idempotency:
        failures.append("STAGE_C_IDEMPOTENCY_KEY is missing — cannot attribute the new run; failing closed")
    else:
        if run.get("idempotency_key") != expected_idempotency:
            failures.append(
                f"run idempotency_key {run.get('idempotency_key')!r} != authorized key {expected_idempotency!r} — "
                "a historical run cannot satisfy the new run's acceptance"
            )
        # Exactly ONE run may carry the fresh Attempt 7 key: zero means the
        # new run is unaccounted for, more than one means a duplicate
        # slipped past idempotency.
        key_runs = count_exact(f"/rest/v1/runs?select=id&idempotency_key=eq.{expected_idempotency}")
        out["runs_with_current_key"] = key_runs
        if key_runs is None:
            failures.append("exact current-key run count unavailable — failing closed")
        elif key_runs != 1:
            failures.append(f"{key_runs} run(s) carry the authorized idempotency key, expected exactly 1")

    # Expected terminal state per the acceptance policy.
    state = run.get("status")
    if state not in expected_terminal:
        failures.append(f"terminal state {state!r} not in acceptance policy {sorted(expected_terminal)}")

    # Attempt / claim / heartbeat invariants.
    if run.get("attempt") != 1:
        failures.append(f"attempt={run.get('attempt')}, expected 1 (single execution, no retry claim)")
    for field in ("worker_id", "started_at", "finished_at", "last_heartbeat_at"):
        if not run.get(field):
            failures.append(f"run.{field} is missing — claim/heartbeat lifecycle incomplete")
    if run.get("launch_state") != "launched":
        failures.append(f"launch_state={run.get('launch_state')!r}, expected 'launched'")

    # Lifecycle events + secret-leak scan (counts only; no values printed).
    events = fetch_rows(f"/rest/v1/run_events?run_id=eq.{run_id}&select=event_type,agent,phase,created_at,payload&order=id.asc", "run_events", failures)
    out["event_count"] = len(events or [])
    out["event_types"] = [e["event_type"] for e in (events or [])]
    if not events:
        failures.append("no lifecycle events recorded for the run")
    blob = json.dumps(events or [])
    hits = {m: blob.count(m) for m in SECRET_MARKERS if blob.count(m)}
    out["secret_marker_hits"] = hits
    if hits:
        failures.append(f"secret markers found in DB events: {sorted(hits)}")

    # Checkpoints (evidence only).
    checkpoints = fetch_rows(f"/rest/v1/run_checkpoints?run_id=eq.{run_id}&select=phase,attempt,created_at&order=created_at.asc", "run_checkpoints", failures)
    out["checkpoints"] = [c["phase"] for c in (checkpoints or [])]

    # Heartbeats must exist and match the claiming worker/attempt.
    beats = fetch_rows(f"/rest/v1/worker_heartbeats?run_id=eq.{run_id}&select=worker_id,attempt,heartbeat_at&order=heartbeat_at.desc", "worker_heartbeats", failures)
    beats = beats or []
    out["heartbeat_count"] = len(beats)
    out["latest_heartbeat"] = beats[0] if beats else {}
    if not beats:
        failures.append("zero worker heartbeats recorded")
    else:
        if beats[0].get("worker_id") != run.get("worker_id"):
            failures.append("latest heartbeat worker_id does not match the run's claiming worker")
        if beats[0].get("attempt") != run.get("attempt"):
            failures.append("latest heartbeat attempt does not match the run attempt")

    # Budget reservations: all settled, zero dangling.
    reservations = fetch_rows(f"/rest/v1/model_call_budget_reservations?run_id=eq.{run_id}&select=call_seq,attempt,status,estimated_cost,actual_cost", "model_call_budget_reservations", failures)
    reservations = reservations or []
    by_status: dict[str, int] = {}
    for r in reservations:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    out["reservations_total"] = len(reservations)
    out["reservations_by_status"] = by_status
    out["dangling_reservations"] = by_status.get("reserved", 0)
    if by_status.get("reserved", 0) != 0:
        failures.append(f"{by_status.get('reserved')} dangling reservation(s) still in 'reserved'")
    unexpected_statuses = sorted(set(by_status) - {"settled", "overage", "released"})
    if unexpected_statuses:
        failures.append(f"unexpected reservation status(es): {unexpected_statuses}")
    out["reserved_cost_sum"] = str(sum((to_decimal(r.get("estimated_cost")) or Decimal(0) for r in reservations), Decimal(0)))

    # Usage ledger decisions and token sums.
    ledger = fetch_rows(f"/rest/v1/run_usage_ledger?run_id=eq.{run_id}&select=decision,call_seq,actual_input_tokens,actual_output_tokens,actual_cost,estimated_cost", "run_usage_ledger", failures)
    ledger = ledger or []
    decisions: dict[str, int] = {}
    for row in ledger:
        decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
    out["ledger_decisions"] = decisions
    ledger_input = sum(int(r["actual_input_tokens"] or 0) for r in ledger)
    ledger_output = sum(int(r["actual_output_tokens"] or 0) for r in ledger)
    out["ledger_input_tokens"] = ledger_input
    out["ledger_output_tokens"] = ledger_output

    # Reservation / ledger / run-usage accounting consistency: Decimal,
    # call_seq-aware, one-to-one (see reconcile_costs).
    usage = run.get("usage")
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = None
    if not isinstance(usage, dict):
        failures.append("run.usage snapshot missing — cannot verify caps; failing closed")
        usage = {}
    usage_cost = to_decimal(usage.get("actual_cost"))
    reconciliation = reconcile_costs(reservations, ledger, usage_cost, failures)
    ledger_cost = reconciliation["ledger_total"]
    out["matched_calls"] = reconciliation["matched_calls"]
    out["settled_cost_sum"] = str(reconciliation["reservation_total_unrounded"])
    out["ledger_actual_cost_sum"] = str(ledger_cost)
    if int(usage.get("input_tokens") or 0) != ledger_input:
        failures.append(f"run.usage input_tokens {usage.get('input_tokens')} != ledger sum {ledger_input}")
    if int(usage.get("output_tokens") or 0) != ledger_output:
        failures.append(f"run.usage output_tokens {usage.get('output_tokens')} != ledger sum {ledger_output}")

    # Tracked cost / token / call caps vs the exact configured values.
    # The caps stay STRICT: no rounding tolerance ever loosens them. ALL
    # THREE cost views must be within the $ cap exactly — the per-row-
    # rounded ledger total, the run.usage snapshot AND the UNROUNDED
    # reservation total: six-decimal rounding may round a true spend of
    # e.g. $3.0000001 down to a ledger/usage total of exactly $3.000000,
    # and the unrounded view is what actually left the budget.
    if not missing_caps:
        max_cost = Decimal(caps["MILO_MAX_COST_PER_RUN"])
        max_calls = int(caps["MILO_MAX_MODEL_CALLS_PER_RUN"])
        max_input = int(caps["MILO_MAX_INPUT_TOKENS_PER_RUN"])
        max_output = int(caps["MILO_MAX_OUTPUT_TOKENS_PER_RUN"])
        max_total = int(caps["MILO_MAX_TOTAL_TOKENS_PER_RUN"])
        reservation_total = reconciliation["reservation_total_unrounded"]
        if ledger_cost > max_cost or (usage_cost or Decimal(0)) > max_cost or reservation_total > max_cost:
            failures.append(
                f"tracked cost (ledger {ledger_cost} / usage {usage_cost} / "
                f"unrounded reservations {reservation_total}) exceeds cap {max_cost}"
            )
        model_calls = int(usage.get("model_calls") or 0)
        if model_calls > max_calls:
            failures.append(f"model_calls {model_calls} exceeds cap {max_calls}")
        if len(reservations) > max_calls:
            failures.append(f"reservation count {len(reservations)} exceeds call cap {max_calls}")
        if ledger_input > max_input:
            failures.append(f"input tokens {ledger_input} exceed cap {max_input}")
        if ledger_output > max_output:
            failures.append(f"output tokens {ledger_output} exceed cap {max_output}")
        if ledger_input + ledger_output > max_total:
            failures.append(f"total tokens {ledger_input + ledger_output} exceed cap {max_total}")

    # Launch invocation audit: exactly one launcher invocation. The
    # run_invocations table has NO `invocation` column (migration 006:
    # launcher/execution_name/payload/created_at) — selecting one made
    # PostgREST answer HTTP 400 with an error dict, which the old code
    # counted as rows. Real columns only, and a failed query fails closed
    # above instead of masquerading as a count mismatch.
    invocations = fetch_rows(f"/rest/v1/run_invocations?run_id=eq.{run_id}&select=launcher,execution_name,created_at", "run_invocations", failures)
    if invocations is not None:
        out["invocations"] = len(invocations)
        out["invocation_launchers"] = sorted(str(i.get("launcher")) for i in invocations)
        if len(invocations) != 1:
            failures.append(f"run_invocations={len(invocations)}, expected exactly 1")

    out["failures"] = failures
    out["ok"] = not failures
    if failures:
        out["operator_action"] = KILL_SWITCH_ACTION
    print(json.dumps(out, default=str))
    if failures:
        sys.exit(1)


def main() -> None:
    mode = os.environ.get("STAGE_C_MODE", "preflight")
    if mode == "preflight":
        preflight()
    elif mode == "setup":
        setup()
    elif mode == "evidence":
        evidence()
    else:
        fail(f"unknown STAGE_C_MODE {mode!r}")


if __name__ == "__main__":
    main()
