"""Strict provider fakes for bootstrap v2 tests.

Every fake:
- rejects unknown operations and unknown resources loudly;
- distinguishes reads from writes and records each call with a global
  sequence number, provider and resource identity;
- models pre-state and post-state separately (writes mutate held state);
- can fail before mutation, report success without applying, return
  malformed post-state, and simulate concurrent drift — all keyed by an
  injectable ``faults`` mapping of call label -> fault mode;
- never stores actual production credentials (all values are synthetic).

Fault modes: ``permission_denied``, ``auth_failure``, ``network``,
``malformed``, ``timeout``, ``rate_limited``, ``fail_before_mutation``,
``report_success_no_apply``, ``apply_wrong_state``, ``concurrent_drift``.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))

from bootstrap_v2.adapters.gcp import GcpProbe  # noqa: E402
from bootstrap_v2.adapters.upstash import ProbeResult  # noqa: E402
from bootstrap_v2.adapters.vercel import EnvProbe, ProjectProbe  # noqa: E402
from bootstrap_v2.model import (  # noqa: E402
    CloudRunContainerState,
    CloudRunEnvVar,
    CloudRunResourceState,
    GcpServiceAccountState,
    IamBinding,
    IamPolicyState,
    LocalIdentityState,
    OperationType,
    ProbeOutcome,
    Provider,
    ResourceIdentity,
    SecretState,
    UpstashDatabaseState,
    VercelEnvVarState,
    VercelProjectState,
    WifState,
)
from bootstrap_v2.policy import (  # noqa: E402
    BootstrapConfig,
    OPERATOR_ACK_EXPECTED,
    REQUIRED_GCP_APIS,
    SECRET_MANAGER_RESOURCE_NAMES,
    VERCEL_REUSED_VARS,
)

BOOTSTRAP_SHA = "1f" * 20
PROJECT_NUMBER = "123456789012"
VERCEL_PROJECT_ID = "prj_abc123def456"
VERCEL_ORG_ID = "team_xyz789"
DB_ID = "db-11111111-2222-3333-4444-555555555555"
DB_ENDPOINT = "milo-production-99999.upstash.io"
REDIS_TOKEN = "fake-upstash-rest-token-not-a-real-credential"
REDIS_FP = hashlib.sha256(REDIS_TOKEN.encode()).hexdigest()

_FAULT_OUTCOMES = {
    "permission_denied": ProbeOutcome.PERMISSION_DENIED,
    "auth_failure": ProbeOutcome.AUTH_FAILURE,
    "network": ProbeOutcome.NETWORK_FAILURE,
    "malformed": ProbeOutcome.MALFORMED_OUTPUT,
    "timeout": ProbeOutcome.TIMEOUT,
    "rate_limited": ProbeOutcome.RATE_LIMITED,
    "api_disabled": ProbeOutcome.API_DISABLED,
}


@dataclass
class CallRecord:
    sequence: int
    provider: str
    kind: str  # "read" | "write"
    operation: str
    resource: str


class CallLog:
    """Shared cross-provider call log with one global sequence."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []
        self._seq = 0
        self.counts: dict[str, int] = {}

    def record(self, provider: str, kind: str, operation: str, resource: str) -> int:
        self._seq += 1
        self.records.append(
            CallRecord(self._seq, provider, kind, operation, resource)
        )
        self.counts[operation] = self.counts.get(operation, 0) + 1
        return self._seq

    def writes(self) -> list[CallRecord]:
        return [r for r in self.records if r.kind == "write"]


def resolve_fault(faults: dict[str, str], log: CallLog, label: str) -> str | None:
    """Resolve a fault for this invocation of ``label``.

    Plain key ``label`` fires on every call; ``label#N`` fires only on the
    N-th call of that label (1-based), enabling e.g. final-audit-only read
    failures.
    """

    if label in faults:
        return faults[label]
    call_number = log.counts.get(label, 0)
    counted = f"{label}#{call_number}"
    return faults.get(counted)


def make_config(**overrides) -> BootstrapConfig:
    values = dict(
        bootstrap_sha=BOOTSTRAP_SHA,
        gcp_project_number=PROJECT_NUMBER,
        vercel_project_id=VERCEL_PROJECT_ID,
        vercel_org_id=VERCEL_ORG_ID,
        supabase_project_ref="abcdefghijklmnopqrst",
        production_origin="https://milo-agent-workspace.vercel.app",
        upstash_database_id="",
        operator_ack=OPERATOR_ACK_EXPECTED,
    )
    values.update(overrides)
    return BootstrapConfig(**values)


def principal_set(config: BootstrapConfig) -> str:
    return (
        "principalSet://iam.googleapis.com/projects/"
        f"{config.gcp_project_number}/locations/global/workloadIdentityPools/"
        f"{config.wif_pool_id}/attribute.project/{config.vercel_project_id}"
    )


def intended_accessors(config: BootstrapConfig) -> dict[str, tuple[str, ...]]:
    api = f"serviceAccount:{config.api_service_account}"
    worker = f"serviceAccount:{config.worker_service_account}"
    return {
        "SUPABASE_URL": (api, worker),
        "SUPABASE_SECRET_KEY": (api, worker),
        "KIMI_API_KEY": (worker,),
        "UPSTASH_REDIS_REST_TOKEN": (api, worker),
    }


def desired_plain_env(config: BootstrapConfig, redis_version: str = "7") -> dict[str, str]:
    env = {
        "ENVIRONMENT": "production",
        "ALLOWED_CORS_ORIGINS": "https://milo-agent-workspace.vercel.app",
        "MILO_GATEWAY_AUDIENCE": "https://milo-agent-api.example-audience",
        "MILO_APPROVED_GATEWAY_IDENTITIES": config.gateway_service_account,
        "MILO_APPROVED_WORKER_IDENTITIES": config.worker_service_account,
        "MILO_WORKER_AUDIENCE": "https://milo-agent-worker.example-audience",
        "JOB_LAUNCHER": "disabled",
        "GATEWAY_ALLOW_EXECUTION_ROUTES": "false",
        "UPSTASH_REDIS_REST_URL": f"https://{DB_ENDPOINT}",
        "MILO_REDIS_DB_ID": DB_ID,
        "MILO_REDIS_TOKEN_FINGERPRINT": REDIS_FP,
        "MILO_REDIS_SECRET_VERSION": redis_version,
        "MILO_BOOTSTRAP_SHA": config.bootstrap_sha,
        "MILO_ENABLE_RUN_CREATION": "false",
        "MILO_ENABLE_PROPOSAL_MUTATIONS": "false",
        "MILO_ENABLE_PROPOSAL_READS": "false",
        "MILO_ENABLE_RUN_CANCELLATION": "false",
        "MILO_ENABLE_EXECUTION_CONTROL": "false",
        "MILO_ENABLE_PAID_EXECUTION": "false",
        "MILO_MAX_COST_PER_RUN": "2.5",
        "MILO_DAILY_USER_BUDGET": "10",
        "MILO_DAILY_PROJECT_BUDGET": "50",
        "MILO_MAX_MODEL_CALLS_PER_RUN": "100",
        "MILO_MAX_TOTAL_TOKENS_PER_RUN": "500000",
        "MILO_MAX_RUN_DURATION_SECONDS": "1800",
    }
    return env


def make_cloud_run_state(
    config: BootstrapConfig,
    is_job: bool,
    redis_version: str = "7",
    plain_overrides: dict[str, str] | None = None,
    extra_env: tuple[CloudRunEnvVar, ...] = (),
) -> CloudRunResourceState:
    name = config.cloud_run_worker_job if is_job else config.cloud_run_api_service
    plain = desired_plain_env(config, redis_version)
    if plain_overrides:
        plain.update(plain_overrides)
    env = [CloudRunEnvVar(name=k, value=v) for k, v in plain.items()]
    refs = {
        "SUPABASE_URL": ("SUPABASE_URL", "1"),
        "SUPABASE_SECRET_KEY": ("SUPABASE_SECRET_KEY", "1"),
        "UPSTASH_REDIS_REST_TOKEN": ("UPSTASH_REDIS_REST_TOKEN", redis_version),
    }
    if is_job:
        refs["KIMI_API_KEY"] = ("KIMI_API_KEY", "1")
    for env_name, (secret, version) in refs.items():
        env.append(
            CloudRunEnvVar(name=env_name, secret_name=secret, secret_version=version)
        )
    env.extend(extra_env)
    kind = "cloud_run_worker_job" if is_job else "cloud_run_api_service"
    sa = config.worker_service_account if is_job else config.api_service_account
    return CloudRunResourceState(
        resource=ResourceIdentity(
            provider=Provider.GCP, kind=kind, name=name, scope=config.gcp_project_id
        ),
        service_account=sa,
        containers=(
            CloudRunContainerState(name=name, image="registry.example/app@sha256:abc", env=tuple(env)),
        ),
        ingress="internal",
        allows_unauthenticated=False,
    )


class FakeLocal:
    def __init__(self, config: BootstrapConfig, **overrides) -> None:
        values = dict(
            repository=config.repository,
            head_sha=config.bootstrap_sha,
            ref=config.trusted_ref,
            worktree_clean=True,
            environment="production",
            operator_ack=OPERATOR_ACK_EXPECTED,
            python_ok=True,
            tooling_ok=True,
            dotenv_influence=(),
            deprecated_metadata_keys=(),
        )
        values.update(overrides)
        self._state = LocalIdentityState(**values)

    def discover(self) -> LocalIdentityState:
        return self._state


class FakeUpstash:
    def __init__(
        self,
        log: CallLog,
        databases: dict[str, UpstashDatabaseState] | None = None,
        tokens: dict[str, str] | None = None,
        faults: dict[str, str] | None = None,
    ) -> None:
        self.log = log
        self.databases = dict(databases or {})
        self.tokens = dict(tokens or {})
        self.faults = dict(faults or {})

    def _fault(self, label: str) -> ProbeOutcome | None:
        mode = resolve_fault(self.faults, self.log, label)
        if mode in _FAULT_OUTCOMES:
            return _FAULT_OUTCOMES[mode]
        return None

    def list_databases(self) -> ProbeResult:
        self.log.record("upstash", "read", "list_databases", "*")
        fault = self._fault("list_databases")
        if fault:
            return ProbeResult(outcome=fault, detail="injected fault")
        return ProbeResult(
            outcome=ProbeOutcome.PRESENT, databases=tuple(self.databases.values())
        )

    def get_database(self, database_id: str) -> tuple[ProbeResult, str]:
        self.log.record("upstash", "read", "get_database", database_id)
        fault = self._fault("get_database")
        if fault:
            return ProbeResult(outcome=fault, detail="injected fault"), ""
        db = self.databases.get(database_id)
        if db is None:
            return ProbeResult(outcome=ProbeOutcome.CLEANLY_ABSENT), ""
        return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=(db,)), self.tokens.get(
            database_id, ""
        )

    def create_database(self, gate, idempotency_key, resource, name, region):
        self.log.record("upstash", "write", "create_database", name)
        operation = gate.authorize(
            OperationType.UPSTASH_CREATE_DATABASE, resource, idempotency_key
        )
        mode = self.faults.get("create_database")
        if mode == "fail_before_mutation":
            gate.record_execution(operation, succeeded=False, error_class="network_failure")
            return ProbeResult(outcome=ProbeOutcome.NETWORK_FAILURE, detail="injected"), ""
        token = REDIS_TOKEN
        state = UpstashDatabaseState(
            database_id=DB_ID,
            name=name,
            state="active",
            tls=True,
            region=region,
            endpoint=DB_ENDPOINT,
            rest_url=f"https://{DB_ENDPOINT}",
            token_fingerprint_sha256=hashlib.sha256(token.encode()).hexdigest(),
        )
        if mode == "report_success_no_apply":
            gate.record_execution(operation, succeeded=True)
            return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=(state,)), DB_ID
        if mode == "apply_wrong_state":
            state = replace(state, tls=False)
        if mode == "apply_wrong_region":
            state = replace(state, region="eu-west-1")
        self.databases[DB_ID] = state
        self.tokens[DB_ID] = token
        gate.record_execution(operation, succeeded=True)
        return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=(state,)), DB_ID


class FakeGcp:
    def __init__(
        self,
        log: CallLog,
        config: BootstrapConfig,
        service_accounts: dict[str, GcpServiceAccountState],
        secrets: dict[str, SecretState],
        payloads: dict[tuple[str, str], str],
        secret_policies: dict[str, IamPolicyState],
        gateway_policy: IamPolicyState,
        invoker_policy: IamPolicyState,
        wif: WifState | None,
        worker: CloudRunResourceState | None,
        api: CloudRunResourceState | None,
        enabled_apis: tuple[str, ...] = tuple(REQUIRED_GCP_APIS),
        faults: dict[str, str] | None = None,
    ) -> None:
        self.log = log
        self.config = config
        self.service_accounts = dict(service_accounts)
        self.secrets = dict(secrets)
        self.payloads = dict(payloads)
        self.secret_policies = dict(secret_policies)
        self.gateway_policy = gateway_policy
        self.invoker_policy = invoker_policy
        self.wif = wif
        self.worker = worker
        self.api = api
        self.enabled_apis = enabled_apis
        self.faults = dict(faults or {})

    def _fault_probe(self, label: str) -> GcpProbe | None:
        mode = resolve_fault(self.faults, self.log, label)
        if mode in _FAULT_OUTCOMES:
            return GcpProbe(outcome=_FAULT_OUTCOMES[mode], detail="injected fault")
        return None

    # ---- reads ----------------------------------------------------------

    def list_enabled_services(self):
        self.log.record("gcp", "read", "list_enabled_services", "*")
        fault = self._fault_probe("list_enabled_services")
        if fault:
            return fault, ()
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.enabled_apis

    def describe_service_account(self, email: str):
        assert email.endswith(".iam.gserviceaccount.com"), f"unknown resource {email}"
        self.log.record("gcp", "read", "describe_service_account", email)
        fault = self._fault_probe(f"describe_service_account:{email}") or self._fault_probe(
            "describe_service_account"
        )
        if fault:
            return fault, None
        state = self.service_accounts.get(email)
        if state is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), state

    def describe_secret(self, name: str):
        assert name in SECRET_MANAGER_RESOURCE_NAMES, f"unknown secret {name}"
        self.log.record("gcp", "read", "describe_secret", name)
        fault = self._fault_probe(f"describe_secret:{name}") or self._fault_probe(
            "describe_secret"
        )
        if fault:
            return fault, None
        state = self.secrets.get(name)
        if state is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), state

    def access_secret_payload(self, name: str, version: str):
        self.log.record("gcp", "read", "access_secret_payload", f"{name}:{version}")
        fault = self._fault_probe("access_secret_payload")
        if fault:
            return fault, ""
        payload = self.payloads.get((name, version))
        if payload is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), ""
        return GcpProbe(outcome=ProbeOutcome.PRESENT), payload

    def get_secret_iam(self, name: str):
        self.log.record("gcp", "read", "get_secret_iam", name)
        fault = self._fault_probe(f"get_secret_iam:{name}") or self._fault_probe(
            "get_secret_iam"
        )
        if fault:
            return fault, None
        policy = self.secret_policies.get(name)
        if policy is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), policy

    def get_service_account_iam(self, email: str):
        self.log.record("gcp", "read", "get_service_account_iam", email)
        fault = self._fault_probe("get_service_account_iam")
        if fault:
            return fault, None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.gateway_policy

    def get_run_invoker_iam(self, service: str):
        self.log.record("gcp", "read", "get_run_invoker_iam", service)
        fault = self._fault_probe("get_run_invoker_iam")
        if fault:
            return fault, None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.invoker_policy

    def describe_wif(self, pool_id: str, provider_id: str, project_number: str):
        self.log.record("gcp", "read", "describe_wif", f"{pool_id}/{provider_id}")
        fault = self._fault_probe("describe_wif")
        if fault:
            return fault, None
        if self.wif is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.wif

    def describe_run_job(self, name: str):
        self.log.record("gcp", "read", "describe_run_job", name)
        fault = self._fault_probe("describe_run_job")
        if fault:
            return fault, None
        if self.worker is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.worker

    def describe_run_service(self, name: str):
        self.log.record("gcp", "read", "describe_run_service", name)
        fault = self._fault_probe("describe_run_service")
        if fault:
            return fault, None
        if self.api is None:
            return GcpProbe(outcome=ProbeOutcome.CLEANLY_ABSENT), None
        return GcpProbe(outcome=ProbeOutcome.PRESENT), self.api

    # ---- writes ---------------------------------------------------------

    def _write(self, label: str, gate, op_type, resource, key, apply_fn):
        self.log.record("gcp", "write", label, resource.name)
        operation = gate.authorize(op_type, resource, key)
        mode = self.faults.get(label) or self.faults.get(f"{label}:{resource.name}")
        if mode == "fail_before_mutation":
            gate.record_execution(operation, succeeded=False, error_class="permission_denied")
            return ProbeOutcome.PERMISSION_DENIED, "injected"
        if mode != "report_success_no_apply":
            apply_fn(wrong=(mode == "apply_wrong_state"))
        gate.record_execution(operation, succeeded=True)
        return ProbeOutcome.PRESENT, ""

    def create_service_account(self, gate, key, resource, email):
        def apply_fn(wrong: bool) -> None:
            self.service_accounts[email] = GcpServiceAccountState(
                email=email, exists=True, disabled=wrong
            )

        return self._write(
            "create_service_account", gate,
            OperationType.GCP_CREATE_SERVICE_ACCOUNT, resource, key, apply_fn,
        )

    def create_secret(self, gate, key, resource, name):
        def apply_fn(wrong: bool) -> None:
            self.secrets[name] = SecretState(name=name, exists=not wrong)
            self.secret_policies.setdefault(
                name,
                IamPolicyState(
                    resource=ResourceIdentity(
                        provider=Provider.GCP,
                        kind="secret_iam_policy",
                        name=name,
                        scope=self.config.gcp_project_id,
                    ),
                    etag="etag-new",
                ),
            )

        return self._write(
            "create_secret", gate, OperationType.GCP_CREATE_SECRET, resource, key, apply_fn
        )

    def add_secret_version(self, gate, key, resource, name, payload):
        def apply_fn(wrong: bool) -> None:
            # Real GCP numbers versions sequentially across ALL states
            # (enabled, disabled, destroyed): the next version is
            # highest_version + 1, never latest_enabled + 1.
            current = self.secrets[name]
            next_version = str(
                int(current.highest_version) + 1 if current.highest_version else 1
            )
            self.secrets[name] = SecretState(
                name=name,
                exists=True,
                enabled_versions=current.enabled_versions + (next_version,),
                latest_enabled_version=next_version,
                highest_version=next_version,
            )
            self.payloads[(name, next_version)] = (
                "wrong-payload" if wrong else payload
            )

        return self._write(
            "add_secret_version", gate,
            OperationType.GCP_ADD_SECRET_VERSION, resource, key, apply_fn,
        )

    def set_secret_iam(self, gate, key, resource, name, policy, members):
        def apply_fn(wrong: bool) -> None:
            kept = tuple(
                b for b in self.secret_policies[name].bindings
                if b.role != "roles/secretmanager.secretAccessor"
            )
            new_members = members if not wrong else members + ("serviceAccount:intruder@x.iam.gserviceaccount.com",)
            self.secret_policies[name] = IamPolicyState(
                resource=policy.resource,
                etag="etag-bumped",
                bindings=kept
                + (
                    IamBinding(
                        role="roles/secretmanager.secretAccessor",
                        members=tuple(sorted(new_members)),
                    ),
                ),
            )

        return self._write(
            "set_secret_iam", gate, OperationType.GCP_SET_SECRET_IAM, resource, key, apply_fn
        )

    def set_gateway_wif_iam(self, gate, key, resource, email, policy, members):
        def apply_fn(wrong: bool) -> None:
            self.gateway_policy = IamPolicyState(
                resource=policy.resource,
                etag="etag-bumped",
                bindings=(
                    IamBinding(
                        role="roles/iam.workloadIdentityUser",
                        members=tuple(sorted(members)) if not wrong else ("allUsers",),
                    ),
                ),
            )

        return self._write(
            "set_gateway_wif_iam", gate, OperationType.GCP_SET_WIF_IAM, resource, key, apply_fn
        )

    def set_run_invoker_iam(self, gate, key, resource, service, policy, members):
        def apply_fn(wrong: bool) -> None:
            self.invoker_policy = IamPolicyState(
                resource=policy.resource,
                etag="etag-bumped",
                bindings=(
                    IamBinding(
                        role="roles/run.invoker",
                        members=tuple(sorted(members)),
                    ),
                ),
            )

        return self._write(
            "set_run_invoker_iam", gate,
            OperationType.GCP_SET_RUN_INVOKER_IAM, resource, key, apply_fn,
        )

    def update_run_config(
        self, gate, op_type, key, resource, name, is_job,
        set_env, set_secret_refs, remove_env, service_account,
    ):
        label = "update_run_job" if is_job else "update_run_service"

        def apply_fn(wrong: bool) -> None:
            target = self.worker if is_job else self.api
            assert target is not None
            env: dict[str, CloudRunEnvVar] = {
                var.name: var
                for container in target.containers
                for var in container.env
                if var.name not in remove_env
            }
            for env_name, value in set_env.items():
                env[env_name] = CloudRunEnvVar(name=env_name, value=value)
            for env_name, ref in set_secret_refs.items():
                secret, _, version = ref.partition(":")
                env[env_name] = CloudRunEnvVar(
                    name=env_name, secret_name=secret, secret_version=version
                )
            if wrong:
                env["MILO_ENABLE_PAID_EXECUTION"] = CloudRunEnvVar(
                    name="MILO_ENABLE_PAID_EXECUTION", value="true"
                )
            new_state = CloudRunResourceState(
                resource=target.resource,
                service_account=service_account,
                containers=(
                    CloudRunContainerState(
                        name=target.containers[0].name,
                        image=target.containers[0].image,
                        env=tuple(sorted(env.values(), key=lambda v: v.name)),
                    ),
                ),
                ingress=target.ingress,
                allows_unauthenticated=False,
            )
            if is_job:
                self.worker = new_state
            else:
                self.api = new_state

        return self._write(label, gate, op_type, resource, key, apply_fn)


class FakeVercel:
    def __init__(
        self,
        log: CallLog,
        project: VercelProjectState,
        env_values: dict[str, str],
        faults: dict[str, str] | None = None,
    ) -> None:
        self.log = log
        self.project = project
        self.env_values = dict(env_values)  # key -> plaintext value
        self.faults = dict(faults or {})

    def _fault(self, label: str) -> ProbeOutcome | None:
        mode = resolve_fault(self.faults, self.log, label)
        if mode in _FAULT_OUTCOMES:
            return _FAULT_OUTCOMES[mode]
        return None

    def get_project(self, project_id: str) -> ProjectProbe:
        self.log.record("vercel", "read", "get_project", project_id)
        fault = self._fault("get_project")
        if fault:
            return ProjectProbe(outcome=fault, detail="injected fault")
        return ProjectProbe(outcome=ProbeOutcome.PRESENT, project=self.project)

    def list_env(self, project_id: str) -> EnvProbe:
        self.log.record("vercel", "read", "list_env", project_id)
        fault = self._fault("list_env")
        if fault:
            return EnvProbe(outcome=fault, detail="injected fault")
        env_vars = tuple(
            VercelEnvVarState(
                key=key,
                target=("production",),
                value_fingerprint_sha256=hashlib.sha256(value.encode()).hexdigest(),
                env_var_id=f"env_{index}",
            )
            for index, (key, value) in enumerate(sorted(self.env_values.items()))
        )
        return EnvProbe(outcome=ProbeOutcome.PRESENT, env_vars=env_vars)

    def set_env_var(self, gate, key, resource, project_id, name, value, secret):
        self.log.record("vercel", "write", "set_env_var", name)
        operation = gate.authorize(OperationType.VERCEL_SET_ENV_VAR, resource, key)
        mode = self.faults.get(f"set_env_var:{name}") or self.faults.get("set_env_var")
        if mode == "fail_before_mutation":
            gate.record_execution(operation, succeeded=False, error_class="network_failure")
            return ProbeOutcome.NETWORK_FAILURE, "injected"
        if mode != "report_success_no_apply":
            self.env_values[name] = value if mode != "apply_wrong_state" else "tampered"
        gate.record_execution(operation, succeeded=True)
        return ProbeOutcome.PRESENT, ""


@dataclass
class FakeWorld:
    config: BootstrapConfig
    log: CallLog
    local: FakeLocal
    gcp: FakeGcp
    upstash: FakeUpstash
    vercel: FakeVercel


def make_happy_world(
    config: BootstrapConfig | None = None,
    upstash_faults: dict[str, str] | None = None,
    gcp_faults: dict[str, str] | None = None,
    vercel_faults: dict[str, str] | None = None,
    database_absent: bool = False,
    drift: dict[str, object] | None = None,
) -> FakeWorld:
    """A fully consistent production world already in the desired state.

    ``drift`` toggles: ``worker_flag_true``, ``vercel_managed_missing``,
    ``sa_absent``, ``secret_iam_missing_member``, ``secret_version_stale``.
    """

    config = config or make_config()
    drift = drift or {}
    log = CallLog()

    db = UpstashDatabaseState(
        database_id=DB_ID,
        name=config.upstash_database_name,
        state="active",
        tls=True,
        region=config.gcp_region,
        endpoint=DB_ENDPOINT,
        rest_url=f"https://{DB_ENDPOINT}",
        token_fingerprint_sha256=REDIS_FP,
    )
    databases = {} if database_absent else {DB_ID: db}
    tokens = {} if database_absent else {DB_ID: REDIS_TOKEN}

    sas = {
        email: GcpServiceAccountState(email=email, exists=True)
        for email in (
            config.api_service_account,
            config.worker_service_account,
            config.gateway_service_account,
        )
    }
    if drift.get("sa_absent"):
        del sas[config.worker_service_account]

    redis_payload = REDIS_TOKEN
    redis_version = "7"
    if drift.get("secret_version_stale"):
        redis_payload = "stale-old-token-value"
    secrets = {
        name: SecretState(
            name=name,
            exists=True,
            enabled_versions=("7",) if name == "UPSTASH_REDIS_REST_TOKEN" else ("1",),
            latest_enabled_version="7" if name == "UPSTASH_REDIS_REST_TOKEN" else "1",
            highest_version="7" if name == "UPSTASH_REDIS_REST_TOKEN" else "1",
        )
        for name in SECRET_MANAGER_RESOURCE_NAMES
    }
    payloads = {("UPSTASH_REDIS_REST_TOKEN", "7"): redis_payload}

    accessors = intended_accessors(config)
    secret_policies = {}
    for name in SECRET_MANAGER_RESOURCE_NAMES:
        members = accessors[name]
        if drift.get("secret_iam_missing_member") and name == "KIMI_API_KEY":
            members = ()
        bindings = (
            (
                IamBinding(
                    role="roles/secretmanager.secretAccessor",
                    members=tuple(sorted(members)),
                ),
            )
            if members
            else ()
        )
        secret_policies[name] = IamPolicyState(
            resource=ResourceIdentity(
                provider=Provider.GCP,
                kind="secret_iam_policy",
                name=name,
                scope=config.gcp_project_id,
            ),
            etag=f"etag-{name}",
            bindings=bindings,
        )

    gateway_policy = IamPolicyState(
        resource=ResourceIdentity(
            provider=Provider.GCP,
            kind="service_account_iam_policy",
            name=config.gateway_service_account,
            scope=config.gcp_project_id,
        ),
        etag="etag-gw",
        bindings=(
            IamBinding(
                role="roles/iam.workloadIdentityUser",
                members=(principal_set(config),),
            ),
        ),
    )
    invoker_policy = IamPolicyState(
        resource=ResourceIdentity(
            provider=Provider.GCP,
            kind="run_invoker_policy",
            name=config.cloud_run_api_service,
            scope=config.gcp_project_id,
        ),
        etag="etag-invoker",
        bindings=(
            IamBinding(
                role="roles/run.invoker",
                members=(f"serviceAccount:{config.gateway_service_account}",),
            ),
        ),
    )
    wif = WifState(
        pool_id=config.wif_pool_id,
        provider_id=config.wif_provider_id,
        issuer_uri=config.wif_issuer,
        allowed_audiences=(config.wif_allowed_audience,),
        attribute_mapping=(("google.subject", "assertion.sub"),),
        attribute_condition=config.wif_attribute_condition,
        pool_state="ACTIVE",
        provider_state="ACTIVE",
    )

    worker_overrides = {}
    if drift.get("worker_flag_true"):
        worker_overrides["GATEWAY_ALLOW_EXECUTION_ROUTES"] = "true"
    worker = make_cloud_run_state(
        config, is_job=True, redis_version=redis_version, plain_overrides=worker_overrides
    )
    api_overrides = {}
    if drift.get("api_flag_true"):
        api_overrides["MILO_ENABLE_RUN_CREATION"] = "true"
    api = make_cloud_run_state(
        config, is_job=False, redis_version=redis_version, plain_overrides=api_overrides
    )

    gcp = FakeGcp(
        log=log,
        config=config,
        service_accounts=sas,
        secrets=secrets,
        payloads=payloads,
        secret_policies=secret_policies,
        gateway_policy=gateway_policy,
        invoker_policy=invoker_policy,
        wif=wif,
        worker=worker,
        api=api,
        faults=gcp_faults,
    )

    vercel_env = {name: f"reused-value-{name}" for name in VERCEL_REUSED_VARS}
    vercel_env.update(
        {
            "GATEWAY_ALLOW_EXECUTION_ROUTES": "false",
            "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI": "false",
            "UPSTASH_REDIS_REST_URL": f"https://{DB_ENDPOINT}",
            "UPSTASH_REDIS_REST_TOKEN": REDIS_TOKEN,
            "MILO_REDIS_TOKEN_FINGERPRINT": REDIS_FP,
        }
    )
    if drift.get("vercel_managed_missing"):
        del vercel_env["MILO_REDIS_TOKEN_FINGERPRINT"]

    vercel = FakeVercel(
        log=log,
        project=VercelProjectState(
            project_id=config.vercel_project_id,
            org_id=config.vercel_org_id,
            name=config.vercel_project_name,
        ),
        env_values=vercel_env,
        faults=vercel_faults,
    )

    return FakeWorld(
        config=config,
        log=log,
        local=FakeLocal(config),
        gcp=gcp,
        upstash=FakeUpstash(log, databases, tokens, faults=upstash_faults),
        vercel=vercel,
    )


#: Every write label the fakes can emit. Tests assert against this set so a
#: newly added write cannot silently escape the fault matrix.
ALL_WRITE_LABELS: frozenset[str] = frozenset(
    {
        "create_database",
        "create_service_account",
        "create_secret",
        "add_secret_version",
        "set_secret_iam",
        "set_gateway_wif_iam",
        "set_run_invoker_iam",
        "update_run_job",
        "update_run_service",
        "set_env_var",
    }
)


def make_engine(world: FakeWorld, mode, output_dir: Path, **kwargs):
    from bootstrap_v2.cli import BootstrapEngine

    return BootstrapEngine(
        config=world.config,
        mode=mode,
        local_port=world.local,
        gcp_port=world.gcp,
        upstash_port=world.upstash,
        vercel_port=world.vercel,
        output_dir=output_dir,
        environ=kwargs.pop("environ", {}),
        **kwargs,
    )
