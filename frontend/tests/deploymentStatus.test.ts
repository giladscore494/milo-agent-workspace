/**
 * `/api/deployment-status` — the read-only answer the operator's website check
 * reads. It must state the two execution values exactly, carry nothing else,
 * and never claim a commit it was not given.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { GET } from '../app/api/deployment-status/route';

const NAMES = ['NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI', 'GATEWAY_ALLOW_EXECUTION_ROUTES', 'VERCEL_GIT_COMMIT_SHA'];
const saved = Object.fromEntries(NAMES.map((name) => [name, process.env[name]]));

afterEach(() => {
  for (const name of NAMES) {
    if (saved[name] === undefined) delete process.env[name];
    else process.env[name] = saved[name];
  }
});

async function read(): Promise<Record<string, unknown>> {
  const response = GET();
  expect(response.status).toBe(200);
  expect(response.headers.get('cache-control')).toBe('no-store');
  return await response.json() as Record<string, unknown>;
}

describe('the deployment status route', () => {
  it('states both execution values as off by default, and no commit', async () => {
    for (const name of NAMES) delete process.env[name];
    expect(await read()).toEqual({
      contract: 'milo-website-deployment/1', execution_ui: false,
      gateway_execution_routes: false, commit_sha: null,
    });
  });

  it('reports exactly "true" as on, and nothing else', async () => {
    process.env.NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI = ' TRUE ';
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    let body = await read();
    expect([body.execution_ui, body.gateway_execution_routes]).toEqual([true, true]);
    process.env.NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI = '1';
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'yes';
    body = await read();
    expect([body.execution_ui, body.gateway_execution_routes]).toEqual([false, false]);
  });

  it('passes through only a full commit id', async () => {
    process.env.VERCEL_GIT_COMMIT_SHA = 'A'.repeat(40);
    expect((await read()).commit_sha).toBe('a'.repeat(40));
    process.env.VERCEL_GIT_COMMIT_SHA = 'abc123';
    expect((await read()).commit_sha).toBeNull();
    process.env.VERCEL_GIT_COMMIT_SHA = `${'a'.repeat(40)}\nextra`;
    expect((await read()).commit_sha).toBeNull();
  });

  it('carries no other field', async () => {
    expect(Object.keys(await read()).sort()).toEqual(
      ['commit_sha', 'contract', 'execution_ui', 'gateway_execution_routes']);
  });
});
