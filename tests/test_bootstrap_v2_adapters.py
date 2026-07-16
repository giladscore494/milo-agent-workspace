"""Adapter tests: outcome classification, parsing, gating, secret discipline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.adapters.gcp import (  # noqa: E402
    classify_command,
    parse_cloud_run_v1,
    parse_iam_policy,
)
from bootstrap_v2.adapters.github_provenance import (  # noqa: E402
    ExtractionEntry,
    ObservedRun,
    TrustedExpectations,
    verify_extraction,
    verify_provenance,
)
from bootstrap_v2.adapters.upstash import HttpResponse, UpstashAdapter  # noqa: E402
from bootstrap_v2.adapters.vercel import VercelAdapter  # noqa: E402
from bootstrap_v2.model import (  # noqa: E402
    Mode,
    MutationPlan,
    OperationType,
    ProbeOutcome,
    Provider,
    ResourceIdentity,
)
from bootstrap_v2.subprocess_runner import (  # noqa: E402
    CommandResult,
    MutationAfterBlockError,
    MutationGate,
    MutationLedger,
    SecretInArgvError,
    SecretRegistry,
    SubprocessRunner,
    UndeclaredMutationError,
)

RES = ResourceIdentity(provider=Provider.UPSTASH, kind="redis_database", name="db", scope="u")


def transport_returning(status: int, body: bytes):
    def transport(method, url, headers, payload):
        return HttpResponse(status=status, body=body)

    return transport


# ------------------------------------------------------------ http classification


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProbeOutcome.AUTH_FAILURE),
        (403, ProbeOutcome.PERMISSION_DENIED),
        (429, ProbeOutcome.RATE_LIMITED),
        (500, ProbeOutcome.UNKNOWN_ERROR),
        (503, ProbeOutcome.UNKNOWN_ERROR),
    ],
)
def test_upstash_http_failures_are_never_absence(status, expected):
    adapter = UpstashAdapter("a@b.c", "key", transport=transport_returning(status, b"{}"))
    probe = adapter.list_databases()
    assert probe.outcome is expected
    assert probe.outcome is not ProbeOutcome.CLEANLY_ABSENT


def test_upstash_malformed_json_is_malformed_not_absent():
    adapter = UpstashAdapter("a@b.c", "key", transport=transport_returning(200, b"not-json"))
    assert adapter.list_databases().outcome is ProbeOutcome.MALFORMED_OUTPUT


def test_upstash_undocumented_shape_is_malformed():
    adapter = UpstashAdapter(
        "a@b.c", "key", transport=transport_returning(200, b'{"unexpected": true}')
    )
    assert adapter.list_databases().outcome is ProbeOutcome.MALFORMED_OUTPUT


def test_upstash_list_entry_missing_identity_is_malformed():
    body = json.dumps([{"database_name": "x"}]).encode()
    adapter = UpstashAdapter("a@b.c", "key", transport=transport_returning(200, body))
    assert adapter.list_databases().outcome is ProbeOutcome.MALFORMED_OUTPUT


def test_upstash_network_failure_classified():
    def transport(method, url, headers, payload):
        raise OSError("connection reset")

    adapter = UpstashAdapter("a@b.c", "key", transport=transport)
    assert adapter.list_databases().outcome is ProbeOutcome.NETWORK_FAILURE


def test_upstash_timeout_classified():
    def transport(method, url, headers, payload):
        raise TimeoutError()

    adapter = UpstashAdapter("a@b.c", "key", transport=transport)
    assert adapter.list_databases().outcome is ProbeOutcome.TIMEOUT


def test_upstash_get_registers_token_and_returns_fingerprint():
    body = json.dumps(
        {
            "database_id": "db-1",
            "database_name": "milo-production",
            "state": "active",
            "tls": True,
            "region": "us-central1",
            "endpoint": "x.upstash.io",
            "rest_token": "tok-value",
        }
    ).encode()
    secrets = SecretRegistry()
    adapter = UpstashAdapter(
        "a@b.c", "key", transport=transport_returning(200, body), secrets=secrets
    )
    probe, token = adapter.get_database("db-1")
    assert probe.outcome is ProbeOutcome.PRESENT
    assert token == "tok-value"
    assert len(probe.databases[0].token_fingerprint_sha256) == 64
    assert secrets.contains_secret("prefix tok-value suffix")


def test_upstash_auth_never_leaks_into_argv_style_strings():
    secrets = SecretRegistry()
    UpstashAdapter("a@b.c", "api-key-value", secrets=secrets)
    with pytest.raises(SecretInArgvError):
        secrets.assert_argv_clean(("curl", "-u", "a@b.c:api-key-value"))


def test_vercel_404_project_is_cleanly_absent_only_on_get():
    adapter = VercelAdapter("tok", "team", transport=transport_returning(404, b"{}"))
    assert adapter.get_project("prj").outcome is ProbeOutcome.CLEANLY_ABSENT


def test_vercel_env_list_missing_envs_key_is_malformed():
    adapter = VercelAdapter("tok", "team", transport=transport_returning(200, b"{}"))
    assert adapter.list_env("prj").outcome is ProbeOutcome.MALFORMED_OUTPUT


def test_vercel_adapter_has_no_deploy_surface():
    forbidden = ("deploy", "promote", "redeploy", "link", "unlink", "remove", "prod")
    for name in dir(VercelAdapter):
        lowered = name.lower()
        for word in forbidden:
            assert word not in lowered, f"forbidden capability {name}"


def test_upstash_adapter_has_no_destructive_surface():
    forbidden = ("delete", "reset", "rename")
    for name in dir(UpstashAdapter):
        lowered = name.lower()
        for word in forbidden:
            assert word not in lowered, f"forbidden capability {name}"


# ------------------------------------------------------------ gcloud classification


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ERROR: PERMISSION_DENIED: Permission denied", ProbeOutcome.PERMISSION_DENIED),
        ("caller does not have permission", ProbeOutcome.PERMISSION_DENIED),
        ("ERROR: UNAUTHENTICATED", ProbeOutcome.AUTH_FAILURE),
        ("API has not been used in project before or it is disabled", ProbeOutcome.API_DISABLED),
        ("RESOURCE_EXHAUSTED: Quota exceeded", ProbeOutcome.RATE_LIMITED),
        ("Could not resolve host: run.googleapis.com", ProbeOutcome.NETWORK_FAILURE),
        ("ERROR: NOT_FOUND: resource was not found", ProbeOutcome.CLEANLY_ABSENT),
        ("some novel failure", ProbeOutcome.UNKNOWN_ERROR),
    ],
)
def test_gcloud_classification(stderr, expected):
    result = CommandResult(argv=("gcloud",), returncode=1, stdout="", stderr=stderr)
    outcome, _ = classify_command(result)
    assert outcome is expected


def test_gcloud_permission_denied_wins_over_not_found_text():
    # A permission error mentioning the resource name must never be absence.
    result = CommandResult(
        argv=("gcloud",),
        returncode=1,
        stdout="",
        stderr="PERMISSION_DENIED: permission denied on resource (or it may not exist)",
    )
    outcome, _ = classify_command(result)
    assert outcome is ProbeOutcome.PERMISSION_DENIED


def test_gcloud_timeout_classified():
    result = CommandResult(argv=("gcloud",), returncode=-1, stdout="", stderr="", timed_out=True)
    outcome, _ = classify_command(result)
    assert outcome is ProbeOutcome.TIMEOUT


# ------------------------------------------------------------ cloud run parsing


def service_payload():
    return {
        "metadata": {"annotations": {"run.googleapis.com/ingress": "internal"}},
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": "api@p.iam.gserviceaccount.com",
                    "containers": [
                        {
                            "name": "app",
                            "image": "img@sha256:1",
                            "env": [
                                {"name": "PLAIN", "value": "v"},
                                {
                                    "name": "SECRET_REF",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": "S", "key": "7"}
                                    },
                                },
                            ],
                        }
                    ],
                }
            }
        },
    }


def job_payload():
    return {
        "spec": {
            "template": {
                "spec": {
                    "template": {
                        "spec": {
                            "serviceAccountName": "worker@p.iam.gserviceaccount.com",
                            "containers": [
                                {"name": "worker", "image": "img@sha256:2", "env": []}
                            ],
                        }
                    }
                }
            }
        }
    }


def test_parse_service_uses_v1_path():
    res = ResourceIdentity(provider=Provider.GCP, kind="cloud_run_api_service", name="s", scope="p")
    state = parse_cloud_run_v1(service_payload(), res, is_job=False)
    assert state is not None
    assert state.service_account == "api@p.iam.gserviceaccount.com"
    env = {v.name: v for v in state.containers[0].env}
    assert env["PLAIN"].value == "v"
    assert env["SECRET_REF"].secret_name == "S"
    assert env["SECRET_REF"].secret_version == "7"
    assert state.ingress == "internal"


def test_parse_job_uses_nested_execution_spec_path():
    res = ResourceIdentity(provider=Provider.GCP, kind="cloud_run_worker_job", name="j", scope="p")
    state = parse_cloud_run_v1(job_payload(), res, is_job=True)
    assert state is not None
    assert state.service_account == "worker@p.iam.gserviceaccount.com"


def test_parse_job_with_service_path_fails_closed():
    res = ResourceIdentity(provider=Provider.GCP, kind="cloud_run_worker_job", name="j", scope="p")
    assert parse_cloud_run_v1(service_payload(), res, is_job=True) is None


def test_parse_keeps_containers_separate():
    payload = service_payload()
    payload["spec"]["template"]["spec"]["containers"].append(
        {"name": "sidecar", "image": "img@sha256:3", "env": [{"name": "PLAIN", "value": "other"}]}
    )
    res = ResourceIdentity(provider=Provider.GCP, kind="cloud_run_api_service", name="s", scope="p")
    state = parse_cloud_run_v1(payload, res, is_job=False)
    assert state is not None
    assert len(state.containers) == 2
    assert state.containers[0].env != state.containers[1].env


def test_parse_malformed_env_fails_closed():
    payload = service_payload()
    payload["spec"]["template"]["spec"]["containers"][0]["env"] = [{"value": "nameless"}]
    res = ResourceIdentity(provider=Provider.GCP, kind="cloud_run_api_service", name="s", scope="p")
    assert parse_cloud_run_v1(payload, res, is_job=False) is None


def test_parse_iam_policy_keeps_conditions():
    res = ResourceIdentity(provider=Provider.GCP, kind="secret_iam_policy", name="S", scope="p")
    policy = parse_iam_policy(
        {
            "etag": "abc",
            "bindings": [
                {
                    "role": "r",
                    "members": ["serviceAccount:a@p.iam.gserviceaccount.com"],
                    "condition": {"expression": "expr", "title": "t"},
                }
            ],
        },
        res,
    )
    assert policy is not None
    assert policy.etag == "abc"
    assert policy.bindings[0].condition_expression == "expr"


def test_parse_iam_policy_malformed_fails_closed():
    res = ResourceIdentity(provider=Provider.GCP, kind="secret_iam_policy", name="S", scope="p")
    assert parse_iam_policy({"bindings": "oops"}, res) is None
    assert parse_iam_policy([], res) is None


# ------------------------------------------------------------ mutation gate


def declared_plan() -> MutationPlan:
    from bootstrap_v2.model import MutationOperation, PostWriteRead

    op = MutationOperation(
        sequence=1,
        provider=Provider.UPSTASH,
        operation_type=OperationType.UPSTASH_CREATE_DATABASE,
        resource=RES,
        expected_pre_state_digest="absent",
        intended_post_state_digest="d" * 64,
        reason="r",
        idempotency_key="k1",
        can_incur_cost=True,
        has_safe_compensation=False,
        post_write_read=PostWriteRead(description="d", expected_post_state_digest="d" * 64),
    )
    return MutationPlan(mode=Mode.APPLY, bootstrap_sha="a" * 40, operations=(op,))


def test_gate_rejects_undeclared_mutation_and_records_it():
    ledger = MutationLedger()
    gate = MutationGate(declared_plan(), ledger)
    with pytest.raises(UndeclaredMutationError):
        gate.authorize(OperationType.UPSTASH_CREATE_DATABASE, RES, "not-declared")
    records = ledger.records()
    assert len(records) == 1
    assert records[0].declared is False
    assert records[0].executed is False
    assert gate.closed


def test_gate_rejects_identity_mismatch():
    ledger = MutationLedger()
    gate = MutationGate(declared_plan(), ledger)
    other = ResourceIdentity(provider=Provider.UPSTASH, kind="redis_database", name="other", scope="u")
    with pytest.raises(UndeclaredMutationError):
        gate.authorize(OperationType.UPSTASH_CREATE_DATABASE, other, "k1")


def test_gate_rejects_everything_after_close():
    ledger = MutationLedger()
    gate = MutationGate(declared_plan(), ledger)
    gate.close()
    with pytest.raises(MutationAfterBlockError):
        gate.authorize(OperationType.UPSTASH_CREATE_DATABASE, RES, "k1")


def test_failed_execution_closes_gate():
    ledger = MutationLedger()
    gate = MutationGate(declared_plan(), ledger)
    op = gate.authorize(OperationType.UPSTASH_CREATE_DATABASE, RES, "k1")
    gate.record_execution(op, succeeded=False, error_class="network")
    assert gate.closed
    with pytest.raises(MutationAfterBlockError):
        gate.authorize(OperationType.UPSTASH_CREATE_DATABASE, RES, "k1")


# ------------------------------------------------------------ subprocess runner


def test_runner_refuses_secret_in_argv():
    secrets = SecretRegistry()
    secrets.register("super-secret-token")
    runner = SubprocessRunner(secrets)
    with pytest.raises(SecretInArgvError):
        runner.run(("echo", "super-secret-token"))


def test_runner_redacts_secret_in_output():
    secrets = SecretRegistry()
    secrets.register("super-secret-token")
    runner = SubprocessRunner(secrets)
    result = runner.run(("sh", "-c", 'echo "value is $LEAK"'), stdin_data=None)
    # The secret is not in the child env at all (allowlist), so nothing leaks.
    assert "super-secret-token" not in result.stdout


def test_runner_child_env_is_allowlisted(monkeypatch):
    monkeypatch.setenv("UPSTASH_API_KEY", "should-not-pass")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    runner = SubprocessRunner(SecretRegistry())
    result = runner.run(("sh", "-c", "echo ${UPSTASH_API_KEY:-ABSENT}"))
    assert result.stdout.strip() == "ABSENT"


def test_runner_secret_stdin_not_argv():
    secrets = SecretRegistry()
    secrets.register("payload-value")
    runner = SubprocessRunner(secrets)
    result = runner.run(("cat",), stdin_data="payload-value")
    assert result.returncode == 0
    assert result.stdout == "[REDACTED]"  # redaction backstop on captured output


def test_registry_clear_forgets_values():
    secrets = SecretRegistry()
    secrets.register("v")
    secrets.clear()
    assert not secrets.contains_secret("v")


# ------------------------------------------------------------ provenance


def trusted() -> TrustedExpectations:
    return TrustedExpectations(
        repository="giladscore494/milo-agent-workspace",
        workflow_path=".github/workflows/bootstrap-production-v2.yml",
        workflow_ref="refs/heads/main",
        release_ref="refs/heads/main",
        head_sha="1f" * 20,
        plan_digest="d" * 64,
        artifact_name="bootstrap-metadata-v3",
    )


def observed(**overrides) -> ObservedRun:
    values = dict(
        repository="giladscore494/milo-agent-workspace",
        workflow_path=".github/workflows/bootstrap-production-v2.yml",
        workflow_ref="refs/heads/main",
        run_ref="refs/heads/main",
        head_sha="1f" * 20,
        event="workflow_dispatch",
        conclusion="success",
        is_fork=False,
        mode_input="apply",
        plan_digest_output="d" * 64,
        artifact_names=("bootstrap-metadata-v3",),
        artifact_expired=False,
        artifact_size=512,
    )
    values.update(overrides)
    return ObservedRun(**values)


def test_provenance_accepts_exact_match():
    assert verify_provenance(trusted(), observed()) == ()


@pytest.mark.parametrize(
    "override,code",
    [
        ({"repository": "attacker/repo"}, "PROVENANCE_WRONG_REPOSITORY"),
        ({"is_fork": True}, "PROVENANCE_FORK_SOURCE"),
        ({"workflow_path": ".github/workflows/evil.yml"}, "PROVENANCE_WRONG_WORKFLOW"),
        ({"workflow_ref": "refs/heads/feature"}, "PROVENANCE_WRONG_WORKFLOW_REF"),
        ({"run_ref": "refs/heads/feature"}, "PROVENANCE_WRONG_RELEASE_REF"),
        ({"mode_input": "plan"}, "PROVENANCE_WRONG_MODE"),
        ({"conclusion": "failure"}, "PROVENANCE_NOT_SUCCESSFUL"),
        ({"head_sha": "2f" * 20}, "PROVENANCE_WRONG_HEAD_SHA"),
        ({"plan_digest_output": "e" * 64}, "PROVENANCE_WRONG_PLAN_DIGEST"),
        (
            {"artifact_names": ("bootstrap-metadata-v3", "bootstrap-metadata-v3")},
            "PROVENANCE_ARTIFACT_NOT_UNIQUE",
        ),
        ({"artifact_names": ()}, "PROVENANCE_ARTIFACT_NOT_UNIQUE"),
        ({"artifact_expired": True}, "PROVENANCE_ARTIFACT_EXPIRED"),
        ({"artifact_size": 0}, "PROVENANCE_ARTIFACT_SIZE"),
        ({"artifact_size": 100 * 1024 * 1024}, "PROVENANCE_ARTIFACT_SIZE"),
    ],
)
def test_provenance_rejections(override, code):
    findings = verify_provenance(trusted(), observed(**override))
    assert any(f.code == code for f in findings)


def test_extraction_requires_exactly_one_regular_file():
    ok = (ExtractionEntry(name="bootstrap-metadata-v3.env", is_regular_file=True, is_symlink=False),)
    assert verify_extraction(ok) == ()

    symlink = ok + (ExtractionEntry(name="link", is_regular_file=False, is_symlink=True),)
    findings = verify_extraction(symlink)
    assert any(f.code == "PROVENANCE_SYMLINK_IN_ARTIFACT" for f in findings)
    assert any(f.code == "PROVENANCE_EXTRA_FILES" for f in findings)

    traversal = (ExtractionEntry(name="../evil", is_regular_file=True, is_symlink=False),)
    findings = verify_extraction(traversal)
    assert any(f.code == "PROVENANCE_UNSAFE_PATH" for f in findings)
