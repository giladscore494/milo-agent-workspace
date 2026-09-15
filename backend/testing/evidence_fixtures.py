"""ONE definition of a valid R3/R4 evidence chain, for both backends.

Why this module exists
----------------------

The catalog evidence-link tests cite a source, a claim and a verdict. Whether
those citations mean anything depends entirely on whether the cited rows are
rows PostgreSQL would actually have produced -- and an earlier round of this
branch built them by hand in the memory tests, missing `evidence_key`,
`task_key`, `fragment_type`, the canonical scope identity, the unit a numeric
located fact requires, and the verifier contract version. Every one of those
payloads is refused by the guarded RPCs, so the memory tests were citing
evidence that could not exist.

The chain below is defined once and submitted through BOTH implementations:

    versioned source -> focused fragment -> located claim -> verified verdict
                                                             with durable support

*   `tests/test_migrations_postgres.py` submits these exact payloads through
    `upsert_source_guarded`, `record_evidence_fragment_guarded`,
    `create_claim_with_source_guarded` and `record_claim_verdict_guarded`
    against real PostgreSQL;
*   `tests/test_catalog_persistence.py` submits the same payloads through
    `MemoryRepository`.

A shape that drifts out of what PostgreSQL accepts therefore breaks the
PostgreSQL test, not only the memory one.

Nothing is invented here. Every vocabulary term and every version constant is
imported from the module that owns it -- `evidence_contracts` for locators,
`fragments` for the content hash, `normalization` for the canonical scope
identity, `support` for the verifier contract version -- so there is no second
copy of a contract to drift.

Test support only: this module is imported by tests and by nothing in the
production path. It builds dicts; it opens no connection and calls nothing.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.engines.swarm_v2.evidence_contracts import record_field_locator
from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.normalization import (SCOPE_NORMALIZATION_VERSION,
                                                    canonical_scope_hash, canonical_scope_key)
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION

#: The task every row of one chain belongs to. `upsert_source_guarded` and
#: `record_evidence_fragment_guarded` both check this: a fragment may only be
#: attributed to the task that captured its source.
TASK_KEY = "task"

#: The source version this chain is pinned to. Both halves are required --
#: `upsert_source_guarded` refuses a kind without an identifier and vice
#: versa -- and a located claim additionally requires its source to be
#: versioned at all.
SOURCE_VERSION_KIND = "dataset_version"
SOURCE_VERSION_ID = "2026.08.1"

#: The fact this chain is about, and the exact location it was read at. The
#: locator is built by the contract module rather than written out, so a change
#: to the locator encoding cannot leave a stale literal behind here.
ENTITY_KEY = "entity"
FIELD_KEY = "engine_displacement_cc"
FIELD_VALUE = 1798
FIELD_UNIT = "cc"
MARKET = "IL"
TIME_SCOPE: Mapping[str, Any] = {"as_of": "2026-08"}
LOCATOR = record_field_locator("rec-1", (FIELD_KEY,)).locator_key

#: `r3_focus_valid` pairs a fragment type with a locator KIND: a
#: `record_field` locator is a `structured_projection` and nothing else.
FRAGMENT_TYPE = "structured_projection"
FRAGMENT_TEXT = "model_name=Fixture Hatch; model_year=2020; engine_displacement_cc=1798"
FRAGMENT_HASH = fragment_content_hash(FRAGMENT_TEXT)

#: The verification contract this chain's verdict was decided under, and the
#: mode it was decided in. Imported, never restated.
VERIFIER_CONTRACT = VERIFIER_CONTRACT_VERSION
VERIFICATION_MODE = "deterministic_structured"
VERDICT_REASON = "R4_STRUCTURED_MATCH"


def source_payload(key: str, *, task: str = TASK_KEY,
                   version_kind: str | None = SOURCE_VERSION_KIND,
                   version_id: str | None = SOURCE_VERSION_ID) -> dict[str, Any]:
    """One versioned source. `evidence_key` and `task_key` are mandatory."""
    return {"agent": "agent", "url": f"https://example.test/{key}", "title": "title",
            "domain": "example.test", "source_type": "primary",
            "source_strength": "strong", "query": "query", "tool_operation": "search",
            "evidence_key": key, "task_key": task,
            "source_version_kind": version_kind, "source_version_id": version_id}


def fragment_payload(key: str, source_id: Any, *, task: str = TASK_KEY,
                     text: str = FRAGMENT_TEXT, locator: str | None = LOCATOR,
                     fragment_type: str | None = FRAGMENT_TYPE,
                     index: int = 0) -> dict[str, Any]:
    """One FOCUSED fragment: a type and a locator that agree, or neither."""
    return {"source_id": str(source_id), "task_key": task, "evidence_key": key,
            "fragment_text": text, "content_hash": fragment_content_hash(text),
            "fragment_index": index, "fragment_type": fragment_type,
            "locator_key": locator}


def claim_payload(key: str, source_id: Any, *, task: str = TASK_KEY,
                  value: Any = FIELD_VALUE, unit: str | None = FIELD_UNIT,
                  locator: str | None = LOCATOR, field: str = FIELD_KEY,
                  entity: str = ENTITY_KEY, market: str | None = MARKET,
                  geography: str | None = None,
                  time_scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One LOCATED claim, carrying the trusted canonical scope identity.

    The scope hash is computed by the production normalization module, which is
    what `create_claim_with_source_guarded` validates against -- a hand-written
    hash would be refused as an invalid canonical scope state.
    """
    scope_time = dict(TIME_SCOPE if time_scope is None else time_scope)
    scope = canonical_scope_key(entity=entity, field=field, geography=geography,
                                market=market, time_scope=scope_time)
    return {"entity_key": entity, "field_key": field, "value": value, "unit": unit,
            "time_scope": scope_time, "market": market, "geography": geography,
            "source_id": str(source_id), "source_strength": "strong", "confidence": .9,
            "agent": "agent", "canonical_scope_hash": canonical_scope_hash(scope),
            "scope_normalization_version": SCOPE_NORMALIZATION_VERSION,
            "evidence_locator": locator, "evidence_key": key, "task_key": task}


def support_link(fragment_id: Any, *, text: str = FRAGMENT_TEXT,
                 locator: str | None = LOCATOR) -> dict[str, Any]:
    """One durable support link, pinned to the fragment's own hash and locator."""
    return {"fragment_id": str(fragment_id), "content_hash": fragment_content_hash(text),
            "locator_key": locator}


def verdict_payload(key: str, claim_id: Any, *, verdict: str = "verified",
                    support: list[dict[str, Any]] | None = None,
                    mode: str = VERIFICATION_MODE,
                    contract: str = VERIFIER_CONTRACT,
                    reason: str = VERDICT_REASON) -> dict[str, Any]:
    """One verdict. A `verified` one must cite durable evidence."""
    return {"claim_id": str(claim_id), "verdict": verdict, "reason": reason,
            "verification_mode": mode, "verifier_contract_version": contract,
            "support": list(support or []), "evidence_key": key}


#: The `support` KEY is absent from the payload entirely. Distinct from every
#: JSON value, `null` included -- which is the whole point of the matrix below.
OMITTED_SUPPORT = object()

#: The message BOTH backends raise for a `support` that is not a JSON array.
SUPPORT_TYPE_ERROR = "invalid claim verdict: support must be an array"

#: The SHARED support-value matrix: (label, value at `support`, accepted?).
#:
#: PostgreSQL distinguishes an ABSENT key from a supplied one:
#: `p_verdict->'support'` is SQL NULL only when the key is missing, so
#: `coalesce(..., '[]'::jsonb)` substitutes an empty array there -- while a
#: supplied JSON `null` is `'null'::jsonb`, which is NOT SQL NULL and so
#: reaches `jsonb_typeof(v_support) <> 'array'` and is refused, as are an
#: object, a string, a number and a boolean.
#:
#: Every case is run against `MemoryRepository.record_claim_verdict` in
#: `tests/test_catalog_persistence.py` and against the real
#: `record_claim_verdict_guarded` in `tests/test_migrations_postgres.py`.
SUPPORT_VALUE_CASES = (
    ("missing", OMITTED_SUPPORT, True),
    ("empty_array", [], True),
    ("null", None, False),
    ("object", {}, False),
    ("string", "", False),
    ("number", 0, False),
    ("boolean", False, False),
)

#: Just the values that must be refused, for the replay half of the matrix.
REJECTED_SUPPORT_CASES = tuple((label, value)
                               for label, value, accepted in SUPPORT_VALUE_CASES
                               if not accepted)


def verdict_payload_with_support(key: str, claim_id: Any, value: Any, *,
                                 verdict: str = "needs_review",
                                 **kwargs: Any) -> dict[str, Any]:
    """One verdict payload whose `support` is EXACTLY `value` -- or absent.

    `verdict_payload` coerces its `support` argument with `list(support or [])`,
    which is the right thing for a well-formed chain and exactly wrong for
    probing what each backend does with a malformed one. This places the raw
    value, or removes the key.

    The default verdict is `needs_review` deliberately: an empty support set is
    legitimate for it, so the "an accepted verdict must cite durable evidence"
    rule cannot mask what the type check does or does not do.
    """
    payload = verdict_payload(key, claim_id, verdict=verdict, support=[], **kwargs)
    if value is OMITTED_SUPPORT:
        payload.pop("support")
    else:
        payload["support"] = value
    return payload


def chain_payloads(label: str) -> dict[str, dict[str, Any]]:
    """The four payloads of one complete chain, keyed by their stable identities.

    `support` is left out of the verdict here because it names the fragment's
    database id, which only exists once the fragment has been written. Callers
    write the source and the fragment, then pass `support_link(fragment_id)`.
    """
    return {"source": source_payload(f"{label}-source"),
            "fragment": fragment_payload(f"{label}-fragment", source_id="<pending>"),
            "claim": claim_payload(f"{label}-claim", source_id="<pending>"),
            "verdict": verdict_payload(f"{label}-verdict", claim_id="<pending>")}


__all__ = ["ENTITY_KEY", "FIELD_KEY", "FIELD_UNIT", "FIELD_VALUE", "FRAGMENT_HASH",
           "FRAGMENT_TEXT", "FRAGMENT_TYPE", "LOCATOR", "MARKET", "OMITTED_SUPPORT",
           "REJECTED_SUPPORT_CASES", "SOURCE_VERSION_ID", "SOURCE_VERSION_KIND",
           "SUPPORT_TYPE_ERROR", "SUPPORT_VALUE_CASES", "TASK_KEY", "TIME_SCOPE",
           "VERDICT_REASON", "VERIFICATION_MODE", "VERIFIER_CONTRACT", "chain_payloads",
           "claim_payload", "fragment_payload", "source_payload", "support_link",
           "verdict_payload", "verdict_payload_with_support"]
