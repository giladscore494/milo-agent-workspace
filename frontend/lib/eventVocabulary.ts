/**
 * Which event types may touch which projection.
 *
 * An event arrives as a `{event_type, agent, phase, progress, payload}` record
 * and the reducer turns it into rendered facts: agents, lifecycle phase,
 * progress, sources, claims, conflicts, and event-derived token and cost
 * totals. Before F5's correction, that projection ran on the event's FIELDS
 * regardless of whether its TYPE was one the frontend recognises — so an
 * unrecognised type carrying `agent`, `phase` and `payload.tokens` produced a
 * V1 agent, a lifecycle phase and a cost total that no backend event had
 * authorised. Substring tests over the type name (`includes('failed')`,
 * `includes('retry')`) made it worse: an invented name could pick its own agent
 * status.
 *
 * So recognition comes first, and it is by exact type, never by substring.
 *
 * `V1_EVENT_TYPES` is the frontend mirror of `V1_EVENT_TYPES` in
 * `backend/runtime.py`, exactly as `lib/runStatus.ts` mirrors
 * `TERMINAL_STATES`. It is the ONLY set whose members may write the V1
 * projection. (The backend's `EVENT_TYPES` — what the API accepts from a
 * worker — is that set UNION the catalog set below; acceptance and projection
 * ownership are deliberately two different questions.)
 *
 * `SWARM_V2_EVENT_TYPES` is the vocabulary `lib/swarmReducer.ts` handles. Its
 * members fold into the swarm slice and are deliberately NOT allowed into the
 * V1 projection: Swarm V2 has no agent concept, so a `task_started` event
 * carrying an `agent` field is a payload making a claim the engine that emitted
 * it cannot make.
 *
 * The two sets overlap on the run-lifecycle types both engines emit. That
 * overlap is intentional and harmless — each side reads only what it owns.
 *
 * Forward compatibility is the fail-safe direction: a type this file does not
 * know is recorded as an observation (the raw event list, and
 * `swarm.unknownEventTypes`) and changes nothing else. A new backend event type
 * is therefore inert in the browser until it is added here on purpose.
 */

/** Mirror of `backend/runtime.py` `EVENT_TYPES`. Keep in step with it. */
export const V1_EVENT_TYPES: ReadonlySet<string> = new Set([
  'run_created', 'run_started', 'run_resumed', 'phase_started', 'phase_completed',
  'agent_created', 'agent_started', 'agent_progress', 'agent_completed', 'agent_failed',
  'chunk_started', 'chunk_completed', 'chunk_failed', 'fallback_started', 'fallback_completed',
  'checkpoint_saved', 'cancellation_requested', 'run_completed', 'run_partial_success',
  'run_failed', 'run_cancelled',
  'tool_access_requested', 'tool_access_granted', 'tool_access_denied', 'tool_used',
  'source_recorded', 'claim_recorded', 'conflict_detected',
  'launch_requested', 'launch_failed', 'run_requeued',
  'budget_warning', 'budget_exhausted', 'token_limit_reached', 'run_timed_out',
  'retry_limit_reached', 'kill_switch_activated',
  'supervisor_shadow_failed',
]);

/**
 * The Swarm V2 vocabulary `lib/swarmReducer.ts` handles, minus the run
 * lifecycle types it shares with V1 (those are already above).
 */
export const SWARM_V2_EVENT_TYPES: ReadonlySet<string> = new Set([
  'commander_plan_created', 'commander_replanned',
  'task_ready', 'task_started', 'task_completed', 'task_failed',
  'tool_called', 'worker_output_repair_started',
  'evidence_added', 'conflict_found', 'grounding_context_resolved',
  'verification_batch_completed', 'verification_completed',
]);

/**
 * The catalog path's two events. Mirror of `CATALOG_EVENT_TYPES` in
 * `backend/runtime.py`.
 *
 * A DEDICATED closed set, not an addition to either set above, because
 * membership is what grants a projection here. Putting these two in
 * `V1_EVENT_TYPES` would hand a catalog event the agent, phase, progress and
 * spend projection; putting them in `SWARM_V2_EVENT_TYPES` would offer them the
 * task and lifecycle machinery. They own exactly one thing: the bounded catalog
 * slice in `lib/catalogStatus.ts`. They keep the raw event stream they already
 * had — every event is appended to it unconditionally — and they gain nothing
 * else.
 *
 * These events are emitted by trusted server code (`backend/worker/main.py`,
 * from `PromotionAttempt.as_event()`) that has no agent, task, phase or spend
 * concept at all. Their payload carries ids, counts, booleans and static reason
 * codes. Anything else in one is a payload making a claim its emitter cannot
 * make, and the reducer ignores it.
 */
export const CATALOG_EVENT_TYPES: ReadonlySet<string> = new Set([
  'catalog_variant_promoted',
  'catalog_promotion_refused',
]);

/**
 * Types that are about the RUN, not about an agent inside it.
 *
 * They are legitimate V1 events and keep every other projection they own — a
 * terminal type still records a raw error, `checkpoint_saved` still records a
 * checkpoint — but none of them may create or mutate an agent row. The V1
 * engine addresses an agent through the agent, chunk, phase, tool and
 * fallback types; a run-level type carrying an `agent` field describes which
 * agent the RUN stopped in, not work that agent did.
 */
const RUN_LEVEL_EVENT_TYPES: ReadonlySet<string> = new Set([
  'run_created', 'run_started', 'run_resumed', 'run_completed', 'run_partial_success',
  'run_failed', 'run_cancelled', 'run_requeued', 'run_timed_out',
  'cancellation_requested', 'launch_requested', 'launch_failed',
  'budget_warning', 'budget_exhausted', 'token_limit_reached',
  'kill_switch_activated', 'supervisor_shadow_failed',
]);

/** Recognised at all: it may be folded rather than only observed. */
export function isKnownEventType(type: string): boolean {
  return V1_EVENT_TYPES.has(type) || SWARM_V2_EVENT_TYPES.has(type)
    || CATALOG_EVENT_TYPES.has(type);
}

/**
 * May this type write the bounded catalog status slice?
 *
 * Exact membership of the catalog set and nothing else. `catalog_` is not a
 * prefix rule and never becomes one: `catalog_variant_promoted_v2` is a type
 * this release has never heard of, and it stays inert like any other.
 */
export function ownsCatalogProjection(type: string): boolean {
  return CATALOG_EVENT_TYPES.has(type);
}

/**
 * May this type write the V1 projection — phase, progress, sources, claims,
 * conflicts, supervisor notes, checkpoints, raw errors and the event-derived
 * token/cost totals?
 *
 * Only a type the backend itself validates. A Swarm V2 type never qualifies.
 */
export function ownsV1Projection(type: string): boolean {
  return V1_EVENT_TYPES.has(type);
}

/**
 * May this type create or mutate a V1 agent row?
 *
 * The V1 projection minus the run-level types above. This is what stops an
 * `agent` field on a run-level or Swarm V2 event from inventing an agent.
 */
export function ownsAgentProjection(type: string): boolean {
  return V1_EVENT_TYPES.has(type) && !RUN_LEVEL_EVENT_TYPES.has(type);
}

/**
 * May this type contribute to the event-derived token and cost totals?
 *
 * Only agent-owned work reports spend against itself. These totals are
 * DEVELOPER TELEMETRY and are labelled as such in the Inspector: the
 * authoritative aggregate is `run.usage` (`lib/runUsage.ts`), which is never
 * reconstructed from events and is not affected by anything here.
 */
export function ownsSpendTelemetry(type: string): boolean {
  return ownsAgentProjection(type);
}
