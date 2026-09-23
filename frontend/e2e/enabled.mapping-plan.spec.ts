import { expect, test } from '@playwright/test';
import { PROJECT_GAMMA, apiToken, authHeaders, loginViaUi } from './helpers';

/**
 * The Mapping Plan, end to end: browser → gateway → API → WorkScope contract →
 * repository → back. The same plan stated as words and as clicks must come back
 * as the same server revision with the same fingerprint, and nothing may run.
 */

async function newConversation(page: import('@playwright/test').Page, project: string | undefined, title: string) {
  // `undefined` stays in the project already selected.
  if (project !== undefined) await page.getByText(project).click();
  await page.getByLabel('Conversation title').fill(title);
  await page.getByRole('button', { name: 'New conversation' }).click();
}

function planPanel(page: import('@playwright/test').Page) {
  return page.locator('section.mapping-plan');
}

async function openPlan(page: import('@playwright/test').Page) {
  const panel = planPanel(page);
  await expect(panel.getByRole('heading', { name: 'Mapping plan' })).toBeVisible();
  // The panel stays open across conversations of one project, so it is only
  // opened when it is closed.
  const disclosure = panel.locator('button[aria-controls="mapping-plan-body"]');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  // The enabled stack lets a member start batches of a PREPARED plan; a plan
  // itself still runs nothing, and the note says which posture this is.
  await expect(panel.getByText(/Planning only|Each batch starts only when you start it/)).toBeVisible();
  return panel;
}

async function fingerprint(panel: import('@playwright/test').Locator): Promise<string> {
  const text = await panel.getByText(/Revision \d+ · fingerprint/).textContent();
  const match = /fingerprint\s+([0-9a-f]{12})/.exec(text ?? '');
  expect(match, text ?? '').not.toBeNull();
  return match![1];
}

test('M1. words and clicks become the SAME server plan, and nothing runs', async ({ page }) => {
  await loginViaUi(page, 'alice');

  await newConversation(page, 'Gamma Swarm', 'm1-words');
  let panel = await openPlan(page);
  await panel.getByLabel('Tell MILO what to map').fill('Map Toyota and Lexus, starting with 2018+, up to 800 variants.');
  await panel.getByRole('button', { name: 'Create plan', exact: true }).click();
  const units = panel.getByRole('list', { name: 'Manufacturers in priority order' });
  await expect(units.getByRole('listitem')).toHaveCount(2);
  await expect(units.getByRole('listitem').nth(0)).toContainText('1. Toyota');
  await expect(units.getByRole('listitem').nth(1)).toContainText('2. Lexus');
  await expect(panel.getByText(/Revision 1/)).toBeVisible();
  const fromWords = await fingerprint(panel);

  await newConversation(page, undefined, 'm1-clicks');
  panel = await openPlan(page);
  const directory = panel.getByRole('list', { name: 'Manufacturer directory' });
  await directory.getByRole('button', { name: 'Add Toyota' }).click();
  await directory.getByRole('button', { name: 'Add Lexus' }).click();
  await panel.getByLabel('From model year (optional)').fill('2018');
  await panel.getByLabel(/Candidate limit/).fill('800');
  await panel.getByRole('button', { name: 'Create plan from these choices' }).click();
  await expect(panel.getByText(/Revision 1/)).toBeVisible();
  const fromClicks = await fingerprint(panel);

  expect(fromClicks).toBe(fromWords);
  // A plan is a draft: no run exists in either conversation.
  await expect(page.getByText(/Run finished with status/)).toHaveCount(0);
});

test('M2. an edit revises the plan against its head, in priority order', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await newConversation(page, 'Gamma Swarm', 'm2-revise');
  const panel = await openPlan(page);
  await panel.getByLabel('Tell MILO what to map').fill('Do Toyota first, then Mazda. Stop after 500 vehicles.');
  await panel.getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect(panel.getByText(/Revision 1/)).toBeVisible();

  await panel.getByRole('button', { name: 'Move Mazda up' }).click();
  await panel.getByRole('button', { name: 'Save plan' }).click();
  await expect(panel.getByText(/Revision 2/)).toBeVisible();
  const units = panel.getByRole('list', { name: 'Manufacturers in priority order' });
  await expect(units.getByRole('listitem').nth(0)).toContainText('1. Mazda');

  await panel.getByLabel('Tell MILO what to map').fill('Map Toyota and Polestar');
  await panel.getByRole('button', { name: 'Update plan' }).click();
  const notes = panel.getByRole('list', { name: 'How the plan was read' });
  await expect(notes).toContainText('polestar');
  await expect(panel.getByText(/Revision 3/)).toBeVisible();
});

test('M3. a project whose engine reads no plan shows no Mapping Plan', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await newConversation(page, 'Alpha Research', 'm3-v1');
  await expect(page.getByLabel('Task content')).toBeVisible();
  await expect(planPanel(page)).toHaveCount(0);
});

const BACKEND = 'http://127.0.0.1:8101';

/**
 * The operator step, done the way this stack can: in production ONLY the
 * operator capture job prepares a plan revision; the enabled E2E backend stands
 * in for it with a worker-identity route over the committed register rows.
 */
async function prepareAsOperator(request: import('@playwright/test').APIRequestContext,
                                 baseURL: string, title: string): Promise<void> {
  const token = await apiToken(request, 'alice');
  const conversations = await request.get(`${baseURL}/api/gateway/projects/${PROJECT_GAMMA}/conversations`,
                                          { headers: authHeaders(token) });
  const conversation = (await conversations.json()).find((row: { title: string }) => row.title === title);
  const open = await request.get(`${baseURL}/api/gateway/conversations/${conversation.id}/work-scopes/open`,
                                 { headers: authHeaders(token) });
  const plan = (await open.json()).work_scope.work_scope_id as string;
  const prepared = await request.post(`${BACKEND}/e2e/operator/work-scopes/${plan}/prepare`, {
    headers: { 'X-Milo-Worker-Token': 'e2e-valid-worker-token' },
  });
  expect(prepared.status()).toBe(200);
  // A browser identity cannot reach the operator step.
  const refused = await request.post(`${BACKEND}/e2e/operator/work-scopes/${plan}/prepare`, {
    headers: { 'X-Milo-Worker-Token': token },
  });
  expect(refused.status()).toBeGreaterThanOrEqual(401);
}

test('M4. a prepared plan runs ONE confirmed batch at a time, to completion', async ({ page, request, baseURL }) => {
  await loginViaUi(page, 'alice');
  await newConversation(page, 'Gamma Swarm', 'm4-batches');
  const panel = await openPlan(page);
  await panel.getByLabel('Tell MILO what to map').fill('Map Toyota, up to 12 variants.');
  await panel.getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect(panel.getByText(/Revision 1/)).toBeVisible();
  const batches = panel.locator('section.mapping-plan-progress');
  await expect(batches).toContainText('has not been prepared yet');
  await expect(batches.getByRole('button', { name: /Start batch/ })).toHaveCount(0);

  await prepareAsOperator(request, baseURL!, 'm4-batches');
  await batches.getByRole('button', { name: 'Refresh progress' }).click();
  await expect(batches).toContainText('Prepared from revision 1: 12 candidates in 2 batches.');

  // Batch 1: nothing is sent before the person confirms ONE paid run.
  await batches.getByRole('button', { name: 'Start batch 1' }).click();
  const confirm = batches.getByRole('group', { name: 'Confirm batch start' });
  await expect(confirm).toContainText('Start Batch 1 of 2 — Toyota, 10 candidates?');
  await confirm.getByRole('button', { name: 'Yes, start this batch' }).click();
  // The run it created is the workspace's live run, and it finishes.
  await expect(page.getByText(/Run finished with status/)).toBeVisible({ timeout: 30_000 });
  await expect(batches.getByLabel('Plan progress')).toContainText('Batches finished1 of 2', { timeout: 30_000 });

  // Nothing started batch 2 by itself: it waits for the person.
  await expect(batches.getByRole('status', { name: 'Current batch' })).toHaveCount(0);
  await batches.getByRole('button', { name: 'Continue with batch 2' }).click();
  await batches.getByRole('group', { name: 'Confirm batch start' })
    .getByRole('button', { name: 'Yes, start this batch' }).click();
  await expect(batches.getByLabel('Plan progress')).toContainText('Batches finished2 of 2', { timeout: 30_000 });
  await expect(batches).toContainText('Every batch of this plan has finished.');
  await expect(batches.getByRole('button', { name: /Start batch|Continue with batch/ })).toHaveCount(0);
});

