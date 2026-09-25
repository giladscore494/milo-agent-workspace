#!/usr/bin/env python3
"""Stage D SEMANTIC acceptance gate: did the run produce an acceptable product?

Stage D already proves a great deal about a run, and every one of those proofs
answers a TECHNICAL question:

* ``execution_state.py`` — did the Cloud Run execution reach a terminal
  Completed=True condition?
* ``verify_executions.py`` — are there exactly baseline+1 executions, all
  terminal?
* ``probe_db.py`` — is the run row terminal, claimed once, heartbeated,
  settled, leak-free, and in the acceptance policy's terminal set?

"The worker executed successfully" is the answer to all of them, and it is not
the answer to the only question that matters to the product: *was the result
semantically acceptable?* A run can pass every check above and have produced
nothing usable — a catalog with zero settled models, an outcome the contract
refused, a result whose every field is still outstanding. Accepting it would
be reading a green process exit as a product.

This gate asks the other question, and it asks it of the ONE authority:
``backend/product_outcome.py``. The worker records the canonical ProductOutcome
on the run's terminal event at finalization time (see
``backend/finalization.py``); the DB probe copies that bounded record into its
evidence output; this script rebuilds it through the canonical reader and
applies the canonical acceptance rule. Nothing here re-derives an outcome, so
there is no second implementation to drift.

Fail closed, in every direction:

* no evidence record, unparseable input, or a record this repository's reader
  refuses  → BLOCKED;
* a product terminal state with NO recorded ProductOutcome → BLOCKED. A run
  that finished without recording what it produced has not been shown to have
  produced anything;
* a recorded outcome that is ``unusable``, ``refused`` or ``not_produced``
  → BLOCKED, however green the execution was.

Usage:
  cat evidence.log | python3 semantic_acceptance.py
  cat evidence.log | python3 semantic_acceptance.py --require-complete

Read-only: it reads stdin, imports the canonical module, prints one structured
JSON verdict line, and exits. No network, no gcloud, no database, no mutation.
Never prints a secret value, and never prints the product payload — the
ProductOutcome record is counts, static codes and a digest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.product_outcome import (  # noqa: E402  (path bootstrap must run first)
    ProductOutcomeError, acceptance_problems, outcome_from_record)

#: Terminal run states that are supposed to carry a product. A run that ends in
#: any of these MUST have recorded a ProductOutcome; one that ends elsewhere
#: (failed, cancelled, timed_out, budget_exhausted) is a truthful non-product
#: outcome and is refused by this gate for that reason, not for a missing
#: record.
PRODUCT_TERMINAL_STATES = frozenset({"completed", "partial_success"})

OPERATOR_ACTION = (
    "Treat the Stage D run as NOT ACCEPTED. The execution may have been "
    "technically clean; the product result was not. Record the outcome in "
    "docs/production-readiness/STAGE_D_AUTHORIZATION.md and do NOT start "
    "another run to try for a better result."
)


def evidence_record(stream: object) -> dict | None:
    """The last ``stage_d_probe: evidence`` record on the stream, or None.

    The probe's output is interleaved with Cloud Logging noise, so the record
    is found the same way ``06-collect-evidence.sh`` finds it: by its marker.
    """
    found: dict | None = None
    for line in stream:  # type: ignore[union-attr]
        text = str(line).strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("stage_d_probe") == "evidence":
            found = record
    return found


def verdict(record: dict | None, *, require_complete: bool = False) -> dict:
    """The gate's structured verdict. ``ok`` is true only on real acceptance."""
    out: dict = {"stage_d_semantic_gate": "verify", "ok": False}
    if record is None:
        out["reason"] = ("no stage_d_probe=evidence record was found on stdin — "
                         "the product outcome cannot be judged; failing closed")
        return out
    run = record.get("run") if isinstance(record.get("run"), dict) else {}
    state = run.get("status")
    out["run_id"] = record.get("run_id")
    out["terminal_status"] = state
    if state not in PRODUCT_TERMINAL_STATES:
        out["reason"] = (
            f"terminal state {state!r} is not a product outcome — the run "
            "ended without producing a result (this is a truthful outcome, "
            "and it is not an acceptable one)")
        return out
    raw = record.get("product_outcome")
    if raw is None:
        out["reason"] = (
            "the run reached a product terminal state but recorded NO canonical "
            "ProductOutcome — what it produced was never stated, so it cannot "
            "be accepted; failing closed")
        return out
    try:
        outcome = outcome_from_record(raw)
    except ProductOutcomeError as exc:
        out["reason"] = (f"the recorded ProductOutcome is not one this "
                         f"repository's contract produces ({exc}); failing closed")
        return out
    out["product_outcome"] = outcome.as_record()
    problems = acceptance_problems(outcome, require_complete=require_complete)
    out["problems"] = problems
    out["semantic_status"] = outcome.semantic_status
    out["usability"] = outcome.usability
    out["ok"] = not problems
    if problems:
        out["reason"] = (
            "the worker execution may have succeeded, but the PRODUCT result "
            "was not semantically acceptable: " + "; ".join(problems))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-complete", action="store_true",
        help=("refuse a truthful `partial` outcome as well; by default a "
              "partial product with real verified content is accepted, "
              "because it is the expected shape of a first government run"))
    args = parser.parse_args(argv)
    try:
        record = evidence_record(sys.stdin)
    except Exception:  # noqa: BLE001 - an unreadable stream is a refusal
        record = None
    result = verdict(record, require_complete=args.require_complete)
    if not result["ok"]:
        result["operator_action"] = OPERATOR_ACTION
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
