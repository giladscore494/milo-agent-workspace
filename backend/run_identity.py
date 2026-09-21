"""The ONE immutable identity a run is created with and can never lose.

Why this module exists
----------------------

A run had no identity of its own. It had a row, and every surface that needed
to know WHAT the run was re-derived the answer, later, from whatever was to
hand:

* ``backend/worker/engine.py`` resolved the engine from the PROJECT's current
  ``workflow_key``, at worker start. A project switched from
  ``vehicle_catalog_v1`` to ``swarm_v2`` between a run's creation and its
  launch -- or between its first attempt and a retry -- changed what that run
  was. The same run row could be executed as V1 on attempt 1 and as V2 on
  attempt 2, resuming a V1 checkpoint into a V2 engine;
* ``backend/export_envelope.py`` read ``run["input"]["workflow_key"]`` first --
  the request METADATA, which the caller supplies -- then ``run["workflow_key"]``
  (a column that does not exist), and finally fell back to the literal
  ``"vehicle_catalog_v1"``. A V2 run whose input did not happen to name its
  workflow exported as V1, and a caller who put ``workflow_key`` in the request
  metadata could choose what an exported run claimed to be;
* the frontend selected V1 or V2 presentation from ``project.workflow_key``, so
  a historical run re-rendered as whatever the project is TODAY;
* nothing recorded which runtime policy or which release the run was admitted
  under, so "which envelope was this paid run authorized against?" had no
  durable answer at all.

Each of those is a different way of answering one question that should never
have been asked twice. This module answers it ONCE, at run creation, and every
later surface CONSUMES that answer.

The rule, stated once
---------------------

A run's identity is established BEFORE execution, from server-owned relations
only, and is immutable for the life of the run. Resume, worker retry,
checkpoint restore, export and finalization READ it. None of them may infer it
from current defaults, engine availability, checkpoint contents, output shape,
event history or caller input, and none of them may rewrite it. A run created
as V2 can never later look like V1, or the reverse.

Mutation fails closed in two places, deliberately: here, so application code
cannot express it, and in the database, where
``20260921000200_immutable_run_identity.sql`` refuses any UPDATE that changes a
non-null ``runs.run_identity``. The trigger is the one that actually holds --
application guards are advisory against a second writer -- and this module is
what stops the mistake being written in the first place.

What is bound
-------------

``run_id``
    The run this identity belongs to. Carried INSIDE the record so a record
    lifted out of its row cannot be read as some other run's.

``workflow_key``
    The engine allowlist key, resolved once from the trusted
    run -> conversation -> project relation. The V1/V2 discriminator.

``engine_version``
    The reviewed engine contract version for that workflow, from
    :data:`ENGINE_VERSIONS` -- the ONE place either engine's version is
    written down. (The zero-cost mock lifecycle engine substitutes for the V1
    workflow in staging and is refused in production; a staging run of
    ``vehicle_catalog_v1`` IS a ``vehicle_catalog_v1`` run, so it does not
    change the run's identity. Its own ``engine_version`` still travels on the
    checkpoint it writes, which is a different question: what this run is,
    versus which build wrote this checkpoint.)

``policy_version`` / ``policy_fingerprint``
    ``POLICY_SCHEMA_VERSION`` and the digest of the REVIEWED runtime-policy
    envelope this image declares. The reviewed envelope is a property of the
    runtime SOURCE, not of a deployment's environment, which is what lets Stage
    D bind it: ``scripts/release/stage-d/policy_envelope.py`` pins the same
    digest and proves it byte-identical to the policy at the accepted release.
    A run therefore records which reviewed envelope admitted it, and that
    record is comparable with the one the release gate verifies.

``release_sha``
    The runtime release the API that created the run was serving
    (``MILO_RELEASE_SHA``, set by the deployer alongside the image tag). Empty
    when the deployment does not pin one -- ``""`` means "this deployment
    stated no release", never "any release will do", and the release gate
    treats an unpinned run as unauthorized rather than as a match.

``event_registry_version`` / ``event_registry_fingerprint``
    The version and exact content digest of the canonical event vocabulary (``backend/event_registry.py``) the run's
    durable event stream speaks. An exported or resumed run states it rather
    than leaving a later reader to assume today's.

Import discipline: this module imports ``backend.event_registry`` and
``backend.runtime_policy``, both of which import nothing from ``backend`` at
module scope, so binding identity cannot create a cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from backend.event_registry import REGISTRY_VERSION, fingerprint as event_registry_fingerprint
from backend.runtime_policy import POLICY_SCHEMA_VERSION, reviewed_first_run_policy

#: The identity record's own schema version. A record that does not carry
#: exactly this is not one this release knows how to read, and reading it
#: anyway would be guessing at a run's identity -- the defect this module
#: exists to remove.
IDENTITY_VERSION = "milo-run-identity/1"

#: The column the record lives in, named once.
RUN_IDENTITY_FIELD = "run_identity"

#: The environment variable a deployment states its release in. Set by
#: ``scripts/deploy/cloud-run.sh`` to the same full commit SHA the images are
#: tagged with, so the run's identity and the image's tag name one release.
RELEASE_SHA_ENV = "MILO_RELEASE_SHA"

#: THE engine version registry: workflow key -> reviewed engine contract
#: version. Both engines import their own value from here, so this is the only
#: place either version is written down. Adding a workflow means adding it
#: here; a workflow with no declared version cannot be bound, which is what
#: stops an engine reaching production without a version identity.
ENGINE_VERSIONS: Mapping[str, str] = {
    "vehicle_catalog_v1": "vehicle_catalog_v1.stage3",
    "swarm_v2": "swarm_v2.1",
    # Control-plane run identity. It is intentionally NOT registered in the
    # model EngineRegistry, so an ordinary worker cannot execute it.
    "operator_capture": "operator_capture.1",
}

#: Workflows that produce a user product and may be selected for paid
#: execution/export. Control-plane identities are intentionally excluded.
PRODUCT_WORKFLOW_KEYS = frozenset({"vehicle_catalog_v1", "swarm_v2"})

#: Historical engine identities this release knows how to READ truthfully.
#:
#: This is deliberately distinct from ENGINE_VERSIONS. ENGINE_VERSIONS answers
#: "which version may NEW execution use now?"; this registry answers "is this
#: persisted historical identity a version we recognise?". Without that
#: separation, bumping an engine from e.g. swarm_v2.1 to swarm_v2.2 would make
#: every truthful v2.1 export suddenly unreadable. Execution/resume performs a
#: stricter current-version check in EngineResolver; history/export does not.
SUPPORTED_ENGINE_VERSIONS: Mapping[str, frozenset[str]] = {
    "vehicle_catalog_v1": frozenset({"vehicle_catalog_v1.stage3"}),
    "swarm_v2": frozenset({"swarm_v2.1"}),
    "operator_capture": frozenset({"operator_capture.1"}),
}

#: The record's fields, in one place, so a reader and a writer cannot disagree
#: about which keys make a complete identity.
IDENTITY_FIELDS: tuple[str, ...] = (
    "identity_version", "run_id", "workflow_key", "engine_version",
    "policy_version", "policy_fingerprint", "release_sha",
    "event_registry_version", "event_registry_fingerprint",
)


class RunIdentityError(ValueError):
    """A run identity that cannot be trusted, in either direction.

    Carries a STATIC code. Nothing derived from a payload is ever interpolated
    into the message: a refusal is not a place to launder unvalidated content
    into a log, an event or an export.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def reviewed_policy_fingerprint() -> str:
    """The digest of the reviewed runtime-policy envelope this image declares."""
    return reviewed_first_run_policy().fingerprint()


def release_sha(env: Mapping[str, str] | None = None) -> str:
    """The release this deployment states it is serving, or ``""``.

    Normalised to lower case and validated as a full 40-character commit SHA.
    Anything else -- a short SHA, a tag, a branch name, whitespace -- is
    treated as UNSTATED rather than recorded, because a release binding that
    can be satisfied by an arbitrary string is not a binding. An unstated
    release is a refusal at the gate, never a wildcard.
    """
    raw = (os.environ if env is None else env).get(RELEASE_SHA_ENV, "")
    value = str(raw or "").strip().lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


def engine_version_for(workflow_key: str) -> str:
    """The reviewed engine version of an allowlisted workflow, or refuse."""
    try:
        return ENGINE_VERSIONS[workflow_key]
    except KeyError as exc:
        raise RunIdentityError(
            "RUN_IDENTITY_WORKFLOW_UNKNOWN",
            "workflow has no declared engine version, so a run of it cannot be "
            "given an identity") from exc


@dataclass(frozen=True)
class RunIdentity:
    """ONE run's immutable identity. Frozen, because it is."""

    run_id: str
    workflow_key: str
    engine_version: str
    policy_version: str
    policy_fingerprint: str
    release_sha: str
    event_registry_version: str
    event_registry_fingerprint: str
    identity_version: str = IDENTITY_VERSION

    # -- construction ----------------------------------------------------
    @classmethod
    def bind(cls, run_id: Any, workflow_key: str, *,
             env: Mapping[str, str] | None = None) -> "RunIdentity":
        """Establish the identity of a run that is about to be created.

        Called ONCE, by the API, from the trusted project relation -- never
        from request metadata, and never by a worker. Every other dimension is
        a property of the running image, so an identity bound here is a
        statement about the code that admitted the run.
        """
        key = str(workflow_key or "").strip()
        if not key:
            raise RunIdentityError(
                "RUN_IDENTITY_WORKFLOW_MISSING",
                "a run identity requires the trusted workflow key")
        return cls(
            run_id=_run_id_text(run_id),
            workflow_key=key,
            engine_version=engine_version_for(key),
            policy_version=POLICY_SCHEMA_VERSION,
            policy_fingerprint=reviewed_policy_fingerprint(),
            release_sha=release_sha(env),
            event_registry_version=REGISTRY_VERSION,
            event_registry_fingerprint=event_registry_fingerprint(),
        )

    @classmethod
    def from_record(cls, record: Any, *, run_id: Any = None) -> "RunIdentity":
        """Read a PERSISTED identity, or refuse.

        Total and fail-closed: a missing record, a record that is not an
        object, an unknown schema version, an absent or blank field, a
        workflow that is not allowlisted, an engine version that is not the
        reviewed one for that workflow, and a record whose ``run_id`` is not
        the run being read are each a refusal. None of them is repaired,
        defaulted or guessed at -- guessing is precisely what this module
        exists to stop.
        """
        if record is None:
            raise RunIdentityError(
                "RUN_IDENTITY_ABSENT",
                "the run carries no persisted identity")
        if not isinstance(record, Mapping):
            raise RunIdentityError(
                "RUN_IDENTITY_MALFORMED",
                "a run identity must be an object")
        if record.get("identity_version") != IDENTITY_VERSION:
            raise RunIdentityError(
                "RUN_IDENTITY_VERSION_UNKNOWN",
                "the run identity is not a schema version this release can read")
        values: dict[str, str] = {}
        for field in IDENTITY_FIELDS:
            if field == "release_sha":
                # The only field that may legitimately be empty: a deployment
                # that pinned no release. It is recorded as stated.
                values[field] = str(record.get(field) or "")
                continue
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RunIdentityError(
                    "RUN_IDENTITY_INCOMPLETE",
                    "the run identity is missing a required dimension")
            values[field] = value.strip()
        identity = cls(**values)  # type: ignore[arg-type]
        if len(identity.policy_fingerprint) != 64 or any(
                char not in "0123456789abcdef" for char in identity.policy_fingerprint.lower()):
            raise RunIdentityError(
                "RUN_IDENTITY_POLICY_FINGERPRINT_INVALID",
                "the run identity carries an invalid policy fingerprint")
        if len(identity.event_registry_fingerprint) != 64 or any(
                char not in "0123456789abcdef"
                for char in identity.event_registry_fingerprint.lower()):
            raise RunIdentityError(
                "RUN_IDENTITY_EVENT_REGISTRY_FINGERPRINT_INVALID",
                "the run identity carries an invalid event-registry fingerprint")
        if identity.release_sha and (
                len(identity.release_sha) != 40
                or any(char not in "0123456789abcdef" for char in identity.release_sha.lower())):
            raise RunIdentityError(
                "RUN_IDENTITY_RELEASE_INVALID",
                "the run identity carries an invalid release SHA")
        if identity.workflow_key not in SUPPORTED_ENGINE_VERSIONS:
            raise RunIdentityError(
                "RUN_IDENTITY_WORKFLOW_UNKNOWN",
                "the run identity names a workflow this release does not know")
        if identity.engine_version not in SUPPORTED_ENGINE_VERSIONS[identity.workflow_key]:
            raise RunIdentityError(
                "RUN_IDENTITY_ENGINE_UNKNOWN",
                "the run identity names an engine version this release cannot read truthfully")
        if run_id is not None and identity.run_id != _run_id_text(run_id):
            raise RunIdentityError(
                "RUN_IDENTITY_RUN_MISMATCH",
                "the persisted identity belongs to a different run")
        return identity

    # -- projection ------------------------------------------------------
    def as_record(self) -> dict[str, str]:
        """The durable record. Exactly :data:`IDENTITY_FIELDS`, nothing else."""
        return {field: getattr(self, field) for field in IDENTITY_FIELDS}

    @property
    def is_swarm_v2(self) -> bool:
        return self.workflow_key == "swarm_v2"

    def differences(self, other: "RunIdentity") -> tuple[str, ...]:
        """Which dimensions two identities disagree on. Empty means identical."""
        return tuple(field for field in IDENTITY_FIELDS
                     if getattr(self, field) != getattr(other, field))


def _run_id_text(run_id: Any) -> str:
    """The canonical text form of a run id, or refuse.

    A run id is a UUID everywhere in this repository. Normalising through
    :class:`UUID` means two spellings of one id cannot look like two runs, and
    a value that is not an id at all is refused rather than recorded.
    """
    if isinstance(run_id, UUID):
        return str(run_id)
    try:
        return str(UUID(str(run_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RunIdentityError(
            "RUN_IDENTITY_RUN_INVALID",
            "a run identity requires the run's own id") from exc


def persisted_identity(run: Any) -> RunIdentity | None:
    """The identity a run row carries, or ``None`` when it carries NONE.

    The ``None`` case is exactly one thing: a run created before this release,
    whose row predates the ``run_identity`` column. It is NOT a fallback for a
    record that is present and wrong -- that raises, like any other untrusted
    identity. Callers decide what an unpinned legacy run may do; they are never
    handed a guess.
    """
    if not isinstance(run, Mapping):
        raise RunIdentityError("RUN_IDENTITY_MALFORMED", "a run must be an object")
    record = run.get(RUN_IDENTITY_FIELD)
    if record is None:
        return None
    return RunIdentity.from_record(record, run_id=run.get("id"))


def require_identity(run: Any) -> RunIdentity:
    """The identity a run row carries. An absent one is a refusal.

    The strict reader, for every surface that must never guess: export, and
    any gate that authorizes a run against a release.
    """
    identity = persisted_identity(run)
    if identity is None:
        raise RunIdentityError(
            "RUN_IDENTITY_ABSENT",
            "the run carries no persisted identity")
    return identity


def execution_identity_problems(
    identity: RunIdentity,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Why this runtime may NOT execute/resume a persisted run identity.

    Historical read/export compatibility is intentionally broader. Execution
    must match the exact engine contract, reviewed policy, event vocabulary and
    release that the run recorded when it was created; otherwise a retry after
    a deploy would silently execute a different runtime while keeping the old
    identity.
    """
    problems: list[str] = []
    current_engine = ENGINE_VERSIONS.get(identity.workflow_key)
    if current_engine != identity.engine_version:
        problems.append("engine_version")
    if identity.policy_version != POLICY_SCHEMA_VERSION:
        problems.append("policy_version")
    if identity.policy_fingerprint != reviewed_policy_fingerprint():
        problems.append("policy_fingerprint")
    if identity.event_registry_version != REGISTRY_VERSION:
        problems.append("event_registry_version")
    if identity.event_registry_fingerprint != event_registry_fingerprint():
        problems.append("event_registry_fingerprint")
    current_release = release_sha(env)
    # An unstated release is never a wildcard. A run and a worker that both
    # forgot MILO_RELEASE_SHA must still refuse execution rather than treating
    # two empty strings as a valid release binding.
    if not identity.release_sha or not current_release or identity.release_sha != current_release:
        problems.append("release_sha")
    return tuple(problems)


def identity_mutation_problems(current: Any, proposed: Any) -> list[str]:
    """Why a proposed identity may not replace `current`. Empty means it may.

    Replacing an identity is only ever legitimate when nothing changes: an
    idempotent re-bind of the record already in force. Anything else is a
    rewrite of what the run IS, which fails closed here and again in the
    database.
    """
    if current is None:
        return []
    try:
        held = RunIdentity.from_record(current)
    except RunIdentityError:
        # An unreadable identity already in the row is not a licence to
        # overwrite it: it is a state a human has to look at.
        return ["the run already carries an identity this release cannot read, "
                "so it cannot be shown to be unchanged"]
    try:
        wanted = RunIdentity.from_record(proposed)
    except RunIdentityError as exc:
        return [f"the proposed identity is not a valid one ({exc.code})"]
    changed = held.differences(wanted)
    if not changed:
        return []
    return ["a run's identity is immutable; this would change "
            + ", ".join(changed)]


__all__ = [
    "ENGINE_VERSIONS", "PRODUCT_WORKFLOW_KEYS", "SUPPORTED_ENGINE_VERSIONS", "IDENTITY_FIELDS", "IDENTITY_VERSION",
    "RELEASE_SHA_ENV", "RUN_IDENTITY_FIELD", "RunIdentity", "RunIdentityError",
    "engine_version_for", "execution_identity_problems", "identity_mutation_problems", "persisted_identity",
    "release_sha", "require_identity", "reviewed_policy_fingerprint",
]
