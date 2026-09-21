/**
 * The immutable identity a run is born with, for tests that render a run.
 *
 * Console 6 gives every executable run an identity at creation, bound from the
 * trusted project relation, and the workspace reads a loaded run's engine from
 * that record alone — never from what the project's `workflow_key` says today.
 * A mocked run with no identity is therefore a run the workspace refuses to
 * render engine-specific surfaces for, which is correct behaviour against a
 * fixture of the product that came before.
 *
 * The event-registry dimensions come from the GENERATED vocabulary rather than
 * being restated here, so a regenerated registry cannot drift from these
 * fixtures without the reader rejecting them.
 */

import { EVENT_REGISTRY_FINGERPRINT, EVENT_REGISTRY_VERSION } from '../../lib/eventVocabulary';
import { RunIdentity } from '../../lib/types';

/** The reviewed engine version of each workflow this release can render. */
const ENGINE_VERSIONS: Record<string, string> = {
  swarm_v2: 'swarm_v2.1',
  vehicle_catalog_v1: 'vehicle_catalog_v1.stage3',
};

/**
 * The identity a run of `workflowKey` was created with.
 *
 * `engine_version` is derived, so a caller changing the workflow cannot leave
 * the other engine's version behind — a record whose pair disagrees is not a
 * valid identity, and the reader returns `undefined` for it.
 */
export function identityFor(workflowKey: string, runId: string,
                            over: Partial<RunIdentity> = {}): RunIdentity {
  return {
    identity_version: 'milo-run-identity/1',
    run_id: runId,
    workflow_key: workflowKey,
    engine_version: ENGINE_VERSIONS[workflowKey] ?? workflowKey,
    policy_version: 'milo-runtime-policy/1',
    policy_fingerprint: 'f'.repeat(64),
    release_sha: 'a'.repeat(40),
    event_registry_version: EVENT_REGISTRY_VERSION,
    event_registry_fingerprint: EVENT_REGISTRY_FINGERPRINT,
    ...over,
  };
}
