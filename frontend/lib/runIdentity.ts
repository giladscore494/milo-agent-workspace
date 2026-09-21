/**
 * Reading a run's own immutable identity — closed, fail-closed, no guessing.
 *
 * WHY A RUN HAS ONE
 * -----------------
 * The browser chose the V2 vs V1 presentation from `project.workflow_key`,
 * which is what the project is TODAY. A project switched from one engine to
 * the other therefore re-rendered every earlier run of it as the wrong engine:
 * a completed V2 run shown through the V1 surfaces, or the reverse. The run
 * itself recorded nothing that could contradict that.
 *
 * `backend/run_identity.py` now binds the run's identity at creation, before
 * anything can execute it, and the database refuses to change it afterwards.
 * This module is the browser's reader for it.
 *
 * THREE RULES, ALL FAIL-CLOSED
 * ----------------------------
 * 1. CLOSED. Only the workflow key is projected, and only when it is one of
 *    the two workflows this release knows. A key this build has never heard of
 *    is not rendered as an engine — it returns `undefined` exactly like an
 *    absent identity, so a future engine is inert in the browser until it is
 *    added here on purpose.
 * 2. `undefined` MEANS "THE RUN SAID NOTHING", NEVER "ASSUME V1". The caller
 *    falls back to the project's workflow key, which is the behaviour a run
 *    created before identities existed has always had. Nothing here invents an
 *    engine from an output shape, an event stream or a payload.
 * 3. TOTAL. A missing run, a null identity, a non-object, a wrong-run record
 *    and a blank key all return `undefined` rather than throwing: this runs
 *    inside a render path, and a malformed field must not take the workspace
 *    down.
 */

import { EVENT_REGISTRY_FINGERPRINT, EVENT_REGISTRY_VERSION } from './eventVocabulary';
import { SWARM_V2_WORKFLOW_KEY, VEHICLE_CATALOG_V1_WORKFLOW_KEY } from './swarmTypes';
import { Run } from './types';

/** The workflow keys this release can render. Exact membership, never a prefix. */
const RENDERABLE_WORKFLOW_KEYS: ReadonlySet<string> = new Set([
  SWARM_V2_WORKFLOW_KEY,
  VEHICLE_CATALOG_V1_WORKFLOW_KEY,
]);

/**
 * The workflow key the RUN itself states, or `undefined`.
 *
 * `undefined` is returned for every uncertain case, and the caller treats it
 * as "the run said nothing" — not as a default engine.
 */
export function runIdentityWorkflowKey(run: Run | undefined): string | undefined {
  const identity = run?.run_identity;
  if (!identity || typeof identity !== 'object') return undefined;
  if (identity.identity_version !== 'milo-run-identity/1') return undefined;
  // A record that names a different run is not this run's identity.
  if (run?.id && identity.run_id && identity.run_id !== run.id) return undefined;
  if (identity.event_registry_version !== EVENT_REGISTRY_VERSION) return undefined;
  if (identity.event_registry_fingerprint !== EVENT_REGISTRY_FINGERPRINT) return undefined;
  const key = typeof identity.workflow_key === 'string' ? identity.workflow_key.trim() : '';
  if (!RENDERABLE_WORKFLOW_KEYS.has(key)) return undefined;
  if (key === SWARM_V2_WORKFLOW_KEY && identity.engine_version !== 'swarm_v2.1') return undefined;
  if (key === VEHICLE_CATALOG_V1_WORKFLOW_KEY && identity.engine_version !== 'vehicle_catalog_v1.stage3') return undefined;
  return key;
}

/** Did this run record an identity at all? Used only to label, never to infer. */
export function hasRunIdentity(run: Run | undefined): boolean {
  return runIdentityWorkflowKey(run) !== undefined;
}
