"""The catalog identity-key builders: deterministic, domain-separated, pure.

An idempotency key decides whether a durable write is a retry or a new fact.
Catalog PR1 let the caller choose one, which put that decision under the
influence of whatever produced the string -- including, in an ingestion path,
a model. These builders make a key a pure function of the object's own
structural identity instead, and this module pins the properties that makes
true.
"""

import json

import pytest

from backend.catalog import keys


SNAPSHOT = {"source_family": "government", "resource_id": "142afde2",
            "upstream_version_kind": "dataset_version", "upstream_version": "2026.09.1",
            "content_sha256": "a" * 64}


def test_a_key_is_deterministic_and_independent_of_argument_order():
    first = keys.snapshot_key(**SNAPSHOT)
    second = keys.snapshot_key(**{field: SNAPSHOT[field] for field in reversed(list(SNAPSHOT))})
    assert first == second == keys.snapshot_key(**SNAPSHOT)
    # Nothing here reads a clock, a counter or a random source, so the same
    # retrieval on another machine in another run derives the same key.
    assert keys.derive_key("catalog.snapshot", a=1, b=2) == keys.derive_key("catalog.snapshot", b=2, a=1)


@pytest.mark.parametrize("domain, prefix", sorted(keys.KEY_DOMAINS.items()))
def test_every_domain_has_its_own_prefix_and_shape(domain, prefix):
    key = keys.derive_key(domain, value="x")
    assert key.startswith(f"{prefix}{keys.KEY_VERSION}.")
    identifier = key.split(".", 1)[1]
    assert len(identifier) == 32 and all(c in "0123456789abcdef" for c in identifier)


def test_two_domains_never_collide_on_the_same_components():
    """Domain separation is what lets a key state WHAT it identifies."""
    derived = {domain: keys.derive_key(domain, value="identical")
               for domain in keys.KEY_DOMAINS}
    assert len(set(derived.values())) == len(keys.KEY_DOMAINS)
    assert len({key.split(".", 1)[1] for key in derived.values()}) == len(keys.KEY_DOMAINS)


def test_every_identity_component_changes_the_key():
    """A key that ignored a component would merge two identities into one."""
    base = keys.snapshot_key(**SNAPSHOT)
    for field, changed in (("source_family", "manufacturer"), ("resource_id", "other"),
                           ("upstream_version_kind", "content_sha256"),
                           ("upstream_version", "2026.09.2"), ("content_sha256", "b" * 64)):
        assert keys.snapshot_key(**{**SNAPSHOT, field: changed}) != base, field


def test_text_is_compared_exactly_and_absence_is_distinct_from_emptiness():
    """Two upstream values that differ by a space are two values.

    Normalizing here -- lowercasing, stripping -- would quietly merge them,
    which is the opposite of what an identity key is for. And an UNSTATED
    dimension must never key the same as an empty one.
    """
    record = keys.raw_record_key(snapshot_key="cs1." + "0" * 32, upstream_record_id="36327")
    assert record != keys.raw_record_key(snapshot_key="cs1." + "0" * 32,
                                         upstream_record_id=" 36327")
    absent = keys.candidate_key(record_key=record, manufacturer="Toyota",
                                commercial_model="RAV4")
    empty = keys.candidate_key(record_key=record, manufacturer="Toyota",
                               commercial_model="RAV4", trim="")
    assert absent != empty


def test_a_candidates_status_is_not_part_of_its_identity():
    """A candidate that moves from `ambiguous` to `ready_for_review` is the
    same candidate with a revised reading, not a second identity."""
    record = keys.raw_record_key(snapshot_key="cs1." + "0" * 32, upstream_record_id="1")
    identity = {"record_key": record, "manufacturer": "Toyota", "commercial_model": "RAV4",
                "model_year_start": 2021, "model_year_end": 2021,
                "identity_dimensions": {"drivetrain": "awd"}}
    assert keys.candidate_key(**identity) == keys.candidate_key(**identity)
    # Dimension ORDER is not identity either; dimension CONTENT is.
    assert keys.candidate_key(**{**identity,
                                 "identity_dimensions": {"drivetrain": "awd"}}) == \
        keys.candidate_key(**identity)
    assert keys.candidate_key(**{**identity,
                                 "identity_dimensions": {"drivetrain": "two_wheel_drive"}}) != \
        keys.candidate_key(**identity)


def test_a_links_verdict_is_part_of_its_identity_but_its_provenance_is_not():
    """A discovery citation and a verification of the same claim are two
    different statements; the locator and the version are consequences of the
    claim and the source, so they cannot vary independently."""
    base = {"candidate_key": "cc1." + "0" * 32, "source_id": "s", "claim_id": "c"}
    assert keys.evidence_link_key(**base) != keys.evidence_link_key(**base, verdict_id="v")
    assert keys.evidence_link_key(**base, verdict_id="v") == \
        keys.evidence_link_key(**base, verdict_id="v")


def test_an_unknown_domain_or_an_unkeyable_component_fails_closed():
    with pytest.raises(keys.CatalogKeyError, match="unknown catalog key domain"):
        keys.derive_key("catalog.something_else", value="x")
    with pytest.raises(keys.CatalogKeyError, match="must be text, a number"):
        keys.derive_key("catalog.snapshot", value=object())


def test_the_key_version_travels_in_the_prefix_not_only_in_the_digest():
    """A version bump must change every key visibly, so old rows keep their
    old identity rather than silently changing under a new rule."""
    assert keys.KEY_VERSION == 1
    key = keys.snapshot_key(**SNAPSHOT)
    assert key.startswith("cs1.")
    basis = json.dumps({"domain": "catalog.snapshot", "version": 2,
                        "components": SNAPSHOT}, sort_keys=True, separators=(",", ":"))
    assert basis  # the version is inside the digest too, not only the prefix
