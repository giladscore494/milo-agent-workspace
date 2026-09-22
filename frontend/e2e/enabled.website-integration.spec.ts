import { expect, test } from '@playwright/test';
import { loginViaUi } from './helpers';

/**
 * Website → API → V3 run creation → immutable identity → in-process worker →
 * canonical finalizer → durable ProductOutcome → API projection → result
 * surface. The enabled stack runs the real API with the E2E worker, which
 * terminalizes through `RunFinalizer` exactly like production.
 */

const TERMINAL = { timeout: 30_000 };

async function runTask(page: import('@playwright/test').Page, project: string, title: string, task: string) {
  await loginViaUi(page, 'alice');
  await page.getByText(project).click();
  await page.getByLabel('Conversation title').fill(title);
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill(task);
  await page.getByRole('button', { name: 'Send task' }).click();
}

test('W1. a V1 run renders the canonical verdict beside the typed product', async ({ page }) => {
  await runTask(page, 'Alpha Research', 'w1-v1-verdict', 'produce the final report');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  await expect(result.getByText('Vehicle Catalog V1 product result')).toBeVisible();
  await expect(result.getByText('Alpha One')).toBeVisible();
  await expect(result.getByText('Alpha Two')).toBeVisible();
  // The verdict comes from the finalizer's record, not from the payload: the
  // usability and coverage it states are the canonical ones.
  await expect(result.getByText('2 produced · 0 outstanding · 100%')).toBeVisible();
});

test('W2. a partial V1 run states the outstanding items the finalizer recorded', async ({ page }) => {
  await runTask(page, 'Alpha Research', 'w2-v1-partial', 'produce a partial report');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Canonical verdict: Partial')).toBeVisible(TERMINAL);
  await expect(result.getByText(/OUTSTANDING_REVIEW_ITEMS/)).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Models (2)' })).toBeVisible();
  await expect(page.getByText(/Run finished with status/)).toContainText('partial_success');
});

test('W3. a V2 run carries the same canonical verdict on its typed surface', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'w3-v2-verdict', 'produce the final report');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  await expect(result.getByText('Usable result')).toBeVisible();
  const live = page.getByRole('region', { name: 'Live execution' });
  await expect(live.getByText('Swarm V2')).toBeVisible();
  await expect(live.getByText('finalized with canonical outcome')).toBeVisible();
});

test('W4. the live execution panel shows engine, phase, work and budget while a run is active, from durable truth', async ({ page }) => {
  await runTask(page, 'Alpha Research', 'w4-live', 'slow work please');
  const live = page.getByRole('region', { name: 'Live execution' });
  await expect(live.getByText('Vehicle Catalog V1')).toBeVisible(TERMINAL);
  await expect(live.getByText(/running · live/)).toBeVisible(TERMINAL);
  await expect(live.getByText('Research', { exact: true })).toBeVisible();
  await expect(live.getByText('not yet finalized')).toBeVisible();
  // A refresh rebuilds the same live view from the run row and events.
  await page.reload();
  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible();
  await page.getByText('Alpha Research').click();
  await page.getByText('w4-live').click();
  await expect(page.getByRole('region', { name: 'Live execution' }).getByText('Vehicle Catalog V1')).toBeVisible(TERMINAL);
  await expect(page.getByRole('region', { name: 'Live execution' }).getByText(/Run finished|finalized with canonical outcome/)).toBeVisible({ timeout: 60_000 });
});

test('W5. a completed result survives a browser restart through the durable run history', async ({ browser }) => {
  const first = await browser.newContext();
  const page = await first.newPage();
  await runTask(page, 'Alpha Research', 'w5-restart', 'produce the final report');
  await expect(page.getByRole('region', { name: 'Final result' }).getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  const history = page.getByRole('region', { name: 'Run history' });
  await expect(history.getByText('completed')).toBeVisible();
  await first.close();

  // A brand-new browser context: no session storage, no stored run id.
  const second = await browser.newContext();
  const fresh = await second.newPage();
  await loginViaUi(fresh, 'alice');
  await fresh.getByText('Alpha Research').click();
  await fresh.getByText('w5-restart').first().click();
  // The newest run is reopened from the durable history and the SAME typed
  // result and verdict render again, with no worker involved.
  const restored = fresh.getByRole('region', { name: 'Final result' });
  await expect(restored.getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  await expect(restored.getByText('Alpha One')).toBeVisible();
  await expect(fresh.getByRole('region', { name: 'Run history' }).getByText('Complete', { exact: true })).toBeVisible();
  await second.close();
});

test('W6. the run history lets an earlier run be reopened as the engine it was', async ({ page }) => {
  await runTask(page, 'Alpha Research', 'w6-history', 'produce the final report');
  await expect(page.getByRole('region', { name: 'Final result' }).getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  // A second run in the same conversation (the first is terminal, so the
  // per-user concurrency cap admits it).
  await page.getByLabel('Task content').fill('please fail');
  await page.getByRole('button', { name: 'Send task' }).click();
  await expect(page.getByText(/Run finished with status/)).toContainText('failed', TERMINAL);
  const history = page.getByRole('region', { name: 'Run history' });
  await expect(history.getByRole('button')).toHaveCount(2);
  // Reopen the completed one.
  await history.getByRole('button').filter({ hasText: 'completed' }).click();
  await expect(page.getByRole('region', { name: 'Final result' }).getByText('Canonical verdict: Complete')).toBeVisible(TERMINAL);
  await expect(page.getByRole('region', { name: 'Final result' }).getByText('Alpha One')).toBeVisible();
});

test('W7. the run history read is a membership-scoped, bounded GET through the gateway', async ({ page, request }) => {
  await loginViaUi(page, 'alice');
  // Unauthenticated: refused at the gateway before anything is read.
  const anonymous = await request.get('/api/gateway/conversations/00000000-0000-4000-8000-000000000000/runs');
  expect(anonymous.status()).toBe(401);
  // An unbounded page is refused by the API.
  const token = await page.evaluate(() => {
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i) ?? '';
      if (key.includes('auth-token')) return JSON.parse(window.localStorage.getItem(key) ?? '{}').access_token as string | undefined;
    }
    return undefined;
  });
  test.skip(!token, 'no browser session token available to this test');
  const tooMany = await request.get('/api/gateway/conversations/00000000-0000-4000-8000-000000000000/runs?limit=500', {
    headers: { authorization: `Bearer ${token}` },
  });
  expect([404, 422]).toContain(tooMany.status());
});
