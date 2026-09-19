"""PASS/FAIL verdict for a Stage D govcheck probe run, from its logs.

Reads `gcloud logging read --format=json(textPayload,jsonPayload)` on
stdin and exits 0 only when the MOST RECENT structured `govcheck` record
says `ok: true`.

The probe's job exit status is deliberately not trusted on its own: a lost
or swallowed exit code must never become a silent "the capture is fine".
Absence of any govcheck record is also a failure — an unverified posture
is not a verified one.
"""

from __future__ import annotations

import json
import sys


def records(payload: object):
    if not isinstance(payload, list):
        return
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        structured = entry.get("jsonPayload")
        if isinstance(structured, dict) and "stage_d_probe" in structured:
            yield structured
            continue
        text = entry.get("textPayload")
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and "stage_d_probe" in parsed:
            yield parsed


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("govcheck: log payload was not valid JSON — failing closed", file=sys.stderr)
        return 1
    for record in records(payload):
        if record.get("stage_d_probe") == "govcheck" and "ok" in record:
            # --order=desc, so the first govcheck record is the newest.
            if record["ok"] is True:
                return 0
            print(f"govcheck: {json.dumps(record.get('problems'), default=str)}", file=sys.stderr)
            return 1
    print("govcheck: no structured govcheck record found — the posture is UNVERIFIED; failing closed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
