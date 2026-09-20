#!/usr/bin/env python3
"""The Stage D operating envelope, GENERATED from the canonical runtime policy.

Stage D used to carry its own hand-written transcription of the whole
envelope in ``stage-d-env.sh``. That is how it came to pin
``MILO_PROVIDER_RPM_LIMIT=350`` against an organization ceiling of 80 -- a
value ``backend.provider_scheduler.ProviderLimitsConfig.from_env`` refuses
outright, so the pinned posture could not have started a worker at all. Two
hand-maintained copies of one envelope will always eventually say two
different things; the fix is to stop keeping two.

Everything printed here is read from ``backend/runtime_policy.py``. Nothing
in this file decides a limit, and nothing downstream of it may edit one:
``verify_caps.py`` re-derives the same values and refuses the run if the
environment it was handed disagrees, so a hand edit to the pinned strings is
caught before any run is created.

Usage:
  policy_envelope.py caps              # STAGE_D_CAPS
  policy_envelope.py provider-limits   # STAGE_D_WORKER_PROVIDER_LIMITS
  policy_envelope.py engine-limits     # STAGE_D_WORKER_ENGINE_LIMITS
  policy_envelope.py fingerprint       # the policy digest
  policy_envelope.py document          # the whole canonical document as JSON

Read-only: it imports the policy, prints, and exits. No network, no gcloud,
no database, no environment mutation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.runtime_policy import (  # noqa: E402  (path bootstrap must run first)
    CAP_ENV_PREFIXES, ENGINE_ENV_PREFIXES, PROVIDER_ENV_PREFIXES,
    reviewed_first_run_policy)

POLICY = reviewed_first_run_policy()

#: The three groups Stage D pins separately, because they are applied to
#: different surfaces: caps go on BOTH the API and the Worker, while provider
#: scheduling and engine parallelism belong to the Worker alone.
GROUPS = {
    "caps": CAP_ENV_PREFIXES,
    "provider-limits": PROVIDER_ENV_PREFIXES,
    "engine-limits": ENGINE_ENV_PREFIXES,
}


def expected(group: str) -> dict[str, str]:
    """The exact environment this group must carry. Generated, never typed."""
    return POLICY.env_expectations(prefixes=GROUPS[group])


def rendered(group: str) -> str:
    """The comma-separated form `gcloud --update-env-vars` and the shell use."""
    return ",".join(f"{key}={value}" for key, value in expected(group).items())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    what = argv[1]
    if what in GROUPS:
        print(rendered(what))
        return 0
    if what == "fingerprint":
        print(POLICY.fingerprint())
        return 0
    if what == "document":
        print(json.dumps(POLICY.document(), sort_keys=True, indent=2))
        return 0
    print(f"unknown selector: {what}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
