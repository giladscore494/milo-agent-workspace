"""The bounded catalog namespace: durable, evidence-backed vehicle catalog state.

PR1 adds PERSISTENCE ONLY. There is no ingestion, no HTTP fetch, no tool, no
engine and no canonical promotion here -- only the contracts the durable
relations are defined by, so the backend and the database state one rule each
rather than two that can drift.
"""

from .contracts import (CANDIDATE_IDENTITY_DIMENSIONS, CANDIDATE_STATUSES,
                        CATALOG_SOURCE_FAMILIES, CATALOG_TRUST_STATES,
                        MAX_RAW_PAYLOAD_CHARS, MAX_RETRIEVAL_METADATA_CHARS,
                        SNAPSHOT_VALIDATION_STATES, TRUST_STATE_BY_FAMILY)

__all__ = ["CANDIDATE_IDENTITY_DIMENSIONS", "CANDIDATE_STATUSES",
           "CATALOG_SOURCE_FAMILIES", "CATALOG_TRUST_STATES",
           "MAX_RAW_PAYLOAD_CHARS", "MAX_RETRIEVAL_METADATA_CHARS",
           "SNAPSHOT_VALIDATION_STATES", "TRUST_STATE_BY_FAMILY"]
