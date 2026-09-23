#!/usr/bin/env python3
"""The complete gate chain ONE website-created run must pass, end to end.

Why this file exists
--------------------

The gates are spread across four surfaces on purpose -- browser bundle,
Next.js server runtime, Cloud Run API, Cloud Run worker -- and each one is
fail-closed by itself. That is good design and a bad operator experience: an
operator who opens three of them and misses the fourth gets a Task Composer
that accepts a task and then does nothing visible, and the reason is in a
different repository layer than the symptom.

So this module is the one place that states the WHOLE chain, in order, with
what each gate does when it is off. It is not a second source of truth: every
flag NAME below is cross-checked against the code that enforces it
(`test_execution_gate_chain.py` fails if a name here is not the name the
backend reads), and the two frontend gates are checked against the strings the
frontend actually reads.

Two gates are easy to miss and cost a whole debugging session, so they are
called out here rather than buried:

*   ``JOB_LAUNCHER`` is not an MILO_ENABLE_* flag and is not in EXECUTION_FLAGS.
    It defaults to ``disabled``, in which case ``build_job_launcher`` returns
    the no-op launcher: the run row is created, the website shows it, and
    **nothing ever executes it**. Run creation being enabled does not launch
    anything by itself.
*   ``MILO_ENABLE_EXECUTION_CONTROL`` gates ``/internal/runs/{id}/(events|
    complete|fail)`` -- the worker's own callbacks. Enabling it in production
    additionally REQUIRES ``MILO_WORKER_AUDIENCE`` and
    ``MILO_APPROVED_WORKER_IDENTITIES``; without them
    ``backend/production_config.py`` fails startup. Turning the flag on without
    those two takes the API down rather than opening a surface.

Nothing here reads or changes any live state. It prints a table.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Where a gate is read, which determines how it is changed.
BROWSER_BUNDLE = "browser bundle (Next.js build-time inline)"
GATEWAY_RUNTIME = "Next.js server runtime (Vercel)"
API_RUNTIME = "Cloud Run API service"
WORKER_RUNTIME = "Cloud Run worker job"

#: How a change to a gate reaches production.
REBUILD = "REBUILD + REDEPLOY the frontend (value is inlined at build time)"
REDEPLOY_FE = "REDEPLOY the frontend (new deployment picks up the env value)"
UPDATE_SERVICE = "gcloud run services update (new revision, no image rebuild)"
UPDATE_JOB = "gcloud run jobs update (applies to the next execution)"


@dataclass(frozen=True)
class Gate:
    name: str
    surface: str
    current_default: str
    required_for_first_run: str
    when_to_enable: str
    requires_redeploy: str
    failure_behavior_when_off: str
    stage: int


#: In the order a single request actually crosses them.
GATE_CHAIN: tuple[Gate, ...] = (
    Gate(
        name="NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI",
        surface=BROWSER_BUNDLE,
        current_default="unset (falsey)",
        required_for_first_run="YES",
        when_to_enable="Stage 2, after Stage 1 verification passes",
        requires_redeploy=REBUILD,
        failure_behavior_when_off=(
            "TaskComposer renders inert and shows 'Task submission is disabled "
            "until a separately approved execution stage.' No request is sent."),
        stage=2,
    ),
    Gate(
        name="GATEWAY_ALLOW_EXECUTION_ROUTES",
        surface=GATEWAY_RUNTIME,
        current_default="unset (falsey)",
        required_for_first_run="YES",
        when_to_enable="Stage 2, with the UI flag",
        requires_redeploy=REDEPLOY_FE,
        failure_behavior_when_off=(
            "isGatewayRequestAllowed() rejects POST /conversations/{id}/runs at "
            "the gateway; the request never reaches the Cloud Run API."),
        stage=2,
    ),
    Gate(
        name="CLOUD_RUN_API_URL",
        surface=GATEWAY_RUNTIME,
        current_default="unset",
        required_for_first_run="YES",
        when_to_enable="Stage 1, as soon as the API service URL exists",
        requires_redeploy=REDEPLOY_FE,
        failure_behavior_when_off=(
            "getCloudRunServiceUrl() throws 'Missing required environment "
            "variable'; every proxied request fails."),
        stage=1,
    ),
    Gate(
        name="GCP_PROJECT_NUMBER / GCP_WORKLOAD_IDENTITY_POOL_ID / "
             "GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID / GCP_SERVICE_ACCOUNT_EMAIL",
        surface=GATEWAY_RUNTIME,
        current_default="unset",
        required_for_first_run="YES",
        when_to_enable="Stage 1",
        requires_redeploy=REDEPLOY_FE,
        failure_behavior_when_off=(
            "getCloudRunIdToken() throws; the gateway cannot mint an ID token, "
            "so the API rejects it as an unverified caller."),
        stage=1,
    ),
    Gate(
        name="MILO_ENABLE_RUN_CREATION",
        surface=API_RUNTIME,
        current_default="false (pinned off by the deployment contract)",
        required_for_first_run="YES",
        when_to_enable="Stage 2",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "ExecutionSurfaceGuardMiddleware rejects POST /conversations/{id}/runs "
            "before routing or validation, with the disabled-surface refusal."),
        stage=2,
    ),
    Gate(
        name="JOB_LAUNCHER",
        surface=API_RUNTIME,
        current_default="disabled",
        required_for_first_run="YES — must be 'cloud_run'",
        when_to_enable="Stage 2, together with run creation",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "EASY TO MISS: build_job_launcher() returns the no-op launcher. The "
            "run row IS created and the website shows it, but no Cloud Run "
            "execution ever starts, so the run never progresses."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_EXECUTION_CONTROL",
        surface=f"{API_RUNTIME} + {WORKER_RUNTIME}",
        current_default="false",
        required_for_first_run="YES",
        when_to_enable="Stage 2",
        requires_redeploy=f"{UPDATE_SERVICE}; {UPDATE_JOB}",
        failure_behavior_when_off=(
            "Gates POST /internal/runs/{id}/(events|complete|fail) — the worker's "
            "own callbacks. Off, the worker cannot report events or terminalize "
            "through the API."),
        stage=2,
    ),
    Gate(
        name="MILO_WORKER_AUDIENCE + MILO_APPROVED_WORKER_IDENTITIES",
        surface=API_RUNTIME,
        current_default="unset",
        required_for_first_run="YES — required BY MILO_ENABLE_EXECUTION_CONTROL",
        when_to_enable="Stage 2, in the SAME update as EXECUTION_CONTROL",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "EASY TO MISS: with EXECUTION_CONTROL on and these unset, "
            "production_config raises WORKER_AUTH_AUDIENCE_MISSING / "
            "WORKER_ALLOWLIST_EMPTY and the API FAILS TO START. Enabling the "
            "flag without these takes the API down rather than opening a route."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_PAID_EXECUTION",
        surface=WORKER_RUNTIME,
        current_default="false",
        required_for_first_run="YES (a MILO run makes model calls)",
        when_to_enable="Stage 2, last",
        requires_redeploy=UPDATE_JOB,
        failure_behavior_when_off=(
            "BudgetTracker's kill switch blocks every provider call. The run is "
            "created and claimed but makes no paid call."),
        stage=2,
    ),
    Gate(
        name="KIMI_API_KEY (or MOONSHOT_API_KEY)",
        surface=WORKER_RUNTIME,
        current_default="bound to nothing",
        required_for_first_run="YES — worker only, never the API",
        when_to_enable="Stage 2, with paid execution",
        requires_redeploy=UPDATE_JOB,
        failure_behavior_when_off=(
            "With paid execution on and no key, the worker refuses the run with "
            "PROVIDER_KEY_MISSING. With paid execution on and no key NAME present "
            "at all, production_config errors PAID_WITHOUT_PROVIDER_KEY at startup."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_CATALOG_EXECUTION",
        surface=WORKER_RUNTIME + " (mirrored on the " + API_RUNTIME + ")",
        current_default="false",
        required_for_first_run="YES for the vehicle-catalog product path",
        when_to_enable=(
            "Stage 2, on the worker AND the API (website-execution-activate.sh "
            "--apply-backend sets both)"),
        requires_redeploy=UPDATE_JOB + "; " + UPDATE_SERVICE,
        failure_behavior_when_off=(
            "catalog_posture() reports master off: no Government tool is "
            "registered and Government preparation is skipped entirely. The run "
            "executes without the catalog capability. On the API it constructs "
            "nothing; with the read below it only routes run creation."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_GOVERNMENT_CATALOG_READ",
        surface=WORKER_RUNTIME + " (mirrored on the " + API_RUNTIME + ")",
        current_default="false",
        required_for_first_run="YES for the vehicle-catalog product path",
        when_to_enable=(
            "Stage 2, and ONLY after the named Mapping Plan revision is prepared "
            "(production-verify.sh --gate prepared); a generic Government "
            "snapshot is not enough"),
        requires_redeploy=UPDATE_JOB + "; " + UPDATE_SERVICE,
        failure_behavior_when_off=(
            "Government preparation is skipped, and a batch-bound run is refused "
            "(GOVERNMENT_READ_REQUIRED). With it ON, the worker refuses every "
            "Swarm V2 run bound to no batch (GOVERNMENT_BATCH_REQUIRED) before a "
            "snapshot is read, and the API -- where it is mirrored -- refuses to "
            "create one (CATALOG_RUN_REQUIRES_MAPPING_PLAN); the website routes "
            "catalog work to the Mapping Plan."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_CATALOG_PROMOTION",
        surface=WORKER_RUNTIME,
        current_default="false",
        required_for_first_run="NO — keep FALSE",
        when_to_enable="Never as part of this activation; separately authorized",
        requires_redeploy=UPDATE_JOB,
        failure_behavior_when_off=(
            "No canonical promotion pipeline is constructed. The run still reads "
            "the register and produces evidence; it writes no canonical facts. "
            "Promotion ON without READ is refused as a contradiction."),
        stage=3,
    ),
    Gate(
        name="MILO_ENABLE_PROPOSAL_READS",
        surface=API_RUNTIME,
        current_default="false",
        required_for_first_run=(
            "NO for a conversation run — YES only if the first run is started "
            "from a workflow proposal rather than the Task Composer"),
        when_to_enable="Stage 2, only if the proposal surface is being used",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "GET /workflow-proposals/{id} is rejected by the surface guard. The "
            "Task Composer path does not touch it."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_PROPOSAL_MUTATIONS",
        surface=API_RUNTIME,
        current_default="false",
        required_for_first_run=(
            "NO for a conversation run — YES only to create/approve/reject/"
            "revise a proposal"),
        when_to_enable="Stage 2, only if the proposal surface is being used",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "POST /workflow-proposals and its approve/reject/revise/project "
            "mutations are rejected. Note MILO_ENABLE_RUN_CREATION separately "
            "gates POST /workflow-proposals/{id}/runs, so starting a run FROM a "
            "proposal needs run creation too."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_WORK_SCOPE_MUTATIONS",
        surface=API_RUNTIME,
        current_default="false",
        required_for_first_run=(
            "NO for a conversation run — YES only to create or revise a Mapping "
            "Plan (a draft work scope; it executes nothing)"),
        when_to_enable=(
            "Stage P (plan authoring: website-execution-activate.sh "
            "--apply-plan-authoring), and kept on at Stage 2"),
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "POST /conversations/{id}/work-scopes and POST /work-scopes/{id}/"
            "revisions are rejected by the surface guard. The plan reads still "
            "answer, and the Mapping Plan stays hidden because the capability "
            "read reports it unavailable. The Task Composer path is untouched."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_WORK_SCOPE_BATCHES",
        surface=API_RUNTIME,
        current_default="false",
        required_for_first_run=(
            "YES for the first paid WEBSITE catalog run -- it is started as ONE "
            "Mapping Plan batch (with MILO_ENABLE_RUN_CREATION); NO for a "
            "conversation run that reads no catalog"),
        when_to_enable=(
            "Only with the Mapping Plan in use, after the plan's revision has "
            "been prepared by the operator capture job"),
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "POST /work-scopes/{id}/runs, /pause and /resume are rejected by the "
            "surface guard. No batch run is created or launched, the plan's "
            "progress read keeps answering, and a batch already running is "
            "untouched (it can still be cancelled through the existing route)."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_WORK_SCOPE_PREPARATION",
        surface=WORKER_RUNTIME,
        current_default="false",
        required_for_first_run=(
            "NO for a conversation run — YES only to PREPARE a Mapping Plan "
            "revision (scoped Government captures and its durable batch queue), "
            "in the operator capture job"),
        when_to_enable=(
            "Never on a service. One operator execution of the capture job at a "
            "time (government-production-capture.sh --prepare-work-scope "
            "--enable-work-scope-preparation), after the vocabulary evidence"),
        requires_redeploy=UPDATE_JOB,
        failure_behavior_when_off=(
            "The capture entrypoint refuses the scoped mode "
            "(CAPTURE_WORK_SCOPE_PREPARATION_DISABLED) before any transport or "
            "repository exists. Nothing is captured, queued or batched; the "
            "whole-resource capture and every run are untouched."),
        stage=2,
    ),
    Gate(
        name="MILO_ENABLE_RUN_CANCELLATION",
        surface=API_RUNTIME,
        current_default="false",
        required_for_first_run="NO — but strongly recommended",
        when_to_enable="Stage 2, so the first run can be stopped",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "POST /runs/{id}/cancel is rejected. The run cannot be cancelled "
            "from the website; it can only run to its own terminal state."),
        stage=2,
    ),
    Gate(
        name="MILO_GATEWAY_AUDIENCE + MILO_APPROVED_GATEWAY_IDENTITIES",
        surface=API_RUNTIME,
        current_default="unset",
        required_for_first_run="YES — required at EVERY stage",
        when_to_enable="Stage 1 (already required by the Stage A deployment)",
        requires_redeploy=UPDATE_SERVICE,
        failure_behavior_when_off=(
            "production_config fails startup with GATEWAY_AUTH_MISSING: without a "
            "verified gateway identity the API would trust bare browser headers."),
        stage=1,
    ),
    Gate(
        name="MILO_EXPECTED_SUPABASE_PROJECT_REF",
        surface=f"{API_RUNTIME} + {WORKER_RUNTIME}",
        current_default="unset",
        required_for_first_run="YES",
        when_to_enable="Stage 1 (bound by the deployment)",
        requires_redeploy=f"{UPDATE_SERVICE}; {UPDATE_JOB}",
        failure_behavior_when_off=(
            "production_config fails startup with PRODUCTION_DEPENDENCY_UNPINNED, "
            "so a runtime can never silently write to another Supabase project."),
        stage=1,
    ),
    Gate(
        name="MILO_RELEASE_SHA",
        surface=f"{API_RUNTIME} + {WORKER_RUNTIME}",
        current_default="unset",
        required_for_first_run="YES",
        when_to_enable="Stage 1, bound by cloud-run.sh to the deployed commit",
        requires_redeploy=f"{UPDATE_SERVICE}; {UPDATE_JOB}",
        failure_behavior_when_off=(
            "Every run records no release, and an unpinned run is refused by the "
            "release/evidence gate."),
        stage=1,
    ),
    Gate(
        name="mandatory-for-paid RuntimePolicy dimensions",
        surface=WORKER_RUNTIME,
        current_default="13 of 18 have NO runtime default",
        required_for_first_run="YES",
        when_to_enable="Stage 1, before paid execution is armed",
        requires_redeploy=UPDATE_JOB,
        failure_behavior_when_off=(
            "With paid execution on, resolve_runtime_policy() refuses the run and "
            "production_config refuses startup. See runtime_policy_manifest.py."),
        stage=1,
    ),
)

STAGE_NAMES = {
    0: "Stage 0 — locked / read-only (current safe posture)",
    1: "Stage 1 — infrastructure verified, website still locked",
    2: "Stage 2 — website execution enabled",
    3: "Stage 3 — human-only; not opened by any script here",
}


def _render_text(gates: Sequence[Gate]) -> str:
    lines: list[str] = ["MILO execution gate chain — one website-created run, end to end", ""]
    for stage in sorted({g.stage for g in gates}):
        lines.append(STAGE_NAMES[stage])
        lines.append("=" * len(STAGE_NAMES[stage]))
        for gate in [g for g in gates if g.stage == stage]:
            lines.append(f"  NAME:                      {gate.name}")
            lines.append(f"  SURFACE:                   {gate.surface}")
            lines.append(f"  CURRENT_DEFAULT:           {gate.current_default}")
            lines.append(f"  REQUIRED_FOR_FIRST_RUN:    {gate.required_for_first_run}")
            lines.append(f"  WHEN_TO_ENABLE:            {gate.when_to_enable}")
            lines.append(f"  REQUIRES_REDEPLOY:         {gate.requires_redeploy}")
            lines.append(f"  FAILURE_BEHAVIOR_WHEN_OFF: {gate.failure_behavior_when_off}")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The complete execution gate chain for one website-created run.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--stage", type=int, default=None,
                        help="show only the gates opened at this stage")
    args = parser.parse_args(list(argv) if argv is not None else None)

    gates = GATE_CHAIN
    if args.stage is not None:
        gates = tuple(g for g in gates if g.stage == args.stage)
    if args.format == "json":
        print(json.dumps([asdict(g) for g in gates], indent=2))
    else:
        print(_render_text(gates))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
