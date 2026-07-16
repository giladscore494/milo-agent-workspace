"""Bootstrap v2 engine and command-line interface.

The engine is the only coordinator of provider operations. It drives the
monotonic state machine: local guard, complete read-only global discovery,
frozen plan, digest-checked apply authorization, stage-ordered mutations
with post-write verification after every individual write, final audit,
metadata commit. The first blocker closes the mutation gate permanently
for the run; recovery is a fresh run with fresh discovery.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import (
    CloudRunResourceState,
    Finding,
    IamPolicyState,
    MetadataStatus,
    MetadataV3,
    Mode,
    MutationOperation,
    MutationPlan,
    OperationType,
    PostWriteVerification,
    ProbeOutcome,
    Provider,
    ReadOperation,
    RecoveryStep,
    RedisIdentity,
    ResourceIdentity,
    RunResult,
    SecretState,
    Severity,
    Stage,
    StageResult,
    UpstashDatabaseState,
    VercelEnvVarState,
    VercelProjectState,
    WifState,
)
from .planner import (
    DiscoveredWorld,
    Observed,
    PlannerEvidenceError,
    _cloud_run_payload,
    build_plan,
    desired_cloud_run_env,
    plan_digest,
    plan_to_canonical_json,
    state_digest,
)
from .policy import (
    API_SECRET_REFS,
    BootstrapConfig,
    NUMERIC_PIN_REQUIRED_SECRETS,
    OPERATOR_ACK_EXPECTED,
    REQUIRED_GCP_APIS,
    SECRET_MANAGER_RESOURCE_NAMES,
    VERCEL_FORBIDDEN_VARS,
    VERCEL_MANAGED_VARS,
    VERCEL_REUSED_VARS,
    WORKER_SECRET_REFS,
    LEGACY_RUNTIME_IDENTITY_PATTERN,
)
from .report import render_human_summary, write_json_report
from .result import build_run_result
from .state_machine import BootstrapStateMachine, StageBlocked
from .subprocess_runner import (
    MutationAfterBlockError,
    MutationGate,
    MutationLedger,
    SecretRegistry,
    SubprocessRunner,
    UndeclaredMutationError,
)
from .validators import cloud_run as cloud_run_validators
from .validators import iam as iam_validators
from .validators import metadata as metadata_validators
from .validators import redis as redis_validators
from .validators import wif as wif_validators

#: Caller-environment variables that would taint discovered evidence. Their
#: presence blocks: provider truth must come from discovery, never from a
#: local .env or exported override.
FORBIDDEN_ENV_OVERRIDES: tuple[str, ...] = (
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "MILO_REDIS_DB_ID",
    "MILO_REDIS_TOKEN_FINGERPRINT",
    "MILO_REDIS_SECRET_VERSION",
    "MILO_RELEASE_SHA",
)


@dataclass
class DiscoveryOutput:
    world: DiscoveredWorld | None = None
    findings: list[Finding] = field(default_factory=list)
    reads: list[ReadOperation] = field(default_factory=list)
    selected_db: UpstashDatabaseState | None = None
    upstash_rest_token: str = ""
    redis_identity: RedisIdentity | None = None
    secret_states: dict[str, SecretState] = field(default_factory=dict)
    secret_policies: dict[str, IamPolicyState] = field(default_factory=dict)
    gateway_sa_policy: IamPolicyState | None = None
    invoker_policy: IamPolicyState | None = None
    worker_state: CloudRunResourceState | None = None
    api_state: CloudRunResourceState | None = None
    vercel_project: VercelProjectState | None = None
    vercel_env: dict[str, VercelEnvVarState] = field(default_factory=dict)
    wif_state: WifState | None = None


class BootstrapEngine:
    def __init__(
        self,
        config: BootstrapConfig,
        mode: Mode,
        local_port,
        gcp_port,
        upstash_port,
        vercel_port,
        output_dir: Path,
        approved_plan_digest: str = "",
        environ: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.local = local_port
        self.gcp = gcp_port
        self.upstash = upstash_port
        self.vercel = vercel_port
        self.output_dir = output_dir
        self.approved_plan_digest = approved_plan_digest
        self.environ = dict(os.environ) if environ is None else environ

        self.machine = BootstrapStateMachine()
        self.ledger = MutationLedger()
        self.gate: MutationGate | None = None
        self.findings: list[Finding] = []
        self.reads: list[ReadOperation] = []
        self.verifications: list = []
        self.created_resources: list[ResourceIdentity] = []
        self.recovery_steps: list[RecoveryStep] = []
        self.plan: MutationPlan | None = None
        self.digest: str = ""
        self.metadata_status = MetadataStatus.NOT_APPLICABLE
        self._read_seq = 0
        self._stage_verifications: list = []

    # ------------------------------------------------------------------ utils

    def _record_read(
        self,
        provider: Provider,
        description: str,
        outcome: ProbeOutcome,
        out: DiscoveryOutput,
        resource: ResourceIdentity | None = None,
        critical: bool = True,
    ) -> None:
        self._read_seq += 1
        out.reads.append(
            ReadOperation(
                sequence=self._read_seq,
                provider=provider,
                description=description,
                outcome=outcome,
                resource=resource,
                critical=critical,
            )
        )

    @staticmethod
    def _finding(
        code: str,
        message: str,
        stage: Stage,
        severity: Severity = Severity.BLOCKED,
        requires_manual: bool = False,
        critical: bool = True,
    ) -> Finding:
        return Finding(
            code=code,
            severity=severity,
            message=message,
            stage=stage,
            critical=critical,
            requires_manual=requires_manual,
        )

    def _complete(self, stage_result: StageResult, to_stage: Stage) -> None:
        self.findings.extend(stage_result.findings)
        self.machine.complete_stage(stage_result, to_stage, self.mode, self.plan)

    # ----------------------------------------------------------- local guard

    def _local_guard(self) -> None:
        state = self.local.discover()
        stage = Stage.LOCAL_GUARD_VERIFIED
        findings: list[Finding] = []

        def blocked(code: str, message: str) -> None:
            findings.append(self._finding(code, message, stage))

        if state.repository != self.config.repository:
            blocked(
                "LOCAL_WRONG_REPOSITORY",
                f"repository {state.repository!r} != {self.config.repository!r}",
            )
        if not state.worktree_clean:
            blocked("LOCAL_DIRTY_WORKTREE", "worktree is not clean")
        if state.head_sha != self.config.bootstrap_sha:
            blocked(
                "LOCAL_SHA_MISMATCH",
                "HEAD does not equal the approved full 40-character bootstrap sha",
            )
        if self.mode is Mode.APPLY and state.ref != self.config.trusted_ref:
            blocked(
                "LOCAL_UNTRUSTED_REF",
                f"apply requires the trusted ref {self.config.trusted_ref!r}, "
                f"got {state.ref!r}",
            )
        if state.environment != "production":
            blocked("LOCAL_WRONG_ENVIRONMENT", "environment must be 'production'")
        if self.mode is Mode.APPLY and state.operator_ack != OPERATOR_ACK_EXPECTED:
            blocked(
                "LOCAL_MISSING_ACK",
                "apply requires the exact operator acknowledgement value",
            )
        if not state.python_ok or not state.tooling_ok:
            blocked("LOCAL_TOOLING", "required python or provider tooling unavailable")
        if state.deprecated_metadata_keys:
            blocked(
                "LOCAL_DEPRECATED_KEYS",
                "deprecated metadata keys present: "
                + ", ".join(state.deprecated_metadata_keys),
            )
        overrides = tuple(
            name for name in FORBIDDEN_ENV_OVERRIDES if name in self.environ
        )
        if overrides:
            blocked(
                "LOCAL_ENV_OVERRIDE",
                "forbidden caller-environment overrides present: "
                + ", ".join(overrides),
            )
        for dotenv in state.dotenv_influence:
            findings.append(
                self._finding(
                    "LOCAL_DOTENV_PRESENT",
                    f"local env file {dotenv} exists; it is never loaded by "
                    "bootstrap v2",
                    stage,
                    severity=Severity.INFO,
                    critical=False,
                )
            )
        self._complete(StageResult(stage=stage, findings=tuple(findings)), stage)

    # ------------------------------------------------------------- discovery

    def _discover(self, strict: bool) -> DiscoveryOutput:
        """Complete read-only global preflight. ``strict`` applies the full
        exact-state validators (audit mode and final audit)."""

        out = DiscoveryOutput()
        stage = Stage.GLOBAL_DISCOVERY_COMPLETE
        cfg = self.config

        # ---- Upstash ---------------------------------------------------
        list_probe = self.upstash.list_databases()
        self._record_read(
            Provider.UPSTASH, "list redis databases", list_probe.outcome, out
        )
        upstash_observed = Observed(outcome=ProbeOutcome.UNKNOWN_ERROR)
        if list_probe.outcome is ProbeOutcome.PRESENT:
            selected, selection_findings = redis_validators.select_database(
                list_probe.databases,
                cfg.upstash_database_id,
                cfg.upstash_database_name,
                stage,
            )
            out.findings.extend(selection_findings)
            if selected is not None:
                detail_probe, rest_token = self.upstash.get_database(
                    selected.database_id
                )
                self._record_read(
                    Provider.UPSTASH,
                    f"get redis database {selected.database_id}",
                    detail_probe.outcome,
                    out,
                )
                if detail_probe.outcome is ProbeOutcome.PRESENT:
                    detail = detail_probe.databases[0]
                    out.findings.extend(
                        redis_validators.verify_database_detail(selected, detail, stage)
                    )
                    out.selected_db = detail
                    out.upstash_rest_token = rest_token
                    upstash_observed = Observed(
                        outcome=ProbeOutcome.PRESENT, state=detail
                    )
                else:
                    upstash_observed = Observed(outcome=detail_probe.outcome)
            elif not selection_findings:
                upstash_observed = Observed(outcome=ProbeOutcome.CLEANLY_ABSENT)
        else:
            upstash_observed = Observed(outcome=list_probe.outcome)

        # ---- GCP: APIs ---------------------------------------------------
        services_probe, enabled = self.gcp.list_enabled_services()
        self._record_read(
            Provider.GCP, "list enabled gcp services", services_probe.outcome, out
        )
        if services_probe.outcome is ProbeOutcome.PRESENT:
            missing = tuple(api for api in REQUIRED_GCP_APIS if api not in enabled)
            if missing:
                out.findings.append(
                    self._finding(
                        "GCP_API_NOT_ENABLED",
                        "required apis not enabled: " + ", ".join(missing),
                        stage,
                    )
                )

        # ---- GCP: service accounts ---------------------------------------
        sa_observed: list[tuple[str, Observed]] = []
        for email in (
            cfg.worker_service_account,
            cfg.gateway_service_account,
            cfg.api_service_account,
        ):
            probe, sa_state = self.gcp.describe_service_account(email)
            self._record_read(
                Provider.GCP, f"describe service account {email}", probe.outcome, out
            )
            sa_observed.append((email, Observed(outcome=probe.outcome, state=sa_state)))

        # ---- GCP: secrets --------------------------------------------------
        secret_observed: list[tuple[str, Observed]] = []
        for name in SECRET_MANAGER_RESOURCE_NAMES:
            probe, secret_state = self.gcp.describe_secret(name)
            self._record_read(
                Provider.GCP, f"describe secret {name}", probe.outcome, out
            )
            if secret_state is not None:
                out.secret_states[name] = secret_state
            secret_observed.append((name, Observed(outcome=probe.outcome, state=secret_state)))

        # ---- GCP: redis token payload fingerprint -------------------------
        version_addition_required = False
        enabled_version = ""
        redis_secret = out.secret_states.get("UPSTASH_REDIS_REST_TOKEN")
        if redis_secret is not None and redis_secret.latest_enabled_version:
            payload_probe, payload = self.gcp.access_secret_payload(
                "UPSTASH_REDIS_REST_TOKEN", redis_secret.latest_enabled_version
            )
            self._record_read(
                Provider.GCP,
                "access redis token payload for fingerprint reconciliation",
                payload_probe.outcome,
                out,
            )
            if payload_probe.outcome is ProbeOutcome.PRESENT and out.selected_db:
                sm_fingerprint = redis_validators.fingerprint_sha256(payload.strip())
                if sm_fingerprint == out.selected_db.token_fingerprint_sha256:
                    enabled_version = redis_secret.latest_enabled_version
                else:
                    version_addition_required = True
                    enabled_version = str(
                        int(redis_secret.latest_enabled_version) + 1
                    )
        elif redis_secret is not None and out.selected_db:
            version_addition_required = True
            enabled_version = "1"
        elif redis_secret is None and out.selected_db:
            # Secret cleanly absent: plan will create it and add version 1.
            version_addition_required = True
            enabled_version = "1"

        # ---- GCP: IAM ------------------------------------------------------
        secret_iam_observed: list[tuple[str, Observed]] = []
        for name in SECRET_MANAGER_RESOURCE_NAMES:
            probe, policy = self.gcp.get_secret_iam(name)
            self._record_read(
                Provider.GCP, f"get iam policy of secret {name}", probe.outcome, out
            )
            if policy is not None:
                out.secret_policies[name] = policy
                out.findings.extend(
                    iam_validators.find_forbidden_principals(policy, stage)
                )
            secret_iam_observed.append((name, Observed(outcome=probe.outcome, state=policy)))

        gateway_probe, gateway_policy = self.gcp.get_service_account_iam(
            cfg.gateway_service_account
        )
        self._record_read(
            Provider.GCP, "get gateway sa iam policy", gateway_probe.outcome, out
        )
        out.gateway_sa_policy = gateway_policy

        invoker_probe, invoker_policy = self.gcp.get_run_invoker_iam(
            cfg.cloud_run_api_service
        )
        self._record_read(
            Provider.GCP, "get run.invoker iam policy", invoker_probe.outcome, out
        )
        out.invoker_policy = invoker_policy
        if invoker_policy is not None:
            out.findings.extend(
                iam_validators.find_forbidden_principals(invoker_policy, stage)
            )

        # ---- GCP: WIF --------------------------------------------------------
        wif_probe, wif_state = self.gcp.describe_wif(
            cfg.wif_pool_id, cfg.wif_provider_id, cfg.gcp_project_number
        )
        self._record_read(Provider.GCP, "describe wif pool/provider", wif_probe.outcome, out)
        out.wif_state = wif_state
        if wif_state is not None:
            out.findings.extend(wif_validators.verify_wif(wif_state, cfg, stage))
        elif wif_probe.outcome is ProbeOutcome.CLEANLY_ABSENT:
            out.findings.append(
                self._finding(
                    "WIF_ABSENT",
                    "workload identity pool/provider not found; must be created "
                    "manually before bootstrap (wif creation is out of scope)",
                    stage,
                    severity=Severity.WARN,
                    requires_manual=True,
                )
            )

        # ---- GCP: Cloud Run ---------------------------------------------------
        job_probe, worker_state = self.gcp.describe_run_job(cfg.cloud_run_worker_job)
        self._record_read(
            Provider.GCP,
            f"describe cloud run job {cfg.cloud_run_worker_job}",
            job_probe.outcome,
            out,
        )
        out.worker_state = worker_state
        service_probe, api_state = self.gcp.describe_run_service(
            cfg.cloud_run_api_service
        )
        self._record_read(
            Provider.GCP,
            f"describe cloud run service {cfg.cloud_run_api_service}",
            service_probe.outcome,
            out,
        )
        out.api_state = api_state

        for label, probe_outcome, run_state, expected_sa, refs in (
            (
                "worker job",
                job_probe.outcome,
                worker_state,
                cfg.worker_service_account,
                WORKER_SECRET_REFS,
            ),
            (
                "api service",
                service_probe.outcome,
                api_state,
                cfg.api_service_account,
                API_SECRET_REFS,
            ),
        ):
            if probe_outcome is ProbeOutcome.CLEANLY_ABSENT:
                out.findings.append(
                    self._finding(
                        "CLOUD_RUN_RESOURCE_ABSENT",
                        f"cloud run {label} does not exist; it must be deployed "
                        "by the release pipeline first (bootstrap never deploys "
                        "images)",
                        stage,
                        severity=Severity.WARN,
                        requires_manual=True,
                    )
                )
            elif run_state is not None:
                if LEGACY_RUNTIME_IDENTITY_PATTERN.match(run_state.service_account):
                    out.findings.append(
                        self._finding(
                            "LEGACY_RUNTIME_IDENTITY",
                            f"cloud run {label} currently runs as the legacy "
                            f"identity {run_state.service_account}; it will be "
                            "migrated to a distinct identity and is never "
                            "silently accepted",
                            stage,
                            severity=Severity.INFO,
                            critical=False,
                        )
                    )
                container, selection = cloud_run_validators.select_application_container(
                    run_state, (cfg.cloud_run_api_service, cfg.cloud_run_worker_job, "app"), stage
                )
                out.findings.extend(selection)
                if container is not None:
                    out.findings.extend(
                        cloud_run_validators.find_env_conflicts(container, stage)
                    )
                if strict:
                    out.findings.extend(
                        cloud_run_validators.validate_resource(
                            run_state,
                            expected_sa,
                            refs,
                            NUMERIC_PIN_REQUIRED_SECRETS,
                            (cfg.cloud_run_api_service, cfg.cloud_run_worker_job, "app"),
                            stage,
                            expected_bootstrap_sha=cfg.bootstrap_sha,
                        )
                    )

        # ---- Vercel -------------------------------------------------------------
        project_probe = self.vercel.get_project(
            cfg.vercel_project_id or cfg.vercel_project_name
        )
        self._record_read(
            Provider.VERCEL, "get vercel project", project_probe.outcome, out
        )
        if project_probe.outcome is ProbeOutcome.PRESENT and project_probe.project:
            project = project_probe.project
            out.vercel_project = project
            if cfg.vercel_project_id and project.project_id != cfg.vercel_project_id:
                out.findings.append(
                    self._finding(
                        "VERCEL_WRONG_PROJECT_ID",
                        f"vercel project id {project.project_id!r} != expected "
                        f"{cfg.vercel_project_id!r}",
                        stage,
                    )
                )
            if cfg.vercel_org_id and project.org_id != cfg.vercel_org_id:
                out.findings.append(
                    self._finding(
                        "VERCEL_WRONG_ORG_ID",
                        f"vercel org id {project.org_id!r} != expected "
                        f"{cfg.vercel_org_id!r}",
                        stage,
                    )
                )
            if project.name != cfg.vercel_project_name:
                out.findings.append(
                    self._finding(
                        "VERCEL_WRONG_PROJECT_NAME",
                        f"vercel project name {project.name!r} != expected "
                        f"{cfg.vercel_project_name!r}",
                        stage,
                    )
                )

        env_probe = self.vercel.list_env(cfg.vercel_project_id or cfg.vercel_project_name)
        self._record_read(
            Provider.VERCEL, "list vercel env inventory", env_probe.outcome, out
        )
        vercel_env_observed: list[tuple[str, Observed]] = []
        if env_probe.outcome is ProbeOutcome.PRESENT:
            for var in env_probe.env_vars:
                if "production" in var.target:
                    out.vercel_env[var.key] = var
            for name in VERCEL_FORBIDDEN_VARS:
                if any(var.key == name for var in env_probe.env_vars):
                    out.findings.append(
                        self._finding(
                            "VERCEL_FORBIDDEN_VARIABLE",
                            f"forbidden vercel variable {name} present; zero "
                            "vercel writes will be performed",
                            stage,
                        )
                    )
            for name in VERCEL_REUSED_VARS:
                if name not in out.vercel_env:
                    out.findings.append(
                        self._finding(
                            "VERCEL_REUSED_VARIABLE_MISSING",
                            f"required reused vercel variable {name} missing "
                            "from production environment",
                            stage,
                            severity=Severity.WARN,
                            requires_manual=True,
                        )
                    )
            for name in VERCEL_MANAGED_VARS:
                observed_var = out.vercel_env.get(name)
                vercel_env_observed.append(
                    (
                        name,
                        Observed(
                            outcome=(
                                ProbeOutcome.PRESENT
                                if observed_var is not None
                                else ProbeOutcome.CLEANLY_ABSENT
                            ),
                            state=observed_var,
                        ),
                    )
                )

        # ---- Redis identity --------------------------------------------------
        if out.selected_db is not None and enabled_version:
            identity = RedisIdentity(
                database_id=out.selected_db.database_id,
                database_name=out.selected_db.name,
                rest_url=out.selected_db.rest_url,
                token_fingerprint_sha256=out.selected_db.token_fingerprint_sha256,
                secret_resource_name="UPSTASH_REDIS_REST_TOKEN",
                enabled_secret_version=enabled_version,
                api_secret_version_pin=enabled_version,
                worker_secret_version_pin=enabled_version,
                vercel_value_fingerprint_sha256=out.selected_db.token_fingerprint_sha256,
                logical_environment="production",
            )
            out.findings.extend(
                redis_validators.verify_redis_identity_coherence(
                    identity, out.selected_db, stage
                )
            )
            out.redis_identity = identity
        elif out.selected_db is None:
            # Not blocking here: Stage A may legitimately create the database
            # in this run. Stages D/E block themselves when the identity is
            # still unavailable, and the final audit reports residual drift.
            out.findings.append(
                self._finding(
                    "REDIS_IDENTITY_UNAVAILABLE",
                    "no coherent redis identity yet (database absent); after "
                    "stage A creates it, a rerun adopts it for cloud run and "
                    "vercel configuration",
                    stage,
                    severity=Severity.INFO,
                    critical=False,
                )
            )

        out.world = DiscoveredWorld(
            upstash_database=upstash_observed,
            service_accounts=tuple(sa_observed),
            secrets=tuple(secret_observed),
            secret_iam=tuple(secret_iam_observed),
            wif=Observed(outcome=wif_probe.outcome, state=wif_state),
            gateway_sa_iam=Observed(outcome=gateway_probe.outcome, state=gateway_policy),
            run_invoker_iam=Observed(outcome=invoker_probe.outcome, state=invoker_policy),
            worker_job=Observed(outcome=job_probe.outcome, state=worker_state),
            api_service=Observed(outcome=service_probe.outcome, state=api_state),
            vercel_project=Observed(
                outcome=project_probe.outcome, state=project_probe.project
            ),
            vercel_env=tuple(vercel_env_observed),
            redis_identity=out.redis_identity,
            redis_secret_version_addition_required=version_addition_required,
        )
        return out

    # ------------------------------------------------------------------ apply

    def _verify_post_write(
        self,
        operation: MutationOperation,
        observed_digest: str,
        detail: str = "",
    ) -> bool:
        verified = observed_digest == operation.post_write_read.expected_post_state_digest
        record = PostWriteVerification(
            idempotency_key=operation.idempotency_key,
            verified=verified,
            observed_post_state_digest=observed_digest,
            expected_post_state_digest=operation.post_write_read.expected_post_state_digest,
            detail=detail,
        )
        self.verifications.append(record)
        self._stage_verifications.append(record)
        return verified

    def _stage_result(
        self,
        stage: Stage,
        findings: tuple[Finding, ...] = (),
        reads: tuple[ReadOperation, ...] = (),
    ) -> StageResult:
        result = StageResult(
            stage=stage,
            findings=findings,
            reads=reads,
            mutations=self.ledger.records(),
            verifications=tuple(self._stage_verifications),
        )
        self._stage_verifications = []
        return result

    def _plan_ops(self, *types: OperationType) -> tuple[MutationOperation, ...]:
        assert self.plan is not None
        return tuple(
            op for op in self.plan.operations if op.operation_type in types
        )

    def _apply_stage_a(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.UPSTASH_STAGE_VERIFIED
        findings: list[Finding] = []
        assert self.gate is not None
        ops = self._plan_ops(OperationType.UPSTASH_CREATE_DATABASE)
        if len(ops) > 1:
            findings.append(
                self._finding(
                    "UPSTASH_MULTIPLE_CREATES",
                    "the frozen plan may contain at most one database create",
                    stage,
                )
            )
        elif ops:
            op = ops[0]
            probe, created_id = self.upstash.create_database(
                self.gate,
                op.idempotency_key,
                op.resource,
                self.config.upstash_database_name,
                self.config.gcp_region,
            )
            if created_id:
                self.created_resources.append(
                    ResourceIdentity(
                        provider=Provider.UPSTASH,
                        kind="redis_database",
                        name=created_id,
                        scope="upstash",
                    )
                )
            if probe.outcome is not ProbeOutcome.PRESENT:
                findings.append(
                    self._finding(
                        "UPSTASH_CREATE_FAILED",
                        f"database create failed: {probe.detail}",
                        stage,
                    )
                )
            else:
                detail_probe, _token = self.upstash.get_database(created_id)
                self._read_seq += 1
                self.reads.append(
                    ReadOperation(
                        sequence=self._read_seq,
                        provider=Provider.UPSTASH,
                        description=f"post-create reread of database {created_id}",
                        outcome=detail_probe.outcome,
                    )
                )
                if detail_probe.outcome is not ProbeOutcome.PRESENT:
                    self._verify_post_write(
                        op, "unavailable", "post-create reread failed"
                    )
                    findings.append(
                        self._finding(
                            "UPSTASH_POST_CREATE_UNVERIFIED",
                            f"created database {created_id} could not be reread; "
                            "run is partial, no later mutations, no metadata; "
                            "rerun to adopt the created database (no rollback is "
                            "claimed)",
                            stage,
                        )
                    )
                else:
                    detail = detail_probe.databases[0]
                    detail_findings = redis_validators.verify_database_detail(
                        UpstashDatabaseState(
                            database_id=created_id,
                            name=self.config.upstash_database_name,
                            state="active",
                            tls=True,
                            region=detail.region,
                            endpoint=detail.endpoint,
                            rest_url=detail.rest_url,
                        ),
                        detail,
                        stage,
                    )
                    findings.extend(detail_findings)
                    observed_payload = {
                        "name": detail.name,
                        "tls": detail.tls,
                        "region": self.config.gcp_region,
                        "state": "active" if detail.state.lower() == "active" else detail.state,
                    }
                    if not self._verify_post_write(op, state_digest(observed_payload)):
                        findings.append(
                            self._finding(
                                "UPSTASH_POST_CREATE_MISMATCH",
                                f"created database {created_id} does not match the "
                                "intended post-state; rerun will adopt the created "
                                "resource",
                                stage,
                            )
                        )
            if findings:
                self.recovery_steps.append(
                    RecoveryStep(
                        order=len(self.recovery_steps) + 1,
                        description=(
                            "Re-run bootstrap v2: fresh discovery will adopt the "
                            "recorded created database instead of creating another"
                        ),
                    )
                )
        self._complete(self._stage_result(stage, tuple(findings)), stage)

    def _apply_stage_b(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.GCP_IDENTITY_SECRET_STAGE_VERIFIED
        findings: list[Finding] = []
        assert self.gate is not None

        for op in self._plan_ops(OperationType.GCP_CREATE_SERVICE_ACCOUNT):
            outcome, detail = self.gcp.create_service_account(
                self.gate, op.idempotency_key, op.resource, op.resource.name
            )
            if outcome is not ProbeOutcome.PRESENT:
                findings.append(
                    self._finding(
                        "SA_CREATE_FAILED",
                        f"service account create failed: {detail}",
                        stage,
                    )
                )
                break
            self.created_resources.append(op.resource)
            probe, sa_state = self.gcp.describe_service_account(op.resource.name)
            if probe.outcome is not ProbeOutcome.PRESENT or sa_state is None:
                self._verify_post_write(op, "unavailable", "post-create reread failed")
                findings.append(
                    self._finding(
                        "SA_POST_CREATE_UNVERIFIED",
                        f"created service account {op.resource.name} could not be "
                        "reread",
                        stage,
                    )
                )
                break
            observed = {"email": sa_state.email, "disabled": sa_state.disabled}
            if not self._verify_post_write(op, state_digest(observed)):
                findings.append(
                    self._finding(
                        "SA_POST_CREATE_MISMATCH",
                        f"service account {op.resource.name} post-state mismatch",
                        stage,
                    )
                )
                break

        if not findings:
            for op in self._plan_ops(OperationType.GCP_CREATE_SECRET):
                outcome, detail = self.gcp.create_secret(
                    self.gate, op.idempotency_key, op.resource, op.resource.name
                )
                if outcome is not ProbeOutcome.PRESENT:
                    findings.append(
                        self._finding(
                            "SECRET_CREATE_FAILED",
                            f"secret create failed: {detail}",
                            stage,
                        )
                    )
                    break
                self.created_resources.append(op.resource)
                probe, secret_state = self.gcp.describe_secret(op.resource.name)
                if probe.outcome is not ProbeOutcome.PRESENT or secret_state is None:
                    self._verify_post_write(op, "unavailable", "post-create reread failed")
                    findings.append(
                        self._finding(
                            "SECRET_POST_CREATE_UNVERIFIED",
                            f"created secret {op.resource.name} could not be reread",
                            stage,
                        )
                    )
                    break
                observed = {"name": secret_state.name, "exists": secret_state.exists}
                if not self._verify_post_write(op, state_digest(observed)):
                    findings.append(
                        self._finding(
                            "SECRET_POST_CREATE_MISMATCH",
                            f"secret {op.resource.name} post-state mismatch",
                            stage,
                        )
                    )
                    break

        if not findings:
            for op in self._plan_ops(OperationType.GCP_ADD_SECRET_VERSION):
                token = discovery.upstash_rest_token
                if not token:
                    findings.append(
                        self._finding(
                            "SECRET_VERSION_INPUT_UNAVAILABLE",
                            "redis token version add required but the declared "
                            "input is unavailable",
                            stage,
                        )
                    )
                    break
                outcome, detail = self.gcp.add_secret_version(
                    self.gate, op.idempotency_key, op.resource, op.resource.name, token
                )
                if outcome is not ProbeOutcome.PRESENT:
                    findings.append(
                        self._finding(
                            "SECRET_VERSION_ADD_FAILED",
                            f"secret version add failed: {detail}",
                            stage,
                        )
                    )
                    break
                probe, secret_state = self.gcp.describe_secret(op.resource.name)
                if (
                    probe.outcome is not ProbeOutcome.PRESENT
                    or secret_state is None
                    or not secret_state.latest_enabled_version
                ):
                    self._verify_post_write(op, "unavailable", "post-add reread failed")
                    findings.append(
                        self._finding(
                            "SECRET_VERSION_UNVERIFIED",
                            "new secret version could not be reread",
                            stage,
                        )
                    )
                    break
                payload_probe, payload = self.gcp.access_secret_payload(
                    op.resource.name, secret_state.latest_enabled_version
                )
                observed_fp = (
                    redis_validators.fingerprint_sha256(payload.strip())
                    if payload_probe.outcome is ProbeOutcome.PRESENT
                    else "unavailable"
                )
                if not self._verify_post_write(op, observed_fp):
                    findings.append(
                        self._finding(
                            "SECRET_VERSION_FINGERPRINT_MISMATCH",
                            "new secret version payload fingerprint does not match "
                            "the intended redis token fingerprint",
                            stage,
                        )
                    )
                    break

        self._complete(self._stage_result(stage, tuple(findings)), stage)

    def _apply_iam_op(
        self,
        op: MutationOperation,
        discovery: DiscoveryOutput,
        stage: Stage,
        findings: list[Finding],
    ) -> bool:
        assert self.gate is not None
        cfg = self.config
        if op.operation_type is OperationType.GCP_SET_SECRET_IAM:
            name = op.resource.name
            policy = discovery.secret_policies.get(name)
            if policy is None:
                findings.append(
                    self._finding(
                        "IAM_PRESTATE_UNAVAILABLE",
                        f"no fresh policy for secret {name}",
                        stage,
                    )
                )
                return False
            members = _intended_secret_accessors_engine(name, cfg)
            outcome, detail = self.gcp.set_secret_iam(
                self.gate, op.idempotency_key, op.resource, name, policy, members
            )
            reread = lambda: self.gcp.get_secret_iam(name)  # noqa: E731
            role = "roles/secretmanager.secretAccessor"
        elif op.operation_type is OperationType.GCP_SET_WIF_IAM:
            policy = discovery.gateway_sa_policy
            if policy is None:
                findings.append(
                    self._finding(
                        "IAM_PRESTATE_UNAVAILABLE",
                        "no fresh gateway sa policy",
                        stage,
                    )
                )
                return False
            members = (
                wif_validators.expected_principal_set(
                    cfg.gcp_project_number, cfg.wif_pool_id, cfg.vercel_project_id
                ),
            )
            outcome, detail = self.gcp.set_gateway_wif_iam(
                self.gate,
                op.idempotency_key,
                op.resource,
                cfg.gateway_service_account,
                policy,
                members,
            )
            reread = lambda: self.gcp.get_service_account_iam(  # noqa: E731
                cfg.gateway_service_account
            )
            role = "roles/iam.workloadIdentityUser"
        else:
            policy = discovery.invoker_policy
            if policy is None:
                findings.append(
                    self._finding(
                        "IAM_PRESTATE_UNAVAILABLE", "no fresh invoker policy", stage
                    )
                )
                return False
            members = (f"serviceAccount:{cfg.gateway_service_account}",)
            outcome, detail = self.gcp.set_run_invoker_iam(
                self.gate,
                op.idempotency_key,
                op.resource,
                cfg.cloud_run_api_service,
                policy,
                members,
            )
            reread = lambda: self.gcp.get_run_invoker_iam(  # noqa: E731
                cfg.cloud_run_api_service
            )
            role = "roles/run.invoker"

        if outcome is not ProbeOutcome.PRESENT:
            findings.append(
                self._finding(
                    "IAM_WRITE_FAILED", f"iam write failed: {detail}", stage
                )
            )
            return False
        probe, new_policy = reread()
        if probe.outcome is not ProbeOutcome.PRESENT or new_policy is None:
            self._verify_post_write(op, "unavailable", "post-write policy reread failed")
            findings.append(
                self._finding(
                    "IAM_POST_WRITE_UNAVAILABLE",
                    "post-write policy reread unavailable; stopping before the "
                    "next mutation",
                    stage,
                )
            )
            return False
        exact = iam_validators.verify_exact_policy(new_policy, role, members, stage)
        findings.extend(exact)
        observed_members: tuple[str, ...] = ()
        for binding in new_policy.bindings:
            if binding.role == role and not binding.condition_expression:
                observed_members = binding.members
        observed = {
            "role": role,
            "members": sorted(observed_members),
            "condition": "",
        }
        if not self._verify_post_write(op, state_digest(observed)):
            findings.append(
                self._finding(
                    "IAM_POST_WRITE_MISMATCH",
                    "post-write policy does not exactly match the intended state "
                    "(possible concurrent drift)",
                    stage,
                )
            )
            return False
        return not exact

    def _apply_stage_c(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.IAM_STAGE_VERIFIED
        findings: list[Finding] = []
        if discovery.wif_state is None:
            wif_findings = (
                self._finding(
                    "IAM_WITHOUT_WIF_EVIDENCE",
                    "iam mutation is forbidden with missing, partial or invalid "
                    "wif evidence",
                    stage,
                ),
            )
            self._complete(self._stage_result(stage, wif_findings), stage)
            return
        for op in self._plan_ops(
            OperationType.GCP_SET_SECRET_IAM,
            OperationType.GCP_SET_WIF_IAM,
            OperationType.GCP_SET_RUN_INVOKER_IAM,
        ):
            if not self._apply_iam_op(op, discovery, stage, findings):
                break
        self._complete(self._stage_result(stage, tuple(findings)), stage)

    def _apply_stage_d(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.CLOUD_RUN_STAGE_VERIFIED
        findings: list[Finding] = []
        assert self.gate is not None
        cfg = self.config
        redis = discovery.redis_identity
        ops = self._plan_ops(
            OperationType.GCP_UPDATE_WORKER_JOB_CONFIG,
            OperationType.GCP_UPDATE_API_SERVICE_CONFIG,
        )
        if ops and redis is None:
            findings.append(
                self._finding(
                    "CLOUD_RUN_WITHOUT_REDIS_IDENTITY",
                    "cloud run configuration requires a coherent redis identity; "
                    "rerun after the database exists",
                    stage,
                )
            )
            self._complete(self._stage_result(stage, tuple(findings)), stage)
            return

        # Worker job strictly before API service.
        ordered = sorted(
            ops,
            key=lambda op: 0
            if op.operation_type is OperationType.GCP_UPDATE_WORKER_JOB_CONFIG
            else 1,
        )
        for op in ordered:
            is_job = op.operation_type is OperationType.GCP_UPDATE_WORKER_JOB_CONFIG
            observed_state = discovery.worker_state if is_job else discovery.api_state
            expected_sa = (
                cfg.worker_service_account if is_job else cfg.api_service_account
            )
            refs = WORKER_SECRET_REFS if is_job else API_SECRET_REFS
            name = cfg.cloud_run_worker_job if is_job else cfg.cloud_run_api_service
            if observed_state is None or redis is None:
                findings.append(
                    self._finding(
                        "CLOUD_RUN_PRESTATE_UNAVAILABLE",
                        f"no fresh state for {name}",
                        stage,
                    )
                )
                break
            desired_env = desired_cloud_run_env(observed_state, cfg, redis, refs)
            set_env = {
                var.name: var.value for var in desired_env if not var.is_secret_ref()
            }
            set_secret_refs = {
                var.name: f"{var.secret_name}:{var.secret_version}"
                for var in desired_env
                if var.is_secret_ref()
            }
            remove_env = tuple(
                var.name
                for container in observed_state.containers
                for var in container.env
                if var.name == "MILO_RELEASE_SHA"
            )
            outcome, detail = self.gcp.update_run_config(
                self.gate,
                op.operation_type,
                op.idempotency_key,
                op.resource,
                name,
                is_job,
                set_env,
                set_secret_refs,
                remove_env,
                expected_sa,
            )
            if outcome is not ProbeOutcome.PRESENT:
                findings.append(
                    self._finding(
                        "CLOUD_RUN_UPDATE_FAILED",
                        f"cloud run update of {name} failed: {detail}",
                        stage,
                    )
                )
                break
            probe, new_state = (
                self.gcp.describe_run_job(name)
                if is_job
                else self.gcp.describe_run_service(name)
            )
            if probe.outcome is not ProbeOutcome.PRESENT or new_state is None:
                self._verify_post_write(op, "unavailable", "post-update reread failed")
                findings.append(
                    self._finding(
                        "CLOUD_RUN_POST_UPDATE_UNAVAILABLE",
                        f"{name} could not be reread after update",
                        stage,
                    )
                )
                break
            if len(new_state.containers) != 1:
                self._verify_post_write(op, "ambiguous", "multiple containers after update")
                findings.append(
                    self._finding(
                        "CLOUD_RUN_POST_UPDATE_AMBIGUOUS",
                        f"{name} has ambiguous containers after update",
                        stage,
                    )
                )
                break
            observed_payload = _cloud_run_payload(
                new_state.service_account, new_state.containers[0].env
            )
            if not self._verify_post_write(op, state_digest(observed_payload)):
                findings.append(
                    self._finding(
                        "CLOUD_RUN_POST_UPDATE_MISMATCH",
                        f"{name} post-update state does not match the frozen intent",
                        stage,
                    )
                )
                break
            findings.extend(
                cloud_run_validators.validate_resource(
                    new_state,
                    expected_sa,
                    refs,
                    NUMERIC_PIN_REQUIRED_SECRETS,
                    (cfg.cloud_run_api_service, cfg.cloud_run_worker_job, "app"),
                    stage,
                    expected_bootstrap_sha=cfg.bootstrap_sha,
                )
            )
            if findings:
                break
        self._complete(self._stage_result(stage, tuple(findings)), stage)

    def _apply_stage_e(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.VERCEL_STAGE_VERIFIED
        findings: list[Finding] = []
        assert self.gate is not None
        cfg = self.config
        redis = discovery.redis_identity
        ops = self._plan_ops(OperationType.VERCEL_SET_ENV_VAR)
        if ops and redis is None:
            findings.append(
                self._finding(
                    "VERCEL_WITHOUT_REDIS_IDENTITY",
                    "vercel writes require a coherent redis identity",
                    stage,
                )
            )
            self._complete(self._stage_result(stage, tuple(findings)), stage)
            return
        if ops:
            values: dict[str, tuple[str, bool]] = {
                "GATEWAY_ALLOW_EXECUTION_ROUTES": ("false", False),
                "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI": ("false", False),
                "UPSTASH_REDIS_REST_URL": (redis.rest_url, False),
                "UPSTASH_REDIS_REST_TOKEN": (discovery.upstash_rest_token, True),
                "MILO_REDIS_TOKEN_FINGERPRINT": (
                    redis.token_fingerprint_sha256,
                    False,
                ),
            }
        for op in ops:
            key = op.resource.name
            value, secret = values[key]
            if not value:
                findings.append(
                    self._finding(
                        "VERCEL_VALUE_UNAVAILABLE",
                        f"no in-memory value available for {key}",
                        stage,
                    )
                )
                break
            outcome, detail = self.vercel.set_env_var(
                self.gate,
                op.idempotency_key,
                op.resource,
                cfg.vercel_project_id or cfg.vercel_project_name,
                key,
                value,
                secret,
            )
            if outcome is not ProbeOutcome.PRESENT:
                findings.append(
                    self._finding(
                        "VERCEL_WRITE_FAILED",
                        f"vercel write of {key} failed: {detail}; no later vercel "
                        "writes will run",
                        stage,
                    )
                )
                break
            env_probe = self.vercel.list_env(
                cfg.vercel_project_id or cfg.vercel_project_name
            )
            if env_probe.outcome is not ProbeOutcome.PRESENT:
                self._verify_post_write(op, "unavailable", "post-write env reread failed")
                findings.append(
                    self._finding(
                        "VERCEL_POST_WRITE_UNAVAILABLE",
                        f"could not reread env after writing {key}",
                        stage,
                    )
                )
                break
            observed_fp = ""
            for var in env_probe.env_vars:
                if var.key == key and "production" in var.target:
                    observed_fp = var.value_fingerprint_sha256
            if not self._verify_post_write(op, observed_fp):
                findings.append(
                    self._finding(
                        "VERCEL_POST_WRITE_MISMATCH",
                        f"{key} does not match the intended fingerprint after write",
                        stage,
                    )
                )
                break
        self._complete(self._stage_result(stage, tuple(findings)), stage)

    def _final_audit(self) -> DiscoveryOutput:
        audit = self._discover(strict=True)
        stage = Stage.FINAL_AUDIT_VERIFIED
        findings = list(audit.findings)
        self.reads.extend(audit.reads)
        if audit.world is not None:
            try:
                residual = build_plan(self.config, audit.world)
            except PlannerEvidenceError as exc:
                findings.append(
                    self._finding("FINAL_AUDIT_EVIDENCE", str(exc), stage)
                )
            else:
                if residual.operations:
                    findings.append(
                        self._finding(
                            "FINAL_AUDIT_RESIDUAL_DRIFT",
                            f"{len(residual.operations)} operations still required "
                            "after apply; live state does not match the frozen "
                            "intent",
                            stage,
                        )
                    )
        self._complete(
            self._stage_result(stage, tuple(findings), reads=tuple(audit.reads)),
            stage,
        )
        return audit

    def _commit_metadata(self, discovery: DiscoveryOutput) -> None:
        stage = Stage.METADATA_COMMITTED
        cfg = self.config
        redis = discovery.redis_identity
        assert redis is not None
        metadata = MetadataV3(
            MILO_METADATA_SCHEMA_VERSION=metadata_validators.SCHEMA_VERSION,
            MILO_BOOTSTRAP_STATUS="applied",
            MILO_ENVIRONMENT="production",
            MILO_BOOTSTRAP_SHA=cfg.bootstrap_sha,
            MILO_PLAN_DIGEST=self.digest,
            MILO_METADATA_GENERATED_AT=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            GITHUB_REPOSITORY=cfg.repository,
            GITHUB_RUN_ID=self.environ.get("GITHUB_RUN_ID", "local-run"),
            GITHUB_WORKFLOW_REF=self.environ.get("GITHUB_WORKFLOW_REF", "local-run"),
            GITHUB_HEAD_REF=self.environ.get("GITHUB_HEAD_REF", cfg.trusted_ref),
            GCP_PROJECT_ID=cfg.gcp_project_id,
            GCP_PROJECT_NUMBER=cfg.gcp_project_number,
            GCP_REGION=cfg.gcp_region,
            CLOUD_RUN_API_SERVICE=cfg.cloud_run_api_service,
            CLOUD_RUN_WORKER_JOB=cfg.cloud_run_worker_job,
            API_SERVICE_ACCOUNT=cfg.api_service_account,
            WORKER_SERVICE_ACCOUNT=cfg.worker_service_account,
            GATEWAY_IDENTITY=cfg.gateway_service_account,
            SUPABASE_PROJECT_REF=cfg.supabase_project_ref,
            VERCEL_PROJECT=cfg.vercel_project_name,
            VERCEL_PROJECT_ID=cfg.vercel_project_id,
            VERCEL_ORG_ID=cfg.vercel_org_id,
            PRODUCTION_ORIGIN=cfg.production_origin,
            MILO_REDIS_LOGICAL_ENVIRONMENT=redis.logical_environment,
            UPSTASH_REDIS_REST_URL=redis.rest_url,
            SUPABASE_URL_SECRET_NAME="SUPABASE_URL",
            SUPABASE_SERVICE_KEY_SECRET_NAME="SUPABASE_SECRET_KEY",
            PROVIDER_KEY_SECRET_NAME="KIMI_API_KEY",
            REDIS_TOKEN_SECRET_NAME="UPSTASH_REDIS_REST_TOKEN",
            MILO_REDIS_DB_ID=redis.database_id,
            MILO_REDIS_TOKEN_FINGERPRINT=redis.token_fingerprint_sha256,
            MILO_REDIS_SECRET_VERSION=redis.enabled_secret_version,
        )
        findings = list(metadata_validators.validate_metadata(metadata))
        if not findings:
            try:
                metadata_validators.write_metadata_atomically(
                    metadata, self.output_dir
                )
                self.metadata_status = MetadataStatus.COMMITTED
            except (OSError, ValueError) as exc:
                findings.append(
                    self._finding(
                        "METADATA_WRITE_FAILED",
                        f"metadata write failed: {exc.__class__.__name__}",
                        stage,
                    )
                )
        if findings:
            self.metadata_status = MetadataStatus.WITHHELD
        self._complete(self._stage_result(stage, tuple(findings)), stage)

    # --------------------------------------------------------------------- run

    def run(self) -> RunResult:
        try:
            return self._run_inner()
        except (StageBlocked, UndeclaredMutationError, MutationAfterBlockError, PlannerEvidenceError) as exc:
            if self.gate is not None:
                self.gate.close()
            if not self.machine.blocked and self.machine.current is not Stage.COMPLETE:
                self.machine.block()
            if isinstance(exc, StageBlocked):
                for reason in exc.reasons:
                    self.findings.append(
                        self._finding("STAGE_BLOCKED", reason, exc.stage)
                    )
            else:
                self.findings.append(
                    self._finding(
                        "RUN_ABORTED", str(exc), self.machine.last_completed_stage()
                    )
                )
            if self.mode is Mode.APPLY and self.ledger.executed_count():
                self.recovery_steps.append(
                    RecoveryStep(
                        order=len(self.recovery_steps) + 1,
                        description=(
                            "Run bootstrap v2 again from a clean checkout: fresh "
                            "discovery adopts partial state idempotently; no "
                            "cross-provider rollback is claimed"
                        ),
                    )
                )
            if self.mode is Mode.APPLY:
                self.metadata_status = MetadataStatus.WITHHELD
            return self._build_result()

    def _run_inner(self) -> RunResult:
        self._local_guard()

        discovery = self._discover(strict=self.mode is Mode.AUDIT)
        self.reads.extend(discovery.reads)
        self._complete(
            StageResult(
                stage=Stage.GLOBAL_DISCOVERY_COMPLETE,
                findings=tuple(discovery.findings),
                reads=tuple(discovery.reads),
            ),
            Stage.GLOBAL_DISCOVERY_COMPLETE,
        )

        assert discovery.world is not None
        self.plan = build_plan(self.config, discovery.world)
        self.digest = plan_digest(self.plan)
        self._complete(StageResult(stage=Stage.PLAN_FROZEN), Stage.PLAN_FROZEN)

        if self.mode is Mode.PLAN:
            self._write_plan_artifact()
            return self._build_result()

        if self.mode is Mode.AUDIT:
            if self.plan.operations:
                self.findings.append(
                    self._finding(
                        "AUDIT_DRIFT",
                        f"audit found {len(self.plan.operations)} operations of "
                        "drift from the intended state",
                        Stage.PLAN_FROZEN,
                    )
                )
                self.machine.block()
                return self._build_result()
            for stage in (
                Stage.APPLY_AUTHORIZED,
                Stage.UPSTASH_STAGE_VERIFIED,
                Stage.GCP_IDENTITY_SECRET_STAGE_VERIFIED,
                Stage.IAM_STAGE_VERIFIED,
                Stage.CLOUD_RUN_STAGE_VERIFIED,
                Stage.VERCEL_STAGE_VERIFIED,
                Stage.FINAL_AUDIT_VERIFIED,
                Stage.METADATA_COMMITTED,
                Stage.COMPLETE,
            ):
                self._complete(StageResult(stage=stage), stage)
            return self._build_result()

        # APPLY: rerun discovery, regenerate the plan, compare digests.
        rediscovery = self._discover(strict=False)
        self.reads.extend(rediscovery.reads)
        auth_findings = list(rediscovery.findings)
        assert rediscovery.world is not None
        regenerated = build_plan(self.config, rediscovery.world)
        regenerated_digest = plan_digest(regenerated)
        if regenerated_digest != self.digest:
            auth_findings.append(
                self._finding(
                    "PLAN_DIGEST_DRIFT",
                    "regenerated plan digest does not match the frozen digest; "
                    "live state drifted between discovery and apply",
                    Stage.APPLY_AUTHORIZED,
                )
            )
        if (
            self.approved_plan_digest
            and self.approved_plan_digest != self.digest
        ):
            auth_findings.append(
                self._finding(
                    "PLAN_DIGEST_NOT_APPROVED",
                    "frozen plan digest does not match the operator-approved "
                    "MILO_PLAN_DIGEST",
                    Stage.APPLY_AUTHORIZED,
                )
            )
        self.plan = regenerated
        self.digest = regenerated_digest
        self.gate = MutationGate(self.plan, self.ledger)
        self._complete(
            StageResult(
                stage=Stage.APPLY_AUTHORIZED,
                findings=tuple(auth_findings),
                reads=tuple(rediscovery.reads),
            ),
            Stage.APPLY_AUTHORIZED,
        )

        self._apply_stage_a(rediscovery)
        self._apply_stage_b(rediscovery)
        self._apply_stage_c(rediscovery)
        self._apply_stage_d(rediscovery)
        self._apply_stage_e(rediscovery)

        final = self._final_audit()
        self._commit_metadata(final)
        self._complete(StageResult(stage=Stage.COMPLETE), Stage.COMPLETE)
        return self._build_result()

    def _write_plan_artifact(self) -> None:
        assert self.plan is not None
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.output_dir, 0o700)
        plan_path = self.output_dir / "bootstrap-v2-plan.json"
        fd = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(plan_to_canonical_json(self.plan) + "\n")

    def _build_result(self) -> RunResult:
        return build_run_result(
            mode=self.mode,
            starting_sha=self.config.bootstrap_sha,
            trusted_ref=self.config.trusted_ref,
            plan_digest=self.digest,
            last_completed_stage=self.machine.last_completed_stage(),
            findings=tuple(self.findings),
            reads=tuple(self.reads),
            mutations=self.ledger.records(),
            verifications=tuple(self.verifications),
            created_resources=tuple(self.created_resources),
            recovery_steps=tuple(self.recovery_steps),
            metadata_status=self.metadata_status,
        )


def _intended_secret_accessors_engine(
    secret_name: str, config: BootstrapConfig
) -> tuple[str, ...]:
    from .planner import _intended_secret_accessors

    return _intended_secret_accessors(secret_name, config)


# ---------------------------------------------------------------------------
# Local guard adapter (real implementation)
# ---------------------------------------------------------------------------


class LocalGuardAdapter:
    """Discovers local identity via git and non-mutating tool checks."""

    def __init__(self, repo_root: Path, config: BootstrapConfig, runner: SubprocessRunner | None = None) -> None:
        self._root = repo_root
        self._config = config
        self._runner = runner or SubprocessRunner(SecretRegistry())

    def _git(self, *args: str) -> str:
        result = self._runner.run(("git", "-C", str(self._root)) + args)
        return result.stdout.strip() if result.returncode == 0 else ""

    def discover(self):
        from .model import LocalIdentityState

        status = self._runner.run(
            ("git", "-C", str(self._root), "status", "--porcelain")
        )
        head = self._git("rev-parse", "HEAD")
        ref = self._git("rev-parse", "--abbrev-ref", "HEAD")
        remote = self._git("remote", "get-url", "origin")
        repository = ""
        if remote:
            trimmed = remote.removesuffix(".git")
            parts = trimmed.replace(":", "/").split("/")
            if len(parts) >= 2:
                repository = "/".join(parts[-2:])
        python_ok = sys.version_info >= (3, 11)
        tooling_ok = all(
            self._runner.run((tool, "--version")).returncode == 0
            for tool in ("git",)
        )
        dotenv = tuple(
            str(path.relative_to(self._root))
            for path in (
                self._root / ".env",
                self._root / ".env.local",
                self._root / "frontend" / ".env",
                self._root / "frontend" / ".env.local",
            )
            if path.exists()
        )
        deprecated = tuple(
            name for name in ("MILO_RELEASE_SHA",) if name in os.environ
        )
        return LocalIdentityState(
            repository=repository,
            head_sha=head,
            ref=ref,
            worktree_clean=status.returncode == 0 and not status.stdout.strip(),
            environment=os.environ.get("MILO_ENVIRONMENT", "production"),
            operator_ack=os.environ.get("MILO_OPERATOR_ACK", ""),
            python_ok=python_ok,
            tooling_ok=tooling_ok,
            dotenv_influence=dotenv,
            deprecated_metadata_keys=deprecated,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap-production-v2",
        description=(
            "MILO transactional production bootstrap v2. "
            "Usage: bootstrap-production-v2.sh --mode plan|apply|audit "
            "--bootstrap-sha <full-sha> --output-dir <private-dir> [options]"
        ),
    )
    parser.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    parser.add_argument("--bootstrap-sha", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--gcp-project-number", default="")
    parser.add_argument("--vercel-project-id", default="")
    parser.add_argument("--vercel-org-id", default="")
    parser.add_argument("--supabase-project-ref", default="")
    parser.add_argument("--production-origin", default="")
    parser.add_argument("--upstash-database-id", default="")
    parser.add_argument("--approved-plan-digest", default="")
    parser.add_argument(
        "--trusted-ref",
        default="claude/production-readiness-j0hhni",
        help="the only ref apply mode accepts as local HEAD",
    )
    parser.add_argument(
        "--confirm-production-change",
        action="store_true",
        help="required for apply mode alongside MILO_OPERATOR_ACK",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    mode = Mode(args.mode)

    if mode is Mode.APPLY and not args.confirm_production_change:
        print(
            "BLOCKED: apply mode requires --confirm-production-change",
            file=sys.stderr,
        )
        return 1

    config = BootstrapConfig(
        bootstrap_sha=args.bootstrap_sha,
        gcp_project_number=args.gcp_project_number,
        vercel_project_id=args.vercel_project_id,
        vercel_org_id=args.vercel_org_id,
        supabase_project_ref=args.supabase_project_ref,
        production_origin=args.production_origin,
        upstash_database_id=args.upstash_database_id,
        trusted_ref=args.trusted_ref,
        operator_ack=os.environ.get("MILO_OPERATOR_ACK", ""),
    )

    from .adapters.gcp import GcpAdapter
    from .adapters.upstash import UpstashAdapter
    from .adapters.vercel import VercelAdapter

    secrets = SecretRegistry()
    runner = SubprocessRunner(secrets)
    repo_root = Path(__file__).resolve().parents[3]

    upstash_email = os.environ.get("MILO_UPSTASH_EMAIL", "") or os.environ.get(
        "UPSTASH_EMAIL", ""
    )
    upstash_key = os.environ.get("MILO_UPSTASH_APIKEY", "") or os.environ.get(
        "UPSTASH_API_KEY", ""
    )
    vercel_token = os.environ.get("VERCEL_TOKEN", "")

    engine = BootstrapEngine(
        config=config,
        mode=mode,
        local_port=LocalGuardAdapter(repo_root, config, runner),
        gcp_port=GcpAdapter(
            config.gcp_project_id, config.gcp_region, runner, secrets
        ),
        upstash_port=UpstashAdapter(upstash_email, upstash_key, secrets=secrets),
        vercel_port=VercelAdapter(
            vercel_token, config.vercel_org_id, secrets=secrets
        ),
        output_dir=Path(args.output_dir),
        approved_plan_digest=args.approved_plan_digest,
    )
    try:
        result = engine.run()
    finally:
        secrets.clear()
    write_json_report(result, Path(args.output_dir))
    sys.stdout.write(render_human_summary(result))
    return result.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
