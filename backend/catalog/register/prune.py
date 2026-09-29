"""The retention entrypoint (PR-D1, D1-5): ``python -m backend.catalog.register.prune``.

Runs on the capture job's image and identity (service-role database access),
executed by scripts/ops/register-retention.sh with argument overrides:

    --list                                   the prunable list and its digest
    --apply --confirm PRUNE --digest <hex>   prune EXACTLY that list

`--apply` recomputes the list, refuses unless its digest is the one given,
and hands both to `public.prune_register_snapshots`, which recomputes and
refuses again under a table lock. Database rows only: an archive object is
never touched. Prints snapshot keys, counts and the digest -- nothing else.
Exit 0 on success, 2 on a refusal, 1 on a failure.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Mapping, Sequence

from backend.catalog.register.retention import CONFIRM_WORD, prune_digest
from backend.errors import AppError

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _open_repository() -> Any:  # pragma: no cover - production wiring
    from backend.config import get_settings
    from backend.repository import SupabaseRepository

    return SupabaseRepository(get_settings())


def listing(repository: Any) -> tuple[list[Mapping[str, Any]], str]:
    rows = list(repository.prunable_register_snapshots())
    return rows, prune_digest(str(row["snapshot_key"]) for row in rows)


def main(argv: Sequence[str] | None = None, *, repository: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.catalog.register.prune")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default=None)
    parser.add_argument("--digest", default=None)
    args, extra = parser.parse_known_args(list(argv or []))
    if extra or (args.list and (args.confirm is not None or args.digest is not None)):
        print("REFUSED CATALOG_PRUNE_REQUEST_INVALID: unsupported arguments")
        return EXIT_REFUSED
    if args.apply and args.confirm != CONFIRM_WORD:
        print(f"REFUSED CATALOG_PRUNE_NOT_CONFIRMED: --confirm must be exactly {CONFIRM_WORD}")
        return EXIT_REFUSED
    if args.apply and not _DIGEST.fullmatch(str(args.digest or "")):
        print("REFUSED CATALOG_PRUNE_REQUEST_INVALID: --digest must be the 64-hex digest the dry-run printed")
        return EXIT_REFUSED
    repo = repository if repository is not None else _open_repository()
    try:
        rows, digest = listing(repo)
    except Exception:
        print("FAILED CATALOG_PRUNE_LIST_UNAVAILABLE: the prunable list could not be read")
        return EXIT_FAILED
    for row in rows:
        print(f"PRUNABLE {row['snapshot_key']} rows={int(row.get('raw_rows') or 0)} "
              f"estimated_bytes={int(row.get('estimated_bytes') or 0)}")
    print(f"TOTAL snapshots={len(rows)} rows={sum(int(r.get('raw_rows') or 0) for r in rows)} "
          f"estimated_bytes={sum(int(r.get('estimated_bytes') or 0) for r in rows)}")
    print(f"DIGEST {digest}")
    if args.list:
        return EXIT_OK
    if digest != args.digest:
        print("REFUSED CATALOG_PRUNE_DIGEST_MISMATCH: the prunable list is not the one that digest names; "
              "run the dry-run again")
        return EXIT_REFUSED
    try:
        result = repo.prune_register_snapshots([str(row["snapshot_key"]) for row in rows], digest)
    except AppError as refused:
        if refused.code == "CATALOG_PRUNE_DIGEST_MISMATCH":
            print("REFUSED CATALOG_PRUNE_DIGEST_MISMATCH: the list changed before the prune; run the dry-run again")
            return EXIT_REFUSED
        print("FAILED CATALOG_PRUNE_FAILED: the prune did not complete; nothing was deleted")
        return EXIT_FAILED
    except Exception:
        print("FAILED CATALOG_PRUNE_FAILED: the prune did not complete; nothing was deleted")
        return EXIT_FAILED
    print(f"PRUNED snapshots={int(result.get('snapshots') or 0)} raw_records={int(result.get('raw_records') or 0)} "
          f"candidates={int(result.get('candidates') or 0)} (archive objects untouched)")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
