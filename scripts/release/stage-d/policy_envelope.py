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
2. ``release_binding_problems`` proves the policy SOURCE this script is using
   is byte-for-byte the policy source AT ``STAGE_D_RELEASE_SHA``.

What is deliberately NOT required
---------------------------------

The toolkit checkout does **not** have to BE the release commit. An earlier
revision required ``HEAD == STAGE_D_RELEASE_SHA``, which made
re-authorization impossible: the reviewed commit that updates
``STAGE_D_RELEASE_SHA`` to a new release R cannot itself be R, so no
authorization commit could ever satisfy its own pin.

What actually has to hold is that the ENVELOPE this script prints is the one
the accepted images enforce, and that is a statement about the policy
CONTENT, not about which commit the operator has checked out. So a later
reviewed authorization or runbook commit may reference release R freely, as
long as it does not change ``backend/runtime_policy.py``. If it does, the
policy those images enforce and the policy this script would print are no
longer the same thing, and that requires a new release -- which is exactly
what the refusal says.

The chain, end to end:

    this script's policy  ==(bytes)==  policy at R
    R  ==(verify_images.py)==  accepted image digests
    accepted digests  ==(verify_caps.py)==  the images the run will execute

and, transitively, because the checkout's policy digest must also equal
``PINNED_POLICY_FINGERPRINT``, the reviewed pin is provably a statement about
R's policy too -- identical bytes cannot produce a different document.

Being unable to PROVE the binding is a refusal, never a pass: no git, a
shallow clone that lacks the commit, a release that does not contain the
policy source at all, or an unreadable file all fail closed.

Usage:
  policy_envelope.py caps                 # STAGE_D_CAPS
  policy_envelope.py provider-limits      # STAGE_D_WORKER_PROVIDER_LIMITS
  policy_envelope.py engine-limits        # STAGE_D_WORKER_ENGINE_LIMITS
  policy_envelope.py execution-increment  # STAGE_D_AUTHORIZED_EXECUTION_INCREMENT
  policy_envelope.py fingerprint          # the reviewed policy digest
  policy_envelope.py document             # the whole canonical document as JSON
  policy_envelope.py binding              # prove the policy IS the release's

Every selector except ``binding`` refuses unless the checkout's policy matches
the pinned fingerprint. ``binding`` additionally proves it is identical to the
policy at ``STAGE_D_RELEASE_SHA``.

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
PINNED_POLICY_FINGERPRINT = "7ffc0f6d36220ecdd773955ef4e89289d804fa86e2e279ddd93a6bd6c37ed52b"

#: The ONE source whose content determines the policy document. It imports
#: nothing from `backend` at module scope and every reviewed value, env key,
#: direction and mandatory rule lives in it, so identical bytes here mean an
#: identical policy document -- which is what makes the byte comparison below
#: a proof about the ENVELOPE and not merely about a file.
POLICY_SOURCE_PATH = "backend/runtime_policy.py"

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


def _git(*args: str, repo_root: Path | None = None,
         binary: bool = False) -> str | bytes | None:
    """Read-only git metadata, or None when it cannot be proven.

    Never raises: an absent binary, a broken repository and a non-zero exit
    are all "cannot prove", which every caller turns into a refusal.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    try:
        result = subprocess.run(("git", "-C", str(root), *args),
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()


def _imported_policy_source() -> Path | None:
    """The file the policy this process is using was actually imported from."""
    from backend import runtime_policy

    origin = getattr(runtime_policy, "__file__", None)
    return Path(origin).resolve() if origin else None


def release_binding_problems(release_sha: str | None, *,
                             repo_root: Path | str | None = None) -> list[str]:
    """Prove the policy in use IS the policy at the accepted release.

    The proof is a byte comparison against the blob at ``release_sha``, NOT a
    check on which commit is checked out: a reviewed authorization commit must
    be able to reference release R without being R. See the module docstring.

    Every failure mode is a refusal, including "cannot tell": a Stage D run
    that cannot prove which code produced its envelope has verified nothing,
    whatever the individual checks printed.
    """
    sandboxed = repo_root is not None
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    problems = fingerprint_problems()

    sha = (release_sha or "").strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        problems.append(
            "STAGE_D_RELEASE_SHA is not a full 40-character commit SHA, so the "
            "policy cannot be bound to the accepted release — failing closed")
        return problems

    if _git("rev-parse", "--verify", f"{sha}^{{commit}}", repo_root=root) is None:
        problems.append(
            f"the accepted release {sha[:12]}… is not a commit this checkout can "
            "read (no git metadata, or a shallow clone that does not contain it), "
            "so the policy cannot be bound to it — failing closed")
        return problems

    released = _git("show", f"{sha}:{POLICY_SOURCE_PATH}", repo_root=root, binary=True)
    if released is None:
        problems.append(
            f"the accepted release {sha[:12]}… does not contain {POLICY_SOURCE_PATH}, "
            "so there is no released policy for this envelope to be identical to "
            "— that release predates the canonical runtime policy and Stage D must "
            "be re-authorized against a release that carries it")
        return problems

    # What Stage D is REALLY using. In production that is the file the policy
    # module was imported from, so the bytes compared are the bytes executed.
    if sandboxed:
        local_path: Path | None = root / POLICY_SOURCE_PATH
    else:
        local_path = _imported_policy_source()
        if local_path is None or local_path != (REPO_ROOT / POLICY_SOURCE_PATH).resolve():
            problems.append(
                "the runtime policy was imported from outside this checkout, so "
                "the bytes compared would not be the bytes in use — failing closed")
            return problems
    try:
        local = local_path.read_bytes()
    except OSError:
        problems.append(
            f"{POLICY_SOURCE_PATH} could not be read, so the policy in use cannot "
            "be compared with the accepted release — failing closed")
        return problems

    if local != released:
        problems.append(
            f"{POLICY_SOURCE_PATH} is not byte-for-byte the policy at the accepted "
            f"release {sha[:12]}… — the envelope this toolkit would apply is not "
            "the one those images enforce; changing the policy requires a new "
            "reviewed release, not a new authorization commit")
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
              f"is byte-identical to {POLICY_SOURCE_PATH} at the accepted release")
        return 0

    # Every other selector PRINTS an envelope value, so it must at least be
    # the reviewed policy. The release half of the binding is checked by
    # `binding` -- which every path that mutates the deployment or creates the
    # run executes first -- and by verify_caps.py, because only those know
    # they are gating a real run.
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
