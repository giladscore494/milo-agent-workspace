import { expect, test } from '@playwright/test';
import { loginViaUi } from './helpers';

/**
 * Stage F4 — the Swarm V2 Final Result surface, end to end.
 *
 * ENABLED stack: execution flags on, in-process mocked worker, mocked model
 * adapters. The run is REAL as far as the browser is concerned — it is created
 * through the gateway, executed by the worker, polled to a terminal state and
 * read back from the durable run row — and the product payload the worker
 * records is built by the SHIPPED `FinalBuilder` + `finalize_product_outcome`,
 * mapped to its durable status by the shipped `durable_run_status`.
 *
 * No paid model call, no live source capture and no real Cloud Run job is
 * possible here: the worker is in-process and the model adapters are mocked.
 *
 * "Gamma Swarm" is the seeded `workflow_key = swarm_v2` project; "Alpha
 * Research" stays on `vehicle_catalog_v1` and is the V1 regression control.
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

test('F4-1. a terminal Swarm V2 run renders a typed product result, not raw JSON', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-usable', 'produce the final report');

  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result).toBeVisible();
  // Before the run finishes, the surface says so rather than showing nothing.
  await expect(result.getByText(/Not finished|Loading/)).toBeVisible();

  await expect(result.getByText('Usable result')).toBeVisible(TERMINAL);
  await expect(result.getByText(/left nothing outstanding/)).toBeVisible();

  // The verified answer, rendered as fields.
  await expect(result.getByText('Fuel type')).toBeVisible();
  await expect(result.getByText('plug-in hybrid')).toBeVisible();
  await expect(result.getByText('Horsepower hp')).toBeVisible();

  // Two verified values for one field: both shown, neither chosen.
  await expect(result.getByText('302')).toBeVisible();
  await expect(result.getByText('306')).toBeVisible();
  await expect(result.getByText('2 verified values — none was chosen.')).toBeVisible();

  // The raw-JSON surface F4 replaces is gone for this workflow.
  await expect(page.getByRole('heading', { name: 'Final artifacts' })).toHaveCount(0);
  await expect(result.locator('pre')).toHaveCount(0);
});

test('F4-2. provenance is safe public references, revealed on demand', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-provenance', 'produce the final report');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Usable result')).toBeVisible(TERMINAL);

  const provenance = result.locator('details.final-result-provenance').first();
  // Collapsed first: the answer leads, the sourcing follows.
  await expect(provenance).not.toHaveAttribute('open', '');
  await provenance.getByText('Provenance').click();
  await expect(provenance.getByText('src-gov-1')).toBeVisible();
  await expect(provenance.getByText('government_record_2026')).toBeVisible();

  // Nothing a product surface must never carry — including the MODERN
  // Supabase server-side key format production actually uses.
  const body = await page.content();
  expect(body).not.toMatch(/Traceback|chain of thought|sk-[A-Za-z0-9_-]{8,}|Bearer |service_role/i);
  expect(body).not.toMatch(/sb_secret_[A-Za-z0-9_-]/i);
});

test('F4-3. a partial result shows the answer AND everything still outstanding', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-partial', 'produce a partial report please');
  const result = page.getByRole('region', { name: 'Final result' });

  await expect(result.getByText('Partial result')).toBeVisible(TERMINAL);
  await expect(result.getByText(/not a completed result/)).toBeVisible();
  // The verified half is still reported.
  await expect(result.getByText('Fuel type')).toBeVisible();
  // …and so is every outstanding item, grouped by what it actually is.
  await expect(result.getByRole('heading', { name: 'Outstanding items (3)' })).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Conflicts (1)' })).toBeVisible();
  await expect(result.getByText('unresolved conflict')).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Coverage gaps (1)' })).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Task failures (1)' })).toBeVisible();

  // The execution surface still reports the durable terminal status, and the
  // two surfaces stay separate: telemetry is never the product answer.
  const execution = page.getByRole('region', { name: 'Swarm run' });
  await expect(execution.getByText(/Run finished with status/)).toBeVisible();
  await expect(execution.getByText('Verified fields')).toHaveCount(0);
  await expect(result.getByText('Usage and scale')).toHaveCount(0);
});

test('F4-4. an empty result is reported as empty, never as a success', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-empty', 'no usable result please');
  const result = page.getByRole('region', { name: 'Final result' });

  await expect(result.getByText('No usable result')).toBeVisible(TERMINAL);
  await expect(result.getByText(/Nothing was disproved either/)).toBeVisible();
  await expect(result.getByText(/No field was verified/)).toBeVisible();
  // `getByText` is a case-insensitive SUBSTRING match, and "No usable result"
  // contains "usable result" — so the negative assertion must be exact.
  await expect(result.getByText('Usable result', { exact: true })).toHaveCount(0);
  await expect(result.getByRole('heading', { name: 'Verified fields' })).toHaveCount(0);
});

test('F4-4b. a partial result with no itemized rows stays honest about having no list', async ({ page }) => {
  // A REJECTED verdict makes the run partial without writing a needs_review
  // row, so this backend-valid payload has an EMPTY needs_review.
  await runTask(page, 'Gamma Swarm', 'f4-rejected', 'a rejected claim please');
  const result = page.getByRole('region', { name: 'Final result' });

  await expect(result.getByText('Partial result')).toBeVisible(TERMINAL);
  await expect(result.getByText(/not a completed result/)).toBeVisible();
  await expect(result.getByText(/did not complete all the work it was required to/)).toBeVisible();
  // The verified half is still reported…
  await expect(result.getByText('Fuel type')).toBeVisible();
  // …and the surface says plainly that no itemized entries exist, rather than
  // pointing at a list that was never recorded.
  await expect(result.getByText(/contains no itemized entries/)).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Outstanding items' })).toBeVisible();
  await expect(result.getByRole('heading', { name: /Conflicts/ })).toHaveCount(0);
  await expect(result.getByRole('heading', { name: /Task failures/ })).toHaveCount(0);
  // Nothing is invented about the rejected claim.
  await expect(result.getByText('horsepower_hp')).toHaveCount(0);
  await expect(result.getByText(/does not infer the missing reason, task or claim/)).toBeVisible();
  const body = await page.content();
  expect(body).not.toMatch(/the verifier rejected|not every claim/i);
});

test('F4-4c. a partial caused only by a task failure keeps the copy true', async ({ page }) => {
  // EVERY gathered claim is VERIFIED here; the run is partial solely because a
  // separate task failed. The surface must not say a claim went unverified.
  await runTask(page, 'Gamma Swarm', 'f4-taskfail', 'an incomplete task please');
  const result = page.getByRole('region', { name: 'Final result' });

  await expect(result.getByText('Partial result')).toBeVisible(TERMINAL);
  await expect(result.getByText(/did not complete all the work it was required to/)).toBeVisible();
  await expect(result.getByText(/not a completed result/)).toBeVisible();
  await expect(result.getByText('Usable result', { exact: true })).toHaveCount(0);

  // The verified half is reported, and the failure lands in its own group.
  await expect(result.getByText('Fuel type')).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Outstanding items (1)' })).toBeVisible();
  await expect(result.getByRole('heading', { name: 'Task failures (1)' })).toBeVisible();

  // No unsupported claim about a claim being unverified or rejected.
  const body = await page.content();
  expect(body).not.toMatch(/not every claim|the verifier rejected/i);
});

test('F4-5. refresh reconstructs the identical result from the durable run output', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-refresh', 'produce a partial report please');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Partial result')).toBeVisible(TERMINAL);
  const before = await result.innerHTML();

  // A real browser refresh: only sessionStorage survives.
  await page.reload();
  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible();
  await page.getByText('Gamma Swarm').click();
  await page.getByText('f4-refresh').click();

  const restored = page.getByRole('region', { name: 'Final result' });
  await expect(restored.getByText('Partial result')).toBeVisible(TERMINAL);
  expect(await restored.innerHTML()).toBe(before);
});

test('F4-6. the surface is usable at a phone width without horizontal scroll', async ({ page }) => {
  // The run is created at desktop width because the Logout control the login
  // helper waits on lives in the sidebar, which is a closed drawer below the
  // desktop breakpoint. The result surface is then narrowed to a phone width,
  // which is what this test is actually about.
  await runTask(page, 'Gamma Swarm', 'f4-mobile', 'produce a partial report please');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Partial result')).toBeVisible(TERMINAL);

  await page.setViewportSize({ width: 375, height: 780 });
  await expect(result.getByText('Partial result')).toBeVisible();

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  // Every outstanding item stays reachable and readable on a narrow screen.
  await expect(result.getByRole('heading', { name: 'Conflicts (1)' })).toBeVisible();
  // Substring match: the field's <dt> carries the humanised label and the
  // durable key together, so its exact text is "Fuel typefuel_type".
  await expect(result.getByText('Fuel type')).toBeVisible();
});

test('F4-7. the result is reachable by keyboard alone', async ({ page }) => {
  await runTask(page, 'Gamma Swarm', 'f4-keyboard', 'produce the final report');
  const result = page.getByRole('region', { name: 'Final result' });
  await expect(result.getByText('Usable result')).toBeVisible(TERMINAL);

  const summary = result.locator('details.final-result-provenance > summary').first();
  await summary.focus();
  await expect(summary).toBeFocused();
  // A native <summary> toggles on Enter with no custom key handling.
  await page.keyboard.press('Enter');
  await expect(result.locator('details.final-result-provenance').first()).toHaveAttribute('open', '');
});

test('F4-8. V1 regression: a vehicle_catalog_v1 run keeps its existing output path', async ({ page }) => {
  await runTask(page, 'Alpha Research', 'f4-v1-control', 'produce the final report');

  await expect(page.getByText(/Run finished with status/)).toBeVisible(TERMINAL);
  // The V1 sanitized-output panel is untouched…
  await expect(page.getByRole('heading', { name: 'Final artifacts' })).toBeVisible();
  await expect(page.getByText(/E2E mocked output/)).toBeVisible();
  // …and the typed Swarm V2 surface never appears for it.
  await expect(page.getByRole('region', { name: 'Final result' })).toHaveCount(0);
});
