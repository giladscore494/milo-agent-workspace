"""GCP adapter: structured gcloud reads and gated writes.

All reads request ``--format=json`` and parse structured output — human
CLI text is never authoritative identity. Every read classifies its
outcome; permission errors, disabled APIs, network failures, timeouts and
parse failures are never treated as absence. Writes pass through the
mutation gate, never place secrets in argv (secret payloads travel via
stdin), and never create service-account keys, deploy images, execute
jobs, or grant broad principals — those operations do not exist here.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..model import (
    CloudRunContainerState,
    CloudRunEnvVar,
    CloudRunResourceState,
    GcpServiceAccountState,
    IamBinding,
    IamPolicyState,
    OperationType,
    ProbeOutcome,
    Provider,
    ResourceIdentity,
    SecretState,
    WifState,
)
from ..subprocess_runner import CommandResult, MutationGate, SecretRegistry, SubprocessRunner

_NOT_FOUND_MARKERS = ("NOT_FOUND", "was not found", "does not exist", "notFound")
_PERMISSION_MARKERS = ("PERMISSION_DENIED", "does not have permission", "denied on resource")
_AUTH_MARKERS = ("UNAUTHENTICATED", "credentials were not found", "gcloud auth login")
_API_DISABLED_MARKERS = ("SERVICE_DISABLED", "API has not been used", "it is disabled")
_RATE_MARKERS = ("RESOURCE_EXHAUSTED", "Quota exceeded", "rateLimitExceeded")
_NETWORK_MARKERS = ("Could not resolve host", "Connection refused", "Network is unreachable", "TransportError")


@dataclass(frozen=True, slots=True)
class GcpProbe:
    outcome: ProbeOutcome
    detail: str = ""
    payload: object | None = None


def classify_command(result: CommandResult) -> tuple[ProbeOutcome, str]:
    """Classify a finished gcloud command. Success is handled by callers."""

    if result.timed_out:
        return ProbeOutcome.TIMEOUT, "command timed out"
    stderr = result.stderr
    for marker in _PERMISSION_MARKERS:
        if marker in stderr:
            return ProbeOutcome.PERMISSION_DENIED, "permission denied"
    for marker in _AUTH_MARKERS:
        if marker in stderr:
            return ProbeOutcome.AUTH_FAILURE, "authentication failure"
    for marker in _API_DISABLED_MARKERS:
        if marker in stderr:
            return ProbeOutcome.API_DISABLED, "required API disabled"
    for marker in _RATE_MARKERS:
        if marker in stderr:
            return ProbeOutcome.RATE_LIMITED, "rate limited"
    for marker in _NETWORK_MARKERS:
        if marker in stderr:
            return ProbeOutcome.NETWORK_FAILURE, "network failure"
    for marker in _NOT_FOUND_MARKERS:
        if marker in stderr:
            return ProbeOutcome.CLEANLY_ABSENT, "positively identified not-found"
    return ProbeOutcome.UNKNOWN_ERROR, f"unclassified failure (exit {result.returncode})"


def parse_iam_policy(payload: object, resource: ResourceIdentity) -> IamPolicyState | None:
    if not isinstance(payload, dict):
        return None
    bindings_raw = payload.get("bindings", [])
    if not isinstance(bindings_raw, list):
        return None
    bindings: list[IamBinding] = []
    for entry in bindings_raw:
        if not isinstance(entry, dict):
            return None
        role = entry.get("role", "")
        members = entry.get("members", [])
        if not isinstance(role, str) or not isinstance(members, list):
            return None
        condition = entry.get("condition", {})
        expression = ""
        title = ""
        if isinstance(condition, dict):
            expression = str(condition.get("expression", ""))
            title = str(condition.get("title", ""))
        bindings.append(
            IamBinding(
                role=role,
                members=tuple(str(m) for m in members),
                condition_expression=expression,
                condition_title=title,
            )
        )
    return IamPolicyState(
        resource=resource,
        etag=str(payload.get("etag", "")),
        bindings=tuple(bindings),
    )


def _parse_env_entry(entry: object) -> CloudRunEnvVar | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    value_from = entry.get("valueFrom", {})
    if isinstance(value_from, dict) and "secretKeyRef" in value_from:
        ref = value_from["secretKeyRef"]
        if not isinstance(ref, dict):
            return None
        return CloudRunEnvVar(
            name=name,
            secret_name=str(ref.get("name", "")),
            secret_version=str(ref.get("key", "")),
        )
    return CloudRunEnvVar(name=name, value=str(entry.get("value", "")))


def parse_cloud_run_v1(
    payload: object, resource: ResourceIdentity, is_job: bool
) -> CloudRunResourceState | None:
    """Parse a Knative-style v1 export. Containers are kept separate.

    Service: spec.template.spec.{serviceAccountName,containers}.
    Job: spec.template.spec.template.spec.{serviceAccountName,containers}
    (the extra ExecutionSpec level).
    """

    if not isinstance(payload, dict):
        return None
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        return None
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    inner = template.get("spec")
    if not isinstance(inner, dict):
        return None
    if is_job:
        inner_template = inner.get("template")
        if not isinstance(inner_template, dict):
            return None
        inner = inner_template.get("spec")
        if not isinstance(inner, dict):
            return None
    containers_raw = inner.get("containers")
    if not isinstance(containers_raw, list) or not containers_raw:
        return None
    containers: list[CloudRunContainerState] = []
    for entry in containers_raw:
        if not isinstance(entry, dict):
            return None
        env: list[CloudRunEnvVar] = []
        for env_entry in entry.get("env", []) or []:
            parsed = _parse_env_entry(env_entry)
            if parsed is None:
                return None
            env.append(parsed)
        containers.append(
            CloudRunContainerState(
                name=str(entry.get("name", "")),
                image=str(entry.get("image", "")),
                env=tuple(env),
            )
        )
    metadata = payload.get("metadata", {})
    annotations = metadata.get("annotations", {}) if isinstance(metadata, dict) else {}
    ingress = str(annotations.get("run.googleapis.com/ingress", "")) if isinstance(annotations, dict) else ""
    return CloudRunResourceState(
        resource=resource,
        service_account=str(inner.get("serviceAccountName", "")),
        containers=tuple(containers),
        ingress=ingress,
    )


class GcpAdapter:
    def __init__(
        self,
        project_id: str,
        region: str,
        runner: SubprocessRunner | None = None,
        secrets: SecretRegistry | None = None,
    ) -> None:
        self._project = project_id
        self._region = region
        self._secrets = secrets or SecretRegistry()
        self._runner = runner or SubprocessRunner(self._secrets)

    # ---- generic structured read --------------------------------------------

    def _read_json(self, argv: tuple[str, ...]) -> GcpProbe:
        result = self._runner.run(argv)
        if result.returncode != 0 or result.timed_out:
            outcome, detail = classify_command(result)
            return GcpProbe(outcome=outcome, detail=detail)
        try:
            payload = json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return GcpProbe(
                outcome=ProbeOutcome.MALFORMED_OUTPUT,
                detail="gcloud returned non-JSON output",
            )
        return GcpProbe(outcome=ProbeOutcome.PRESENT, payload=payload)

    # ---- reads ---------------------------------------------------------------

    def describe_service_account(self, email: str) -> tuple[GcpProbe, GcpServiceAccountState | None]:
        probe = self._read_json(
            (
                "gcloud", "iam", "service-accounts", "describe", email,
                "--project", self._project, "--format", "json",
            )
        )
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, None
        payload = probe.payload
        if not isinstance(payload, dict) or payload.get("email") != email:
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="service account response did not echo the exact email",
                ),
                None,
            )
        return probe, GcpServiceAccountState(
            email=email, exists=True, disabled=bool(payload.get("disabled", False))
        )

    def describe_secret(self, name: str) -> tuple[GcpProbe, SecretState | None]:
        probe = self._read_json(
            (
                "gcloud", "secrets", "describe", name,
                "--project", self._project, "--format", "json",
            )
        )
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, None
        versions_probe = self._read_json(
            (
                "gcloud", "secrets", "versions", "list", name,
                "--project", self._project, "--filter", "state=ENABLED",
                "--format", "json",
            )
        )
        if versions_probe.outcome is not ProbeOutcome.PRESENT:
            return versions_probe, None
        payload = versions_probe.payload
        if not isinstance(payload, list):
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="secret versions response is not a JSON array",
                ),
                None,
            )
        versions: list[str] = []
        for entry in payload:
            if not isinstance(entry, dict):
                return (
                    GcpProbe(
                        outcome=ProbeOutcome.MALFORMED_OUTPUT,
                        detail="secret version entry is not an object",
                    ),
                    None,
                )
            full_name = str(entry.get("name", ""))
            versions.append(full_name.rsplit("/", 1)[-1])
        numeric = sorted((v for v in versions if v.isdigit()), key=int)
        return probe, SecretState(
            name=name,
            exists=True,
            enabled_versions=tuple(versions),
            latest_enabled_version=numeric[-1] if numeric else "",
        )

    def access_secret_payload(self, name: str, version: str) -> tuple[GcpProbe, str]:
        """Read one secret payload into memory only, registering it for
        leak prevention. Used solely for Redis fingerprint reconciliation."""

        result = self._runner.run(
            (
                "gcloud", "secrets", "versions", "access", version,
                "--secret", name, "--project", self._project,
            )
        )
        if result.returncode != 0 or result.timed_out:
            outcome, detail = classify_command(result)
            return GcpProbe(outcome=outcome, detail=detail), ""
        payload = result.stdout
        self._secrets.register(payload.strip())
        return GcpProbe(outcome=ProbeOutcome.PRESENT), payload

    def get_iam_policy(self, argv_target: tuple[str, ...], resource: ResourceIdentity) -> tuple[GcpProbe, IamPolicyState | None]:
        probe = self._read_json(argv_target + ("--format", "json"))
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, None
        policy = parse_iam_policy(probe.payload, resource)
        if policy is None:
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="IAM policy response is not a documented policy object",
                ),
                None,
            )
        return probe, policy

    def get_secret_iam(self, name: str) -> tuple[GcpProbe, IamPolicyState | None]:
        resource = ResourceIdentity(
            provider=Provider.GCP, kind="secret_iam_policy", name=name, scope=self._project
        )
        return self.get_iam_policy(
            (
                "gcloud", "secrets", "get-iam-policy", name,
                "--project", self._project,
            ),
            resource,
        )

    def get_service_account_iam(self, email: str) -> tuple[GcpProbe, IamPolicyState | None]:
        resource = ResourceIdentity(
            provider=Provider.GCP,
            kind="service_account_iam_policy",
            name=email,
            scope=self._project,
        )
        return self.get_iam_policy(
            (
                "gcloud", "iam", "service-accounts", "get-iam-policy", email,
                "--project", self._project,
            ),
            resource,
        )

    def get_run_invoker_iam(self, service: str) -> tuple[GcpProbe, IamPolicyState | None]:
        resource = ResourceIdentity(
            provider=Provider.GCP, kind="run_invoker_policy", name=service, scope=self._project
        )
        return self.get_iam_policy(
            (
                "gcloud", "run", "services", "get-iam-policy", service,
                "--project", self._project, "--region", self._region,
            ),
            resource,
        )

    def describe_wif(self, pool_id: str, provider_id: str, project_number: str) -> tuple[GcpProbe, WifState | None]:
        pool_probe = self._read_json(
            (
                "gcloud", "iam", "workload-identity-pools", "describe", pool_id,
                "--project", self._project, "--location", "global",
                "--format", "json",
            )
        )
        if pool_probe.outcome is not ProbeOutcome.PRESENT:
            return pool_probe, None
        provider_probe = self._read_json(
            (
                "gcloud", "iam", "workload-identity-pools", "providers", "describe",
                provider_id, "--workload-identity-pool", pool_id,
                "--project", self._project, "--location", "global",
                "--format", "json",
            )
        )
        if provider_probe.outcome is not ProbeOutcome.PRESENT:
            return provider_probe, None
        pool = pool_probe.payload
        provider = provider_probe.payload
        if not isinstance(pool, dict) or not isinstance(provider, dict):
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="WIF describe responses are not objects",
                ),
                None,
            )
        oidc = provider.get("oidc", {})
        if not isinstance(oidc, dict):
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="WIF provider has no documented oidc block",
                ),
                None,
            )
        mapping_raw = provider.get("attributeMapping", {})
        mapping = (
            tuple(sorted((str(k), str(v)) for k, v in mapping_raw.items()))
            if isinstance(mapping_raw, dict)
            else ()
        )
        audiences_raw = oidc.get("allowedAudiences", [])
        audiences = (
            tuple(str(a) for a in audiences_raw) if isinstance(audiences_raw, list) else ()
        )
        return provider_probe, WifState(
            pool_id=pool_id,
            provider_id=provider_id,
            issuer_uri=str(oidc.get("issuerUri", "")),
            allowed_audiences=audiences,
            attribute_mapping=mapping,
            attribute_condition=str(provider.get("attributeCondition", "")),
            pool_state=str(pool.get("state", "")),
            provider_state=str(provider.get("state", "")),
        )

    def describe_run_service(self, name: str) -> tuple[GcpProbe, CloudRunResourceState | None]:
        resource = ResourceIdentity(
            provider=Provider.GCP, kind="cloud_run_api_service", name=name, scope=self._project
        )
        probe = self._read_json(
            (
                "gcloud", "run", "services", "describe", name,
                "--project", self._project, "--region", self._region,
                "--format", "json",
            )
        )
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, None
        state = parse_cloud_run_v1(probe.payload, resource, is_job=False)
        if state is None:
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="cloud run service response did not match v1 schema",
                ),
                None,
            )
        return probe, state

    def describe_run_job(self, name: str) -> tuple[GcpProbe, CloudRunResourceState | None]:
        resource = ResourceIdentity(
            provider=Provider.GCP, kind="cloud_run_worker_job", name=name, scope=self._project
        )
        probe = self._read_json(
            (
                "gcloud", "run", "jobs", "describe", name,
                "--project", self._project, "--region", self._region,
                "--format", "json",
            )
        )
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, None
        state = parse_cloud_run_v1(probe.payload, resource, is_job=True)
        if state is None:
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="cloud run job response did not match v1 job schema",
                ),
                None,
            )
        return probe, state

    def list_enabled_services(self) -> tuple[GcpProbe, tuple[str, ...]]:
        probe = self._read_json(
            (
                "gcloud", "services", "list", "--enabled",
                "--project", self._project, "--format", "json",
            )
        )
        if probe.outcome is not ProbeOutcome.PRESENT:
            return probe, ()
        payload = probe.payload
        if not isinstance(payload, list):
            return (
                GcpProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="services list response is not a JSON array",
                ),
                (),
            )
        names: list[str] = []
        for entry in payload:
            if isinstance(entry, dict):
                config = entry.get("config", {})
                if isinstance(config, dict):
                    names.append(str(config.get("name", "")))
        return probe, tuple(names)

    # ---- gated writes ---------------------------------------------------------

    def _gated_command(
        self,
        gate: MutationGate,
        operation_type: OperationType,
        resource: ResourceIdentity,
        idempotency_key: str,
        argv: tuple[str, ...],
        stdin_data: str | None = None,
    ) -> tuple[ProbeOutcome, str]:
        operation = gate.authorize(operation_type, resource, idempotency_key)
        result = self._runner.run(argv, stdin_data=stdin_data)
        if result.returncode != 0 or result.timed_out:
            outcome, detail = classify_command(result)
            gate.record_execution(operation, succeeded=False, error_class=outcome.value)
            return outcome, detail
        gate.record_execution(operation, succeeded=True)
        return ProbeOutcome.PRESENT, ""

    def create_service_account(
        self, gate: MutationGate, idempotency_key: str, resource: ResourceIdentity, email: str
    ) -> tuple[ProbeOutcome, str]:
        account_id = email.split("@", 1)[0]
        return self._gated_command(
            gate,
            OperationType.GCP_CREATE_SERVICE_ACCOUNT,
            resource,
            idempotency_key,
            (
                "gcloud", "iam", "service-accounts", "create", account_id,
                "--project", self._project,
                "--display-name", account_id,
                "--format", "json",
            ),
        )

    def create_secret(
        self, gate: MutationGate, idempotency_key: str, resource: ResourceIdentity, name: str
    ) -> tuple[ProbeOutcome, str]:
        return self._gated_command(
            gate,
            OperationType.GCP_CREATE_SECRET,
            resource,
            idempotency_key,
            (
                "gcloud", "secrets", "create", name,
                "--project", self._project,
                "--replication-policy", "automatic",
                "--format", "json",
            ),
        )

    def add_secret_version(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        name: str,
        payload: str,
    ) -> tuple[ProbeOutcome, str]:
        """Secret payload travels via stdin (--data-file=-), never argv."""

        self._secrets.register(payload.strip())
        return self._gated_command(
            gate,
            OperationType.GCP_ADD_SECRET_VERSION,
            resource,
            idempotency_key,
            (
                "gcloud", "secrets", "versions", "add", name,
                "--project", self._project,
                "--data-file=-",
                "--format", "json",
            ),
            stdin_data=payload,
        )

    def set_iam_policy(
        self,
        gate: MutationGate,
        operation_type: OperationType,
        idempotency_key: str,
        resource: ResourceIdentity,
        argv_prefix: tuple[str, ...],
        policy: IamPolicyState,
        role: str,
        members: tuple[str, ...],
    ) -> tuple[ProbeOutcome, str]:
        """Etag-aware full-policy write via a private 0600 temp file."""

        bindings = [
            {
                "role": binding.role,
                "members": list(binding.members),
                **(
                    {
                        "condition": {
                            "expression": binding.condition_expression,
                            "title": binding.condition_title,
                        }
                    }
                    if binding.condition_expression
                    else {}
                ),
            }
            for binding in policy.bindings
            if binding.role != role
        ]
        if members:
            bindings.append({"role": role, "members": sorted(members)})
        payload = {"etag": policy.etag, "bindings": bindings}

        tmp_dir = Path(tempfile.mkdtemp(prefix="milo-iam-"))
        os.chmod(tmp_dir, 0o700)
        policy_path = tmp_dir / "policy.json"
        try:
            fd = os.open(policy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return self._gated_command(
                gate,
                operation_type,
                resource,
                idempotency_key,
                argv_prefix + (str(policy_path), "--format", "json"),
            )
        finally:
            policy_path.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def set_secret_iam(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        name: str,
        policy: IamPolicyState,
        members: tuple[str, ...],
    ) -> tuple[ProbeOutcome, str]:
        return self.set_iam_policy(
            gate,
            OperationType.GCP_SET_SECRET_IAM,
            idempotency_key,
            resource,
            (
                "gcloud", "secrets", "set-iam-policy", name,
                "--project", self._project,
            ),
            policy,
            "roles/secretmanager.secretAccessor",
            members,
        )

    def set_run_invoker_iam(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        service: str,
        policy: IamPolicyState,
        members: tuple[str, ...],
    ) -> tuple[ProbeOutcome, str]:
        return self.set_iam_policy(
            gate,
            OperationType.GCP_SET_RUN_INVOKER_IAM,
            idempotency_key,
            resource,
            (
                "gcloud", "run", "services", "set-iam-policy", service,
                "--project", self._project, "--region", self._region,
            ),
            policy,
            "roles/run.invoker",
            members,
        )

    def set_gateway_wif_iam(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        email: str,
        policy: IamPolicyState,
        members: tuple[str, ...],
    ) -> tuple[ProbeOutcome, str]:
        return self.set_iam_policy(
            gate,
            OperationType.GCP_SET_WIF_IAM,
            idempotency_key,
            resource,
            (
                "gcloud", "iam", "service-accounts", "set-iam-policy", email,
            ),
            policy,
            "roles/iam.workloadIdentityUser",
            members,
        )

    def update_run_config(
        self,
        gate: MutationGate,
        operation_type: OperationType,
        idempotency_key: str,
        resource: ResourceIdentity,
        name: str,
        is_job: bool,
        set_env: dict[str, str],
        set_secret_refs: dict[str, str],
        remove_env: tuple[str, ...],
        service_account: str,
    ) -> tuple[ProbeOutcome, str]:
        """Configuration-only update: env vars, secret refs, service account.

        No image flag exists on this code path; deployment, execution and
        traffic promotion are impossible from this adapter.
        """

        kind = ("jobs",) if is_job else ("services",)
        argv: list[str] = [
            "gcloud", "run", *kind, "update", name,
            "--project", self._project, "--region", self._region,
            "--service-account", service_account,
        ]
        if remove_env:
            argv.append("--remove-env-vars=" + ",".join(sorted(remove_env)))
        if set_env:
            for key in sorted(set_env):
                self._secrets.assert_argv_clean((set_env[key],))
            argv.append(
                "--update-env-vars="
                + ",".join(f"{key}={set_env[key]}" for key in sorted(set_env))
            )
        if set_secret_refs:
            argv.append(
                "--update-secrets="
                + ",".join(
                    f"{key}={set_secret_refs[key]}" for key in sorted(set_secret_refs)
                )
            )
        if not is_job:
            argv.append("--no-allow-unauthenticated")
        argv.extend(("--format", "json"))
        return self._gated_command(
            gate, operation_type, resource, idempotency_key, tuple(argv)
        )
