#!/usr/bin/env python3
"""Operator tool: rebuild the variant coverage ledger from run history (PR-Z).

The ledger (`catalog_variant_coverage`, migration 20260927000100) is an INDEX
over finished Mapping Plan batch runs: which register variant each one
enriched, left unresolved, or failed to settle, at which level. The worker
writes it when a run finishes; this tool rebuilds it from the same durable
data for runs that finished before the ledger existed, or after a write that
failed (a failed ledger write never changes a run's outcome, so it is only
ever repaired here).

Usage (operator workstation, service credentials in the environment)::

    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python scripts/catalog/backfill_variant_coverage.py [--dry-run] [--run-id <uuid> ...]

What it reads, and what it never reads
--------------------------------------

Keyset pages of at most 50 finished (`completed` / `partial_success`) batch
runs, oldest first; per run, ONE `runs.output`, ONE latest checkpoint (its
preparation record) and one bounded snapshot page per distinct identity the
result names. Never the whole ledger and never a whole snapshot.

Idempotent: each run is applied through `rebuild_catalog_variant_coverage`,
the same derivation and the same never-weaken upsert the finalize path uses,
in the order the runs finished -- so a second backfill changes nothing. It
writes the ledger and nothing else; no run, evidence row or source value is
touched. No model is called and no Government transport is constructed.

Prints one JSON report: runs seen / recorded, entries, per-reason skips and
the per-run status counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.catalog.coverage import (BATCH_COVERAGE_LEVEL, COVERAGE_LEVELS,  # noqa: E402
                                      MAX_BACKFILL_PAGE, backfill)


def main(argv: list[str] | None = None, *, repository: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--level", default=BATCH_COVERAGE_LEVEL, choices=COVERAGE_LEVELS)
    parser.add_argument("--page-size", type=int, default=MAX_BACKFILL_PAGE)
    parser.add_argument("--run-id", action="append", default=None,
                        help="rebuild only these runs (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="derive and report, write nothing")
    args = parser.parse_args(argv)
    if repository is None:
        from backend.dependencies import get_repository

        repository = get_repository()
    report = backfill(repository, level=args.level, page_size=args.page_size,
                      dry_run=args.dry_run, run_ids=args.run_id)
    print(json.dumps({"dry_run": bool(args.dry_run), "level": args.level,
                      **report.as_record()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
