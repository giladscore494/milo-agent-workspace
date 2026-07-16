"""Pure validator tests: redis, iam, wif, cloud run, metadata v3."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.model import (  # noqa: E402
    CloudRunContainerState,
    CloudRunEnvVar,
    CloudRunResourceState,
    IamBinding,
    IamPolicyState,
    MetadataV3,
    Provider,
    RedisIdentity,
    ResourceIdentity,
    Stage,
    UpstashDatabaseState,
    WifState,
)
from bootstrap_v2.policy import NUMERIC_PIN_REQUIRED_SECRETS, WORKER_SECRET_REFS  # noqa: E402
from bootstrap_v2.validators import cloud_run as cr  # noqa: E402
from bootstrap_v2.validators import iam as iamv  # noqa: E402
from bootstrap_v2.validators import metadata as metav  # noqa: E402
from bootstrap_v2.validators import redis as redisv  # noqa: E402
from bootstrap_v2.validators import wif as wifv  # noqa: E402
from tests.bootstrap_v2_fakes import (  # noqa: E402
    DB_ENDPOINT,
    DB_ID,
    REDIS_FP,
    make_cloud_run_state,
    make_config,
)

STAGE = Stage.GLOBAL_DISCOVERY_COMPLETE
CONFIG = make_config()


def db(**overrides) -> UpstashDatabaseState:
    values = dict(
        database_id=DB_ID,
        name="milo-production",
        state="active",
        tls=True,
        region="us-central1",
        endpoint=DB_ENDPOINT,
        rest_url=f"https://{DB_ENDPOINT}",
        token_fingerprint_sha256=REDIS_FP,
    )
    values.update(overrides)
    return UpstashDatabaseState(**values)


def redis_identity(**overrides) -> RedisIdentity:
    values = dict(
        database_id=DB_ID,
        database_name="milo-production",
        rest_url=f"https://{DB_ENDPOINT}",
        token_fingerprint_sha256=REDIS_FP,
        secret_resource_name="UPSTASH_REDIS_REST_TOKEN",
        enabled_secret_version="7",
        api_secret_version_pin="7",
        worker_secret_version_pin="7",
        vercel_value_fingerprint_sha256=REDIS_FP,
        logical_environment="production",
    )
    values.update(overrides)
    return RedisIdentity(**values)


# ---------------------------------------------------------------- redis


def test_full_fingerprint_format_is_required():
    assert redisv.is_full_fingerprint("a" * 64)
    assert not redisv.is_full_fingerprint("a" * 16)  # truncation rejected
    assert not redisv.is_full_fingerprint("A" * 64)  # uppercase rejected
    assert not redisv.is_full_fingerprint("a" * 63)
    assert not redisv.is_full_fingerprint("")
    assert len(redisv.fingerprint_sha256("x")) == 64


def test_select_database_by_exact_name():
    selected, findings = redisv.select_database((db(),), "", "milo-production")
    assert selected is not None and not findings


def test_select_database_name_is_case_sensitive():
    selected, findings = redisv.select_database((db(),), "", "MILO-PRODUCTION")
    assert selected is None and not findings  # cleanly absent, not an error


def test_select_database_ambiguous_name_blocks():
    twin = db(database_id="db-other-9999")
    selected, findings = redisv.select_database((db(), twin), "", "milo-production")
    assert selected is None
    assert any(f.code == "REDIS_AMBIGUOUS_NAME" for f in findings)


def test_select_database_by_exact_id():
    selected, findings = redisv.select_database((db(),), DB_ID, "ignored")
    assert selected is not None and not findings
    selected, findings = redisv.select_database((db(),), "db-missing", "x")
    assert selected is None
    assert any(f.code == "REDIS_ID_NOT_FOUND" for f in findings)


def test_list_detail_id_equality_is_required():
    findings = redisv.verify_database_detail(db(), db(database_id="db-other"), STAGE)
    assert any(f.code == "REDIS_LIST_DETAIL_ID_MISMATCH" for f in findings)


def test_detail_requires_active_tls_and_canonical_url():
    findings = redisv.verify_database_detail(db(), db(state="deleting"), STAGE)
    assert any(f.code == "REDIS_NOT_ACTIVE" for f in findings)
    findings = redisv.verify_database_detail(db(), db(tls=False), STAGE)
    assert any(f.code == "REDIS_TLS_NOT_TRUE" for f in findings)
    findings = redisv.verify_database_detail(
        db(), db(rest_url="http://elsewhere.example"), STAGE
    )
    assert any(f.code == "REDIS_REST_URL_NOT_CANONICAL" for f in findings)


def test_identity_coherence_rejects_mixed_databases():
    findings = redisv.verify_redis_identity_coherence(
        redis_identity(database_id="db-OTHER"), db()
    )
    assert any(f.code == "REDIS_IDENTITY_MIXED_DB_ID" for f in findings)


def test_identity_coherence_rejects_truncated_fingerprint():
    findings = redisv.verify_redis_identity_coherence(
        redis_identity(token_fingerprint_sha256=REDIS_FP[:16]), db()
    )
    assert any(f.code == "REDIS_FINGERPRINT_NOT_FULL" for f in findings)


def test_identity_coherence_rejects_pin_mismatch_and_latest():
    findings = redisv.verify_redis_identity_coherence(
        redis_identity(api_secret_version_pin="6"), db()
    )
    assert any(f.code == "REDIS_VERSION_PIN_MISMATCH" for f in findings)
    findings = redisv.verify_redis_identity_coherence(
        redis_identity(
            enabled_secret_version="latest",
            api_secret_version_pin="latest",
            worker_secret_version_pin="latest",
        ),
        db(),
    )
    assert any(f.code == "REDIS_VERSION_NOT_NUMERIC" for f in findings)


def test_clean_identity_is_coherent():
    assert redisv.verify_redis_identity_coherence(redis_identity(), db()) == ()


# ---------------------------------------------------------------- iam


def policy(*bindings: IamBinding) -> IamPolicyState:
    return IamPolicyState(
        resource=ResourceIdentity(
            provider=Provider.GCP, kind="secret_iam_policy", name="X", scope="p"
        ),
        etag="e",
        bindings=bindings,
    )


def test_unexpected_accessor_is_blocking_not_warning():
    from bootstrap_v2.model import Severity

    findings = iamv.verify_exact_policy(
        policy(
            IamBinding(
                role="roles/secretmanager.secretAccessor",
                members=("serviceAccount:ok@x.iam.gserviceaccount.com", "serviceAccount:intruder@x.iam.gserviceaccount.com"),
            )
        ),
        "roles/secretmanager.secretAccessor",
        ("serviceAccount:ok@x.iam.gserviceaccount.com",),
        STAGE,
    )
    unexpected = [f for f in findings if f.code == "IAM_UNEXPECTED_MEMBER"]
    assert unexpected and all(f.severity is Severity.BLOCKED for f in unexpected)


def test_same_role_bindings_with_conditions_are_never_merged():
    findings = iamv.verify_exact_policy(
        policy(
            IamBinding(role="r", members=("serviceAccount:a@x.iam.gserviceaccount.com",)),
            IamBinding(
                role="r",
                members=("serviceAccount:b@x.iam.gserviceaccount.com",),
                condition_expression="request.time < timestamp('2027-01-01T00:00:00Z')",
            ),
        ),
        "r",
        ("serviceAccount:a@x.iam.gserviceaccount.com", "serviceAccount:b@x.iam.gserviceaccount.com"),
        STAGE,
    )
    assert any(f.code == "IAM_BINDING_COUNT" for f in findings)


def test_condition_mismatch_blocks():
    findings = iamv.verify_exact_policy(
        policy(
            IamBinding(
                role="r",
                members=("serviceAccount:a@x.iam.gserviceaccount.com",),
                condition_expression="resource.name == 'other'",
            )
        ),
        "r",
        ("serviceAccount:a@x.iam.gserviceaccount.com",),
        STAGE,
    )
    assert any(f.code == "IAM_CONDITION_MISMATCH" for f in findings)


def test_broad_principals_always_block():
    for member in ("allUsers", "allAuthenticatedUsers"):
        findings = iamv.find_forbidden_principals(
            policy(IamBinding(role="roles/run.invoker", members=(member,))), STAGE
        )
        assert any(f.code == "IAM_BROAD_PRINCIPAL" for f in findings)


def test_exact_policy_passes():
    findings = iamv.verify_exact_policy(
        policy(
            IamBinding(
                role="roles/run.invoker",
                members=("serviceAccount:gw@x.iam.gserviceaccount.com",),
            )
        ),
        "roles/run.invoker",
        ("serviceAccount:gw@x.iam.gserviceaccount.com",),
        STAGE,
    )
    assert findings == ()


def test_unexpected_role_on_secret_blocks():
    findings = iamv.verify_secret_accessors(
        policy(
            IamBinding(
                role="roles/secretmanager.secretAccessor",
                members=("serviceAccount:a@x.iam.gserviceaccount.com",),
            ),
            IamBinding(
                role="roles/secretmanager.admin",
                members=("serviceAccount:a@x.iam.gserviceaccount.com",),
            ),
        ),
        ("serviceAccount:a@x.iam.gserviceaccount.com",),
        STAGE,
    )
    assert any(f.code == "IAM_UNEXPECTED_SECRET_ROLE" for f in findings)


# ---------------------------------------------------------------- wif


def wif_state(**overrides) -> WifState:
    values = dict(
        pool_id=CONFIG.wif_pool_id,
        provider_id=CONFIG.wif_provider_id,
        issuer_uri=CONFIG.wif_issuer,
        allowed_audiences=(CONFIG.wif_allowed_audience,),
        attribute_mapping=(("google.subject", "assertion.sub"),),
        attribute_condition=CONFIG.wif_attribute_condition,
        pool_state="ACTIVE",
        provider_state="ACTIVE",
    )
    values.update(overrides)
    return WifState(**values)


def test_wif_exact_values_pass():
    assert wifv.verify_wif(wif_state(), CONFIG) == ()


@pytest.mark.parametrize(
    "override,code",
    [
        ({"issuer_uri": "https://oidc.vercel.com/other-team"}, "WIF_ISSUER_MISMATCH"),
        ({"allowed_audiences": ("https://vercel.com/other",)}, "WIF_AUDIENCE_MISMATCH"),
        (
            {"allowed_audiences": (CONFIG.wif_allowed_audience, "https://extra")},
            "WIF_AUDIENCE_MISMATCH",
        ),
        ({"attribute_condition": "owner:someone-else"}, "WIF_CONDITION_MISMATCH"),
        ({"attribute_mapping": ()}, "WIF_ATTRIBUTE_MAPPING_MISMATCH"),
        ({"pool_state": "DELETED"}, "WIF_NOT_ACTIVE"),
    ],
)
def test_wif_inexactness_blocks(override, code):
    findings = wifv.verify_wif(wif_state(**override), CONFIG)
    assert any(f.code == code for f in findings)


def test_wif_principal_set_format():
    value = wifv.expected_principal_set("123", "pool", "prj")
    assert value == (
        "principalSet://iam.googleapis.com/projects/123/locations/global/"
        "workloadIdentityPools/pool/attribute.project/prj"
    )


# ---------------------------------------------------------------- cloud run


def container(*env: CloudRunEnvVar, name: str = "app") -> CloudRunContainerState:
    return CloudRunContainerState(name=name, image="img@sha256:d", env=env)


def resource(*containers: CloudRunContainerState, sa: str = "sa@x.iam.gserviceaccount.com") -> CloudRunResourceState:
    return CloudRunResourceState(
        resource=ResourceIdentity(
            provider=Provider.GCP, kind="cloud_run_api_service", name="svc", scope="p"
        ),
        service_account=sa,
        containers=containers,
    )


def test_multiple_candidate_containers_block():
    state = resource(container(name="one"), container(name="two"))
    selected, findings = cr.select_application_container(state, ("app",), STAGE)
    assert selected is None
    assert any(f.code == "CLOUD_RUN_AMBIGUOUS_CONTAINER" for f in findings)


def test_single_named_match_is_selected_among_sidecars():
    state = resource(container(name="app"), container(name="sidecar"))
    selected, findings = cr.select_application_container(state, ("app",), STAGE)
    assert selected is not None and selected.name == "app"
    assert findings == ()


def test_duplicate_env_names_block_never_last_write_wins():
    c = container(
        CloudRunEnvVar(name="A", value="1"), CloudRunEnvVar(name="A", value="2")
    )
    findings = cr.find_env_conflicts(c, STAGE)
    assert any(f.code == "CLOUD_RUN_DUPLICATE_ENV" for f in findings)


def test_plain_and_secret_definition_for_same_name_blocks():
    c = container(
        CloudRunEnvVar(name="A", value="plain"),
        CloudRunEnvVar(name="A", secret_name="A", secret_version="1"),
    )
    findings = cr.find_env_conflicts(c, STAGE)
    assert any(f.code == "CLOUD_RUN_PLAIN_SECRET_CONFLICT" for f in findings)


@pytest.mark.parametrize(
    "value,code",
    [
        ("nan", "BUDGET_NOT_FINITE"),
        ("inf", "BUDGET_NOT_FINITE"),
        ("-1", "BUDGET_NOT_POSITIVE"),
        ("0", "BUDGET_NOT_POSITIVE"),
        ("abc", "BUDGET_MALFORMED"),
        ("999999", "BUDGET_ABOVE_STAGE_A_BOUND"),
    ],
)
def test_budget_rejections(value, code):
    env = [
        CloudRunEnvVar(name=name, value="1")
        for name in (
            "MILO_DAILY_USER_BUDGET",
            "MILO_DAILY_PROJECT_BUDGET",
            "MILO_MAX_MODEL_CALLS_PER_RUN",
            "MILO_MAX_TOTAL_TOKENS_PER_RUN",
            "MILO_MAX_RUN_DURATION_SECONDS",
        )
    ]
    env.append(CloudRunEnvVar(name="MILO_MAX_COST_PER_RUN", value=value))
    findings = cr.validate_budgets(container(*env), STAGE)
    assert any(f.code == code for f in findings)


def test_missing_budget_blocks():
    findings = cr.validate_budgets(container(), STAGE)
    assert any(f.code == "BUDGET_MISSING" for f in findings)


def test_secret_backed_budget_blocks():
    env = [
        CloudRunEnvVar(name=name, value="1")
        for name in (
            "MILO_MAX_COST_PER_RUN",
            "MILO_DAILY_USER_BUDGET",
            "MILO_DAILY_PROJECT_BUDGET",
            "MILO_MAX_MODEL_CALLS_PER_RUN",
            "MILO_MAX_TOTAL_TOKENS_PER_RUN",
        )
    ]
    env.append(
        CloudRunEnvVar(
            name="MILO_MAX_RUN_DURATION_SECONDS",
            secret_name="X",
            secret_version="1",
        )
    )
    findings = cr.validate_budgets(container(*env), STAGE)
    assert any(f.code == "BUDGET_SECRET_BACKED" for f in findings)


@pytest.mark.parametrize("value", ["", "0", "no", "off", "False", "FALSE", "true"])
def test_execution_flag_representations_other_than_false_block(value):
    env = [
        CloudRunEnvVar(name=name, value="false")
        for name in (
            "MILO_ENABLE_RUN_CREATION",
            "MILO_ENABLE_PROPOSAL_MUTATIONS",
            "MILO_ENABLE_PROPOSAL_READS",
            "MILO_ENABLE_RUN_CANCELLATION",
            "MILO_ENABLE_EXECUTION_CONTROL",
            "GATEWAY_ALLOW_EXECUTION_ROUTES",
        )
    ]
    env.append(CloudRunEnvVar(name="MILO_ENABLE_PAID_EXECUTION", value=value))
    findings = cr.validate_execution_flags(container(*env), STAGE)
    assert any(f.code == "EXECUTION_FLAG_NOT_FALSE" for f in findings)


def test_missing_execution_flag_is_not_false():
    findings = cr.validate_execution_flags(container(), STAGE)
    assert any(f.code == "EXECUTION_FLAG_MISSING" for f in findings)


def test_latest_or_nonnumeric_redis_pin_blocks():
    for version in ("latest", "", "v7"):
        c = container(
            CloudRunEnvVar(
                name="UPSTASH_REDIS_REST_TOKEN",
                secret_name="UPSTASH_REDIS_REST_TOKEN",
                secret_version=version,
            ),
            CloudRunEnvVar(name="SUPABASE_URL", secret_name="SUPABASE_URL", secret_version="1"),
            CloudRunEnvVar(name="SUPABASE_SECRET_KEY", secret_name="SUPABASE_SECRET_KEY", secret_version="1"),
            CloudRunEnvVar(name="KIMI_API_KEY", secret_name="KIMI_API_KEY", secret_version="1"),
        )
        findings = cr.validate_secret_refs(
            c, WORKER_SECRET_REFS, NUMERIC_PIN_REQUIRED_SECRETS, STAGE
        )
        assert any(f.code == "SECRET_REF_PIN_NOT_NUMERIC" for f in findings), version


def test_server_secret_as_plain_text_blocks():
    c = container(
        CloudRunEnvVar(name="KIMI_API_KEY", value="sk-plaintext"),
        CloudRunEnvVar(name="SUPABASE_URL", secret_name="SUPABASE_URL", secret_version="1"),
        CloudRunEnvVar(name="SUPABASE_SECRET_KEY", secret_name="SUPABASE_SECRET_KEY", secret_version="1"),
        CloudRunEnvVar(name="UPSTASH_REDIS_REST_TOKEN", secret_name="UPSTASH_REDIS_REST_TOKEN", secret_version="7"),
    )
    findings = cr.validate_secret_refs(
        c, WORKER_SECRET_REFS, NUMERIC_PIN_REQUIRED_SECRETS, STAGE
    )
    assert any(f.code == "SECRET_CONFIGURED_AS_PLAIN" for f in findings)


def test_wrong_secret_resource_blocks():
    c = container(
        CloudRunEnvVar(name="KIMI_API_KEY", secret_name="MOONSHOT_API_KEY", secret_version="1"),
        CloudRunEnvVar(name="SUPABASE_URL", secret_name="SUPABASE_URL", secret_version="1"),
        CloudRunEnvVar(name="SUPABASE_SECRET_KEY", secret_name="SUPABASE_SECRET_KEY", secret_version="1"),
        CloudRunEnvVar(name="UPSTASH_REDIS_REST_TOKEN", secret_name="UPSTASH_REDIS_REST_TOKEN", secret_version="7"),
    )
    findings = cr.validate_secret_refs(
        c, WORKER_SECRET_REFS, NUMERIC_PIN_REQUIRED_SECRETS, STAGE
    )
    assert any(f.code == "SECRET_REF_WRONG_RESOURCE" for f in findings)


def test_deprecated_milo_release_sha_blocks():
    c = container(CloudRunEnvVar(name="MILO_RELEASE_SHA", value="deadbeef"))
    findings = cr.validate_deprecated_keys(c, STAGE)
    assert any(f.code == "DEPRECATED_ENV_PRESENT" for f in findings)


def test_full_resource_validation_passes_on_desired_state():
    state = make_cloud_run_state(CONFIG, is_job=True)
    findings = cr.validate_resource(
        state,
        CONFIG.worker_service_account,
        WORKER_SECRET_REFS,
        NUMERIC_PIN_REQUIRED_SECRETS,
        (CONFIG.cloud_run_worker_job,),
        STAGE,
        expected_bootstrap_sha=CONFIG.bootstrap_sha,
    )
    assert findings == ()


def test_wrong_service_account_and_unauthenticated_block():
    state = replace(
        make_cloud_run_state(CONFIG, is_job=False),
        service_account="wrong@x.iam.gserviceaccount.com",
        allows_unauthenticated=True,
    )
    findings = cr.validate_resource(
        state,
        CONFIG.api_service_account,
        {},
        (),
        (CONFIG.cloud_run_api_service,),
        STAGE,
    )
    codes = {f.code for f in findings}
    assert "CLOUD_RUN_WRONG_SERVICE_ACCOUNT" in codes
    assert "CLOUD_RUN_UNAUTHENTICATED" in codes


# ---------------------------------------------------------------- metadata


def valid_metadata(**overrides) -> MetadataV3:
    values = dict(
        MILO_METADATA_SCHEMA_VERSION="3",
        MILO_BOOTSTRAP_STATUS="applied",
        MILO_ENVIRONMENT="production",
        MILO_BOOTSTRAP_SHA="1f" * 20,
        MILO_PLAN_DIGEST="a" * 64,
        MILO_METADATA_GENERATED_AT="2026-07-16T00:00:00Z",
        GITHUB_REPOSITORY="giladscore494/milo-agent-workspace",
        GITHUB_RUN_ID="12345",
        GITHUB_WORKFLOW_REF="repo/.github/workflows/x.yml@refs/heads/main",
        GITHUB_HEAD_REF="claude/production-readiness-j0hhni",
        GCP_PROJECT_ID="big-cabinet-457321-t7",
        GCP_PROJECT_NUMBER="123456789012",
        GCP_REGION="us-central1",
        CLOUD_RUN_API_SERVICE="milo-agent-api",
        CLOUD_RUN_WORKER_JOB="milo-agent-worker",
        API_SERVICE_ACCOUNT="milo-agent-api@big-cabinet-457321-t7.iam.gserviceaccount.com",
        WORKER_SERVICE_ACCOUNT="milo-agent-worker@big-cabinet-457321-t7.iam.gserviceaccount.com",
        GATEWAY_IDENTITY="milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com",
        SUPABASE_PROJECT_REF="abcdefghijklmnopqrst",
        VERCEL_PROJECT="milo-agent-workspace",
        VERCEL_PROJECT_ID="prj_abc123def456",
        VERCEL_ORG_ID="team_xyz789",
        PRODUCTION_ORIGIN="https://milo-agent-workspace.vercel.app",
        MILO_REDIS_LOGICAL_ENVIRONMENT="production",
        UPSTASH_REDIS_REST_URL=f"https://{DB_ENDPOINT}",
        SUPABASE_URL_SECRET_NAME="SUPABASE_URL",
        SUPABASE_SERVICE_KEY_SECRET_NAME="SUPABASE_SECRET_KEY",
        PROVIDER_KEY_SECRET_NAME="KIMI_API_KEY",
        REDIS_TOKEN_SECRET_NAME="UPSTASH_REDIS_REST_TOKEN",
        MILO_REDIS_DB_ID=DB_ID,
        MILO_REDIS_TOKEN_FINGERPRINT=REDIS_FP,
        MILO_REDIS_SECRET_VERSION="7",
    )
    values.update(overrides)
    return MetadataV3(**values)


def test_valid_metadata_passes():
    assert metav.validate_metadata(valid_metadata()) == ()


def test_metadata_wrong_schema_version_blocks():
    findings = metav.validate_metadata(
        valid_metadata(MILO_METADATA_SCHEMA_VERSION="2")
    )
    assert any(f.code == "METADATA_WRONG_SCHEMA_VERSION" for f in findings)


def test_metadata_empty_value_blocks():
    findings = metav.validate_metadata(valid_metadata(GCP_PROJECT_NUMBER=""))
    assert any(f.code == "METADATA_MISSING_VALUE" for f in findings)


def test_metadata_control_chars_and_nul_block():
    findings = metav.validate_metadata(valid_metadata(VERCEL_PROJECT="a\x00b"))
    assert any(f.code == "METADATA_BAD_VALUE" for f in findings)
    findings = metav.validate_metadata(valid_metadata(VERCEL_PROJECT="a\x1bb"))
    assert any(f.code == "METADATA_BAD_VALUE" for f in findings)


def test_unknown_keys_rejected_by_closed_schema():
    with pytest.raises(TypeError):
        MetadataV3(**{**valid_metadata().as_mapping(), "EXTRA_KEY": "x"})  # type: ignore[arg-type]
    text = metav.render_metadata(valid_metadata()) + "EXTRA_KEY=value\n"
    _, findings = metav.parse_metadata_text(text)
    assert any(f.code == "METADATA_UNKNOWN_KEY" for f in findings)


def test_duplicate_keys_rejected():
    text = metav.render_metadata(valid_metadata()) + "GCP_REGION=us-central1\n"
    _, findings = metav.parse_metadata_text(text)
    assert any(f.code == "METADATA_DUPLICATE_KEY" for f in findings)


@pytest.mark.parametrize(
    "key",
    [
        "MILO_RELEASE_SHA",
        "UPSTASH_REDIS_REST_TOKEN",
        "UPSTASH_API_KEY",
        "KIMI_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "VERCEL_TOKEN",
    ],
)
def test_forbidden_and_deprecated_keys_rejected(key):
    text = metav.render_metadata(valid_metadata()) + f"{key}=value\n"
    _, findings = metav.parse_metadata_text(text)
    assert any(f.code == "METADATA_FORBIDDEN_KEY" for f in findings)


def test_secret_looking_unknown_key_rejected():
    text = metav.render_metadata(valid_metadata()) + "SOME_NEW_TOKEN=value\n"
    _, findings = metav.parse_metadata_text(text)
    assert any(
        f.code in ("METADATA_UNKNOWN_KEY", "METADATA_SECRET_LOOKING_KEY")
        for f in findings
    )


def test_oversized_metadata_rejected():
    _, findings = metav.parse_metadata_text("X=" + "a" * (metav.MAX_FILE_SIZE + 10))
    assert any(f.code == "METADATA_TOO_LARGE" for f in findings)


def test_symlink_metadata_rejected(tmp_path):
    real = tmp_path / "real.env"
    real.write_text(metav.render_metadata(valid_metadata()))
    link = tmp_path / "link.env"
    link.symlink_to(real)
    _, findings = metav.read_metadata_file(link)
    assert any(f.code == "METADATA_SYMLINK" for f in findings)


def test_atomic_write_mode_and_roundtrip(tmp_path):
    out = tmp_path / "private"
    path = metav.write_metadata_atomically(valid_metadata(), out)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (out.stat().st_mode & 0o777) == 0o700
    parsed, findings = metav.read_metadata_file(path)
    assert findings == ()
    assert parsed["MILO_METADATA_SCHEMA_VERSION"] == "3"
    leftovers = [p for p in out.iterdir() if p.name.startswith(".metadata-")]
    assert leftovers == []


def test_write_refuses_invalid_metadata(tmp_path):
    with pytest.raises(ValueError):
        metav.write_metadata_atomically(
            valid_metadata(MILO_PLAN_DIGEST=""), tmp_path / "out"
        )
    assert not (tmp_path / "out" / "bootstrap-metadata-v3.env").exists()
