"""R3: the deterministic bounds and closed vocabularies of real evidence.

Split out of .evidence_contracts for ONE reason: the plan/verification
contracts in .contracts need the same numbers to bound the provenance they
now carry, and .evidence_contracts imports .contracts for StrictContract.
Keeping the numbers in a dependency-free leaf module means there is exactly
one definition of each bound and no import cycle.

Server-owned constants, never environment-tunable and never model-authored.
The three FRAGMENT bounds are deliberately NOT redefined here: R3 reuses B2's
durable limits unchanged (400 characters per fragment, 4 fragments per
source, 1200 fragment characters per source) so .fragments, the SQL
constraints and the drift-detection tests stay one contract.
"""

from __future__ import annotations

MAX_SOURCE_VERSION_CHARS = 128
MAX_LOCATOR_RECORD_ID_CHARS = 128
MAX_LOCATOR_PATH_SEGMENTS = 6
MAX_LOCATOR_SEGMENT_CHARS = 64
MAX_LOCATOR_SECTION_CHARS = 120
MAX_LOCATOR_KEY_CHARS = 800
MAX_DOCUMENT_OFFSET = 1_000_000
MAX_FACTS_PER_BUNDLE = 8
MAX_FACT_VALUE_JSON_BYTES = 512
MAX_FACT_VALUE_DEPTH = 3
MAX_FACT_COLLECTION_ITEMS = 16
MAX_TIME_SCOPE_KEYS = 8
MAX_PROJECTION_FIELDS = 8
MAX_LOCATOR_SCOPE_IDS = 8
MAX_UNIT_CHARS = 32
# A tool result is already bounded by the registry's output bound; this is the
# independent ceiling for the snapshot a content version may be computed over.
MAX_TOOL_SNAPSHOT_JSON_BYTES = 32_768
# `kind:identifier` -- the canonical durable form of a source version.
MAX_SOURCE_VERSION_KEY_CHARS = MAX_SOURCE_VERSION_CHARS + 32

SOURCE_VERSION_KINDS = ("content_sha256", "dataset_version", "document_revision", "git_commit")
LOCATOR_KINDS = ("document_span", "record_field")
FRAGMENT_TYPES = ("structured_projection", "verbatim_excerpt")

__all__ = ["FRAGMENT_TYPES", "LOCATOR_KINDS", "MAX_DOCUMENT_OFFSET",
           "MAX_FACTS_PER_BUNDLE", "MAX_FACT_COLLECTION_ITEMS", "MAX_FACT_VALUE_DEPTH",
           "MAX_FACT_VALUE_JSON_BYTES", "MAX_LOCATOR_KEY_CHARS",
           "MAX_LOCATOR_PATH_SEGMENTS", "MAX_LOCATOR_RECORD_ID_CHARS",
           "MAX_LOCATOR_SCOPE_IDS", "MAX_LOCATOR_SECTION_CHARS",
           "MAX_LOCATOR_SEGMENT_CHARS", "MAX_PROJECTION_FIELDS",
           "MAX_SOURCE_VERSION_CHARS", "MAX_SOURCE_VERSION_KEY_CHARS",
           "MAX_TIME_SCOPE_KEYS", "MAX_TOOL_SNAPSHOT_JSON_BYTES", "MAX_UNIT_CHARS",
           "SOURCE_VERSION_KINDS"]
