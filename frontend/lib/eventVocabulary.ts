/**
 * Which event types may touch which projection — DERIVED, not transcribed.
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
 * WHERE THE SETS COME FROM
 * ------------------------
 * `eventRegistry.generated.json` is written by `backend/event_registry.py`,
 * which is the ONE place this repository declares an event vocabulary. This
 * module used to hand-mirror two of the backend's sets and hand-MAINTAIN a
 * third (`SWARM_V2_EVENT_TYPES`) that had no backend counterpart at all — and
 * it had drifted: five types `backend/engines/swarm_v2/engine.py` really emits
 * were missing from it, so they were silently filed as unknown here while the
 * database recorded them. Nothing is transcribed now. `tests/eventVocabulary`
 * proves this module's sets are exactly the manifest's groups, and the
 * backend's `tests/test_event_registry.py` proves the manifest is exactly
 * `backend/event_registry.py` — so neither side can move without the other.
 *
 * WHAT MEMBERSHIP MEANS
 * ---------------------
 * Membership grants a PROJECTION, and the groups are separate because they
 * grant different ones:
 *
 * - `V1_EVENT_TYPES` is the ONLY set whose members may write the V1
 *   projection (agents, phase, progress, sources, claims, conflicts,
 *   checkpoints, spend telemetry).
 * - `SWARM_V2_EVENT_TYPES` fold into the swarm slice and are deliberately NOT
 *   allowed into the V1 projection: Swarm V2 has no agent concept, so a
 *   `task_started` carrying an `agent` field is a payload making a claim the
 *   engine that emitted it cannot make.
 * - `CATALOG_EVENT_TYPES` own exactly the bounded catalog status slice.
 * - `OPERATIONAL_EVENT_TYPES` own NOTHING. They are durable operator signals
 *   (provider pacing, retry accounting, a missing output cap) emitted by
 *   server code with no agent, task, phase or spend concept. Recognising them
 *   stops them being counted as unknown; it grants them no projection.
 *
 * The V1 and V2 sets overlap on the run-lifecycle types both engines emit.
 * That overlap is intentional and harmless — each side reads only what it owns.
 *
 * Forward compatibility is still the fail-safe direction: a type this release
 * does not know is recorded as an observation (the raw event list, and
 * `swarm.unknownEventTypes`) and changes nothing else.
 */

import registry from './eventRegistry.generated.json';

/** The vocabulary's identity, carried on every run's immutable identity. */
export const EVENT_REGISTRY_VERSION: string = registry.registry_version;

function group(name: keyof typeof registry.groups): ReadonlySet<string> {
  return new Set(registry.groups[name]);
}

/**
 * Mirror of `V1_EVENT_TYPES` in `backend/event_registry.py`.
 *
 * NOT the backend's acceptance set: that is this group UNION the Swarm V2,
 * catalog and operational groups, and it answers a different question.
 * `ACCEPTED_EVENT_TYPES` below is what the API ACCEPTS from a worker; this set
 * is what OWNS the V1 projection. Using the union here would hand a provider
 * pacing signal the agent, phase, progress and spend projection.
 */
export const V1_EVENT_TYPES: ReadonlySet<string> = group('v1');

/** The Swarm V2 vocabulary `lib/swarmReducer.ts` handles. */
export const SWARM_V2_EVENT_TYPES: ReadonlySet<string> = group('swarm_v2');

/**
 * The catalog path's two events.
 *
 * A DEDICATED closed set, not an addition to either set above, because
 * membership is what grants a projection here. They own exactly one thing: the
 * bounded catalog slice in `lib/catalogStatus.ts`. They keep the raw event
 * stream they already had — every event is appended to it unconditionally —
 * and they gain nothing else.
 *
 * These events are emitted by trusted server code (`backend/worker/main.py`,
 * from `PromotionAttempt.as_event()`) that has no agent, task, phase or spend
 * concept at all. Their payload carries ids, counts, booleans and static reason
 * codes. Anything else in one is a payload making a claim its emitter cannot
 * make, and the reducer ignores it.
 */
export const CATALOG_EVENT_TYPES: ReadonlySet<string> = group('catalog');

/**
 * Durable operator signals that own no projection at all.
 *
 * They are recognised so they stop being counted as unknown types, and they
 * are kept out of every other set so recognising them cannot hand them a
 * projection their emitters could not support.
 */
export const OPERATIONAL_EVENT_TYPES: ReadonlySet<string> = group('operational');

/** Everything a trusted emitter may durably append. */
export const ACCEPTED_EVENT_TYPES: ReadonlySet<string> = new Set(registry.accepted);

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
const RUN_LEVEL_EVENT_TYPES: ReadonlySet<string> = group('run_level');

/** Recognised at all: it may be folded rather than only observed. */
export function isKnownEventType(type: string): boolean {
  return V1_EVENT_TYPES.has(type) || SWARM_V2_EVENT_TYPES.has(type)
    || CATALOG_EVENT_TYPES.has(type) || OPERATIONAL_EVENT_TYPES.has(type);
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
 * Only a type the backend itself validates. A Swarm V2 type never qualifies,
 * and neither does an operational one.
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
