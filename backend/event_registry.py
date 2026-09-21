"""The ONE canonical event vocabulary of this repository.

Why this module exists
----------------------

The same logical question -- "is this a legitimate run event, and what may it
say?" -- was answered by four independently maintained lists that were free to
disagree, and did:

* ``backend/runtime.py`` declared ``V1_EVENT_TYPES`` and ``CATALOG_EVENT_TYPES``
  and made their union ``EVENT_TYPES`` the API's acceptance set. It named no
  Swarm V2 type at all, so ``POST /internal/runs/{id}/events`` answered 422 --
  "unknown event type" -- to ``task_started``, an event the V2 engine emits on
  every task of every run;
* the V2 engine, the executor, the worker, the budget tracker and the provider
  quota coordinator emitted 27 further types through ``SupabaseEventSink``,
  which validated NOTHING. They reached ``run_events`` durably while the
  in-process sink used by tests would have refused them and the API would have
  refused them. Three boundaries, three different answers, for one event;
* ``frontend/lib/eventVocabulary.ts`` hand-mirrored two of the backend's sets
  and hand-maintained a third (``SWARM_V2_EVENT_TYPES``) that had no backend
  counterpart to mirror. Five of the types the V2 engine really emits were
  missing from it, so they were silently filed as unknown in the browser;
* ``backend/catalog/operator_capture.py`` tested for the capture-progress type
  ``catalog_snapshot_replayed`` with a bare string literal, against a
  vocabulary declared nowhere.

That is not four bugs; it is one architectural defect. This module is the fix:
every event type this repository can emit is declared ONCE, here, together with
the projection it owns, and every surface that accepts, emits, projects or
verifies an event derives its answer from this declaration.

What a set MEANS
----------------

Membership grants a PROJECTION, and the sets are deliberately separate because
they grant different ones. This is the F5 rule and it is preserved exactly:

* :data:`V1_EVENT_TYPES` -- and only these -- may write the browser's V1
  projection: agents, lifecycle phase, progress, sources, claims, conflicts,
  checkpoints and the event-derived spend telemetry.
* :data:`SWARM_V2_EVENT_TYPES` fold into the swarm slice. Swarm V2 has no agent
  concept, so a ``task_started`` carrying an ``agent`` field is a payload making
  a claim its emitter cannot make, and it is not granted the V1 projection.
* :data:`CATALOG_EVENT_TYPES` own exactly the bounded catalog status slice.
* :data:`OPERATIONAL_EVENT_TYPES` own NOTHING. They are emitted by trusted
  server code -- the worker's retry accounting, the budget tracker's cap
  diagnostics, the provider quota coordinator's pacing signals -- which knows
  about no agent, task, phase or product. They are durable because an operator
  needs them, and they are projection-less because their emitters cannot assert
  anything a projection would render.

:data:`EVENT_TYPES` is the union of those four: the authoritative ACCEPTANCE
vocabulary, and the only set a durable append is checked against.

:data:`CAPTURE_PROGRESS_EVENT_TYPES` is deliberately NOT in that union. The
Government capture's ingestor reports progress through the same callback shape,
but ``backend/catalog/operator_capture.py`` keeps those names in memory and
writes none of them to ``run_events``. Declaring them here gives that vocabulary
a name without making any of it durable, and replaces the bare string literal
the capture used to test for.

Fail closed, at the boundaries that matter
------------------------------------------

``InMemoryEventSink`` already refused an unknown type. ``SupabaseEventSink`` --
the sink that actually writes to the database -- did not, which made the
in-process check a test-only formality. Both now refuse, and so does the API's
worker event route, so an event type this release has never heard of cannot
become durable through any path.

Nothing here redefines ProductOutcome. Events transport and project it; the
canonical outcome remains ``backend/product_outcome.py``'s alone.

Import discipline: this module imports nothing from ``backend`` at module
scope, so every emitter and every boundary can depend on it without a cycle.
"""

from __future__ import annotations

import hashlib
import json

#: The identity of this vocabulary. It is carried on a run's immutable
#: identity (``backend/run_identity.py``), so a run states which event
#: vocabulary its durable stream speaks rather than leaving a later reader to
#: assume the current one.
REGISTRY_VERSION = "milo-event-registry/1"

# ---------------------------------------------------------------------------
# V1 -- the vehicle_catalog_v1 vocabulary
# ---------------------------------------------------------------------------
#: The V1 engine's own vocabulary, and the ONLY set whose members may write the
#: browser's agent, phase, progress, source, claim, conflict and spend
#: projection. Nothing may be added to it casually: membership is an authority
#: grant, not a label.
V1_EVENT_TYPES = frozenset({
    "run_created", "run_started", "run_resumed", "phase_started", "phase_completed",
    "agent_created", "agent_started", "agent_progress", "agent_completed", "agent_failed",
    "chunk_started", "chunk_completed", "chunk_failed", "fallback_started", "fallback_completed",
    "checkpoint_saved", "cancellation_requested", "run_completed", "run_partial_success",
    "run_failed", "run_cancelled",
    "tool_access_requested", "tool_access_granted", "tool_access_denied", "tool_used",
    "source_recorded", "claim_recorded", "conflict_detected",
    "launch_requested", "launch_failed", "run_requeued",
    "budget_warning", "budget_exhausted", "token_limit_reached", "run_timed_out",
    "retry_limit_reached", "kill_switch_activated",
    "supervisor_shadow_failed",
})

#: Types that are about the RUN, not about an agent inside it. They keep every
#: other projection they own; none of them may create or mutate an agent row.
RUN_LEVEL_EVENT_TYPES = frozenset({
    "run_created", "run_started", "run_resumed", "run_completed", "run_partial_success",
    "run_failed", "run_cancelled", "run_requeued", "run_timed_out",
    "cancellation_requested", "launch_requested", "launch_failed",
    "budget_warning", "budget_exhausted", "token_limit_reached",
    "kill_switch_activated", "supervisor_shadow_failed",
})

# ---------------------------------------------------------------------------
# Swarm V2
# ---------------------------------------------------------------------------
#: Every type the Swarm V2 engine, its executor and its worker really emit.
#:
#: The last five were emitted by ``backend/engines/swarm_v2/engine.py`` and
#: appeared in no vocabulary anywhere: not in the backend's acceptance set, not
#: in the frontend's swarm set. They were written durably by the unvalidating
#: Supabase sink and filed as unknown by the browser. They are legitimate V2
#: events and they are declared here.
SWARM_V2_EVENT_TYPES = frozenset({
    # Shared run lifecycle emitted by the canonical worker around V2. These
    # intentionally overlap V1: the registry describes projection ownership,
    # and the Swarm reducer may only fold names granted here.
    "run_created", "run_started", "run_resumed", "run_completed",
    "run_partial_success", "run_failed", "run_cancelled", "run_timed_out",
    "cancellation_requested", "budget_warning", "budget_exhausted",
    "token_limit_reached", "checkpoint_saved",
    # V2 engine/executor events.
    "commander_plan_created", "commander_replanned",
    "task_ready", "task_started", "task_completed", "task_failed",
    "tool_called", "worker_output_repair_started",
    "evidence_added", "conflict_found", "grounding_context_resolved",
    "verification_batch_completed", "verification_completed",
    "conflict_resolution_recorded",
    "correction_round_started", "correction_round_blocked",
    "correction_round_declined", "correction_round_finalizing",
})

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
#: The catalog promotion path's two events, emitted by trusted server code
#: (``PromotionAttempt.as_event()``) whose payload is ids, counts, booleans and
#: static reason codes. A dedicated closed set, never folded into V1 or V2:
#: membership is what grants a projection, and these own exactly the bounded
#: catalog status slice.
CATALOG_EVENT_TYPES = frozenset({
    "catalog_variant_promoted",
    "catalog_promotion_refused",
})

# ---------------------------------------------------------------------------
# Operational / diagnostic
# ---------------------------------------------------------------------------
#: Durable operator signals that own NO projection.
#:
#: Every one of these was already being written to ``run_events`` by trusted
#: server code and named in no vocabulary. They are recognised here so a durable
#: append no longer has to bypass validation to succeed -- and given their own
#: set, rather than added to V1, so recognising them cannot hand a provider
#: pacing signal the agent, phase, progress and spend projection.
OPERATIONAL_EVENT_TYPES = frozenset({
    # backend/worker/main.py -- semantic retry accounting and provider pacing.
    "retry_limit_checked", "provider_backpressure_wait",
    # backend/provider_quota.py -- the shared coordinator's diagnostics.
    "provider_rate_limited", "provider_limiter_drift", "provider_quota_paused",
    "provider_lease_quarantined", "provider_lease_operator_reclaimed",
    # backend/provider_scheduler.py -- an inference lease this process believed
    # it held turned out not to be its own. It reaches the same durable sink
    # through the coordinator's diagnostic callback.
    "provider_lease_ownership_lost",
    # backend/budget.py -- a model call admitted without a usable output cap.
    "model_output_cap_missing",
})

#: THE acceptance vocabulary: every type a trusted emitter may durably append.
#: Recognition is exact set membership, never a substring or a prefix test.
EVENT_TYPES = (V1_EVENT_TYPES | SWARM_V2_EVENT_TYPES
               | CATALOG_EVENT_TYPES | OPERATIONAL_EVENT_TYPES)

# ---------------------------------------------------------------------------
# Non-durable: the Government capture's progress vocabulary
# ---------------------------------------------------------------------------
#: The ingestor's progress signals. They travel on the same callback shape as a
#: run event and are NEVER durable: ``backend/catalog/operator_capture.py``
#: keeps at most 64 of these names in memory to tell a replay from a first
#: capture, and writes none of them.
#:
#: They are declared here so that vocabulary has one home -- the capture used to
#: test for one of them with a bare literal -- and deliberately excluded from
#: :data:`EVENT_TYPES` so declaring them cannot make any of them durable.
CAPTURE_PROGRESS_EVENT_TYPES = frozenset({
    "catalog_refresh_unchanged", "catalog_refresh_completed",
    "catalog_snapshot_replayed", "catalog_records_written",
    "catalog_candidates_skipped", "catalog_candidates_written",
    "catalog_snapshot_activated",
})

#: The ONE name the capture report's "was this a replay?" question is asked by.
CAPTURE_SNAPSHOT_REPLAYED = "catalog_snapshot_replayed"


class UnknownEventType(ValueError):
    """A durable append was attempted for a type this release does not know."""

    def __init__(self, event_type: object) -> None:
        # Only the offending TYPE is carried, never the payload: a refusal
        # message is not a place to launder unvalidated content.
        self.event_type = event_type
        super().__init__(f"unknown event type {event_type!r}")


def is_known_event_type(event_type: object) -> bool:
    """Is this a type any trusted emitter may durably append?"""
    return isinstance(event_type, str) and event_type in EVENT_TYPES


def require_known_event_type(event_type: object) -> str:
    """Return `event_type`, or refuse. The trusted-boundary check."""
    if not is_known_event_type(event_type):
        raise UnknownEventType(event_type)
    return str(event_type)


def owns_v1_projection(event_type: str) -> bool:
    """May this type write the V1 projection?"""
    return event_type in V1_EVENT_TYPES


def owns_agent_projection(event_type: str) -> bool:
    """May this type create or mutate a V1 agent row?"""
    return event_type in V1_EVENT_TYPES and event_type not in RUN_LEVEL_EVENT_TYPES


def owns_catalog_projection(event_type: str) -> bool:
    """May this type write the bounded catalog status slice?"""
    return event_type in CATALOG_EVENT_TYPES


def owns_swarm_projection(event_type: str) -> bool:
    """May this type fold into the Swarm V2 slice?"""
    return event_type in SWARM_V2_EVENT_TYPES


#: The groups, in the order the manifest lists them. One place, so a new group
#: cannot be added to the module and forgotten by the manifest.
GROUPS: tuple[str, ...] = ("v1", "swarm_v2", "catalog", "operational",
                           "run_level", "capture_progress")

_GROUP_MEMBERS = {
    "v1": V1_EVENT_TYPES,
    "swarm_v2": SWARM_V2_EVENT_TYPES,
    "catalog": CATALOG_EVENT_TYPES,
    "operational": OPERATIONAL_EVENT_TYPES,
    "run_level": RUN_LEVEL_EVENT_TYPES,
    "capture_progress": CAPTURE_PROGRESS_EVENT_TYPES,
}


def manifest() -> dict[str, object]:
    """The whole vocabulary as ONE deterministic, serializable document.

    This is what ``config/event_registry.json`` holds and what the frontend's
    mirror is checked against, in both directions, in CI. It is GENERATED, so
    the backend and the browser cannot describe two different vocabularies
    without a test failing -- which is the drift this module exists to remove.
    """
    return {
        "registry_version": REGISTRY_VERSION,
        "groups": {name: sorted(_GROUP_MEMBERS[name]) for name in GROUPS},
        # The acceptance set, spelled out rather than left to be recomputed:
        # a reader must not have to know which groups are durable.
        "accepted": sorted(EVENT_TYPES),
    }


def serialize() -> str:
    """The manifest's canonical bytes."""
    return json.dumps(manifest(), indent=2, sort_keys=True) + "\n"


def fingerprint() -> str:
    """A stable digest of the whole vocabulary.

    Two surfaces that print the same fingerprint are provably speaking about
    the same event vocabulary. It is bound onto a run's immutable identity, so
    a run records which vocabulary produced its durable stream.
    """
    payload = json.dumps(manifest(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CAPTURE_PROGRESS_EVENT_TYPES", "CAPTURE_SNAPSHOT_REPLAYED",
    "CATALOG_EVENT_TYPES", "EVENT_TYPES", "GROUPS", "OPERATIONAL_EVENT_TYPES",
    "REGISTRY_VERSION", "RUN_LEVEL_EVENT_TYPES", "SWARM_V2_EVENT_TYPES",
    "UnknownEventType", "V1_EVENT_TYPES", "fingerprint", "is_known_event_type",
    "manifest", "owns_agent_projection", "owns_catalog_projection",
    "owns_swarm_projection", "owns_v1_projection", "require_known_event_type",
    "serialize",
]
