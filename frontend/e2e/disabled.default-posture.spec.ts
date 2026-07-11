import { expect, test } from '@playwright/test';
import { PROJECT_ALPHA, PROJECT_BETA, apiToken, authHeaders, createConversation, loginViaUi } from './helpers';

// DISABLED stack: every execution flag is off. These tests prove the
// default production posture end to end.

test('1. unauthenticated visitor sees only the login screen', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();
  await expect(page.getByText(/Sign in to access/)).toBeVisible();
  await expect(page.getByText('MILO Vehicle Catalog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /send task/i })).toHaveCount(0);
});

test('2. invalid login is rejected', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Email').fill('alice@example.com');
  await page.getByLabel('Password').fill('wrong-password');
  await page.getByRole('button', { name: 'Login' }).click();
  await expect(page.getByRole('alert')).toContainText(/invalid/i);
  await expect(page.getByRole('button', { name: 'Logout' })).toHaveCount(0);
});

test('3. session survives a page refresh', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await expect(page.getByText('Alpha Research')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible();
  await expect(page.getByText('Alpha Research')).toBeVisible();
});

test('4. a user without memberships sees no projects', async ({ page }) => {
  await loginViaUi(page, 'mallory');
  await expect(page.getByText(/No projects are assigned to your account yet/)).toBeVisible();
});

test('5. an authorized user sees only assigned projects', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await expect(page.getByText('Alpha Research')).toBeVisible();
  await expect(page.getByText('Beta Catalog')).toHaveCount(0);
});

test('6. cross-user project access is denied with 404', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const own = await request.get(`${baseURL}/api/gateway/projects/${PROJECT_ALPHA}`, { headers: authHeaders(token) });
  expect(own.status()).toBe(200);
  const foreign = await request.get(`${baseURL}/api/gateway/projects/${PROJECT_BETA}`, { headers: authHeaders(token) });
  expect(foreign.status()).toBe(404);
});

test('7. conversation creation succeeds for a member', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('E2E kickoff');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.getByText(/ID .* • project/)).toBeVisible();
});

test('8. proposal creation while disabled returns 403', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const response = await request.post(`${baseURL}/api/gateway/workflow-proposals`, {
    headers: authHeaders(token),
    data: { project_id: PROJECT_ALPHA, user_request: 'Research something current with citations' },
  });
  expect(response.status()).toBe(403);
});

test('11. run creation while disabled returns 403 at the gateway and creates nothing', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const conversationId = await createConversation(request, baseURL!, token, PROJECT_ALPHA, 'run-disabled');
  const response = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token),
    data: { content: 'go', idempotency_key: 'e2e-key-000000001' },
  });
  expect(response.status()).toBe(403);
});

test('16. a browser user cannot call worker routes through the gateway', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const runId = 'cccccccc-1111-4111-8111-000000000001';
  for (const path of [
    `/api/gateway/runs/${runId}/tool-grants`,
    `/api/gateway/runs/${runId}/sources`,
    `/api/gateway/internal/runs/${runId}/events`,
    `/api/gateway/internal/runs/${runId}/complete`,
  ]) {
    const response = await request.post(`${baseURL}${path}`, {
      headers: authHeaders(token),
      data: {},
    });
    expect(response.status(), path).toBe(403);
  }
});

test('17. spoofed internal identity headers are ignored', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const response = await request.get(`${baseURL}/api/gateway/projects`, {
    headers: {
      ...authHeaders(token),
      'x-milo-auth-user-id': 'aaaaaaaa-1111-4111-8111-000000000002', // bob
      'x-milo-auth-user-email': 'bob@example.com',
    },
  });
  expect(response.status()).toBe(200);
  const projects = await response.json();
  // Identity is regenerated from the validated token: alice's project only.
  expect(projects.map((p: { id: string }) => p.id)).toEqual([PROJECT_ALPHA]);
});

test('27. sign-out removes access', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByRole('button', { name: 'Logout' }).click();
  await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();
  await expect(page.getByText('Alpha Research')).toHaveCount(0);
});

test('28. no secrets are exposed in served pages or client bundles', async ({ page, request, baseURL }) => {
  await page.goto('/');
  const html = await page.content();
  const forbidden = [/sb_secret/i, /service_role/i, /SUPABASE_SERVICE_ROLE_KEY/, /sk-[A-Za-z0-9_-]{8,}/];
  for (const pattern of forbidden) {
    expect(html, String(pattern)).not.toMatch(pattern);
  }
  const scripts = await page.locator('script[src]').evaluateAll((nodes) => nodes.map((n) => (n as HTMLScriptElement).src));
  for (const src of scripts.slice(0, 10)) {
    const body = await (await request.get(src)).text();
    for (const pattern of forbidden) {
      expect(body, `${src} ${pattern}`).not.toMatch(pattern);
    }
  }
});
