"""Register capture configuration (PR-D1, D1-3 / D1-4 / D1-8). Read from the
environment -- never hard-coded at a call site -- with the reviewed defaults.

    MILO_DB_CAPACITY_BYTES               default 500 MB = 500,000,000 bytes (the
                                         plan's database size; decimal, the lower
                                         reading of "500 MB")
    MILO_DB_CAPACITY_THRESHOLD           default 0.80
    MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE  default 3000: every register row costs,
                                         once compacted (PR-L2), its raw record
                                         skeleton (603 B), candidate keys
                                         (431 B; the identity is read from the
                                         variant, the identity indexes skip it),
                                         variant (983 B) and two ledger levels
                                         (903 B), tables + TOAST + indexes:
                                         2,920 B measured (tests/
                                         test_register_compaction_postgres.py),
                                         rounded up to the next 500
    MILO_REGISTER_GROUP_MAX_ROWS         default 10000 (one request's cap)
    MILO_REGISTER_ARCHIVE_BUCKET         no default (the capture job's archive)

A malformed value falls back to the default -- the reviewed bound -- rather
than widening anything; an out-of-range threshold is clamped into (0, 1].
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

CAPACITY_BYTES_ENV = "MILO_DB_CAPACITY_BYTES"
CAPACITY_THRESHOLD_ENV = "MILO_DB_CAPACITY_THRESHOLD"
BYTES_PER_ROW_ENV = "MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE"
GROUP_MAX_ROWS_ENV = "MILO_REGISTER_GROUP_MAX_ROWS"
ARCHIVE_BUCKET_ENV = "MILO_REGISTER_ARCHIVE_BUCKET"

DEFAULT_CAPACITY_BYTES = 500_000_000
DEFAULT_CAPACITY_THRESHOLD = 0.80
DEFAULT_BYTES_PER_ROW = 3000
DEFAULT_GROUP_MAX_ROWS = 10_000

#: Planning constants for the full-register projection (PR-L2, asserted in
#: tests/test_register_compaction_postgres.py). The database without the
#: register's four tables: production 2026-09-30, pg_database_size 144.5 MB
#: minus catalog_raw_records + catalog_candidate_variants + catalog_variants +
#: catalog_variant_coverage (121.0 MB) = 23.6 MB, rounded up to 25 MB.
NON_REGISTER_BASE_BYTES = 25_000_000
#: The register's rows (the latest directory's total, production 2026-09-30).
FULL_REGISTER_ROWS = 101_686

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


@dataclass(frozen=True)
class RegisterConfig:
    capacity_bytes: int
    capacity_threshold: float
    bytes_per_row: int
    group_max_rows: int
    archive_bucket: str

    @property
    def capacity_limit_bytes(self) -> int:
        """The threshold in bytes: capacity x threshold."""
        return int(self.capacity_bytes * self.capacity_threshold)

    def projected_bytes(self, current_bytes: int, expected_rows: int) -> int:
        return int(current_bytes) + int(expected_rows) * self.bytes_per_row

    def as_view(self) -> dict[str, object]:
        return {"capacity_bytes": self.capacity_bytes, "threshold": self.capacity_threshold,
                "limit_bytes": self.capacity_limit_bytes, "bytes_per_row_estimate": self.bytes_per_row,
                "group_max_rows": self.group_max_rows}


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(str(source.get(name) or default).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def load(env: Mapping[str, str] | None = None) -> RegisterConfig:
    source = os.environ if env is None else env
    try:
        threshold = float(str(source.get(CAPACITY_THRESHOLD_ENV) or DEFAULT_CAPACITY_THRESHOLD).strip())
    except ValueError:
        threshold = DEFAULT_CAPACITY_THRESHOLD
    if not (0 < threshold <= 1):
        threshold = DEFAULT_CAPACITY_THRESHOLD
    bucket = str(source.get(ARCHIVE_BUCKET_ENV) or "").strip()
    return RegisterConfig(
        capacity_bytes=_positive_int(source, CAPACITY_BYTES_ENV, DEFAULT_CAPACITY_BYTES),
        capacity_threshold=threshold,
        bytes_per_row=_positive_int(source, BYTES_PER_ROW_ENV, DEFAULT_BYTES_PER_ROW),
        group_max_rows=_positive_int(source, GROUP_MAX_ROWS_ENV, DEFAULT_GROUP_MAX_ROWS),
        archive_bucket=bucket if _BUCKET.fullmatch(bucket) else "")


__all__ = ["ARCHIVE_BUCKET_ENV", "BYTES_PER_ROW_ENV", "CAPACITY_BYTES_ENV", "CAPACITY_THRESHOLD_ENV",
           "DEFAULT_BYTES_PER_ROW", "DEFAULT_CAPACITY_BYTES", "DEFAULT_CAPACITY_THRESHOLD",
           "DEFAULT_GROUP_MAX_ROWS", "FULL_REGISTER_ROWS", "GROUP_MAX_ROWS_ENV", "NON_REGISTER_BASE_BYTES",
           "RegisterConfig", "load"]
