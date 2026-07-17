from pathlib import Path

SCRIPT = Path("scripts/deploy/cloud-run.sh").read_text()


def test_worker_job_uses_only_job_scoped_executor_with_overrides_role():
    assert "gcloud run jobs add-iam-policy-binding \"$WORKER_JOB\"" in SCRIPT
    assert "--role roles/run.jobsExecutorWithOverrides" in SCRIPT
    assert "run jobs add-iam-policy-binding" in SCRIPT
    assert "projects add-iam-policy-binding" not in SCRIPT
    assert "--role roles/owner" not in SCRIPT.lower()
    assert "--role roles/editor" not in SCRIPT.lower()
    assert "--role roles/run.admin" not in SCRIPT.lower()


def test_worker_execution_does_not_grant_invoker_role():
    iam_binding_section = SCRIPT.split("gcloud run jobs add-iam-policy-binding", maxsplit=1)[1]
    iam_binding_section = iam_binding_section.split("gcloud run deploy", maxsplit=1)[0]
    assert "roles/run.invoker" not in iam_binding_section


def test_api_env_vars_use_alternate_delimiter_preserving_comma_separated_cors():
    assert 'ENV_VAR_DELIMITER="@"' in SCRIPT
    assert '--set-env-vars "^${ENV_VAR_DELIMITER}^ENVIRONMENT=production' in SCRIPT
    assert '${ENV_VAR_DELIMITER}ALLOWED_CORS_ORIGINS=$ALLOWED_CORS_ORIGINS"' in SCRIPT
    example = "https://app.example.com,https://admin.example.com"
    rendered = (
        "^@^ENVIRONMENT=production"
        "@JOB_LAUNCHER=cloud_run"
        "@GCP_PROJECT_ID=project"
        "@GCP_REGION=region"
        "@CLOUD_RUN_WORKER_JOB=milo-agent-worker"
        f"@ALLOWED_CORS_ORIGINS={example}"
    )
    assert f"ALLOWED_CORS_ORIGINS={example}" in rendered
    assert "https://app.example.com,https://admin.example.com" in rendered


def test_cors_validation_rejects_selected_alternate_delimiter():
    assert 'ENV_VAR_DELIMITER="@"' in SCRIPT
    assert 'ALLOWED_CORS_ORIGINS must not contain the gcloud env-var delimiter' in SCRIPT
    assert '[[ "$origin" == *"$ENV_VAR_DELIMITER"* ]]' in SCRIPT


def test_check_mode_exits_before_build_deploy_iam_or_worker_execution():
    check_block_start = SCRIPT.index('if [[ "$DEPLOY_MODE" == "check" ]]')
    apply_start = SCRIPT.index("gcloud builds submit")
    check_block = SCRIPT[check_block_start:apply_start]
    assert "exit 0" in check_block
    assert "gcloud builds submit" not in check_block
    assert "gcloud run jobs deploy" not in check_block
    assert "gcloud run deploy" not in check_block
    assert "add-iam-policy-binding" not in check_block
    assert "gcloud run jobs execute" not in SCRIPT
    assert "POST /runs" not in SCRIPT


def test_supabase_secret_manager_mapping_preserves_service_role_env_contract():
    assert "SUPABASE_SECRET_KEY" in SCRIPT
    assert "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" in SCRIPT
    assert "gcloud secrets versions access" not in SCRIPT


API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
WORKER_SA = "milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"


def _worker_deploy_block():
    start = SCRIPT.index("gcloud run jobs deploy")
    end = SCRIPT.index("gcloud run jobs add-iam-policy-binding")
    return SCRIPT[start:end]


def _iam_binding_block():
    start = SCRIPT.index("gcloud run jobs add-iam-policy-binding")
    end = SCRIPT.index("gcloud run deploy")
    return SCRIPT[start:end]


def _api_deploy_block():
    start = SCRIPT.index("gcloud run deploy")
    return SCRIPT[start:]


def test_api_and_worker_service_accounts_default_to_distinct_identities():
    assert f"API_SERVICE_ACCOUNT=${{API_SERVICE_ACCOUNT:-{API_SA}}}" in SCRIPT
    assert f"WORKER_SERVICE_ACCOUNT=${{WORKER_SERVICE_ACCOUNT:-{WORKER_SA}}}" in SCRIPT
    assert API_SA != WORKER_SA
    # The legacy single-identity variable must be gone.
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


def test_launcher_permission_is_granted_to_api_identity_not_worker():
    block = _iam_binding_block()
    assert "--role roles/run.jobsExecutorWithOverrides" in block
    assert '--member "serviceAccount:$API_SERVICE_ACCOUNT"' in block
    assert '--member "serviceAccount:$WORKER_SERVICE_ACCOUNT"' not in block


def test_api_deployment_does_not_reference_kimi_api_key():
    block = _api_deploy_block()
    assert "KIMI_API_KEY" not in block


def test_worker_job_still_references_kimi_api_key():
    block = _worker_deploy_block()
    assert "KIMI_API_KEY=KIMI_API_KEY:latest" in block


def test_job_launcher_mode_defaults_to_disabled():
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}" in SCRIPT


def test_job_launcher_mode_fails_closed_on_invalid_values():
    # Only the two allowed values may pass validation; anything else must fail.
    assert 'case "$JOB_LAUNCHER_MODE" in' in SCRIPT
    assert "  disabled|cloud_run) ;;" in SCRIPT
    assert "JOB_LAUNCHER_MODE must be 'disabled' or 'cloud_run'. Default is 'disabled'." in SCRIPT
    # The validation must run before the API deploy that consumes the value.
    validation_index = SCRIPT.index('case "$JOB_LAUNCHER_MODE" in')
    api_deploy_index = SCRIPT.index("gcloud run deploy")
    assert validation_index < api_deploy_index


def test_api_deployment_uses_job_launcher_mode_not_hardcoded_cloud_run():
    block = _api_deploy_block()
    assert "JOB_LAUNCHER=$JOB_LAUNCHER_MODE" in block
    assert "JOB_LAUNCHER=cloud_run" not in block


def test_cloud_run_requires_explicit_operator_override():
    # cloud_run is never the default: it appears only via the configurable
    # variable and the apply-mode override warning, never as the shipped value.
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-cloud_run}" not in SCRIPT
    assert "JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}" in SCRIPT
    # An explicit override to cloud_run in apply mode emits a visible warning.
    assert '[[ "$DEPLOY_MODE" == "apply" && "$JOB_LAUNCHER_MODE" == "cloud_run" ]]' in SCRIPT
    assert "explicit operator override" in SCRIPT


def test_job_launcher_mode_is_printed_in_targets():
    targets_start = SCRIPT.index("cat <<TARGETS")
    targets_end = SCRIPT.index("TARGETS", targets_start + len("cat <<TARGETS"))
    targets_block = SCRIPT[targets_start:targets_end]
    assert "Job launcher mode: $JOB_LAUNCHER_MODE" in targets_block
