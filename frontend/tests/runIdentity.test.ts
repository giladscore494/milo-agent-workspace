/**
 * A historical run renders as the engine it WAS, not as the engine its project
 * is today — and an uncertain identity is never read as a default engine.
 */

import { describe, expect, it } from 'vitest';
import { EVENT_REGISTRY_FINGERPRINT, EVENT_REGISTRY_VERSION } from '../lib/eventVocabulary';
import { hasRunIdentity, runIdentityWorkflowKey } from '../lib/runIdentity';
import { Run, RunIdentity } from '../lib/types';

const RUN_ID = '11111111-1111-4111-8111-000000000001';
const OTHER_ID = '22222222-2222-4222-8222-000000000002';

/** The reviewed engine version of each workflow this release can render. */
const ENGINE_VERSIONS: Record<string, string> = {
  swarm_v2: 'swarm_v2.1',
  vehicle_catalog_v1: 'vehicle_catalog_v1.stage3',
};

function identity(over: Partial<RunIdentity> = {}): RunIdentity {
  const workflowKey = over.workflow_key ?? 'swarm_v2';
  return {
    identity_version: 'milo-run-identity/1',
    run_id: RUN_ID,
    workflow_key: workflowKey,
    // Derived, so overriding the workflow cannot leave the OTHER engine's
    // version behind: a record whose pair disagrees is not a valid identity,
    // and would make a "valid" case here silently unreadable.
    engine_version: ENGINE_VERSIONS[workflowKey] ?? 'swarm_v2.1',
    policy_version: 'milo-runtime-policy/1',
    policy_fingerprint: 'f'.repeat(64),
    release_sha: 'a'.repeat(40),
    // The run states which event vocabulary its stream speaks, and the reader
    // refuses one that is not this build's. Taken from the generated registry
    // rather than restated, so a regenerated vocabulary cannot drift from here.
    event_registry_version: EVENT_REGISTRY_VERSION,
    event_registry_fingerprint: EVENT_REGISTRY_FINGERPRINT,
    ...over,
  };
}

function run(over: Partial<Run> = {}): Run {
  return { id: RUN_ID, conversation_id: OTHER_ID, status: 'completed', ...over };
}

describe('the run states its own engine', () => {
  it('reads the workflow key the run was created with', () => {
    expect(runIdentityWorkflowKey(run({ run_identity: identity() }))).toBe('swarm_v2');
    expect(runIdentityWorkflowKey(run({
      run_identity: identity({ workflow_key: 'vehicle_catalog_v1' }),
    }))).toBe('vehicle_catalog_v1');
    expect(hasRunIdentity(run({ run_identity: identity() }))).toBe(true);
  });
});

describe('an uncertain identity is never a default engine', () => {
  it.each([
    ['no run at all', undefined],
    ['no identity', run()],
    ['a null identity', run({ run_identity: null })],
    ['an identity that is not an object', run({ run_identity: 'swarm_v2' as never })],
    ['an identity naming a different run', run({ run_identity: identity({ run_id: OTHER_ID }) })],
    ['a blank workflow key', run({ run_identity: identity({ workflow_key: '   ' }) })],
    ['a workflow this build cannot render', run({ run_identity: identity({ workflow_key: 'swarm_v3' }) })],
    ['a prefix of a known key', run({ run_identity: identity({ workflow_key: 'swarm_v2_beta' }) })],
  ])('returns undefined for %s', (_label, value) => {
    expect(runIdentityWorkflowKey(value as Run | undefined)).toBeUndefined();
    expect(hasRunIdentity(value as Run | undefined)).toBe(false);
  });
});
