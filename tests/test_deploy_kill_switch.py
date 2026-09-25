"""Executable proofs for scripts/deploy/kill-switch.sh (cleanup D5).

The script is the canonical emergency order of
docs/production-readiness/ROLLBACK.md as commands. It is run here only
against PATH-shimmed gcloud and vercel mocks: no network, no real GCP or
Vercel mutation, and no test ever executes it against production.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KILL_SWITCH = REPO / "scripts" / "deploy" / "kill-switch.sh"
CONTRACT = REPO / "scripts" / "deploy" / "deployment-contract.sh"
ACTIVATE = REPO / "scripts" / "deploy" / "website-execution-activate.sh"
ACK = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"
DEPLOYMENT = "https://milo-prod-abc123.vercel.app"

MOCK_GCLOUD = r"""#!/usr/bin/env bash
args="$*"
printf 'gcloud %s\n' "${args}" >> "${MOCK_LOG}"
case "${args}" in
  "auth list"*) echo "operator@example.invalid" ;;
  "config get-value project"*) echo "p" ;;
  "run jobs update job "*|"run services update api "*|"run jobs update capture "*)
    [[ "$4" == capture && ! -f "${MOCK_DIR}/capture.json" ]] && { echo "not found" >&2; exit 1; }
    [[ "${MOCK_JOBS_UPDATE_EXIT:-0}" == 0 || "$2" != jobs ]] || exit "${MOCK_JOBS_UPDATE_EXIT}"
    [[ "${MOCK_SERVICES_UPDATE_EXIT:-0}" == 0 || "$2" != services ]] || exit "${MOCK_SERVICES_UPDATE_EXIT}"
    python3 "${MOCK_DIR}/apply.py" "${MOCK_DIR}/$4.json" "$@" ;;
  "run services describe api "*) cat "${MOCK_DIR}/api.json" ;;
  "run jobs describe job "*) cat "${MOCK_DIR}/job.json" ;;
  "run jobs describe capture "*)
    [[ -f "${MOCK_DIR}/capture.json" ]] || { echo "not found" >&2; exit 1; }
    cat "${MOCK_DIR}/capture.json" ;;
  *) echo "unexpected gcloud invocation: ${args}" >&2; exit 9 ;;
esac
"""

MOCK_VERCEL = r"""#!/usr/bin/env bash
args="$*"
stdin=""
if [[ "${args}" == "env add"* ]]; then stdin="$(cat)"; fi
printf 'vercel %s%s\n' "${args}" "${stdin:+ <stdin:${stdin}>}" >> "${MOCK_LOG}"
printf '%s\n' "${PWD}" >> "${MOCK_LOG}.cwd"
case "${args}" in
  "whoami"*) exit "${MOCK_VERCEL_WHOAMI_EXIT:-0}" ;;
  "env rm"*) exit "${MOCK_VERCEL_RM_EXIT:-0}" ;;
  "env add"*) exit "${MOCK_VERCEL_ADD_EXIT:-0}" ;;
  "redeploy"*) exit "${MOCK_VERCEL_REDEPLOY_EXIT:-0}" ;;
  *) echo "unexpected vercel invocation: ${args}" >&2; exit 9 ;;
esac
"""

# The mock's state: a successful update is applied to the described service
# or job, so the script's read-back sees what its own commands did. With
# MOCK_IGNORE_UPDATES=1 an update "succeeds" but changes nothing (a change
# that did not take), which the read-back must catch.
MOCK_APPLY = r"""
import json, os, sys

path, args = sys.argv[1], sys.argv[2:]
if os.environ.get("MOCK_IGNORE_UPDATES") == "1":
    sys.exit(0)
doc = json.load(open(path))
spec = doc["spec"]["template"]["spec"]
if "template" in spec:
    spec = spec["template"]["spec"]
env = spec["containers"][0]["env"]
for flag, value in zip(args, args[1:]):
    if flag == "--update-env-vars":
        delim = ","
        if value.startswith("^"):
            delim, value = value[1], value[3:]
        for pair in value.split(delim):
            name, _, val = pair.partition("=")
            env[:] = [e for e in env if e["name"] != name] + [{"name": name, "value": val}]
    elif flag in ("--remove-env-vars", "--remove-secrets"):
        env[:] = [e for e in env if e["name"] != value]
json.dump(doc, open(path, "w"))
"""


def env_doc(pairs, *, job=False):
    env = []
    for name, value in pairs.items():
        if isinstance(value, dict):
            env.append({"name": name, **value})
        else:
            env.append({"name": name, "value": value})
    container = {"containers": [{"env": env}]}
    if job:
        return {"spec": {"template": {"spec": {"template": {"spec": container}}}}}
    return {"spec": {"template": {"spec": container}}}


def contract_array(name: str) -> list[str]:
    out = subprocess.run(
        ["bash", "-c", f'source "{CONTRACT}"; printf "%s\\n" "${{{name}[@]}}"'],
        capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def contract_scalar(name: str) -> str:
    out = subprocess.run(["bash", "-c", f'source "{CONTRACT}"; printf "%s" "${name}"'],
                         capture_output=True, text=True, check=True)
    return out.stdout


API_OPENED = sorted(set(contract_array("MILO_PLAN_AUTHORING_API_ENABLE_FLAGS")
                        + contract_array("MILO_STAGE2_API_ENABLE_FLAGS")))
WORKER_OPENED = sorted(set(contract_array("MILO_STAGE2_WORKER_ENABLE_FLAGS")))
API_PINNED_OFF = contract_array("MILO_STAGE2_API_PINNED_OFF_FLAGS")
WORKER_PINNED_OFF = contract_array("MILO_STAGE2_WORKER_PINNED_OFF_FLAGS")
CAPTURE_FLAG = contract_scalar("MILO_CAPTURE_MASTER_FLAG_NAME")
STAGE_A_FLAGS = contract_array("MILO_STAGE_A_FLAG_NAMES")
VERCEL_OPENED = [contract_scalar("MILO_STAGE2_VERCEL_RUN_START_FLAG"),
                 contract_scalar("MILO_STAGE2_VERCEL_RUNTIME_FLAG"),
                 contract_scalar("MILO_STAGE2_VERCEL_BUILD_FLAG")]

CLOSED_API = env_doc({**{flag: "false" for flag in API_OPENED},
                      "MILO_ENABLE_PAID_EXECUTION": "false", "JOB_LAUNCHER": "disabled"})
CLOSED_WORKER = env_doc({flag: "false" for flag in WORKER_OPENED}, job=True)
WORKER_WITH_SECRET_KEY = env_doc({**{flag: "true" for flag in WORKER_OPENED},
                                  "KIMI_API_KEY": {"valueFrom": {"secretKeyRef": {"name": "KIMI_API_KEY"}}}},
                                 job=True)


def run_switch(tmp_path, *args, env=None, api=CLOSED_API, worker=CLOSED_WORKER, capture=None,
               ack=True, capture_configured=True, vercel_linked=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (mock_dir / "api.json").write_text(json.dumps(api))
    (mock_dir / "job.json").write_text(worker if isinstance(worker, str) else json.dumps(worker))
    if capture is not None:
        (mock_dir / "capture.json").write_text(json.dumps(capture))
    (mock_dir / "apply.py").write_text(MOCK_APPLY)
    for name, body in (("gcloud", MOCK_GCLOUD), ("vercel", MOCK_VERCEL)):
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)
    config = tmp_path / "operator.env"
    config.write_text("GCP_PROJECT_ID=p\nGCP_REGION=r\nCLOUD_RUN_API_SERVICE=api\n"
                      "CLOUD_RUN_WORKER_JOB=job\n"
                      + ("CLOUD_RUN_CAPTURE_JOB=capture\n" if capture_configured else ""))
    log = tmp_path / "calls.log"
    log.write_text("")
    # The directory linked to the Vercel project; every vercel command runs in it.
    linked = tmp_path / "frontend-link"
    (linked / ".vercel").mkdir(parents=True, exist_ok=True)
    if vercel_linked:
        (linked / ".vercel" / "project.json").write_text('{"projectId": "p", "orgId": "o"}')
    full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
                "MOCK_LOG": str(log), "MOCK_DIR": str(mock_dir)}
    if ack:
        full_env["MILO_OPERATOR_ACK"] = ACK
    full_env.update(env or {})
    result = subprocess.run(["bash", str(KILL_SWITCH), "--operator-config", str(config),
                             "--vercel-cwd", str(linked), *args],
                            capture_output=True, text=True, env=full_env, timeout=120)
    return result, [line for line in log.read_text().splitlines() if line]


def mutations(calls):
    return [c for c in calls
            if not re.match(r"gcloud (auth list|config get-value|run (jobs|services) describe)|vercel whoami", c)]


def vercel_dirs(tmp_path):
    return set((tmp_path / "calls.log.cwd").read_text().split())


# --- dry run -----------------------------------------------------------------

def test_dry_run_is_the_default_and_calls_nothing(tmp_path):
    result, calls = run_switch(tmp_path, ack=False)
    assert result.returncode == 0, result.stderr
    assert calls == [], "the default mode must not invoke gcloud or vercel at all"
    assert "DRY RUN: nothing was changed" in result.stdout
    for step in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
        assert step in result.stdout


def test_dry_run_prints_the_order_in_rollback_md_order(tmp_path):
    result, _ = run_switch(tmp_path, ack=False)
    out = result.stdout
    markers = ["vercel env rm GATEWAY_ALLOW_RUN_START_ROUTES", "vercel redeploy",
               "MILO_ENABLE_PAID_EXECUTION=false",
               "MILO_ENABLE_RUN_CREATION=false",
               "MILO_ENABLE_GOVERNMENT_CATALOG_READ=false",
               "--remove-secrets KIMI_API_KEY",
               "MILO_ENABLE_RUN_CANCELLATION=false",
               "vercel env rm GATEWAY_ALLOW_EXECUTION_ROUTES"]
    positions = [out.index(m) for m in markers]
    assert positions == sorted(positions), list(zip(markers, positions))


# --- apply guards ------------------------------------------------------------

def test_apply_refuses_without_the_operator_ack(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, ack=False)
    assert result.returncode == 2
    assert "MILO_OPERATOR_ACK" in result.stderr
    assert calls == []


def test_apply_refuses_without_the_deployment_to_redeploy(tmp_path):
    result, calls = run_switch(tmp_path, "--apply")
    assert result.returncode == 2
    assert "--vercel-deployment" in result.stderr
    assert calls == []


def test_apply_refuses_when_the_vercel_directory_is_not_linked(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               vercel_linked=False)
    assert result.returncode == 2
    assert ".vercel/project.json missing" in result.stderr
    assert mutations(calls) == []


def test_apply_refuses_when_vercel_is_not_logged_in(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               env={"MOCK_VERCEL_WHOAMI_EXIT": "1"})
    assert result.returncode == 2
    assert "vercel whoami" in result.stderr
    assert mutations(calls) == []


def test_every_vercel_command_runs_in_the_linked_directory(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(c.startswith("vercel env add") for c in calls)
    assert vercel_dirs(tmp_path) == {str(tmp_path / "frontend-link")}


def test_the_default_vercel_directory_is_the_linked_frontend(tmp_path):
    config = tmp_path / "operator.env"
    config.write_text("GCP_PROJECT_ID=p\nGCP_REGION=r\nCLOUD_RUN_API_SERVICE=api\n"
                      "CLOUD_RUN_WORKER_JOB=job\n")
    result = subprocess.run(["bash", str(KILL_SWITCH), "--operator-config", str(config)],
                            capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=60)
    assert result.returncode == 0, result.stderr
    assert f"# vercel commands run in: {REPO / 'frontend'}" in result.stdout


def test_order_only_and_remaining_only_are_exclusive(tmp_path):
    result, calls = run_switch(tmp_path, "--order-only", "--remaining-only")
    assert result.returncode == 2
    assert "exclusive" in result.stderr
    assert calls == []


# --- apply: the exact order --------------------------------------------------

def test_apply_executes_the_canonical_order_then_the_remaining_flags(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               worker=WORKER_WITH_SECRET_KEY)
    applied = mutations(calls)
    expected_prefix = [
        "vercel env rm GATEWAY_ALLOW_RUN_START_ROUTES production --yes",
        "vercel env add GATEWAY_ALLOW_RUN_START_ROUTES production <stdin:false>",
        f"vercel redeploy {DEPLOYMENT}",
        "gcloud run jobs update job --region r --project p --update-env-vars MILO_ENABLE_PAID_EXECUTION=false",
        "gcloud run services update api --region r --project p --update-env-vars MILO_ENABLE_PAID_EXECUTION=false",
        "gcloud run services update api --region r --project p --update-env-vars "
        "^;^MILO_ENABLE_RUN_CREATION=false;MILO_ENABLE_WORK_SCOPE_BATCHES=false;JOB_LAUNCHER=disabled",
        "gcloud run jobs update job --region r --project p --update-env-vars MILO_ENABLE_GOVERNMENT_CATALOG_READ=false",
        "gcloud run services update api --region r --project p --update-env-vars MILO_ENABLE_GOVERNMENT_CATALOG_READ=false",
        "gcloud run jobs update job --region r --project p --remove-secrets KIMI_API_KEY",
    ]
    assert applied[:len(expected_prefix)] == expected_prefix
    # Step 6 comes strictly after step 5.
    rest = applied[len(expected_prefix):]
    assert any("MILO_ENABLE_RUN_CANCELLATION=false" in c for c in rest)
    assert any(c.startswith("vercel env add GATEWAY_ALLOW_EXECUTION_ROUTES") for c in rest)
    assert rest[-1] == f"vercel redeploy {DEPLOYMENT}"
    # The mock applied every change, so the read-back is closed and the run is OK.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: OK" in result.stdout


def test_every_flag_the_activation_opens_is_closed(tmp_path):
    """By construction: the lists come from deployment-contract.sh."""
    _, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT)
    applied = mutations(calls)
    api_set = " ".join(c for c in applied if c.startswith("gcloud run services update"))
    job_set = " ".join(c for c in applied if c.startswith("gcloud run jobs update"))
    for flag in API_OPENED:
        assert f"{flag}=false" in api_set, f"API flag {flag} is opened but never closed"
    for flag in WORKER_OPENED:
        assert f"{flag}=false" in job_set, f"worker flag {flag} is opened but never closed"
    assert "JOB_LAUNCHER=disabled" in api_set
    # The contract's pinned-off flags are re-asserted false too (idempotent),
    # so a drifted MILO_ENABLE_CATALOG_PROMOTION / WORK_SCOPE_PREPARATION is closed.
    for flag in API_PINNED_OFF:
        assert f"{flag}=false" in api_set, f"API pinned-off flag {flag} is not re-asserted"
    for flag in WORKER_PINNED_OFF:
        assert f"{flag}=false" in job_set, f"worker pinned-off flag {flag} is not re-asserted"
    assert "MILO_ENABLE_CATALOG_PROMOTION" in API_PINNED_OFF + WORKER_PINNED_OFF
    # Every flag Stage A pins off, including the dormant proposal flags.
    for flag in STAGE_A_FLAGS:
        assert f"{flag}=false" in api_set and f"{flag}=false" in job_set, flag
    for flag in VERCEL_OPENED:
        assert f"vercel env add {flag} production <stdin:false>" in applied, flag


def test_the_contract_lists_are_the_flags_the_activation_script_opens():
    """If website-execution-activate.sh ever opens a flag outside the contract
    arrays, this test (not production) is where that is found."""
    text = ACTIVATE.read_text()
    opened_by_name = set(re.findall(r"\b(MILO_ENABLE_[A-Z_]+|GATEWAY_ALLOW_[A-Z_]+)\b", text))
    covered = set(API_OPENED) | set(WORKER_OPENED) | set(VERCEL_OPENED) | {
        "MILO_ENABLE_PAID_EXECUTION"}
    assert opened_by_name <= covered, sorted(opened_by_name - covered)


def test_an_env_form_key_and_the_alias_are_removed_with_the_right_flag(tmp_path):
    worker = env_doc({"MILO_ENABLE_PAID_EXECUTION": "true",
                      "KIMI_API_KEY": "plain-value-not-a-secret-ref",
                      "MOONSHOT_API_KEY": {"valueFrom": {"secretKeyRef": {"name": "M"}}}}, job=True)
    _, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, worker=worker)
    applied = mutations(calls)
    assert "gcloud run jobs update job --region r --project p --remove-env-vars KIMI_API_KEY" in applied
    assert "gcloud run jobs update job --region r --project p --remove-secrets MOONSHOT_API_KEY" in applied
    assert not any("--remove-secrets KIMI_API_KEY" in c for c in applied)


# --- the Government capture job -------------------------------------------------

def test_an_existing_capture_job_has_its_master_flag_closed_and_read_back(tmp_path):
    capture = env_doc({CAPTURE_FLAG: "true"}, job=True)
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, capture=capture)
    assert (f"gcloud run jobs update capture --region r --project p --update-env-vars {CAPTURE_FLAG}=false"
            in mutations(calls))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "capture: 1 flag(s) checked, 0 problem(s)" in result.stdout


def test_a_capture_flag_that_stays_open_fails_the_read_back(tmp_path):
    capture = env_doc({CAPTURE_FLAG: "true"}, job=True)
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, capture=capture,
                           env={"MOCK_IGNORE_UPDATES": "1"})
    assert result.returncode == 1
    assert f"NOT CLOSED (capture): {CAPTURE_FLAG}=true" in result.stdout


def test_an_absent_capture_job_is_reported_and_not_an_error(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT)
    assert not any(c.startswith("gcloud run jobs update capture") for c in calls)
    assert "The capture job capture does not exist: nothing to close." in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_unconfigured_capture_job_is_never_described(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               capture_configured=False)
    assert not any("capture" in c for c in calls)
    assert "CLOUD_RUN_CAPTURE_JOB is not configured" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


# --- fail closed --------------------------------------------------------------

def test_a_failing_step_is_reported_and_the_later_steps_still_run(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               env={"MOCK_VERCEL_REDEPLOY_EXIT": "1"})
    applied = mutations(calls)
    assert result.returncode == 1
    assert "STEP 1 FAILED" in result.stderr
    assert any("MILO_ENABLE_PAID_EXECUTION=false" in c for c in applied)
    assert any("MILO_ENABLE_RUN_CANCELLATION=false" in c for c in applied)
    assert "RESULT: INCOMPLETE" in result.stderr


def test_a_failing_gcloud_update_is_reported_and_the_later_steps_still_run(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                               env={"MOCK_JOBS_UPDATE_EXIT": "1"})
    assert result.returncode == 1
    assert "STEP 2 FAILED" in result.stderr and "STEP 4 FAILED" in result.stderr
    applied = mutations(calls)
    assert any(c.startswith("gcloud run services update api") and "RUN_CREATION=false" in c for c in applied)
    assert applied[-1] == f"vercel redeploy {DEPLOYMENT}"


def test_an_undescribable_worker_fails_step_5(tmp_path):
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                           worker="not json")
    assert result.returncode == 1
    assert "STEP 5 FAILED" in result.stderr


def test_traffic_pinned_to_an_old_revision_fails_the_read_back(tmp_path):
    pinned = json.loads(json.dumps(CLOSED_API))
    pinned["status"] = {"traffic": [{"revisionName": "api-old", "percent": 100}]}
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, api=pinned)
    assert result.returncode == 1
    assert "traffic is not 100% on the latest revision" in result.stdout


def test_traffic_on_the_latest_revision_passes_the_read_back(tmp_path):
    serving = json.loads(json.dumps(CLOSED_API))
    serving["status"] = {"traffic": [{"latestRevision": True, "percent": 100}]}
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, api=serving)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_read_back_that_is_still_open_fails(tmp_path):
    still_open = env_doc({**{flag: "false" for flag in API_OPENED},
                          "MILO_ENABLE_PAID_EXECUTION": "false", "JOB_LAUNCHER": "cloud_run"})
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT, api=still_open,
                           env={"MOCK_IGNORE_UPDATES": "1"})
    assert result.returncode == 1
    assert "NOT CLOSED (api): JOB_LAUNCHER=cloud_run" in result.stdout


def test_a_provider_key_still_bound_after_step_5_fails(tmp_path):
    result, _ = run_switch(tmp_path, "--apply", "--vercel-deployment", DEPLOYMENT,
                           worker=WORKER_WITH_SECRET_KEY, env={"MOCK_IGNORE_UPDATES": "1"})
    # The removal "succeeded" but did not take, so the read-back still shows the key.
    assert result.returncode == 1
    assert "NOT CLOSED (worker): KIMI_API_KEY still bound" in result.stdout


def test_the_catalog_flag_is_closed_on_the_worker_and_verified(tmp_path):
    """Rollback must close the catalog without a code rollback: the switch SETS
    MILO_ENABLE_CATALOG_EXECUTION false on the worker (where the capability is
    built) and VERIFIES it, so "the switch ran" means "the catalog is closed"."""
    flag = "MILO_ENABLE_CATALOG_EXECUTION"
    worker = env_doc({flag: "true"}, job=True)
    _, calls = run_switch(tmp_path / "set", "--apply", "--vercel-deployment", DEPLOYMENT, worker=worker)
    assert any(c.startswith("gcloud run jobs update job") and f"{flag}=false" in c
               for c in mutations(calls))
    result, _ = run_switch(tmp_path / "verify", "--apply", "--vercel-deployment", DEPLOYMENT,
                           worker=worker, env={"MOCK_IGNORE_UPDATES": "1"})
    assert result.returncode == 1
    assert f"NOT CLOSED (worker): {flag}=true" in result.stdout


# --- scopes -------------------------------------------------------------------

def test_order_only_stops_after_step_5_and_keeps_cancellation_open(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--order-only", "--vercel-deployment", DEPLOYMENT)
    applied = mutations(calls)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("MILO_ENABLE_RUN_CANCELLATION" in c for c in applied)
    assert not any("GATEWAY_ALLOW_EXECUTION_ROUTES" in c for c in applied)
    assert any("MILO_ENABLE_GOVERNMENT_CATALOG_READ=false" in c for c in applied)


def test_remaining_only_runs_step_6_alone(tmp_path):
    result, calls = run_switch(tmp_path, "--apply", "--remaining-only", "--vercel-deployment", DEPLOYMENT)
    applied = mutations(calls)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("GATEWAY_ALLOW_RUN_START_ROUTES" in c for c in applied)
    assert not any("MILO_ENABLE_PAID_EXECUTION" in c for c in applied)
    assert any("MILO_ENABLE_RUN_CANCELLATION=false" in c for c in applied)


# --- the document and the script agree ---------------------------------------

def test_rollback_md_names_this_script_and_the_same_five_steps():
    rollback = (REPO / "docs" / "production-readiness" / "ROLLBACK.md").read_text()
    assert "scripts/deploy/kill-switch.sh" in rollback
    section = rollback.split("## Execution flags — emergency order", 1)[1].split("\n## ", 1)[0]
    order = [section.index(m) for m in (
        "GATEWAY_ALLOW_RUN_START_ROUTES=false", "MILO_ENABLE_PAID_EXECUTION=false",
        "MILO_ENABLE_RUN_CREATION=false", "MILO_ENABLE_GOVERNMENT_CATALOG_READ=false",
        "Remove the provider API key")]
    assert order == sorted(order)


def test_the_script_never_enables_anything():
    text = KILL_SWITCH.read_text()
    assert not re.search(r"(MILO_ENABLE_[A-Z_]+|GATEWAY_ALLOW_[A-Z_]+)=true", text)
    assert "--update-secrets" not in text and "--set-env-vars" not in text
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")).lower()
    assert "executions" not in code, "the switch never cancels (or touches) an execution"
    assert "delete" not in code, "the switch never deletes a resource"
    assert not re.search(r"vercel (rm|remove)\b", code), "only env values are replaced, never a deployment"


def test_help_exits_zero_without_configuration():
    result = subprocess.run(["bash", str(KILL_SWITCH), "--help"], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert result.returncode == 0
    assert "--dry-run" in result.stdout and "--apply" in result.stdout
