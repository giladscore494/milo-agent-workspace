import { expect, test } from '@playwright/test';
import { apiToken, loginViaUi } from './helpers';

// Gateway identity enforcement against the REAL backend authorization code:
// identity headers are worthless without a verified gateway service token.

const BACKEND = 'http://127.0.0.1:8101';
const ALICE_ID = 'aaaaaaaa-1111-4111-8111-000000000001';

test('gateway identity: spoofed identity headers straight to Cloud Run are rejected', async ({ request }) => {
  const bare = await request.get(`${BACKEND}/projects`, {
    headers: { 'x-milo-auth-user-id': ALICE_ID },
  });
  expect(bare.status()).toBe(401);
  expect((await bare.json()).error.code).toBe('GATEWAY_AUTH_REQUIRED');
});

test('gateway identity: a browser Supabase token is not a gateway identity', async ({ request }) => {
  const token = await apiToken(request, 'alice');
  const response = await request.get(`${BACKEND}/projects`, {
    headers: {
      'x-milo-auth-user-id': ALICE_ID,
      'X-Milo-Gateway-Token': token,
    },
  });
  expect(response.status()).toBe(401);
  expect((await response.json()).error.code).toBe('GATEWAY_AUTH_INVALID');
});

test('gateway identity: a worker identity cannot impersonate the gateway', async ({ request }) => {
  const response = await request.get(`${BACKEND}/projects`, {
    headers: {
      'x-milo-auth-user-id': ALICE_ID,
      'X-Milo-Gateway-Token': 'e2e-valid-worker-token',
    },
  });
  expect(response.status()).toBe(403);
  expect((await response.json()).error.code).toBe('GATEWAY_IDENTITY_NOT_APPROVED');
});

test('gateway identity: the gateway identity cannot call worker mutation routes', async ({ request }) => {
  const response = await request.post(`${BACKEND}/internal/runs/cccccccc-1111-4111-8111-000000000002/events`, {
    headers: { 'X-Milo-Worker-Token': 'e2e-local-gateway-token' },
    data: { event_type: 'agent_progress', message: 'gateway pretending to be worker' },
  });
  expect(response.status()).toBe(403);
  expect((await response.json()).error.code).toBe('WORKER_IDENTITY_NOT_APPROVED');
});

test('switching runs does not retain stale state', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();

  // Conversation 1: run to completion (produces events + output).
  await page.getByLabel('Conversation title').fill('convo-stale-a');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('produce the final report');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/E2E mocked output/)).toBeVisible();
  await expect(page.getByText('run_started').first()).toBeVisible();

  // Conversation 2: no run yet — nothing from run A may remain visible.
  await page.getByLabel('Conversation title').fill('convo-stale-b');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.getByText(/E2E mocked output/)).toHaveCount(0);
  await expect(page.getByText('run_started')).toHaveCount(0);
  await expect(page.getByText(/Run finished with status/)).toHaveCount(0);
});
