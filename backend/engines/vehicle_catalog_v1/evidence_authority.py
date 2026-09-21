"""R5: evidence, not assertion, is what makes a V1 field verified.

What was wrong
--------------

V1's `source_verifier` was handed field NAMES and nothing else. Its input was
built by `compact_verifier_input`, which reduced every technical record to

    {"model": ..., "confidence": ..., "sources": [...],
     "fields": ["engine", "power_hp", "seats"]}

-- the list of keys that happened to be non-empty. So the phase that decided
whether a model was `verified` could not see a single value it was verifying.
A model could state `power_hp: 5000`, and the verifier would confirm that the
record HAS a power field.

And whatever it concluded went nowhere durable. `verification_status:
"verified"` was a string in a JSON document, resting on a model's assertion
and on a list of URLs the same model wrote down. Nothing recorded WHICH value
was verified, WHERE it was read, at WHICH version of the source, or by WHICH
evidence -- so nothing could be re-checked, and a model-reported URL was, by
itself, the whole of the provenance.

What this module is
-------------------

Trusted server code that turns V1's observed technical records into the SAME
durable evidence primitives Swarm V2 uses -- there is no second truth system
here, and deliberately so:

    VersionedEvidenceSource   the claimed source, pinned to a content version
                              the server computed over the record it read
    FocusedEvidenceFragment   a deterministic `structured_projection` of the
                              record at an exact `record_field` locator
    StructuredEvidenceFact    field + value + unit + identity, at that locator
    ClaimCreate               the durable claim the Evidence Board writes
    VerificationVerdict       the durable decision, with support links naming
                              the exact fragments it rests on

so a V1 verified field is the same chain every other verified fact in this
repository is:

    field/value -> claim -> source -> version + locator -> durable fragment

WHAT THE MODEL MAY AND MAY NOT DO
---------------------------------

A model proposes and classifies. It may state a record, and it may say a model
looks wrong -- which is taken, and can only ever DOWNGRADE a field. It may not
decide that anything is verified, it may not name a source version, a locator,
an entity key or an evidence identity (every one of those is derived here from
the record's own content), and a URL it wrote down is source METADATA rather
than evidence: a record that states no usable value at that location produces
no fragment, no claim and no verdict at all, so there is nothing for a
`verified` to attach to.

THE FOUR DETERMINISTIC RULES
----------------------------

Applied by this module, over durable evidence, never by a prompt:

1.  **Located support.** The value must be readable at an exact locator of a
    versioned record, and exactly one durable fragment must sit at that
    locator. No locator, no version, no fragment -> no verified field.
2.  **Value identity.** Two records of one run that state DIFFERENT quantities
    for the same field, in the same identity scope, contradict each other.
    Both are rejected rather than silently resolved by confidence, which is
    what the merge did. `value_identity` is the shared contract, so `1.6 l`
    and `1600 cc` are one statement and `1600 hp` and `1600 kw` are two.
3.  **Market evidence.** In a market whose policy requires Israeli evidence,
    the deterministic `source_policy` classification of the record's own URLs
    decides whether they are Israeli-market evidence. This has always been
    deterministic server code; what is new is that it now gates the VERDICT
    rather than only annotating the final document.
4.  **The model's own classification, downwards only.**

WHAT A `verified` V1 FIELD REQUIRES
-----------------------------------

Not the decision this module just made. `verified` survives only when ALL of
the following are true, and each one of them used to be a way it failed OPEN:

*   the verdict AND its durable support links were persisted successfully;
*   the AUTHORITATIVE current-state read succeeded. "We could not tell" is
    never read as "still verified" -- no repository, no such read, no lease, a
    failed read and a malformed answer all demote the field;
*   `CurrentVerdict.state` is `supported`;
*   and the current `verdict_id` is EXACTLY the verdict this pass settled. A
    supported state naming another row is history, or current-state drift, and
    the fact being written rests on the row that was settled and on nothing
    else.

Anything else demotes the field to `needs_review` with a static reason naming
which requirement failed (`V1_EVIDENCE_VERDICT_NOT_DURABLE`,
`V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE`, `V1_EVIDENCE_CURRENT_STATE_DRIFT`,
`V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED`). The research run carries on either
way; a LOST LEASE still escapes as infrastructure.

So V1 obeys exactly the rule every other consumer of verified evidence obeys
(`backend/engines/swarm_v2/current_verdict.py`): an older `verified` row that a
newer verdict, contradiction or supersession has replaced authorizes nothing --
and neither does a verdict nobody can confirm is durable and current.

WITHOUT A BOARD
---------------

The Evidence Board is optional, and its absence is not a loophole -- it is the
strictest case. With no board (a local run, a test, any wiring with no
repository and no run lease) every bundle is still built and validated and
every field is still decided, nothing is persisted, and therefore NOTHING is
verified: a field with no durable claim fails the first requirement above
before the others are even asked.

NO PROMOTION PATH. V1 evidence records `tool_operation` of its own, which is
not the one `catalog_run_pending_promotions` matches on, so nothing written
here can ever reach the canonical catalog. That is a property of the data, not
a flag: the promotion read names one registered Government operation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Sequence

from backend.engines.swarm_v2.comparison import value_identity
from backend.engines.swarm_v2.contracts import SupportLink, VerificationVerdict
from backend.engines.swarm_v2.current_verdict import (CurrentVerdict, CurrentVerdictError,
                                                      parse_current_verdict)
from backend.engines.swarm_v2.evidence_bounds import (IDENTITY_DIMENSIONS,
                                                      MAX_FACTS_PER_BUNDLE,
                                                      MAX_IDENTITY_DIMENSION_CHARS)
from backend.engines.swarm_v2.evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                                         SourceVersion, StructuredEvidenceFact,
                                                         VersionedEvidenceSource,
                                                         build_evidence_bundle, canonical_json,
                                                         record_field_locator,
                                                         structured_projection)
from backend.engines.swarm_v2.fragments import MAX_FRAGMENTS_PER_SOURCE
from backend.engines.swarm_v2.normalization import normalize_field_key
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.errors import LEASE_FAILURE_CODES, AppError

from .source_policy import evaluate_sources, market_requires_israel_policy

#: The provenance V1 evidence records on every source it writes. It is NOT the
#: one registered register operation `public.catalog_run_pending_promotions`
#: matches on (`backend/catalog/pipeline.PROMOTABLE_TOOL_OPERATION` names it),
#: so a V1 source can never be picked up by the canonical promotion path,
#: whatever any flag says.
V1_EVIDENCE_TOOL = "vehicle_catalog_v1"
V1_EVIDENCE_OPERATION = "technical_enrichment"
V1_TOOL_OPERATION = f"{V1_EVIDENCE_TOOL}.{V1_EVIDENCE_OPERATION}"

#: The task provenance every V1 evidence row carries.
V1_TASK_KEY = "vehicle_catalog_v1.verification"

#: The verification mode V1 decides in. A located value compared against the
#: record it was read from is a deterministic STRUCTURED decision, which is
#: also the only mode whose `verified` answers may cite durable evidence.
V1_VERIFICATION_MODE = "deterministic_structured"

#: The fields of each technical agent that may become durable evidence, per
#: agent, as a closed server-owned allowlist. Derived from what
#: `core.merge_model_data` actually reads out of each agent's record, minus the
#: prose fields (`safety`, `equipment_notes`, `notes`, `trims`): a deterministic
#: projection can only quote a SCALAR, and prose is not a located fact.
V1_EVIDENCE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "trims_years_agent": ("generation_or_series", "years_sold"),
    "engines_fuel_power_agent": ("engine", "fuel_type", "power_hp", "torque_nm"),
    "transmission_drivetrain_performance_agent": ("drivetrain", "transmission",
                                                  "zero_to_100_kmh_sec"),
    "dimensions_safety_equipment_agent": ("body_type", "height_mm", "length_mm", "seats",
                                          "trunk_liters", "width_mm"),
}

#: The unit a NUMERIC value of each field is stated in. "1798 is not a fact
#: until it says cc": `StructuredEvidenceFact` refuses a number with no unit,
#: and the shared comparison contract can only relate two quantities through a
#: unit. A non-numeric value carries no unit at all.
V1_FIELD_UNITS: Mapping[str, str] = {
    "height_mm": "mm", "length_mm": "mm", "width_mm": "mm",
    "power_hp": "hp", "torque_nm": "Nm", "trunk_liters": "l",
    "seats": "count", "zero_to_100_kmh_sec": "s", "years_sold": "year",
}

#: Record fields that state one of the CLOSED identity dimensions. A claim only
#: ever states a dimension the record itself stated; nothing is invented, and a
#: dimension only ever narrows a comparison.
V1_IDENTITY_FIELDS: Mapping[str, str] = {
    "body_type": "body_style", "drivetrain": "drivetrain", "engine": "engine",
    "generation_or_series": "generation", "transmission": "transmission",
    "variant_or_generation": "generation",
}

#: Every verdict reason this module can produce, with its safe message. Closed,
#: bounded and backend-owned: no model prose, no URL and no quoted source text
#: can occupy a durable verdict reason.
V1_VERDICT_REASONS: Mapping[str, str] = {
    "V1_EVIDENCE_LOCATED_MATCH":
        "the value is durably located in the record it was read from",
    "V1_EVIDENCE_VALUE_CONTRADICTED":
        "two records of this run state different values for that field",
    "V1_EVIDENCE_NO_ISRAEL_MARKET_SOURCE":
        "no Israeli-market source supports that value",
    "V1_EVIDENCE_MODEL_FLAGGED":
        "the verifier left that model for review",
    "V1_EVIDENCE_UNSUPPORTED":
        "no durable evidence was captured for that value",
    "V1_EVIDENCE_VERDICT_NOT_DURABLE":
        "the verdict for that value could not be made durable",
    "V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE":
        "the current verification state of that value could not be read",
    "V1_EVIDENCE_CURRENT_STATE_DRIFT":
        "the current verdict for that value is not the one just settled",
    "V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED":
        "the current verdict does not support that value",
}

#: The accepted verdict's reason. Named once so nothing can spell it twice.
V1_ACCEPTED_REASON = "V1_EVIDENCE_LOCATED_MATCH"

#: The model-verifier statuses that are a flag against a model. `partial` is
#: included: it is the status `merge_model_data` also treats as outstanding.
V1_FLAGGED_STATUSES = frozenset({"needs_review", "partial", "rejected"})


class V1EvidenceError(ValueError):
    """A V1 evidence failure carrying ONLY a static, code-owned reason."""


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def model_key(name: Any) -> str:
    """The folded identity of one canonical model name.

    `normalize_field_key` is the shared formatting-only normalization the
    evidence contracts use everywhere else, so "RAV4 Hybrid" and "rav4
    hybrid " are one model here for exactly the reason they are one scope
    there.
    """
    return normalize_field_key(_text(name) or "")


def entity_key_for(manufacturer: Any, name: Any) -> str:
    """The durable entity a V1 claim is about. Derived, never model-authored."""
    folded = model_key(name)
    if not folded:
        raise V1EvidenceError("V1_EVIDENCE_ENTITY_UNKNOWN")
    return f"{V1_EVIDENCE_TOOL}:{normalize_field_key(_text(manufacturer) or '')}:{folded}"[:200]


def record_id_for(agent: str, entity: str, index: int) -> str:
    """A bounded, server-derived identifier for ONE observed record.

    A locator record id is a LITERAL identifier (`^[A-Za-z0-9_][A-Za-z0-9_.:@-]*$`),
    and a model name is free text in any script, so the identity is hashed
    rather than spelled: the digest is over the agent, the entity and the
    record's position in that agent's own output, which is what distinguishes
    two records the same agent returned for one model.
    """
    digest = hashlib.sha256(f"{agent}|{entity}|{index}".encode()).hexdigest()[:32]
    return f"v1_{digest}"


def _scalar_fields(item: Mapping[str, Any], allowed: Sequence[str]) -> dict[str, Any]:
    """The allowlisted SCALAR fields a record actually states.

    A list, a mapping and an empty string are not located facts: a
    deterministic projection can only quote a scalar, so they are left out
    rather than stringified into something that looks like evidence.
    """
    stated: dict[str, Any] = {}
    for name in allowed:
        value = item.get(name)
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        stated[name] = value.strip() if isinstance(value, str) else value
    return stated


def _identity_of(item: Mapping[str, Any]) -> dict[str, str]:
    """The closed identity dimensions the record itself stated."""
    identity: dict[str, str] = {}
    for name, dimension in V1_IDENTITY_FIELDS.items():
        if dimension not in IDENTITY_DIMENSIONS:
            continue
        text = _text(item.get(name))
        if text is None or len(text) > MAX_IDENTITY_DIMENSION_CHARS:
            continue
        identity.setdefault(dimension, text)
    return identity


def _unit_for(field_key: str, value: Any) -> str | None:
    """The unit of a NUMERIC value, and nothing for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return V1_FIELD_UNITS.get(field_key)


#: The durable bound on ONE source URL, mirrored from
#: `VersionedEvidenceSource.url`. A model-supplied string past it is DROPPED
#: rather than truncated: half a URL is not a source, and letting it fail the
#: whole contract would cost the record every field it states.
MAX_SOURCE_URL_CHARS = 1000


def _source_urls(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("sources")
    if not isinstance(raw, list):
        return []
    return [url for url in (_text(entry) for entry in raw)
            if url and len(url) <= MAX_SOURCE_URL_CHARS]


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    text = url if "://" in url else f"https://{url}"
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        host = ""
    return (host or "unknown").lower()[:200]


@dataclass(frozen=True)
class V1EvidenceRecord:
    """ONE observed technical record, and the evidence it can support.

    `record` is the exact scalar projection the fragments and facts are read
    from, and `version` is a content hash OF THAT PROJECTION -- so evidence
    acquired from a record that later says something different is different
    evidence, and can never merge onto the old rows.
    """

    agent: str
    entity_key: str
    model_name: str
    record_id: str
    record: Mapping[str, Any]
    identity: Mapping[str, str]
    urls: tuple[str, ...]
    source_class: str
    israel_market_evidence: bool
    confidence: float

    @property
    def version(self) -> SourceVersion:
        return SourceVersion(kind="content_sha256",
                             identifier=hashlib.sha256(
                                 canonical_json(dict(self.record)).encode()).hexdigest())


def observed_records(*, manufacturer: str, technical: Mapping[str, Any]) -> tuple[V1EvidenceRecord, ...]:
    """Read one run's technical phase into bounded, identified records.

    Deterministic and total: agents in name order, their items in the order the
    phase produced them, and a record that states no usable scalar field simply
    produces nothing -- which is the "a URL is not evidence" case. A record
    carrying only `sources` is exactly that.
    """
    records: list[V1EvidenceRecord] = []
    for agent in sorted(technical or {}):
        parsed = (technical or {}).get(agent)
        allowed = V1_EVIDENCE_FIELDS.get(agent)
        if not isinstance(parsed, Mapping) or not allowed:
            continue
        items = parsed.get("items")
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            name = _text(item.get("model")) or _text(item.get("canonical_model_name"))
            stated = _scalar_fields(item, allowed)
            if name is None or not stated:
                continue
            try:
                entity = entity_key_for(manufacturer, name)
            except V1EvidenceError:
                continue
            urls = _source_urls(item)
            policy = evaluate_sources(urls, manufacturer)
            records.append(V1EvidenceRecord(
                agent=agent, entity_key=entity, model_name=name,
                record_id=record_id_for(agent, entity, index),
                record={"model": name, **stated}, identity=_identity_of(item),
                urls=tuple(urls), source_class=str(policy["best_source_class"]),
                israel_market_evidence=bool(policy["israel_market_evidence"]),
                confidence=_confidence_of(item)))
    return tuple(records)


def _confidence_of(item: Mapping[str, Any]) -> float:
    """The record's stated confidence, as the bounded number a claim carries."""
    return {"high": 0.9, "medium": 0.6, "low": 0.3}.get(
        str(item.get("confidence") or "").strip().lower(), 0.5)


def _located(record: V1EvidenceRecord, name: str, *, market: str, period: str,
             fragment_index: int) -> tuple[Any, StructuredEvidenceFact]:
    """The fragment and the fact ONE field of ONE record contributes.

    Both are read from the record's own scalar projection at the same exact
    locator, which is what ties the value to a place rather than to a claim
    that it was seen somewhere.
    """
    locator = record_field_locator(record.record_id, (name,))
    fragment = structured_projection(record=dict(record.record), fields=["model", name],
                                     locator=locator, fragment_index=fragment_index)
    fact = StructuredEvidenceFact(
        entity_key=record.entity_key, field_key=name, value=record.record[name],
        unit=_unit_for(name, record.record[name]),
        time_scope={"period": str(period)[:64]}, geography=str(market)[:200],
        market=str(market)[:200], identity=dict(record.identity), locator=locator)
    return fragment, fact


def _buildable(record: V1EvidenceRecord, name: str, *, market: str, period: str) -> bool:
    """Whether this field can be evidenced at all, asked before it is placed."""
    try:
        _located(record, name, market=market, period=period, fragment_index=0)
    except (EvidenceContractError, ValueError):
        return False
    return True


def evidence_bundles(record: V1EvidenceRecord, *, market: str, period: str
                     ) -> tuple[EvidenceBundle, ...]:
    """Every bundle ONE observed record contributes, in field order.

    A bundle is bounded to `MAX_FRAGMENTS_PER_SOURCE` fragments, so a record
    stating more fields than that becomes SEVERAL bundles -- each naming, in
    its own `query`, exactly the fields it was read for. That is what keeps the
    bundles' source identities distinct instead of collapsing onto one row and
    losing evidence: a source here is "this record, read for these fields".

    The URL is source METADATA. It appears on the source row, it is never a
    fragment, and it never supports a claim: the fragments are deterministic
    projections of the record the server observed, at exact locators.
    """
    stated = sorted(name for name in record.record if name != "model")
    # ONE field the contracts refuse -- an over-long projection, a value past
    # the durable bound -- costs that field and nothing else. Dropping the
    # whole record would let one unbounded string erase the evidence for
    # everything beside it.
    fields = [name for name in stated
              if _buildable(record, name, market=market, period=period)]
    if not fields:
        return ()
    url = record.urls[0] if record.urls else ""
    groups = [fields[start:start + MAX_FRAGMENTS_PER_SOURCE]
              for start in range(0, len(fields), MAX_FRAGMENTS_PER_SOURCE)]
    bundles: list[EvidenceBundle] = []
    for group in groups[:MAX_FACTS_PER_BUNDLE]:
        fragments, facts = [], []
        for index, name in enumerate(group):
            fragment, fact = _located(record, name, market=market, period=period,
                                      fragment_index=index)
            fragments.append(fragment)
            facts.append(fact)
        source = VersionedEvidenceSource(
            agent=record.agent,
            # A record whose model named no URL still has a source ROW -- the
            # observed record itself -- and it is marked as exactly that. What
            # it does not have is Israeli-market standing, which rule 3 reads
            # from the deterministic classification rather than from this text.
            url=url or f"{V1_EVIDENCE_TOOL}://{record.record_id}",
            title=record.model_name[:400],
            domain=_domain_of(url) if url else V1_EVIDENCE_TOOL,
            source_type="model_reported" if url else "observed_record",
            source_strength=record.source_class, query=f"{market} {period} " + ",".join(group),
            tool_operation=V1_TOOL_OPERATION, version=record.version,
            confidence=record.confidence)
        bundles.append(build_evidence_bundle(source=source,
                                             locator_scope=(record.record_id,),
                                             facts=facts, fragments=fragments))
    return tuple(bundles)


@dataclass(frozen=True)
class FieldClaim:
    """One durable (or would-be durable) V1 claim, with what supports it."""

    entity_key: str
    field_key: str
    value: Any
    unit: str | None
    identity: tuple[tuple[str, str], ...]
    agent: str
    model_name: str
    israel_market_evidence: bool
    locator: str
    claim_id: str | None = None
    source_id: str | None = None
    fragment_id: str | None = None
    content_hash: str | None = None

    @property
    def scope(self) -> tuple[Any, ...]:
        """The identity two claims must share before they can contradict."""
        return (self.entity_key, self.field_key, self.identity)

    @property
    def quantity(self) -> Any:
        return value_identity(self.value, self.unit)

    @property
    def durable(self) -> bool:
        """Whether this claim has the durable support a verdict may cite."""
        return bool(self.claim_id and self.fragment_id and self.content_hash
                    and self.source_id)


def contradicted_scopes(claims: Sequence[FieldClaim]) -> frozenset[tuple[Any, ...]]:
    """Scopes where this run's own records state two different quantities.

    The merge picked the highest-confidence record and moved on. A
    contradiction is not a ranking problem: two records of one run stating
    different values for one field in one identity scope have not verified
    anything, and both sides are refused rather than one being chosen.
    """
    seen: dict[tuple[Any, ...], set[Any]] = {}
    for claim in claims:
        seen.setdefault(claim.scope, set()).add(claim.quantity)
    return frozenset(scope for scope, values in seen.items() if len(values) > 1)


def decide_verdict(claim: FieldClaim, *, contradicted: frozenset[tuple[Any, ...]],
                   flagged_models: frozenset[str], israel_required: bool) -> tuple[str, str]:
    """The `(verdict, reason)` ONE claim gets. Pure, total, deterministic.

    Ordered worst-first so a field that fails several rules is reported by the
    strongest thing wrong with it, and so no rule can be skipped by another
    rule's answer.
    """
    if not claim.durable:
        return ("needs_review", "V1_EVIDENCE_UNSUPPORTED")
    if claim.scope in contradicted:
        return ("rejected", "V1_EVIDENCE_VALUE_CONTRADICTED")
    if israel_required and not claim.israel_market_evidence:
        return ("needs_review", "V1_EVIDENCE_NO_ISRAEL_MARKET_SOURCE")
    if model_key(claim.model_name) in flagged_models:
        return ("needs_review", "V1_EVIDENCE_MODEL_FLAGGED")
    return ("verified", V1_ACCEPTED_REASON)


def flagged_model_keys(verifier: Any) -> frozenset[str]:
    """The models the MODEL verifier classified as anything but verified.

    Read from its output, folded to the same model identity everything else
    here uses. This is the only thing a model contributes to a verdict, and it
    can only ever take a field DOWN.
    """
    parsed = verifier.get("parsed") if isinstance(verifier, Mapping) and "parsed" in verifier \
        else verifier
    if not isinstance(parsed, Mapping):
        return frozenset()
    flagged: set[str] = set()
    for key in ("verified_models", "needs_review", "rejected_data_points"):
        for item in parsed.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            name = model_key(item.get("model") or item.get("canonical_model_name"))
            if not name:
                continue
            status = str(item.get("status") or ("needs_review" if key != "verified_models"
                                                else "")).strip().lower()
            if status in V1_FLAGGED_STATUSES:
                flagged.add(name)
    return frozenset(flagged)


@dataclass(frozen=True)
class V1EvidenceReport:
    """What the evidence says about one run, field by field.

    `verified_fields` is keyed by folded model identity, so the caller can ask
    "is this model evidenced" without re-deriving any identity of its own.
    """

    fields: tuple[tuple[str, str, str, str], ...] = ()   # (model, field, verdict, reason)
    durable_claims: int = 0
    durable_verdicts: int = 0
    recorded: bool = False
    reasons: Mapping[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    verified_fields: Mapping[str, tuple[str, ...]] = dataclass_field(default_factory=dict)

    def verified(self, name: Any) -> bool:
        """Whether that model's evidence supports calling it verified.

        BOTH halves, and the second is the strict one: at least one field is
        verified on durable evidence, and NOTHING the evidence produced about
        this model was refused. A model whose engine could not be evidenced is
        not "verified except for the engine" -- it is a model with an open
        question, which is exactly what `needs_review` is for.
        """
        key = model_key(name)
        return (bool(self.verified_fields.get(key))
                and set(self.reasons.get(key, ())) <= {V1_ACCEPTED_REASON})

    def rejected(self, name: Any) -> bool:
        """Whether any field of that model was contradicted outright."""
        return "V1_EVIDENCE_VALUE_CONTRADICTED" in self.reasons.get(model_key(name), ())

    def as_event(self) -> dict[str, Any]:
        """The bounded, browser-safe shape a run event carries.

        Counts and static codes only: no value, no URL, no model text and no
        SQL message can reach a run event through here.
        """
        counted: dict[str, int] = {}
        for _model, _field, verdict, _reason in self.fields:
            counted[verdict] = counted.get(verdict, 0) + 1
        return {"models": len(self.reasons), "fields": len(self.fields),
                "verdicts": counted, "durable_claims": self.durable_claims,
                "durable_verdicts": self.durable_verdicts, "recorded": self.recorded}


class V1EvidenceAuthority:
    """The trusted server path from V1's records to durable verified facts.

    Constructed by wiring that already holds the run's Evidence Board (and
    therefore its worker lease), exactly like every other evidence writer in
    this repository: every write goes through the same lease-guarded,
    idempotent RPCs, and this object holds no repository handle of its own, no
    credential and no model client.

    `board=None` is a complete, supported configuration: the evidence is still
    built, validated and decided, and only the durable write is absent.
    """

    def __init__(self, *, board: Any = None, repository: Any = None,
                 task_key: str = V1_TASK_KEY) -> None:
        self._board = board
        # The repository is handed in rather than reached for through the
        # board: the current-verdict read is a READ the authority performs in
        # its own right, and a private attribute of another object is not an
        # interface.
        self._repository = repository
        self._task_key = task_key

    @property
    def board(self) -> Any:
        return self._board

    def record(self, *, manufacturer: str, market: str, period: str,
               technical: Mapping[str, Any], verifier: Any) -> V1EvidenceReport:
        """Build, persist and decide one run's V1 evidence. Never raises.

        An evidence failure is never allowed to fail a research run: a record
        the contracts refuse contributes NO evidence, which means its fields
        cannot be reported as verified -- the safe direction. A durable write
        that fails is the same: the decision still stands, and the run's own
        result is untouched.
        """
        israel_required = market_requires_israel_policy(market)
        flagged = flagged_model_keys(verifier)
        claims: list[FieldClaim] = []
        recorded = False
        for record in observed_records(manufacturer=manufacturer, technical=technical):
            for bundle in self._bundles(record, market=market, period=period):
                persisted, wrote = self._persist(bundle, record)
                claims.extend(persisted)
                recorded = recorded or wrote
        return self._decide(claims, contradicted=contradicted_scopes(claims),
                            flagged_models=flagged, israel_required=israel_required,
                            recorded=recorded)

    # --- building ------------------------------------------------------------

    @staticmethod
    def _bundles(record: V1EvidenceRecord, *, market: str,
                 period: str) -> tuple[EvidenceBundle, ...]:
        try:
            return evidence_bundles(record, market=market, period=period)
        except (EvidenceContractError, ValueError):
            # A record the evidence contracts refuse is not evidence. It is
            # dropped, and its fields stay unverifiable.
            return ()

    # --- persistence ---------------------------------------------------------

    def _persist(self, bundle: EvidenceBundle,
                 record: V1EvidenceRecord) -> tuple[list[FieldClaim], bool]:
        """Write one bundle, or fall back to the same claims without ids.

        The claims are the SAME either way -- the field, the value, the unit,
        the identity and the locator all come from the bundle. What the durable
        write adds is the ids a verdict's support links need, so a claim with
        no durable row is `needs_review` at worst and never `verified`.
        """
        facts = {fact.locator.locator_key: fact for fact in bundle.facts}
        if self._board is None:
            return ([self._claim(record, fact) for fact in bundle.facts], False)
        try:
            acquired = self._board.record_evidence_bundle(bundle, task_key=self._task_key)
        except AppError as failure:
            if failure.code in LEASE_FAILURE_CODES:
                # A stale worker is an INFRASTRUCTURE outcome, never an answer
                # about the evidence: it must reach the worker's own lease
                # handling exactly as every other guarded write's does.
                raise
            return ([self._claim(record, fact) for fact in bundle.facts], False)
        except Exception:
            # An evidence write must never fail a research run. Without the
            # durable row the field simply cannot be verified.
            return ([self._claim(record, fact) for fact in bundle.facts], False)
        fragments = {str(row.get("locator_key")): row for row in acquired.fragments}
        persisted = []
        for row in acquired.claims:
            fact = facts.get(str(row.get("evidence_locator")))
            fragment = fragments.get(str(row.get("evidence_locator")))
            if fact is None or fragment is None:
                continue
            persisted.append(self._claim(record, fact, claim_row=row, fragment_row=fragment))
        return (persisted, True)

    @staticmethod
    def _claim(record: V1EvidenceRecord, fact: StructuredEvidenceFact, *,
               claim_row: Mapping[str, Any] | None = None,
               fragment_row: Mapping[str, Any] | None = None) -> FieldClaim:
        return FieldClaim(
            entity_key=fact.entity_key, field_key=fact.field_key, value=fact.value,
            unit=fact.unit, identity=tuple(sorted(dict(fact.identity).items())),
            agent=record.agent, model_name=record.model_name,
            israel_market_evidence=record.israel_market_evidence,
            locator=fact.locator.locator_key,
            claim_id=None if claim_row is None else str(claim_row.get("id")),
            source_id=None if claim_row is None else str(claim_row.get("source_id")),
            fragment_id=None if fragment_row is None else str(fragment_row.get("id")),
            content_hash=None if fragment_row is None else str(fragment_row.get("content_hash")))

    # --- deciding ------------------------------------------------------------

    def _decide(self, claims: Sequence[FieldClaim], *,
                contradicted: frozenset[tuple[Any, ...]], flagged_models: frozenset[str],
                israel_required: bool, recorded: bool) -> V1EvidenceReport:
        decided: list[tuple[str, str, str, str]] = []
        reasons: dict[str, list[str]] = {}
        verified: dict[str, list[str]] = {}
        durable_claims = durable_verdicts = 0
        for claim in claims:
            verdict, reason = decide_verdict(claim, contradicted=contradicted,
                                             flagged_models=flagged_models,
                                             israel_required=israel_required)
            if claim.durable:
                durable_claims += 1
                verdict, reason, settled = self._durable_answer(claim, verdict, reason)
                durable_verdicts += 1 if settled else 0
            key = model_key(claim.model_name)
            decided.append((claim.model_name, claim.field_key, verdict, reason))
            reasons.setdefault(key, [])
            if reason not in reasons[key]:
                reasons[key].append(reason)
            if verdict == "verified":
                verified.setdefault(key, []).append(claim.field_key)
        return V1EvidenceReport(
            fields=tuple(decided), durable_claims=durable_claims,
            durable_verdicts=durable_verdicts, recorded=recorded,
            reasons={key: tuple(value) for key, value in reasons.items()},
            verified_fields={key: tuple(sorted(set(value)))
                             for key, value in verified.items()})

    def _durable_answer(self, claim: FieldClaim, verdict: str,
                        reason: str) -> tuple[str, str, bool]:
        """What ONE claim's verdict is once durability has been PROVEN.

        This is where `verified` stops being a local decision. Three things
        have to be true of it, and every one of them is a way it used to fail
        OPEN:

        1.  the verdict AND its support links are durable. A write that failed
            left the local `verified` standing, so a field could be reported
            verified while the row that verifies it does not exist;
        2.  the authoritative current-state read SUCCEEDS. A read that failed,
            or a backend that does not offer one, fell back to the local
            decision -- which is the one answer it may never give: "we could
            not tell" is not "still verified";
        3.  the state is `supported` AND names EXACTLY the verdict just
            settled. A supported state naming another row is history or
            current-state drift; the field being written rests on the verdict
            this pass produced and on nothing else.

        Anything else demotes the field to `needs_review` with a static reason
        saying which of the three failed. The run itself carries on -- an
        evidence failure is never allowed to fail a research run -- and a LOST
        LEASE still escapes as infrastructure.

        A decision that is NOT `verified` is already a refusal, so it needs no
        proof: it keeps its own verdict and reason whether or not the durable
        write succeeded. `settled` is reported separately so the report counts
        durable verdicts rather than accepted ones.
        """
        row = self._settle(claim, verdict, reason)
        settled = row is not None
        if verdict != "verified":
            return (verdict, reason, settled)
        if not settled:
            return ("needs_review", "V1_EVIDENCE_VERDICT_NOT_DURABLE", False)
        state = self._current_state(claim)
        if state is None:
            return ("needs_review", "V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE", True)
        if state.authorizes(row.get("id")):
            return (verdict, reason, True)
        if state.supported:
            return ("needs_review", "V1_EVIDENCE_CURRENT_STATE_DRIFT", True)
        return ("needs_review", "V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED", True)

    def _settle(self, claim: FieldClaim, verdict: str,
                reason: str) -> Mapping[str, Any] | None:
        """Make ONE decision durable, with the exact fragment behind it.

        Returns the durable ROW -- the caller needs its id to ask whether that
        exact verdict is the current one -- or `None` when nothing durable was
        written. A row that cannot state its own id is not a durable row.

        The verdict and its support links are ONE guarded write, so a support
        link that could not be stored is a failed verdict here rather than a
        verdict with nothing behind it. The durable state check above closes
        the remaining case from the other side: a verdict whose support is not
        durable resolves to `unsupported`, which is not `supported`.
        """
        if self._board is None or not claim.durable:
            return None
        support = [SupportLink(source_id=str(claim.source_id),
                               content_hash=str(claim.content_hash),
                               fragment_id=str(claim.fragment_id), locator=claim.locator)]
        try:
            row = self._board.record_verification_verdict(VerificationVerdict(
                claim_id=str(claim.claim_id), verdict=verdict, reason=reason,
                mode=V1_VERIFICATION_MODE, contract_version=VERIFIER_CONTRACT_VERSION,
                support=support))
        except AppError as failure:
            if failure.code in LEASE_FAILURE_CODES:
                raise
            return None
        except Exception:
            return None
        if not isinstance(row, Mapping) or not _text(row.get("id")):
            return None
        return row

    def _current_state(self, claim: FieldClaim) -> CurrentVerdict | None:
        """The AUTHORITATIVE current state of one claim, or `None`.

        `None` means the question could not be answered -- no repository, no
        such read, no lease, a failed read, a malformed row, or no row for this
        claim -- and it is never the same thing as an answer. The caller fails
        closed on it.

        A LOST LEASE is the one failure that escapes: this worker is no longer
        the run's writer, and that has to reach the worker's lease handling
        rather than become a statement about the evidence.
        """
        lease = getattr(self._board, "lease", None)
        read = getattr(self._repository, "claim_current_verdict_states", None)
        if not callable(read) or lease is None:
            return None
        try:
            rows = read(lease.run_id, [str(claim.claim_id)], limit=1)
        except AppError as failure:
            if failure.code in LEASE_FAILURE_CODES:
                raise
            return None
        except Exception:
            return None
        if not isinstance(rows, (list, tuple)):
            return None
        for row in rows:
            try:
                state = parse_current_verdict(row)
            except CurrentVerdictError:
                continue
            if state.claim_id == str(claim.claim_id):
                return state
        return None


def apply_evidence_authority(verifier_data: Any, report: V1EvidenceReport) -> Any:
    """Hold the MODEL's classification to what the evidence actually supports.

    In place and downwards only, on the verifier document the final builder
    reads, so the deterministic merge that builds `verification_status` is
    untouched: a model the evidence supports keeps exactly the status the
    verifier gave it, and one it does not is moved to `needs_review` (or
    `rejected`, when this run's own records contradicted it) with a bounded
    static note saying which rule refused it.

    This is the same shape `apply_israel_source_policy` already has, and for
    the same reason: a deterministic server rule may lower a model's claim
    about its own output, and may never raise one.
    """
    parsed = verifier_data.get("parsed") if isinstance(verifier_data, Mapping) \
        and "parsed" in verifier_data else verifier_data
    if not isinstance(parsed, dict) or not isinstance(parsed.get("verified_models"), list):
        return verifier_data
    for item in parsed["verified_models"]:
        if not isinstance(item, dict) or str(item.get("status")) != "verified":
            continue
        name = item.get("model") or item.get("canonical_model_name")
        if report.verified(name):
            continue
        item["status"] = "rejected" if report.rejected(name) else "needs_review"
        if str(item.get("confidence") or "").lower() == "high":
            item["confidence"] = "medium"
        note = V1_VERDICT_REASONS.get(
            next((reason for reason in report.reasons.get(model_key(name), ())
                  if reason != V1_ACCEPTED_REASON), "V1_EVIDENCE_UNSUPPORTED"),
            V1_VERDICT_REASONS["V1_EVIDENCE_UNSUPPORTED"])
        issues = item.get("issues")
        item["issues"] = ([str(issue)[:120] for issue in issues[:1]]
                          if isinstance(issues, list) else []) + [note[:120]]
    return verifier_data


__all__ = ["MAX_SOURCE_URL_CHARS", "V1_ACCEPTED_REASON", "V1_EVIDENCE_FIELDS",
           "V1_EVIDENCE_OPERATION",
           "V1_EVIDENCE_TOOL", "V1_FIELD_UNITS", "V1_FLAGGED_STATUSES",
           "V1_IDENTITY_FIELDS", "V1_TASK_KEY", "V1_TOOL_OPERATION",
           "V1_VERDICT_REASONS", "V1_VERIFICATION_MODE", "FieldClaim", "V1EvidenceAuthority",
           "V1EvidenceError", "V1EvidenceRecord", "V1EvidenceReport",
           "apply_evidence_authority", "contradicted_scopes", "decide_verdict",
           "entity_key_for", "evidence_bundles", "flagged_model_keys", "model_key",
           "observed_records", "record_id_for"]
