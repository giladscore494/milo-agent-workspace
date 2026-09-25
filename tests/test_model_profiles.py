"""PR-R commit 1: the fail-closed Model Profile Registry."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, build_guarded_client_factory
from backend.model_profiles import (MODEL_NOT_ALLOWLISTED, MODEL_PROFILE_UNKNOWN, PROFILES,
                                    SWARM_MODEL_CONFIG_INVALID, ModelConfigError,
                                    UnknownModelProfile, get_profile,
                                    validate_swarm_model_contract)
from backend.production_config import validate


def test_registered_profiles_carry_the_verified_prices():
    k3, k26 = PROFILES["kimi-k3"], PROFILES["kimi-k2.6"]
    assert (k3.price_input_miss, k3.price_input_hit, k3.price_output) == (
        Decimal("3.00"), Decimal("0.30"), Decimal("15.00"))
    assert (k3.price_cache_write_5m, k3.price_cache_write_1h) == (Decimal("3.00"), Decimal("6.00"))
    assert k3.reasoning == "always" and k3.reasoning_control == "reasoning_effort"
    assert k3.output_cap_field == "max_completion_tokens"
    assert {"temperature", "top_p", "n", "presence_penalty", "frequency_penalty"} <= k3.forbidden_params
    # The k2.6 price correction: 0.60/2.50 -> 0.95/4.00, hit 0.16, no write charge.
    assert (k26.price_input_miss, k26.price_input_hit, k26.price_output) == (
        Decimal("0.95"), Decimal("0.16"), Decimal("4.00"))
    assert k26.price_cache_write_5m is None and k26.price_cache_write_1h is None
    assert k26.reasoning == "optional" and k26.reasoning_control == "thinking_toggle"


def test_unknown_model_has_no_profile_and_no_zero_price():
    for name in ("kimi", "kimi-k2", "gpt-4o", "", None):
        with pytest.raises(UnknownModelProfile) as caught:
            get_profile(name)
        assert caught.value.code == MODEL_PROFILE_UNKNOWN
    with pytest.raises(UnknownModelProfile):
        get_profile("unregistered-model").usage_cost(input_tokens=1000, output_tokens=1000)


def test_k26_price_correction_is_what_the_registry_charges():
    # 1M in + 1M out at 0.95 + 4.00; the stale table said 0.60 + 2.50.
    k26 = get_profile("kimi-k2.6")
    assert float(k26.usage_cost(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(4.95)
    assert float(k26.price_input_miss) == pytest.approx(0.95)
    assert float(k26.price_output) == pytest.approx(4.00)


def _guarded(tracker, calls):
    class Inner:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    return build_guarded_client_factory(tracker, lambda *_: Inner())("k", "u")


def test_guarded_call_with_unknown_model_is_refused_before_any_provider_request():
    records = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=5), kill_switch=lambda: True,
                            ledger_recorder=records.append)
    calls = []
    client = _guarded(tracker, calls)
    with pytest.raises(BudgetExceeded) as caught:
        client.chat.completions.create(model="kimi-k9", messages=[{"content": "x"}], max_tokens=10)
    assert caught.value.code == MODEL_PROFILE_UNKNOWN
    assert calls == []
    assert tracker.model_calls == 0 and tracker.actual_cost == 0
    assert records and records[-1]["decision"] == "rejected"
    assert records[-1]["rejection_reason"] == MODEL_PROFILE_UNKNOWN
    # The refusal is terminal: nothing further is admitted on this run.
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="kimi-k2.6", messages=[{"content": "x"}], max_tokens=10)
    assert calls == []


def test_swarm_model_contract_requires_profiles_and_both_roles_allowlisted():
    good = {"MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k3,kimi-k2.6",
            "MILO_COMMANDER_MODEL": "kimi-k3", "MILO_SWARM_WORKER_MODEL": "kimi-k2.6"}
    assert validate_swarm_model_contract(good, require_present=True) == (
        "kimi-k3", "kimi-k2.6", ("kimi-k3", "kimi-k2.6"))
    cases = [
        ({**good, "MILO_COMMANDER_MODEL": "kimi-k9",
          "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k9,kimi-k2.6"}, MODEL_PROFILE_UNKNOWN),
        ({**good, "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k3,kimi-k2.6,other"}, MODEL_PROFILE_UNKNOWN),
        # The worker model was never checked against the allowlist before.
        ({**good, "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k3"}, MODEL_NOT_ALLOWLISTED),
        ({**good, "MILO_SWARM_WORKER_MODEL": ""}, SWARM_MODEL_CONFIG_INVALID),
    ]
    for env, code in cases:
        with pytest.raises(ModelConfigError) as caught:
            validate_swarm_model_contract(env, require_present=True)
        assert caught.value.code == code
    # Shared validation checks only what is set: an API env carries none.
    assert validate_swarm_model_contract({}, require_present=False) == ("", "", ())


def test_paid_configuration_validation_refuses_an_unprofiled_swarm_model():
    report = validate({"MILO_ENABLE_PAID_EXECUTION": "true",
                       "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k9",
                       "MILO_COMMANDER_MODEL": "kimi-k9"})
    assert MODEL_PROFILE_UNKNOWN in {issue.code for issue in report.errors}


def test_paid_worker_boot_refuses_a_swarm_run_whose_model_has_no_profile(monkeypatch):
    """Worker boot in the paid posture: refused with a static code, zero calls."""
    from uuid import UUID

    from fastapi.testclient import TestClient

    from backend.dependencies import get_job_launcher, get_repository
    from backend.main import app
    from tests.test_swarm_v2_smoke_offline import (FakeKimiCompletions, InlineWorkerLauncher,
                                                   build_repo, create_run, patch_client,
                                                   swarm_env)

    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    launcher = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    try:
        completions = FakeKimiCompletions()
        patch_client(monkeypatch, completions)
        # Created under a valid configuration; the WORKER then boots with an
        # unprofiled Commander model (e.g. a new job revision).
        monkeypatch.setenv("MILO_COMMANDER_MODEL_ALLOWLIST", "kimi-k9,kimi-k2.6")
        monkeypatch.setenv("MILO_COMMANDER_MODEL", "kimi-k9")
        response = create_run(TestClient(app), conversation_id, idempotency_key="profile-0001")
        assert response.status_code == 202, response.text
        run = repo.get_run(UUID(response.json()["run_id"]))
    finally:
        app.dependency_overrides.clear()
    assert launcher.exit_codes == [0]
    assert run["status"] == "failed"
    assert run["error"]["code"] == MODEL_PROFILE_UNKNOWN
    assert completions.calls == []


@pytest.mark.parametrize("commander,allowlist,worker,verdict", [
    ("kimi-k3", "kimi-k3,kimi-k2.6", "kimi-k2.6", "[PASS] model-contract"),
    ("kimi-k9", "kimi-k9", "kimi-k2.6", "[BLOCKED] model-contract"),
    ("kimi-k3", "kimi-k3", "kimi-k2.6", "[BLOCKED] model-contract"),   # worker not allowlisted
])
def test_production_config_check_enforces_the_model_contract(tmp_path, commander, allowlist,
                                                             worker, verdict):
    import subprocess

    env_file = tmp_path / "worker.env"
    env_file.write_text(f"ENVIRONMENT=production\nMILO_COMMANDER_MODEL={commander}\n"
                        f"MILO_COMMANDER_MODEL_ALLOWLIST={allowlist}\n"
                        f"MILO_SWARM_WORKER_MODEL={worker}\n")
    result = subprocess.run(["bash", "scripts/release/check-production-config.sh",
                             "--env-file", str(env_file)],
                            capture_output=True, text=True, timeout=180)
    assert verdict in result.stdout + result.stderr
    # Values are never printed, only the verdict and the static code.
    assert "kimi-k9" not in result.stdout + result.stderr
