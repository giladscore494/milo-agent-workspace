"""Production configuration validation tests."""

import subprocess
import sys
from pathlib import Path

import pytest

from backend.production_config import validate, validate_production_config

REPO = Path(__file__).resolve().parents[1]

BASE_PROD = {
    "ENVIRONMENT": "production",
    "SUPABASE_URL": "https://real.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "placeholder-not-a-real-key",
    "ALLOWED_CORS_ORIGINS": "https://app.example.com",
    "UPSTASH_REDIS_REST_URL": "https://redis.example",
    "UPSTASH_REDIS_REST_TOKEN": "token",
}


def codes(report):
    return {issue.code for issue in report.issues}


def test_clean_production_config_has_no_errors():
    report = validate(BASE_PROD)
    assert report.ok(), [i.message for i in report.errors]


def test_wildcard_cors_is_rejected():
    report = validate({**BASE_PROD, "ALLOWED_CORS_ORIGINS": "*"})
    assert "CORS_WILDCARD" in codes(report)
    assert not report.ok()


def test_secret_in_next_public_is_rejected():
    report = validate({**BASE_PROD, "NEXT_PUBLIC_SUPABASE_ANON_KEY": "sb_secret_abc123"})
    assert "PUBLIC_CONTAINS_SECRET" in codes(report)


def test_production_requires_shared_rate_limiter():
    env = {k: v for k, v in BASE_PROD.items() if not k.startswith("UPSTASH")}
    report = validate(env)
    assert "PROD_MEMORY_RATE_LIMITER" in codes(report)


def test_execution_without_budget_is_error_in_production():
    report = validate({**BASE_PROD, "MILO_ENABLE_RUN_CREATION": "true"})
    assert "EXECUTION_WITHOUT_BUDGET" in codes(report)
    assert not report.ok()


def test_execution_with_full_budget_is_ok():
    env = {
        **BASE_PROD,
        "MILO_ENABLE_RUN_CREATION": "true",
        "MILO_MAX_MODEL_CALLS_PER_RUN": "40",
        "MILO_MAX_TOTAL_TOKENS_PER_RUN": "200000",
        "MILO_MAX_ESTIMATED_COST_PER_RUN": "5",
        "MILO_MAX_RUN_DURATION_SECONDS": "1800",
        "MILO_MAX_RETRIES": "3",
    }
    report = validate(env)
    assert "EXECUTION_WITHOUT_BUDGET" not in codes(report)
    assert report.ok(), [i.message for i in report.errors]


def test_paid_execution_requires_provider_key_and_budget():
    report = validate({**BASE_PROD, "MILO_ENABLE_PAID_EXECUTION": "true"})
    assert "PAID_WITHOUT_PROVIDER_KEY" in codes(report)
    assert "PAID_WITHOUT_BUDGET" in codes(report)


def test_worker_mutations_require_service_auth_config():
    report = validate({**BASE_PROD, "MILO_ENABLE_EXECUTION_CONTROL": "true"})
    assert "WORKER_AUTH_AUDIENCE_MISSING" in codes(report)
    assert "WORKER_ALLOWLIST_EMPTY" in codes(report)


def test_public_ui_without_backend_execution_warns_not_errors():
    report = validate({**BASE_PROD, "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI": "true"})
    assert "UI_WITHOUT_BACKEND_EXECUTION" in codes(report)
    assert report.ok()  # warning, not an error: UI renders a disabled state


def test_local_environment_only_warns():
    env = {"ENVIRONMENT": "local", "MILO_ENABLE_RUN_CREATION": "true", "ALLOWED_CORS_ORIGINS": "http://localhost:3000"}
    report = validate(env)
    assert report.warnings
    assert report.ok()  # local never hard-fails on budgets
    # And validate_production_config does not raise outside production.
    validate_production_config(env)


def test_validate_production_config_raises_in_production_on_error():
    with pytest.raises(RuntimeError):
        validate_production_config({**BASE_PROD, "ALLOWED_CORS_ORIGINS": "*"})


def test_static_unsafe_default_scanner_passes_on_repo():
    result = subprocess.run([sys.executable, "scripts/check_unsafe_defaults.py"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_execution_flags_default_off_in_repo_config():
    # The scanner is the enforcement; this asserts the intent explicitly.
    result = subprocess.run([sys.executable, "scripts/check_unsafe_defaults.py"], cwd=REPO, capture_output=True, text=True)
    assert "all execution flags default-off" in result.stdout
