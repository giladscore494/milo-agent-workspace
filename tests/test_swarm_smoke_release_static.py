"""Offline regression checks for the manual Swarm V2 activation transaction."""
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/release/stage-c/activate-swarm-v2-smoke.sh"


def test_cleanup_is_armed_before_first_gcloud_mutation():
    text = SCRIPT.read_text()
    assert text.index("trap cleanup EXIT ERR INT TERM") < text.index("gcloud run services")


def test_activation_does_not_create_worker_execution_or_enable_execution_control():
    text = SCRIPT.read_text()
    assert "jobs execute" not in text
    assert "MILO_ENABLE_EXECUTION_CONTROL=${STAGE_C_ON}" not in text
    assert "MILO_ENABLE_EXECUTION_CONTROL=false" in text


def test_activation_builds_healthy_fail_closed_revision_before_worker_key_binding():
    text = SCRIPT.read_text()
    assert text.index("--no-traffic --update-env-vars") < text.index("--update-secrets=\"KIMI_API_KEY")
    assert text.index("update-traffic") < text.index("--update-secrets=\"KIMI_API_KEY")
    assert text.index("./kill-switch.sh") < text.index("--update-secrets=\"KIMI_API_KEY")


def test_failure_cleanup_preserves_original_status_and_removes_probe():
    text = SCRIPT.read_text()
    assert "original_status=$?" in text
    assert "gcloud run jobs delete" in text
    assert 'exit "${original_status}"' in text


def test_widened_activation_validates_worker_auth_before_mutation():
    text = SCRIPT.read_text()
    audience = text.index("MILO_WORKER_AUDIENCE missing")
    allowlist = text.index("MILO_APPROVED_WORKER_IDENTITIES missing")
    first_mutation = text.index("--no-traffic --update-env-vars")
    assert audience < first_mutation and allowlist < first_mutation
