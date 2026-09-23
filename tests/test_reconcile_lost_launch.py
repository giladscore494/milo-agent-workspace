"""A LOST launch, reconciled through the existing launch reconciliation tool.

If the API takes launch ownership (the launch compare-and-set moves a queued
run to `launching`) and its process dies before it records `launched`,
`launch_failed` or `launch_unknown`, the run rests at `queued` + `launching`.
`scripts/release/reconcile-launch-unknown.sh` resolves it like `launch_unknown`:
it lists it read-only, and after the operator has checked Cloud Run it applies
`confirmed-launched`, `confirmed-not-launched` or `leave-unresolved` under the
same protected apply guard and audit. The two mutating decisions go through
`public.reconcile_lost_launch`, whose guards are exercised in real PostgreSQL
by tests/test_migrations_postgres.py. What is proven here is the tool around
it: it never decides without the full guard, makes exactly ONE guarded call for
ONE run, never launches anything, refuses a run whose worker holds a lease, and
writes an audit record only after the database reports that exactly that run
changed. `launch_unknown` keeps its own guarded updates, unchanged.

The mocks are strict: any command they do not recognise fails loudly.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release" / "reconcile-launch-unknown.sh"
ACK = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"
PROJECT = "milo-prod"
ACCOUNT = "operator@milo-prod.iam.gserviceaccount.com"
RUN = "5f0c3b7e-2a51-4d4e-9d6f-3a8e1c2b7d40"

GCLOUD_MOCK = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
case "$*" in
  *"config get-value account"*) printf '%s\n' "${MOCK_GCLOUD_ACCOUNT}";;
  *"config get-value project"*) printf '%s\n' "${MOCK_GCLOUD_PROJECT}";;
  *) printf 'MOCK-UNAUTHORIZED gcloud: %s\n' "$*" >&2; exit 98;;
esac
"""

#: SQL arrives with -c, or on stdin for the guarded decision (whose values
#: travel as psql variables, never spliced into the text).
PSQL_MOCK = r"""#!/usr/bin/env bash
sql=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[$i]}" == "-c" ]] && sql="${args[$((i+1))]}"
done
[[ -z "$sql" ]] && sql="$(cat)"
printf 'psql %s\n' "$*" >> "$MOCK_LOG"
printf '%s\n----\n' "$sql" >> "$MOCK_SQL_LOG"
case "$sql" in
  *"public.reconcile_lost_launch("*)
    if [[ -n "${MOCK_RECONCILE_ERROR:-}" ]]; then
      printf 'ERROR:  %s\n' "$MOCK_RECONCILE_ERROR" >&2; exit 3
    fi
    printf '%s\n' "${MOCK_RECONCILE_OUT:-true|launch_failed|queued}";;
  *"with upd as (update"*) printf '%s\n' "${MOCK_UPDATE:-1|launch_failed|queued}";;
  *"update public.runs"*|*"insert into"*|*"delete from"*)
    printf 'MOCK-UNAUTHORIZED unguarded mutation: %s\n' "$sql" >&2; exit 98;;
  *"select launch_state from public.runs"*) printf '%s\n' "${MOCK_LEAVE_STATE-}";;
  *"select launch_state ||"*) printf '%s\n' "${MOCK_READ-launching|queued|none}";;
  *"launch_state = 'launch_unknown'"*) printf '%s\n' "${MOCK_LIST_UNKNOWN-}";;
  *"status = 'queued' and launch_state = 'launching' and updated_at <="*)
    printf '%s\n' "${MOCK_LIST_LOST-}";;
  *) printf 'MOCK-UNAUTHORIZED psql: %s\n' "$sql" >&2; exit 98;;
esac
"""


@pytest.fixture()
def tooling(tmp_path: Path):
    """Strict mock CLIs on PATH, and a clean git worktree to run from."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", GCLOUD_MOCK), ("psql", PSQL_MOCK)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()
    log, sql_log = tmp_path / "invocations.log", tmp_path / "sql.log"
    log.touch()
    sql_log.touch()
    audit = tmp_path / "audit.log"  # outside the checkout: writing it dirties nothing

    def run(*args: str, **env: str):
        run_env = dict(os.environ)
        run_env.update({"PATH": f"{bin_dir}:{run_env['PATH']}", "MOCK_LOG": str(log),
                        "MOCK_SQL_LOG": str(sql_log), "MOCK_GCLOUD_ACCOUNT": ACCOUNT,
                        "MOCK_GCLOUD_PROJECT": PROJECT, "MILO_DB": "postgres://ignored"})
        run_env.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env=run_env, cwd=repo, timeout=120)

    run.repo, run.head, run.log, run.sql_log, run.audit = repo, head, log, sql_log, audit  # type: ignore[attr-defined]
    return run


def apply_args(tooling, resolution: str, *extra: str) -> list[str]:
    return ["--run-id", RUN, "--resolution", resolution, "--apply",
            "--environment", "production", "--expected-project", PROJECT,
            "--expected-account", ACCOUNT, "--expected-sha", tooling.head,
            "--confirm-production-change", "--database-url-env", "MILO_DB",
            "--audit-file", str(tooling.audit), *extra]


def assert_strict(result) -> None:
    assert "MOCK-UNAUTHORIZED" not in result.stdout + result.stderr, result.stdout + result.stderr


def decisions(tooling) -> list[str]:
    """Every guarded lost-launch decision the tool sent."""
    return [block for block in tooling.sql_log.read_text().split("----\n")
            if "public.reconcile_lost_launch(" in block]


def decision_calls(tooling) -> list[str]:
    return [line for line in tooling.log.read_text().splitlines()
            if line.startswith("psql") and "outcome=" in line]


def test_the_default_mode_lists_lost_launches_read_only(tooling):
    lost = f"{RUN} | 2026-09-23 | attempt=1 | quiet_for=4000s | worker=none"
    result = tooling("--database-url-env", "MILO_DB", MOCK_LIST_LOST=lost)
    assert_strict(result)
    assert result.returncode == 0, result.stdout
    assert "[WARN] list:lost-launch" in result.stdout and lost in result.stdout
    assert "[PASS] list — no unresolved launch_unknown runs found" in result.stdout
    sql = tooling.sql_log.read_text()
    assert "make_interval(secs => 1800)" in sql  # the default threshold
    assert "public.reconcile_lost_launch(" not in sql
    assert "update " not in sql.lower() and "insert " not in sql.lower()
    assert not tooling.audit.exists()


def test_a_threshold_below_the_floor_is_refused_before_anything_is_read(tooling):
    result = tooling(*apply_args(tooling, "confirmed-not-launched", "--min-quiet-seconds", "60"),
                     MILO_OPERATOR_ACK=ACK)
    assert result.returncode != 0
    assert "[BLOCKED] min-quiet-seconds" in result.stdout
    assert tooling.log.read_text() == ""


@pytest.mark.parametrize("resolution,outcome,target", [
    ("confirmed-not-launched", "not_launched", "launch_failed"),
    ("confirmed-launched", "launched", "launched"),
])
def test_a_lost_launch_decision_is_one_guarded_call_audited_only_after_success(
        tooling, resolution, outcome, target):
    result = tooling(*apply_args(tooling, resolution, "--min-quiet-seconds", "2700"),
                     MILO_OPERATOR_ACK=ACK, MOCK_RECONCILE_OUT=f"true|{target}|queued")
    assert_strict(result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[PASS] apply:{resolution}" in result.stdout
    assert "nothing was launched" in result.stdout
    calls = decision_calls(tooling)
    assert len(calls) == 1
    for variable in (f"run_id={RUN}", f"outcome={outcome}", "quiet=2700", f"operator={ACCOUNT}"):
        assert variable in calls[0]
    (sql,) = decisions(tooling)
    assert ("public.reconcile_lost_launch(:'run_id'::uuid, :'outcome', :'quiet'::integer, "
            ":'operator')") in sql
    # The launch_unknown path's blind guarded UPDATE is never used for it.
    assert "with upd as (update" not in tooling.sql_log.read_text()
    audit = tooling.audit.read_text()
    for field in (f"run={RUN}", f"resolution={resolution}", "prev_launch_state=launching",
                  f"new_launch_state={target}", "run_status=queued", f"operator={ACCOUNT}",
                  "min_quiet_seconds=2700"):
        assert field in audit
    # Nothing is ever launched: no gcloud call but the identity checks.
    gcloud = [line for line in tooling.log.read_text().splitlines() if line.startswith("gcloud")]
    assert gcloud and all("config get-value" in line for line in gcloud)


def test_a_run_whose_worker_holds_a_lease_is_never_reconciled(tooling):
    result = tooling(*apply_args(tooling, "confirmed-not-launched"), MILO_OPERATOR_ACK=ACK,
                     MOCK_READ="launching|queued|active")
    assert_strict(result)
    assert result.returncode != 0
    assert "[BLOCKED] apply:lease" in result.stdout
    assert decisions(tooling) == [] and not tooling.audit.exists()


@pytest.mark.parametrize("code", ["LOST_LAUNCH_NOT_QUIET", "LOST_LAUNCH_CLAIMED",
                                  "LOST_LAUNCH_TRACED", "LOST_LAUNCH_WRONG_STATE",
                                  "LOST_LAUNCH_NOT_FOUND", "LOST_LAUNCH_CHANGED"])
def test_a_database_refusal_is_blocked_and_never_audited(tooling, code):
    result = tooling(*apply_args(tooling, "confirmed-not-launched"), MILO_OPERATOR_ACK=ACK,
                     MOCK_RECONCILE_ERROR=code)
    assert_strict(result)
    assert result.returncode != 0
    assert "[BLOCKED] apply:refused" in result.stdout and code in result.stdout
    assert not tooling.audit.exists()


def test_an_already_decided_run_is_an_idempotent_no_op(tooling):
    result = tooling(*apply_args(tooling, "confirmed-not-launched"), MILO_OPERATOR_ACK=ACK,
                     MOCK_RECONCILE_OUT="false|launch_failed|queued")
    assert result.returncode == 0, result.stdout
    assert "[NOT_APPLICABLE] apply:confirmed-not-launched" in result.stdout
    assert not tooling.audit.exists()


@pytest.mark.parametrize("answer", ["true|launched|queued", "true|launch_failed|running",
                                    "garbage"])
def test_an_unexpected_answer_fails_closed(tooling, answer):
    result = tooling(*apply_args(tooling, "confirmed-not-launched"), MILO_OPERATOR_ACK=ACK,
                     MOCK_RECONCILE_OUT=answer)
    assert result.returncode != 0
    assert "[BLOCKED] apply:confirmed-not-launched" in result.stdout
    assert not tooling.audit.exists()


@pytest.mark.parametrize("resolution,state", [
    # requeue is only ever valid from launch_failed: a lost launch is decided first.
    ("requeue", "launching|queued|none"),
    # A worker claimed it: whatever its label says, it is not a lost launch.
    ("confirmed-not-launched", "launching|running|none"),
    ("confirmed-launched", "launching|starting|active"),
])
def test_anything_but_a_queued_lost_launch_is_refused_before_any_call(tooling, resolution, state):
    result = tooling(*apply_args(tooling, resolution), MILO_OPERATOR_ACK=ACK, MOCK_READ=state)
    assert_strict(result)
    assert result.returncode != 0
    assert "[BLOCKED] apply:state" in result.stdout
    assert decisions(tooling) == [] and not tooling.audit.exists()


@pytest.mark.parametrize("drop,blocked", [
    ("--confirm-production-change", "apply-guard:confirm"),
    ("--environment", "apply-guard:environment"),
    ("ack", "apply-guard:ack"),
])
def test_no_decision_without_the_full_guard(tooling, drop, blocked):
    args = apply_args(tooling, "confirmed-not-launched")
    env = {"MILO_OPERATOR_ACK": ACK}
    if drop == "ack":
        env = {}
    else:
        index = args.index(drop)
        del args[index:index + (1 if drop == "--confirm-production-change" else 2)]
    result = tooling(*args, **env)
    assert_strict(result)
    assert result.returncode != 0
    assert f"[BLOCKED] {blocked}" in result.stdout, result.stdout
    assert decisions(tooling) == [] and not tooling.audit.exists()


def test_leave_unresolved_is_recorded_for_a_lost_launch(tooling):
    result = tooling(*apply_args(tooling, "leave-unresolved"), MILO_OPERATOR_ACK=ACK,
                     MOCK_LEAVE_STATE="launching")
    assert_strict(result)
    assert result.returncode == 0, result.stdout
    assert "db_verified=verified-launching" in tooling.audit.read_text()
    assert decisions(tooling) == []


def test_launch_unknown_keeps_its_own_guarded_update(tooling):
    result = tooling(*apply_args(tooling, "confirmed-not-launched"), MILO_OPERATOR_ACK=ACK,
                     MOCK_READ="launch_unknown|queued|none", MOCK_UPDATE="1|launch_failed|queued")
    assert_strict(result)
    assert result.returncode == 0, result.stdout
    assert decisions(tooling) == []
    update = [block for block in tooling.sql_log.read_text().split("----\n")
              if "with upd as (update" in block]
    assert len(update) == 1 and "launch_state = 'launch_unknown'" in update[0]
    audit = tooling.audit.read_text()
    assert "prev_launch_state=launch_unknown" in audit and "min_quiet_seconds" not in audit
