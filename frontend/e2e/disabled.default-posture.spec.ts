import { expect, test } from '@playwright/test';
import { ALICE_PROJECTS, APPROVED_PUBLIC_VARS, PROJECT_ALPHA, PROJECT_BETA, apiToken, authHeaders, createConversation, loginViaUi } from './helpers';

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
  // The Supabase SDK's own sentence ("Invalid login credentials") is upstream
  // prose about an upstream system. The user sees OUR copy for the same
  // classification, and the SDK's words never reach the page.
  await expect(page.getByText('That email and password combination was not accepted.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Invalid login credentials');
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

test('7b. clicking New conversation with no title succeeds with the safe default', async ({ page }) => {
  // Regression: an empty title used to reach the NOT NULL conversations.title
  // column as null (PostgreSQL 23502) and fail the click.
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.getByText(/ID .* • project/)).toBeVisible();
});

test('7c. API conversation creation without a title returns 201 and the default title', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const response = await request.post(`${baseURL}/api/gateway/projects/${PROJECT_ALPHA}/conversations`, {
    headers: authHeaders(token),
    data: {},
  });
  expect(response.status()).toBe(201);
  expect((await response.json()).title).toBe('New conversation');
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
  // Identity is regenerated from the validated token, so the answer is alice's
  // memberships -- and never bob's, which is what the spoofed headers claimed.
  const ids = projects.map((p: { id: string }) => p.id);
  expect(ids).toEqual(ALICE_PROJECTS);
  expect(ids).not.toContain(PROJECT_BETA);
});

test('27. sign-out removes access', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByRole('button', { name: 'Logout' }).click();
  await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();
  await expect(page.getByText('Alpha Research')).toHaveCount(0);
});

test('27b. sign-out also clears the browser record of which run each conversation was on', async ({ page }) => {
  await loginViaUi(page, 'alice');
  // Session storage is workspace state the browser keeps for itself. A
  // signed-out page may not keep it, and a refresh after sign-out must not be
  // able to reopen anything from it.
  await page.evaluate(() => {
    window.sessionStorage.setItem('milo.activeRun.11111111-1111-4111-8111-000000000001', 'a-previous-run');
    window.sessionStorage.setItem('unrelated.key', 'kept');
  });

  await page.getByRole('button', { name: 'Logout' }).click();
  await expect(page.getByRole('button', { name: 'Login' })).toBeVisible();

  const stored = await page.evaluate(() => ({
    run: window.sessionStorage.getItem('milo.activeRun.11111111-1111-4111-8111-000000000001'),
    unrelated: window.sessionStorage.getItem('unrelated.key'),
  }));
  expect(stored.run).toBeNull();
  // Only MILO's own keys are cleared; the page does not empty storage it does
  // not own.
  expect(stored.unrelated).toBe('kept');
});

test('28. no secrets are exposed in served pages or client bundles', async ({ page, request, baseURL }) => {
  await page.goto('/');
  const html = await page.content();
  // Secret VALUE shapes (sb_secret_ keys, provider keys, the stack's own
  // backend service-key placeholder). The literal word "service_role"
  // appears in vendored supabase-js type enums and is not a secret.
  const forbidden = [/sb_secret_[A-Za-z0-9]/i, /sk-[A-Za-z0-9_-]{16,}/, /e2e-offline-placeholder/, /SUPABASE_SERVICE_ROLE_KEY\s*[:=]\s*['"][^'"]+/];
  for (const pattern of forbidden) {
    expect(html, String(pattern)).not.toMatch(pattern);
  }
  const scripts = await page.locator('script[src]').evaluateAll((nodes) => nodes.map((n) => (n as HTMLScriptElement).src));
  // EVERY served script, not a sample of them: a credential in the eleventh
  // chunk is a credential. `npm run test:secrets` scans the built bundle on
  // disk; this scans what the running server actually hands the browser, and
  // the two are different evidence.
  expect(scripts.length, 'the page served no scripts to scan').toBeGreaterThan(0);
  for (const src of scripts) {
    const body = await (await request.get(src)).text();
    for (const pattern of forbidden) {
      expect(body, `${src} ${pattern}`).not.toMatch(pattern);
    }
    // Only approved public configuration may be inlined into browser code.
    const publicVars = body.match(/NEXT_PUBLIC_[A-Z0-9_]+/g) ?? [];
    for (const name of new Set(publicVars)) {
      expect(APPROVED_PUBLIC_VARS, `${src} ${name}`).toContain(name);
    }
  }
});
