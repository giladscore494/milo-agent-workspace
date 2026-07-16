"""Pure planner tests: canonical serialization, digest, ordering, evidence."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.model import (  # noqa: E402
    IamBinding,
    IamPolicyState,
    Mode,
    OperationType,
    ProbeOutcome,
    Provider,
    RedisIdentity,
    ResourceIdentity,
    SecretState,
    UpstashDatabaseState,
    VercelProjectState,
    WifState,
)
from bootstrap_v2.planner import (  # noqa: E402
    DiscoveredWorld,
    Observed,
    PlannerEvidenceError,
    build_plan,
    canonical_json,
    desired_cloud_run_env,
    plan_digest,
    plan_to_canonical_json,
    state_digest,
)
from bootstrap_v2.policy import (  # noqa: E402
    SECRET_MANAGER_RESOURCE_NAMES,
    WORKER_SECRET_REFS,
)
from tests.bootstrap_v2_fakes import (  # noqa: E402
    DB_ENDPOINT,
    DB_ID,
    REDIS_FP,
    REDIS_TOKEN,
    intended_accessors,
    make_cloud_run_state,
    make_config,
    principal_set,
)

CONFIG = make_config()


def make_redis_identity(version: str = "7") -> RedisIdentity:
    return RedisIdentity(
        database_id=DB_ID,
        database_name=CONFIG.upstash_database_name,
        rest_url=f"https://{DB_ENDPOINT}",
        token_fingerprint_sha256=REDIS_FP,
        secret_resource_name="UPSTASH_REDIS_REST_TOKEN",
        enabled_secret_version=version,
        api_secret_version_pin=version,
        worker_secret_version_pin=version,
        vercel_value_fingerprint_sha256=REDIS_FP,
    )


def make_world(**overrides) -> DiscoveredWorld:
    config = CONFIG
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
    accessors = intended_accessors(config)
    secret_iam = tuple(
        (
            name,
            Observed(
                outcome=ProbeOutcome.PRESENT,
                state=IamPolicyState(
                    resource=ResourceIdentity(
                        provider=Provider.GCP,
                        kind="secret_iam_policy",
                        name=name,
                        scope=config.gcp_project_id,
                    ),
                    etag="e",
                    bindings=(
                        IamBinding(
                            role="roles/secretmanager.secretAccessor",
                            members=tuple(sorted(accessors[name])),
                        ),
                    ),
                ),
            ),
        )
        for name in SECRET_MANAGER_RESOURCE_NAMES
    )
    values = dict(
        upstash_database=Observed(outcome=ProbeOutcome.PRESENT, state=db),
        service_accounts=tuple(
            (email, Observed(outcome=ProbeOutcome.PRESENT, state=None))
            for email in (
                config.worker_service_account,
                config.gateway_service_account,
                config.api_service_account,
            )
        ),
        secrets=tuple(
            (
                name,
                Observed(
                    outcome=ProbeOutcome.PRESENT,
                    state=SecretState(name=name, exists=True, latest_enabled_version="7"),
                ),
            )
            for name in SECRET_MANAGER_RESOURCE_NAMES
        ),
        secret_iam=secret_iam,
        wif=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=WifState(
                pool_id=config.wif_pool_id,
                provider_id=config.wif_provider_id,
                issuer_uri=config.wif_issuer,
                allowed_audiences=(config.wif_allowed_audience,),
                attribute_mapping=(("google.subject", "assertion.sub"),),
                attribute_condition=config.wif_attribute_condition,
            ),
        ),
        gateway_sa_iam=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=IamPolicyState(
                resource=ResourceIdentity(
                    provider=Provider.GCP,
                    kind="service_account_iam_policy",
                    name=config.gateway_service_account,
                    scope=config.gcp_project_id,
                ),
                etag="e",
                bindings=(
                    IamBinding(
                        role="roles/iam.workloadIdentityUser",
                        members=(principal_set(config),),
                    ),
                ),
            ),
        ),
        run_invoker_iam=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=IamPolicyState(
                resource=ResourceIdentity(
                    provider=Provider.GCP,
                    kind="run_invoker_policy",
                    name=config.cloud_run_api_service,
                    scope=config.gcp_project_id,
                ),
                etag="e",
                bindings=(
                    IamBinding(
                        role="roles/run.invoker",
                        members=(f"serviceAccount:{config.gateway_service_account}",),
                    ),
                ),
            ),
        ),
        worker_job=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=make_cloud_run_state(config, is_job=True),
        ),
        api_service=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=make_cloud_run_state(config, is_job=False),
        ),
        vercel_project=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=VercelProjectState(
                project_id=config.vercel_project_id,
                org_id=config.vercel_org_id,
                name=config.vercel_project_name,
            ),
        ),
        vercel_env=tuple(
            (key, Observed(outcome=ProbeOutcome.PRESENT, state=state))
            for key, state in _happy_vercel_env().items()
        ),
        redis_identity=make_redis_identity(),
        redis_secret_version_addition_required=False,
    )
    values.update(overrides)
    return DiscoveredWorld(**values)


def _happy_vercel_env():
    from bootstrap_v2.model import VercelEnvVarState

    def fp(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    return {
        "GATEWAY_ALLOW_EXECUTION_ROUTES": VercelEnvVarState(
            key="GATEWAY_ALLOW_EXECUTION_ROUTES",
            target=("production",),
            value_fingerprint_sha256=fp("false"),
        ),
        "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI": VercelEnvVarState(
            key="NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI",
            target=("production",),
            value_fingerprint_sha256=fp("false"),
        ),
        "UPSTASH_REDIS_REST_URL": VercelEnvVarState(
            key="UPSTASH_REDIS_REST_URL",
            target=("production",),
            value_fingerprint_sha256=fp(f"https://{DB_ENDPOINT}"),
        ),
        "UPSTASH_REDIS_REST_TOKEN": VercelEnvVarState(
            key="UPSTASH_REDIS_REST_TOKEN",
            target=("production",),
            value_fingerprint_sha256=fp(REDIS_TOKEN),
        ),
        "MILO_REDIS_TOKEN_FINGERPRINT": VercelEnvVarState(
            key="MILO_REDIS_TOKEN_FINGERPRINT",
            target=("production",),
            value_fingerprint_sha256=fp(REDIS_FP),
        ),
    }


def test_clean_world_yields_empty_plan():
    plan = build_plan(CONFIG, make_world())
    assert plan.operations == ()


def test_plan_digest_is_full_lowercase_sha256_and_deterministic():
    world = make_world()
    plan_a = build_plan(CONFIG, world)
    plan_b = build_plan(CONFIG, make_world())
    digest_a = plan_digest(plan_a)
    assert digest_a == plan_digest(plan_b)
    assert len(digest_a) == 64
    assert digest_a == digest_a.lower()
    assert digest_a == hashlib.sha256(
        plan_to_canonical_json(plan_a).encode()
    ).hexdigest()


def test_canonical_json_is_key_sorted_and_stable():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert state_digest({"a": 1}) == state_digest({"a": 1})
    assert state_digest({"a": 1}) != state_digest({"a": 2})
    assert state_digest(None) == "absent"


def test_digest_changes_when_content_changes():
    base = plan_digest(build_plan(CONFIG, make_world()))
    drifted = plan_digest(
        build_plan(
            CONFIG,
            make_world(
                upstash_database=Observed(outcome=ProbeOutcome.CLEANLY_ABSENT)
            ),
        )
    )
    assert base != drifted


def test_upstash_create_planned_only_when_cleanly_absent():
    world = make_world(
        upstash_database=Observed(outcome=ProbeOutcome.CLEANLY_ABSENT),
        redis_identity=None,
    )
    plan = build_plan(CONFIG, world)
    creates = [
        op
        for op in plan.operations
        if op.operation_type is OperationType.UPSTASH_CREATE_DATABASE
    ]
    assert len(creates) == 1
    assert creates[0].expected_pre_state_digest == "absent"
    assert creates[0].can_incur_cost is True
    assert creates[0].has_safe_compensation is False


@pytest.mark.parametrize(
    "outcome",
    [
        ProbeOutcome.PERMISSION_DENIED,
        ProbeOutcome.AUTH_FAILURE,
        ProbeOutcome.NETWORK_FAILURE,
        ProbeOutcome.MALFORMED_OUTPUT,
        ProbeOutcome.TIMEOUT,
        ProbeOutcome.RATE_LIMITED,
        ProbeOutcome.API_DISABLED,
        ProbeOutcome.UNKNOWN_ERROR,
    ],
)
def test_non_decisive_evidence_never_plans(outcome):
    world = make_world(upstash_database=Observed(outcome=outcome))
    with pytest.raises(PlannerEvidenceError):
        build_plan(CONFIG, world)


def test_stage_ordering_upstash_before_gcp_before_iam_before_run_before_vercel():
    world = make_world(
        service_accounts=(
            (CONFIG.worker_service_account, Observed(outcome=ProbeOutcome.CLEANLY_ABSENT)),
            (CONFIG.gateway_service_account, Observed(outcome=ProbeOutcome.PRESENT)),
            (CONFIG.api_service_account, Observed(outcome=ProbeOutcome.PRESENT)),
        ),
        run_invoker_iam=Observed(
            outcome=ProbeOutcome.PRESENT,
            state=IamPolicyState(
                resource=ResourceIdentity(
                    provider=Provider.GCP,
                    kind="run_invoker_policy",
                    name=CONFIG.cloud_run_api_service,
                    scope=CONFIG.gcp_project_id,
                ),
                etag="e",
            ),
        ),
        vercel_env=(),
    )
    plan = build_plan(CONFIG, world)
    kinds = [op.operation_type for op in plan.operations]
    sa_index = kinds.index(OperationType.GCP_CREATE_SERVICE_ACCOUNT)
    iam_index = kinds.index(OperationType.GCP_SET_RUN_INVOKER_IAM)
    vercel_index = kinds.index(OperationType.VERCEL_SET_ENV_VAR)
    assert sa_index < iam_index < vercel_index
    sequences = [op.sequence for op in plan.operations]
    assert sequences == sorted(sequences)


def test_worker_config_planned_before_api_config():
    stale = make_cloud_run_state(CONFIG, is_job=True, plain_overrides={"MILO_BOOTSTRAP_SHA": "0" * 40})
    stale_api = make_cloud_run_state(CONFIG, is_job=False, plain_overrides={"MILO_BOOTSTRAP_SHA": "0" * 40})
    world = make_world(
        worker_job=Observed(outcome=ProbeOutcome.PRESENT, state=stale),
        api_service=Observed(outcome=ProbeOutcome.PRESENT, state=stale_api),
    )
    plan = build_plan(CONFIG, world)
    kinds = [op.operation_type for op in plan.operations]
    assert kinds.index(OperationType.GCP_UPDATE_WORKER_JOB_CONFIG) < kinds.index(
        OperationType.GCP_UPDATE_API_SERVICE_CONFIG
    )


def test_desired_env_removes_deprecated_and_forces_flags_false():
    from bootstrap_v2.model import CloudRunEnvVar

    observed = make_cloud_run_state(
        CONFIG,
        is_job=True,
        plain_overrides={"MILO_ENABLE_PAID_EXECUTION": "true"},
        extra_env=(CloudRunEnvVar(name="MILO_RELEASE_SHA", value="deadbeef"),),
    )
    desired = desired_cloud_run_env(
        observed, CONFIG, make_redis_identity(), WORKER_SECRET_REFS
    )
    by_name = {var.name: var for var in desired}
    assert "MILO_RELEASE_SHA" not in by_name
    assert by_name["MILO_ENABLE_PAID_EXECUTION"].value == "false"
    assert by_name["MILO_BOOTSTRAP_SHA"].value == CONFIG.bootstrap_sha
    redis_ref = by_name["UPSTASH_REDIS_REST_TOKEN"]
    assert redis_ref.secret_name == "UPSTASH_REDIS_REST_TOKEN"
    assert redis_ref.secret_version == "7"
    assert redis_ref.secret_version.isdigit()


def test_plan_json_contains_no_secret_values():
    world = make_world(
        vercel_env=(),  # forces vercel writes including the token
    )
    plan = build_plan(CONFIG, world)
    text = plan_to_canonical_json(plan)
    assert REDIS_TOKEN not in text
    parsed = json.loads(text)
    assert parsed["schema"] == "milo-bootstrap-v2-plan"


def test_secret_accessor_planning_uses_exact_consumers():
    world = make_world(
        secret_iam=tuple(
            (
                name,
                Observed(
                    outcome=ProbeOutcome.PRESENT,
                    state=IamPolicyState(
                        resource=ResourceIdentity(
                            provider=Provider.GCP,
                            kind="secret_iam_policy",
                            name=name,
                            scope=CONFIG.gcp_project_id,
                        ),
                        etag="e",
                    ),
                ),
            )
            for name in SECRET_MANAGER_RESOURCE_NAMES
        )
    )
    plan = build_plan(CONFIG, world)
    iam_ops = [
        op
        for op in plan.operations
        if op.operation_type is OperationType.GCP_SET_SECRET_IAM
    ]
    assert {op.resource.name for op in iam_ops} == set(SECRET_MANAGER_RESOURCE_NAMES)


def test_every_operation_declares_a_post_write_read():
    world = make_world(
        upstash_database=Observed(outcome=ProbeOutcome.CLEANLY_ABSENT),
        redis_identity=None,
    )
    plan = build_plan(CONFIG, world)
    assert plan.operations
    for op in plan.operations:
        assert op.post_write_read.description
        assert op.post_write_read.expected_post_state_digest
        assert op.idempotency_key
        assert op.reason
