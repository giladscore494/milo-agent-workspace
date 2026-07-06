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
