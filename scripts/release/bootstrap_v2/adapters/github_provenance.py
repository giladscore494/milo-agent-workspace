"""Artifact provenance verification (library for artifact consumers).

An artifact is never trusted from filename and numeric run ID alone, and
the verifier is never controlled solely by the candidate workflow code it
validates: every trusted expectation arrives from the caller's pinned
configuration, not from the artifact or the run under inspection.

The bootstrap engine PRODUCES the metadata artifact; it does not consume
it. This module is the verifier that any downstream workflow (for example
the deploy pipeline) must invoke before trusting a bootstrap metadata
artifact. Until that pipeline is wired, the checks here are exercised by
tests/test_bootstrap_v2_adapters.py and no executable path skips them —
because no executable path consumes artifacts yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Finding, Severity, Stage

MAX_ARTIFACT_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TrustedExpectations:
    """Pinned, independent expectations for a valid bootstrap artifact."""

    repository: str
    workflow_path: str
    workflow_ref: str
    release_ref: str
    head_sha: str
    plan_digest: str
    artifact_name: str


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """Facts read from the GitHub API about the candidate run/artifact."""

    repository: str
    workflow_path: str
    workflow_ref: str
    run_ref: str
    head_sha: str
    event: str
    conclusion: str
    is_fork: bool
    mode_input: str
    plan_digest_output: str
    artifact_names: tuple[str, ...]
    artifact_expired: bool
    artifact_size: int


@dataclass(frozen=True, slots=True)
class ExtractionEntry:
    name: str
    is_regular_file: bool
    is_symlink: bool


def _blocked(code: str, message: str) -> Finding:
    return Finding(
        code=code,
        severity=Severity.BLOCKED,
        message=message,
        stage=Stage.APPLY_AUTHORIZED,
    )


def verify_provenance(
    expected: TrustedExpectations, observed: ObservedRun
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    def check(code: str, condition: bool, message: str) -> None:
        if not condition:
            findings.append(_blocked(code, message))

    check(
        "PROVENANCE_WRONG_REPOSITORY",
        observed.repository == expected.repository,
        f"artifact repository {observed.repository!r} != {expected.repository!r}",
    )
    check(
        "PROVENANCE_FORK_SOURCE",
        not observed.is_fork,
        "artifact originates from a fork",
    )
    check(
        "PROVENANCE_WRONG_WORKFLOW",
        observed.workflow_path == expected.workflow_path,
        f"workflow identity {observed.workflow_path!r} is not the trusted workflow",
    )
    check(
        "PROVENANCE_WRONG_WORKFLOW_REF",
        observed.workflow_ref == expected.workflow_ref,
        f"workflow code ref {observed.workflow_ref!r} is not the trusted ref",
    )
    check(
        "PROVENANCE_WRONG_RELEASE_REF",
        observed.run_ref == expected.release_ref,
        f"run ref {observed.run_ref!r} is not the trusted release ref",
    )
    check(
        "PROVENANCE_WRONG_MODE",
        observed.mode_input == "apply",
        f"run mode {observed.mode_input!r} is not apply",
    )
    check(
        "PROVENANCE_NOT_SUCCESSFUL",
        observed.conclusion == "success",
        f"run conclusion {observed.conclusion!r} is not success",
    )
    check(
        "PROVENANCE_WRONG_HEAD_SHA",
        observed.head_sha == expected.head_sha,
        f"run head sha {observed.head_sha!r} != expected {expected.head_sha!r}",
    )
    check(
        "PROVENANCE_WRONG_PLAN_DIGEST",
        observed.plan_digest_output == expected.plan_digest,
        "run plan digest does not equal the approved plan digest",
    )
    matching = tuple(
        name for name in observed.artifact_names if name == expected.artifact_name
    )
    check(
        "PROVENANCE_ARTIFACT_NOT_UNIQUE",
        len(matching) == 1,
        f"expected exactly one artifact named {expected.artifact_name!r}, "
        f"found {len(matching)}",
    )
    check(
        "PROVENANCE_ARTIFACT_EXPIRED",
        not observed.artifact_expired,
        "artifact is expired",
    )
    check(
        "PROVENANCE_ARTIFACT_SIZE",
        0 < observed.artifact_size <= MAX_ARTIFACT_SIZE,
        f"artifact size {observed.artifact_size} outside expected bounds",
    )
    return tuple(findings)


def verify_extraction(entries: tuple[ExtractionEntry, ...]) -> tuple[Finding, ...]:
    """Safe extraction: exactly one regular metadata file, nothing else."""

    findings: list[Finding] = []
    for entry in entries:
        if entry.is_symlink:
            findings.append(
                _blocked(
                    "PROVENANCE_SYMLINK_IN_ARTIFACT",
                    f"artifact contains symlink {entry.name!r}",
                )
            )
        elif not entry.is_regular_file:
            findings.append(
                _blocked(
                    "PROVENANCE_NON_REGULAR_FILE",
                    f"artifact contains non-regular file {entry.name!r}",
                )
            )
        if entry.name.startswith("/") or ".." in entry.name.split("/"):
            findings.append(
                _blocked(
                    "PROVENANCE_UNSAFE_PATH",
                    f"artifact entry has unsafe path {entry.name!r}",
                )
            )
    regular = tuple(e for e in entries if e.is_regular_file and not e.is_symlink)
    if len(entries) != 1 or len(regular) != 1:
        findings.append(
            _blocked(
                "PROVENANCE_EXTRA_FILES",
                f"expected exactly one regular metadata file, found "
                f"{len(entries)} entries",
            )
        )
    return tuple(findings)
