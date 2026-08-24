"""Static and behavioral validation of the Swarm V2 smoke controller.

The previous production smoke attempt never reached the engine: its IAM
preflight piped gcloud JSON into a Python heredoc, and the pipe and the
heredoc competed for stdin. These tests pin the structural fixes (file-
argument parsers, temp-dir lifecycle with an EXIT trap, strict mode) and
cross-check the controller's expected environment contract against the
variable names the backend actually reads, so the two can never drift.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = REPO_ROOT / "scripts" / "release" / "swarm-v2-smoke"
CONTROLLER = SMOKE_DIR / "run-swarm-smoke.sh"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SMOKE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parse_env_contract = load("parse_env_contract")
parse_iam = load("parse_iam")
parse_executions = load("parse_executions")
parse_run_state = load("parse_run_state")


# --- controller shell hygiene ------------------------------------------------

def test_controller_bash_syntax_and_strict_mode():
    subprocess.run(["bash", "-n", str(CONTROLLER)], check=True)
    text = CONTROLLER.read_text()
    assert "set -euo pipefail" in text
    assert re.search(r"trap shutdown_guard EXIT", text)
    assert "trap 'exit 130' INT" in text
    assert "trap 'exit 143' TERM" in text
    assert "trap 'exit 129' HUP" in text
    assert "canonical_shutdown" in text
    assert "mktemp -d" in text


def test_controller_shellcheck_clean():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck unavailable in this environment")
    subprocess.run(["shellcheck", "-S", "warning", str(CONTROLLER)], check=True)


def test_no_parser_competes_for_stdin():
    """The recorded failure mode: `gcloud ... | python3 - <<HEREDOC`. Every
    parser invocation must read a file ARGUMENT, never stdin."""
    text = CONTROLLER.read_text()
    assert "python3 -" not in text.replace("python3 --", "")
    assert not re.search(r"\|\s*python3", text), "no JSON may be piped into python"
    assert not re.search(r"python3[^\n]*<<", text), "no heredoc may feed a python parser"
    for line in text.splitlines():
        if "python3" in line and "parse_" in line:
            assert "${WORKDIR}" in line or "${HERE}" in line, line


def test_parsers_have_no_stdin_reads():
    for helper in ("parse_env_contract", "parse_iam", "parse_executions", "parse_run_state"):
        source = (SMOKE_DIR / f"{helper}.py").read_text()
        assert "sys.stdin" not in source, f"{helper} must not read stdin"
        assert not re.search(r"(?<![\w.])input\(", source), f"{helper} must not read interactively"


# --- environment contract parity vs the backend ------------------------------

def backend_source_text() -> str:
    parts = []
    for path in (REPO_ROOT / "backend").rglob("*.py"):
        parts.append(path.read_text())
    return "\n".join(parts)


def test_every_expected_env_name_is_read_by_the_backend():
    source = backend_source_text()
    for name in {**parse_env_contract.WORKER_EXPECTED,
                 **parse_env_contract.API_EXPECTED,
                 **parse_env_contract.FLAGS_AT_REST}:
        assert name in source, f"smoke contract names {name}, but no backend code reads it"


def test_expected_values_match_the_production_facts():
    expected = parse_env_contract.WORKER_EXPECTED
    assert expected["MILO_COMMANDER_MODEL"] == "kimi-k2.6"
    assert expected["MILO_SWARM_WORKER_MODEL"] == "kimi-k2.6"
    assert expected["MILO_COMMANDER_MODEL_ALLOWLIST"] == "kimi-k2.6"
    assert expected["MILO_SWARM_MAX_ACTIVE_WORKERS"] == "8"
    assert expected["MILO_PROVIDER_MAX_CONCURRENCY"] == "8"
    assert expected["MILO_MAX_COST_PER_RUN"] == "3.00"
    assert expected["MILO_MAX_MODEL_CALLS_PER_RUN"] == "200"


def test_budget_env_names_come_from_the_authoritative_config():
    from backend.budget import BudgetConfig
    from backend.provider_scheduler import ProviderLimitsConfig

    known = set(BudgetConfig.ENV_KEYS.values()) | set(ProviderLimitsConfig.ENV_KEYS.values())
    for name in parse_env_contract.WORKER_EXPECTED:
        if name.startswith("MILO_MAX_") or name.startswith("MILO_PROVIDER_"):
            assert name in known, f"{name} is not a name the budget/scheduler code reads"


# --- parser behavior ---------------------------------------------------------

def env_entry(name, value=None, secret=False):
    if secret:
        return {"name": name, "valueFrom": {"secretKeyRef": {"key": "latest", "name": name}}}
    return {"name": name, "value": value}


WORKER_SA = "milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
GATEWAY_SA = "milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"


def worker_document(smoke_active=False, drop=(), override=None, service_account=WORKER_SA):
    values = {**parse_env_contract.WORKER_EXPECTED,
              **(parse_env_contract.WORKER_FLAGS_SMOKE if smoke_active
                 else parse_env_contract.FLAGS_AT_REST)}
    values.update(override or {})
    env = [env_entry(k, v) for k, v in values.items() if k not in drop]
    env += [env_entry(name, secret=True) for name in parse_env_contract.SECRET_BACKED]
    if smoke_active:
        env.append(env_entry("KIMI_API_KEY", secret=True))
    inner = {"containers": [{"env": env}]}
    if service_account:
        inner["serviceAccountName"] = service_account
    return {"spec": {"template": {"spec": {"template": {"spec": inner}}}}}


def api_env(smoke_active=False, drop=(), override=None):
    values = {**parse_env_contract.API_EXPECTED,
              **(parse_env_contract.API_FLAGS_SMOKE if smoke_active
                 else parse_env_contract.FLAGS_AT_REST)}
    values.update(override or {})
    env = [env_entry(k, v) for k, v in values.items() if k not in drop]
    env += [env_entry(name, secret=True) for name in parse_env_contract.SECRET_BACKED]
    return env


def api_revision_document(name="api-rev-1", smoke_active=False, drop=(),
                          override=None, service_account=API_SA):
    spec = {"containers": [{"env": api_env(smoke_active, drop, override)}]}
    if service_account:
        spec["serviceAccountName"] = service_account
    return {"metadata": {"name": name}, "spec": spec}


def api_service_document(ready="api-rev-1", traffic_percent=100, smoke_active=False,
                         template_override=None, traffic_revision=None):
    """Service doc whose TEMPLATE can be safe independently of the revision."""
    return {
        "status": {
            "latestReadyRevisionName": ready,
            "traffic": [{"revisionName": traffic_revision or ready,
                         "percent": traffic_percent}],
        },
        "spec": {"template": {"spec": {
            "serviceAccountName": API_SA,
            "containers": [{"env": api_env(smoke_active, override=template_override)}],
        }}},
    }


def test_worker_contract_passes_and_detects_each_violation(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(worker_document()))
    assert parse_env_contract.main(["worker", str(good)]) == 0

    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps(worker_document(smoke_active=True)))
    assert parse_env_contract.main(["worker", str(smoke), "--smoke-active"]) == 0

    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(worker_document(drop=("MILO_COMMANDER_MODEL",))))
    assert parse_env_contract.main(["worker", str(missing)]) == 1

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(worker_document(override={"MILO_MAX_COST_PER_RUN": "30.00"})))
    assert parse_env_contract.main(["worker", str(wrong)]) == 1

    # A provider key bound at rest is a violation; unbound during the smoke
    # window is a violation the other way.
    keyed_at_rest = worker_document()
    keyed_at_rest["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
        env_entry("KIMI_API_KEY", secret=True))
    keyed = tmp_path / "keyed.json"
    keyed.write_text(json.dumps(keyed_at_rest))
    assert parse_env_contract.main(["worker", str(keyed)]) == 1

    unkeyed_smoke = worker_document(smoke_active=True)
    unkeyed_smoke["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
        e for e in unkeyed_smoke["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
        if e["name"] != "KIMI_API_KEY"]
    unkeyed = tmp_path / "unkeyed.json"
    unkeyed.write_text(json.dumps(unkeyed_smoke))
    assert parse_env_contract.main(["worker", str(unkeyed), "--smoke-active"]) == 1


def test_paid_flag_on_at_rest_is_a_violation(tmp_path):
    doc = worker_document(override={"MILO_ENABLE_PAID_EXECUTION": "true"})
    path = tmp_path / "paid.json"
    path.write_text(json.dumps(doc))
    assert parse_env_contract.main(["worker", str(path)]) == 1


def test_iam_parser_requires_gateway_and_forbids_public(tmp_path):
    gateway = "milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"
    good = {"bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{gateway}"]}]}
    path = tmp_path / "iam.json"
    path.write_text(json.dumps(good))
    assert parse_iam.main([str(path), "--required-invoker", gateway, "--forbid-public"]) == 0

    public = {"bindings": [{"role": "roles/run.invoker",
                            "members": [f"serviceAccount:{gateway}", "allUsers"]}]}
    path.write_text(json.dumps(public))
    assert parse_iam.main([str(path), "--required-invoker", gateway, "--forbid-public"]) == 1

    missing = {"bindings": [{"role": "roles/run.viewer", "members": ["user:x@example.com"]}]}
    path.write_text(json.dumps(missing))
    assert parse_iam.main([str(path), "--required-invoker", gateway]) == 1


def test_execution_parser_counts_and_verdicts(tmp_path):
    running = {"status": {"conditions": [{"type": "Completed", "status": "Unknown"}]}}
    done = {"status": {"conditions": [{"type": "Completed", "status": "True"}], "succeededCount": 1}}
    failed = {"status": {"conditions": [{"type": "Completed", "status": "False"}], "failedCount": 1}}

    listing = tmp_path / "list.json"
    listing.write_text(json.dumps([running, done, failed]))
    assert parse_executions.main(["active-count", str(listing)]) == 0

    single = tmp_path / "one.json"
    for doc, expected in ((done, "succeeded"), (failed, "failed:0:1"), (running, "running")):
        single.write_text(json.dumps(doc))
        assert parse_executions.verdict(doc) == expected
        assert parse_executions.main(["verdict", str(single)]) == 0

    assert parse_executions.active_count([running, done, failed]) == 1


def test_malformed_json_is_a_usage_error_not_a_crash(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert parse_env_contract.main(["worker", str(broken)]) == 2
    assert parse_iam.main([str(broken), "--required-invoker", "x@y.z"]) == 2
    assert parse_executions.main(["verdict", str(broken)]) == 2


# --- serving-revision authority (PR #67 semantics) ---------------------------

parse_serving_state = load("parse_serving_state")


def write_json(path, payload):
    path.write_text(json.dumps(payload))
    return str(path)


def test_serving_state_verifies_ready_name_and_full_traffic(tmp_path):
    service = write_json(tmp_path / "svc.json", api_service_document())
    revision = write_json(tmp_path / "rev.json", api_revision_document())
    assert parse_serving_state.main(["resolve", service]) == 0
    assert parse_serving_state.main(["verify", service, revision]) == 0


def test_latest_ready_differs_from_inspected_revision_fails(tmp_path):
    service = write_json(tmp_path / "svc.json", api_service_document(ready="api-rev-2"))
    revision = write_json(tmp_path / "rev.json", api_revision_document(name="api-rev-1"))
    assert parse_serving_state.main(["verify", service, revision]) == 1


def test_ready_revision_without_full_traffic_fails(tmp_path):
    service = write_json(tmp_path / "svc.json", api_service_document(traffic_percent=60))
    revision = write_json(tmp_path / "rev.json", api_revision_document())
    assert parse_serving_state.main(["verify", service, revision]) == 1
    diverted = write_json(tmp_path / "svc2.json",
                          api_service_document(traffic_revision="api-rev-0"))
    assert parse_serving_state.main(["verify", diverted, revision]) == 1


def test_missing_latest_ready_revision_is_fail_closed(tmp_path):
    service = write_json(tmp_path / "svc.json", {"status": {}, "spec": {}})
    revision = write_json(tmp_path / "rev.json", api_revision_document())
    assert parse_serving_state.main(["resolve", service]) == 2
    assert parse_serving_state.main(["verify", service, revision]) == 1


def test_unsafe_serving_revision_fails_even_when_template_is_safe(tmp_path):
    """The service template alone is never authoritative: contract checks
    run against the revision document, and an unsafe revision fails even
    though the template carries the safe posture."""
    revision = api_revision_document(override={"MILO_ENABLE_PAID_EXECUTION": "true"})
    path = write_json(tmp_path / "rev.json", revision)
    assert parse_env_contract.main(["api", path]) == 1
    # Positive control: the same revision without the override passes.
    safe = write_json(tmp_path / "safe.json", api_revision_document())
    assert parse_env_contract.main(["api", safe]) == 0


# --- execution-control identity gating and runtime service accounts ---------

def test_execution_control_without_worker_identity_config_fails():
    doc = api_revision_document(override={"MILO_ENABLE_EXECUTION_CONTROL": "true"})
    problems = parse_env_contract.check("api", doc, smoke_active=False)
    assert any("MILO_WORKER_AUDIENCE" in p for p in problems)
    assert any("MILO_APPROVED_WORKER_IDENTITIES" in p for p in problems)

    configured = api_revision_document(override={
        "MILO_ENABLE_EXECUTION_CONTROL": "true",
        "MILO_WORKER_AUDIENCE": "https://milo-agent-api-beplbca7yq-uc.a.run.app",
        "MILO_APPROVED_WORKER_IDENTITIES": WORKER_SA,
    })
    problems = parse_env_contract.check("api", configured, smoke_active=False)
    assert not any("MILO_WORKER_AUDIENCE" in p or "allowlist" in p for p in problems)

    wildcard = api_revision_document(override={
        "MILO_ENABLE_EXECUTION_CONTROL": "true",
        "MILO_WORKER_AUDIENCE": "*",
        "MILO_APPROVED_WORKER_IDENTITIES": "*",
    })
    problems = parse_env_contract.check("api", wildcard, smoke_active=False)
    assert any("MILO_WORKER_AUDIENCE" in p for p in problems)
    assert any("allowlist" in p for p in problems)


def test_runtime_service_account_mismatch_fails(tmp_path):
    good = write_json(tmp_path / "w.json", worker_document())
    assert parse_env_contract.main(["worker", good, "--service-account", WORKER_SA]) == 0
    wrong = write_json(tmp_path / "w2.json",
                       worker_document(service_account="legacy-sa@example.iam.gserviceaccount.com"))
    assert parse_env_contract.main(["worker", wrong, "--service-account", WORKER_SA]) == 1
    unset = write_json(tmp_path / "w3.json", worker_document(service_account=None))
    assert parse_env_contract.main(["worker", unset, "--service-account", WORKER_SA]) == 1


def test_kimi_secret_iam_must_be_worker_only(tmp_path):
    worker_only = {"bindings": [{"role": "roles/secretmanager.secretAccessor",
                                 "members": [f"serviceAccount:{WORKER_SA}"]}]}
    path = write_json(tmp_path / "iam.json", worker_only)
    assert parse_iam.main([path, "--secret-accessor-only", WORKER_SA]) == 0

    empty = write_json(tmp_path / "empty.json", {"bindings": []})
    assert parse_iam.main([empty, "--secret-accessor-only", WORKER_SA]) == 0
    assert parse_iam.main([
        empty, "--secret-accessor-only", WORKER_SA, "--require-allowed-accessor"
    ]) == 1

    inherited = write_json(tmp_path / "project.json", {
        "bindings": [{"role": "roles/secretmanager.secretAccessor",
                      "members": [f"serviceAccount:{API_SA}"]}]
    })
    assert parse_iam.main([
        path, "--secret-accessor-only", WORKER_SA,
        "--inherited-policy-file", inherited,
    ]) == 1

    intruder = {"bindings": [{"role": "roles/secretmanager.secretAccessor",
                              "members": [f"serviceAccount:{WORKER_SA}",
                                          f"serviceAccount:{API_SA}"]}]}
    path = write_json(tmp_path / "bad.json", intruder)
    assert parse_iam.main([path, "--secret-accessor-only", WORKER_SA]) == 1

    other_binding_only = {"bindings": [
        {"role": "roles/secretmanager.viewer", "members": ["user:ops@example.com"]},
        {"role": "roles/secretmanager.secretAccessor",
         "members": ["serviceAccount:legacy@example.iam.gserviceaccount.com"]},
    ]}
    path = write_json(tmp_path / "legacy.json", other_binding_only)
    assert parse_iam.main([path, "--secret-accessor-only", WORKER_SA]) == 1


# --- executable controller regression harness --------------------------------

SMOKE_MOCK_GCLOUD = r"""#!/usr/bin/env bash
args="$*"
printf '%s\n' "${args}" >> "${MOCK_LOG}"
case "${args}" in
  *"secrets get-iam-policy"*) cat "${MOCK_DIR}/secret-iam.json" ;;
  *"projects get-iam-policy"*) cat "${MOCK_DIR}/project-iam.json" ;;
  *"services get-iam-policy"*) cat "${MOCK_DIR}/api-iam.json" ;;
  *"revisions describe"*) cat "${MOCK_DIR}/api-revision.json" ;;
  *"services describe"*) cat "${MOCK_DIR}/api.json" ;;
  *"jobs describe"*) cat "${MOCK_DIR}/worker.json" ;;
  *"executions list"*) cat "${MOCK_DIR}/executions.json" ;;
  *"executions cancel"*)
    if [ -f "${MOCK_DIR}/executions-after-cancel.json" ]; then
      cp "${MOCK_DIR}/executions-after-cancel.json" "${MOCK_DIR}/executions.json"
    fi ;;
  *"jobs update"*) exit "${MOCK_JOBS_UPDATE_EXIT:-0}" ;;
  *"services update"*) exit "${MOCK_SERVICES_UPDATE_EXIT:-0}" ;;
  *)
    echo "unexpected gcloud invocation: ${args}" >&2
    exit 9 ;;
esac
"""


def run_controller(tmp_path, mode, *, api=None, api_revision=None, worker=None,
                   executions=(), after_cancel=None, api_iam=None, secret_iam=None,
                   extra_env=None):
    import os
    import sys

    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_json(mock_dir / "api.json", api if api is not None else api_service_document())
    write_json(mock_dir / "api-revision.json",
               api_revision if api_revision is not None else api_revision_document())
    write_json(mock_dir / "worker.json", worker if worker is not None else worker_document())
    write_json(mock_dir / "executions.json", list(executions))
    if after_cancel is not None:
        write_json(mock_dir / "executions-after-cancel.json", after_cancel)
    write_json(mock_dir / "api-iam.json", api_iam if api_iam is not None else {
        "bindings": [{"role": "roles/run.invoker",
                      "members": [f"serviceAccount:{GATEWAY_SA}"]}]})
    write_json(mock_dir / "secret-iam.json", secret_iam if secret_iam is not None else {
        "bindings": [{"role": "roles/secretmanager.secretAccessor",
                      "members": [f"serviceAccount:{WORKER_SA}"]}]})
    write_json(mock_dir / "project-iam.json", {"bindings": []})
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(SMOKE_MOCK_GCLOUD)
    gcloud.chmod(0o755)
    log = mock_dir / "gcloud.log"
    log.write_text("")
    python_dir = str(Path(sys.executable).parent)
    env = {
        "PATH": f"{bin_dir}:{python_dir}:/usr/bin:/bin",
        "MOCK_DIR": str(mock_dir),
        "MOCK_LOG": str(log),
        "HOME": str(tmp_path),
        "KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS": "2",
        "KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS": "0",
        **(extra_env or {}),
    }
    result = subprocess.run(["bash", str(CONTROLLER), mode], capture_output=True,
                            text=True, env=env, timeout=120)
    return result, log.read_text()


def terminal_execution(name):
    return {"metadata": {"name": name},
            "status": {"completionTime": "2026-08-24T00:00:00Z",
                       "conditions": [{"type": "Completed", "status": "True"}]}}


def running_execution(name):
    return {"metadata": {"name": name},
            "status": {"completionTime": None,
                       "conditions": [{"type": "Completed", "status": "Unknown"}]}}


def test_controller_preflight_passes_on_fully_safe_posture(tmp_path):
    result, log = run_controller(tmp_path, "preflight")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preflight OK" in result.stdout
    assert "revisions describe" in log, "the serving revision must be inspected"


def test_controller_preflight_fails_when_serving_revision_is_unsafe(tmp_path):
    """Safe service template, unsafe live revision: must fail."""
    result, _ = run_controller(
        tmp_path, "preflight",
        api=api_service_document(),  # template fully safe
        api_revision=api_revision_document(override={"MILO_ENABLE_PAID_EXECUTION": "true"}),
    )
    assert result.returncode != 0
    assert "serving-revision env contract" in result.stderr + result.stdout


def test_controller_preflight_fails_on_ready_revision_mismatch(tmp_path):
    result, _ = run_controller(
        tmp_path, "preflight",
        api=api_service_document(ready="api-rev-9"),
        api_revision=api_revision_document(name="api-rev-1"),
    )
    assert result.returncode != 0
    assert "serving revision" in (result.stderr + result.stdout).lower()


def test_controller_preflight_fails_without_full_traffic(tmp_path):
    result, _ = run_controller(
        tmp_path, "preflight",
        api=api_service_document(traffic_percent=40),
    )
    assert result.returncode != 0


def test_controller_preflight_fails_on_unauthorized_secret_accessor(tmp_path):
    result, _ = run_controller(
        tmp_path, "preflight",
        secret_iam={"bindings": [{"role": "roles/secretmanager.secretAccessor",
                                  "members": [f"serviceAccount:{WORKER_SA}",
                                              "serviceAccount:intruder@evil.iam.gserviceaccount.com"]}]},
    )
    assert result.returncode != 0
    assert "worker-only" in result.stderr + result.stdout


def test_controller_kill_delegates_to_canonical_shutdown(tmp_path):
    """kill performs the COMPLETE PR #67 fail-closed shutdown: all six API
    flags + launcher, worker paid flag, both provider aliases removed from
    both surfaces, every active execution cancelled, postconditions
    verified."""
    kill_api = api_service_document()
    kill_api["spec"]["template"]["spec"]["containers"][0]["env"] = [
        env_entry(k, v) for k, v in {**parse_env_contract.FLAGS_AT_REST,
                                     "JOB_LAUNCHER": "disabled"}.items()]
    result, log = run_controller(
        tmp_path, "kill",
        api=kill_api,
        executions=[running_execution("milo-agent-worker-live1")],
        after_cancel=[terminal_execution("milo-agent-worker-live1")],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KILL SWITCH APPLIED" in result.stdout
    for flag in ("MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_PROPOSAL_MUTATIONS",
                 "MILO_ENABLE_PROPOSAL_READS", "MILO_ENABLE_RUN_CANCELLATION",
                 "MILO_ENABLE_EXECUTION_CONTROL", "MILO_ENABLE_PAID_EXECUTION"):
        assert f"{flag}=false" in log
    assert "JOB_LAUNCHER=disabled" in log
    for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
        assert f"--remove-secrets={alias}" in log
        assert f"--remove-env-vars={alias}" in log
    assert "executions cancel" in log
    assert "milo-agent-worker-live1" in log


def test_controller_kill_fails_when_flags_remain_after_cancel(tmp_path):
    """Execution cancellation succeeds but the API stays enabled: the kill
    postcondition must fail — cancellation alone is never success."""
    unsafe_api = api_service_document()
    unsafe_api["spec"]["template"]["spec"]["containers"][0]["env"] = [
        env_entry("MILO_ENABLE_RUN_CREATION", "true"),
        env_entry("JOB_LAUNCHER", "cloud_run"),
    ]
    unsafe_revision = api_revision_document(
        override={"MILO_ENABLE_RUN_CREATION": "true", "JOB_LAUNCHER": "cloud_run"})
    result, log = run_controller(
        tmp_path, "kill",
        api=unsafe_api,
        api_revision=unsafe_revision,
        executions=[running_execution("milo-agent-worker-live2")],
        after_cancel=[terminal_execution("milo-agent-worker-live2")],
    )
    assert result.returncode != 0
    assert "executions cancel" in log  # the cancellation itself succeeded
    assert "KILL SWITCH INCOMPLETE" in result.stderr + result.stdout


def test_controller_kill_fails_when_provider_binding_remains(tmp_path):
    keyed_worker = worker_document()
    keyed_worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
        env_entry("KIMI_API_KEY", secret=True))
    kill_api = api_service_document()
    kill_api["spec"]["template"]["spec"]["containers"][0]["env"] = [
        env_entry(k, v) for k, v in {**parse_env_contract.FLAGS_AT_REST,
                                     "JOB_LAUNCHER": "disabled"}.items()]
    result, _ = run_controller(tmp_path, "kill", api=kill_api, worker=keyed_worker)
    assert result.returncode != 0
    assert "KILL SWITCH INCOMPLETE" in result.stderr + result.stdout


# --- automatic shutdown and durable semantic acceptance ----------------------

def test_execute_arms_automatic_shutdown_before_validation(tmp_path):
    result, log = run_controller(tmp_path, "execute")
    assert result.returncode != 0
    assert "automatic canonical fail-closed shutdown" in result.stdout
    assert "jobs update" in log
    assert "services update" in log
    assert "KILL SWITCH APPLIED" in result.stdout


def test_controller_binds_cloud_run_success_to_semantic_verification():
    text = CONTROLLER.read_text()
    succeeded = text.split("succeeded)", 1)[1].split(";;", 1)[0]
    assert "semantic_verify_run" in succeeded
    assert "SMOKE_SAFETY_ARMED=1" in text
    assert "trap shutdown_guard EXIT" in text


def _positive_run(run_id="11111111-1111-4111-8111-111111111111"):
    return [{
        "id": run_id,
        "status": "completed",
        "attempt": 1,
        "finished_at": "2026-08-24T16:00:00Z",
        "usage": {"model_calls": 4, "actual_cost": 0.02},
    }]


def _positive_checkpoint(run_id="11111111-1111-4111-8111-111111111111"):
    return [{
        "run_id": run_id,
        "engine_version": "swarm_v2.1",
        "workflow_key": "swarm_v2",
        "phase": "swarm_v2",
        "attempt": 1,
    }]


def test_semantic_positive_smoke_requires_completed_checkpoint_and_caps(tmp_path):
    run_id = "11111111-1111-4111-8111-111111111111"
    run_file = write_json(tmp_path / "run.json", _positive_run(run_id))
    checkpoint_file = write_json(
        tmp_path / "checkpoint.json", _positive_checkpoint(run_id)
    )
    args = [
        run_file, checkpoint_file,
        "--run-id", run_id,
        "--expected-attempt", "1",
        "--max-model-calls", "200",
        "--max-actual-cost", "3.00",
    ]
    assert parse_run_state.main(args) == 0

    failed = _positive_run(run_id)
    failed[0]["status"] = "failed"
    write_json(tmp_path / "run.json", failed)
    assert parse_run_state.main(args) == 1

    over_cap = _positive_run(run_id)
    over_cap[0]["usage"] = {"model_calls": 201, "actual_cost": 3.01}
    write_json(tmp_path / "run.json", over_cap)
    assert parse_run_state.main(args) == 1

    write_json(tmp_path / "run.json", _positive_run(run_id))
    write_json(tmp_path / "checkpoint.json", [])
    assert parse_run_state.main(args) == 1
