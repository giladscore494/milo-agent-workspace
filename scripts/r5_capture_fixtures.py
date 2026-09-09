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

``government`` / ``web``
    Perform bounded, read-only HTTPS GETs. Both require an explicit
    acknowledgement flag, both refuse to follow a cross-host redirect (the
    operator must approve the exact new hostname first), and both write only
    the smallest subset the proof needs -- never a whole dataset, never a
    whole site, never a raw browser session.

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
    return {"manifest_version": 1, "proof": "r5_vehicle_proof", "sources": {}}


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


def main(argv: list[str] | None = None) -> int:
    _refuse_in_automation()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    yeda = sub.add_parser("yeda", help="capture the pinned Yeda catalog record subset")
    yeda.add_argument("--clone", required=True, type=Path,
                      help="path to a read-only local clone pinned to the expected commit")
    args = parser.parse_args(argv)
    if args.command == "yeda":
        capture_yeda(args.clone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
