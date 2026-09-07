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

R3 update: this contract now also carries the provenance the evidence itself
records -- the version of the source the fragments were read at, the exact
locator each fragment came from, and whether a fragment is a verbatim
document excerpt or a deterministic projection of a structured record.  All
three are OPTIONAL: a source or fragment written before R3 carries none of
them and resolves exactly as it did before, which is what keeps historical
records readable without pretending they are complete R3 evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence
from uuid import UUID

from .comparison import StructuredSourceFact
from .contracts import EvidenceReference
from .evidence_bounds import FRAGMENT_TYPES, MAX_FACTS_PER_BUNDLE
from .evidence_contracts import (EvidenceContractError, fragment_type_for, parse_locator_key,
                                 parse_version_key)
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
# R4: the structured facts durably recorded FROM a source are the comparison
# authority of the deterministic verifier.  One bundle may hold at most
# MAX_FACTS_PER_BUNDLE facts, and a source may legitimately be acquired by
# more than one bundle, so the read is bounded generously per source but still
# hard-bounded; a source holding more is corrupted context and fails closed
# rather than arriving quietly trimmed.
MAX_STRUCTURED_FACTS_PER_SOURCE = 4 * MAX_FACTS_PER_BUNDLE
STRUCTURED_FACT_OVER_READ_PER_SOURCE = MAX_STRUCTURED_FACTS_PER_SOURCE + 1

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
    the text itself, plus (R3) the exact location the text was read from and
    whether it is a verbatim document excerpt or a deterministic projection
    of a structured record.  Row ids, evidence keys, timestamps and every
    other database internal stay out.

    `fragment_type` and `locator` are all-or-nothing and optional: an R3
    fragment always has both, a pre-R3 fragment has neither, and a
    half-specified one is corrupted grounding context that fails closed.  A
    locator is validated with the SAME parser the acquisition contract uses:
    it must be the canonical rendering of one of the two closed locator
    shapes, and the fragment type must be the one that shape allows.
    """

    fragment_index: int
    content_hash: str
    text: str
    fragment_type: str | None = None
    locator: str | None = None
    # R4: the durable row id, when the resolver could supply one.  It is the
    # exact evidence identity a stored verdict's support link records, so a
    # verdict can be replayed against the row that produced it rather than
    # against a hash that merely matches.  A resolver that never reads a
    # database legitimately has none, and the content hash still identifies
    # the evidence -- which is why this is optional and never a substitute for
    # the hash.
    fragment_id: str | None = None

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
        if (self.fragment_type is None) != (self.locator is None):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if self.fragment_type is not None and self.fragment_type not in FRAGMENT_TYPES:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if self.fragment_id is not None and (not isinstance(self.fragment_id, str)
                                             or not 1 <= len(self.fragment_id) <= 200):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if self.locator is not None:
            try:
                located = parse_locator_key(self.locator)
            except EvidenceContractError:
                # `from None`: the contract error is static, but keep ONE
                # grounding reason at this boundary.
                raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None
            if fragment_type_for(located) != self.fragment_type:
                raise GroundingContractError("SOURCE_CONTEXT_INVALID")

    @property
    def identity(self) -> tuple[str, str | None]:
        """What makes a durable fragment THIS fragment: its text and its place.

        R3 evidence identity is the content hash together with the locator, so
        the same sentence read from two different records (or two fields of
        one record) is two pieces of evidence.  A pre-R3 fragment has no
        locator and is identified by its text alone, exactly as before.
        """
        return (self.content_hash, self.locator)


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
    source_version: str | None = None
    fragments: tuple[SourceFragment, ...] = ()
    # R4: the structured facts durably recorded FROM this source.  They are
    # the comparison authority of the deterministic verifier: a claim about
    # this source is checked against what the source itself states, not
    # against what a model believes about it.  Empty means the source is
    # free-text (or pre-R3) evidence, and such a claim continues to the
    # grounded model verifier exactly as before.
    facts: tuple[StructuredSourceFact, ...] = ()

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
        # R3: the canonical `kind:identifier` of the version this source was
        # read at.  None means a pre-R3 source whose version was never
        # captured -- readable, but never complete R3 evidence.  A malformed
        # or unknown version kind is corruption and fails closed.
        if self.source_version is not None:
            try:
                parse_version_key(self.source_version)
            except EvidenceContractError:
                # The SAME kind-specific rule as the acquisition contract and
                # the guarded RPC: `content_sha256:abc` is corruption here too.
                raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None
        if not isinstance(self.fragments, tuple):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if len(self.fragments) > MAX_FRAGMENTS_PER_SOURCE:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if sum(len(item.text) for item in self.fragments) > MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        # Durable identity is (source, task, content hash, locator): one
        # source can never legitimately hold the same fragment from the same
        # place twice, but the SAME text read from two different locators is
        # two distinct pieces of R3 evidence and must resolve.  A pre-R3
        # fragment has no locator, so for legacy evidence this is exactly the
        # content-hash uniqueness that always applied.
        identities = [item.identity for item in self.fragments]
        if len(set(identities)) != len(identities):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if not isinstance(self.facts, tuple):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if len(self.facts) > MAX_STRUCTURED_FACTS_PER_SOURCE:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        if any(not isinstance(item, StructuredSourceFact) or item.source_id != self.source_id
               or item.task_id != self.task_id for item in self.facts):
            # A fact of another source, or of another task, can never describe
            # THIS source: that is corrupted context, not missing context.
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        fact_ids = [item.fact_id for item in self.facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")

    @property
    def structured_facts(self) -> tuple[StructuredSourceFact, ...]:
        """This source's located structured facts, in deterministic order."""
        return tuple(sorted((item for item in self.facts if item.locator),
                            key=lambda item: item.fact_id))

    @property
    def content_hashes(self) -> frozenset[str]:
        """The ONLY hashes a verified verdict may cite for this source."""
        return frozenset(item.content_hash for item in self.fragments)

    @property
    def is_r3_qualified(self) -> bool:
        """Whether this context is COMPLETE R3 evidence.

        True only when the source records the version it was read at and every
        fragment records exactly where it came from.  A historical source is
        still perfectly readable and still grounds a verdict from the text it
        captured -- it simply is not complete R3 evidence, and this property is
        how that distinction is stated rather than inferred.
        """
        return bool(self.source_version) and bool(self.fragments) and \
            all(item.locator and item.fragment_type for item in self.fragments)

    def ordered_fragments(self) -> tuple[SourceFragment, ...]:
        return tuple(sorted(self.fragments, key=lambda item: (item.fragment_index, item.content_hash,
                                                              item.locator or "")))


@dataclass(frozen=True)
class GroundedCandidate:
    """One claim joined to the durable evidence of its OWN source."""

    reference: EvidenceReference
    source: ResolvedSourceEvidence

    def __post_init__(self) -> None:
        if self.reference.source_id != self.source.source_id or \
                self.reference.task_id != self.source.task_id:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        # R3: a claim that recorded the version it was read at may only be
        # grounded by that exact version of its source.  A reference with no
        # version is pre-R3 and is not held to a version it never had.
        if self.reference.source_version is not None and \
                self.reference.source_version != self.source.source_version:
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
        facts = self._read_structured_facts(source_ids, sources)
        return {source_id: self._context(sources[source_id], fragments.get(source_id, ()),
                                         facts.get(source_id, ()))
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
                                              text=row["fragment_text"],
                                              fragment_type=row.get("fragment_type"),
                                              locator=row.get("locator_key"),
                                              fragment_id=(None if row.get("id") is None
                                                           else str(row["id"])))
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

    def _read_structured_facts(self, source_ids: Sequence[str],
                               sources: Mapping[str, Mapping[str, Any]],
                               ) -> dict[str, list[StructuredSourceFact]]:
        """R4: the located structured facts durably recorded FROM these sources.

        The third and last bounded internal read.  Like the other two it is
        run-scoped, source-scoped, column-allowlisted and caller-parameterised
        -- never SQL, never model-steerable and never a network call.  A
        repository that does not expose the read at all simply yields no facts,
        so a pre-R4 deployment keeps exactly its previous behaviour instead of
        failing.

        Only LOCATED claims count as structured facts: a claim with no evidence
        locator is a statement someone made about a source, not a fact read
        from an exact place inside it, and it is never a comparison authority.
        """
        read = getattr(self._repository, "list_structured_facts_for_sources", None)
        if read is None:
            return {}
        facts: dict[str, list[StructuredSourceFact]] = {}
        for chunk in _chunks(source_ids, MAX_SOURCES_PER_RESOLVER_READ):
            rows = read(self._run_id, chunk,
                        limit=len(chunk) * STRUCTURED_FACT_OVER_READ_PER_SOURCE)
            for row in rows:
                source_id = str(row.get("source_id"))
                source = sources.get(source_id)
                if source is None or str(row.get("run_id")) != self._run_key:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                # task -> source -> fact is one lineage, exactly as it is for
                # a fragment: the same run is not enough.
                if row.get("task_key") != source.get("task_key"):
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                locator = row.get("evidence_locator")
                if locator is None:
                    continue  # a claim, not a located structured fact
                try:
                    parse_locator_key(locator)
                except EvidenceContractError:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None
                identity = row.get("identity_scope") or {}
                if not isinstance(identity, Mapping):
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                time_scope = row.get("time_scope") or {}
                if not isinstance(time_scope, Mapping):
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
                try:
                    fact = StructuredSourceFact(
                        fact_id=str(row["id"]), source_id=source_id,
                        task_id=str(row["task_key"]), field=str(row["field_key"]),
                        entity=str(row["entity_key"]), value=row["value"],
                        unit=row.get("unit"), geography=row.get("geography"),
                        market=row.get("market"), time_scope=dict(time_scope),
                        identity=dict(identity), locator=str(locator))
                except (KeyError, TypeError, ValueError):
                    # `from None`: the raised message would quote durable evidence.
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None
                owned = facts.setdefault(source_id, [])
                owned.append(fact)
                # The over-read exists so this can fire: a source holding more
                # structured facts than the durable bound is corrupted context
                # and is never quietly trimmed to the first N rows.
                if len(owned) > MAX_STRUCTURED_FACTS_PER_SOURCE:
                    raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        return facts

    @staticmethod
    def _context(row: Mapping[str, Any], fragments: Iterable[SourceFragment],
                 facts: Iterable[StructuredSourceFact] = ()) -> ResolvedSourceEvidence:
        """Build one source context from the explicit safe column allowlist."""
        ordered = tuple(sorted(fragments, key=lambda item: (item.fragment_index, item.content_hash,
                                                            item.locator or "")))
        # R3: the version columns are all-or-nothing.  A half-populated pair is
        # never guessed or repaired -- it is corrupted provenance.
        kind, identifier = row.get("source_version_kind"), row.get("source_version_id")
        if (kind is None) != (identifier is None):
            raise GroundingContractError("SOURCE_CONTEXT_INVALID")
        try:
            return ResolvedSourceEvidence(
                source_id=str(row["id"]), task_id=row.get("task_key"), url=row.get("url"),
                title=row.get("title"), domain=row.get("domain"),
                source_type=row.get("source_type"), source_strength=row.get("source_strength"),
                source_date=row.get("source_date"),
                source_version=None if kind is None else f"{kind}:{identifier}",
                fragments=ordered,
                facts=tuple(sorted(facts, key=lambda item: item.fact_id)))
        except KeyError:
            raise GroundingContractError("SOURCE_CONTEXT_INVALID") from None


__all__ = ["FRAGMENT_OVER_READ_PER_SOURCE", "GROUNDING_REASONS",
           "MAX_SOURCES_PER_RESOLVER_READ", "MAX_STRUCTURED_FACTS_PER_SOURCE",
           "STRUCTURED_FACT_OVER_READ_PER_SOURCE",
           "VERIFIER_GROUNDING_VERSION", "EvidenceResolver", "GroundedCandidate",
           "GroundingContractError", "RepositoryEvidenceResolver", "ResolvedSourceEvidence",
           "SourceFragment", "resolve_source_context"]
