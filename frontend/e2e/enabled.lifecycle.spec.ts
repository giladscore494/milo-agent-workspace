import { expect, test } from '@playwright/test';
import { PROJECT_ALPHA, apiToken, authHeaders, createConversation, loginViaUi } from './helpers';

// ENABLED stack: execution flags on, mocked in-process worker, mocked model
// adapters. No real Cloud Run job, Supabase project or paid model call.

const BACKEND = 'http://127.0.0.1:8101';

test('9. proposal lifecycle succeeds when enabled', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const created = await request.post(`${baseURL}/api/gateway/workflow-proposals`, {
    headers: authHeaders(token),
    data: { project_id: PROJECT_ALPHA, user_request: 'Create a current market research report with citations for EV charging' },
  });
  expect(created.status()).toBe(201);
  const proposal = await created.json();
  expect(proposal.status).toBe('approved');
  expect(proposal.created_by).toBeTruthy();
  const read = await request.get(`${baseURL}/api/gateway/workflow-proposals/${proposal.id}`, { headers: authHeaders(token) });
  expect(read.status()).toBe(200);
  const approved = await request.post(`${baseURL}/api/gateway/workflow-proposals/${proposal.id}/approve`, {
    headers: authHeaders(token),
    data: { reason: 'looks good' },
  });
  expect(approved.status()).toBe(200);
  expect((await approved.json()).approved_at).toBeTruthy();
});

test('10. cross-user proposal access is denied', async ({ request, baseURL }) => {
  const alice = await apiToken(request, 'alice');
  const bob = await apiToken(request, 'bob');
  const created = await request.post(`${baseURL}/api/gateway/workflow-proposals`, {
    headers: authHeaders(alice),
    data: { project_id: PROJECT_ALPHA, user_request: 'Create a current cited report about freight brokers' },
  });
  const proposal = await created.json();
  const denied = await request.get(`${baseURL}/api/gateway/workflow-proposals/${proposal.id}`, { headers: authHeaders(bob) });
  expect(denied.status()).toBe(404);
  const deniedMutation = await request.post(`${baseURL}/api/gateway/workflow-proposals/${proposal.id}/reject`, {
    headers: authHeaders(bob),
    data: {},
  });
  expect(deniedMutation.status()).toBe(404);
});

test('12+13. run creation succeeds; double-click produces exactly one run', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-doubleclick');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.getByText(/ID .* • project/)).toBeVisible();
  await page.getByLabel('Task content').fill('produce the final report');
  const send = page.getByRole('button', { name: 'Send task' });
  await send.dblclick();
  await expect(page.getByText('Status')).toBeVisible();
  // Exactly one run id is shown and polling starts for it.
  const backendRuns = await page.evaluate(async () => {
    const keys = Object.keys(window.sessionStorage).filter((k) => k.startsWith('milo.activeRun.'));
    return keys.map((k) => window.sessionStorage.getItem(k));
  });
  expect(backendRuns.filter(Boolean)).toHaveLength(1);
});

test('14. a duplicate idempotency key returns the original run', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const conversationId = await createConversation(request, baseURL!, token, PROJECT_ALPHA, 'idem');
  const body = { content: 'produce the final report', idempotency_key: 'e2e-key-dup-000001' };
  const first = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, { headers: authHeaders(token), data: body });
  expect(first.status()).toBe(202);
  const second = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, { headers: authHeaders(token), data: body });
  expect(second.status()).toBe(202);
  expect((await second.json()).run_id).toBe((await first.json()).run_id);
});

test('15. the same key with a different payload returns a conflict', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const conversationId = await createConversation(request, baseURL!, token, PROJECT_ALPHA, 'idem-conflict');
  const key = 'e2e-key-conflict-01';
  const first = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token),
    data: { content: 'original payload', idempotency_key: key },
  });
  expect(first.status()).toBe(202);
  const conflict = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token),
    data: { content: 'DIFFERENT payload', idempotency_key: key },
  });
  expect(conflict.status()).toBe(409);
});

test('16b/18. browser tokens and invalid worker identities are rejected on worker routes', async ({ request }) => {
  const token = await apiToken(request, 'alice');
  const source = { agent: 'a', url: 'https://example.com', title: 't', domain: 'example.com', source_type: 'web', source_strength: 'high', query: 'q', tool_operation: 'search' };
  const runId = 'cccccccc-1111-4111-8111-000000000009';
  // Browser Supabase token in the worker header: signature verification fails.
  const browserToken = await request.post(`${BACKEND}/runs/${runId}/sources`, {
    headers: { 'X-Milo-Worker-Token': token },
    data: source,
  });
  expect(browserToken.status()).toBe(401);
  // Unapproved service identity.
  const unapproved = await request.post(`${BACKEND}/runs/${runId}/sources`, {
    headers: { 'X-Milo-Worker-Token': 'e2e-unapproved-worker-token' },
    data: source,
  });
  expect(unapproved.status()).toBe(403);
  // Spoofed browser identity headers alone.
  const spoofed = await request.post(`${BACKEND}/runs/${runId}/sources`, {
    headers: { 'x-milo-auth-user-id': 'aaaaaaaa-1111-4111-8111-000000000001' },
    data: source,
  });
  expect(spoofed.status()).toBe(401);
});

test('19. a valid mocked worker identity can write authorized events', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const conversationId = await createConversation(request, baseURL!, token, PROJECT_ALPHA, 'worker-writes');
  const created = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token),
    data: { content: 'produce the final report', idempotency_key: 'e2e-key-worker-0001' },
  });
  const runId = (await created.json()).run_id;
  const event = await request.post(`${BACKEND}/internal/runs/${runId}/events`, {
    headers: { 'X-Milo-Worker-Token': 'e2e-valid-worker-token' },
    data: { event_type: 'agent_progress', message: 'written by verified worker identity' },
  });
  expect(event.status()).toBe(201);
});

test('20+26. polling reconstructs run state and renders final output', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-polling');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('produce the final report');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText('run_started')).toBeVisible();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/E2E mocked output/)).toBeVisible();
  await expect(page.getByText('source_recorded')).toBeVisible();
});

test('21. a refresh reconnects to the same run', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-refresh');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('slow crawl please');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText('Status')).toBeVisible();
  const runIdBefore = await page.evaluate(() => {
    const key = Object.keys(window.sessionStorage).find((k) => k.startsWith('milo.activeRun.'));
    return key ? window.sessionStorage.getItem(key) : null;
  });
  expect(runIdBefore).toBeTruthy();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible();
  await page.getByText('Alpha Research').click();
  await page.getByText('convo-refresh').click();
  await expect(page.getByText(runIdBefore!)).toBeVisible();
});

test('22. cancellation succeeds and reaches a terminal cancelled state', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-cancel');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('slow crawl please');
  await page.getByRole('button', { name: 'Send task' }).click();
  await page.getByRole('button', { name: 'Cancel run' }).click();
  await page.getByLabel('Cancellation reason').fill('changed my mind');
  await page.getByRole('button', { name: 'Confirm cancellation' }).click();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('cancelled', { exact: false }).first()).toBeVisible();
});

test('23. budget exhaustion stops execution', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-budget');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('exhaust budget now');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('budget_exhausted').first()).toBeVisible();
});

test('24. timeout stops execution', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-timeout');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('simulate a timeout');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('timed_out').first()).toBeVisible();
});

test('25. worker failure displays a safe error', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('convo-failure');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill('please fail cleanly');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('failed').first()).toBeVisible();
  const body = await page.content();
  expect(body).not.toMatch(/Traceback|stack trace|sk-[A-Za-z0-9_-]{8,}/);
});
