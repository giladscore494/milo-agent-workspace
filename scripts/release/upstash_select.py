"""Exact, unambiguous Upstash production database selection and validation.

Two modes, both line-protocol on stdout (never prints a token or management
key — only ids, names, non-secret metadata and the canonical REST URL):

  --select   : from a databases-list JSON, choose the production database by
               EXACT id (source of truth) or EXACT, case-sensitive name.
               Prints `SELECT|<id>` / `CREATE|` / `BLOCKED|<reason>`.
  --validate : from a single-database detail JSON, verify it is a safe active
               production database and normalize its canonical REST URL.
               Prints `OK|<canonical_rest_url>` / `BLOCKED|<reason>`.

No substring matching. More than one exact match is BLOCKED (never "first").
Names/metadata indicating a non-production database are rejected.
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
_VALID_STATES = ("active", "running", "enabled")


def _db_list(obj):
    if isinstance(obj, dict):
        return obj.get("databases", obj.get("data", []))
    return obj if isinstance(obj, list) else []


def _name_of(db: dict) -> str:
    return str(db.get("database_name") or db.get("name") or "")


def _id_of(db: dict) -> str:
    return str(db.get("database_id") or db.get("id") or "")


def _rejected(name: str) -> bool:
    low = name.lower()
    # word-boundary-ish token match so "production" never trips on "prod".
    return any(re.search(rf"(^|[^a-z]){t}([^a-z]|$)", low) for t in REJECT_TOKENS)


def canonical_rest_url(endpoint: str, rest_url_field: str = "") -> str:
    """Return the canonical https REST URL, or '' when the input is malformed.

    Fail-closed: accepts ONLY a documented Upstash REST host (`*.upstash.io`) or
    a bare slug (normalized to `<slug>.upstash.io`). Any other host, a non-https
    scheme, or an endpoint carrying userinfo, a port, a path, a query string or
    a fragment is rejected.
    """
    for candidate in (rest_url_field.strip(), endpoint.strip()):
        if not candidate:
            continue
        val = candidate
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
    return ""


def do_select(list_json: str, expected_name: str, db_id: str) -> str:
    try:
        dbs = _db_list(json.loads(list_json))
    except Exception:
        return "BLOCKED|databases listing was not valid JSON"
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
    return f"SELECT|{_id_of(db)}"


def do_validate(detail_json: str, expected_name: str, expected_platform: str,
                expected_region: str) -> str:
    try:
        db = json.loads(detail_json)
    except Exception:
        return "BLOCKED|database detail was not valid JSON"
    if not isinstance(db, dict):
        return "BLOCKED|unexpected database detail shape"
    name = _name_of(db)
    if not name:
        return "BLOCKED|database detail missing a name"
    if _rejected(name):
        return f"BLOCKED|database name '{name}' indicates a non-production database"
    if expected_name and name != expected_name:
        return f"BLOCKED|database name '{name}' does not exactly equal expected '{expected_name}'"

    # Fail-closed on missing/null metadata: state, tls, platform and region MUST
    # be present and correct.
    raw_state = db.get("state", db.get("database_state"))
    if raw_state is None:
        return "BLOCKED|database detail is missing the 'state' field"
    state = str(raw_state).lower()
    if state not in _VALID_STATES:
        return f"BLOCKED|database state is '{state}', not active"

    if db.get("tls") is not True:
        return "BLOCKED|database 'tls' is missing or not exactly true; TLS is mandatory"

    raw_platform = db.get("platform", db.get("database_type"))
    if raw_platform is None or not str(raw_platform).strip():
        return "BLOCKED|database detail is missing the 'platform' field"
    if expected_platform and str(raw_platform).lower() != expected_platform.lower():
        return f"BLOCKED|database platform '{raw_platform}' does not equal expected '{expected_platform}'"

    raw_region = db.get("primary_region", db.get("region"))
    if raw_region is None or not str(raw_region).strip():
        return "BLOCKED|database detail is missing the primary region field"
    region = str(raw_region).lower()
    if expected_region and region not in (expected_region.lower(), "global"):
        return f"BLOCKED|database primary region '{region}' does not equal expected '{expected_region}'"

    url = canonical_rest_url(str(db.get("endpoint") or ""), str(db.get("rest_url") or db.get("rest_endpoint") or ""))
    if not url:
        return "BLOCKED|could not normalize a canonical https *.upstash.io REST URL from the database endpoint (malformed/foreign host)"
    return f"OK|{url}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["select", "validate"], required=True)
    p.add_argument("--json", required=True, help="list JSON (select) or detail JSON (validate)")
    p.add_argument("--expected-name", default="milo-production")
    p.add_argument("--database-id", default="")
    p.add_argument("--expected-platform", default="gcp")
    p.add_argument("--expected-region", default="us-central1")
    args = p.parse_args()
    if args.mode == "select":
        print(do_select(args.json, args.expected_name, args.database_id))
    else:
        print(do_validate(args.json, args.expected_name, args.expected_platform, args.expected_region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
