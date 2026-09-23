#!/usr/bin/env python3
"""Fail closed unless every Cloud Run worker execution has completed."""

import json
import subprocess
import sys


def main() -> int:
    job, region, project = sys.argv[1:]
    result = subprocess.run(
        ["gcloud", "run", "jobs", "executions", "list", "--job", job,
         "--region", region, "--project", project, "--format=json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        print(f"cannot list worker executions (gcloud exit {result.returncode}): "
              f"{result.stderr.strip()}")
        return 2

    try:
        executions = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("cannot verify worker executions: gcloud returned invalid JSON")
        return 2
    if not isinstance(executions, list):
        print("cannot verify worker executions: expected a JSON list")
        return 2

    pending = []
    for execution in executions:
        if not isinstance(execution, dict):
            print("cannot verify worker executions: invalid execution entry")
            return 2
        metadata = execution.get("metadata")
        status = execution.get("status")
        if not isinstance(metadata, dict) or not metadata.get("name") or not isinstance(status, dict):
            print("cannot verify worker executions: missing name or status")
            return 2
        if not status.get("completionTime"):
            pending.append(metadata["name"])

    if pending:
        print(f"{len(pending)} worker execution(s) still without completionTime: "
              f"{', '.join(pending)}; wait for them to terminalize before capturing or deploying")
        return 1
    print(f"{len(executions)} execution(s), all terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
