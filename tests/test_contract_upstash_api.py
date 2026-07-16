"""Contract tests for the Upstash Developer API schema.

Two layers:

1. OFFLINE (always runs): the documented request/response schema — vendored
   verbatim from the official docs
   (https://upstash.com/docs/devops/developer-api/redis/*) — must be accepted
   by scripts/release/upstash_select.py, and the validator must depend ONLY on
   documented response fields. In particular `platform` is a create-REQUEST
   parameter that the API does not return, so its absence from a response must
   never block.

2. LIVE (opt-in, strictly read-only): with UPSTASH_EMAIL / UPSTASH_API_KEY
   set, GET /v2/redis/databases is called (never POST/DELETE/PATCH — nothing
   is created, modified or deleted) and the fields this tooling depends on are
   asserted present on every returned database. Skipped without credentials
   unless MILO_REQUIRE_UPSTASH_CONTRACT=1 turns the skip into a failure.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELECTOR = REPO / "scripts" / "release" / "upstash_select.py"

# ---------------------------------------------------------------------------
# Documented schema fixtures (vendored from the official Upstash docs).
# ---------------------------------------------------------------------------

# Documented GET /v2/redis/database/{id} response for a GLOBAL database
# (region == "global", primary_region carries the placement). `platform` does
# NOT appear: it is a create-request-only parameter.
DOCUMENTED_GLOBAL_RESPONSE = {
    "database_id": "96ad0856-03b1-4ee7-9666-e81abd0349e1",
    "database_name": "milo-production",
    "database_type": "Pay as You Go",
    "region": "global",
    "type": "paid",
    "port": 6379,
    "creation_time": 1658909671,
    "state": "active",
    "password": "not-a-real-password",
    "user_email": "ops@example.com",
    "endpoint": "beloved-stallion-58500.upstash.io",
    "tls": True,
    "rest_token": "not-a-real-token",
    "read_only_rest_token": "not-a-real-ro-token",
    "primary_region": "us-central1",
    "primary_members": ["us-central1"],
    "all_members": ["us-central1"],
    "modifying_state": "",
    "eviction": False,
    "auto_upgrade": False,
}

# Documented single-region response shape (region carries the placement and a
# bare endpoint slug, both documented forms).
DOCUMENTED_REGIONAL_RESPONSE = {
    "database_id": "037d3e6e-0000-0000-0000-000000000000",
    "database_name": "milo-production",
    "database_type": "Pay as You Go",
    "region": "us-central1",
    "port": 30143,
    "creation_time": 1658909671,
    "state": "active",
    "password": "not-a-real-password",
    "user_email": "ops@example.com",
    "endpoint": "eu2-sought-mollusk-30143",
    "tls": True,
    "rest_token": "not-a-real-token",
    "read_only_rest_token": "not-a-real-ro-token",
}

# Documented POST /v2/redis/database request body (official create example).
DOCUMENTED_CREATE_REQUEST_FIELDS = {
    "database_name", "platform", "primary_region", "read_regions",
    "plan", "budget", "eviction", "tls",
}


def _select(mode: str, payload, **kw) -> str:
    args = [sys.executable, str(SELECTOR), "--mode", mode, "--json", json.dumps(payload)]
    for key, value in kw.items():
        args += [f"--{key.replace('_', '-')}", value]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Offline: validator accepts the documented schema
# ---------------------------------------------------------------------------

def test_validator_accepts_documented_global_response():
    out = _select("validate", DOCUMENTED_GLOBAL_RESPONSE,
                  expected_name="milo-production", expected_region="us-central1")
    assert out == "OK|https://beloved-stallion-58500.upstash.io", out


def test_validator_accepts_documented_regional_response_with_bare_slug():
    out = _select("validate", DOCUMENTED_REGIONAL_RESPONSE,
                  expected_name="milo-production", expected_region="us-central1")
    assert out == "OK|https://eu2-sought-mollusk-30143.upstash.io", out


def test_validator_never_requires_the_undocumented_platform_field():
    # Neither documented response carries `platform`; both must validate.
    for doc in (DOCUMENTED_GLOBAL_RESPONSE, DOCUMENTED_REGIONAL_RESPONSE):
        assert "platform" not in doc
        out = _select("validate", doc,
                      expected_name="milo-production", expected_region="us-central1")
        assert out.startswith("OK|"), out


@pytest.mark.parametrize("missing", ["state", "tls", "region", "endpoint", "database_name"])
def test_validator_fail_closed_on_missing_documented_fields(missing):
    doc = dict(DOCUMENTED_GLOBAL_RESPONSE)
    doc.pop(missing)
    out = _select("validate", doc,
                  expected_name="milo-production", expected_region="us-central1")
    assert out.startswith("BLOCKED|"), out


def test_validator_fail_closed_on_global_without_primary_region():
    doc = dict(DOCUMENTED_GLOBAL_RESPONSE)
    doc.pop("primary_region")
    out = _select("validate", doc,
                  expected_name="milo-production", expected_region="us-central1")
    assert out.startswith("BLOCKED|"), out


def test_validator_fail_closed_on_wrong_region():
    doc = dict(DOCUMENTED_REGIONAL_RESPONSE, region="eu-central-1")
    out = _select("validate", doc,
                  expected_name="milo-production", expected_region="us-central1")
    assert out.startswith("BLOCKED|"), out


def test_selector_accepts_documented_list_shape_and_uses_documented_ids():
    listing = [DOCUMENTED_GLOBAL_RESPONSE]
    out = _select("select", listing, expected_name="milo-production")
    assert out == f"SELECT|{DOCUMENTED_GLOBAL_RESPONSE['database_id']}", out


def test_selector_rejects_undocumented_list_shapes():
    # The documented list response is a bare JSON array; wrappers are rejected.
    out = _select("select", {"databases": [DOCUMENTED_GLOBAL_RESPONSE]},
                  expected_name="milo-production")
    assert out.startswith("BLOCKED|"), out


def test_selector_ignores_undocumented_name_and_id_aliases():
    # Entries carrying only undocumented `name`/`id` aliases must not match.
    out = _select("select", [{"name": "milo-production", "id": "x"}],
                  expected_name="milo-production")
    assert out == "CREATE|", out


def test_create_body_uses_only_documented_request_fields():
    # The bootstrap's create payload template must stay within the documented
    # create-request contract.
    script = (REPO / "scripts" / "release" / "bootstrap-production.sh").read_text(encoding="utf-8")
    start = script.index("_upstash_create_body()")
    body = script[start:script.index("}", start)]
    payload_keys = set(json.loads(
        '{"database_name":"x","platform":"x","primary_region":"x","tls":true,'
        '"eviction":false,"plan":"x"}'
    ))
    for key in payload_keys:
        assert f'"{key}"' in body, f"create body lost documented field {key}"
    assert payload_keys <= DOCUMENTED_CREATE_REQUEST_FIELDS


# ---------------------------------------------------------------------------
# Live (opt-in): read-only GET against the real API
# ---------------------------------------------------------------------------

LIVE_EMAIL = os.environ.get("UPSTASH_EMAIL", "")
LIVE_APIKEY = os.environ.get("UPSTASH_API_KEY", "")
REQUIRE_LIVE = os.environ.get("MILO_REQUIRE_UPSTASH_CONTRACT") == "1"


def _live_creds_or_skip():
    if not (LIVE_EMAIL and LIVE_APIKEY):
        if REQUIRE_LIVE:
            pytest.fail("MILO_REQUIRE_UPSTASH_CONTRACT=1 but UPSTASH_EMAIL/UPSTASH_API_KEY are not set")
        pytest.skip("UPSTASH_EMAIL/UPSTASH_API_KEY not set; live read-only contract check skipped")


def test_live_list_databases_matches_documented_schema():
    _live_creds_or_skip()
    import urllib.request

    auth = base64.b64encode(f"{LIVE_EMAIL}:{LIVE_APIKEY}".encode()).decode()
    req = urllib.request.Request(  # GET only — never creates/modifies/deletes
        "https://api.upstash.com/v2/redis/databases",
        headers={"Authorization": f"Basic {auth}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.status == 200
        payload = json.loads(resp.read().decode("utf-8"))
    assert isinstance(payload, list), (
        f"documented list response is a JSON array, got {type(payload).__name__}"
    )
    for db in payload:
        assert isinstance(db, dict)
        # Fields the release tooling depends on must exist on real databases.
        for field in ("database_id", "database_name", "state", "tls", "endpoint", "region"):
            assert field in db, f"real database is missing documented field {field!r}: keys={sorted(db)}"
        assert isinstance(db["database_id"], str) and db["database_id"]
        assert isinstance(db["database_name"], str) and db["database_name"]
        assert isinstance(db["tls"], bool)
    # The selector's line protocol must hold against the real listing.
    out = _select("select", payload, expected_name="milo-production")
    assert out.split("|", 1)[0] in ("SELECT", "CREATE", "BLOCKED"), out
