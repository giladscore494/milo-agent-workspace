"""R4: ONE bounded correction round, built from what verification discovered.

Verification is the last thing a Swarm V2 run does, so anything it discovers
used to be terminal: an unresolved contradiction, a claim whose source
captured no evidence and a claim the evidence did not support all went
straight into `needs_review` with no chance for the run to go and look.  The
Commander never heard about them, because the replan loop had already ended.

This module closes that gap WITHOUT adding an orchestrator.  It produces two
things and nothing else:

*   `correction_issues` -- a compact, bounded, structured summary of what
    verification found: the identity and scope in question, the competing
    values and units, the source and source-version references, whether the
    evidence was missing entirely, and a short backend-authored explanation.
*   `correction_allowance` -- whether the run may spend ONE such round, given
    the task, tool-call, model-call, retry, cost and replan budgets that
    already exist.

The engine takes it from there through the SAME Commander, PlanValidator,
executor, budget, retry, checkpoint and cancellation path every other round
uses.  There is no second orchestrator, no open-ended agent loop and no
"keep going until it works": the allowance is exactly one round for the whole
run, and when it is spent (or refused) the remaining issues become
`needs_review` and the run finalizes under the R1 outcome contract.

Pure module: no database access, no provider call, no tool execution, no
global mutable state.  Everything it emits is bounded and backend-authored --
no source text, no provider payload, no model prose and no chain of thought
can travel in a correction summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .comparison import reference_identity
from .normalization import canonical_value_key

#: The whole-run allowance.  Not a per-phase or per-issue budget: ONE.
MAX_CORRECTION_ROUNDS = 1

# Deterministic bounds on the summary itself.  A correction summary becomes a
# model prompt and durable checkpoint state, so it is bounded before it can be
# either -- by issue count, by claims per issue, by the size of one quoted
# value and by the serialized size of the whole thing.
MAX_CORRECTION_ISSUES = 12
MAX_CORRECTION_CLAIMS_PER_ISSUE = 6
MAX_CORRECTION_VALUE_CHARS = 120
MAX_CORRECTION_SUMMARY_JSON_BYTES = 8_192

# The pre-flight model-call floor for one correction round: the Commander
# decision that starts it, at least one worker call inside it, and the
# Commander decision that ends it.  Like every other floor in this engine it is
# a refusal-to-start check -- BudgetTracker stays the sole authority that
# refuses an individual call.
MIN_CORRECTION_MODEL_CALLS = 3

#: What verification discovered.  Static, code-owned and finite: an issue code
#: never carries a value, a source excerpt or any provider material.
CORRECTION_ISSUE_CODES = frozenset({
    "R4_ISSUE_UNRESOLVED_CONFLICT",
    "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_ISSUE_MISSING_EVIDENCE",
    "R4_ISSUE_UNVERIFIED",
})

#: The short, backend-authored explanation attached to each issue.  These are
#: the ONLY prose a correction summary carries, and they are constants.
CORRECTION_ISSUE_SUMMARIES: Mapping[str, str] = {
    "R4_ISSUE_UNRESOLVED_CONFLICT": (
        "Two or more claims state different values for exactly this identity and scope, "
        "and no source that is authoritative for this field settles it. Research a source "
        "that is authoritative for this field and states this exact identity and scope."),
    "R4_ISSUE_STRUCTURED_MISMATCH": (
        "The claim does not match the structured fact its own source records for this "
        "identity and scope. Re-read the value, the unit and the scope from a source that "
        "states them explicitly."),
    "R4_ISSUE_MISSING_EVIDENCE": (
        "No durable evidence was captured from this claim's source, so the claim can be "
        "neither supported nor contradicted. Acquire evidence for this exact identity, "
        "field and scope."),
    "R4_ISSUE_UNVERIFIED": (
        "The captured evidence does not establish this claim for its exact identity and "
        "scope. Acquire evidence that states this field for this identity and scope."),
}

#: Which durable verdict reason maps to which issue.  A reason this map does
#: not name is not a verifier-discovered gap and never triggers a round.
_ISSUE_BY_REASON: Mapping[str, str] = {
    "unresolved conflict": "R4_ISSUE_UNRESOLVED_CONFLICT",
    "SOURCE_CONTEXT_UNAVAILABLE": "R4_ISSUE_MISSING_EVIDENCE",
    "R4_VALUE_MISMATCH": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_VALUE_NOT_COMPARABLE": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_UNIT_MISSING": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_UNIT_NOT_CONVERTIBLE": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_FIELD_NOT_IN_SOURCE": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_SCOPE_MISMATCH": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_IDENTITY_MISMATCH": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_SOURCE_VERSION_MISMATCH": "R4_ISSUE_STRUCTURED_MISMATCH",
    "R4_AMBIGUOUS_SOURCE_FACT": "R4_ISSUE_UNRESOLVED_CONFLICT",
    "source evidence is insufficient or ambiguous": "R4_ISSUE_UNVERIFIED",
    "source evidence does not support claim": "R4_ISSUE_UNVERIFIED",
    "verifier omitted claim": "R4_ISSUE_UNVERIFIED",
}

#: The order issues are reported in: a contradiction the run could close is
#: more actionable than a claim that merely did not verify.
_ISSUE_PRIORITY = ("R4_ISSUE_UNRESOLVED_CONFLICT", "R4_ISSUE_STRUCTURED_MISMATCH",
                   "R4_ISSUE_MISSING_EVIDENCE", "R4_ISSUE_UNVERIFIED")

#: Why a correction round was refused.  Static and durable-safe.
CORRECTION_BLOCK_REASONS = frozenset({
    "R4_CORRECTION_ALLOWANCE_SPENT",
    "R4_CORRECTION_NO_TASK_BUDGET",
    "R4_CORRECTION_NO_TOOL_CALL_BUDGET",
    "R4_CORRECTION_NO_MODEL_CALL_BUDGET",
    "R4_CORRECTION_NO_RETRY_BUDGET",
    "R4_CORRECTION_NO_COST_BUDGET",
    "R4_CORRECTION_NO_REPLAN_BUDGET",
})


@dataclass(frozen=True)
class CorrectionAllowance:
    """Whether the run may spend its one correction round, and why not."""

    allowed: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.allowed != (self.reason is None):
            raise ValueError("a refused correction round names exactly one static reason")
        if self.reason is not None and self.reason not in CORRECTION_BLOCK_REASONS:
            raise ValueError("correction block reason must come from the static allowlist")


def correction_allowance(*, rounds_used: int, remaining: Any, replans_used: int,
                         max_replans: int) -> CorrectionAllowance:
    """Decide whether ONE more bounded correction round may start.

    Every existing budget is consulted and the FIRST unavailable one refuses:
    the whole-run allowance, then tasks, tool calls, model calls, semantic
    retries, cost units and finally the plan's own replan allowance -- a
    correction round IS a replan and is charged as one, so a plan that
    permits none never gets an extra round through this door.

    This is a pre-flight floor, exactly like the engine's plan feasibility
    check.  BudgetTracker remains the single hard authority that refuses an
    individual call, and a refusal there is never laundered into a verdict.
    """
    if rounds_used >= MAX_CORRECTION_ROUNDS:
        return CorrectionAllowance(False, "R4_CORRECTION_ALLOWANCE_SPENT")
    if remaining.tasks < 1:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_TASK_BUDGET")
    if remaining.tool_calls < 1:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_TOOL_CALL_BUDGET")
    if remaining.model_calls < MIN_CORRECTION_MODEL_CALLS:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_MODEL_CALL_BUDGET")
    if getattr(remaining, "retries", 1) < 1:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_RETRY_BUDGET")
    if remaining.cost_units < 1:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_COST_BUDGET")
    if replans_used >= max_replans:
        return CorrectionAllowance(False, "R4_CORRECTION_NO_REPLAN_BUDGET")
    return CorrectionAllowance(True)


def _bounded_value(value: Any) -> tuple[Any, bool]:
    """One competing value, or a marker when it is too large to carry."""
    encoded = canonical_value_key(value)
    return (value, False) if len(encoded) <= MAX_CORRECTION_VALUE_CHARS else (None, True)


def _claim_entry(reference: Any, verdict: Any) -> dict[str, Any]:
    """One competing claim, as identifiers, values and static codes only."""
    value, omitted = _bounded_value(reference.value)
    entry: dict[str, Any] = {
        "claim_id": reference.claim_id, "source_id": reference.source_id,
        "task_id": reference.task_id, "value": value, "unit": reference.unit,
        "verdict": None if verdict is None else verdict.verdict,
        "reason": None if verdict is None else verdict.reason,
    }
    if reference.source_version is not None:
        entry["source_version"] = reference.source_version
    if omitted:
        entry["value_omitted"] = True
    return entry


def correction_issues(references: Iterable[Any], verdicts: Iterable[Any]) -> list[dict[str, Any]]:
    """The compact structured issue summary ONE correction round is built from.

    Claims are grouped by their COMPLETE R4 identity (entity, field,
    geography, market, time/model-year scope and every stated identity
    dimension), so the Commander is told which exact thing is unresolved
    rather than "the model is unclear".  Each issue names the competing values
    and units, the sources and source versions behind them, and a static
    backend-authored explanation of what would settle it.

    Every string in the result is either an identifier the backend resolved or
    a constant from this module.  No fragment text, prompt, provider payload,
    exception message or model prose can reach it.
    """
    by_claim = {item.claim_id: item for item in verdicts}
    grouped: dict[tuple[str, Any], list[Any]] = {}
    for reference in sorted(references, key=lambda item: item.claim_id):
        verdict = by_claim.get(reference.claim_id)
        if verdict is None or verdict.verdict == "verified":
            continue
        code = _ISSUE_BY_REASON.get(verdict.reason)
        if code is None:
            # A verdict this contract does not treat as a discoverable gap
            # (an unsupported claim, a superseded loser) is not a research
            # instruction: the run already knows the answer for it.
            continue
        grouped.setdefault((code, reference_identity(reference)), []).append(reference)

    issues: list[dict[str, Any]] = []
    for (code, identity), claims in grouped.items():
        first = claims[0]
        issues.append({
            "code": code,
            "entity": first.entity,
            "field": first.field,
            "scope": {"geography": first.geography, "market": first.market,
                      "time_scope": dict(first.time_scope or {})},
            "identity": dict(first.identity or {}),
            "claims": [_claim_entry(item, by_claim.get(item.claim_id))
                       for item in claims[:MAX_CORRECTION_CLAIMS_PER_ISSUE]],
            "claim_count": len(claims),
            "missing_evidence": code == "R4_ISSUE_MISSING_EVIDENCE",
            "summary": CORRECTION_ISSUE_SUMMARIES[code],
        })
    issues.sort(key=lambda item: (_ISSUE_PRIORITY.index(item["code"]), item["entity"],
                                  item["field"], item["claims"][0]["claim_id"]))
    return issues[:MAX_CORRECTION_ISSUES]


def correction_summary(issues: Sequence[Mapping[str, Any]], *,
                       resolutions: Iterable[Any] = ()) -> dict[str, Any]:
    """The bounded payload the Commander receives for its one correction round.

    It carries the issues, the open contradictions as typed decisions (so the
    Commander sees that a conflict was CONSIDERED and left open rather than
    never examined) and the remaining-round count.  The whole payload is size
    bounded before it can become a prompt or durable state; if it does not
    fit, issues are dropped from the least actionable end rather than being
    truncated mid-structure.
    """
    open_conflicts = [{"scope_hash": item.scope_hash, "entity": item.entity,
                       "field": item.field, "reason": item.reason,
                       "policy_version": item.policy_version,
                       "claim_ids": list(item.claim_ids)}
                      for item in resolutions if item.state == "unresolved"]
    kept = list(issues)
    while True:
        payload = {"issues": kept, "open_conflicts": open_conflicts[:MAX_CORRECTION_ISSUES],
                   "correction_rounds_remaining": MAX_CORRECTION_ROUNDS}
        if len(canonical_value_key(payload).encode()) <= MAX_CORRECTION_SUMMARY_JSON_BYTES:
            return payload
        if kept:
            kept = kept[:-1]
            continue
        if open_conflicts:
            open_conflicts = open_conflicts[:-1]
            continue
        return {"issues": [], "open_conflicts": [],
                "correction_rounds_remaining": MAX_CORRECTION_ROUNDS}


__all__ = ["CORRECTION_BLOCK_REASONS", "CORRECTION_ISSUE_CODES",
           "CORRECTION_ISSUE_SUMMARIES", "MAX_CORRECTION_CLAIMS_PER_ISSUE",
           "MAX_CORRECTION_ISSUES", "MAX_CORRECTION_ROUNDS",
           "MAX_CORRECTION_SUMMARY_JSON_BYTES", "MAX_CORRECTION_VALUE_CHARS",
           "MIN_CORRECTION_MODEL_CALLS", "CorrectionAllowance", "correction_allowance",
           "correction_issues", "correction_summary"]
