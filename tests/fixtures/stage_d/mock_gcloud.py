"""A stateful `gcloud` stand-in for the Stage D toolkit tests.

The Stage D cleanup guarantees are about an END STATE — paid execution
off, both provider aliases unbound on both surfaces, zero active Worker
executions, both disposable probe jobs absent. Asserting that a script
"called the kill switch" proves none of it. So this mock keeps real
mutable state in a JSON file and serves describes from it, which lets the
tests run the REAL `kill-switch.sh` and `07-post-run-lockdown.sh` and
then assert the state those scripts actually produced.

It is deliberately only as faithful as the toolkit needs: the flag/secret
surfaces of the Worker job and API service, the execution listing, the
job listing, and the handful of read-only describes the gates perform.
Anything the toolkit does not call is not implemented, and an
unrecognised invocation FAILS loudly rather than silently returning 0 —
a permissive mock would hide exactly the bugs these tests exist to catch.

Driven by two environment variables:
  MOCK_STATE  — path to the JSON state file (created on first use)
  MOCK_LOG    — path to an invocation log (one argv line per call)
"""

from __future__ import annotations

import json
import os
import sys

REGISTRY = "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent"
RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"
API_DIGEST = "sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6"
WORKER_DIGEST = "sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5"
WORKER_JOB = "milo-agent-worker"
API_SERVICE = "milo-agent-api"
READY_REVISION = "milo-agent-api-00080-nm8"
DB_PROBE = "stage-d-db-probe"
GW_PROBE = "stage-d-gw-probe"
PROBE_IMAGE_REPO = "docker.io/library/python"
PROBE_IMAGE_DIGEST = "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
GATEWAY_SA = "milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"

DEFAULT_FLAGS = {
    "MILO_ENABLE_RUN_CREATION": "false",
    "MILO_ENABLE_PROPOSAL_MUTATIONS": "false",
    "MILO_ENABLE_PROPOSAL_READS": "false",
    "MILO_ENABLE_RUN_CANCELLATION": "false",
    "MILO_ENABLE_EXECUTION_CONTROL": "false",
    "MILO_ENABLE_PAID_EXECUTION": "false",
    "MILO_ENABLE_CATALOG_EXECUTION": "false",
    "JOB_LAUNCHER": "disabled",
}


def default_state() -> dict:
    return {
        "worker_env": dict(DEFAULT_FLAGS),
        "worker_secrets": [],
        "api_env": dict(DEFAULT_FLAGS),
        "api_secrets": [],
        # Seven terminal executions: the discovered live baseline.
        "executions": [
            {"name": f"milo-agent-worker-{suffix}", "terminal": True}
            for suffix in ("mcfrx", "gggdc", "dk4xv", "gnj5d", "fvfcb", "2tckh", "bw8kj")
        ],
        "jobs": [WORKER_JOB],
        "govcheck_ok": True,
        "govcheck_available": True,
        "fail_commands": [],
        # Per-mode verdicts the probe executions report.
        "probe_verdicts": {"govcheck": True, "terminalize": True},
        # Modes whose execution deliberately emits NO structured record.
        "probe_silent_modes": [],
        # Retained records from an OLDER, deleted-and-recreated job of
        # the same name. Only an UNATTRIBUTED (job-name-only) log query
        # can see these — which is exactly the bug the attributed query
        # fixes.
        "stale_logs": [],
        # execution name -> structured records produced by it.
        "execution_logs": {},
        "probe_executions": {},
        "next_execution": 1,
        # Probe job templates, keyed by job name.
        "probe_jobs": {},
    }


def load() -> dict:
    path = os.environ["MOCK_STATE"]
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        state = default_state()
    for key, value in default_state().items():
        state.setdefault(key, value)
    return state


def save(state: dict) -> None:
    with open(os.environ["MOCK_STATE"], "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def flag_value(arg: str) -> str:
    return arg.split("=", 1)[1] if "=" in arg else ""


def apply_env_updates(env: dict, raw: str) -> None:
    for pair in raw.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            env[key.strip()] = value.strip()


def container_env(env: dict, secrets: list[str]) -> list[dict]:
    entries = [{"name": k, "value": v} for k, v in sorted(env.items())]
    entries += [{"name": s, "valueFrom": {"secretKeyRef": {"key": "latest", "name": s}}}
                for s in sorted(secrets)]
    return entries


def worker_job_json(state: dict, image: str | None = None) -> dict:
    return {"spec": {"template": {"spec": {"template": {"spec": {
        "serviceAccountName": "milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com",
        "containers": [{
            "image": image or f"{REGISTRY}/worker:{RELEASE_SHA}",
            "env": container_env(state["worker_env"], state["worker_secrets"]),
        }],
    }}}}}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "latestCreatedExecution": {"name": state["executions"][0]["name"]} if state["executions"] else {},
    }}


def api_service_json(state: dict) -> dict:
    return {
        "spec": {"template": {"spec": {"containers": [{
            "image": f"{REGISTRY}/api:{RELEASE_SHA}",
            "env": container_env(state["api_env"], state["api_secrets"]),
        }]}}},
        "status": {
            "url": "https://milo-agent-api-beplbca7yq-uc.a.run.app",
            "latestReadyRevisionName": READY_REVISION,
            "traffic": [{"revisionName": READY_REVISION, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def api_revision_json(state: dict) -> dict:
    return {
        "metadata": {"name": READY_REVISION},
        "spec": {"containers": [{
            "image": f"{REGISTRY}/api@{API_DIGEST}",
            "env": container_env(state["api_env"], state["api_secrets"]),
        }]},
    }


def default_probe_job(name: str) -> dict:
    if name == DB_PROBE:
        return {"image": f"{PROBE_IMAGE_REPO}@{PROBE_IMAGE_DIGEST}", "sa": API_SA,
                # env name -> (Secret Manager secret, version)
                "secrets": {"SUPABASE_URL": ["SUPABASE_URL", "latest"],
                            "SUPABASE_SERVICE_ROLE_KEY": ["SUPABASE_SECRET_KEY", "latest"]}}
    return {"image": f"{PROBE_IMAGE_REPO}@{PROBE_IMAGE_DIGEST}", "sa": GATEWAY_SA, "secrets": {}}


def probe_job_json(state: dict, name: str) -> dict:
    spec = (state.get("probe_jobs") or {}).get(name) or default_probe_job(name)
    env = [{"name": "PROBE_SOURCE_GZIP_B64", "value": "<elided>"}]
    for env_name, ref in (spec.get("secrets") or {}).items():
        secret, version = (ref if isinstance(ref, (list, tuple)) else (ref, "latest"))
        env.append({"name": env_name,
                    "valueFrom": {"secretKeyRef": {"name": secret, "key": version}}})
    return {"spec": {"template": {"spec": {"template": {"spec": {
        "serviceAccountName": spec.get("sa"),
        "containers": [{"image": spec.get("image"), "env": env}],
    }}}}}}


def executions_json(state: dict) -> list[dict]:
    out = []
    for execution in state["executions"]:
        status = ({"completionTime": "2026-08-24T21:47:38Z",
                   "conditions": [{"type": "Completed", "status": "True"}]}
                  if execution["terminal"] else {"completionTime": None})
        out.append({"metadata": {"name": execution["name"]}, "status": status})
    return out


def main() -> int:
    args = sys.argv[1:]
    joined = " ".join(args)
    if os.environ.get("MOCK_LOG"):
        with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as fh:
            fh.write(joined + "\n")
    state = load()

    # Deliberate failure injection: any command whose text contains one of
    # these substrings exits non-zero.
    for needle in state.get("fail_commands", []):
        if needle in joined:
            print(f"ERROR: (gcloud) injected failure for {needle!r}", file=sys.stderr)
            return 1

    def has(*needles: str) -> bool:
        return all(n in joined for n in needles)

    # ---- mutations -----------------------------------------------------
    if has("run jobs update", WORKER_JOB):
        for arg in args:
            if arg.startswith("--update-env-vars"):
                apply_env_updates(state["worker_env"], flag_value(arg))
            elif arg.startswith("--update-secrets"):
                for pair in flag_value(arg).split(","):
                    name = pair.split("=", 1)[0].strip()
                    if name and name not in state["worker_secrets"]:
                        state["worker_secrets"].append(name)
            elif arg.startswith("--remove-secrets"):
                name = flag_value(arg).strip()
                if name not in state["worker_secrets"]:
                    save(state)
                    print(f"ERROR: secret {name} not bound", file=sys.stderr)
                    return 1
                state["worker_secrets"].remove(name)
            elif arg.startswith("--remove-env-vars"):
                name = flag_value(arg).strip()
                if name not in state["worker_env"]:
                    save(state)
                    print(f"ERROR: env var {name} not set", file=sys.stderr)
                    return 1
                del state["worker_env"][name]
        save(state)
        return 0

    if has("run services update", API_SERVICE):
        for arg in args:
            if arg.startswith("--update-env-vars"):
                apply_env_updates(state["api_env"], flag_value(arg))
            elif arg.startswith("--remove-secrets"):
                name = flag_value(arg).strip()
                if name not in state["api_secrets"]:
                    save(state)
                    print(f"ERROR: secret {name} not bound", file=sys.stderr)
                    return 1
                state["api_secrets"].remove(name)
            elif arg.startswith("--remove-env-vars"):
                name = flag_value(arg).strip()
                if name not in state["api_env"]:
                    save(state)
                    print(f"ERROR: env var {name} not set", file=sys.stderr)
                    return 1
                del state["api_env"][name]
        save(state)
        return 0

    if has("run jobs create"):
        name = args[args.index("create") + 1]
        if name not in state["jobs"]:
            state["jobs"].append(name)
        save(state)
        return 0

    if has("run jobs delete"):
        name = args[args.index("delete") + 1]
        if name not in state["jobs"]:
            print(f"ERROR: job {name} not found", file=sys.stderr)
            return 1
        state["jobs"].remove(name)
        save(state)
        return 0

    if has("run jobs executions cancel"):
        name = args[args.index("cancel") + 1]
        for execution in state["executions"]:
            if execution["name"] == name:
                execution["terminal"] = True
        save(state)
        return 0

    if has("run jobs execute"):
        job = args[args.index("execute") + 1]
        if not state.get("govcheck_available", True):
            print("ERROR: probe execution failed", file=sys.stderr)
            return 1
        mode = ""
        for arg in args:
            if "STAGE_D_MODE=" in arg:
                mode = arg.split("STAGE_D_MODE=", 1)[1].split(":::", 1)[0].strip()
        index = int(state.get("next_execution", 1))
        state["next_execution"] = index + 1
        exec_name = f"{job}-exec{index}"
        verdicts = state.get("probe_verdicts") or {}
        ok = bool(verdicts.get(mode, True))
        state.setdefault("probe_executions", {})[exec_name] = {
            "job": job, "mode": mode, "ok": ok,
        }
        records = []
        if mode and mode not in (state.get("probe_silent_modes") or []):
            record = {"stage_d_probe": mode, "ok": ok}
            if not ok:
                record["problems"] = [f"injected {mode} failure"]
            records.append(record)
        state.setdefault("execution_logs", {})[exec_name] = records
        save(state)
        print(exec_name)
        return 0

    # ---- reads ---------------------------------------------------------
    if has("run jobs executions list"):
        print(json.dumps(executions_json(state)))
        return 0

    if has("run jobs executions describe"):
        name = args[args.index("describe") + 1]
        probe = (state.get("probe_executions") or {}).get(name)
        if probe is not None:
            print(json.dumps({
                "metadata": {"name": name},
                "status": {"completionTime": "2026-09-19T00:00:00Z",
                           "conditions": [{"type": "Completed",
                                           "status": "True" if probe["ok"] else "False"}]},
            }))
            return 0
        print(json.dumps({
            "metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [
                {"image": f"{REGISTRY}/worker@{WORKER_DIGEST}"}]}}},
            "status": {"completionTime": "2026-09-19T00:00:00Z",
                       "conditions": [{"type": "Completed", "status": "True"}]},
        }))
        return 0

    if has("run jobs list"):
        for name in state["jobs"]:
            print(name)
        return 0

    if has("run jobs describe", WORKER_JOB):
        print(json.dumps(worker_job_json(state)))
        return 0

    if has("run jobs describe"):
        name = args[args.index("describe") + 1]
        if name in (DB_PROBE, GW_PROBE):
            if name not in state["jobs"]:
                print(f"ERROR: job {name} not found", file=sys.stderr)
                return 1
            print(json.dumps(probe_job_json(state, name)))
            return 0

    if has("run services describe", API_SERVICE):
        if "value(status.conditions" in joined:
            print("True")
        else:
            print(json.dumps(api_service_json(state)))
        return 0

    if has("run revisions describe"):
        print(json.dumps(api_revision_json(state)))
        return 0

    if has("secrets get-iam-policy"):
        print(json.dumps({"bindings": [{
            "role": "roles/secretmanager.secretAccessor",
            "members": ["serviceAccount:milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"],
        }]}))
        return 0

    if has("run jobs get-iam-policy"):
        print(json.dumps({"bindings": [{
            "role": "roles/run.jobsExecutorWithOverrides",
            "members": ["serviceAccount:milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"],
        }]}))
        return 0

    if has("artifacts docker images list"):
        print(json.dumps([
            {"package": f"{REGISTRY}/api", "version": API_DIGEST, "tags": RELEASE_SHA},
            {"package": f"{REGISTRY}/worker", "version": WORKER_DIGEST, "tags": RELEASE_SHA},
        ]))
        return 0

    if has("logging read"):
        # An ATTRIBUTED query names one execution and may see only that
        # execution's records. An UNATTRIBUTED (job-name-only) query also
        # sees retained records from older jobs of the same name — the
        # stale-log hazard the attributed form exists to eliminate.
        exec_name = ""
        for arg in args:
            if 'execution_name"=' in arg:
                exec_name = arg.split('execution_name"=', 1)[1].split()[0].strip()
        if exec_name:
            records = (state.get("execution_logs") or {}).get(exec_name, [])
        else:
            records = list(state.get("stale_logs") or [])
            for entries in (state.get("execution_logs") or {}).values():
                records.extend(entries)
            if not records:
                legacy = {"stage_d_probe": "govcheck", "ok": bool(state.get("govcheck_ok", True))}
                if not legacy["ok"]:
                    legacy["problems"] = ["Government capture run was CLAIMED"]
                records = [legacy]
        print(json.dumps([{"textPayload": json.dumps(r)} for r in records]))
        return 0

    # An unrecognised invocation must never be a silent success.
    print(f"MOCK GCLOUD: unhandled invocation: {joined}", file=sys.stderr)
    return 9


if __name__ == "__main__":
    sys.exit(main())
