"""Stage D production DB probe (runs inside `stage-d-db-probe` Cloud Run job).

Runs AS the API runtime service account with SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY bound from Secret Manager (that identity's
existing access — no new grants). Talks to PostgREST/auth-admin with the
Python standard library only, prints structured JSON evidence and NEVER
prints a secret value.

Modes (env STAGE_D_MODE):
  preflight — verify the migration-dependent schema/RPC surface, enforce
              (via real server-side exact counts) that public.runs holds
              exactly STAGE_D_EXPECTED_PRIOR_RUNS rows and zero rows for
              the Stage D idempotency key, and prove the prepared
              Government capture run is still untouched. Fails closed on
              any other value, on an unavailable count, and on
              missing/invalid baseline configuration
  govcheck  — the Government-capture invariant ALONE, read-only. Safe to
              run at any time; used by the post-run lockdown and by
              resolve-government-capture.sh's verification
  setup     — two phases. PHASE A is read-only: validate any
              pre-existing stage-d-smoke project (forbidden ids,
              workflow key, exact configuration, membership set, zero
              active user/project runs). A rejected project is never
              written to, and the verdict reports an empty mutation
              list. PHASE B mutates, then re-reads and proves the exact
              final membership and zero active runs
  terminalize — drive the authorized Stage D run to a terminal state
              with guarded, identity-checked transitions along the
              supported lifecycle, release any dangling budget
              reservation through the supported RPC, and prove the run
              is terminal with zero active user/project runs and zero
              reservations left in the reserved status. Refuses ANY run
              candidate, recorded or recovered, unless the recorded
              user and conversation identity is supplied and matches
  evidence  — executable acceptance gate for env STAGE_D_RUN_ID: exits
              non-zero unless EVERY acceptance criterion holds, including
              exactly one new authorized run over the pinned prior
              baseline (total = STAGE_D_EXPECTED_PRIOR_RUNS + 1) carrying
              the Stage D idempotency key, and the Government-capture
              invariant still intact. Historical runs never satisfy the
              new run's acceptance and are never deleted or hidden. The
              canonical ProductOutcome the worker recorded is copied into
              the verdict as `product_outcome` for the host-side semantic
              gate; this probe never derives or re-judges one

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
TEST_EMAIL = os.environ.get("STAGE_D_TEST_EMAIL", "stage-d-smoke@invalid.milo")
PROJECT_SLUG = os.environ.get("STAGE_D_PROJECT_SLUG", "stage-d-smoke")
WORKFLOW_KEY = os.environ.get("STAGE_D_WORKFLOW_KEY", "vehicle_catalog_v1")

KILL_SWITCH_ACTION = (
    "Treat the Stage D run as FAILED. RUN THE KILL SWITCH NOW: "
    "scripts/release/stage-d/kill-switch.sh — then restore posture with "
    "07-post-run-lockdown.sh and record the failure in "
    "docs/production-readiness/STAGE_D_AUTHORIZATION.md."
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
    print(json.dumps({"stage_d_probe": "BLOCKED", "ok": False, "reason": reason}))
    sys.exit(1)


def expected_prior_runs() -> int | None:
    """Pinned historical run baseline from the environment, else None.

    A missing, empty or non-integer STAGE_D_EXPECTED_PRIOR_RUNS returns
    None so callers fail closed — the gates must never fall back to
    assuming an empty system. Production holds seven real rows.
    """
    raw = os.environ.get("STAGE_D_EXPECTED_PRIOR_RUNS")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


# ---------------------------------------------------------------------------
# The prepared Government capture invariant
# ---------------------------------------------------------------------------

#: Tables that would carry a trace if ANY worker had ever touched the
#: prepared capture run. All of them must stay empty for it.
GOV_TRACE_TABLES = (
    "run_events",
    "run_usage_ledger",
    "model_call_budget_reservations",
    "worker_heartbeats",
    "run_invocations",
    "run_checkpoints",
    "run_blackboards",
)

#: The two postures the prepared capture run may legitimately be in.
#:
#: PREPARED — exactly as `operator_capture.py --prepare` left it: queued,
#:   resting in the launcher-unacquirable launch_state 'none', never
#:   claimed. This is the state production is in today.
#: RETIRED — terminally cancelled by resolve-government-capture.sh's
#:   guarded compare-and-set. `cancelled` is outside claim_run_lease's
#:   acquirable set, so a retired run can never be claimed by anything.
#:
#: Anything else — running, launched, a worker_id, a lease, a started_at —
#: means something claimed or launched the capture, which is exactly the
#: event Stage D exists to make impossible. It fails every gate closed.
GOV_PREPARED = "prepared"
GOV_RETIRED = "retired"


def government_capture_invariant(problems: list[str]) -> dict:
    """Read-only proof that the prepared capture run is still untouched.

    Never writes, never claims, never launches and never cancels. Appends
    to `problems` in place and returns the structured evidence.
    """
    run_id = os.environ.get("STAGE_D_GOV_CAPTURE_RUN_ID")
    expected_key = os.environ.get("STAGE_D_GOV_CAPTURE_KEY")
    out: dict = {"run_id": run_id, "expected_idempotency_key": expected_key}
    if not run_id or not expected_key:
        problems.append(
            "STAGE_D_GOV_CAPTURE_RUN_ID / STAGE_D_GOV_CAPTURE_KEY are not both set — "
            "the Government-capture invariant cannot be proved; failing closed"
        )
        return out

    status, body = call(
        "GET",
        f"/rest/v1/runs?id=eq.{run_id}"
        "&select=id,status,launch_state,worker_id,attempt,started_at,finished_at,"
        "last_heartbeat_at,lease_expires_at,idempotency_key,cancellation_reason",
    )
    if status != 200 or not isinstance(body, list):
        problems.append(
            f"Government capture run query did not return a row list (HTTP {status}) — failing closed"
        )
        return out
    if not body:
        # The row is GONE. Stage D never deletes a run row, so its absence
        # is unexplained drift, not a resolution.
        problems.append(
            f"Government capture run {run_id} is ABSENT from public.runs — Stage D deletes no run row, "
            "so this is unexplained drift; failing closed"
        )
        out["present"] = False
        return out
    row = body[0]
    out["present"] = True
    out["row"] = row

    if row.get("idempotency_key") != expected_key:
        problems.append(
            f"Government capture run idempotency_key {row.get('idempotency_key')!r} != expected "
            f"{expected_key!r} — this is not the prepared capture row; failing closed"
        )

    # Never claimed, under either allowed posture.
    for field in ("worker_id", "started_at", "last_heartbeat_at", "lease_expires_at"):
        if row.get(field):
            problems.append(
                f"Government capture run has {field}={row.get(field)!r} — it was CLAIMED by a worker. "
                "This is the exact event Stage D forbids; failing closed"
            )
    if row.get("attempt") not in (1, None):
        problems.append(
            f"Government capture run attempt={row.get('attempt')!r}, expected 1 — a claim incremented it; failing closed"
        )

    state = str(row.get("status"))
    launch_state = str(row.get("launch_state"))
    if state == "queued" and launch_state == "none":
        out["posture"] = GOV_PREPARED
    elif state == "cancelled" and launch_state == "none":
        out["posture"] = GOV_RETIRED
    else:
        out["posture"] = "UNEXPECTED"
        problems.append(
            f"Government capture run is status={state!r} launch_state={launch_state!r}; the only allowed "
            f"postures are prepared (queued/none) or retired (cancelled/none) — failing closed"
        )

    # No worker trace of any kind. A single row in any of these tables
    # would mean the capture actually ran.
    traces: dict[str, int | None] = {}
    for table in GOV_TRACE_TABLES:
        count = count_exact(f"/rest/v1/{table}?select=run_id&run_id=eq.{run_id}")
        traces[table] = count
        if count is None:
            problems.append(f"exact {table} count for the Government capture run unavailable — failing closed")
        elif count != 0:
            problems.append(
                f"Government capture run has {count} {table} row(s) — it was EXECUTED; failing closed"
            )
    out["traces"] = traces
    return out


def assert_stage_d_key_is_not_the_capture_key(problems: list[str]) -> None:
    """The Stage D run may never borrow the capture's identity."""
    stage_d_key = os.environ.get("STAGE_D_IDEMPOTENCY_KEY")
    capture_key = os.environ.get("STAGE_D_GOV_CAPTURE_KEY")
    if stage_d_key and capture_key and stage_d_key == capture_key:
        problems.append(
            "STAGE_D_IDEMPOTENCY_KEY equals the Government capture key — the Stage D run would replay "
            "the prepared capture instead of creating a new run; failing closed"
        )


# ===========================================================================
# THE REQUIRED RPC SURFACE, rebuilt from CURRENT main.
# ===========================================================================
#
# This used to be a hand-written list of six RPCs, pinned when the guarded
# worker writes landed. Everything built afterwards -- the durable
# execution-usage ledger, atomic guarded finalization, the current-verdict
# authority, the R3/R4 evidence writers, the catalog writers, and the
# run-identity and fencing primitives -- became a RUNTIME DEPENDENCY without
# becoming a PREFLIGHT REQUIREMENT. A production database missing
# `record_run_usage_guarded` or `finalize_run_guarded` passed every Stage D
# check and would then have failed on the first paid model call, after the
# money was spent. That is exactly the failure a preflight exists to prevent.
#
# It is now GENERATED. `scripts/release/release_inventory.py` derives the whole
# inventory from two facts about the repository as it is: every RPC name the
# runtime actually calls (an AST scan of the repository layer, plus this
# probe's own `/rest/v1/rpc/` calls), and every function the migrations create,
# with the arguments each one requires. `tests/test_release_inventory.py` fails
# if the literal below is not exactly what that derivation produces from
# current main, so the list cannot fall behind the runtime again.
#
# It stays a LITERAL here, and only here, because this probe is transported
# into a bare pinned image as one SHA-256-pinned file with the standard library
# alone: it cannot import the deriving module. The arrangement is the same one
# `policy_envelope.PINNED_POLICY_FINGERPRINT` uses -- generated content,
# reviewed placement.
#
# Optional (defaulted) arguments are deliberately excluded, so a migration that
# ADDS an optional parameter does not fail the check while a missing or renamed
# required one does -- EXCEPT for the RPCs in EXACT_RPC_SIGNATURES below.
#
# Regenerate with:
#   python3 scripts/release/release_inventory.py rpcs
REQUIRED_RPC_ARGS: dict[str, set[str]] = {
    "activate_catalog_snapshot_guarded": {
        "p_activation", "p_attempt", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "append_run_event_guarded": {
        "p_attempt", "p_event_type", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "append_usage_ledger_guarded": {
        "p_attempt", "p_entry", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "bind_run_identity": {"p_identity", "p_run_id"},
    "catalog_candidate_manufacturers": {"p_snapshot_id"},
    "catalog_candidate_model_years": {"p_commercial_model", "p_manufacturer", "p_snapshot_id"},
    "catalog_candidate_models": {"p_manufacturer", "p_snapshot_id"},
    "catalog_candidate_variant_page": {"p_snapshot_id"},
    "catalog_raw_record_by_upstream_id": {"p_snapshot_id", "p_upstream_record_id"},
    "catalog_run_pending_promotions": {"p_run_id", "p_tool_operation"},
    "catalog_snapshot_candidate_diff": {"p_previous_snapshot_id", "p_snapshot_id"},
    "claim_current_verdict_states": {"p_run_id"},
    "claim_run_lease": {"p_run_id", "p_worker_id"},
    "create_agent_message_guarded": {
        "p_attempt", "p_lease_token", "p_message", "p_run_id", "p_worker_id"
    },
    "create_claim_with_source_guarded": {
        "p_attempt", "p_claim", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_conflict_guarded": {
        "p_attempt", "p_conflict", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_message_and_run_v2": {
        "p_content", "p_conversation_id", "p_idempotency_key", "p_metadata",
        "p_request_fingerprint", "p_requested_by"
    },
    "create_project_from_proposal_with_owner_v2": {
        "p_configuration", "p_description", "p_name", "p_owner", "p_proposal_id",
        "p_slug"
    },
    "create_supervisor_decision_guarded": {
        "p_attempt", "p_decision", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_tool_access_request_guarded": {
        "p_attempt", "p_lease_token", "p_request", "p_run_id", "p_worker_id"
    },
    "create_tool_grant_guarded": {
        "p_attempt", "p_grant", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_tool_usage_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_usage", "p_worker_id"
    },
    "finalize_run_guarded": {
        "p_attempt", "p_expected_status", "p_lease_token", "p_run_id", "p_status",
        "p_worker_id"
    },
    "heartbeat_run_guarded": {"p_attempt", "p_lease_token", "p_run_id", "p_worker_id"},
    "link_catalog_candidate_evidence_guarded": {
        "p_attempt", "p_lease_token", "p_link", "p_run_id", "p_worker_id"
    },
    "patch_run_blackboard_evidence_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_summary", "p_worker_id"
    },
    "promote_catalog_variant_guarded": {
        "p_attempt", "p_lease_token", "p_promotion", "p_run_id", "p_worker_id"
    },
    "record_catalog_candidate_guarded": {
        "p_attempt", "p_candidate", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "record_catalog_raw_record_guarded": {
        "p_attempt", "p_lease_token", "p_record", "p_run_id", "p_worker_id"
    },
    "record_catalog_snapshot_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_snapshot", "p_worker_id"
    },
    "record_claim_verdict_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_verdict", "p_worker_id"
    },
    "record_conflict_resolution_guarded": {
        "p_attempt", "p_lease_token", "p_resolution", "p_run_id", "p_worker_id"
    },
    "record_evidence_fragment_guarded": {
        "p_attempt", "p_fragment", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "record_run_usage_guarded": {
        "p_attempt", "p_lease_token", "p_ledger", "p_run_id", "p_worker_id"
    },
    "reserve_daily_project_budget": {"p_amount", "p_daily_limit", "p_project_id", "p_run_id"},
    "reserve_daily_user_budget": {"p_amount", "p_daily_limit", "p_run_id", "p_user_id"},
    "reserve_model_call_budget_guarded": {
        "p_attempt", "p_call_seq", "p_daily_project_limit", "p_daily_user_limit",
        "p_estimated_cost", "p_lease_token", "p_project_id", "p_run_id",
        "p_user_id", "p_worker_id"
    },
    "reserve_model_call_budget_v2": {
        "p_call_seq", "p_daily_project_limit", "p_daily_user_limit",
        "p_estimated_cost", "p_project_id", "p_run_id", "p_user_id"
    },
    "save_checkpoint_guarded": {
        "p_attempt", "p_engine_version", "p_lease_token", "p_phase", "p_run_id",
        "p_worker_id", "p_workflow_key"
    },
    "settle_model_call_budget": {"p_actual_cost", "p_reservation_id"},
    "settle_model_call_budget_guarded": {
        "p_actual_cost", "p_attempt", "p_lease_token", "p_reservation_id",
        "p_run_id", "p_worker_id"
    },
    "settle_model_call_budget_v2": {"p_actual_cost", "p_reservation_id"},
    "transition_run_worker_guarded": {
        "p_attempt", "p_expected_status", "p_lease_token", "p_run_id", "p_status",
        "p_worker_id"
    },
    "update_run_usage_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_usage", "p_worker_id"
    },
    "upsert_run_blackboard_guarded": {
        "p_attempt", "p_blackboard", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "upsert_source_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_source", "p_worker_id"
    },
}

# RPCs whose advertised argument set must EQUAL the pinned set: a missing
# argument AND an additional one both fail the preflight. The cleanup calls
# settle_model_call_budget with exactly these four keys, and PostgREST
# resolves a function by name AND argument keys. A deployed signature with
# a required extra argument would answer the cleanup's call with PGRST202,
# leaving the reservation held against the daily budget forever; one with
# a defaulted extra would route the call into a function body this
# authorization never reviewed. The OpenAPI document shows one signature
# per function name, so exact equality is the strongest read-only check
# available. Subset semantics remain for the other RPCs, which the Stage D
# toolkit never invokes itself.
EXACT_RPC_SIGNATURES: dict[str, set[str]] = {
    # Every parameter, defaulted ones included: the point of an exact check is
    # that an EXTRA deployed parameter is a different function. Generated from
    # the same inventory as REQUIRED_RPC_ARGS above.
    "settle_model_call_budget": {
        "p_actual_cost", "p_rejection_reason", "p_reservation_id", "p_status"
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

    Never POSTs to an RPC: PostgREST resolves functions by name AND
    argument keys, so an existing function with required parameters
    answers 404 (PGRST202) to an empty body. A mismatched-invocation 404
    is NOT proof of absence, and probing by invoking mutating RPCs is
    unsafe by construction. Instead the service-role GET of the PostgREST
    root returns the OpenAPI document; each required RPC must be exposed
    as /rpc/<name> and advertise every required argument name — and, for
    EXACT_RPC_SIGNATURES, no other argument at all. Any unavailable,
    malformed, unauthorized or ambiguous metadata fails closed.
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
        elif rpc in EXACT_RPC_SIGNATURES and advertised != EXACT_RPC_SIGNATURES[rpc]:
            exact = EXACT_RPC_SIGNATURES[rpc]
            missing = sorted(exact - advertised)
            unexpected = sorted(advertised - exact)
            checks[f"rpc_{rpc}"] = "SIGNATURE_MISMATCH"
            problems.append(
                f"rpc_{rpc}: the advertised argument set must equal {sorted(exact)} exactly — "
                f"missing {missing}, unexpected {unexpected}; the cleanup calls this function with "
                "exactly those arguments, so any other deployed signature could leave reservations held"
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


def govcheck() -> None:
    """The Government-capture invariant alone. Read-only; mutates nothing."""
    problems: list[str] = []
    evidence = government_capture_invariant(problems)
    assert_stage_d_key_is_not_the_capture_key(problems)
    print(json.dumps({
        "stage_d_probe": "govcheck",
        "government_capture": evidence,
        "problems": problems,
        "ok": not problems,
    }, default=str))
    if problems:
        sys.exit(1)


def preflight() -> None:
    checks: dict[str, str] = {}
    problems: list[str] = []
    # The FULL required RPC surface of current main -- every guarded worker
    # write, the execution-usage ledger, atomic finalization, the
    # current-verdict authority, the evidence and catalog writers, and the
    # run-identity/fencing primitives -- verified WITHOUT invoking anything
    # (see check_rpc_surface). A missing or mis-signed required RPC BLOCKS the
    # preflight, and the preflight runs before the authorized run is created,
    # so it blocks before any worker execution.
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
    # Console 2: the durable ExecutionUsageLedger row (20260920000100). Its
    # guarded writer is covered by the RPC surface above; this proves the table
    # the writer projects into actually exists.
    status, _ = call("GET", "/rest/v1/run_execution_usage?select=run_id&limit=1")
    checks["run_execution_usage"] = "present" if status == 200 else "MISSING"
    # Console 4: the current-verdict authority's durable rows (20260907000100
    # / 20260921000100).
    status, _ = call("GET", "/rest/v1/claim_verdicts?select=id&limit=1")
    checks["claim_verdicts"] = "present" if status == 200 else "MISSING"
    # This console: the immutable run identity (20260921000200). A column that
    # is absent means runs would be created with NO engine identity, which is
    # the state every consumer now refuses.
    status, _ = call("GET", "/rest/v1/runs?select=run_identity&limit=1")
    checks["runs_identity_column"] = "present" if status == 200 else "MISSING"
    problems.extend(
        f"{k} is MISSING" for k, v in checks.items() if v == "MISSING" and not k.startswith("rpc_")
    )

    # Stage D precondition: the runs table must hold EXACTLY the pinned
    # live baseline of 7 rows — enforced with a real server-side exact
    # count, failing closed if the count OR the baseline configuration is
    # unavailable/invalid. A count of 6 fails exactly like a count of 8.
    expected_prior = expected_prior_runs()
    total_runs = count_exact("/rest/v1/runs?select=id")
    checks["existing_runs"] = "UNKNOWN" if total_runs is None else str(total_runs)
    checks["expected_prior_runs"] = "INVALID" if expected_prior is None else str(expected_prior)
    if expected_prior is None:
        problems.append("STAGE_D_EXPECTED_PRIOR_RUNS is missing or not a non-negative integer — failing closed")
    if total_runs is None:
        problems.append("exact run count unavailable (no Content-Range total) — failing closed")
    elif expected_prior is not None and total_runs != expected_prior:
        problems.append(
            f"runs table holds {total_runs} row(s), expected exactly the pinned prior baseline of {expected_prior} — "
            "an unexpected run may exist (or history was altered); refusing to create the authorized run"
        )

    # Zero pre-existing rows under the Stage D idempotency key — the key
    # is fresh and unique, so any row under it means the authorization was
    # already consumed or the key was reused.
    idempotency_key = os.environ.get("STAGE_D_IDEMPOTENCY_KEY")
    if not idempotency_key:
        problems.append("STAGE_D_IDEMPOTENCY_KEY is missing — failing closed")
    else:
        stage_d_runs = count_exact(f"/rest/v1/runs?select=id&idempotency_key=eq.{idempotency_key}")
        checks["existing_stage_d_runs"] = "UNKNOWN" if stage_d_runs is None else str(stage_d_runs)
        if stage_d_runs is None:
            problems.append("exact Stage D run count unavailable — failing closed")
        elif stage_d_runs != 0:
            problems.append(f"{stage_d_runs} pre-existing run(s) with idempotency key {idempotency_key!r}")
    assert_stage_d_key_is_not_the_capture_key(problems)

    # The prepared Government capture must still be untouched BEFORE the
    # Stage D run is created — never claimed, never executed.
    government = government_capture_invariant(problems)

    print(json.dumps({
        "stage_d_probe": "preflight",
        "checks": checks,
        "government_capture": government,
        "problems": problems,
        "ok": not problems,
    }, default=str))
    if problems:
        sys.exit(1)


def forbidden_project_ids() -> set[str]:
    raw = os.environ.get("STAGE_D_FORBIDDEN_PROJECT_IDS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


#: The immutable configuration the Stage D project must carry. A reused
#: project whose configuration differs is NOT the project this
#: authorization describes, so it is refused rather than adopted.
EXPECTED_PROJECT_CONFIGURATION = {"stage": "stage-d"}

#: The role the sole member must hold. Comparing user ids alone would
#: accept the right person with the wrong (or a missing) role, and role is
#: what authorization actually reads.
EXPECTED_MEMBER_ROLE = "owner"


def membership_tuples(members: list) -> list[tuple[str, str]]:
    """(user_id, role) for every member, sorted — the exact comparison set."""
    return sorted(
        (str(m.get("user_id")), str(m.get("role")) if m.get("role") is not None else "<null>")
        for m in (members or []) if isinstance(m, dict))

#: Every run state that counts as ACTIVE for the concurrency caps
#: (mirrors backend/repository/supabase.py ACTIVE_RUN_STATES).
ACTIVE_RUN_STATES = ("queued", "launching", "starting", "running", "waiting", "cancellation_requested")


def active_state_filter() -> str:
    return "in.(" + ",".join(ACTIVE_RUN_STATES) + ")"


TERMINAL_RUN_STATES = ("completed", "partial_success", "failed", "cancelled",
                       "timed_out", "budget_exhausted")


def project_conversation_ids(project_id: str, problems: list[str]) -> list[str] | None:
    """Conversation ids belonging to a project, or None on a read failure."""
    rows = fetch_rows(
        f"/rest/v1/conversations?project_id=eq.{project_id}&select=id&limit=1000",
        "conversations", problems)
    if rows is None:
        return None
    return [str(row.get("id")) for row in rows if row.get("id")]


def count_active_runs(*, user_id: str | None = None, conversation_ids: list[str] | None = None):
    """Exact active-run count for a user or for a set of conversations."""
    active = active_state_filter()
    if user_id is not None:
        return count_exact(f"/rest/v1/runs?select=id&requested_by=eq.{user_id}&status={active}")
    if not conversation_ids:
        return 0
    joined = ",".join(conversation_ids)
    return count_exact(f"/rest/v1/runs?select=id&conversation_id=in.({joined})&status={active}")


def validate_existing_project(project: dict, user_id: str | None, problems: list[str]) -> dict:
    """READ-ONLY validation of a project Stage D is about to reuse.

    Called BEFORE any membership or conversation write. An earlier revision
    recorded these problems but went on to upsert membership and create a
    conversation anyway, leaving rows behind in a project it had just
    rejected. Validation and mutation are now separate phases.
    """
    evidence: dict = {}
    project_id = str(project.get("id"))
    evidence["project_id"] = project_id

    if project_id in forbidden_project_ids():
        problems.append(
            f"resolved project {project_id} is on the Stage D forbidden list — it is the Government "
            "capture's project or a prior smoke project; refusing to use it")

    evidence["workflow_key"] = project.get("workflow_key")
    if str(project.get("workflow_key")) != WORKFLOW_KEY:
        problems.append(
            f"project {project_id} has workflow_key {project.get('workflow_key')!r}, expected {WORKFLOW_KEY!r} — "
            "the Stage D caps are derived from Stage C Attempt 7 evidence for that pipeline")

    configuration = project.get("configuration")
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError:
            configuration = None
    evidence["configuration"] = configuration
    if configuration != EXPECTED_PROJECT_CONFIGURATION:
        problems.append(
            f"project {project_id} configuration is {configuration!r}, expected "
            f"{EXPECTED_PROJECT_CONFIGURATION!r} — refusing to adopt a project this authorization does not describe")

    # The membership set must already be exactly the dedicated test user
    # (or empty, for a project created by a previous run whose membership
    # upsert is still owed). Any OTHER member could create a run in this
    # project and break the one-run guarantee.
    members = fetch_rows(
        f"/rest/v1/project_members?project_id=eq.{project_id}&select=user_id,role",
        "project_members", problems)
    if members is None:
        members = []
        problems.append("membership set could not be read — failing closed")
    actual = membership_tuples(members)
    evidence["members_before"] = [f"{u}:{r}" for u, r in actual]
    allowed = [] if user_id is None else [(str(user_id), EXPECTED_MEMBER_ROLE)]
    if actual not in ([], allowed):
        expected_text = "empty" if user_id is None else f"exactly [{str(user_id)}:{EXPECTED_MEMBER_ROLE}]"
        problems.append(
            f"project {project_id} membership is {[f'{u}:{r}' for u, r in actual]}, expected empty or "
            f"{expected_text} — another member, or the right member with the wrong role, could create a run "
            "and break the one-run guarantee")

    # Zero active runs, for the user AND anywhere in the project.
    if user_id is not None:
        user_active = count_active_runs(user_id=user_id)
        evidence["active_runs_for_user"] = user_active
        if user_active is None:
            problems.append("exact active-run count for the Stage D test user unavailable — failing closed")
        elif user_active != 0:
            problems.append(
                f"the Stage D test user already has {user_active} active run(s); "
                "MILO_MAX_CONCURRENT_RUNS_PER_USER=1 would refuse the authorized run")

    conversation_ids = project_conversation_ids(project_id, problems)
    if conversation_ids is None:
        problems.append("the project's conversations could not be read — failing closed")
    else:
        evidence["project_conversations"] = len(conversation_ids)
        project_active = count_active_runs(conversation_ids=conversation_ids)
        evidence["active_runs_for_project"] = project_active
        if project_active is None:
            problems.append("exact active-run count for the Stage D project unavailable — failing closed")
        elif project_active != 0:
            problems.append(
                f"the Stage D project already has {project_active} active run(s); "
                "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1 would refuse the authorized run")
    return evidence


def setup() -> None:
    """Create/reuse the DEDICATED Stage D test identity in two phases.

    PHASE A is strictly read-only: look up the test user and the project,
    and — if the project already exists — validate every property this
    authorization depends on. If anything is wrong, NOTHING is created,
    written or upserted, and the verdict reports an empty mutation list so
    the absence of side effects is provable rather than assumed.

    PHASE B runs only once Phase A passed, and afterwards RE-READS the
    membership and the active-run counts to prove the authorized end state.
    """
    problems: list[str] = []
    mutations: list[str] = []
    evidence: dict = {"stage_d_probe": "setup"}

    # ---------------- PHASE A — read-only ----------------
    status, body = call("GET", "/auth/v1/admin/users?per_page=1000")
    if status != 200:
        fail(f"could not list users (HTTP {status}) — failing closed before any mutation")
    users = body.get("users", body) if isinstance(body, dict) else body
    matches = [u for u in (users or []) if isinstance(u, dict) and u.get("email") == TEST_EMAIL]
    user_id = matches[0]["id"] if matches else None
    evidence["user_existed"] = user_id is not None

    status, rows = call(
        "GET", f"/rest/v1/projects?slug=eq.{PROJECT_SLUG}&select=id,workflow_key,configuration")
    if status != 200:
        fail(f"could not look up the project (HTTP {status}) — failing closed before any mutation")
    existing = rows[0] if rows else None
    evidence["project_existed"] = existing is not None

    if existing is not None:
        evidence.update(validate_existing_project(existing, user_id, problems))

    if problems:
        # Refused during read-only validation: prove nothing was written.
        evidence.update({"mutations": mutations, "problems": problems, "ok": False})
        print(json.dumps(evidence, default=str))
        sys.exit(1)

    # ---------------- PHASE B — mutations ----------------
    if user_id is None:
        status, body = call("POST", "/auth/v1/admin/users",
                            {"email": TEST_EMAIL, "email_confirm": True})
        if status not in (200, 201):
            fail(f"test user creation failed (HTTP {status})")
        user_id = body["id"]
        mutations.append("created_test_user")
    evidence["user_id"] = user_id

    if existing is None:
        status, body = call(
            "POST", "/rest/v1/projects",
            {
                "slug": PROJECT_SLUG,
                "name": "Stage D smoke",
                "description": "Operator-controlled Stage D expansion-step project",
                "workflow_key": WORKFLOW_KEY,
                "configuration": EXPECTED_PROJECT_CONFIGURATION,
            },
            headers={"Prefer": "return=representation"})
        if status != 201 or not body:
            fail(f"project creation failed (HTTP {status})")
        project_id = body[0]["id"]
        mutations.append("created_project")
        # A freshly created project still must not be a forbidden id.
        if project_id in forbidden_project_ids():
            fail(f"created project {project_id} is on the Stage D forbidden list — refusing to proceed")
    else:
        project_id = str(existing["id"])
    evidence["project_id"] = project_id

    status, _ = call(
        "POST", "/rest/v1/project_members",
        {"project_id": project_id, "user_id": user_id, "role": "owner"},
        headers={"Prefer": "resolution=merge-duplicates"})
    if status not in (200, 201):
        fail(f"membership upsert failed (HTTP {status})")
    mutations.append("upserted_membership")

    status, rows = call("GET", f"/rest/v1/conversations?project_id=eq.{project_id}&select=id&limit=1")
    if status == 200 and rows:
        conversation_id = rows[0]["id"]
    else:
        status, body = call(
            "POST", "/rest/v1/conversations",
            {"project_id": project_id, "title": "stage-d-smoke"},
            headers={"Prefer": "return=representation"})
        if status != 201 or not body:
            fail(f"conversation creation failed (HTTP {status})")
        conversation_id = body[0]["id"]
        mutations.append("created_conversation")
    evidence["conversation_id"] = conversation_id
    evidence["workflow_key_expected"] = WORKFLOW_KEY

    # ---------------- PHASE C — prove the authorized end state ----------
    members = fetch_rows(
        f"/rest/v1/project_members?project_id=eq.{project_id}&select=user_id,role",
        "project_members", problems)
    actual = membership_tuples(members)
    evidence["members"] = [f"{u}:{r}" for u, r in actual]
    if actual != [(str(user_id), EXPECTED_MEMBER_ROLE)]:
        problems.append(
            f"after the authorized mutation the membership is {[f'{u}:{r}' for u, r in actual]}, expected "
            f"exactly [{str(user_id)}:{EXPECTED_MEMBER_ROLE}] — the project is not exclusively owned by the "
            "test user")

    user_active = count_active_runs(user_id=user_id)
    evidence["active_runs_for_user"] = user_active
    if user_active != 0:
        problems.append(f"the Stage D test user has {user_active} active run(s) after setup")

    conversation_ids = project_conversation_ids(project_id, problems)
    if conversation_ids is None:
        problems.append("the project's conversations could not be re-read — failing closed")
    else:
        project_active = count_active_runs(conversation_ids=conversation_ids)
        evidence["active_runs_for_project"] = project_active
        evidence["project_conversations"] = len(conversation_ids)
        if project_active != 0:
            problems.append(f"the Stage D project has {project_active} active run(s) after setup")

    evidence.update({"mutations": mutations, "problems": problems, "ok": not problems})
    print(json.dumps(evidence, default=str))
    if problems:
        sys.exit(1)


#: The run request's own marker. probe_gateway.py sends
#: metadata {"stage": "stage-d-smoke"}, which the API stores at
#: input.metadata. A recovered row that does not carry it is not the
#: Stage D run, whatever its idempotency key says.
STAGE_D_RUN_METADATA_STAGE = "stage-d-smoke"

#: Every column the identity gate and the proofs need.
RUN_SELECT = ("id,status,launch_state,worker_id,attempt,started_at,finished_at,"
              "idempotency_key,requested_by,conversation_id,input")


def run_metadata(run: dict) -> dict:
    payload = run.get("input")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    return metadata if isinstance(metadata, dict) else {}


#: Both must be supplied non-empty before terminalize may touch ANY run
#: candidate, recorded or recovered. The lockdown reads them from
#: state.json (user_id and conversation_id, written BEFORE the run was
#: created); an operator never types them in.
REQUIRED_RECORDED_IDENTITY = ("STAGE_D_EXPECTED_USER_ID", "STAGE_D_EXPECTED_CONVERSATION_ID")


def recorded_identity_present() -> dict[str, bool]:
    return {name: bool((os.environ.get(name) or "").strip()) for name in REQUIRED_RECORDED_IDENTITY}


def required_recorded_identity(name: str, run_id: str, problems: list[str]) -> str | None:
    """A recorded identity field, or None with a fail-closed problem recorded.

    Absence is a refusal, not a skipped check: without the recorded
    identity nothing ties the candidate row to the run this authorization
    created, and sharing the idempotency key and the metadata marker is not
    enough to act on.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        problems.append(
            f"{name} is missing or empty — the recorded identity of the Stage D run is unknown, so run "
            f"{run_id} cannot be tied to this authorization; refusing to terminalize it before any write")
        return None
    return value


def check_stage_d_run_identity(run: dict, expected_key: str, problems: list[str]) -> None:
    """Every identity property the authorized Stage D run must have.

    Applied to a run id read from state.json AND to one recovered by
    idempotency key, because a recovered row is exactly the case where
    nothing else has vouched for it. Both recorded identity fields
    (REQUIRED_RECORDED_IDENTITY) are mandatory for either: an absent field
    is a refusal, and terminalize() applies this gate before any PATCH or
    settlement RPC.
    """
    run_id = str(run.get("id"))
    capture_run_id = os.environ.get("STAGE_D_GOV_CAPTURE_RUN_ID")
    capture_operation = os.environ.get("STAGE_D_GOV_CAPTURE_OPERATION", "catalog.government.capture")

    if capture_run_id and run_id == capture_run_id:
        problems.append(f"run {run_id} is the prepared Government capture — refusing to touch it")
    if run.get("idempotency_key") != expected_key:
        problems.append(
            f"run {run_id} carries idempotency_key {run.get('idempotency_key')!r}, not the authorized Stage D key "
            f"{expected_key!r} — refusing to terminalize a run this authorization does not own")

    metadata = run_metadata(run)
    if metadata.get("milo_operation") == capture_operation:
        problems.append(f"run {run_id} carries the operator-capture marker — refusing to touch it")
    if str(metadata.get("stage")) != STAGE_D_RUN_METADATA_STAGE:
        problems.append(
            f"run {run_id} metadata stage is {metadata.get('stage')!r}, expected "
            f"{STAGE_D_RUN_METADATA_STAGE!r} — this is not the Stage D smoke run")

    # The recorded identity is REQUIRED, not merely honoured when present.
    # 05-execute-run.sh writes user_id and conversation_id to state.json
    # BEFORE the run is created, so the run this authorization created
    # always has both on record. A cleanup that cannot produce them has
    # lost the only evidence tying a row to that run; the candidate is
    # refused, recorded or recovered alike, before any write. Only the
    # zero-row case can pass without them, because there is nothing to
    # mutate.
    expected_user = required_recorded_identity("STAGE_D_EXPECTED_USER_ID", run_id, problems)
    if expected_user is not None and str(run.get("requested_by")) != expected_user:
        problems.append(
            f"run {run_id} was requested by {run.get('requested_by')!r}, not the recorded Stage D test user "
            f"{expected_user!r}")
    expected_conversation = required_recorded_identity("STAGE_D_EXPECTED_CONVERSATION_ID", run_id, problems)
    if expected_conversation is not None and str(run.get("conversation_id")) != expected_conversation:
        problems.append(
            f"run {run_id} belongs to conversation {run.get('conversation_id')!r}, not the recorded Stage D "
            f"conversation {expected_conversation!r}")


def terminalize() -> None:
    """Drive the authorized Stage D run to a terminal state and PROVE it.

    Cancelling a Cloud Run execution does NOT make the database run
    terminal: the Worker installs no SIGTERM handler, so an interrupted
    run can be left `running` forever, holding its lease, its concurrency
    slot and its budget reservations.

    THE LOST-RUN WINDOW. 05-execute-run.sh creates the run inside the
    gateway probe and only writes `run_id` to state.json after parsing the
    probe's structured output. If the shell, the session, the pipeline or
    log retrieval dies in between, the run EXISTS but the cleanup is handed
    an empty run id. Treating that as "no run" would delete the
    credentialed probe and walk away from an active run. So an absent
    recorded id is RECOVERED by querying the pinned idempotency key:

      * zero rows  — genuinely no run; prove it and return;
      * one row    — recover it and apply the SAME identity gate,
                     terminalization, reservation cleanup and proofs;
      * many rows  — ambiguous; fail closed having mutated nothing.

    Every path is identity-checked: the key, the run-request metadata
    marker, the recorded user and conversation — both REQUIRED; an absent
    field refuses the candidate before any write — and an explicit refusal
    of the prepared Government capture. Terminalization uses the repository's
    own supported lifecycle (active -> cancellation_requested ->
    cancelled), each step a guarded compare-and-set, plus the supported
    service-role `settle_model_call_budget` RPC to release any dangling
    reservation — including on a run that was ALREADY terminal, because a
    finished run can still hold reserved budget.

    It then PROVES: the run is terminal; zero active runs remain for its
    user and project; zero reservations remain in status 'reserved'.
    Nothing is deleted, and no other run is touched.
    """
    recorded_run_id = (os.environ.get("STAGE_D_RUN_ID") or "").strip()
    expected_key = os.environ.get("STAGE_D_IDEMPOTENCY_KEY")
    problems: list[str] = []
    actions: list[str] = []
    out: dict = {"stage_d_probe": "terminalize", "recorded_run_id": recorded_run_id or None,
                 "actions": actions}

    def emit_and_exit() -> None:
        out.update({"problems": problems, "ok": not problems})
        print(json.dumps(out, default=str))
        sys.exit(1 if problems else 0)

    if not expected_key:
        problems.append("STAGE_D_IDEMPOTENCY_KEY is missing — the run's identity cannot be checked; failing closed")
        emit_and_exit()

    # ---------------- resolve the run, recovering if necessary ----------
    if recorded_run_id:
        rows = fetch_rows(f"/rest/v1/runs?id=eq.{recorded_run_id}&select={RUN_SELECT}", "runs", problems)
        if rows is None:
            emit_and_exit()
        if not rows:
            problems.append(f"run {recorded_run_id} was not found — failing closed")
            emit_and_exit()
        run = rows[0]
        out["recovered"] = False
    else:
        # NEVER infer "no run" from an empty state field: ask the database.
        rows = fetch_rows(
            f"/rest/v1/runs?idempotency_key=eq.{expected_key}&select={RUN_SELECT}", "runs", problems)
        if rows is None:
            emit_and_exit()
        out["rows_under_key"] = len(rows)
        if len(rows) == 0:
            out.update({"no_run_recorded": True, "recovered": False, "terminal": True,
                        "reservations_still_reserved": 0})
            actions.append("no_run_under_key")
            emit_and_exit()
        if len(rows) > 1:
            problems.append(
                f"{len(rows)} runs carry the Stage D idempotency key {expected_key!r} — the authorized run is "
                "ambiguous; refusing to terminalize anything")
            emit_and_exit()
        run = rows[0]
        out["recovered"] = True
        out["recovered_run_id"] = str(run.get("id"))
        actions.append("recovered_run_id_from_idempotency_key")

    run_id = str(run.get("id"))
    out["run_id"] = run_id
    out["run_before"] = {k: v for k, v in run.items() if k != "input"}

    # ---------------- identity gate ----------------
    # Runs BEFORE the first PATCH and before the settlement RPC. A missing
    # recorded identity field is a refusal here, for a recorded run id and
    # a recovered row alike.
    out["recorded_identity_present"] = recorded_identity_present()
    check_stage_d_run_identity(run, expected_key, problems)
    if problems:
        emit_and_exit()

    # -- Guarded, identity-checked transitions along the supported path.
    #
    # Bounded re-read loop rather than a straight line: losing the CAS is
    # NOT itself a failure. The usual cause is the worker writing its own
    # terminal result concurrently, which is exactly the outcome wanted —
    # so a lost race is re-read and retried, and the POSTCONDITION PROOF
    # below is what decides. What the CAS guarantees is that this probe
    # never overwrites somebody else's result and never touches a run this
    # authorization does not own.
    observed = str(run.get("status"))
    for _attempt in range(MAX_TERMINALIZE_ATTEMPTS):
        if observed in TERMINAL_RUN_STATES:
            break
        target, extra = (
            ("cancelled", {"finished_at": "now()"})
            if observed == "cancellation_requested"
            else ("cancellation_requested",
                  {"cancellation_requested_at": "now()",
                   "cancellation_reason": "stage-d cleanup: run terminalized by the post-run lockdown"}))
        moved = guarded_transition(run_id, observed, target, expected_key, actions, extra=extra)
        if moved is not None:
            observed = moved
            continue
        actions.append(f"cas_lost_at_{observed}")
        rows = fetch_rows(f"/rest/v1/runs?id=eq.{run_id}&select={RUN_SELECT}", "runs", problems)
        if not rows:
            problems.append(f"run {run_id} disappeared while being terminalized — failing closed")
            break
        current = str(rows[0].get("status"))
        if current == observed:
            problems.append(
                f"the guarded transition {observed} -> {target} matched no row although the run is still "
                f"{observed!r} — it may not be the authorized run; failing closed")
            break
        observed = current
    out["status_after_transition"] = observed

    # -- Release any dangling reservation through the supported RPC. This
    # runs even when the run was ALREADY terminal: a finished run can still
    # hold reserved budget, and leaving it reserved keeps the daily budget
    # consumed forever.
    reserved = fetch_rows(
        f"/rest/v1/model_call_budget_reservations?run_id=eq.{run_id}&status=eq.reserved&select=id,call_seq",
        "model_call_budget_reservations", problems)
    released = 0
    for row in (reserved or []):
        reservation_id = row.get("id")
        if not reservation_id:
            problems.append("a reserved reservation has no id — cannot release it; failing closed")
            continue
        status, _body = call("POST", "/rest/v1/rpc/settle_model_call_budget", {
            "p_reservation_id": reservation_id,
            # A row still in 'reserved' was never settled with a real cost,
            # so 0 is the only defensible value; the reason records why.
            "p_actual_cost": 0,
            "p_status": "released",
            "p_rejection_reason": "stage-d cleanup: reservation released when the run was terminalized",
        })
        if status not in (200, 201, 204):
            problems.append(f"releasing reservation {reservation_id} failed (HTTP {status}) — failing closed")
        else:
            released += 1
    out["reservations_released"] = released
    if released:
        actions.append(f"released_{released}_dangling_reservations")

    # ---------------- PROVE the postconditions ----------------
    rows = fetch_rows(f"/rest/v1/runs?id=eq.{run_id}&select={RUN_SELECT}", "runs", problems)
    final = rows[0] if rows else {}
    out["run_after"] = {k: v for k, v in final.items() if k != "input"}
    final_status = str(final.get("status"))
    out["terminal"] = final_status in TERMINAL_RUN_STATES
    if final_status not in TERMINAL_RUN_STATES:
        problems.append(
            f"run {run_id} is still {final_status!r}, which is not terminal — the database run was NOT closed")

    user_id = final.get("requested_by")
    if user_id:
        user_active = count_active_runs(user_id=str(user_id))
        out["active_runs_for_user"] = user_active
        if user_active is None:
            problems.append("exact active-run count for the run's user unavailable — failing closed")
        elif user_active != 0:
            problems.append(f"{user_active} active run(s) remain for the run's user — failing closed")
    else:
        problems.append("the run has no requested_by — its user's active-run count cannot be proved; failing closed")

    conversation_id = final.get("conversation_id")
    project_id = None
    if conversation_id:
        conv_rows = fetch_rows(
            f"/rest/v1/conversations?id=eq.{conversation_id}&select=project_id", "conversations", problems)
        if conv_rows:
            project_id = conv_rows[0].get("project_id")
    if project_id:
        conversation_ids = project_conversation_ids(str(project_id), problems)
        if conversation_ids is None:
            problems.append("the run's project conversations could not be read — failing closed")
        else:
            project_active = count_active_runs(conversation_ids=conversation_ids)
            out["active_runs_for_project"] = project_active
            if project_active is None:
                problems.append("exact active-run count for the run's project unavailable — failing closed")
            elif project_active != 0:
                problems.append(f"{project_active} active run(s) remain in the run's project — failing closed")
    else:
        problems.append("the run's project could not be resolved — its active-run count cannot be proved; failing closed")

    still_reserved = count_exact(
        f"/rest/v1/model_call_budget_reservations?select=id&run_id=eq.{run_id}&status=eq.reserved")
    out["reservations_still_reserved"] = still_reserved
    if still_reserved is None:
        problems.append("exact dangling-reservation count unavailable — failing closed")
    elif still_reserved != 0:
        problems.append(f"{still_reserved} reservation(s) remain in status 'reserved' — the budget is still held")

    emit_and_exit()


#: Bounded: a benign race is retried, a pathological one is never looped on.
MAX_TERMINALIZE_ATTEMPTS = 5


def guarded_transition(run_id: str, observed: str, target: str, expected_key: str,
                       actions: list[str], extra: dict | None = None) -> str | None:
    """One guarded, identity-checked compare-and-set on a run's status.

    The PATCH matches on the run id, the OBSERVED status and the
    authorized idempotency key, so a concurrent transition (a worker
    writing its own terminal result) matches zero rows instead of being
    overwritten, and a run this authorization does not own is never
    touched.

    Returns the new status on success, or None when the CAS matched no
    row. None is a signal to re-read, not a verdict: the caller decides.
    """
    # PostgREST cannot evaluate now(); the sentinel becomes an explicit
    # UTC timestamp.
    payload = {"status": target}
    for key, value in (extra or {}).items():
        payload[key] = utc_now_iso() if value == "now()" else value
    status, body = call(
        "PATCH",
        f"/rest/v1/runs?id=eq.{run_id}&status=eq.{observed}&idempotency_key=eq.{expected_key}",
        payload,
        headers={"Prefer": "return=representation"},
    )
    if status not in (200, 204) or not isinstance(body, list) or len(body) != 1:
        return None
    actions.append(f"{observed}->{target}")
    return target


def utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


SECRET_MARKERS = ("sk-", "KIMI_API_KEY", "MOONSHOT_API_KEY", "service_role", "sb_secret")

# Cap values the evidence gate verifies against; provided via STAGE_D_CAPS
# (the exact string from stage-d-env.sh) so there is a single source of
# expected values. Missing keys fail the gate closed.
def expected_run_identity():
    """The identity dimensions this release binds, as Stage D pinned them.

    Produced by `policy_envelope.py run-identity` on the operator host, where
    the repository is available; this probe runs in a bare image with the
    standard library alone, so it receives the expectation rather than deriving
    it. `run_id` is deliberately absent from it -- that dimension is a property
    of the run, and it is checked separately against STAGE_D_RUN_ID.

    Returns None when the pin is absent or unparseable, which every caller
    treats as a refusal.
    """
    raw = (os.environ.get("STAGE_D_EXPECTED_RUN_IDENTITY") or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    # A pin that states no release binds nothing; refuse rather than pass.
    if not str(parsed.get("release_sha") or "").strip():
        return None
    return {key: str(value) for key, value in parsed.items() if key != "run_id"}


REQUIRED_CAP_KEYS = (
    "MILO_MAX_MODEL_CALLS_PER_RUN",
    "MILO_MAX_INPUT_TOKENS_PER_RUN",
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN",
    "MILO_MAX_COST_PER_RUN",
)

# Per-call rounding tolerance for the ledger<->reservation reconciliation.
# The ledger stores actual_cost as numeric(12,6) (migration 013), so each
# settled ledger row is the 6-decimal rounding of the UNROUNDED per-call
# cost the worker settled into its reservation row (plain numeric,
# migration 015). Half an ulp of that rounding is the largest legitimate
# per-row difference; anything larger is tampering or corruption. There is
# deliberately NO fixed aggregate tolerance: signed per-row rounding drift
# accumulates with the call count (Stage C Attempt 7: 84 calls, per-row
# drift <= 0.0000005, aggregate signed drift 0.0000088), so an aggregate
# bound is either too loose for small runs or false-fails large ones — the
# reconciliation is one-to-one by call_seq instead.
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

    A PostgREST error (missing column, bad filter, RLS denial, ...) answers
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
    for pair in os.environ.get("STAGE_D_CAPS", "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            caps[key.strip()] = value.strip()
    return caps


def evidence() -> None:
    """Executable acceptance gate — exits non-zero if ANY criterion fails.

    Verifies exactly one new authorized run over the pinned prior baseline
    (total = STAGE_D_EXPECTED_PRIOR_RUNS + 1) and that the ONE requested
    run carries the Stage D idempotency key — the six historical rows are
    counted only as baseline and can never satisfy the new run's
    acceptance. The Government-capture invariant is re-proved here too: a
    Stage D run that somehow claimed the prepared capture fails the gate.

    Two criteria live in 06-collect-evidence.sh because they need gcloud
    or the gateway identity: the worker execution total (pinned baseline
    + 1, all terminal) and the post-completion idempotent replay; the
    shell gate enforces both.
    """
    run_id = os.environ["STAGE_D_RUN_ID"]
    expected_terminal = {
        s.strip()
        for s in os.environ.get("STAGE_D_EXPECTED_TERMINAL_STATES", "completed").split(",")
        if s.strip()
    }
    expected_idempotency = os.environ.get("STAGE_D_IDEMPOTENCY_KEY")
    caps = expected_caps()
    failures: list[str] = []
    out: dict = {"stage_d_probe": "evidence", "run_id": run_id}

    missing_caps = [k for k in REQUIRED_CAP_KEYS if k not in caps]
    if missing_caps:
        failures.append(f"fail-closed: expected cap values missing from STAGE_D_CAPS: {missing_caps}")

    # The Stage D run must NEVER be the prepared Government capture run.
    capture_run_id = os.environ.get("STAGE_D_GOV_CAPTURE_RUN_ID")
    if capture_run_id and run_id == capture_run_id:
        failures.append(
            "STAGE_D_RUN_ID is the prepared Government capture run — Stage D never executes the capture; failing closed"
        )
    assert_stage_d_key_is_not_the_capture_key(failures)

    # Run row (lease token excluded from output).
    rows = fetch_rows(
        f"/rest/v1/runs?id=eq.{run_id}"
        "&select=id,status,attempt,worker_id,launch_state,started_at,finished_at,"
        "last_heartbeat_at,lease_expires_at,usage,error,requested_by,idempotency_key,"
        "run_identity",
        "runs",
        failures,
    )
    if not rows:
        failures.append("run row not found")
        print(json.dumps({"stage_d_probe": "evidence", "ok": False, "failures": failures, "operator_action": KILL_SWITCH_ACTION}))
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
        failures.append("STAGE_D_EXPECTED_PRIOR_RUNS is missing or not a non-negative integer — failing closed")
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
        failures.append("STAGE_D_IDEMPOTENCY_KEY is missing — cannot attribute the new run; failing closed")
    else:
        if run.get("idempotency_key") != expected_idempotency:
            failures.append(
                f"run idempotency_key {run.get('idempotency_key')!r} != authorized key {expected_idempotency!r} — "
                "a historical run cannot satisfy the new run's acceptance"
            )
        # Exactly ONE run may carry the fresh Stage D key: zero means the
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
    # Lease evidence: the run must have held a real, bounded lease.
    if not run.get("lease_expires_at"):
        failures.append("run.lease_expires_at is missing — no worker lease was ever established")

    # ===================================================================
    # RELEASE BINDING: the run this authorization actually executed must be
    # a run of the ACCEPTED RELEASE.
    # ===================================================================
    #
    # Every other link in the chain was already proven -- the accepted runtime
    # source is byte-identical to the policy this toolkit uses, that policy's
    # fingerprint is the reviewed one, the release tag resolves to the accepted
    # digests, the serving revision and the executing job run those digests --
    # and none of them said anything about the RUN. A run recorded no policy,
    # no release and no engine of its own: its engine was re-derived from a
    # project row at claim time.
    #
    # `runs.run_identity` (migration 20260921000200) is bound at creation and
    # immutable, and this compares it with what THIS release would have bound.
    # The identity is raw JSON from the database, so it is parsed defensively
    # and every failure mode is a refusal.
    identity = run.get("run_identity")
    if isinstance(identity, str):
        try:
            identity = json.loads(identity)
        except json.JSONDecodeError:
            identity = None
            failures.append("run.run_identity is not readable JSON — failing closed")
    out["run_identity"] = identity
    expected_identity = expected_run_identity()
    out["expected_run_identity"] = expected_identity
    if expected_identity is None:
        failures.append(
            "STAGE_D_EXPECTED_RUN_IDENTITY is missing or unparseable — the authorized "
            "run cannot be bound to the accepted release; failing closed")
    elif identity is None:
        failures.append(
            "the authorized run carries NO immutable identity, so it cannot be bound to "
            "the accepted release; failing closed")
    else:
        drift = sorted(key for key, want in expected_identity.items()
                       if str(identity.get(key) or "") != str(want))
        if drift:
            failures.append(
                f"the run's immutable identity disagrees with the accepted release on "
                f"{drift} — the run that executed is not a run of this release; failing closed")
        if str(identity.get("run_id") or "") != str(run_id):
            failures.append(
                "the run's immutable identity names a different run; failing closed")

    # Lifecycle events + secret-leak scan (counts only; no values printed).
    events = fetch_rows(f"/rest/v1/run_events?run_id=eq.{run_id}&select=event_type,agent,phase,created_at,payload&order=id.asc", "run_events", failures)
    out["event_count"] = len(events or [])
    out["event_types"] = [e["event_type"] for e in (events or [])]
    if not events:
        failures.append("no lifecycle events recorded for the run")

    # The canonical ProductOutcome the WORKER recorded when it finalized the
    # run (backend/product_outcome.py, written by backend/finalization.py).
    #
    # This probe deliberately does NOT derive one. Deriving it here would be a
    # second implementation of the semantic rule, free to disagree with the one
    # that actually decided the run's terminal status -- exactly the drift that
    # made Stage D transcribe its own copy of the runtime envelope. The record
    # is copied through verbatim and JUDGED on the operator host by
    # semantic_acceptance.py, which imports the one canonical module.
    #
    # It is a bounded record by construction: static status vocabulary, integer
    # counts, allowlisted blocking codes and a payload DIGEST. No fragment of
    # the product payload travels with it.
    product_outcome = None
    for event in (events or []):
        if event.get("event_type") not in ("run_completed", "run_partial_success"):
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict) and isinstance(payload.get("product_outcome"), dict):
            product_outcome = payload["product_outcome"]
    out["product_outcome"] = product_outcome
    if state in ("completed", "partial_success") and product_outcome is None:
        # A run that reached a PRODUCT terminal state without recording what it
        # produced has not been shown to have produced anything. Technical
        # success is not a product result, so this fails closed here as well as
        # in the semantic gate.
        failures.append(
            "the run reached a product terminal state but recorded no canonical "
            "ProductOutcome on its terminal event — semantic acceptance cannot "
            "be judged; failing closed")
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
    if not reservations:
        failures.append("zero budget reservations recorded — a paid run must reserve before every model call")
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
    out["model_calls"] = int(usage.get("model_calls") or 0)
    out["usage_tokens"] = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "total": usage.get("total_tokens"),
    }
    if int(usage.get("model_calls") or 0) <= 0:
        failures.append("run.usage model_calls is zero — no provider call was accounted for")
    if int(usage.get("input_tokens") or 0) != ledger_input:
        failures.append(f"run.usage input_tokens {usage.get('input_tokens')} != ledger sum {ledger_input}")
    if int(usage.get("output_tokens") or 0) != ledger_output:
        failures.append(f"run.usage output_tokens {usage.get('output_tokens')} != ledger sum {ledger_output}")

    # Tracked cost / token / call caps vs the exact configured values.
    # The caps stay STRICT: no rounding tolerance ever loosens them. ALL
    # THREE cost views must be within the $ cap exactly — the per-row-
    # rounded ledger total, the run.usage snapshot AND the UNROUNDED
    # reservation total: six-decimal rounding may round a true spend of
    # e.g. $1.0000001 down to a ledger/usage total of exactly $1.000000,
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
    # launcher/execution_name/payload/created_at) — real columns only, and
    # a failed query fails closed above instead of masquerading as a count
    # mismatch.
    invocations = fetch_rows(f"/rest/v1/run_invocations?run_id=eq.{run_id}&select=launcher,execution_name,created_at", "run_invocations", failures)
    if invocations is not None:
        out["invocations"] = len(invocations)
        out["invocation_launchers"] = sorted(str(i.get("launcher")) for i in invocations)
        if len(invocations) != 1:
            failures.append(f"run_invocations={len(invocations)}, expected exactly 1")

    # The prepared Government capture must STILL be untouched after the
    # Stage D run — no claim, no execution, no trace.
    out["government_capture"] = government_capture_invariant(failures)

    out["failures"] = failures
    out["ok"] = not failures
    if failures:
        out["operator_action"] = KILL_SWITCH_ACTION
    print(json.dumps(out, default=str))
    if failures:
        sys.exit(1)


def main() -> None:
    mode = os.environ.get("STAGE_D_MODE", "preflight")
    if mode == "preflight":
        preflight()
    elif mode == "govcheck":
        govcheck()
    elif mode == "setup":
        setup()
    elif mode == "terminalize":
        terminalize()
    elif mode == "evidence":
        evidence()
    else:
        fail(f"unknown STAGE_D_MODE {mode!r}")


if __name__ == "__main__":
    main()
