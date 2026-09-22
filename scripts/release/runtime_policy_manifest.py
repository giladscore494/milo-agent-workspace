#!/usr/bin/env python3
"""The one operator-facing view of every runtime policy binding.

DERIVED, never maintained by hand. Every row comes from
``backend.runtime_policy.POLICY_DIMENSIONS`` -- the same declaration the
runtime resolves itself from -- so this table cannot fall behind the policy
the way a duplicated list in a document does. If a dimension is added,
renamed, or becomes mandatory, this manifest says so on the next run without
anyone editing it.

Three questions it answers for an operator, per dimension:

*   is it MANDATORY for paid execution (derived from the policy: a dimension
    is mandatory exactly when leaving it unset would not reproduce the
    reviewed value);
*   what the repository reviewed it AT, and whether a runtime default exists;
*   what the LIVE deployment currently binds, when ``--live-from-cloud-run``
    is given -- read with ``gcloud run ... describe``, which is read-only.

A mandatory dimension with no runtime default and no live binding is a
**FAIL**: the runtime fails closed on it, so the deployment would not start
paid execution. That is the whole point of running this before a release.

No secret is read or printed. Policy dimensions are limits and counts; the
credentials that sit beside them are checked for existence by
``scripts/deploy/production-preflight.sh``, never by value.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.runtime_policy import (  # noqa: E402
    MANDATORY_FOR_PAID_EXECUTION, POLICY_DIMENSIONS)

#: Dimensions whose value is configuration, not a credential. Every policy
#: dimension is in this class -- the distinction exists so the manifest can
#: state it explicitly rather than leaving an operator to assume.
PLAIN_CONFIG = "plain config"

#: Where a dimension's value legitimately comes from.
SOURCE_ENV = "Cloud Run env var"
SOURCE_CODE = "runtime default (code)"
SOURCE_UNBOUND = "UNBOUND"


class ManifestError(RuntimeError):
    """A live read could not be performed, so no claim is made about it."""


def _gcloud_env(project: str, region: str, *, service: str | None = None,
                job: str | None = None) -> dict[str, str]:
    """Read one resource's plain environment variables, read-only.

    A failure raises rather than returning an empty mapping: "no variables"
    and "we could not look" are different facts, and reporting the second as
    the first would mark every mandatory dimension FAIL for a deployment that
    is actually fine.
    """
    if shutil.which("gcloud") is None:
        raise ManifestError("gcloud is not installed or not on PATH")
    if service:
        args = ["gcloud", "run", "services", "describe", service,
                "--format=json(spec.template.spec.containers[0].env)"]
    else:
        args = ["gcloud", "run", "jobs", "describe", str(job),
                "--format=json(spec.template.spec.template.spec.containers[0].env)"]
    args += [f"--region={region}", f"--project={project}"]
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise ManifestError(f"gcloud describe failed: {detail[-1] if detail else 'unknown error'}")
    try:
        document = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"gcloud returned unparseable JSON: {exc}") from None
    node: object = document
    for key in ("spec", "template", "spec", "template", "spec", "containers"):
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, Mapping):
            continue
    containers = node if isinstance(node, list) else []
    resolved: dict[str, str] = {}
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for entry in container.get("env") or []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            # A secret-backed variable states a reference, not a value. It is
            # recorded as bound-without-value: the manifest must never print
            # what is behind it, and it has no business reading it either.
            if isinstance(name, str) and "value" in entry:
                resolved[name] = str(entry.get("value") or "")
            elif isinstance(name, str):
                resolved[name] = "<secret-backed>"
    return resolved


def _rows(live: Mapping[str, str] | None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for dimension in POLICY_DIMENSIONS:
        env_key = dimension.env_key or ""
        mandatory = dimension.name in MANDATORY_FOR_PAID_EXECUTION
        has_default = dimension.runtime_default is not None
        live_value = None
        if live is not None and env_key:
            live_value = live.get(env_key)

        if live is None:
            source = SOURCE_ENV if env_key else SOURCE_CODE
            status = "UNKNOWN"
        elif live_value not in (None, ""):
            source = SOURCE_ENV
            status = "PASS"
        elif has_default:
            source = SOURCE_CODE
            status = "PASS"
        elif not env_key:
            source = SOURCE_CODE
            status = "PASS"
        else:
            source = SOURCE_UNBOUND
            status = "FAIL" if mandatory else "WARN"

        rows.append({
            "dimension": dimension.name,
            "env_var": env_key or "(not env-configurable)",
            "required": "MANDATORY" if mandatory else "optional",
            "reviewed_value": dimension.reviewed_text,
            "runtime_default": "-" if dimension.runtime_default is None
                               else str(dimension.runtime_default),
            "value_class": PLAIN_CONFIG,
            "source": source,
            "live_value": "-" if live_value in (None, "") else str(live_value),
            "status": status,
            "enforced_by": dimension.enforced_by,
        })
    return rows


def _render_text(rows: Sequence[Mapping[str, object]], surface: str) -> str:
    headers = ("DIMENSION", "ENV VAR", "REQUIRED", "REVIEWED", "DEFAULT", "LIVE", "STATUS")
    keys = ("dimension", "env_var", "required", "reviewed_value", "runtime_default",
            "live_value", "status")
    widths = [len(h) for h in headers]
    for row in rows:
        for index, key in enumerate(keys):
            widths[index] = max(widths[index], len(str(row[key])))
    lines = [f"RuntimePolicy manifest — live surface: {surface}",
             "Value class for every dimension below: plain config (never Secret Manager).",
             ""]
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(row[key]).ljust(widths[i])
                               for i, key in enumerate(keys)).rstrip())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator-facing RuntimePolicy manifest, derived from canonical code.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--live-from-cloud-run", action="store_true",
                        help="read live values from Cloud Run (read-only describes)")
    parser.add_argument("--project", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--api-service", default=None)
    parser.add_argument("--worker-job", default=None)
    parser.add_argument("--surface", choices=("worker", "api"), default="worker",
                        help="which deployed surface supplies the live values "
                             "(the worker runs the engine, so it is the default)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    live: dict[str, str] | None = None
    surface = "not read (declared values only)"
    if args.live_from_cloud_run:
        missing = [name for name, value in (("--project", args.project),
                                            ("--region", args.region))
                   if not value]
        target = args.worker_job if args.surface == "worker" else args.api_service
        if not target:
            missing.append("--worker-job" if args.surface == "worker" else "--api-service")
        if missing:
            print(f"FAIL: --live-from-cloud-run requires {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            if args.surface == "worker":
                live = _gcloud_env(args.project, args.region, job=target)
            else:
                live = _gcloud_env(args.project, args.region, service=target)
        except ManifestError as exc:
            print(f"FAIL: could not read live values: {exc}", file=sys.stderr)
            return 2
        surface = f"{args.surface} {target} ({args.project}/{args.region})"

    rows = _rows(live)
    if args.format == "json":
        print(json.dumps({"surface": surface, "dimensions": rows}, indent=2, sort_keys=True))
    else:
        print(_render_text(rows, surface))

    failed = [row for row in rows if row["status"] == "FAIL"]
    if failed:
        print("", file=sys.stderr)
        for row in failed:
            print(f"FAIL: mandatory-for-paid dimension {row['dimension']} is unbound — "
                  f"set {row['env_var']} on the deployed surface "
                  f"(reviewed value: {row['reviewed_value']})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
