"""Deterministic, domain-separated identity keys for the durable catalog.

Why these exist
---------------

Catalog PR1 let the CALLER name every durable object. A caller-chosen key has
two failure modes, and the corrective round closes both:

*   the same logical object stored twice under two names, so a replay that
    renamed itself silently duplicated instead of collapsing;
*   a key derived from anything a model emitted, which would put an
    idempotency identity -- the thing that decides whether a write is a retry
    or a new fact -- under the influence of a generated string.

Every key here is a pure function of the object's own STRUCTURAL identity: the
fields that make it the object it is, and nothing else. No timestamp, no run
id, no worker id, no counter, and nothing a model produced. The same retrieval
on two machines, in two runs, a month apart, derives the same key.

Domain separation
-----------------

Each builder hashes a distinct literal prefix together with its fields, so a
snapshot key and a record key can never collide even if their field values
coincide, and so a key states which KIND of object it identifies. The short
human prefix (`cs1.`, `cr1.`, `cc1.`, `cl1.`) keeps that visible in a log line
and in a database row without having to look the value up.

The `1` is a version. If an identity ever has to take a new field, the prefix
becomes `cs2.` and old rows keep their old keys rather than silently changing
identity under a schema they were not written for.

Pure module: hashing and string formatting only. No I/O, no clock, no
randomness, no global mutable state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

#: The key version. Bumping it changes every derived key, so it is part of the
#: prefix rather than hidden inside the digest.
KEY_VERSION = 1

#: Domain -> the short prefix its keys carry. Closed and distinct: two domains
#: never share a prefix, so a key always says what it identifies.
KEY_DOMAINS: Mapping[str, str] = {
    "catalog.snapshot": "cs",
    "catalog.raw_record": "cr",
    "catalog.candidate": "cc",
    "catalog.evidence_link": "cl",
    # Catalog PR3: the canonical catalog's own identities, and the identity of
    # ONE promotion transaction.
    "catalog.canonical_model": "cm",
    "catalog.canonical_variant": "cv",
    "catalog.promotion": "cp",
}

#: How much of the digest a key carries. 32 hex characters is 128 bits, which
#: makes an accidental collision across the whole catalog not a thing that
#: happens, while keeping the key short enough to read and to index.
_DIGEST_CHARS = 32


class CatalogKeyError(ValueError):
    """An identity that cannot be keyed, with a static, safe reason."""


def _canonical(value: Any) -> Any:
    """The canonical form of one identity component.

    Text is compared as the source wrote it -- never lowercased, never
    stripped, never normalized -- because two upstream values that differ by a
    space are two different values, and collapsing them here would merge two
    identities into one. `None` is preserved as a distinct absent marker, so
    an unstated dimension and an empty one never derive the same key.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise CatalogKeyError("a catalog identity component must be text, a number, a bool, "
                          "an object, a list or absent")


def derive_key(domain: str, **components: Any) -> str:
    """The key for one identity in one domain, or fail closed.

    The digest is taken over the domain, the version and the canonical JSON of
    the components with sorted keys, so the result depends on the VALUES and
    never on the order a caller happened to pass them in.
    """
    prefix = KEY_DOMAINS.get(domain)
    if prefix is None:
        raise CatalogKeyError("unknown catalog key domain")
    basis = json.dumps({"domain": domain, "version": KEY_VERSION,
                        "components": _canonical(components)},
                       sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{prefix}{KEY_VERSION}.{digest}"


def snapshot_key(*, source_family: str, resource_id: str, upstream_version_kind: str,
                 upstream_version: str, content_sha256: str) -> str:
    """One retrieval of one resource at one version with one content identity.

    `content_sha256` is in the identity on purpose: two retrievals of the same
    resource at the same declared version that returned DIFFERENT bytes are two
    different snapshots, and giving them one key would make the second look
    like a replay of the first.
    """
    return derive_key("catalog.snapshot", source_family=source_family,
                      resource_id=resource_id, upstream_version_kind=upstream_version_kind,
                      upstream_version=upstream_version, content_sha256=content_sha256)


def raw_record_key(*, snapshot_key: str, upstream_record_id: str) -> str:
    """One upstream row inside one snapshot.

    The payload is NOT in the identity. A snapshot is immutable, so within it
    an upstream record id names exactly one row; putting the payload in the key
    would make a corrupted re-read look like a different record instead of the
    idempotency conflict it is.
    """
    return derive_key("catalog.raw_record", snapshot_key=snapshot_key,
                      upstream_record_id=upstream_record_id)


def candidate_key(*, record_key: str, manufacturer: str, commercial_model: str,
                  model_year_start: int | None = None, model_year_end: int | None = None,
                  official_model_code: str | None = None, trim: str | None = None,
                  identity_dimensions: Mapping[str, Any] | None = None) -> str:
    """One READING of one raw record.

    `status` is deliberately absent: a candidate that moves from `ambiguous` to
    `ready_for_review` is the same candidate with a revised reading, and a key
    that changed with the status would file the revision as a second identity.
    """
    return derive_key("catalog.candidate", record_key=record_key, manufacturer=manufacturer,
                      commercial_model=commercial_model, model_year_start=model_year_start,
                      model_year_end=model_year_end, official_model_code=official_model_code,
                      trim=trim, identity_dimensions=dict(identity_dimensions or {}))


def evidence_link_key(*, candidate_key: str, source_id: str, claim_id: str,
                      verdict_id: str | None = None) -> str:
    """One piece of evidence cited for one candidate.

    The locator and the source version are NOT in the identity: both are
    derived from the cited claim and source rather than supplied, so they are
    consequences of `(source_id, claim_id)` and adding them would only let a
    caller change a key by restating a field it does not control.

    `verdict_id` IS in the identity, because a discovery link and the
    verification of the same claim are two different statements about the same
    candidate and both are worth keeping.
    """
    return derive_key("catalog.evidence_link", candidate_key=candidate_key,
                      source_id=str(source_id), claim_id=str(claim_id),
                      verdict_id=None if verdict_id is None else str(verdict_id))


def canonical_model_key(*, manufacturer: str, commercial_model: str) -> str:
    """ONE canonical model -- a marque and a commercial model, and nothing else.

    No year, no code and no trim: those distinguish VARIANTS of a model, and
    folding one into the model's identity would make `RAV4 (2024)` and
    `RAV4 (2025)` two different models.

    The text is the source's own, uncollapsed, exactly as
    `backend/catalog/keys.py` treats every other identity component: two
    manufacturer spellings the register publishes separately are two
    manufacturers here, because deciding they are one is a reconciliation
    judgement with its own evidence requirement -- not something a key builder
    may make silently.
    """
    return derive_key("catalog.canonical_model", manufacturer=manufacturer,
                      commercial_model=commercial_model)


def canonical_variant_key(*, model_key: str, model_year_start: int, model_year_end: int,
                          official_model_code: str | None = None, trim: str | None = None,
                          identity_dimensions: Mapping[str, Any] | None = None) -> str:
    """ONE canonical variant of one canonical model.

    Every stated identity component participates, so two trims of one model
    year are two canonical variants rather than one row that silently won.
    `None` is preserved distinctly by `_canonical`, so an unstated code and an
    empty one never derive the same key.

    The REVISABLE facts are in the identity on purpose: a canonical variant is
    the thing a promotion establishes, and a later revision of a field is an
    append to that variant's provenance, not a new variant. Which is why the
    key is derived from what the promotion states ONCE, at revision 1, and the
    current value of each field is then read from the append-only provenance.
    """
    return derive_key("catalog.canonical_variant", model_key=model_key,
                      model_year_start=model_year_start, model_year_end=model_year_end,
                      official_model_code=official_model_code, trim=trim,
                      identity_dimensions=dict(identity_dimensions or {}))


def promotion_key(*, candidate_key: str, variant_key: str,
                  fields: Mapping[str, Any]) -> str:
    """ONE promotion transaction: this candidate, this variant, these facts.

    The FIELD SET AND ITS VALUES are in the identity, which is what makes a
    conflicting replay detectable: the same key presented with a different set
    of promoted fields, or with the same fields at different values, is a
    different promotion wearing the same name and is refused rather than
    collapsing onto the stored one.

    The run, the worker, the attempt and the time are deliberately absent: a
    retry of the same promotion after a crash must replay onto the same
    identity, and it cannot do that if the identity moves with the attempt.
    """
    return derive_key("catalog.promotion", candidate_key=candidate_key,
                      variant_key=variant_key,
                      fields={str(name): fields[name] for name in sorted(fields)})


__all__ = ["KEY_DOMAINS", "KEY_VERSION", "CatalogKeyError", "candidate_key",
           "canonical_model_key", "canonical_variant_key", "derive_key",
           "evidence_link_key", "promotion_key", "raw_record_key", "snapshot_key"]
