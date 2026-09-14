#!/usr/bin/env python3
"""Capture the pinned R5 proof fixtures. MANUAL DEVELOPMENT ACTION ONLY.

This script is never invoked by the test suite and never by CI. It exists so
the R5 fixtures under backend/testing/r5_proof/fixtures/ are reproducible and
auditable: every committed byte has a recorded origin, an immutable upstream
version where one exists, and a SHA-256 that the offline tests re-check on
every run.

Refusing to run under CI is deliberate. A refresh changes evidence, and
evidence must never change because a pipeline happened to execute.

Sub-commands
------------

``yeda``
    Reads a LOCAL read-only clone of the Yeda catalog repository, pinned to an
    exact commit, and writes the bounded record subset the proof needs. No
    network access: the operator clones the public repository first, and this
    script verifies the checkout is at the expected commit and that the
    catalog blob matches the expected git blob SHA before reading anything.

``import-capture``
    Imports the Government and Web fixtures from a source-capture archive
    produced OUTSIDE this environment, because this environment's egress proxy
    refuses ``data.gov.il`` and ``www.toyota.co.il`` with HTTP 403 to CONNECT.
    The archive is treated as untrusted input: its own ``SHA256SUMS.txt``, its
    manifest digests, its byte counts, its HTTP statuses, its authentication
    flags and its host allowlist are all re-verified here before a byte is
    copied, and only the exact response bodies the proof reads are committed --
    never a whole dataset, never a whole site, never a raw browser session.

Nothing here writes to any upstream service, holds any credential, or touches
production. Every request is a GET.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.testing.r5_proof.web import (WEB_TEXT_PROJECTION_VERSION,
                                          visible_text_projection)

FIXTURE_ROOT = Path("backend/testing/r5_proof/fixtures")
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

# The exact upstream identity of the Yeda catalog this proof is pinned to.
YEDA_REPOSITORY = "giladscore494/reliabilityAIModelsR2"
YEDA_COMMIT_SHA = "f7bf132abf1b1ec8f9d81076560a5a618447a9e6"
YEDA_CATALOG_PATH = "my-flask-app/app/data/model_technical_catalog_il.json"
YEDA_BLOB_SHA = "383ba9bf12ddfafeac79fe42ea449c11204dc017"
YEDA_CATALOG_SHA256 = "0a8d0e4240cbf11ad908012168d9dd60cafc2c9f75b92caecbd73f126c349ad6"

# The selected vehicle's exact position inside that pinned catalog.
YEDA_MODEL_INDEX = 860
YEDA_VARIANT_INDEX = 4


class CaptureRefused(SystemExit):
    """A capture that must not proceed, with an operator-readable reason."""

    def __init__(self, message: str):
        super().__init__(f"refused: {message}")


def _refuse_in_automation() -> None:
    """A fixture refresh is a human decision, never a pipeline side effect."""
    for variable in ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST"):
        if os.environ.get(variable):
            raise CaptureRefused(
                f"{variable} is set; fixture capture is a manual development action")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fixture(relative: str, payload: Any) -> tuple[Path, str]:
    """Write one fixture deterministically and return its path and digest.

    `ensure_ascii=False` keeps Hebrew source text as the source wrote it, and
    the fixed separators/indent make the committed bytes reproducible: running
    this script twice on the same upstream version produces the identical file.
    """
    path = FIXTURE_ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path, sha256_of(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"manifest_version": 2, "proof": "r5_vehicle_proof", "sources": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    MANIFEST_PATH.write_text(text, encoding="utf-8")


# --- Yeda -------------------------------------------------------------------

def _git(clone: Path, *args: str) -> str:
    result = subprocess.run(("git", "-C", str(clone), *args), capture_output=True,
                            text=True, check=False)
    if result.returncode != 0:
        raise CaptureRefused(f"git {' '.join(args)} failed in {clone}")
    return result.stdout.strip()


def capture_yeda(clone: Path) -> None:
    """Write the bounded real Yeda record subset from a pinned local clone.

    Three independent checks run before a single byte is read: the checkout is
    at the expected commit, the catalog file is the expected git blob, and its
    content hashes to the expected SHA-256. A clone at any other version is
    refused rather than silently producing evidence from a different catalog.
    """
    head = _git(clone, "rev-parse", "HEAD")
    if head != YEDA_COMMIT_SHA:
        raise CaptureRefused(f"clone is at {head}, expected {YEDA_COMMIT_SHA}")
    blob = _git(clone, "rev-parse", f"HEAD:{YEDA_CATALOG_PATH}")
    if blob != YEDA_BLOB_SHA:
        raise CaptureRefused(f"catalog blob is {blob}, expected {YEDA_BLOB_SHA}")
    catalog_file = clone / YEDA_CATALOG_PATH
    if sha256_of(catalog_file) != YEDA_CATALOG_SHA256:
        raise CaptureRefused("catalog content hash does not match the pinned value")

    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    models = catalog["models"]
    record = models[YEDA_MODEL_INDEX]
    variant = record["technical_variants_il"][YEDA_VARIANT_INDEX]
    if (record["make"], record["model"]) != ("Toyota", "RAV4"):
        raise CaptureRefused("the pinned model index does not hold the selected vehicle")
    if variant["fuel_type"] != "plug_in_hybrid":
        raise CaptureRefused("the pinned variant index does not hold the selected variant")

    # The catalog's own identity fields travel WITH the record, so the fixture
    # states which catalog version it is a subset of without the reader having
    # to consult the 7.3 MB original.
    payload = {
        "catalog": {
            "generated_at": catalog["generated_at"],
            "market": catalog["market"],
            "model_count": len(models),
            "catalog_hash": yeda_catalog_hash(catalog),
        },
        "model_index": YEDA_MODEL_INDEX,
        "model": record,
    }
    path, digest = write_fixture("yeda/rav4_model_record.json", payload)

    manifest = load_manifest()
    manifest.setdefault("sources", {})["yeda"] = {
        "source_kind": "git_repository_file",
        "canonical_url":
            f"https://github.com/{YEDA_REPOSITORY}/blob/{YEDA_COMMIT_SHA}/{YEDA_CATALOG_PATH}",
        "repository": YEDA_REPOSITORY,
        "repository_path": YEDA_CATALOG_PATH,
        "commit_sha": YEDA_COMMIT_SHA,
        "blob_sha": YEDA_BLOB_SHA,
        "upstream_sha256": YEDA_CATALOG_SHA256,
        "upstream_catalog_hash": payload["catalog"]["catalog_hash"],
        "upstream_generated_at": catalog["generated_at"],
        "retrieved_at_utc": utc_now(),
        "record_locator": {
            "model_index": YEDA_MODEL_INDEX,
            "variant_index": YEDA_VARIANT_INDEX,
            "make": record["make"],
            "model": record["model"],
            # The catalog's OWN market identifier, from the catalog root.
            "catalog_market": catalog["market"],
            # The record's `market` field verbatim. It is NOT a market
            # identifier: across this catalog it takes the values IL,
            # IL-confirmed, IL-likely and global-reference-only, so it states
            # how confident the catalog is that the model is present in the
            # Israeli market. The trusted mapper reads it through a closed
            # vocabulary and never parses this string for a market name.
            "record_market_presence": record["market"],
            "record_profile_confidence": record.get("profile_confidence"),
            "variant_support_level": variant.get("support_level"),
        },
        "capture_method":
            "read from a read-only local clone pinned to commit_sha; the checkout "
            "commit, the git blob sha and the file sha256 are all verified before "
            "the catalog is parsed",
        "fixture_kind": "exact_record_subset",
        "fixture_path": "yeda/rav4_model_record.json",
        "fixture_sha256": digest,
        "fixture_byte_count": path.stat().st_size,
        "upstream_committed": False,
    }
    save_manifest(manifest)
    print(f"wrote {path} ({path.stat().st_size} bytes, sha256 {digest})")


def yeda_catalog_hash(catalog: dict[str, Any]) -> str:
    """The catalog's OWN short hash, computed exactly as the Yeda service does.

    Mirrors `get_catalog_hash` in the upstream `vehicle_catalog_service.py`, so
    the manifest records the upstream's own version identifier rather than one
    this repository invented.
    """
    basis = json.dumps({"generated_at": catalog.get("generated_at"),
                        "model_count": len(catalog.get("models", []))},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# --- captured Government / Web sources --------------------------------------
#
# Unlike `yeda`, these are NOT fetched here. They are IMPORTED from a capture
# archive produced outside this environment by the MILO R5 source-capture app,
# because this environment's egress proxy refuses `data.gov.il` and
# `www.toyota.co.il` with HTTP 403 to CONNECT. The archive is treated as
# untrusted input: its own checksum file, its manifest digests, its byte
# counts, its HTTP statuses and its host allowlist are all re-verified here
# before a single byte is copied, and the copy is byte-for-byte.

#: The exact upstream identity of the Israeli Ministry of Transport dataset.
CKAN_PACKAGE_ID = "degem-rechev-wltp"
WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"

#: The only hosts a capture entry may name. A redirect or a requested URL on
#: any other host invalidates the whole archive rather than one entry.
APPROVED_CAPTURE_HOSTS = frozenset({"data.gov.il", "www.toyota.co.il"})

#: The archive's own source type for one page of a datastore query. A page
#: carries a position in its query; the package-metadata and schema entries
#: carry none, and must never be given one.
GOVERNMENT_PAGE_SOURCE_TYPE = "government_datastore_page"

#: The one overall status an importable archive may carry. Every other status
#: the capture app can emit means the capture did not establish what R5 needs.
IMPORTABLE_CAPTURE_STATUS = "ready_for_r5_bundle_review"

#: What is imported, and nothing else: capture entry id -> (fixture path,
#: manifest source key).
#:
#: All three pages of the pinned `q=RAV4` query are committed. The query
#: reports 233 rows and the datastore served them as 100 + 100 + 33, so a
#: two-page import would have committed a PREFIX of the query while its
#: provenance named the whole of it -- and the shortfall would be invisible,
#: because every page reports the same honest total. The third page carries no
#: row this proof selects, which is a fact the proof establishes by scanning
#: it, not a reason to leave it out.
#:
#: The archive also holds the whole `additional` resource and the derived
#: consolidations. Those are a DIFFERENT resource and a derived artefact
#: respectively, not missing pages of this query, and neither is committed.
IMPORTED_GOVERNMENT: dict[str, tuple[str, str]] = {
    "government_package_show": ("government/package_show.json", "government_package"),
    "government_wltp_q0_page_000001": ("government/wltp_page_000001.json",
                                       "government_wltp_page_1"),
    "government_wltp_q0_page_000002": ("government/wltp_page_000002.json",
                                       "government_wltp_page_2"),
    "government_wltp_q0_page_000003": ("government/wltp_page_000003.json",
                                       "government_wltp_page_3"),
}
#: capture entry id -> (fixture path, manifest source key, document id).
#:
#: The committed fixture is the page's deterministic VISIBLE-TEXT PROJECTION,
#: not the raw HTML. Two reasons, in this order:
#:
#: 1.  The projection is the evidence surface. Every document-span locator
#:     points into it, and the raw markup around it supports no claim.
#: 2.  The raw page carries the SITE's own client-side tokens -- Mapbox
#:     publishable keys and an analytics key that every visitor receives --
#:     and GitHub push protection classifies them as secrets and refuses the
#:     push. Bypassing that protection to commit a third party's token is not
#:     something a proof gets to decide, and redacting bytes would break the
#:     digest chain anyway.
#:
#: The raw response is therefore NOT committed, exactly as the 7.3 MB Yeda
#: catalog is not: its SHA-256 is recorded as `upstream_sha256` and remains
#: the source version, so the evidence is still pinned to the whole body.
IMPORTED_WEB: dict[str, tuple[str, str, str]] = {
    "toyota_rav4_phev": ("web/toyota_il_rav4_phev.visible_text.txt",
                         "web_toyota_rav4_phev", "toyota_il_rav4_phev"),
}

#: The Government records this proof actually reads, per committed page. Stated
#: here so the manifest records WHICH rows the page was imported for, and so a
#: page whose expected rows are absent is refused instead of silently committed.
#:
#: `government_wltp_page_3` is deliberately absent from this table and gets no
#: entry with an invented id: no row on that page matches the identity this
#: proof selects, and a page is imported because it completes the query, not
#: because it holds a selected row. Its manifest locator is page-level and says
#: exactly that.
GOVERNMENT_RECORD_IDS: dict[str, tuple[int, ...]] = {
    "government_wltp_page_1": (36327,),
    "government_wltp_page_2": (37392, 37393),
}


def _page_locator(entry: dict[str, Any], path: Path) -> dict[str, Any]:
    """One datastore page's position in its query, recorded and re-checked.

    The archive says which offset was requested, at which page size, how many
    rows came back and what total the datastore reported. The committed body
    echoes all four, so both are compared here: a page whose recorded position
    and whose own response disagree is refused rather than imported with a
    locator that would then be checked against nothing.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    result = body.get("result")
    if not isinstance(result, dict):
        raise CaptureRefused(f"{entry['source_id']} is not a datastore response")
    records = result.get("records")
    if not isinstance(records, list):
        raise CaptureRefused(f"{entry['source_id']} states no records")
    stated = {"requested_offset": result.get("offset"), "requested_limit": result.get("limit"),
              "returned_record_count": len(records), "reported_total": result.get("total")}
    recorded = {field: entry.get(field) for field in stated}
    if any(not isinstance(value, int) or isinstance(value, bool)
           for value in recorded.values()):
        raise CaptureRefused(f"{entry['source_id']} records no complete page position")
    if stated != recorded:
        raise CaptureRefused(f"{entry['source_id']} does not answer at its recorded position")
    if _text_param(result.get("q")) != _text_param(entry.get("query_token")) or \
            str(result.get("resource_id")) != str(entry.get("resource_id")):
        raise CaptureRefused(f"{entry['source_id']} answers a different query or resource")
    return {**recorded, "query_token": entry["query_token"],
            "resource_id": entry["resource_id"]}


def _text_param(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _capture_manifest(root: Path) -> dict[str, Any]:
    """Re-verify the archive from the inside, then return its manifest."""
    sums = root / "SHA256SUMS.txt"
    manifest_path = root / "manifest.json"
    if not sums.is_file() or not manifest_path.is_file():
        raise CaptureRefused(f"{root} is not a capture archive root")
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        target = root / relative
        if not target.is_file():
            raise CaptureRefused(f"archive lists a missing file: {relative}")
        if sha256_of(target) != digest:
            raise CaptureRefused(f"archive checksum mismatch: {relative}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status = manifest.get("overall_status")
    if status != IMPORTABLE_CAPTURE_STATUS:
        raise CaptureRefused(f"archive status is {status!r}, not {IMPORTABLE_CAPTURE_STATUS!r}")
    for field in ("api_key_used", "credentials_used", "cookies_supplied"):
        if manifest.get(field):
            raise CaptureRefused(f"archive records {field}; only unauthenticated captures import")
    for field in ("government_failures", "web_failures", "integrity_problems"):
        if manifest.get(field):
            raise CaptureRefused(f"archive records {field}")
    return manifest


def _capture_entry(manifest: dict[str, Any], source_id: str) -> dict[str, Any]:
    """One archive entry, re-verified against the bytes it describes."""
    entry = next((item for item in manifest["entries"]
                  if item.get("source_id") == source_id), None)
    if entry is None:
        raise CaptureRefused(f"archive has no entry {source_id!r}")
    if entry.get("http_status") != 200:
        raise CaptureRefused(f"{source_id} was not HTTP 200")
    if str(entry.get("validation_result", "")).startswith("failed"):
        raise CaptureRefused(f"{source_id} failed capture validation")
    for field in ("api_key_used", "credentials_used", "cookies_supplied"):
        if entry.get(field):
            raise CaptureRefused(f"{source_id} records {field}")
    for url in (entry["requested_url"], entry["final_url"], *(entry.get("redirect_chain") or [])):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in APPROVED_CAPTURE_HOSTS:
            raise CaptureRefused(f"{source_id} names a non-approved URL")
    return entry


def _absent_metadata(entry: dict[str, Any], *, names: tuple[str, ...],
                     values: dict[str, Any]) -> dict[str, Any]:
    """Record every optional validator explicitly, present or absent.

    A value the capture actually received is copied; one it did not is written
    as an explicit null AND named in `absent_source_metadata`. Neither half is
    ever inferred: the capture app preserves ETag and Last-Modified when a
    server sends them, so their absence here is the server's answer, not ours.
    """
    recorded = dict(values)
    return {**recorded, "absent_source_metadata": sorted(
        name for name in names if recorded.get(name) is None)}


def _copy_exact(source: Path, relative: str) -> tuple[Path, str, int]:
    """Copy one captured body byte-for-byte into the fixture tree."""
    target = FIXTURE_ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = source.read_bytes()
    target.write_bytes(payload)
    return target, hashlib.sha256(payload).hexdigest(), len(payload)


def _write_projection(source: Path, relative: str) -> tuple[Path, str, int]:
    """Project one captured page's visible text and commit exactly that.

    The projection is a pure function of the captured bytes under
    `WEB_TEXT_PROJECTION_VERSION`, so this is a deterministic normalization --
    recorded as `deterministic_projection`, never as the response itself. It is
    checked to be idempotent before it is written: projecting the projection
    must be a no-op, which is what makes the committed file demonstrably a
    projection OUTPUT rather than an edited copy of the page.
    """
    body = source.read_bytes().decode("utf-8")
    projected = visible_text_projection(body)
    if visible_text_projection(projected) != projected:
        raise CaptureRefused(f"{relative} is not a stable visible-text projection")
    if not projected.strip():
        raise CaptureRefused(f"{relative} projected to no visible text")
    target = FIXTURE_ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = projected.encode("utf-8")
    target.write_bytes(payload)
    return target, hashlib.sha256(payload).hexdigest(), len(payload)


def government_source_entry(capture: dict[str, Any], entry: dict[str, Any], key: str,
                            relative: str, path: Path, digest: str, size: int,
                            dataset_version: str, wltp: dict[str, Any]) -> dict[str, Any]:
    """Build ONE government source's manifest entry from its captured bytes.

    Extracted from `capture_import` so there is exactly one rule for what a
    committed government fixture's provenance says, and so that rule can be
    exercised offline against the committed bytes -- a manifest entry edited by
    hand into a shape this function would never produce is then a test failure
    rather than a plausible-looking line in a diff.

    Pure: it reads the capture entry and the committed file and returns a dict.
    It writes nothing and fetches nothing.
    """
    headers = entry.get("headers") or {}
    record_ids = GOVERNMENT_RECORD_IDS.get(key)
    locator: dict[str, Any] = {"raw_capture_path": entry["raw_path"],
                               "ckan_package_id": CKAN_PACKAGE_ID}
    # The package-metadata response is about the DATASET, not about one
    # resource, so it carries no resource id to record and none is
    # invented for it.
    if entry.get("resource_id"):
        locator["resource_id"] = entry["resource_id"]
    if entry.get("source_type") == GOVERNMENT_PAGE_SOURCE_TYPE:
        # A datastore page's locator is its POSITION IN THE QUERY, which it
        # has whether or not it holds a row this proof selects. Recorded
        # from the archive and then checked against the page's own echo of
        # it, so a page the capture described wrongly is refused here
        # rather than committed and trusted later.
        locator["page"] = _page_locator(entry, path)
    if record_ids is not None:
        body = json.loads(path.read_text(encoding="utf-8"))
        index_of = {record["_id"]: position for position, record
                    in enumerate(body["result"]["records"])}
        missing = [item for item in record_ids if item not in index_of]
        if missing:
            raise CaptureRefused(f"{entry['source_id']} does not hold records {missing}")
        locator["record_ids"] = list(record_ids)
        locator["record_indexes"] = {str(item): index_of[item] for item in record_ids}
    elif "page" in locator:
        # Said out loud, so an empty record list is a reviewed fact about
        # this page rather than a field a later edit could quietly fill.
        locator["selected_records"] = (
            "none; this page completes the pinned query and holds no row "
            "this proof selects")
    return {
        "source_kind": "government_dataset_resource",
        "canonical_url": entry["requested_url"],
        "requested_url": entry["requested_url"], "final_url": entry["final_url"],
        "redirect_chain": list(entry.get("redirect_chain") or []),
        "http_status": entry["http_status"],
        "retrieved_at_utc": entry["finished_utc"],
        "capture_method": (
            "public unauthenticated read-only HTTPS GET captured by "
            f"{capture['tool']} into {capture['capture_id']}, then imported "
            "byte-for-byte after the archive's own checksums, byte counts, HTTP "
            "statuses and host allowlist were independently re-verified"),
        "fixture_kind": "exact_response",
        "fixture_path": relative,
        "fixture_sha256": digest,
        "fixture_byte_count": size,
        "upstream_sha256": entry["sha256"],
        "response_byte_count": size,
        "upstream_committed": True,
        "content_type": entry.get("content_type"),
        "resource_id": entry.get("resource_id") or WLTP_RESOURCE_ID,
        "ckan_package_id": CKAN_PACKAGE_ID,
        "query": dict(entry.get("query_params") or {}),
        "source_version_kind": "dataset_version",
        "source_version": dataset_version,
        "source_version_origin": (
            "package_show -> resources[WLTP_RESOURCE_ID].last_modified; the "
            "resource carries no revision_id key at all, so none is recorded. "
            "Its published `hash` is recorded beside this as provenance but is "
            "NOT used as the version: it is an MD5 of the full 53 MB CSV export, "
            "which is neither a SHA-256 nor the JSON the datastore API served"),
        "resource_content_hash": wltp.get("hash"),
        "resource_metadata_modified": wltp.get("metadata_modified"),
        "record_locator": locator,
        **_absent_metadata(entry, names=("etag", "last_modified", "resource_revision_id"),
                           values={"etag": headers.get("ETag"),
                                   "last_modified": headers.get("Last-Modified"),
                                   "resource_revision_id": wltp.get("revision_id")}),
    }


def capture_import(root: Path) -> None:
    """Import the verified Government and Web sources from a capture archive."""
    capture = _capture_manifest(root)
    manifest = load_manifest()
    manifest["manifest_version"] = 2
    manifest.setdefault("sources", {})
    manifest["capture_archive"] = {
        "capture_id": capture["capture_id"], "tool": capture["tool"],
        "started_utc": capture["started_utc"], "finished_utc": capture["finished_utc"],
        "overall_status": capture["overall_status"],
        "manifest_sha256": sha256_of(root / "manifest.json"),
        "request_count": capture["totals"]["request_count"],
        "authentication": capture["authentication"],
        "note": ("byte-exact bodies of public unauthenticated read-only HTTPS GETs, "
                 "captured outside this environment because its egress proxy refuses "
                 "data.gov.il and www.toyota.co.il with HTTP 403 to CONNECT"),
    }

    # The dataset's own immutable version marker, read from package_show.
    package_entry = _capture_entry(capture, "government_package_show")
    package = json.loads((root / package_entry["raw_path"]).read_text(encoding="utf-8"))
    resources = {item["id"]: item for item in package["result"]["resources"]}
    wltp = resources.get(WLTP_RESOURCE_ID)
    if wltp is None:
        raise CaptureRefused("package_show does not describe the WLTP resource")
    dataset_version = wltp.get("last_modified")
    if not isinstance(dataset_version, str) or not dataset_version:
        raise CaptureRefused("the WLTP resource states no last_modified to pin to")

    for source_id, (relative, key) in sorted(IMPORTED_GOVERNMENT.items()):
        entry = _capture_entry(capture, source_id)
        path, digest, size = _copy_exact(root / entry["raw_path"], relative)
        if digest != entry["sha256"] or size != entry["byte_count"]:
            raise CaptureRefused(f"{source_id} did not import byte-for-byte")
        manifest["sources"][key] = government_source_entry(
            capture, entry, key, relative, path, digest, size, dataset_version, wltp)
        print(f"imported {path} ({size} bytes, sha256 {digest})")

    for source_id, (relative, key, document_id) in sorted(IMPORTED_WEB.items()):
        entry = _capture_entry(capture, source_id)
        raw_path = root / entry["raw_path"]
        raw = raw_path.read_bytes()
        # The RAW response is verified here even though it is not committed:
        # its digest is what the evidence is pinned to, and the projection
        # below is only trustworthy because it was derived from these bytes.
        if hashlib.sha256(raw).hexdigest() != entry["sha256"] or len(raw) != entry["byte_count"]:
            raise CaptureRefused(f"{source_id} is not the captured response")
        path, digest, size = _write_projection(raw_path, relative)
        headers = entry.get("headers") or {}
        manifest["sources"][key] = {
            "source_kind": "saved_web_document",
            "canonical_url": entry["final_url"],
            "requested_url": entry["requested_url"], "final_url": entry["final_url"],
            "redirect_chain": list(entry.get("redirect_chain") or []),
            "http_status": entry["http_status"],
            "retrieved_at_utc": entry["finished_utc"],
            "capture_method": (
                "public unauthenticated read-only HTTPS GET captured by "
                f"{capture['tool']} into {capture['capture_id']}; the response body "
                "was verified against the archive's recorded digest and byte count and "
                "then projected to its visible text by "
                f"{WEB_TEXT_PROJECTION_VERSION}. The raw HTML is NOT committed: it "
                "carries the site's own client-side Mapbox and analytics tokens, which "
                "GitHub push protection refuses, and the projection is the evidence "
                "surface every locator points into. The raw response digest is recorded "
                "as upstream_sha256 and remains the source version"),
            "fixture_kind": "deterministic_projection",
            "fixture_path": relative,
            "fixture_sha256": digest,
            "fixture_byte_count": size,
            "upstream_sha256": entry["sha256"],
            "response_byte_count": entry["byte_count"],
            "upstream_committed": False,
            "content_type": entry.get("content_type"),
            "document_id": document_id,
            "capture_validation_result": entry.get("validation_result"),
            # No ETag, no Last-Modified and no site-published revision: the
            # SHA-256 of the EXACT FULL captured body is the only immutable
            # identifier this document has, which is precisely the case
            # SourceVersion's `content_sha256` kind exists for.
            "source_version_kind": "content_sha256",
            "source_version": entry["sha256"],
            "source_version_origin": (
                "sha256 of the exact full captured response body; the response "
                "carried neither an ETag nor a Last-Modified header"),
            "text_projection_version": WEB_TEXT_PROJECTION_VERSION,
            "record_locator": {"document_id": document_id,
                               "raw_capture_path": entry["raw_path"],
                               "projection_char_count": len(
                                   path.read_text(encoding="utf-8")),
                               "text_projection_version": WEB_TEXT_PROJECTION_VERSION},
            **_absent_metadata(entry, names=("etag", "last_modified"),
                               values={"etag": headers.get("ETag"),
                                       "last_modified": headers.get("Last-Modified")}),
        }
        print(f"imported {path} ({size} bytes, sha256 {digest})")

    save_manifest(manifest)


def main(argv: list[str] | None = None) -> int:
    _refuse_in_automation()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    yeda = sub.add_parser("yeda", help="capture the pinned Yeda catalog record subset")
    yeda.add_argument("--clone", required=True, type=Path,
                      help="path to a read-only local clone pinned to the expected commit")
    imported = sub.add_parser("import-capture",
                              help="import the verified Government and Web capture archive")
    imported.add_argument("--capture-root", required=True, type=Path,
                          help="path to the extracted, verified capture archive root")
    args = parser.parse_args(argv)
    if args.command == "yeda":
        capture_yeda(args.clone)
    elif args.command == "import-capture":
        capture_import(args.capture_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
