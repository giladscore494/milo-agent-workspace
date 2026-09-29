"""The REGISTER_COVERAGE line of the `gates` workflow (PR-D1, D1-6).

    python -m backend.catalog.register.coverage [--service-json PATH] < coverage.json

stdin is `public.catalog_register_coverage()` (read-only, as the read-only
role). The capacity bound is the API's own: MILO_DB_CAPACITY_BYTES and
MILO_DB_CAPACITY_THRESHOLD read from the API service's plain environment
(`gcloud run services describe --format=json`, given as --service-json), and
the reviewed defaults when that cannot be read. Prints one line:

    REGISTER_COVERAGE=INFO directory <version>; units a/b; rows c/d; database x/y bytes; unverified snapshots n
    REGISTER_COVERAGE=FAIL ...   the database is above the capacity threshold

Informational, except FAIL above the threshold (exit 1). Counts, sizes and the
directory's content hash only -- nothing else is read, and nothing is secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping, Sequence

from backend.catalog.register import config as register_config

_CONFIG_KEYS = (register_config.CAPACITY_BYTES_ENV, register_config.CAPACITY_THRESHOLD_ENV)


def service_env(document: Any) -> dict[str, str]:
    """The capacity keys among a Cloud Run service's PLAIN env entries."""

    def containers(node: Any) -> list[Any]:
        if isinstance(node, Mapping):
            if isinstance(node.get("containers"), list) and node["containers"]:
                return node["containers"]
            for child in node.values():
                found = containers(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = containers(child)
                if found:
                    return found
        return []

    found = containers(document.get("spec", document) if isinstance(document, Mapping) else document)
    env = (found[0].get("env") if found and isinstance(found[0], Mapping) else None) or []
    return {str(entry["name"]): str(entry["value"]) for entry in env
            if isinstance(entry, Mapping) and entry.get("name") in _CONFIG_KEYS and "value" in entry}


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def coverage_line(coverage: Mapping[str, Any], config: register_config.RegisterConfig,
                  *, config_source: str) -> tuple[str, str]:
    """(result, detail): result is INFO or FAIL (database above the threshold)."""
    database = _count(coverage.get("database_bytes"))
    limit = config.capacity_limit_bytes
    version = coverage.get("register_version")
    directory = version if isinstance(version, str) and len(version) == 64 else "not read yet"
    detail = (f"directory {directory}; "
              f"units {_count(coverage.get('units_captured'))}/{_count(coverage.get('units_total'))}; "
              f"rows {_count(coverage.get('rows_captured'))}/{_count(coverage.get('rows_total'))}; "
              f"database {database}/{limit} bytes ({config.capacity_threshold:.2f} of "
              f"{config.capacity_bytes}, {config_source}); "
              f"unverified snapshots {_count(coverage.get('unverified_snapshots'))}")
    return ("FAIL" if database > limit else "INFO"), detail


def main(argv: Sequence[str] | None = None, stdin: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.catalog.register.coverage")
    parser.add_argument("--service-json", default=None)
    args = parser.parse_args(list(argv or []))
    source = "reviewed defaults"
    env: dict[str, str] = {}
    if args.service_json:
        try:
            with open(args.service_json, encoding="utf-8") as handle:
                env = service_env(json.load(handle))
            source = "the API's configuration"
        except (OSError, ValueError):
            env, source = {}, "reviewed defaults; the API's configuration was unreadable"
    try:
        coverage = json.loads((stdin or sys.stdin).read())
        if not isinstance(coverage, Mapping):
            raise ValueError
    except ValueError:
        print("REGISTER_COVERAGE=INFO not available (the coverage read did not return a document)")
        return 0
    result, detail = coverage_line(coverage, register_config.load(env), config_source=source)
    print(f"REGISTER_COVERAGE={result} {detail}")
    return 1 if result == "FAIL" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
