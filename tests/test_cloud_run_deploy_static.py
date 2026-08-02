"""Static safety proofs for scripts/deploy/cloud-run.sh.

These assertions encode the production hardening contract for the Cloud Run
deployment: immutable full-SHA image tags, non-destructive environment and
secret updates, Stage A execution flags pinned off, no provider key on any
runtime, mandatory gateway identity configuration, separate runtime
identities, worker-before-API ordering, and post-deployment verification.
"""

from pathlib import Path

SCRIPT = Path("scripts/deploy/cloud-run.sh").read_text()
CONTRACT = Path("scripts/deploy/deployment-contract.sh").read_text()
# The contract is sourced by the script, so a value defined there is as
# binding as one written inline.
SOURCES = SCRIPT + CONTRACT

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
    text = SCRIPT if f"{name}=(" in SCRIPT else CONTRACT
    start = text.index(f"{name}=(")
    end = text.index("\n)", start)
    return text[start:end]


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
    assert (
        'API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY'
        '/$MILO_API_IMAGE_REPO:$RELEASE_SHA"'
    ) in SCRIPT
    assert (
        'WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY'
        '/$MILO_WORKER_IMAGE_REPO:$RELEASE_SHA"'
    ) in SCRIPT
    assert ":$SHORT_SHA" not in SCRIPT


def test_image_repository_paths_come_from_the_shared_contract():
    """One canonical repository path, shared with the plan generator."""
    assert 'MILO_API_IMAGE_REPO="api"' in CONTRACT
    assert 'MILO_WORKER_IMAGE_REPO="worker"' in CONTRACT
    assert 'source "$SCRIPT_DIR/deployment-contract.sh"' in SCRIPT


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


def test_preservation_compares_full_binding_identity_not_just_names():
    """A remapped secret keeps its name while changing what it reads."""
    identities = SCRIPT.split("binding_identities() {", 1)[1].split("\n}", 1)[0]
    assert '$1 == "env"    { print "env\\t" $2 }' in identities
    assert '$1 == "secret" { print "secret\\t" $2 "\\t" $3 }' in identities
    assert "was REMAPPED from" in SCRIPT
    assert "no secret reference removed or remapped" in SCRIPT
    # The before/after snapshots use the same full-identity comparison.
    assert "API_BINDINGS_BEFORE=$(binding_identities" in SCRIPT
    assert "WORKER_BINDINGS_BEFORE=$(binding_identities" in SCRIPT
    assert "binding_names" not in SCRIPT


def test_api_env_vars_use_alternate_delimiter_preserving_comma_separated_cors():
    assert 'ENV_VAR_DELIMITER="$MILO_ENV_VAR_DELIMITER"' in SCRIPT
    assert "printf '^%s^%s' \"$ENV_VAR_DELIMITER\"" in SCRIPT
    assert '--update-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}")"' in _api_deploy_block()
    assert '--update-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}")"' in _worker_deploy_block()
    assert '"ALLOWED_CORS_ORIGINS=${ALLOWED_CORS_ORIGINS:-}"' in _array_literal("API_ENV_VARS")


def test_cors_validation_rejects_selected_alternate_delimiter():
    assert 'MILO_ENV_VAR_DELIMITER=";"' in CONTRACT
    assert "ALLOWED_CORS_ORIGINS must not contain the gcloud env-var delimiter" in SCRIPT
    assert '[[ "$origin" == *"$ENV_VAR_DELIMITER"* ]]' in SCRIPT


def test_the_delimiter_cannot_be_a_character_that_appears_in_an_identity():
    """Service account emails all contain '@'; it cannot be the delimiter."""
    assert 'MILO_ENV_VAR_DELIMITER="@"' not in CONTRACT
    delimiter = CONTRACT.split('MILO_ENV_VAR_DELIMITER="', 1)[1][0]
    assert delimiter not in "@."


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
    flags = _array_literal("MILO_STAGE_A_EXECUTION_FLAGS")
    for name in EXECUTION_FLAGS:
        assert f"{name}=false" in flags, f"{name} must be pinned false for Stage A"
        assert f"{name}=true" not in SOURCES
    # The script takes the canonical list rather than keeping its own copy.
    assert 'STAGE_A_EXECUTION_FLAGS=("${MILO_STAGE_A_EXECUTION_FLAGS[@]}")' in SCRIPT


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
        "MILO_GATEWAY_AUDIENCE=$MILO_GATEWAY_AUDIENCE",
        "MILO_APPROVED_GATEWAY_IDENTITIES=$MILO_APPROVED_GATEWAY_IDENTITIES",
    ):
        assert required in api_env, f"API deployment must set {required}"


# ---------------------------------------------------------------------------
# 5b. gateway identity is required even while execution is disabled
# ---------------------------------------------------------------------------


def test_gateway_identity_is_required_before_anything_is_built():
    assert "require_gateway_identity_config" in SCRIPT
    assert "MILO_GATEWAY_AUDIENCE must be set" in SCRIPT
    assert "MILO_APPROVED_GATEWAY_IDENTITIES must list" in SCRIPT
    guard = SCRIPT.index("  require_gateway_identity_config")
    assert guard < SCRIPT.index("gcloud builds submit")


def test_gateway_identity_values_are_validated_not_merely_present():
    assert "is not a service account email" in SCRIPT
    assert "MILO_APPROVED_GATEWAY_IDENTITIES must not contain '*'" in SCRIPT
    assert "MILO_APPROVED_GATEWAY_IDENTITIES contains an empty entry." in SCRIPT


def test_gateway_variables_are_deployed_not_left_to_existing_configuration():
    """Preservation keeps unknown values; it does not supply approved ones."""
    for name in ("MILO_GATEWAY_AUDIENCE", "MILO_APPROVED_GATEWAY_IDENTITIES"):
        assert name in _array_literal("API_ENV_VARS")
        assert name in _array_literal("MILO_API_REQUIRED_ENV_NAMES")


def test_deployed_gateway_values_are_verified_not_merely_present():
    """A stale audience left on the service would pass a presence check."""
    tail = SCRIPT[SCRIPT.index("gcloud run deploy ") :]
    assert 'verify_gateway_identity "API service' in tail
    assert "instead of the approved" in SCRIPT
    assert "no caller would be verifiable" in SCRIPT


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
# 6. no provider key on ANY runtime during Stage A
# ---------------------------------------------------------------------------


def test_no_provider_key_is_bound_to_either_resource():
    for array in ("API_SECRETS", "WORKER_SECRETS", "API_ENV_VARS", "WORKER_ENV_VARS"):
        literal = _array_literal(array)
        for key in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
            assert key not in literal, f"{array} must not bind {key} during Stage A"


def test_provider_key_is_not_a_stage_a_prerequisite():
    """Requiring the secret to exist would gate Stage A on a Stage C artifact."""
    required = SCRIPT.split("REQUIRED_SECRETS=(", 1)[1].split(")", 1)[0]
    assert "KIMI_API_KEY" not in required
    assert "MOONSHOT_API_KEY" not in required
    assert "SUPABASE_SECRET_KEY" in required


def test_neither_deploy_command_references_a_provider_key():
    for block in (_api_deploy_block(), _worker_deploy_block()):
        assert "KIMI_API_KEY" not in block
        assert "MOONSHOT_API_KEY" not in block


def test_a_reintroduced_provider_key_binding_fails_preflight():
    assert "require_no_provider_key_bindings" in SCRIPT
    guard = SCRIPT.index("  require_no_provider_key_bindings")
    assert guard < SCRIPT.index("gcloud builds submit")
    assert "must not be bound during Stage A" in SCRIPT


def test_provider_key_absence_is_verified_on_both_resources_after_deployment():
    tail = SCRIPT[SCRIPT.index("gcloud run deploy ") :]
    assert 'verify_no_provider_key "API service' in tail
    assert 'verify_no_provider_key "Worker job' in tail
    assert "Stage A binds no provider key to any runtime" in SCRIPT
    # Both a secret binding and a literal value are caught.
    assert '$1 == "env" || $1 == "secret" { print $2 }' in SCRIPT


def test_supabase_secret_manager_mapping_preserves_service_role_env_contract():
    assert "SUPABASE_SECRET_KEY" in SCRIPT
    assert "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:$MILO_SECRET_VERSION" in SCRIPT
    assert 'MILO_SECRET_VERSION="latest"' in CONTRACT
    assert "gcloud secrets versions access" not in SCRIPT


def test_api_and_worker_carry_identical_supabase_and_upstash_bindings():
    api = _array_literal("API_SECRETS")
    worker = _array_literal("WORKER_SECRETS")
    for binding in (
        "SUPABASE_URL=SUPABASE_URL:$MILO_SECRET_VERSION",
        "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:$MILO_SECRET_VERSION",
        "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:$MILO_SECRET_VERSION",
        "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:$MILO_SECRET_VERSION",
    ):
        assert binding in api
        assert binding in worker


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
        'verify_no_provider_key "Worker job',
        'verify_no_provider_key "API service',
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
    assert 'python3 -c "$CONTAINER_REPORT_PY" \\' in SCRIPT
    assert 'JOB_LAUNCHER "${STAGE_A_FLAG_NAMES[@]}" "${GATEWAY_IDENTITY_VAR_NAMES[@]}"' in SCRIPT
    assert "allow_values = set(sys.argv[1:])" in SCRIPT
    assert "gcloud secrets versions access" not in SCRIPT
