#!/usr/bin/env python3
"""The ONE definition of how the capture job is executed to prepare a plan revision.

Why this exists
---------------

A Mapping Plan revision is prepared by executing the EXISTING capture Cloud Run
job (`scripts/catalog/government-production-capture.sh --ensure-job` defines
it: `python -m backend.catalog.operator_capture` on the release worker image)
with the scoped entrypoint arguments and ONE per-execution environment
override that turns the scoped-preparation switch on for that execution only.

Two callers execute it, and they must never disagree about what they send:

*   the operator script, `government-production-capture.sh
    --prepare-work-scope` (Cloud Shell), which runs this file as a script and
    passes its output to `gcloud run jobs execute --args=... --update-env-vars`;
*   the API's Prepare route (`backend/catalog/scope/prepare_trigger.py`), which
    sends the same values to the Cloud Run Admin API's `jobs.run` as
    `overrides.containerOverrides[0].args` and `.env`.

So the arguments and the override are built HERE and nowhere else.
`tests/test_work_scope_prepare.py` runs the script through a mock `gcloud` and
holds what it executed byte for byte to what the API would send.

Standard library only, and runnable as a plain file
(`python3 backend/capture_invocation.py ...`), so the operator script needs no
package import and no third-party dependency to use it. The pinned values are
restated from `backend/catalog/government/source.py` and
`backend/catalog/operator_capture.py` (which refuse any other value), and a
test holds them equal, exactly as it does for `deployment-contract.sh`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

#: The module the capture job's container runs (`python -m <module>`).
ENTRYPOINT_MODULE = "backend.catalog.operator_capture"

#: The pinned upstream identity and bounds (`backend/catalog/government/source.py`).
PACKAGE_ID = "degem-rechev-wltp"
RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
PAGE_LIMIT = 1000
MAX_PAGES = 200
MAX_RECORDS = 120000

#: The two acknowledgements the entrypoint matches exactly.
EGRESS_ACKNOWLEDGEMENT = "I ACKNOWLEDGE LIVE GOVERNMENT EGRESS"
SCHEMA_REPORT_ACKNOWLEDGEMENT = "I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED"

#: The scoped-preparation switch, by name. The job definition pins it off; the
#: ONE override below turns it on for one execution.
PREPARATION_SWITCH = "MILO_ENABLE_WORK_SCOPE_PREPARATION"
PREPARATION_SWITCH_ON = "true"
#: PR-D1: the per-execution switch of the register modes (directory refresh
#: and group capture). Set ONLY by these invocations, like the one above.
REGISTER_SWITCH = "MILO_ENABLE_REGISTER_CAPTURE_JOB"
REGISTER_SWITCH_ON = "true"
#: PR-D3: the per-execution switch of the manufacturer normalisation mode --
#: the ONE model call's budget kill switch. Set ONLY by the invocation below.
NORMALISATION_SWITCH = "MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REVISION = re.compile(r"^[1-9][0-9]{0,8}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_REF = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class InvocationError(ValueError):
    """A value that is not one of the shapes the entrypoint accepts."""


def _checked(pattern: re.Pattern[str], value: object, name: str) -> str:
    text = str(value)
    if not pattern.fullmatch(text):
        raise InvocationError(f"{name} is not in the shape the capture entrypoint accepts")
    return text


@dataclass(frozen=True)
class Invocation:
    """One execution of the capture job: its container args and env overrides."""

    #: The entrypoint's OWN arguments, without `-m <module>`.
    entrypoint_args: tuple[str, ...]
    env_overrides: tuple[tuple[str, str], ...]

    @property
    def container_args(self) -> tuple[str, ...]:
        """The container's full args. An execution's args REPLACE the job's,
        so the module is always restated first."""
        return ("-m", ENTRYPOINT_MODULE, *self.entrypoint_args)

    def entrypoint_args_csv(self) -> str:
        """The entrypoint args as gcloud's comma-separated list.

        gcloud splits `--args` on commas, so a value that contains one would
        silently become two arguments; every value here is shape-checked
        comma-free, and this refuses one anyway."""
        if any("," in arg for arg in self.entrypoint_args):
            raise InvocationError("a capture argument contains a comma")
        return ",".join(self.entrypoint_args)

    def env_override_arg(self) -> str:
        """The overrides as gcloud's `--update-env-vars` value."""
        return ",".join(f"{name}={value}" for name, value in self.env_overrides)


def capture_arguments(*, project_ref: str, run_id: str) -> tuple[str, ...]:
    """The whole-resource capture's own arguments, for one prepared run."""
    return ("--execute",
            "--acknowledge-live-government-egress", EGRESS_ACKNOWLEDGEMENT,
            "--acknowledge-schema-report-reviewed", SCHEMA_REPORT_ACKNOWLEDGEMENT,
            "--project-ref", _checked(_PROJECT_REF, project_ref, "project ref"),
            "--run-id", _checked(_UUID, run_id, "run id"),
            "--package-id", PACKAGE_ID,
            "--resource-id", RESOURCE_ID,
            "--page-limit", str(PAGE_LIMIT),
            "--max-pages", str(MAX_PAGES),
            "--max-records", str(MAX_RECORDS))


def work_scope_preparation(*, project_ref: str, run_id: str, work_scope_id: str,
                           revision: int | str, digest: str) -> Invocation:
    """The ONE execution that prepares one exact plan revision."""
    return Invocation(
        entrypoint_args=(*capture_arguments(project_ref=project_ref, run_id=run_id),
                         "--work-scope-id", _checked(_UUID, work_scope_id, "work scope id"),
                         "--work-scope-revision", _checked(_REVISION, revision, "revision"),
                         "--work-scope-digest", _checked(_DIGEST, digest, "digest")),
        env_overrides=((PREPARATION_SWITCH, PREPARATION_SWITCH_ON),))


def register_capture(*, project_ref: str, run_id: str, group_id: str) -> Invocation:
    """PR-D1: the ONE execution that captures one register capture group."""
    return Invocation(
        entrypoint_args=(*capture_arguments(project_ref=project_ref, run_id=run_id),
                         "--register-group-id", _checked(_UUID, group_id, "register group id")),
        env_overrides=((REGISTER_SWITCH, REGISTER_SWITCH_ON),))


def register_directory(*, project_ref: str, run_id: str) -> Invocation:
    """PR-D1: the ONE execution that refreshes the register directory."""
    return Invocation(
        entrypoint_args=(*capture_arguments(project_ref=project_ref, run_id=run_id),
                         "--register-directory"),
        env_overrides=((REGISTER_SWITCH, REGISTER_SWITCH_ON),))


def manufacturer_normalisation(*, project_ref: str, run_id: str, proposal_id: str) -> Invocation:
    """PR-D3: the ONE execution that makes one proposal's guarded K3 call."""
    return Invocation(
        entrypoint_args=(*capture_arguments(project_ref=project_ref, run_id=run_id),
                         "--normalisation-proposal-id", _checked(_UUID, proposal_id, "proposal id")),
        env_overrides=((NORMALISATION_SWITCH, "true"),))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a capture job invocation.")
    parser.add_argument("what", choices=("capture-args", "work-scope-args", "work-scope-env"))
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--work-scope-id")
    parser.add_argument("--work-scope-revision")
    parser.add_argument("--work-scope-digest")
    args = parser.parse_args(argv)
    try:
        if args.what == "capture-args":
            invocation = Invocation(capture_arguments(project_ref=args.project_ref,
                                                      run_id=args.run_id), ())
            print(invocation.entrypoint_args_csv())
            return 0
        invocation = work_scope_preparation(
            project_ref=args.project_ref, run_id=args.run_id,
            work_scope_id=args.work_scope_id or "", revision=args.work_scope_revision or "",
            digest=args.work_scope_digest or "")
    except InvocationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(invocation.entrypoint_args_csv() if args.what == "work-scope-args"
          else invocation.env_override_arg())
    return 0


if __name__ == "__main__":
    sys.exit(main())
