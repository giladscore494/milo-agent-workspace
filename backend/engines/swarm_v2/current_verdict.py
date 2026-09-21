"""R5: the ONE deterministic answer to "is this claim verified RIGHT NOW".

Why this module exists
----------------------

`public.claim_verdicts` is append-only, and that is correct: a verdict is an
audit record, and rewriting one would destroy the history it exists for. But
append-only history is not the same thing as CURRENT TRUTH, and every reader
that asked "does a verified verdict exist for this claim" was quietly
conflating the two:

    t0  verdict: verified          (the source said 1798 cc)
    t1  verdict: rejected          (a re-verification found 1600 cc)

    exists(verdict = 'verified')  ->  true, forever.

So an invalidation, a contradiction, a supersession or a verdict that cites no
durable evidence at all could be bypassed by any older `verified` row that
happened to remain in the table. A stale `verified` could authorize a
promotion; a claim whose conflict was decided against it still read as
verified; a `verified` row whose support links were never written read exactly
like one whose evidence is durable.

This module is the single deterministic resolution every reader now uses. It
is PURE -- no database handle, no provider call, no global state -- so the
same rule can be applied to rows read through the Supabase repository, to rows
held by `MemoryRepository`, and to rows a V1 evidence pass has just written.

`supabase/migrations/20260921000100_current_verdict_authority.sql` implements
exactly this rule in SQL, so the database refuses what this module refuses
even for a writer that never came through this code. The two definitions are
pinned together by tests/test_current_verdict_authority.py and
tests/test_migrations_postgres.py.

The rule, stated once
---------------------

For ONE claim, in this order:

1.  the claim row must still be ACTIVE -- a deactivated claim is
    ``invalidated`` and nothing it ever carried is current;
2.  a conflict RESOLUTION that names it among the superseded claims makes it
    ``superseded`` -- the decision is durable, the losing claim keeps its row,
    and its old verdict stops being current truth;
3.  an UNRESOLVED conflict over the claim makes it ``contested`` -- two
    sources disagree and nobody has decided, so nothing is current;
4.  otherwise the CURRENT verdict is the claim's LATEST verdict row, ordered
    by ``created_at`` descending, then -- for rows written in the same instant
    -- a non-``verified`` verdict ahead of a ``verified`` one, then the
    greatest id. The middle term is the fail-closed tiebreak: when two
    verdicts are indistinguishable in time, the one that does NOT assert
    verification wins;
5.  no verdict row at all is ``unverified``;
6.  a current ``rejected`` / ``needs_review`` verdict is exactly that;
7.  a current ``verified`` verdict that cites no durable support, or that
    states no verification mode and contract version, is ``unsupported`` --
    an assertion, not evidence;
8.  and only then is the claim ``supported``.

``supported`` is the ONLY state that means "verified now". Every other state
is a refusal, and a refusal names which property failed.

Idempotent replay cannot create a contradiction here: the durable write path
derives a verdict's identity from its own content, so replaying a verdict
lands on the SAME row rather than appending a second one, and a set of rows
that differ only by replay resolves to the same current verdict whatever order
they are read in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: The bounded identifier of THIS resolution contract. It is recorded next to
#: a resolved state that travels anywhere, so a consumer can never present a
#: state resolved under an older rule as one resolved under this one.
CURRENT_VERDICT_CONTRACT_VERSION = "r5.current_verdict.1"

#: The ONE state that authorizes anything: verified now, on durable evidence.
SUPPORTED_STATE = "supported"

#: Every state this resolution can produce. Closed and server-owned.
CURRENT_VERDICT_STATES = ("supported", "unsupported", "unverified", "needs_review",
                          "rejected", "contested", "superseded", "invalidated")

#: The safe, static message of each state. A refusal names the PROPERTY that
#: failed -- never a row, a value, a provider message or a SQL error.
CURRENT_VERDICT_REASONS: Mapping[str, str] = {
    "supported": "the current verdict is verified on durable evidence",
    "unsupported": "the current verdict claims verification but cites no durable evidence",
    "unverified": "no verdict has been settled for that claim",
    "needs_review": "the current verdict leaves that claim for review",
    "rejected": "the current verdict rejects that claim",
    "contested": "that claim sits in an unresolved contradiction",
    "superseded": "a conflict decision superseded that claim",
    "invalidated": "that claim is no longer active",
}

#: The verdict vocabulary of `public.claim_verdicts`, restated for validation
#: rather than re-invented: an unknown verdict is never charitably read as a
#: weaker one, it is refused as an unknown state.
VERDICT_VALUES = ("verified", "needs_review", "rejected")

#: The verification modes whose `verified` answers MUST cite durable evidence.
#: Mirrors `backend/engines/swarm_v2/support.EVIDENCE_BEARING_MODES`; a
#: `verified` verdict may only ever be reached in one of them.
_EVIDENCE_BEARING_MODES = frozenset({"deterministic_structured", "grounded_model"})


class CurrentVerdictError(ValueError):
    """A resolution failure carrying ONLY a static, code-owned reason."""


@dataclass(frozen=True)
class CurrentVerdict:
    """The resolved CURRENT state of one claim, and what decided it.

    `verdict_id` is the durable row that is current, when one is -- which is
    what a consumer compares its own citation against. A consumer that holds a
    different verdict id is holding history, not truth, however `verified` that
    history reads.
    """

    claim_id: str
    state: str
    verdict_id: str | None = None
    verdict: str | None = None
    reason: str | None = None
    verification_mode: str | None = None
    support_count: int = 0
    contract_version: str = CURRENT_VERDICT_CONTRACT_VERSION

    @property
    def supported(self) -> bool:
        """Whether this claim is verified NOW, on durable evidence."""
        return self.state == SUPPORTED_STATE

    @property
    def safe_message(self) -> str:
        return CURRENT_VERDICT_REASONS[self.state]

    def authorizes(self, verdict_id: Any) -> bool:
        """Whether THAT cited verdict is the current, supported one.

        Two questions in one, deliberately: a citation of a verdict that is no
        longer current is refused even when the current verdict also says
        `verified`, because the fact a consumer is about to write down rests
        on the row it cited and on nothing else.
        """
        return (self.supported and self.verdict_id is not None
                and str(verdict_id) == self.verdict_id)

    def as_row(self) -> dict[str, Any]:
        """The bounded shape a repository read returns for one claim."""
        return {"claim_id": self.claim_id, "state": self.state,
                "verdict_id": self.verdict_id, "verdict": self.verdict,
                "reason": self.reason, "verification_mode": self.verification_mode,
                "support_count": int(self.support_count),
                "contract_version": self.contract_version}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _created_at_key(value: Any) -> tuple[int, float, str]:
    """A total, deterministic ordering key for a durable `created_at`.

    Rows of one table are homogeneous in practice, but a resolution that is
    the authority on current truth may not depend on that: a value that cannot
    be read as a timestamp sorts by its own text, consistently, rather than
    raising or being treated as the newest thing in the table.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (1, moment.timestamp(), moment.isoformat())
    text = _text(value)
    if text is None:
        # No timestamp at all is the OLDEST thing here: a row that cannot say
        # when it was written may never displace one that can.
        return (0, 0.0, "")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return (0, 0.0, text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (1, moment.timestamp(), text)


def _support_count(row: Mapping[str, Any]) -> int:
    """How many durable evidence rows this verdict cites.

    Both durable shapes are accepted because both are real: PostgreSQL stores
    support as rows of `claim_verdict_supports` and reports a count, while the
    in-memory repository keeps the verdict's own `support` list. Neither is
    trusted to be well formed -- anything that is not a list of links, or not
    a non-negative count, is ZERO cited rows, which fails closed.
    """
    if "support_count" in row:
        raw = row.get("support_count")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return 0
        return raw
    support = row.get("support")
    if isinstance(support, (list, tuple)):
        return len(support)
    return 0


def _verdict_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """The ordering the CURRENT verdict is the maximum of.

    `created_at` first, then -- for rows written in the same instant -- a
    verdict that is NOT `verified` ahead of one that is, then the id. The
    middle term is what makes an indistinguishable tie fail closed instead of
    resolving by chance, and the id keeps the answer independent of the order
    rows were read in.

    The same expression is written in SQL as
    `order by created_at desc, (verdict = 'verified') asc, id desc limit 1`.
    """
    verified = 1 if _text(row.get("verdict")) == "verified" else 0
    return (_created_at_key(row.get("created_at")), -verified, str(row.get("id") or ""))


def current_verdict_row(verdicts: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The ONE current verdict row of a claim's history, or None."""
    rows = [row for row in verdicts if isinstance(row, Mapping)]
    return max(rows, key=_verdict_sort_key) if rows else None


def _attribute(item: Any, name: str) -> Any:
    """One field of a durable ROW or of an in-memory verdict object."""
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _object_sort_key(item: Any) -> tuple[Any, ...]:
    verified = 1 if _text(_attribute(item, "verdict")) == "verified" else 0
    return (_created_at_key(_attribute(item, "created_at")), -verified,
            str(_attribute(item, "id") or ""))


def current_verdict_by_claim(verdicts: Iterable[Any]) -> dict[str, Any]:
    """Index verdicts by claim, resolving duplicates by the SAME rule.

    For in-process verdict objects -- the ones a verification pass produces
    and a checkpoint stores -- rather than durable rows. One pass is supposed
    to settle exactly one verdict per claim, and this is what makes that a
    guarantee instead of an assumption: `{item.claim_id: item for item in
    verdicts}` silently kept whichever one came LAST, so the answer depended
    on list order, and a stray second verdict could raise a claim to
    `verified` by arriving later.

    The ordering is the one `current_verdict_row` applies: `created_at` when
    the objects carry one, then a non-`verified` verdict ahead of a `verified`
    one, then id. So an ambiguous pair fails closed, and a unique verdict per
    claim -- which is the normal case -- is returned unchanged.
    """
    grouped: dict[str, list[Any]] = {}
    for item in verdicts:
        claim_id = _text(_attribute(item, "claim_id"))
        if claim_id is not None:
            grouped.setdefault(claim_id, []).append(item)
    return {claim_id: max(items, key=_object_sort_key)
            for claim_id, items in grouped.items()}


def _state_of(row: Mapping[str, Any]) -> tuple[str, int]:
    """The state a current verdict row itself implies, and its support count."""
    verdict = _text(row.get("verdict"))
    support = _support_count(row)
    if verdict not in VERDICT_VALUES:
        # An unknown verdict is not a weaker known one. It states nothing this
        # contract can read, so it supports nothing.
        return ("unsupported", support)
    if verdict != "verified":
        return (verdict, support)
    mode = _text(row.get("verification_mode"))
    contract = _text(row.get("verifier_contract_version"))
    if mode not in _EVIDENCE_BEARING_MODES or contract is None:
        # A `verified` that cannot say HOW it was reached, or that was reached
        # in a mode which never compares evidence, is an assertion.
        return ("unsupported", support)
    return (("supported" if support > 0 else "unsupported"), support)


def resolve_current_verdict(*, claim: Mapping[str, Any],
                            verdicts: Sequence[Mapping[str, Any]] = (),
                            superseded_claim_ids: Iterable[Any] = (),
                            contested_claim_ids: Iterable[Any] = ()) -> CurrentVerdict:
    """Resolve ONE claim's current verdict state. Total, pure, deterministic.

    `claim` is the durable claim row. `verdicts` is that claim's verdict
    history -- in any order, including one containing replayed duplicates of
    the same logical verdict. The two id sets are the durable contradiction
    state: claims a `conflict_resolutions` row superseded, and claims an
    unresolved `conflicts` row still contests.
    """
    if not isinstance(claim, Mapping):
        raise CurrentVerdictError("a durable claim row is required")
    claim_id = _text(claim.get("id")) or _text(claim.get("claim_id"))
    if claim_id is None:
        raise CurrentVerdictError("a durable claim row must state its id")
    status = _text(claim.get("status")) or "active"
    if status != "active":
        return CurrentVerdict(claim_id=claim_id, state="invalidated")
    if claim_id in {str(item) for item in superseded_claim_ids}:
        return CurrentVerdict(claim_id=claim_id, state="superseded")
    if claim_id in {str(item) for item in contested_claim_ids}:
        return CurrentVerdict(claim_id=claim_id, state="contested")
    row = current_verdict_row(
        row for row in verdicts
        if isinstance(row, Mapping) and str(row.get("claim_id") or "") == claim_id)
    if row is None:
        return CurrentVerdict(claim_id=claim_id, state="unverified")
    state, support = _state_of(row)
    return CurrentVerdict(claim_id=claim_id, state=state,
                          verdict_id=_text(row.get("id")), verdict=_text(row.get("verdict")),
                          reason=_text(row.get("reason")),
                          verification_mode=_text(row.get("verification_mode")),
                          support_count=support)


def resolve_current_verdicts(*, claims: Iterable[Mapping[str, Any]],
                             verdicts: Iterable[Mapping[str, Any]],
                             superseded_claim_ids: Iterable[Any] = (),
                             contested_claim_ids: Iterable[Any] = (),
                             ) -> dict[str, CurrentVerdict]:
    """Resolve a whole set of claims at once, by the same rule, keyed by claim id."""
    history: dict[str, list[Mapping[str, Any]]] = {}
    for row in verdicts:
        if not isinstance(row, Mapping):
            continue
        key = _text(row.get("claim_id"))
        if key is not None:
            history.setdefault(key, []).append(row)
    superseded = {str(item) for item in superseded_claim_ids}
    contested = {str(item) for item in contested_claim_ids}
    resolved: dict[str, CurrentVerdict] = {}
    for claim in claims:
        current = resolve_current_verdict(
            claim=claim, verdicts=history.get(_text(claim.get("id")) or "", ()),
            superseded_claim_ids=superseded, contested_claim_ids=contested)
        resolved[current.claim_id] = current
    return resolved


def superseded_claim_ids(resolutions: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Every claim a durable conflict RESOLUTION decided against."""
    losers: set[str] = set()
    for row in resolutions:
        if not isinstance(row, Mapping) or _text(row.get("state")) != "resolved":
            continue
        for claim_id in row.get("superseded_claim_ids") or ():
            text = _text(claim_id)
            if text is not None:
                losers.add(text)
    return frozenset(losers)


def contested_claim_ids(conflicts: Iterable[Mapping[str, Any]], *,
                        unresolved_outcome: str = "unresolved_needs_review"
                        ) -> frozenset[str]:
    """Every claim an UNRESOLVED contradiction still covers."""
    contested: set[str] = set()
    for row in conflicts:
        if not isinstance(row, Mapping) or _text(row.get("outcome")) != unresolved_outcome:
            continue
        for claim_id in row.get("claim_ids") or ():
            text = _text(claim_id)
            if text is not None:
                contested.add(text)
    return frozenset(contested)


def parse_current_verdict(row: Any) -> CurrentVerdict:
    """Rebuild a resolved state from a durable read, or fail closed.

    A repository read is durable data, not a trusted object: an unknown state,
    a missing claim id or a `supported` state that names no verdict row is
    refused here rather than believed.
    """
    if isinstance(row, CurrentVerdict):
        return row
    if not isinstance(row, Mapping):
        raise CurrentVerdictError("a resolved current-verdict row is required")
    claim_id = _text(row.get("claim_id"))
    state = _text(row.get("state"))
    if claim_id is None or state not in CURRENT_VERDICT_STATES:
        raise CurrentVerdictError("a resolved current-verdict row is malformed")
    verdict_id = _text(row.get("verdict_id"))
    if state == SUPPORTED_STATE and verdict_id is None:
        raise CurrentVerdictError("a supported state must name its current verdict")
    support = row.get("support_count")
    return CurrentVerdict(
        claim_id=claim_id, state=state, verdict_id=verdict_id,
        verdict=_text(row.get("verdict")), reason=_text(row.get("reason")),
        verification_mode=_text(row.get("verification_mode")),
        support_count=support if isinstance(support, int) and not isinstance(support, bool)
                      and support >= 0 else 0,
        contract_version=_text(row.get("contract_version")) or CURRENT_VERDICT_CONTRACT_VERSION)


__all__ = ["CURRENT_VERDICT_CONTRACT_VERSION", "CURRENT_VERDICT_REASONS",
           "CURRENT_VERDICT_STATES", "SUPPORTED_STATE", "VERDICT_VALUES", "CurrentVerdict",
           "CurrentVerdictError", "contested_claim_ids", "current_verdict_by_claim",
           "current_verdict_row",
           "parse_current_verdict", "resolve_current_verdict", "resolve_current_verdicts",
           "superseded_claim_ids"]
