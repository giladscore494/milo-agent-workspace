"""Static safety proofs for scripts/deploy/cloud-run.sh.

These assertions encode the production hardening contract for the Cloud Run
deployment: immutable full-SHA image tags, non-destructive environment and
secret updates, Stage A execution flags pinned off, a worker-only provider
key, separate runtime identities, worker-before-API ordering, and mandatory
post-deployment verification.
"""

from pathlib import Path

SCRIPT = Path("scripts/deploy/cloud-run.sh").read_text()

API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
WORKER_SA = "milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"

# The canonical Stage A execution flags (mirrors production_config.EXECUTION_FLAGS).
EXECUTION_FLAGS = (
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
)


def _command_starting_with(prefix: str) -> str:
    """The full shell command (including backslash continuations) for a prefix."""
    lines = SCRIPT.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            block = [line]
            cursor = index
            while block[-1].rstrip().endswith("\\"):
                cursor += 1
                block.append(lines[cursor])
            return "\n".join(block)
    raise AssertionError(f"command not found in cloud-run.sh: {prefix!r}")


def _array_literal(name: str) -> str:
    start = SCRIPT.index(f"{name}=(")
    end = SCRIPT.index("\n)", start)
    return SCRIPT[start:end]


def _worker_deploy_block():
    return _command_starting_with("gcloud run jobs deploy ")


def _api_deploy_block():
    return _command_starting_with("gcloud run deploy ")


def _iam_binding_block():
    return _command_starting_with("gcloud run jobs add-iam-policy-binding ")


# ---------------------------------------------------------------------------
# 1. immutable full-SHA image tags
# ---------------------------------------------------------------------------


def test_release_sha_is_the_full_forty_character_commit_sha():
    assert "RELEASE_SHA=$(git rev-parse HEAD)" in SCRIPT
    # A short SHA is ambiguous and must never become a production image tag.
    assert "git rev-parse --short" not in SCRIPT
    assert "SHORT_SHA" not in SCRIPT


def test_release_sha_is_validated_as_forty_hex_characters():
    assert '[[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in SCRIPT
    assert "require_full_release_sha" in SCRIPT
    # The guard runs during preflight, before anything is built or deployed.
    guard_index = SCRIPT.index("  require_full_release_sha")
    assert guard_index < SCRIPT.index("gcloud builds submit")


def test_both_image_tags_use_the_full_release_sha():
    assert 'API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/api:$RELEASE_SHA"' in SCRIPT
    assert 'WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/worker:$RELEASE_SHA"' in SCRIPT
    assert ":$SHORT_SHA" not in SCRIPT


# ---------------------------------------------------------------------------
# 2/3. non-destructive environment and secret updates
# ---------------------------------------------------------------------------


def test_no_destructive_set_env_or_set_secrets_anywhere_in_the_script():
    assert "--set-env-vars" not in SCRIPT
    assert "--set-secrets" not in SCRIPT
    assert "--clear-env-vars" not in SCRIPT
    assert "--clear-secrets" not in SCRIPT
    assert "--remove-env-vars" not in SCRIPT
    assert "--remove-secrets" not in SCRIPT


def test_api_deploy_uses_non_destructive_update_flags():
    block = _api_deploy_block()
    assert "--update-env-vars" in block
    assert "--update-secrets" in block
    assert "--set-env-vars" not in block
    assert "--set-secrets" not in block


def test_worker_deploy_uses_non_destructive_update_flags():
    block = _worker_deploy_block()
    assert "--update-env-vars" in block
    assert "--update-secrets" in block
    assert "--set-env-vars" not in block
    assert "--set-secrets" not in block


def test_pre_existing_bindings_are_snapshotted_and_verified_after_deploy():
    # A snapshot is taken before the build/deploy commands run...
    snapshot_index = SCRIPT.index("API_BINDINGS_BEFORE=")
    assert snapshot_index < SCRIPT.index("gcloud builds submit")
    assert SCRIPT.index("WORKER_BINDINGS_BEFORE=") < SCRIPT.index("gcloud builds submit")
    # ...and compared afterwards, failing the deployment on any lost binding.
    assert 'assert_bindings_preserved "API service' in SCRIPT
    assert 'assert_bindings_preserved "Worker job' in SCRIPT
    assert "lost pre-existing environment/secret bindings" in SCRIPT
    assert SCRIPT.index('assert_bindings_preserved "API service') > SCRIPT.index("gcloud run deploy ")


def test_api_env_vars_use_alternate_delimiter_preserving_comma_separated_cors():
    assert 'ENV_VAR_DELIMITER="@"' in SCRIPT
    assert "printf '^%s^%s' \"$ENV_VAR_DELIMITER\"" in SCRIPT
    assert '--update-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}")"' in _api_deploy_block()
    assert '--update-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}")"' in _worker_deploy_block()
    assert '"ALLOWED_CORS_ORIGINS=${ALLOWED_CORS_ORIGINS:-}"' in _array_literal("API_ENV_VARS")


def test_cors_validation_rejects_selected_alternate_delimiter():
    assert 'ENV_VAR_DELIMITER="@"' in SCRIPT
    assert "ALLOWED_CORS_ORIGINS must not contain the gcloud env-var delimiter" in SCRIPT
    assert '[[ "$origin" == *"$ENV_VAR_DELIMITER"* ]]' in SCRIPT


# ---------------------------------------------------------------------------
# 4. worker first, never executed
# ---------------------------------------------------------------------------


def test_worker_job_is_deployed_before_the_api_service():
    assert SCRIPT.index("gcloud run jobs deploy ") < SCRIPT.index("gcloud run deploy ")
    builds = [line for line in SCRIPT.splitlines() if line.startswith("gcloud builds submit")]
    assert len(builds) == 2
    assert "cloudbuild-worker.yaml" in builds[0]
    assert "cloudbuild-api.yaml" in builds[1]


def test_deployment_never_executes_the_worker_job():
    assert "gcloud run jobs execute" not in SCRIPT
    assert "jobs run " not in SCRIPT
    assert "POST /runs" not in SCRIPT
    # Execution count is compared before and after the deployment.
    assert "WORKER_EXECUTIONS_BEFORE=$(worker_execution_count)" in SCRIPT
    assert 'verify_no_worker_execution "$WORKER_EXECUTIONS_BEFORE" "$(worker_execution_count)"' in SCRIPT
    assert "Deployment must never execute the worker." in SCRIPT


def test_check_mode_exits_before_build_deploy_iam_or_worker_execution():
    check_block_start = SCRIPT.index('if [[ "$DEPLOY_MODE" == "check" ]]')
    apply_start = SCRIPT.index("gcloud builds submit")
    check_block = SCRIPT[check_block_start:apply_start]
    assert "exit 0" in check_block
    assert "gcloud builds submit" not in check_block
    assert "gcloud run jobs deploy" not in check_block
    assert "gcloud run deploy" not in check_block
    assert "add-iam-policy-binding" not in check_block


# ---------------------------------------------------------------------------
# 5. Stage A execution flags
# ---------------------------------------------------------------------------


def test_stage_a_execution_flags_are_all_pinned_false():
    flags = _array_literal("STAGE_A_EXECUTION_FLAGS")
    for name in EXECUTION_FLAGS:
        assert f"{name}=false" in flags, f"{name} must be pinned false for Stage A"
        assert f"{name}=true" not in SCRIPT


def test_api_and_worker_both_receive_every_stage_a_flag():
    for array in ("API_ENV_VARS", "WORKER_ENV_VARS"):
        assert '"${STAGE_A_EXECUTION_FLAGS[@]}"' in _array_literal(array)


def test_api_env_vars_include_every_required_stage_a_variable():
    api_env = _array_literal("API_ENV_VARS")
    for required in (
        "ENVIRONMENT=production",
        "JOB_LAUNCHER=$JOB_LAUNCHER_MODE",
        "GCP_PROJECT_ID=$PROJECT_ID",
        "GCP_REGION=$REGION",
        "CLOUD_RUN_WORKER_JOB=$WORKER_JOB",
    ):
        assert required in api_env, f"API deployment must set {required}"


def test_worker_env_vars_include_every_required_stage_a_variable():
    worker_env = _array_literal("WORKER_ENV_VARS")
    for required in ("ENVIRONMENT=production", "GCP_PROJECT_ID=$PROJECT_ID", "GCP_REGION=$REGION"):
        assert required in worker_env, f"worker deployment must set {required}"


def test_job_launcher_mode_defaults_to_disabled():
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}" in SCRIPT


def test_job_launcher_mode_fails_closed_on_invalid_values():
    assert 'case "$JOB_LAUNCHER_MODE" in' in SCRIPT
    assert "  disabled|cloud_run) ;;" in SCRIPT
    assert "JOB_LAUNCHER_MODE must be 'disabled' or 'cloud_run'. Default is 'disabled'." in SCRIPT
    validation_index = SCRIPT.index('case "$JOB_LAUNCHER_MODE" in')
    assert validation_index < SCRIPT.index("gcloud run deploy ")


def test_api_deployment_uses_job_launcher_mode_not_hardcoded_cloud_run():
    assert "JOB_LAUNCHER=$JOB_LAUNCHER_MODE" in _array_literal("API_ENV_VARS")
    assert "JOB_LAUNCHER=cloud_run" not in SCRIPT


def test_cloud_run_requires_explicit_operator_override():
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-cloud_run}" not in SCRIPT
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}" in SCRIPT
    assert '[[ "$DEPLOY_MODE" == "apply" && "$JOB_LAUNCHER_MODE" == "cloud_run" ]]' in SCRIPT
    assert "explicit operator override" in SCRIPT


def test_job_launcher_mode_and_release_sha_are_printed_in_targets():
    targets_start = SCRIPT.index("cat <<TARGETS")
    targets_end = SCRIPT.index("TARGETS", targets_start + len("cat <<TARGETS"))
    targets_block = SCRIPT[targets_start:targets_end]
    assert "Job launcher mode: $JOB_LAUNCHER_MODE" in targets_block
    assert "Release SHA (full): $RELEASE_SHA" in targets_block
    assert "--update-env-vars / --update-secrets" in targets_block


# ---------------------------------------------------------------------------
# 6. KIMI_API_KEY stays worker-only
# ---------------------------------------------------------------------------


def test_api_secret_bindings_never_include_the_provider_key():
    api_secrets = _array_literal("API_SECRETS")
    assert "KIMI_API_KEY" not in api_secrets
    assert "MOONSHOT_API_KEY" not in api_secrets


def test_api_deploy_command_never_references_the_provider_key():
    assert "KIMI_API_KEY" not in _api_deploy_block()


def test_worker_secret_bindings_still_include_the_provider_key():
    assert "KIMI_API_KEY=KIMI_API_KEY:latest" in _array_literal("WORKER_SECRETS")


def test_provider_key_absence_on_the_api_is_verified_after_deployment():
    assert 'verify_secret_absent "API service' in SCRIPT
    assert "KIMI_API_KEY" in SCRIPT.split("verify_secret_absent \"API service", 1)[1].splitlines()[0]
    assert "must not be bound to secret" in SCRIPT


def test_supabase_secret_manager_mapping_preserves_service_role_env_contract():
    assert "SUPABASE_SECRET_KEY" in SCRIPT
    assert "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" in SCRIPT
    assert "gcloud secrets versions access" not in SCRIPT


# ---------------------------------------------------------------------------
# 7. separate runtime identities
# ---------------------------------------------------------------------------


def test_api_and_worker_service_accounts_default_to_distinct_identities():
    assert f"API_SERVICE_ACCOUNT=${{API_SERVICE_ACCOUNT:-{API_SA}}}" in SCRIPT
    assert f"WORKER_SERVICE_ACCOUNT=${{WORKER_SERVICE_ACCOUNT:-{WORKER_SA}}}" in SCRIPT
    assert API_SA != WORKER_SA
    assert "SERVICE_ACCOUNT=${SERVICE_ACCOUNT:-" not in SCRIPT


def test_preflight_fails_when_identities_are_equal_and_verifies_both_exist():
    assert '[[ "$API_SERVICE_ACCOUNT" == "$WORKER_SERVICE_ACCOUNT" ]]' in SCRIPT
    assert "must be distinct identities" in SCRIPT
    assert 'gcloud iam service-accounts describe "$API_SERVICE_ACCOUNT"' in SCRIPT
    assert 'gcloud iam service-accounts describe "$WORKER_SERVICE_ACCOUNT"' in SCRIPT


def test_worker_job_deploys_with_worker_identity():
    block = _worker_deploy_block()
    assert '--service-account "$WORKER_SERVICE_ACCOUNT"' in block
    assert '--service-account "$API_SERVICE_ACCOUNT"' not in block


def test_api_service_deploys_with_api_identity():
    block = _api_deploy_block()
    assert '--service-account "$API_SERVICE_ACCOUNT"' in block
    assert '--service-account "$WORKER_SERVICE_ACCOUNT"' not in block


def test_worker_job_uses_only_job_scoped_executor_with_overrides_role():
    assert 'gcloud run jobs add-iam-policy-binding "$WORKER_JOB"' in SCRIPT
    assert "--role roles/run.jobsExecutorWithOverrides" in SCRIPT
    assert "projects add-iam-policy-binding" not in SCRIPT
    assert "--role roles/owner" not in SCRIPT.lower()
    assert "--role roles/editor" not in SCRIPT.lower()
    assert "--role roles/run.admin" not in SCRIPT.lower()


def test_worker_execution_does_not_grant_invoker_role():
    assert "roles/run.invoker" not in _iam_binding_block()


def test_launcher_permission_is_granted_to_api_identity_not_worker():
    block = _iam_binding_block()
    assert "--role roles/run.jobsExecutorWithOverrides" in block
    assert '--member "serviceAccount:$API_SERVICE_ACCOUNT"' in block
    assert '--member "serviceAccount:$WORKER_SERVICE_ACCOUNT"' not in block


# ---------------------------------------------------------------------------
# 8. post-deployment verification
# ---------------------------------------------------------------------------


def test_every_required_verification_runs_after_deployment():
    tail = SCRIPT[SCRIPT.index("gcloud run deploy ") :]
    for call in (
        'verify_image_digest "Worker job',
        'verify_image_digest "API service',
        'verify_service_account "Worker job',
        'verify_service_account "API service',
        'verify_env_names "Worker job',
        'verify_env_names "API service',
        'verify_secret_refs "Worker job',
        'verify_secret_refs "API service',
        'verify_stage_a_flags "Worker job',
        'verify_stage_a_flags "API service',
        "verify_no_public_access job",
        "verify_no_public_access service",
        "verify_no_worker_execution",
    ):
        assert call in tail, f"post-deployment verification missing: {call}"


def test_image_verification_compares_the_registry_digest_and_release_sha():
    assert "gcloud artifacts docker images describe" in SCRIPT
    assert "image_summary.digest" in SCRIPT
    assert "does not resolve to the release digest" in SCRIPT
    assert 'could not be tied to release SHA $RELEASE_SHA' in SCRIPT


def test_private_ingress_verification_rejects_public_principals():
    assert "gcloud run services get-iam-policy" in SCRIPT
    assert "gcloud run jobs get-iam-policy" in SCRIPT
    assert '"(allUsers|allAuthenticatedUsers)"' in SCRIPT
    assert "must stay private" in SCRIPT
    assert "--no-allow-unauthenticated" in _api_deploy_block()
    assert "--allow-unauthenticated" not in SCRIPT.replace("--no-allow-unauthenticated", "")


def test_verification_reports_names_and_references_but_never_secret_values():
    # Only the non-secret Stage A flags are ever read by value.
    assert 'binding_report() {' in SCRIPT
    assert 'python3 -c "$CONTAINER_REPORT_PY" JOB_LAUNCHER "${STAGE_A_FLAG_NAMES[@]}"' in SCRIPT
    assert "allow_values = set(sys.argv[1:])" in SCRIPT
    assert "gcloud secrets versions access" not in SCRIPT
