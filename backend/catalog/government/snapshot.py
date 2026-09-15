"""THREE digests, named apart, so none of them can be mistaken for another.

A capture produces several SHA-256 values that mean entirely different things.
Confusing them is how a system ends up believing it verified something it did
not, so each is defined exactly once, here, with the thing it is a function of:

1.  **The captured-response checksum** -- `CapturedPage.body_sha256`. The
    SHA-256 of ONE response body, byte for byte, as `data.gov.il` served it.
    It identifies a transmission and nothing else.

2.  **The page-chain digest** -- `page_chain_digest`. A single value that
    commits to the ORDERED list of captured-response checksums. Reordering,
    dropping, adding or altering any page changes it, and it stays 64
    characters however many pages a capture holds, so a capture of any size can
    state its page provenance inside the durable metadata bound.

3.  **The snapshot identity checksum** -- `snapshot_content_sha256`, stored as
    `catalog_source_snapshots.content_sha256`. The SHA-256 of a canonical,
    domain-separated manifest of WHAT WAS CAPTURED: the resource, the query, the
    page size, the reported total, the schema fingerprint and every page's
    checksum, offset, size and row count. It is deliberately NOT a page body
    digest -- a snapshot is a whole result set -- and it deliberately excludes
    the retrieval TIME, so re-reading unchanged content replays onto the same
    snapshot instead of creating a new one every time a worker runs.

And one digest that is NOT computed here at all:

4.  **The stored raw-payload digest** -- `catalog_raw_records.payload_sha256`.
    PostgreSQL derives it from the `jsonb` value it actually stores, over its
    own `jsonb::text` rendering. It is storage-local by design (see
    `backend/catalog/digest.py`), no caller may supply it, and nothing in this
    package predicts it.

The FOURTH identifier, `schema_fingerprint`, is defined in `client.py` beside
the field list it reads, and is carried through here unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from backend.catalog.contracts import MAX_RETRIEVAL_METADATA_CHARS

from . import source as src
from .client import ResourceCapture
from .normalize import CaptureNormalization
from .source import GovernmentSourceError

#: The contract version of the snapshot identity manifest. Part of the hashed
#: basis, so changing what identity means changes every derived checksum
#: instead of silently re-reading old ones under new rules.
SNAPSHOT_CONTENT_CONTRACT = "gov.snapshot.1"

#: The domain separator of the page chain. Hashed as the chain's seed, so a
#: chain digest can never collide with a bare page checksum.
PAGE_CHAIN_CONTRACT = "gov.pagechain.1"

#: What this package records about ITSELF in a snapshot's retrieval metadata.
CAPTURE_TOOL = "milo-catalog-government/1"


def page_chain_digest(page_checksums: tuple[str, ...] | list[str]) -> str:
    """The ordered commitment to a capture's captured-response checksums.

    Exactly: seed with `sha256(b"gov.pagechain.1")`, then for each page
    checksum in capture order replace the accumulator with
    `sha256(accumulator || unhexlify(page_checksum))`. Returns lowercase hex.
    Order-sensitive by construction, so two captures that hold the same pages
    in a different order do not chain to the same value.
    """
    accumulator = hashlib.sha256(PAGE_CHAIN_CONTRACT.encode("utf-8")).digest()
    for checksum in page_checksums:
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise GovernmentSourceError("GOV_RESULT_SHAPE_INVALID")
        try:
            raw = bytes.fromhex(checksum)
        except ValueError:
            raise GovernmentSourceError("GOV_RESULT_SHAPE_INVALID") from None
        accumulator = hashlib.sha256(accumulator + raw).digest()
    return accumulator.hex()


def snapshot_content_basis(capture: ResourceCapture) -> str:
    """The exact text the snapshot identity checksum is taken over.

    Returned rather than only hashed so a reviewer can print it, diff two
    captures and see precisely which property differs -- an identity that can
    only be compared as a digest is an identity nobody can debug.
    """
    return json.dumps({
        "contract": SNAPSHOT_CONTENT_CONTRACT,
        "source_family": src.GOVERNMENT_SOURCE_FAMILY,
        "package_id": capture.metadata.package_id,
        "resource_id": capture.resource_id,
        "query": {str(key): capture.query[key] for key in sorted(capture.query)},
        "page_limit": capture.page_limit,
        "reported_total": capture.reported_total,
        "schema_fingerprint": capture.schema_fingerprint,
        "pages": [{"offset": page.offset, "limit": page.limit,
                   "record_count": page.record_count, "sha256": page.body_sha256}
                  for page in capture.pages],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_content_sha256(capture: ResourceCapture) -> str:
    """The SNAPSHOT IDENTITY CHECKSUM of one complete capture, lowercase hex."""
    return hashlib.sha256(snapshot_content_basis(capture).encode("utf-8")).hexdigest()


def retrieval_metadata(capture: ResourceCapture,
                       normalization: CaptureNormalization) -> dict[str, Any]:
    """The bounded, safe provenance of one capture -- INCLUDING its reading gap.

    Safe means: no credential (none exists on this path and the flag below says
    so), no response body, no row, and no field name the guarded RPC's
    credential screen would refuse. Bounded means: checked against the durable
    metadata bound HERE, so an over-long object is a local refusal rather than
    a database error halfway through an ingestion.

    The normalization summary is carried because the gap has to OUTLIVE the
    ingestion that found it. A raw row that could not be read used to be
    reported once, in memory, by whichever run happened to write it, and then
    vanished from every replay and every later reader -- so an active snapshot
    could hold records that no candidate accounted for and nothing said so. It
    is a function of the captured content, so a replay of the same content
    reconstructs it exactly, which `ingest.py` checks rather than assumes.

    What gets dropped under pressure is DEFINED rather than incidental. Every
    page checksum is committed to by `page_chain_sha256` whatever the page
    count, so the per-page list is the one part that may be omitted: it is
    carried verbatim while the page count is within `MAX_INLINE_PAGE_CHECKSUMS`
    AND the whole object fits the durable bound, and dropped otherwise.
    `page_checksums_inline` states plainly which of the two a snapshot holds, so
    a reader never has to infer why a list is absent. The normalization summary
    is never dropped: it decides whether a snapshot may answer a query at all.
    """
    checksums = tuple(page.body_sha256 for page in capture.pages)
    metadata: dict[str, Any] = {
        "capture_contract": SNAPSHOT_CONTENT_CONTRACT,
        "capture_tool": CAPTURE_TOOL,
        "endpoint": src.DATASTORE_SEARCH,
        "api_host": src.DATA_GOV_HOST,
        "package_id": capture.metadata.package_id,
        "resource_id": capture.resource_id,
        "publisher": capture.metadata.publisher,
        "dataset_title": capture.metadata.dataset_title,
        # The market is a property of the SOURCE, recorded once here. No row
        # carries one and none is invented on one.
        "dataset_market_scope": src.GOVERNMENT_DATASET_MARKET,
        "query": {str(key): capture.query[key] for key in sorted(capture.query)},
        "page_limit": capture.page_limit,
        "page_count": len(capture.pages),
        "first_offset": capture.pages[0].offset if capture.pages else 0,
        "last_offset": capture.pages[-1].offset if capture.pages else 0,
        "reported_total": capture.reported_total,
        "captured_record_count": capture.record_count,
        "schema_fingerprint": capture.schema_fingerprint,
        "schema_field_count": len(capture.field_schema),
        "page_chain_sha256": page_chain_digest(checksums),
        "metadata_response_sha256": capture.metadata.metadata_response_sha256,
        "resource_content_hash": capture.metadata.resource_content_hash,
        "resource_metadata_modified": capture.metadata.resource_metadata_modified,
        "http_status": 200,
        "redirect_chain": [],
        "authenticated": False,
        "started_at": capture.started_at,
        "completed_at": capture.completed_at,
        **normalization.durable_summary(),
    }
    inline = {**metadata, "page_checksums_inline": True,
              "page_checksums": [{"offset": page.offset, "limit": page.limit,
                                  "record_count": page.record_count,
                                  "sha256": page.body_sha256}
                                 for page in capture.pages]}
    if len(capture.pages) <= src.MAX_INLINE_PAGE_CHECKSUMS and _fits(inline):
        return inline
    return _bounded(metadata)


def _fits(metadata: Mapping[str, Any]) -> bool:
    return len(json.dumps(metadata, separators=(",", ":"),
                          ensure_ascii=False)) <= MAX_RETRIEVAL_METADATA_CHARS


def _bounded(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The metadata without its inline page list, or a refusal.

    Reached when the list does not fit. The chain digest still commits to every
    page checksum, so nothing about the capture's provenance is lost -- only
    the convenience of reading the checksums without recomputing them.
    """
    without = {**metadata, "page_checksums_inline": False}
    if not _fits(without):
        raise GovernmentSourceError("GOV_METADATA_TOO_LARGE")
    return without


def snapshot_payload(capture: ResourceCapture,
                     normalization: CaptureNormalization) -> dict[str, Any]:
    """The `record_catalog_snapshot` payload for one complete capture.

    Deliberately NOT carrying `snapshot_key`: the key is DERIVED from these
    structural fields by `backend.catalog.payloads`, which is the corrected
    Catalog PR1 contract, and a caller that named its own would only be
    inviting the two to disagree. `activated_at` and `stored_record_count` are
    equally absent -- activation is a separate, evidenced decision, and the
    payload preparer refuses either as an input.
    """
    return {
        "source_family": src.GOVERNMENT_SOURCE_FAMILY,
        "resource_id": capture.resource_id,
        "upstream_version": capture.metadata.upstream_version,
        "upstream_version_kind": capture.metadata.upstream_version_kind,
        "content_sha256": snapshot_content_sha256(capture),
        "retrieved_at": capture.metadata.retrieved_at,
        "declared_record_count": capture.reported_total,
        "retrieval_metadata": retrieval_metadata(capture, normalization),
    }


def raw_record_payloads(capture: ResourceCapture, snapshot: Mapping[str, Any]):
    """Every captured row as `(write payload, the row itself)`, in capture order.

    The row travels beside its payload so a caller never has to re-derive the
    pairing by position -- a zip of two separately produced sequences is exactly
    how a record ends up read as its neighbour.

    The payload is the row EXACTLY as the register served it, including fields
    this catalog does not read and fields it has never heard of -- an unknown
    field is preserved, never dropped, because a row that has been edited on
    the way in is no longer the row the register published.

    `payload_sha256` is deliberately absent: the database derives it from the
    bytes it stores, and the corrected guarded RPC refuses a supplied one.
    """
    for locator, record in capture.located_records():
        yield ({
            "snapshot_id": snapshot["id"],
            "snapshot_key": snapshot["snapshot_key"],
            "resource_id": capture.resource_id,
            "upstream_record_id": str(record["_id"]),
            "payload": dict(record),
            "source_locator": dict(locator),
        }, record)


__all__ = ["CAPTURE_TOOL", "PAGE_CHAIN_CONTRACT", "SNAPSHOT_CONTENT_CONTRACT",
           "page_chain_digest", "raw_record_payloads", "retrieval_metadata",
           "snapshot_content_basis", "snapshot_content_sha256", "snapshot_payload"]
