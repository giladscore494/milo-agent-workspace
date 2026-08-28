"""B5: durable source evidence resolution for the grounded Verifier.

Before B5 the Verifier received EvidenceReference metadata only -- a claim, a
source id and a URL -- so a model could settle a claim from plausibility or
world knowledge instead of from the evidence the research actually captured.
This module is the missing link:

    EvidenceReference.source_id
      -> durable public.sources metadata
      -> durable public.source_evidence_fragments text (B2)

Boundaries this module exists to keep:

*   The Verifier never touches a database, a URL, a tool or a provider.  It
    receives an injected EvidenceResolver and can therefore only ever see what
    a resolver returns.
*   A resolver reads DURABLE material only.  There is no live fetch fallback:
    a source whose page was never captured has no grounding context, and the
    verifier evaluates the evidence that existed when the research ran -- not
    whatever the web says today.
*   Nothing here reconstructs, summarises or asks a model for evidence.  B2
    fragments are the sole evidence authority.
*   Absence of evidence is not corruption.  B2 deliberately allows a source
    with no fragment, so `source exists + zero fragments` resolves to a source
    context with no fragments; the Verifier turns that into a deterministic
    needs_review/SOURCE_CONTEXT_UNAVAILABLE.  A cross-run, cross-task or
    out-of-bounds source relationship is a different thing entirely and fails
    closed.

Stated plainly: the production ToolRegistry does not yet register a real
source-bearing web/vehicle tool, so today's runs capture few or no fragments
and this contract will mostly resolve to SOURCE_CONTEXT_UNAVAILABLE.  That is
the correct, honest outcome -- ungrounded claims stop reaching `verified`
now -- and it is deliberately NOT patched over here with a network fetcher or
a stand-in tool.  Real capture arrives when a later PR wires acquisition into
EvidenceBoard.record_source_with_evidence().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence
from uuid import UUID

from .contracts import EvidenceReference
from .fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE,
                        MAX_FRAGMENTS_PER_SOURCE, fragment_content_hash)

# The verifier-grounding contract version.  It lives here, next to the
# evidence contract it describes, so SwarmState and the Verifier can both
# depend on it without importing each other.  Version 0 means "verifier
# verdicts in this checkpoint were produced BEFORE grounded verification
# existed"; version 1 means "every model-backed verdict in this checkpoint was
# produced against durable B2 source evidence".
VERIFIER_GROUNDING_VERSION = 1

# One bounded internal read covers this many sources at a time.
#
# The chunk size is derived from a detection requirement, not from the
# repository's cap.  A fragment read limited to exactly MAX_FRAGMENTS_PER_SOURCE
# rows per source cannot tell a source holding the legal maximum from a
# corrupted one holding more: the row that would prove the corruption is the
# one the LIMIT drops.  So every read deliberately asks for ONE row per source
# beyond the durable bound, purely so an over-limit source is observable and
# can fail closed instead of arriving quietly trimmed.
#
# That over-read is what fixes the chunk size:
#
#     40 sources * (4 + 1) rows == 200 == MAX_EVIDENCE_FRAGMENT_ROWS
#
# so a full chunk still fits the repository's own row cap with nothing lost,
# and 40 <= MAX_SOURCE_CONTEXT_ROWS keeps the paired source read inside its cap
# too.  tests/test_swarm_v2_grounded_verifier.py pins all three relationships.
MAX_SOURCES_PER_RESOLVER_READ = 40
# One row past the durable per-source bound: enough to DETECT corruption,
# never enough to hold a fifth fragment in a resolved context.
FRAGMENT_OVER_READ_PER_SOURCE = MAX_FRAGMENTS_PER_SOURCE + 1

GROUNDING_REASONS = frozenset({"SOURCE_CONTEXT_INVALID"})

_HASH_SHAPE = re.compile(r"^[0-9a-f]{64}$")


class GroundingContractError(ValueError):
    """A grounding contract failure carrying ONLY a static code.

    Source text, URLs, titles, stored hashes and repository diagnostics never
    reach this exception, so its message is safe for durable state, run events
    and telemetry.
    """

    MESSAGES = {
        "SOURCE_CONTEXT_INVALID": "durable source context violates the grounding contract",
    }

    def __init__(self, reason_code: str):
        if reason_code not in GROUNDING_REASONS:
            raise ValueError("grounding reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class SourceFragment:
    """One durable, bounded piece of quoted source text.

    Carries exactly what verification needs -- position, durable identity and
    the text itself.  Row ids, evidence keys, timestamps and every other
    database internal stay out.
    """

    fragment_index: int
    content_hash: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_FRAGMENT_CHARS:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if (not isinstance(self.fragment_index, int) or isinstance(self.fragment_index, bool)
                or not 0 <= self.fragment_index < MAX_FRAGMENTS_PER_SOURCE):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if not isinstance(self.content_hash, str) or not _HASH_SHAPE.match(self.content_hash):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        # The durable hash is recomputed from the durable text with the SAME
        # B2 helper that produced it, so a rewritten fragment, a hash from a
        # different fragment and a model-invented hash all fail closed here.
        if fragment_content_hash(self.text) != self.content_hash:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")


@dataclass(frozen=True)
class ResolvedSourceEvidence:
    """The complete grounding context of ONE durable source.

    The B2 durable bounds are re-enforced by this constructor rather than
    trusted from the repository: an over-long fragment, a fifth fragment or an
    over-budget source is a corrupted grounding context, never something to
    quietly trim down to the first four rows.
    """

    source_id: str
    task_id: str
    url: str
    title: str
    domain: str
    source_type: str
    source_strength: str
    source_date: str | None
    fragments: tuple[SourceFragment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if any(not isinstance(value, str) for value in
               (self.url, self.title, self.domain, self.source_type, self.source_strength)):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if self.source_date is not None and not isinstance(self.source_date, str):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if not isinstance(self.fragments, tuple):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if len(self.fragments) > MAX_FRAGMENTS_PER_SOURCE:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if sum(len(item.text) for item in self.fragments) > MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        # Durable identity is (source, task, content hash), so one source can
        # never legitimately hold the same fragment twice; a duplicate would
        # also make the verified-evidence hash lookup ambiguous.
        hashes = [item.content_hash for item in self.fragments]
        if len(set(hashes)) != len(hashes):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")

    @property
    def content_hashes(self) -> frozenset[str]:
        """The ONLY hashes a verified verdict may cite for this source."""
        return frozenset(item.content_hash for item in self.fragments)

    def ordered_fragments(self) -> tuple[SourceFragment, ...]:
        return tuple(sorted(self.fragments, key=lambda item: (item.fragment_index, item.content_hash)))


@dataclass(frozen=True)
class GroundedCandidate:
    """One claim joined to the durable evidence of its OWN source."""

    reference: EvidenceReference
    source: ResolvedSourceEvidence

    def __post_init__(self) -> None:
        if self.reference.source_id != self.source.source_id or \
                self.reference.task_id != self.source.task_id:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")

    @property
    def claim_id(self) -> str:
        return self.reference.claim_id


class EvidenceResolver(Protocol):
    """Maps evidence references to their durable source context.

    The ONE boundary allowed to reach durable storage on the verifier's
    behalf.  An implementation may perform bounded internal repository reads
    and nothing else: no web access, no URL re-fetch, no provider call, no
    tool execution and no reconstruction of evidence that was never captured.

    Returns one ResolvedSourceEvidence per REFERENCED source id, keyed by that
    source id.  A source that exists with no captured fragment resolves to a
    context whose `fragments` is empty; a source that cannot be resolved
    safely raises GroundingContractError.
    """

    def resolve(self, evidence: Sequence[EvidenceReference]) -> Mapping[str, ResolvedSourceEvidence]:
        ...


def resolve_source_context(resolver: EvidenceResolver,
                           references: Sequence[EvidenceReference]) -> dict[str, ResolvedSourceEvidence]:
    """Return claim_id -> validated source context, or fail closed.

    The provenance firewall is applied HERE, to whatever the injected resolver
    returned, so a permissive resolver cannot widen what counts as grounding:
    every reference must resolve to a context for its exact source id whose
    task provenance matches the claim's own.
    """
    resolved = resolver.resolve(tuple(references))
    if not isinstance(resolved, Mapping):
        raise GroundingContractError("SOURCE_CONTEXT_INVALID")
    contexts: dict[str, ResolvedSourceEvidence] = {}
    for item in references:
        context = resolved.get(item.source_id)
        if not isinstance(context, ResolvedSourceEvidence):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if context.source_id != item.source_id or context.task_id != item.task_id:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        contexts[item.claim_id] = context
    return contexts


def _chunks(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[start:start + size] for start in range(0, len(values), size)]


class RepositoryEvidenceResolver:
    """The production EvidenceResolver: two bounded internal reads, nothing else.

    Reads are deduplicated by source id, so twenty claims sharing one source
    resolve that source ONCE.  No repository method other than the two bounded
    run-scoped reads is reachable from here, and neither of them can be
    steered by model output: the caller supplies a run plus source ids.
    """

    def __init__(self, repository: Any, *, run_id: UUID | str):
        self._repository = repository
        self._run_id = run_id
        self._run_key = str(run_id)

    def resolve(self, evidence: Sequence[EvidenceReference]) -> dict[str, ResolvedSourceEvidence]:
        references = list(evidence)
        if any(item.run_id != self._run_key for item in references):
            # A reference from another run can never be grounded by this run's
            # durable evidence; that is corruption, not missing context.
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        source_ids = sorted({item.source_id for item in references})
        if not source_ids:
            return {}
        sources = self._read_sources(source_ids)
        fragments = self._read_fragments(source_ids, sources)
        return {source_id: self._context(sources[source_id], fragments.get(source_id, ()))
                for source_id in source_ids}

    def _read_sources(self, source_ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        rows: dict[str, Mapping[str, Any]] = {}
        for chunk in _chunks(source_ids, MAX_SOURCES_PER_RESOLVER_READ):
            requested = set(chunk)
            for row in self._repository.list_sources_for_ids(self._run_id, chunk, limit=len(chunk)):
                identity = str(row.get("id"))
                if str(row.get("run_id")) != self._run_key or identity not in requested \
                        or identity in rows:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                rows[identity] = row
        if set(rows) != set(source_ids):
            # A claim references a source that does not exist in this run.
            # public.claims.source_id is a restricted foreign key, so this is a
            # broken durable relationship -- never "no context available".
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        return rows

    def _read_fragments(self, source_ids: Sequence[str],
                        sources: Mapping[str, Mapping[str, Any]]) -> dict[str, list[SourceFragment]]:
        fragments: dict[str, list[SourceFragment]] = {}
        for chunk in _chunks(source_ids, MAX_SOURCES_PER_RESOLVER_READ):
            rows = self._repository.list_evidence_fragments_for_sources(
                self._run_id, chunk, limit=len(chunk) * FRAGMENT_OVER_READ_PER_SOURCE)
            for row in rows:
                source_id = str(row.get("source_id"))
                source = sources.get(source_id)
                if source is None or str(row.get("run_id")) != self._run_key:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                # task -> source -> fragment is one lineage: the same run is
                # not enough for a fragment to ground this source's claims.
                if row.get("task_key") != source.get("task_key"):
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                try:
                    fragment = SourceFragment(fragment_index=row["fragment_index"],
                                              content_hash=row["content_hash"],
                                              text=row["fragment_text"])
                except (KeyError, TypeError):
                    # `from None`: the raised message would quote durable text.
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None
                owned = fragments.setdefault(source_id, [])
                owned.append(fragment)
                # The over-read exists so this can fire. A source holding more
                # than the durable bound is corrupted grounding context and
                # fails closed here -- it is never quietly trimmed back to the
                # first MAX_FRAGMENTS_PER_SOURCE rows, which would present a
                # broken source as a valid one.
                if len(owned) > MAX_FRAGMENTS_PER_SOURCE:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        return fragments

    @staticmethod
    def _context(row: Mapping[str, Any],
                 fragments: Iterable[SourceFragment]) -> ResolvedSourceEvidence:
        """Build one source context from the explicit safe column allowlist."""
        ordered = tuple(sorted(fragments, key=lambda item: (item.fragment_index, item.content_hash)))
        try:
            return ResolvedSourceEvidence(
                source_id=str(row["id"]), task_id=row.get("task_key"), url=row.get("url"),
                title=row.get("title"), domain=row.get("domain"),
                source_type=row.get("source_type"), source_strength=row.get("source_strength"),
                source_date=row.get("source_date"), fragments=ordered)
        except KeyError:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None


__all__ = ["FRAGMENT_OVER_READ_PER_SOURCE", "GROUNDING_REASONS",
           "MAX_SOURCES_PER_RESOLVER_READ",
           "VERIFIER_GROUNDING_VERSION", "EvidenceResolver", "GroundedCandidate",
           "GroundingContractError", "RepositoryEvidenceResolver", "ResolvedSourceEvidence",
           "SourceFragment", "resolve_source_context"]
