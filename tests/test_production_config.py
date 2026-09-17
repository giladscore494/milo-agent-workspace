"""Production configuration validation tests."""

import subprocess
import sys
from pathlib import Path

import pytest

from backend.production_config import (
    EXECUTION_FLAGS,
    ProductionConfigError,
    supabase_url_matches_project_ref,
    validate,
    validate_production_config,
)


def test_startup_failure_has_stable_sanitized_codes(capsys):
    env = {**BASE_PROD, "MILO_ENABLE_EXECUTION_CONTROL": "true"}
    env.pop("MILO_WORKER_AUDIENCE", None)
    env.pop("MILO_APPROVED_WORKER_IDENTITIES", None)
    with pytest.raises(ProductionConfigError) as excinfo:
        validate_production_config(env)
    assert str(excinfo.value) == "CONFIG_VALIDATION_FAILED[WORKER_ALLOWLIST_EMPTY,WORKER_AUTH_AUDIENCE_MISSING]"
    diagnostic = capsys.readouterr().err
    assert '"event": "production_config_validation_failed"' in diagnostic
    assert "WORKER_AUTH_AUDIENCE_MISSING" in diagnostic
    assert PROD_PROJECT_REF not in diagnostic
    assert "service-secret" not in diagnostic

REPO = Path(__file__).resolve().parents[1]

# Synthetic 20-character Supabase project refs. The real production ref is
# operator configuration supplied from the approved manifest at deploy time
# and is deliberately absent from this repository.
PROD_PROJECT_REF = "abcdefghijklmnopqrst"
OTHER_PROJECT_REF = "zyxwvutsrqponmlkjihg"

BASE_PROD = {
    "ENVIRONMENT": "production",
    "SUPABASE_URL": f"https://{PROD_PROJECT_REF}.supabase.co",
    "MILO_EXPECTED_SUPABASE_PROJECT_REF": PROD_PROJECT_REF,
    "SUPABASE_SERVICE_ROLE_KEY": "placeholder-not-a-real-key",
    "ALLOWED_CORS_ORIGINS": "https://app.example.com",
    "UPSTASH_REDIS_REST_URL": "https://redis.example",
    "UPSTASH_REDIS_REST_TOKEN": "token",
    "MILO_GATEWAY_AUDIENCE": "https://milo-api.example.internal",
    "MILO_APPROVED_GATEWAY_IDENTITIES": "gateway@example.iam.gserviceaccount.com",
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


def test_production_requires_gateway_identity_configuration():
    env = {k: v for k, v in BASE_PROD.items() if not k.startswith("MILO_GATEWAY") and not k.startswith("MILO_APPROVED_GATEWAY")}
    report = validate(env)
    assert "GATEWAY_AUTH_MISSING" in codes(report)
    assert not report.ok()


def test_shared_gateway_and_worker_identity_is_rejected():
    env = {
        **BASE_PROD,
        "MILO_APPROVED_WORKER_IDENTITIES": "gateway@example.iam.gserviceaccount.com",
    }
    report = validate(env)
    assert "SHARED_GATEWAY_WORKER_IDENTITY" in codes(report)


def test_test_adapters_are_rejected_in_production():
    report = validate({**BASE_PROD, "CLOUD_RUN_AUTH_MODE": "e2e-test"})
    assert "TEST_ADAPTER_IN_PRODUCTION" in codes(report)
    report2 = validate({**BASE_PROD, "MILO_E2E_INPROCESS_WORKER": "true"})
    assert "TEST_ADAPTER_IN_PRODUCTION" in codes(report2)
    report3 = validate({**BASE_PROD, "MILO_WORKER_ENGINE": "mock"})
    assert "TEST_ADAPTER_IN_PRODUCTION" in codes(report3)


def test_release_scripts_are_not_exempt_from_unsafe_default_scanning():
    text = Path(REPO / "scripts" / "check_unsafe_defaults.py").read_text()
    assert "scripts/release/" not in text.split("ALLOWED_PREFIXES")[1].split(")")[0]


BASE_STAGING = {
    "ENVIRONMENT": "staging",
    "SUPABASE_URL": "https://cxlwavxvwgrfikkudtzf.supabase.co",
    "UPSTASH_REDIS_REST_URL": "https://milo-redis-shim-staging-501121602031.us-central1.run.app",
    "MILO_EXPECTED_SUPABASE_PROJECT_REF": "cxlwavxvwgrfikkudtzf",
    "MILO_EXPECTED_REDIS_HOST": "milo-redis-shim-staging-501121602031.us-central1.run.app",
}


def test_staging_with_matching_dependency_pins_is_clean():
    report = validate(BASE_STAGING)
    assert not [i for i in report.errors if i.code.startswith("STAGING_")]


def test_staging_without_dependency_pins_fails_closed():
    env = {k: v for k, v in BASE_STAGING.items() if not k.startswith("MILO_EXPECTED_")}
    report = validate(env)
    assert [i for i in report.errors if i.code == "STAGING_DEPENDENCY_UNPINNED"]
    with pytest.raises(RuntimeError, match="STAGING_DEPENDENCY_UNPINNED"):
        validate_production_config(env)


def test_staging_pointed_at_wrong_supabase_project_fails_closed():
    env = {**BASE_STAGING, "SUPABASE_URL": "https://someotherproject.supabase.co"}
    report = validate(env)
    assert [i for i in report.errors if i.code == "STAGING_DEPENDENCY_MISMATCH"]
    with pytest.raises(RuntimeError, match="STAGING_DEPENDENCY_MISMATCH") as excinfo:
        validate_production_config(env)
    # Fail-closed without echoing dependency values.
    assert "someotherproject" not in str(excinfo.value)


def test_staging_pointed_at_wrong_redis_host_fails_closed():
    env = {**BASE_STAGING, "UPSTASH_REDIS_REST_URL": "https://prod-db.upstash.io"}
    report = validate(env)
    assert [i for i in report.errors if i.code == "STAGING_DEPENDENCY_MISMATCH"]
    with pytest.raises(RuntimeError) as excinfo:
        validate_production_config(env)
    assert "prod-db" not in str(excinfo.value)


def test_production_does_not_raise_staging_codes():
    """Production has its own pin; it is never validated as staging."""
    report = validate(BASE_PROD)
    assert not [i for i in report.errors if i.code.startswith("STAGING_")]


# ---------------------------------------------------------------------------
# production Supabase target pinning
# ---------------------------------------------------------------------------
# Staging has refused a wrong Supabase project since it was built. Production
# did not: the project-ref pin was explicitly staging-only, so a production
# runtime handed the wrong SUPABASE_URL — a stale secret version, a restored
# snapshot's project, a copy-paste — started and wrote to it. These tests are
# the contract that it now refuses, as deliberately as staging does.


def test_production_with_a_matching_expected_ref_is_clean():
    report = validate(BASE_PROD)
    assert report.ok(), [i.message for i in report.errors]
    assert not [i for i in report.errors if i.code.startswith("PRODUCTION_DEPENDENCY")]


def test_production_without_an_expected_supabase_ref_fails_closed():
    env = {k: v for k, v in BASE_PROD.items() if k != "MILO_EXPECTED_SUPABASE_PROJECT_REF"}
    report = validate(env)
    assert "PRODUCTION_DEPENDENCY_UNPINNED" in codes(report)
    assert not report.ok()
    with pytest.raises(RuntimeError, match="PRODUCTION_DEPENDENCY_UNPINNED"):
        validate_production_config(env)


def test_production_pointed_at_a_different_supabase_project_fails_closed():
    env = {**BASE_PROD, "SUPABASE_URL": f"https://{OTHER_PROJECT_REF}.supabase.co"}
    report = validate(env)
    assert "PRODUCTION_DEPENDENCY_MISMATCH" in codes(report)
    with pytest.raises(RuntimeError, match="PRODUCTION_DEPENDENCY_MISMATCH"):
        validate_production_config(env)


def test_production_pin_failures_never_reveal_the_ref_or_the_observed_host(capsys):
    """The failure must be unambiguous without naming either project."""
    env = {**BASE_PROD, "SUPABASE_URL": f"https://{OTHER_PROJECT_REF}.supabase.co"}
    with pytest.raises(ProductionConfigError) as excinfo:
        validate_production_config(env)

    diagnostic = capsys.readouterr().err
    report = validate(env)
    messages = " ".join(i.message for i in report.issues)
    for stream in (str(excinfo.value), diagnostic, messages):
        assert PROD_PROJECT_REF not in stream
        assert OTHER_PROJECT_REF not in stream
        assert "supabase.co" not in stream
    # …while still naming the reason exactly.
    assert "PRODUCTION_DEPENDENCY_MISMATCH" in str(excinfo.value)
    assert "PRODUCTION_DEPENDENCY_MISMATCH" in diagnostic


def test_production_with_a_malformed_expected_ref_fails_closed():
    for bad in ("not-a-ref", "SHOUTING", "*", f"https://{PROD_PROJECT_REF}.supabase.co", "short", "  "):
        env = {**BASE_PROD, "MILO_EXPECTED_SUPABASE_PROJECT_REF": bad}
        report = validate(env)
        assert not report.ok(), bad
        assert {"PRODUCTION_DEPENDENCY_MALFORMED", "PRODUCTION_DEPENDENCY_UNPINNED"} & codes(report), bad


def test_production_with_a_missing_supabase_url_fails_closed():
    env = {k: v for k, v in BASE_PROD.items() if k != "SUPABASE_URL"}
    report = validate(env)
    assert "PRODUCTION_DEPENDENCY_UNPINNED" in codes(report)
    assert "MISSING_BACKEND_SETTING" in codes(report)
    with pytest.raises(RuntimeError):
        validate_production_config(env)


# The pin must validate the WHOLE URL, not merely its hostname. Matching the
# host alone accepted every one of these — each carries the expected host
# while being a different endpoint, or not a usable API base URL at all. A pin
# that accepts them is not pinning the target.
REJECTED_URLS = [
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/evil", id="path"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/rest/v1", id="api-path"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/?", id="empty-query"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co?foo=bar", id="query"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/?foo=bar", id="root-query"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co#fragment", id="fragment"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/#fragment", id="root-fragment"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co:444", id="explicit-port"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co:443", id="default-port-explicit"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co:bad", id="malformed-port"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co:", id="empty-port"),
    pytest.param(f"https://user@{PROD_PROJECT_REF}.supabase.co", id="userinfo"),
    pytest.param(f"https://user:pw@{PROD_PROJECT_REF}.supabase.co", id="credentials"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co.evil.test", id="suffix-smuggling"),
    pytest.param(f"https://evil.test/{PROD_PROJECT_REF}.supabase.co", id="ref-in-path"),
    pytest.param(f"https://{PROD_PROJECT_REF}.pooler.supabase.com", id="pooler"),
    pytest.param(f"postgresql://user@{PROD_PROJECT_REF}.supabase.co:5432/postgres", id="postgresql"),
    pytest.param(f"http://{PROD_PROJECT_REF}.supabase.co", id="http"),
    pytest.param(f"https://{OTHER_PROJECT_REF}.supabase.co", id="other-project"),
    pytest.param("https://db.internal.example", id="not-supabase"),
    pytest.param("not a url at all", id="not-a-url"),
    pytest.param("", id="empty"),
]

# The project's API base URL. Both forms are equivalent to this runtime, not
# merely similar: backend/config.py types SUPABASE_URL as a pydantic HttpUrl,
# which normalises both to `https://<ref>.supabase.co/`.
ACCEPTED_URLS = [
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co", id="root"),
    pytest.param(f"https://{PROD_PROJECT_REF}.supabase.co/", id="root-trailing-slash"),
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_production_rejects_every_url_form_it_cannot_prove(url):
    env = {**BASE_PROD, "SUPABASE_URL": url}
    report = validate(env)
    assert not report.ok(), url
    assert {"PRODUCTION_DEPENDENCY_MISMATCH", "PRODUCTION_DEPENDENCY_UNPINNED"} & codes(report), url


@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_production_accepts_the_hosted_root_url(url):
    report = validate({**BASE_PROD, "SUPABASE_URL": url})
    assert report.ok(), [i.message for i in report.errors]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_the_url_matcher_rejects_everything_but_the_hosted_root(url):
    assert not supabase_url_matches_project_ref(url, PROD_PROJECT_REF), url


@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_the_url_matcher_accepts_the_hosted_root(url):
    assert supabase_url_matches_project_ref(url, PROD_PROJECT_REF), url


def test_the_url_matcher_is_case_insensitive_on_scheme_and_host():
    """Hostnames are case-insensitive; the pin must not fail on casing alone."""
    assert supabase_url_matches_project_ref(
        f"HTTPS://{PROD_PROJECT_REF.upper()}.SUPABASE.CO", PROD_PROJECT_REF
    )


def test_the_url_matcher_never_raises_on_a_malformed_url():
    """A malformed URL is a mismatch, never an exception escaping validation.

    `:bad` is the important one: reading `urlparse(...).port` raises
    ValueError, so an implementation that reached for the port to reject it
    would crash instead of failing closed.
    """
    for url in ("http://[oops", f"https://{PROD_PROJECT_REF}.supabase.co:bad", "://", "https://"):
        assert supabase_url_matches_project_ref(url, PROD_PROJECT_REF) is False, url


def test_the_url_matcher_requires_an_expected_ref():
    """An empty expected ref must never make `https://.supabase.co` a match."""
    assert not supabase_url_matches_project_ref("https://.supabase.co", "")
    assert not supabase_url_matches_project_ref(f"https://{PROD_PROJECT_REF}.supabase.co", "")


def test_the_production_pin_does_not_weaken_staging():
    """Staging keeps its own codes and its own Redis pin."""
    assert not [i for i in validate(BASE_STAGING).errors if i.code.startswith("STAGING_")]
    wrong = {**BASE_STAGING, "SUPABASE_URL": f"https://{OTHER_PROJECT_REF}.supabase.co"}
    assert "STAGING_DEPENDENCY_MISMATCH" in codes(validate(wrong))
    assert "PRODUCTION_DEPENDENCY_MISMATCH" not in codes(validate(wrong))


def test_pinning_production_enables_no_execution_flag():
    """The pin is a refusal, not an activation."""
    report = validate(BASE_PROD)
    assert report.ok()
    for flag in EXECUTION_FLAGS:
        assert not BASE_PROD.get(flag), f"{flag} must not be set by the production baseline"
    # And a pinned production is still rejected the moment execution is on
    # without budgets — the pin changes nothing about staged activation.
    assert "EXECUTION_WITHOUT_BUDGET" in codes(validate({**BASE_PROD, "MILO_ENABLE_RUN_CREATION": "true"}))


# ---------------------------------------------------------------------------
# secret scan
# ---------------------------------------------------------------------------
# The scanner has NO allowlist, and in particular nothing is exempt because of
# what a file is named. An earlier revision exempted any match that was the
# tail of a real repository filename, to stop the `service_role` heuristic
# firing on the legitimate migration filename that MIGRATIONS.md must name
# exactly. That was too broad for a security scanner: filenames are
# contributor-controlled, so adding one file could switch off detection for
# credential-shaped text anywhere else in the tree.
#
# The false positive is fixed in the pattern instead — key material contains a
# long unbroken alphanumeric run; a snake_case filename does not.
#
# Every sample below is assembled at runtime rather than written literally, so
# this file does not itself become a finding.
_ROLE = "service" + "_role"
_PEM = "-----BEGIN " + "PRIVATE KEY" + "-----"
BENIGN_MIGRATION = "20260706192500_grant_" + _ROLE + "_schema_privileges.sql"


def _scanner():
    sys.path.insert(0, str(REPO / "scripts"))
    import secret_scan

    return secret_scan


def test_secret_scan_passes_on_the_repository():
    result = subprocess.run([sys.executable, "scripts/secret_scan.py"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret scan passed" in result.stdout


def test_secret_scan_still_catches_real_credential_material():
    scanner = _scanner()

    for leaked in (
        _ROLE + "_eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc",
        _ROLE + "." + "a" * 24,
        # A key sitting behind another word — the old pattern missed this one.
        _ROLE + "_key_eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "sk-" + "a" * 40,
        _PEM,
        f'SUPABASE_SERVICE_ROLE_KEY="{_ROLE}.' + "a" * 24 + '"',
    ):
        assert scanner.findings_for(leaked), f"scanner missed: {leaked[:16]}…"


def test_the_benign_migration_filename_is_not_credential_shaped():
    """It is allowed because of its SHAPE, not because a file by that name exists."""
    scanner = _scanner()

    assert (REPO / "supabase" / "migrations" / BENIGN_MIGRATION).exists()
    assert not scanner.findings_for(f"see `{BENIGN_MIGRATION}` for the grants")
    # …and it is really in the docs, where the strict-order check needs it.
    assert BENIGN_MIGRATION in (REPO / "docs" / "production-readiness" / "MIGRATIONS.md").read_text()


def test_a_credential_spelled_like_a_filename_is_still_caught():
    """Dressing key material up with a .sql suffix does not make it benign."""
    scanner = _scanner()

    assert scanner.findings_for(_ROLE + "_" + "a" * 24 + ".sql")


def _old_filename_exemption_would_suppress(secret: str, decoy_name: str) -> bool:
    """Would the REMOVED rule have exempted `secret` given a file `decoy_name`?

    The rule was: skip a match that is the tail of any real filename. This
    reproduces it so the tests below can prove they are exercising the actual
    hole, rather than a decoy that never exploited it (a filename ending in
    `.sql` does not, because the matched text stops before the extension).
    """
    return decoy_name.endswith(secret)


def test_no_filename_can_exempt_credential_text_elsewhere(tmp_path):
    """The adversarial case the filename-based exemption made possible.

    A contributor adds a file whose basename ENDS WITH credential-shaped text.
    Under the removed rule that text became exempt repository-wide, so the same
    string in a real file stopped being reported at all.
    """
    scanner = _scanner()
    secret = _ROLE + "_" + "a" * 24
    decoy = f"x_{secret}"

    # Precondition: this decoy really does exploit the removed rule.
    assert _old_filename_exemption_would_suppress(secret, decoy)

    (tmp_path / decoy).write_text("select 1;\n")
    (tmp_path / "config.env").write_text(f"SUPABASE_KEY={secret}\n")

    findings = scanner.scan(tmp_path)

    assert any(finding.endswith("config.env") for finding in findings), (
        f"a decoy filename suppressed a real finding: {findings}"
    )


@pytest.mark.parametrize(
    ("leak_file", "secret"),
    [
        pytest.param("jwt.env", _ROLE + "_eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc", id="service-role-jwt"),
        pytest.param("openai.env", "sk-" + "b" * 40, id="sk-key"),
        pytest.param("key.pem", _PEM, id="pem"),
    ],
)
def test_every_pattern_survives_a_decoy_filename(tmp_path, leak_file, secret):
    """No pattern may be switched off by naming a file after its match."""
    scanner = _scanner()
    decoy = f"x{secret}"
    assert _old_filename_exemption_would_suppress(secret, decoy)

    (tmp_path / decoy).write_text("select 1;\n")
    (tmp_path / leak_file).write_text(secret + "\n")

    findings = {Path(f).name for f in scanner.scan(tmp_path)}

    assert leak_file in findings, f"{leak_file} was not reported; findings={findings}"


def test_the_scanner_reports_a_clean_tree_as_clean(tmp_path):
    scanner = _scanner()
    (tmp_path / "notes.md").write_text(f"The migration is `{BENIGN_MIGRATION}`.\n")

    assert scanner.scan(tmp_path) == []
