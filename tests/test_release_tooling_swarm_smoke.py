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


# --- controller shell hygiene ------------------------------------------------

def test_controller_bash_syntax_and_strict_mode():
    subprocess.run(["bash", "-n", str(CONTROLLER)], check=True)
    text = CONTROLLER.read_text()
    assert "set -euo pipefail" in text
    assert re.search(r"trap cleanup EXIT", text), "temp workspace must be removed on every exit path"
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
    for helper in ("parse_env_contract", "parse_iam", "parse_executions"):
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


def worker_document(smoke_active=False, drop=(), override=None):
    values = {**parse_env_contract.WORKER_EXPECTED,
              **(parse_env_contract.WORKER_FLAGS_SMOKE if smoke_active
                 else parse_env_contract.FLAGS_AT_REST)}
    values.update(override or {})
    env = [env_entry(k, v) for k, v in values.items() if k not in drop]
    env += [env_entry(name, secret=True) for name in parse_env_contract.SECRET_BACKED]
    if smoke_active:
        env.append(env_entry("KIMI_API_KEY", secret=True))
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}}}


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
