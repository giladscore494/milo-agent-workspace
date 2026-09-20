#!/usr/bin/env python3
"""The Stage D operating envelope, GENERATED from the canonical runtime policy
and BOUND to the accepted release.

Stage D used to carry its own hand-written transcription of the whole
envelope in ``stage-d-env.sh``. That is how it came to pin
``MILO_PROVIDER_RPM_LIMIT=350`` against an organization ceiling of 80 -- a
value ``backend.provider_scheduler.ProviderLimitsConfig.from_env`` refuses
outright, so the pinned posture could not have started a worker at all. Two
hand-maintained copies of one envelope will always eventually say two
different things; the fix is to stop keeping two.

Why this file also carries a release binding
--------------------------------------------

Generating the envelope from ``backend/runtime_policy.py`` removes the second
transcription, but it introduces a different way for Stage D to be wrong:
this script reads the LOCAL CHECKOUT, while Stage D verifies separately
pinned release IMAGE DIGESTS. An operator running it from a different commit,
a bad merge or a dirty working tree would generate and verify an envelope
that is not the one the deployed images actually enforce, and every check
would pass because each side was being compared with itself.

So the policy is bound to the release twice, and both bindings fail closed:

1. ``PINNED_POLICY_FINGERPRINT`` below is a LITERAL reviewed constant, edited
   in a reviewed commit exactly like ``STAGE_D_API_IMAGE_DIGEST``. A checkout
   whose policy differs from the reviewed one produces a different digest and
   Stage D refuses. This needs no git metadata, so it still holds in a
   container, an archive export or a shallow copy.
2. ``release_binding_problems`` proves the checkout IS the accepted release:
   ``HEAD`` equals ``STAGE_D_RELEASE_SHA`` and the policy source is
   unmodified. ``STAGE_D_RELEASE_SHA`` is the commit the accepted images were
   built from and ``verify_images.py`` is the authority that the running
   images are those digests -- so checkout == release SHA closes the chain
   from "the policy this script printed" to "the policy those images run".

Being unable to PROVE the binding is a refusal, never a pass: no git, a
shallow clone without the commit, or an unreadable tree all fail closed.

Usage:
  policy_envelope.py caps                 # STAGE_D_CAPS
  policy_envelope.py provider-limits      # STAGE_D_WORKER_PROVIDER_LIMITS
  policy_envelope.py engine-limits        # STAGE_D_WORKER_ENGINE_LIMITS
  policy_envelope.py execution-increment  # STAGE_D_AUTHORIZED_EXECUTION_INCREMENT
  policy_envelope.py fingerprint          # the reviewed policy digest
  policy_envelope.py document             # the whole canonical document as JSON
  policy_envelope.py binding              # prove the checkout IS the release

Every selector except ``binding`` refuses unless the checkout's policy matches
the pinned fingerprint. ``binding`` additionally requires the release SHA.

Read-only: it imports the policy, reads git metadata, prints, and exits. No
network, no gcloud, no database, no environment mutation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.runtime_policy import (  # noqa: E402  (path bootstrap must run first)
    CAP_ENV_PREFIXES, ENGINE_ENV_PREFIXES, PROVIDER_ENV_PREFIXES,
    reviewed_first_run_policy)

POLICY = reviewed_first_run_policy()

#: The reviewed policy document's digest, pinned as a LITERAL in this reviewed
#: file — the same kind of constant as the accepted image digests, and changed
#: the same way: deliberately, in a reviewed commit, after an intended change.
#:
#: Regenerate with:
#:   python3 -c 'import sys; sys.path.insert(0, ".");
#:   from backend.runtime_policy import reviewed_first_run_policy as p;
#:   print(p().fingerprint())'
#:
#: `tests/test_runtime_policy_authority.py` fails if this drifts from the
#: policy in the checkout, so editing a reviewed value without re-pinning is
#: caught in CI rather than by a production run.
PINNED_POLICY_FINGERPRINT = "2a184d45707ec5d84b8a513b58fe627a8bf0671f7ae076b2e08d2d0533e4a65f"

#: The source whose content determines the policy document. Kept explicit so
#: the cleanliness check names what it is proving, rather than asserting the
#: whole tree is clean (which no real operator checkout ever is).
POLICY_SOURCE_PATHS = ("backend/runtime_policy.py",)

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


def authorized_execution_increment() -> int:
    """How many NEW paid worker executions this authorization covers.

    Read from the canonical policy's `first_paid_run_execution_cap`, so the
    number of executions Stage D will accept after the run and the number the
    policy authorizes are the same number. Stage D used to compute
    `baseline + 1` in shell arithmetic, which was a second authority for the
    same rule.
    """
    return int(POLICY["first_paid_run_execution_cap"])


def fingerprint_problems() -> list[str]:
    """Does the checkout's policy match the reviewed, pinned one?"""
    actual = POLICY.fingerprint()
    if actual == PINNED_POLICY_FINGERPRINT:
        return []
    return [
        "the runtime policy in this checkout does not match the reviewed "
        f"policy pinned in policy_envelope.py (pinned {PINNED_POLICY_FINGERPRINT[:12]}…, "
        f"checkout {actual[:12]}…) — the envelope Stage D would apply is not "
        "the one that was reviewed"
    ]


def _git(*args: str) -> str | None:
    """Read-only git metadata, or None when it cannot be proven."""
    try:
        result = subprocess.run(("git", "-C", str(REPO_ROOT), *args),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def release_binding_problems(release_sha: str | None) -> list[str]:
    """Prove this checkout IS the accepted release, or say why it cannot be.

    Every failure mode is a refusal, including "cannot tell": a Stage D run
    that cannot prove which code generated its envelope has not verified
    anything, whatever the individual checks printed.
    """
    problems = fingerprint_problems()
    sha = (release_sha or "").strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        problems.append(
            "STAGE_D_RELEASE_SHA is not a full 40-character commit SHA, so the "
            "policy cannot be bound to the accepted release — failing closed")
        return problems

    head = _git("rev-parse", "HEAD")
    if head is None:
        problems.append(
            "this checkout's HEAD commit could not be read (no git metadata?), "
            "so the policy cannot be proven to come from the accepted release "
            "— failing closed")
        return problems
    if head.lower() != sha:
        problems.append(
            f"this checkout is at {head[:12]}… but the accepted release is "
            f"{sha[:12]}… — the policy printed here is not the policy the "
            "accepted images enforce; check out the release before running "
            "Stage D, or re-authorize against a new release")
    dirty = _git("status", "--porcelain", "--", *POLICY_SOURCE_PATHS)
    if dirty is None:
        problems.append(
            "the policy source's working-tree state could not be read — failing closed")
    elif dirty:
        problems.append(
            "the policy source is modified in this working tree "
            f"({', '.join(sorted(line[2:].strip() for line in dirty.splitlines()))}) — "
            "a Stage D envelope may only be generated from committed, reviewed code")
    return problems


def _refuse(problems: list[str]) -> int:
    print("STAGE D REFUSED: the runtime policy is not bound to the accepted release:",
          file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    what = argv[1]

    if what == "binding":
        problems = release_binding_problems(os.environ.get("STAGE_D_RELEASE_SHA"))
        if problems:
            return _refuse(problems)
        print(f"OK: policy {POLICY.fingerprint()[:12]}… is the reviewed policy and "
              f"this checkout is the accepted release")
        return 0

    # Every other selector PRINTS an envelope value, so it must at least be
    # the reviewed policy. The release-SHA half of the binding is checked by
    # `binding` (which stage-d-env.sh runs) and by verify_caps.py, because
    # only those two know they are gating a real run.
    problems = fingerprint_problems()
    if problems:
        return _refuse(problems)

    if what in GROUPS:
        print(rendered(what))
        return 0
    if what == "execution-increment":
        print(authorized_execution_increment())
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
