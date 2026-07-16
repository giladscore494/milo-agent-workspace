"""Exact, unambiguous Upstash production database selection and validation.

Parsing relies ONLY on fields documented by the official Upstash Developer API
(https://upstash.com/docs/devops/developer-api/redis/*):

  list response   : a JSON ARRAY of database objects;
  database object : database_id, database_name, region (may be "global"),
                    primary_region (global databases), state, tls, endpoint,
                    rest_token, ... — `platform` is a create-REQUEST parameter
                    and is NEVER expected in a response.

Two modes, both line-protocol on stdout (never prints a token or management
key — only ids, names, non-secret metadata and the canonical REST URL):

  --select   : from a databases-list JSON, choose the production database by
               EXACT id (source of truth) or EXACT, case-sensitive name.
               Prints `SELECT|<id>` / `CREATE|` / `BLOCKED|<reason>`.
  --validate : from a single-database detail JSON, verify it is a safe active
               production database and normalize its canonical REST URL.
               Prints `OK|<canonical_rest_url>` / `BLOCKED|<reason>`.

No substring matching. More than one exact match is BLOCKED (never "first").
Names/metadata indicating a non-production database are rejected. Missing or
null documented fields are BLOCKED (fail closed) — undocumented fields are
never consulted, and their absence can never block.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

REJECT_TOKENS = ("dev", "test", "staging", "preview", "backup", "old", "archive")
# The only documented Upstash REST endpoint host shape: <labels>.upstash.io.
_UPSTASH_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*\.upstash\.io$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
# The documented healthy state is exactly "active"; anything else (deleted,
# hidden, archived, ...) is rejected fail-closed for production use.
_VALID_STATES = ("active",)


def _name_of(db: dict) -> str:
    # Documented field only (`database_name`); no undocumented fallback.
    return str(db.get("database_name") or "")


def _id_of(db: dict) -> str:
    # Documented field only (`database_id`); no undocumented fallback.
    return str(db.get("database_id") or "")


def _rejected(name: str) -> bool:
    low = name.lower()
    # word-boundary-ish token match so "production" never trips on "prod".
    return any(re.search(rf"(^|[^a-z]){t}([^a-z]|$)", low) for t in REJECT_TOKENS)


def canonical_rest_url(endpoint: str) -> str:
    """Return the canonical https REST URL, or '' when the input is malformed.

    Fail-closed: accepts ONLY a documented Upstash REST host (`*.upstash.io`) or
    a bare endpoint slug (normalized to `<slug>.upstash.io`, matching the
    documented `endpoint` field which may be either form). Any other host, a
    non-https scheme, or an endpoint carrying userinfo, a port, a path, a query
    string or a fragment is rejected.
    """
    val = endpoint.strip()
    if not val:
        return ""
    if val.startswith("http://"):
        return ""  # TLS is mandatory
    rest = val[len("https://"):] if val.startswith("https://") else val
    # Reject anything beyond a bare host: userinfo (@), port (:), path (/),
    # query (?), fragment (#) or whitespace.
    if any(c in rest for c in "@:/?# \t"):
        return ""
    host = rest.strip(".")
    if _UPSTASH_HOST_RE.match(host):
        return f"https://{host}"
    if _SLUG_RE.match(host):
        return f"https://{host}.upstash.io"
    return ""


def do_select(list_json: str, expected_name: str, db_id: str) -> str:
    try:
        dbs = json.loads(list_json)
    except Exception:
        return "BLOCKED|databases listing was not valid JSON"
    # The documented list response is a JSON array of database objects.
    if not isinstance(dbs, list):
        return "BLOCKED|databases listing was not the documented JSON array of databases (unexpected shape)"
    if not all(isinstance(db, dict) for db in dbs):
        return "BLOCKED|databases listing contained a non-object entry (unexpected shape)"
    if db_id:
        for db in dbs:
            if _id_of(db) == db_id:
                return f"SELECT|{db_id}"
        return f"BLOCKED|explicit database id '{db_id}' not found in the account"
    exact = [db for db in dbs if _name_of(db) == expected_name]
    if len(exact) == 0:
        return "CREATE|"
    if len(exact) > 1:
        return f"BLOCKED|{len(exact)} databases exactly named '{expected_name}'; refusing to guess (never select the first)"
    db = exact[0]
    if _rejected(_name_of(db)):
        return f"BLOCKED|selected database name '{_name_of(db)}' indicates a non-production database"
    selected = _id_of(db)
    if not selected:
        return "BLOCKED|selected database entry has no documented 'database_id' field"
    return f"SELECT|{selected}"


def do_validate(detail_json: str, expected_name: str, expected_region: str) -> str:
    try:
        db = json.loads(detail_json)
    except Exception:
        return "BLOCKED|database detail was not valid JSON"
    if not isinstance(db, dict):
        return "BLOCKED|unexpected database detail shape"
    name = _name_of(db)
    if not name:
        return "BLOCKED|database detail is missing the documented 'database_name' field"
    if _rejected(name):
        return f"BLOCKED|database name '{name}' indicates a non-production database"
    if expected_name and name != expected_name:
        return f"BLOCKED|database name '{name}' does not exactly equal expected '{expected_name}'"

    # Fail-closed on missing/null DOCUMENTED metadata: state, tls, region and
    # endpoint MUST be present and correct. Undocumented fields (e.g. a
    # `platform` echo of the create request) are never consulted.
    raw_state = db.get("state")
    if raw_state is None:
        return "BLOCKED|database detail is missing the documented 'state' field"
    state = str(raw_state).lower()
    if state not in _VALID_STATES:
        return f"BLOCKED|database state is '{state}', not active"

    if db.get("tls") is not True:
        return "BLOCKED|database 'tls' is missing or not exactly true; TLS is mandatory"

    raw_region = db.get("region")
    if raw_region is None or not str(raw_region).strip():
        return "BLOCKED|database detail is missing the documented 'region' field"
    region = str(raw_region).lower()
    if region == "global":
        raw_primary = db.get("primary_region")
        if raw_primary is None or not str(raw_primary).strip():
            return "BLOCKED|global database detail is missing the documented 'primary_region' field"
        if expected_region and str(raw_primary).lower() != expected_region.lower():
            return f"BLOCKED|global database primary region '{raw_primary}' does not equal expected '{expected_region}'"
    elif expected_region and region != expected_region.lower():
        return f"BLOCKED|database region '{region}' does not equal expected '{expected_region}'"

    url = canonical_rest_url(str(db.get("endpoint") or ""))
    if not url:
        return "BLOCKED|could not normalize a canonical https *.upstash.io REST URL from the documented database 'endpoint' (malformed/foreign host)"
    return f"OK|{url}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["select", "validate"], required=True)
    p.add_argument("--json", required=True, help="list JSON (select) or detail JSON (validate)")
    p.add_argument("--expected-name", default="milo-production")
    p.add_argument("--database-id", default="")
    p.add_argument("--expected-region", default="us-central1")
    args = p.parse_args()
    if args.mode == "select":
        print(do_select(args.json, args.expected_name, args.database_id))
    else:
        print(do_validate(args.json, args.expected_name, args.expected_region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
