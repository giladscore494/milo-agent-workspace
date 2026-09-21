/**
 * A historical run renders as the engine it WAS, not as the engine its project
 * is today — and an uncertain identity is never read as a default engine.
 */

import { describe, expect, it } from 'vitest';
import { hasRunIdentity, runIdentityWorkflowKey } from '../lib/runIdentity';
import { Run, RunIdentity } from '../lib/types';

const RUN_ID = '11111111-1111-4111-8111-000000000001';
const OTHER_ID = '22222222-2222-4222-8222-000000000002';

function identity(over: Partial<RunIdentity> = {}): RunIdentity {
  return {
    identity_version: 'milo-run-identity/1',
    run_id: RUN_ID,
    workflow_key: 'swarm_v2',
    engine_version: 'swarm_v2.1',
    policy_version: 'milo-runtime-policy/1',
    policy_fingerprint: 'f'.repeat(64),
    release_sha: 'a'.repeat(40),
    event_registry_version: 'milo-event-registry/1',
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
