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
# R4: the closed set of identity dimensions a structured fact may qualify
# itself with, and the bounds of one entry.  These exist because "make +
# commercial model + an overlapping year" does NOT identify a variant: a
# generation change, a different engine or transmission, a different market or
# a different official/model code are all DIFFERENT things, and comparing a
# claim against a fact that differs on any of them is comparing two vehicles.
# The vocabulary is closed and server-owned: a mapper may state a dimension it
# read, never invent one.
IDENTITY_DIMENSIONS = ("body_style", "drivetrain", "engine", "generation", "model_code",
                       "transmission", "trim")
MAX_IDENTITY_DIMENSION_CHARS = 120
# R4: the bounded identifier of the verification contract a durable verdict
# was decided under, and the bound on ONE verdict's support-link list.
MAX_VERIFIER_CONTRACT_VERSION_CHARS = 120
MAX_VERDICT_REASON_CODE_CHARS = 64
# A tool result is already bounded by the registry's output bound; this is the
# independent ceiling for the snapshot a content version may be computed over.
MAX_TOOL_SNAPSHOT_JSON_BYTES = 32_768
# `kind:identifier` -- the canonical durable form of a source version.
MAX_SOURCE_VERSION_KEY_CHARS = MAX_SOURCE_VERSION_CHARS + 32

SOURCE_VERSION_KINDS = ("content_sha256", "dataset_version", "document_revision", "git_commit")
LOCATOR_KINDS = ("document_span", "record_field")
FRAGMENT_TYPES = ("structured_projection", "verbatim_excerpt")

# The kind-specific identifier rule of every source version kind.  ONE
# definition: the Python contract, the grounding reader, the guarded RPCs and
# the table constraints all apply exactly these expressions (the SQL copies are
# pinned against these strings by tests/test_evidence_migration_static.py).
# Written in the POSIX-compatible subset PostgreSQL's `~` and Python's `re`
# read identically.
SOURCE_VERSION_PATTERNS = {
    "content_sha256": r"^[0-9a-f]{64}$",
    "dataset_version": r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$",
    "document_revision": r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$",
    "git_commit": r"^[0-9a-f]{7,64}$",
}
# A locator segment is a LITERAL object key and a record id is a LITERAL
# identifier.  Neither pattern admits `$`, `*`, `[`, `]`, `?`, a quote, a
# comma or `..`, so JSONPath/expression/filter syntax is rejected as a
# malformed key rather than being parsed and then refused.
LOCATOR_SEGMENT_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"
LOCATOR_RECORD_ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,127}$"
# The fragment type is a function of the locator shape, never a free choice: a
# verbatim excerpt can only come from a document span and a structured
# projection can only come from a record field.
FRAGMENT_TYPE_BY_LOCATOR_KIND = {"document_span": "verbatim_excerpt",
                                 "record_field": "structured_projection"}

__all__ = ["FRAGMENT_TYPES", "FRAGMENT_TYPE_BY_LOCATOR_KIND", "IDENTITY_DIMENSIONS",
           "LOCATOR_KINDS", "MAX_IDENTITY_DIMENSION_CHARS",
           "MAX_VERDICT_REASON_CODE_CHARS", "MAX_VERIFIER_CONTRACT_VERSION_CHARS",
           "LOCATOR_RECORD_ID_PATTERN", "LOCATOR_SEGMENT_PATTERN", "MAX_DOCUMENT_OFFSET",
           "MAX_FACTS_PER_BUNDLE", "MAX_FACT_COLLECTION_ITEMS", "MAX_FACT_VALUE_DEPTH",
           "MAX_FACT_VALUE_JSON_BYTES", "MAX_LOCATOR_KEY_CHARS",
           "MAX_LOCATOR_PATH_SEGMENTS", "MAX_LOCATOR_RECORD_ID_CHARS",
           "MAX_LOCATOR_SCOPE_IDS", "MAX_LOCATOR_SECTION_CHARS",
           "MAX_LOCATOR_SEGMENT_CHARS", "MAX_PROJECTION_FIELDS",
           "MAX_SOURCE_VERSION_CHARS", "MAX_SOURCE_VERSION_KEY_CHARS",
           "MAX_TIME_SCOPE_KEYS", "MAX_TOOL_SNAPSHOT_JSON_BYTES", "MAX_UNIT_CHARS",
           "SOURCE_VERSION_KINDS", "SOURCE_VERSION_PATTERNS"]
