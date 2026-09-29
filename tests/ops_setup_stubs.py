"""A stateful stand-in for `gcloud` and `gh`, for the PR-OBS setup scripts.

One executable, installed as both names on PATH. State lives in a JSON file
(STUB_STATE), every invocation's argv is appended to STUB_LOG, and every
call that would CHANGE something is appended to state["mutations"], so a
test can prove a second run changes nothing. Only the calls
scripts/ops/setup-backup.sh and scripts/ops/setup-sentry.sh make are
understood; anything else fails loudly (exit 90).
"""

from __future__ import annotations

STUB_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["STUB_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([tool] + args) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def mutate(kind, *detail):
    state.setdefault("mutations", []).append([kind, *detail])

def opt(name, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default

def policy(key):
    return state.setdefault("policies", {}).setdefault(key, {"bindings": []})

def bind(key, member, role):
    p = policy(key)
    for b in p["bindings"]:
        if b["role"] == role:
            if member not in b["members"]:
                b["members"].append(member)
            return
    p["bindings"].append({"role": role, "members": [member]})

def done(out="", code=0):
    save()
    if out:
        print(out)
    sys.exit(code)

joined = " ".join(args)
if tool == "gcloud":
    if args[:3] == ["config", "get-value", "account"]:
        done("operator@example.test")
    if args[:2] == ["projects", "describe"]:
        done("123456789")
    if args[:2] == ["projects", "get-iam-policy"]:
        done(json.dumps(policy("project")))
    if args[:3] == ["services", "list", "--enabled"]:
        api = opt("--filter").split(":", 1)[1]
        done(api if api in state.setdefault("apis", []) else "")
    if args[:2] == ["services", "enable"]:
        state["apis"].append(args[2]); mutate("enable", args[2]); done()
    if args[:3] == ["storage", "buckets", "describe"]:
        b = state.setdefault("buckets", {}).get(args[3])
        done(json.dumps(b)) if b else done("", 1)
    if args[:3] in (["storage", "buckets", "create"], ["storage", "buckets", "update"]):
        name = args[3]
        b = state.setdefault("buckets", {}).setdefault(name, {"location": (opt("--location") or "").upper()})
        if "--uniform-bucket-level-access" in args: b["uniform_bucket_level_access"] = True
        if "--public-access-prevention" in args: b["public_access_prevention"] = "enforced"
        if opt("--retention-period"): b["retention_policy"] = {"retentionPeriod": opt("--retention-period").rstrip("s"), "isLocked": False}
        if opt("--lifecycle-file"): b["lifecycle_config"] = json.loads(Path(opt("--lifecycle-file")).read_text())
        mutate(args[2] + "-bucket", name); done()
    if args[:3] == ["storage", "buckets", "get-iam-policy"]:
        done(json.dumps(policy("bucket:" + args[3])))
    if args[:3] == ["storage", "buckets", "add-iam-policy-binding"]:
        bind("bucket:" + args[3], opt("--member"), opt("--role")); mutate("bind", args[3], opt("--role")); done()
    if args[:3] == ["iam", "service-accounts", "describe"]:
        done("") if args[3] in state.setdefault("accounts", []) else done("", 1)
    if args[:3] == ["iam", "service-accounts", "create"]:
        state["accounts"].append(args[3] + "@" + opt("--project") + ".iam.gserviceaccount.com")
        mutate("create-sa", args[3]); done()
    if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
        if "sa:" + args[3] in state.get("unreadable_policies", []):
            done("", 1)
        done(json.dumps(policy("sa:" + args[3])))
    if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:
        bind("sa:" + args[3], opt("--member"), opt("--role")); mutate("bind", args[3], opt("--role")); done()
    if args[:3] == ["iam", "workload-identity-pools", "describe"]:
        if args[3] not in state.setdefault("pools", []): done("", 1)
        done(state.setdefault("pool_state", {}).get(args[3], "ACTIVE") if opt("--format") == "value(state)" else "")
    if args[:3] == ["iam", "workload-identity-pools", "create"]:
        state["pools"].append(args[3]); mutate("create-pool", args[3]); done()
    if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
        p = state.setdefault("providers", {}).get(args[4])
        if not p: done("", 1)
        done(p["condition"] if opt("--format") == "value(attributeCondition)" else json.dumps(
            {"attributeCondition": p["condition"], "state": "ACTIVE"}))
    if args[:4] in (["iam", "workload-identity-pools", "providers", "create-oidc"],
                    ["iam", "workload-identity-pools", "providers", "update-oidc"]):
        state.setdefault("providers", {})[args[4]] = {"condition": opt("--attribute-condition"),
                                                      "mapping": opt("--attribute-mapping")}
        mutate(args[3], args[4]); done()
    if args[:2] == ["secrets", "describe"]:
        done("") if args[2] in state.setdefault("secrets", {}) else done("", 1)
    if args[:2] == ["secrets", "create"]:
        state["secrets"][args[2]] = []; mutate("create-secret", args[2]); done()
    if args[:2] == ["secrets", "get-iam-policy"]:
        done(json.dumps(policy("secret:" + args[2])))
    if args[:2] == ["secrets", "add-iam-policy-binding"]:
        bind("secret:" + args[2], opt("--member"), opt("--role")); mutate("bind", args[2], opt("--role")); done()
    if args[:3] == ["secrets", "versions", "access"]:
        versions = state.setdefault("secrets", {}).get(opt("--secret")) or []
        if not versions: done("", 1)
        sys.stdout.write(versions[-1]); done()
    if args[:3] == ["secrets", "versions", "add"]:
        state["secrets"][args[3]].append(sys.stdin.read()); mutate("add-version", args[3]); done()
    if args[:3] == ["secrets", "versions", "list"]:
        done("\n".join(f"{i + 1}" for i, _ in enumerate(state.setdefault("secrets", {}).get(args[3]) or [])))
elif tool == "gh":
    envs = state.setdefault("environments", {})
    if args[:2] == ["auth", "status"]:
        done("")
    if args and args[0] == "api":
        method = opt("-X", "GET")
        path = [a for a in args[1:] if a.startswith("repos/")][0]
        parts = path.split("/")
        if len(parts) == 3:
            done("424242" if "--jq" in args else json.dumps({"id": 424242}))
        name = parts[4]
        if path.endswith("/deployment-branch-policies") or "/deployment-branch-policies/" in path:
            env = envs.get(name)
            if env is None: done("", 1)
            if method == "GET":
                if "--jq" in args:
                    done("\n".join(f'{p["id"]} {p["name"]}' for p in env["branches"]))
                done(json.dumps({"branch_policies": [{"id": p["id"], "name": p["name"], "type": "branch"} for p in env["branches"]]}))
            if method == "POST":
                env["branches"].append({"id": len(env["branches"]) + 100, "name": opt("-f").split("=", 1)[1]})
                mutate("branch-policy", name); done("{}")
            if method == "DELETE":
                env["branches"] = [p for p in env["branches"] if str(p["id"]) != parts[-1]]
                mutate("delete-branch-policy", name); done()
        if method == "GET":
            env = envs.get(name)
            if env is None: done("", 1)
            done(json.dumps({"name": name, "protection_rules": env["rules"],
                             "deployment_branch_policy": env["policy"]}))
        if method == "PUT":
            body = json.loads(sys.stdin.read())
            env = envs.setdefault(name, {"rules": [], "branches": [], "secrets": {}, "variables": {}})
            env["policy"] = body.get("deployment_branch_policy")
            env["rules"] = [] if body.get("reviewers") is None else env["rules"]
            mutate("put-environment", name); done("{}")
    if args[:2] == ["secret", "list"]:
        env = envs.get(opt("--env")) or {"secrets": {}}
        done("\n".join(sorted(env["secrets"])))
    if args[:2] == ["secret", "set"]:
        env = envs.get(opt("--env"))
        if env is None: done("", 1)
        env["secrets"][args[2]] = sys.stdin.read(); mutate("secret-set", args[2]); done()
    if args[:2] == ["variable", "set"]:
        env = envs.get(opt("--env"))
        if env is None: done("", 1)
        if env["variables"].get(args[2]) != opt("--body"):
            mutate("variable-set", args[2])
        env["variables"][args[2]] = opt("--body"); done()
    if args[:2] == ["variable", "list"]:
        env = envs.get(opt("--env")) or {"variables": {}}
        done("\n".join(f"{k}={v}" for k, v in sorted(env["variables"].items())))
print("unexpected " + tool + " invocation: " + joined, file=sys.stderr)
sys.exit(90)
'''
