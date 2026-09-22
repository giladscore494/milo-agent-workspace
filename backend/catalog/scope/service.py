"""The WorkScope operations the API exposes, in the order they must happen.

Every operation authorizes FIRST -- the same non-disclosing membership check
every other conversation and project route performs -- and only then reads,
interprets or writes. Nothing here launches, prepares or executes: a plan is a
draft, and the capability read below says so to the browser in as many words.

One path for chat and clicks
----------------------------

`create_work_scope` and `revise_work_scope` take EITHER an instruction OR an
edit, never both, and reduce either to the four fields `contract.
scope_from_fields` validates. From there the two inputs share every line: one
validator, one canonical text, one digest, one repository write. That is what
makes "the chat scope" and "the UI scope" the same object rather than two that
agree.

A stale plan fails closed
-------------------------

A revision names the head it was made against -- revision number AND digest --
and is refused (409 `WORK_SCOPE_STALE`) unless that is still exactly the head.
The check runs here, so a stale request is refused before anything is
interpreted, and again inside the database under a row lock, so two revisions
racing for one head cannot both land.
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from backend.errors import AppError, NotFoundError
from backend.execution_guard import is_stage_enabled
from backend.redaction import redact_secret_text

from . import contract as wsc
from . import coverage as wcov
from . import directory as mdir
from . import interpret as wi

#: The server flag behind every WorkScope WRITE. Default off, pinned off in
#: every deployment contract, and enforced by `ExecutionSurfaceGuardMiddleware`
#: before a request body is even read.
WORK_SCOPE_MUTATIONS_FLAG = "MILO_ENABLE_WORK_SCOPE_MUTATIONS"

#: The engines a plan can feed. Only Swarm V2 reads the Government catalog;
#: a Vehicle Catalog V1 project maps the one scope its configuration states.
WORK_SCOPE_WORKFLOWS = frozenset({"swarm_v2"})

#: How many earlier revisions a read returns beside the head. The history is
#: for reading back what was asked; it is bounded like every other list here.
MAX_HISTORY = 10

#: The longest instruction text a response carries back.
MAX_INSTRUCTION_ECHO_CHARS = wi.MAX_INSTRUCTION_CHARS

REQUEST_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_REQUEST_INVALID": "state exactly one of an instruction or an edit",
    "WORK_SCOPE_WORKFLOW_UNSUPPORTED": "this project's engine does not read a mapping plan",
    "WORK_SCOPE_STALE": "the plan changed since it was read; reload it and try again",
    "WORK_SCOPE_COVERAGE_UNAVAILABLE":
        "the catalog coverage this instruction depends on could not be read",
    "WORK_SCOPE_UNREADABLE": "the stored plan could not be read",
    "WORK_SCOPE_UNAVAILABLE": "the mapping plan could not be read or written",
}


def _refusal(code: str, status: int) -> AppError:
    return AppError(code, REQUEST_REASONS[code], status)


def _not_found() -> AppError:
    return NotFoundError("work_scope", "requested")


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------

def capabilities(repo: Any, user_id: UUID, project_id: UUID) -> dict[str, Any]:
    """What the Mapping Plan may do for this project, from server-owned truth.

    `available` is the one answer the browser needs to decide whether to show
    the surface at all. The limits are the contract's own constants, so a
    browser that renders a batch-size choice renders exactly the server's
    bound. `can_prepare` and `can_start_batches` are false in this release by
    construction: there is no preparation and no batch path to enable.
    """
    project = repo.get_project(project_id, user_id)
    supported = project.get("workflow_key") in WORK_SCOPE_WORKFLOWS
    mutations = is_stage_enabled(WORK_SCOPE_MUTATIONS_FLAG)
    reason = None if supported and mutations else (
        "workflow_not_supported" if not supported else "mutations_disabled")
    return {
        "available": supported and mutations,
        "reason": reason,
        "contract": wsc.WORK_SCOPE_CONTRACT,
        "directory_version": mdir.DIRECTORY_VERSION,
        "limits": {
            "max_units": wsc.MAX_UNITS,
            "max_items": wsc.MAX_WORK_SCOPE_ITEMS,
            "default_max_items": wsc.DEFAULT_MAX_ITEMS,
            "max_batch_size": wsc.MAX_BATCH_SIZE,
            "default_batch_size": wsc.DEFAULT_BATCH_SIZE,
            "min_model_year": wsc.MIN_MODEL_YEAR,
            "max_model_year": wsc.MAX_MODEL_YEAR,
            "max_instruction_chars": wi.MAX_INSTRUCTION_CHARS,
        },
        "can_prepare": False,
        "can_start_batches": False,
    }


def directory(repo: Any, user_id: UUID, project_id: UUID) -> dict[str, Any]:
    """The reviewed directory, with each entry's canonical coverage.

    A coverage read that fails still returns the directory, with every count
    `unavailable` -- a plan can be made without counts, and a missing count is
    never shown as zero.
    """
    repo.get_project(project_id, user_id)
    coverage = wcov.read_coverage(repo)
    return {
        "directory_version": mdir.DIRECTORY_VERSION,
        "origins": [{"key": key, "label": label} for key, label in mdir.ORIGIN_LABELS.items()],
        "entries": [{
            "key": entry.key,
            "name": entry.name,
            "name_he": entry.name_he,
            "origin": entry.origin,
            "register_marque": entry.register_marque,
            "register_marque_verified": entry.register_marque_verified,
            "coverage": {"state": coverage.state(entry.key),
                         "canonical_variants": coverage.count(entry.key)},
        } for entry in mdir.DIRECTORY],
        "coverage": {"available": coverage.available,
                     "catalog_variants": coverage.catalog_variants,
                     "attributed_variants": coverage.attributed_variants},
    }


def open_work_scope(repo: Any, user_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    """The conversation's open plan, or `{"work_scope": null}` when it has none."""
    repo.get_conversation(conversation_id, user_id)
    row = _repository(lambda: repo.open_work_scope(conversation_id))
    return {"work_scope": None if row is None else _state(repo, row)}


def work_scope(repo: Any, user_id: UUID, work_scope_id: UUID) -> dict[str, Any]:
    """One plan, for a member of its conversation's project. 404 otherwise."""
    return _state(repo, _authorized(repo, user_id, work_scope_id))


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------

def create_work_scope(repo: Any, user_id: UUID, conversation_id: UUID,
                      instruction: str | None, edit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Revision 1 of a new plan in this conversation."""
    conversation = repo.get_conversation(conversation_id, user_id)
    _require_supported(repo.get_project(UUID(str(conversation["project_id"]))))
    scope, notes, kind = _resolve(repo, instruction, edit, current=None)
    result = _repository(lambda: repo.create_work_scope(
        conversation_id, user_id, _revision_payload(scope, kind, instruction, notes)))
    return {"applied": True, "notes": [note.as_record() for note in notes],
            "work_scope": _state(repo, result["work_scope"])}


def revise_work_scope(repo: Any, user_id: UUID, work_scope_id: UUID, expected_revision: int,
                      expected_digest: str, instruction: str | None,
                      edit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Revision n+1, against exactly the head the caller names."""
    row = _authorized(repo, user_id, work_scope_id)
    _require_supported(repo.get_project(UUID(str(row["project_id"]))))
    if row.get("head_revision") != expected_revision or row.get("head_digest") != expected_digest:
        raise _refusal("WORK_SCOPE_STALE", 409)
    head = _head_revision(repo, row)
    current = _stored_scope(head)
    scope, notes, kind = _resolve(repo, instruction, edit, current=current)
    if scope.canonical_text() == head["scope_text"]:
        # Understood, and identical: nothing to write. The caller learns that
        # rather than seeing a revision that changes nothing.
        notes = (*notes, wi.Note("WORK_SCOPE_NOTE_NO_CHANGE"))
        return {"applied": False, "notes": [note.as_record() for note in notes],
                "work_scope": _state(repo, row)}
    result = _repository(lambda: repo.revise_work_scope(
        work_scope_id, expected_revision, expected_digest, user_id,
        _revision_payload(scope, kind, instruction, notes)))
    return {"applied": True, "notes": [note.as_record() for note in notes],
            "work_scope": _state(repo, result["work_scope"])}


def _resolve(repo: Any, instruction: str | None, edit: Mapping[str, Any] | None,
             current: wsc.WorkScope | None) -> tuple[wsc.WorkScope, tuple[wi.Note, ...], str]:
    """Either input, reduced to ONE validated scope. The shared path."""
    if (instruction is None) == (edit is None):
        raise _refusal("WORK_SCOPE_REQUEST_INVALID", 422)
    try:
        if edit is not None:
            return wsc.scope_from_fields(edit), (), "edit"
        reading = wi.read_instruction(instruction)
        coverage = None
        if reading.needs_coverage:
            read = wcov.read_coverage(repo)
            if not read.available:
                raise _refusal("WORK_SCOPE_COVERAGE_UNAVAILABLE", 503)
            coverage = wcov.interpretation_coverage(read)
        interpretation = wi.apply_reading(reading, current, coverage)
        return wsc.scope_from_fields(interpretation.fields), interpretation.notes, "instruction"
    except (wsc.WorkScopeError, wi.InstructionError) as exc:
        raise AppError(exc.code, exc.safe_message, 422) from None


def _revision_payload(scope: wsc.WorkScope, kind: str, instruction: str | None,
                      notes: tuple[wi.Note, ...]) -> dict[str, Any]:
    return {"scope_text": scope.canonical_text(), "input_kind": kind,
            "instruction": instruction if kind == "instruction" else None,
            "notes": [note.as_record() for note in notes]}


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _require_supported(project: Mapping[str, Any]) -> None:
    if project.get("workflow_key") not in WORK_SCOPE_WORKFLOWS:
        raise _refusal("WORK_SCOPE_WORKFLOW_UNSUPPORTED", 409)


def _repository(call: Any) -> Any:
    """A repository call whose failure surfaces only as a static code.

    The refusals the repository itself classifies (stale, not found, an open
    plan already exists, not editable) pass through unchanged; any other
    repository failure -- whose message can quote SQL -- becomes one
    sanitized code.
    """
    try:
        return call()
    except AppError as exc:
        if exc.code.startswith("WORK_SCOPE_") or exc.code.endswith("_NOT_FOUND"):
            raise
        raise _refusal("WORK_SCOPE_UNAVAILABLE", 502) from None


def _authorized(repo: Any, user_id: UUID, work_scope_id: UUID) -> dict[str, Any]:
    """The plan, if the caller is a member of its project. One 404 otherwise.

    Absent and not-a-member are the same answer, exactly like every other
    membership-scoped read: the conversation lookup raises the ordinary
    non-disclosing 404, and it is re-raised as the plan's own.
    """
    row = _repository(lambda: repo.get_work_scope(work_scope_id))
    if row is None:
        raise _not_found()
    try:
        repo.get_conversation(UUID(str(row["conversation_id"])), user_id)
    except NotFoundError:
        raise _not_found() from None
    return row


def _head_revision(repo: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    revisions = _repository(lambda: repo.list_work_scope_revisions(row["id"], limit=1))
    if not revisions or revisions[0].get("revision") != row.get("head_revision") \
            or revisions[0].get("digest") != row.get("head_digest"):
        raise _refusal("WORK_SCOPE_UNREADABLE", 502)
    return revisions[0]


def _stored_scope(revision: Mapping[str, Any]) -> wsc.WorkScope:
    try:
        scope = wsc.scope_from_text(revision.get("scope_text"))
    except wsc.WorkScopeError:
        raise _refusal("WORK_SCOPE_UNREADABLE", 502) from None
    if scope.digest() != revision.get("digest"):
        raise _refusal("WORK_SCOPE_UNREADABLE", 502)
    return scope


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_secret_text(value)[:limit]


def _notes(value: Any) -> list[dict[str, Any]]:
    """Stored notes through the CLOSED note vocabulary; anything else dropped."""
    projected: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, Mapping) or item.get("code") not in wi.NOTE_CODES:
            continue
        note: dict[str, Any] = {"code": item["code"]}
        terms = [_text(term, wi.MAX_TERM_CHARS) for term in item.get("terms") or []
                 if isinstance(term, str)][:wi.MAX_REPORTED_TERMS]
        units = [key for key in item.get("units") or [] if mdir.entry_for(key) is not None]
        if terms:
            note["terms"] = [term for term in terms if term]
        if units:
            note["units"] = units[:wsc.MAX_UNITS]
        projected.append(note)
    return projected


def _revision_view(revision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision": revision.get("revision"),
        "digest": revision.get("digest"),
        "input_kind": revision.get("input_kind"),
        "instruction": _text(revision.get("instruction"), MAX_INSTRUCTION_ECHO_CHARS),
        "notes": _notes(revision.get("notes")),
        "created_at": _text(revision.get("created_at"), 64),
    }


def _state(repo: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """The browser-safe state of one plan: its head, its plan and its history.

    Built key by key. The plan is re-read from the stored canonical text
    through the strict parser, and its digest re-derived and compared, so a
    stored row that is not a valid plan is refused rather than rendered.
    """
    revisions = _repository(lambda: repo.list_work_scope_revisions(
        row["id"], limit=MAX_HISTORY + 1))
    if not revisions or revisions[0].get("revision") != row.get("head_revision") \
            or revisions[0].get("digest") != row.get("head_digest"):
        raise _refusal("WORK_SCOPE_UNREADABLE", 502)
    scope = _stored_scope(revisions[0])
    return {
        "work_scope_id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "project_id": str(row["project_id"]),
        "status": row.get("status"),
        "revision": row.get("head_revision"),
        "digest": row.get("head_digest"),
        "current": scope.current,
        "plan": {"contract": wsc.WORK_SCOPE_CONTRACT,
                 "directory_version": scope.directory_version,
                 **scope.fields()},
        "head": _revision_view(revisions[0]),
        "history": [_revision_view(revision) for revision in revisions[1:]],
        "created_at": _text(row.get("created_at"), 64),
        "updated_at": _text(row.get("updated_at"), 64),
    }


__all__ = ["MAX_HISTORY", "REQUEST_REASONS", "WORK_SCOPE_MUTATIONS_FLAG",
           "WORK_SCOPE_WORKFLOWS", "capabilities", "create_work_scope", "directory",
           "open_work_scope", "revise_work_scope", "work_scope"]
