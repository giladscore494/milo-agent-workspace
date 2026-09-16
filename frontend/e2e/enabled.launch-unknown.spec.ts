import { APIRequestContext, expect, test } from '@playwright/test';
import { authHeaders, apiToken, createConversation, loginViaUi, PROJECT_ALPHA } from './helpers';

/**
 * Stage F5 — `launch_unknown`, end to end, through the production condition.
 *
 * The API reaches this state when a worker-launch request may or may not have
 * started an execution (`JobLaunchUncertain`, raised by
 * `CloudRunJobLauncher.launch` when the connection breaks after the request
 * went out). Nothing about the handling is stubbed here: the test-only launcher
 * seam raises that exact exception, and `backend/main.py` does the rest —
 * parking the run, emitting the event, refusing to relaunch.
 *
 * The property under test is a SAFETY property, not a feature: a run that might
 * already be executing must never be launched a second time by anything the
 * user or the browser does. Automatic reconciliation is INTENTIONALLY_DEFERRED
 * (`docs/production-readiness/FINAL_ACCEPTANCE.md`) because guessing wrong
 * means a double execution and a double spend, so the only correct behaviour
 * is to park, say so, and wait for an operator.
 */

const UNCERTAIN = 'please attempt an uncertain launch of this run';

async function eventsFor(request: APIRequestContext, baseURL: string, token: string, runId: string) {
  const response = await request.get(`${baseURL}/api/gateway/runs/${runId}/events`, { headers: authHeaders(token) });
  expect(response.status()).toBe(200);
  return (await response.json()) as { id: number; event_type: string; payload?: Record<string, unknown> }[];
}

test('F5-L1. an uncertain launch parks the run and a replay returns it without relaunching', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const conversationId = await createConversation(request, baseURL!, token, PROJECT_ALPHA, 'f5-launch-unknown');
  const body = { content: UNCERTAIN, metadata: {}, idempotency_key: 'f5-launch-unknown-0001' };

  // 1. The first attempt fails SAFELY: a 502 carrying a classification, not a
  //    provider message, a stack trace or an internal URL.
  const first = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token), data: body,
  });
  expect(first.status()).toBe(502);
  const firstBody = await first.json();
  expect(firstBody.error.code).toBe('JOB_LAUNCH_UNKNOWN');
  expect(firstBody.error.message).toContain('parked for reconciliation');
  expect(JSON.stringify(firstBody)).not.toMatch(/Traceback|run\.googleapis\.com|Bearer |sk-[A-Za-z0-9_-]{8,}/i);

  // 2. An EXPLICIT replay with the same key returns the SAME run, accepted.
  const replay = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token), data: body,
  });
  expect(replay.status()).toBe(202);
  const runId = (await replay.json()).run_id as string;
  expect(runId).toBeTruthy();

  const again = await request.post(`${baseURL}/api/gateway/conversations/${conversationId}/runs`, {
    headers: authHeaders(token), data: body,
  });
  expect(again.status()).toBe(202);
  expect((await again.json()).run_id).toBe(runId);

  // 3. The run is parked, not terminal, and flagged for reconciliation.
  const read = await request.get(`${baseURL}/api/gateway/runs/${runId}`, { headers: authHeaders(token) });
  expect(read.status()).toBe(200);
  const run = await read.json();
  expect(run.launch_state).toBe('launch_unknown');
  expect(run.launch_reconciliation_required).toBe(true);
  expect(run.status).toBe('queued');
  // The launch exception itself is operational data and never crosses to the
  // browser: only the finite classification does.
  expect(run.launch_error).toBeUndefined();
  expect(run.lease_token).toBeUndefined();

  // 4. The launcher ran exactly ONCE. A second invocation would have appended
  //    a second launch_failed event; a successful one would have appended
  //    run_created and started a worker.
  const events = await eventsFor(request, baseURL!, token, runId);
  const launchFailures = events.filter((event) => event.event_type === 'launch_failed');
  expect(launchFailures).toHaveLength(1);
  expect(launchFailures[0].payload?.reconciliation_required).toBe(true);
  expect(launchFailures[0].payload?.recoverable).toBe(false);
  expect(events.filter((event) => event.event_type === 'run_created')).toHaveLength(0);
  expect(events.filter((event) => event.event_type === 'run_started')).toHaveLength(0);

  // 5. Still nothing after time passes: no timer, no poll and no API path
  //    relaunches a parked run on its own.
  await new Promise((resolve) => setTimeout(resolve, 3_000));
  const later = await eventsFor(request, baseURL!, token, runId);
  expect(later.filter((event) => event.event_type === 'launch_failed')).toHaveLength(1);
  expect(later.filter((event) => event.event_type === 'run_started')).toHaveLength(0);
  const stillParked = await (await request.get(`${baseURL}/api/gateway/runs/${runId}`, { headers: authHeaders(token) })).json();
  expect(stillParked.status).toBe('queued');
  expect(stillParked.launch_state).toBe('launch_unknown');
});

test('F5-L2. the browser shows a safe error, then that reconciliation is required and nothing retries', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.getByLabel('Conversation title').fill('f5-launch-unknown-ui');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await page.getByLabel('Task content').fill(UNCERTAIN);
  await page.getByRole('button', { name: 'Send task' }).click();

  // The first attempt surfaces a safe, classified error — and no run opens,
  // because the API did not return one.
  // Scoped to the workspace's own alert: Next.js keeps an empty
  // `role="alert"` route announcer in the document at all times.
  const alert = page.locator('p.alert[role="alert"]');
  await expect(alert).toContainText('JOB_LAUNCH_UNKNOWN');
  // The MEANING is preserved from copy authored in lib/errorText.ts, not
  // repeated from the API's own sentence — which the surface never shows.
  await expect(alert).toContainText('parked for operator reconciliation');
  await expect(alert).toContainText('will not be relaunched automatically');
  await expect(alert).not.toContainText('worker launch outcome is unknown; the run is parked');
  await expect(page.locator('body')).not.toContainText('Traceback');

  // The user retries explicitly. Same content, same idempotency key, so the
  // API returns the parked run instead of creating or launching a second one.
  await page.getByRole('button', { name: 'Send task' }).click();

  const note = page.getByRole('status');
  await expect(note).toContainText('Launch outcome unknown', { timeout: 20_000 });
  await expect(note).toContainText('operator reconciliation required');
  await expect(note).toContainText('will not be relaunched automatically');
  await expect(note).toContainText('nothing on this screen retries it');

  // It is parked, not finished: no terminal verdict is shown for it.
  await expect(page.getByText(/Run finished with status/)).toHaveCount(0);

  // And it stays that way while the page keeps polling.
  await page.waitForTimeout(5_000);
  await expect(note).toContainText('operator reconciliation required');
  await expect(page.getByText(/Run finished with status/)).toHaveCount(0);
  await expect(page.getByText('run_started')).toHaveCount(0);
});
