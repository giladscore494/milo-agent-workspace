"""R5: the versioned, machine-validated manifest of the pinned proof fixtures.

Every byte the R5 proof reads is committed, and every committed byte is
described here: where it came from, which immutable upstream version it was
read at, how it was captured, whether it is an exact response, an exact record
subset or a deterministic projection, and the SHA-256 it must still hash to.

The point is not documentation. This module is the gate: nothing reaches a
proof tool until its fixture has been re-hashed and matched against the
manifest, so a single changed byte fails the proof closed instead of quietly
producing different evidence. A fixture with no manifest entry, a manifest
entry with no fixture, an unknown fixture kind and a missing provenance field
are all refusals, not warnings.

Pure module: file reads only. No network access, no provider call, no database
access, no tool execution and no global mutable state. Refreshing a fixture is
an explicit manual development action (scripts/r5_capture_fixtures.py); nothing
here can fetch, refresh or repair anything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

MANIFEST_VERSION = 1

#: How a committed fixture relates to what the upstream source actually served.
#: The distinction is load-bearing: a verbatim excerpt and a normalized
#: projection support different claims, so a fixture must say which it is
#: rather than leaving a reader to assume the stronger one.
FIXTURE_KINDS = frozenset({
    "exact_response",            # the upstream response body, byte for byte
    "exact_record_subset",       # whole records, verbatim, from a larger document
    "deterministic_projection",  # a reproducible normalization of upstream material
})

#: Provenance every source must carry, whatever kind it is.
REQUIRED_SOURCE_FIELDS = ("source_kind", "canonical_url", "retrieved_at_utc",
                          "capture_method", "fixture_kind", "fixture_path",
                          "fixture_sha256", "record_locator")

#: The extra provenance each source KIND must carry. A git-backed source has to
#: name the commit and blob it was read at; a dataset resource has to name the
#: resource and the snapshot; a saved document has to name the document.
REQUIRED_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "git_repository_file": ("repository", "repository_path", "commit_sha", "blob_sha",
                            "upstream_sha256"),
    "government_dataset_resource": ("resource_id", "upstream_sha256"),
    "saved_web_document": ("document_id", "upstream_sha256"),
}

_SHA256_LENGTH = 64

R5_MANIFEST_REASONS = frozenset({
    "R5_FIXTURE_CHECKSUM_MISMATCH",
    "R5_FIXTURE_MISSING",
    "R5_FIXTURE_NOT_JSON",
    "R5_FIXTURE_PATH_INVALID",
    "R5_MANIFEST_INVALID",
    "R5_MANIFEST_SOURCE_UNKNOWN",
})


class ProofManifestError(ValueError):
    """A manifest/fixture failure carrying ONLY a static, code-owned reason.

    The rejected path, the mismatched digest and the file's contents never
    travel with the classification, so the safe representation is fit for a
    durable task result, a run event and telemetry alike.
    """

    MESSAGES = {
        "R5_FIXTURE_CHECKSUM_MISMATCH": "a committed fixture does not match its manifest checksum",
        "R5_FIXTURE_MISSING": "a manifest entry names a fixture that is not committed",
        "R5_FIXTURE_NOT_JSON": "a committed fixture is not the JSON document the manifest describes",
        "R5_FIXTURE_PATH_INVALID": "a fixture path escapes the committed fixture root",
        "R5_MANIFEST_INVALID": "the proof fixture manifest is missing or malformed",
        "R5_MANIFEST_SOURCE_UNKNOWN": "the proof fixture manifest describes no such source",
    }

    def __init__(self, reason_code: str):
        if reason_code not in R5_MANIFEST_REASONS:
            raise ValueError("manifest reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


def _resolved_fixture_path(relative: Any) -> Path:
    """Resolve one fixture path INSIDE the committed root, or refuse.

    A manifest is committed data, not input, but it is still the one place a
    path string reaches the filesystem, so absolute paths and `..` traversal
    are refused here rather than trusted.
    """
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise ProofManifestError("R5_FIXTURE_PATH_INVALID")
    root = FIXTURE_ROOT.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ProofManifestError("R5_FIXTURE_PATH_INVALID")
    return candidate


def _validate_source(entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise ProofManifestError("R5_MANIFEST_INVALID")
    for field in REQUIRED_SOURCE_FIELDS:
        value = entry.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ProofManifestError("R5_MANIFEST_INVALID")
    kind = entry["source_kind"]
    if kind not in REQUIRED_BY_KIND:
        raise ProofManifestError("R5_MANIFEST_INVALID")
    for field in REQUIRED_BY_KIND[kind]:
        value = entry.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ProofManifestError("R5_MANIFEST_INVALID")
    if entry["fixture_kind"] not in FIXTURE_KINDS:
        raise ProofManifestError("R5_MANIFEST_INVALID")
    digest = entry["fixture_sha256"]
    if not isinstance(digest, str) or len(digest) != _SHA256_LENGTH \
            or any(character not in "0123456789abcdef" for character in digest):
        raise ProofManifestError("R5_MANIFEST_INVALID")
    if not isinstance(entry["record_locator"], Mapping) or not entry["record_locator"]:
        raise ProofManifestError("R5_MANIFEST_INVALID")
    return entry


def load_manifest() -> Mapping[str, Any]:
    """Read and structurally validate the whole manifest, or fail closed."""
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # `from None`: the decoder message quotes the malformed document.
        raise ProofManifestError("R5_MANIFEST_INVALID") from None
    if not isinstance(raw, Mapping) or raw.get("manifest_version") != MANIFEST_VERSION:
        raise ProofManifestError("R5_MANIFEST_INVALID")
    sources = raw.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise ProofManifestError("R5_MANIFEST_INVALID")
    for entry in sources.values():
        _validate_source(entry)
    return raw


def source_entry(source_key: str) -> Mapping[str, Any]:
    """The validated manifest entry for ONE source, or fail closed."""
    entry = load_manifest()["sources"].get(str(source_key))
    if entry is None:
        raise ProofManifestError("R5_MANIFEST_SOURCE_UNKNOWN")
    return _validate_source(entry)


def verify_fixture(source_key: str) -> tuple[Mapping[str, Any], bytes]:
    """Re-hash ONE committed fixture against its manifest entry.

    The digest is computed over the bytes on disk and compared BEFORE the file
    is parsed, so a tampered fixture never reaches a JSON decoder, a tool or a
    mapper. This is the single choke point every proof read goes through.
    """
    entry = source_entry(source_key)
    path = _resolved_fixture_path(entry["fixture_path"])
    try:
        payload = path.read_bytes()
    except OSError:
        raise ProofManifestError("R5_FIXTURE_MISSING") from None
    if hashlib.sha256(payload).hexdigest() != entry["fixture_sha256"]:
        raise ProofManifestError("R5_FIXTURE_CHECKSUM_MISMATCH")
    return entry, payload


def load_fixture(source_key: str) -> tuple[Mapping[str, Any], Any]:
    """Return `(manifest entry, parsed fixture)` for ONE verified source."""
    entry, payload = verify_fixture(source_key)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProofManifestError("R5_FIXTURE_NOT_JSON") from None
    if not isinstance(document, Mapping):
        raise ProofManifestError("R5_FIXTURE_NOT_JSON")
    return entry, document


def verify_all_fixtures() -> tuple[str, ...]:
    """Re-hash every committed fixture. Returns the verified source keys."""
    keys = tuple(sorted(load_manifest()["sources"]))
    for key in keys:
        verify_fixture(key)
    return keys


__all__ = ["FIXTURE_KINDS", "FIXTURE_ROOT", "MANIFEST_PATH", "MANIFEST_VERSION",
           "R5_MANIFEST_REASONS", "REQUIRED_BY_KIND", "REQUIRED_SOURCE_FIELDS",
           "ProofManifestError", "load_fixture", "load_manifest", "source_entry",
           "verify_all_fixtures", "verify_fixture"]
