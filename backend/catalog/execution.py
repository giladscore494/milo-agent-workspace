"""CODE-2: the one independent switch for the catalog execution path.

Why this exists
---------------

Catalog PR3 wired a real capability into every `swarm_v2` run: the bounded
read-only Government tool, its evidence mapper, and the lease-guarded canonical
promotion pipeline. All three were constructed unconditionally. Nothing in the
repository could stop them individually -- the global switches
(`MILO_ENABLE_RUN_CREATION`, `MILO_ENABLE_PAID_EXECUTION`, `JOB_LAUNCHER`) can
stop *everything*, which is not the same thing and is not a rollback anyone
wants to reach for over a catalog defect.

What kept the path harmless in practice was a property of the deployed
environment -- a catalog schema with no usable snapshot in it -- and that is an
accident, not a control. The moment a snapshot exists the capability is live,
with no way to close it that does not also stop the product.

So: ONE flag, server-side, default off.

What it is, exactly
-------------------

*   **One flag, not several.** A second partially-overlapping catalog flag
    would create a posture where "the catalog is off" is true of one switch and
    false of another, which is worse than having none.
*   **Off unless explicitly, recognisably on.** Unset is off. `false` is off.
    Empty is off. A value nobody recognises is a misconfiguration, and the safe
    reading of a misconfiguration is off. The accepted true forms are exactly
    `production_config.TRUE_VALUES` -- the same set `execution_guard` and the
    configuration validator already use -- so there is one spelling of "an
    operator turned this on" in the whole repository.
*   **Server-side only.** It is read from the worker's process environment. It
    has no `NEXT_PUBLIC_` twin, so it never reaches the browser bundle, and it
    is never read from a plan, a task, an event payload, project metadata or
    run metadata: a model that could name this string still could not set it.
*   **Narrow.** Enabling it enables the catalog capability inside a Swarm V2
    run that is ALREADY happening. It creates no run, opens no execution route,
    authorizes no paid call, deploys nothing, schedules nothing, starts no
    capture and reveals no UI. Every existing kill switch remains authoritative
    above it: with run creation off there is no run for it to be enabled inside.

What it deliberately does NOT do
--------------------------------

It does not delete, mutate or hide a single durable catalog row. Disabling the
catalog path stops this worker from READING candidates and WRITING canonical
facts; whatever the database already holds is left exactly as it is, so
re-enabling the flag resumes from the same durable state rather than from a
gap. The rollback runbook states that explicitly, because an operator reaching
for a kill switch mid-incident deserves to know it is not destructive.

The value lives in the deployment environment (Cloud Run job configuration),
never in Supabase and never in this repository: `scripts/check_unsafe_defaults.py`
fails the build if any tracked file commits it as enabled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from backend.production_config import TRUE_VALUES

#: The single server-side name. It is spelled once, here, and every deployment
#: contract, validator, kill switch and readiness document refers to this one.
CATALOG_EXECUTION_FLAG = "MILO_ENABLE_CATALOG_EXECUTION"


def catalog_execution_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Is the catalog execution path enabled for this process?

    `env` exists so a validator can ask the same question of a deployment's
    recorded environment without mutating its own. Production calls this with
    no argument, which reads `os.environ` and nothing else -- in particular not
    anything a model, a plan or an event payload could have influenced.
    """
    source = os.environ if env is None else env
    return (source.get(CATALOG_EXECUTION_FLAG) or "").strip().lower() in TRUE_VALUES


__all__ = ["CATALOG_EXECUTION_FLAG", "catalog_execution_enabled"]
