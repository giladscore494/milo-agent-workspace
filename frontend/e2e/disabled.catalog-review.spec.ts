import { expect, test } from '@playwright/test';
import {
  PROJECT_ALPHA,
  PROJECT_BETA,
  apiToken,
  authHeaders,
  loginViaUi,
} from './helpers';

/**
 * CODE-3 — the read-only catalog review surface, on the DISABLED stack.
 *
 * This file runs against the stack with EVERY execution flag off and
 * `GATEWAY_ALLOW_EXECUTION_ROUTES` unset. That is the point: the whole claim
 * CODE-3 makes is that reading durable catalog state is not execution, so the
 * surface has to work in exactly the posture an operator rolls back into.
 *
 * The durable rows come from `backend/testing/catalog_review_seed.py` — the
 * committed R5 capture fixtures landed through the real ingestion path. No
 * socket is opened to `data.gov.il` and no model is called.
 */

const CANONICAL = (project: string) => `/api/gateway/projects/${project}/catalog/canonical`;
const REVIEW = (project: string) => `/api/gateway/projects/${project}/catalog/review-candidates`;

test('C1. a member reads the canonical catalog while execution is disabled', async ({
  request,
  baseURL,
}) => {
  const token = await apiToken(request, 'alice');
  const response = await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}`, {
    headers: authHeaders(token),
  });
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.page.limit).toBeGreaterThan(0);
  expect(body.page.total).toBeGreaterThan(0);
  expect(Array.isArray(body.items)).toBeTruthy();
  expect(body.items[0].canonical_key).toMatch(/^cv1\.[0-9a-f]{32}$/);
});

test('C2. a member reads the ready-for-review candidates while execution is disabled', async ({
  request,
  baseURL,
}) => {
  const token = await apiToken(request, 'alice');
  const response = await request.get(`${baseURL}${REVIEW(PROJECT_ALPHA)}`, {
    headers: authHeaders(token),
  });
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.available).toBe(true);
  expect(body.status).toBe('ready_for_review');
  expect(body.snapshot.snapshot_key).toMatch(/^cs1\.[0-9a-f]{32}$/);
  expect(body.snapshot.resource_id).toBe('142afde2-6228-49f9-8a29-9b6c3a0cbe40');
  // Every item is `ready_for_review`, and nothing else got in.
  expect(body.items.length).toBeGreaterThan(0);
  for (const item of body.items) expect(item.status).toBe('ready_for_review');
});

test('C3. run creation stays refused in the same session that reads the catalog', async ({
  request,
  baseURL,
}) => {
  // The two halves of the claim, in one test: the read works AND the execution
  // posture is genuinely off — so C1/C2 are not passing because a flag leaked on.
  const token = await apiToken(request, 'alice');
  expect((await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}`, { headers: authHeaders(token) })).status())
    .toBe(200);
  const blocked = await request.post(
    `${baseURL}/api/gateway/conversations/1f90f4ce-7844-4031-91d6-b74e40e1884e/runs`,
    { headers: authHeaders(token), data: { content: 'go', idempotency_key: 'e2e-catalog-0001' } },
  );
  expect(blocked.status()).toBe(403);
});

test('C4. an unauthenticated catalog read is rejected', async ({ request, baseURL }) => {
  for (const path of [CANONICAL(PROJECT_ALPHA), REVIEW(PROJECT_ALPHA)]) {
    const response = await request.get(`${baseURL}${path}`);
    expect(response.status()).toBe(401);
  }
});

test('C5. a non-member cannot inspect the catalog through another project id', async ({
  request,
  baseURL,
}) => {
  // Alice is not a member of Beta. The catalog is global, so the refusal has to
  // come from the membership check rather than from the rows.
  const token = await apiToken(request, 'alice');
  for (const path of [CANONICAL(PROJECT_BETA), REVIEW(PROJECT_BETA)]) {
    const response = await request.get(`${baseURL}${path}`, { headers: authHeaders(token) });
    expect(response.status()).toBe(404);
    expect(await response.text()).not.toContain('cv1.');
    expect(await response.text()).not.toContain('cs1.');
  }
});

test('C6. every mutating method on both paths is unavailable', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  for (const path of [CANONICAL(PROJECT_ALPHA), REVIEW(PROJECT_ALPHA)]) {
    const post = await request.post(`${baseURL}${path}`, {
      headers: authHeaders(token),
      data: {},
    });
    expect(post.status()).toBe(403);
    for (const method of ['put', 'patch', 'delete'] as const) {
      const response = await request[method](`${baseURL}${path}`, {
        headers: authHeaders(token),
        data: {},
      });
      // No handler is exported for these verbs at all.
      expect(response.status()).toBe(405);
    }
  }
});

test('C7. pagination is bounded by the server, not by the caller', async ({ request, baseURL }) => {
  const token = await apiToken(request, 'alice');
  const refused = await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}?limit=100000`, {
    headers: authHeaders(token),
  });
  expect(refused.status()).toBe(400);
  expect((await refused.json()).error.code).toBe('CATALOG_REVIEW_PAGE_INVALID');

  const negative = await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}?offset=-1`, {
    headers: authHeaders(token),
  });
  expect(negative.status()).toBe(400);

  // A legitimate page, and its exact total.
  const page = await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}?limit=5&offset=0`, {
    headers: authHeaders(token),
  });
  const body = await page.json();
  expect(body.items).toHaveLength(5);
  expect(body.page.has_more).toBe(true);
  expect(body.page.total).toBeGreaterThan(5);
});

test('C8. an unsupported query parameter is inert, not query control', async ({
  request,
  baseURL,
}) => {
  const token = await apiToken(request, 'alice');
  const baseline = await request.get(`${baseURL}${CANONICAL(PROJECT_ALPHA)}?limit=3`, {
    headers: authHeaders(token),
  });
  const steered = await request.get(
    `${baseURL}${CANONICAL(PROJECT_ALPHA)}?limit=3&order=manufacturer.desc&select=*&table=catalog_raw_records&status=promoted`,
    { headers: authHeaders(token) },
  );
  expect(steered.status()).toBe(200);
  expect(await steered.json()).toEqual(await baseline.json());
});

test('C9. no raw register row, evidence, SQL or credential reaches the browser', async ({
  request,
  baseURL,
}) => {
  const token = await apiToken(request, 'alice');
  for (const path of [CANONICAL(PROJECT_ALPHA), REVIEW(PROJECT_ALPHA)]) {
    const text = await (
      await request.get(`${baseURL}${path}?limit=100`, { headers: authHeaders(token) })
    ).text();
    for (const forbidden of [
      'tozar', 'kinuy_mishari', 'degem_nm', 'koah_sus', 'ramat_gimur',
      'payload', 'payload_sha256', 'source_locator', 'upstream_record_id',
      'chain_of_thought', 'lease_token', 'worker_id', 'service_role',
      'variant_id', 'field_revisions', 'pg_catalog', 'errcode',
      // SQL markers that cannot occur in register identity text. A bare
      // "SELECT" would be a false positive: the pinned capture really does
      // state a trim called SELECTION.
      'SELECT ', 'select ', 'FROM catalog', 'from public.',
    ]) {
      expect(text, `${forbidden} in ${path}`).not.toContain(forbidden);
    }
  }
});

test('C10. the workspace renders both views, read only', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();

  const panel = page.getByRole('region', { name: 'Catalog review' });
  await expect(panel).toBeVisible();
  await panel.getByRole('button', { name: 'Show' }).click();

  // Canonical first. The claim is about the ROWS, so it is scoped to the
  // table: the word "Candidates" also sits in the other tab's hint, which is
  // rendered in both views by design.
  await expect(panel.getByRole('table')).toBeVisible();
  await expect(panel.getByRole('tab', { name: /Canonical catalog/ }))
    .toHaveAttribute('aria-selected', 'true');
  await expect(panel.getByRole('table').getByText('not canonical')).toHaveCount(0);
  await expect(panel.getByText('Government candidates awaiting review')).toHaveCount(0);

  // Then the review view, clearly marked as NOT canonical.
  await panel.getByRole('tab', { name: /Ready for review/ }).click();
  await expect(panel.getByText('Government candidates awaiting review')).toBeVisible();
  await expect(panel.getByRole('table').getByText('Candidate').first()).toBeVisible();
  await expect(panel.getByRole('table').getByText('not canonical').first()).toBeVisible();

  // No control on this surface can change anything.
  const labels = await panel.getByRole('button').allTextContents();
  for (const label of labels) {
    expect(label).not.toMatch(/approve|reject|promote|capture|delete|activate|enable/i);
  }
});

test('C11. pagination works in the browser and stays bounded', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  const panel = page.getByRole('region', { name: 'Catalog review' });
  await panel.getByRole('button', { name: 'Show' }).click();
  await expect(panel.getByRole('table')).toBeVisible();

  await expect(panel.getByRole('button', { name: 'Previous page' })).toBeDisabled();
  await expect(panel.getByText(/Showing from row 1 of \d+\./)).toBeVisible();
  await panel.getByRole('button', { name: 'Next page' }).click();
  await expect(panel.getByText(/Showing from row 26 of \d+\./)).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Previous page' })).toBeEnabled();
});

test('C12. the surface is usable at mobile width', async ({ page }) => {
  // Sign in and select the project at desktop width, THEN narrow: below the
  // desktop breakpoint the project list lives in a drawer, and the sign-in
  // screen has no drawer to open.
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  await page.setViewportSize({ width: 390, height: 844 });

  const panel = page.getByRole('region', { name: 'Catalog review' });
  await expect(panel).toBeVisible();
  await panel.getByRole('button', { name: 'Show' }).click();
  await expect(panel.getByRole('table')).toBeVisible();

  // The wide table scrolls inside its own region; the PAGE never scrolls
  // sideways, which is what makes a phone usable rather than merely rendered.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test('C13. no browser bundle or page carries a catalog secret', async ({ page }) => {
  await loginViaUi(page, 'alice');
  await page.getByText('Alpha Research').click();
  const panel = page.getByRole('region', { name: 'Catalog review' });
  await panel.getByRole('button', { name: 'Show' }).click();
  await expect(panel.getByRole('table')).toBeVisible();

  const html = await page.content();
  for (const forbidden of ['service_role', 'sb_secret_', 'lease_token', 'SUPABASE_SERVICE_ROLE_KEY']) {
    expect(html).not.toContain(forbidden);
  }
});
