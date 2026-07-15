import { APIRequestContext, Page, expect } from '@playwright/test';

export const USERS = {
  alice: { email: 'alice@example.com', password: 'alice-Password-1', id: 'aaaaaaaa-1111-4111-8111-000000000001' },
  bob: { email: 'bob@example.com', password: 'bob-Password-1', id: 'aaaaaaaa-1111-4111-8111-000000000002' },
  mallory: { email: 'mallory@example.com', password: 'mallory-Password-1', id: 'aaaaaaaa-1111-4111-8111-000000000003' },
};

export const PROJECT_ALPHA = 'bbbbbbbb-1111-4111-8111-000000000001';
export const PROJECT_BETA = 'bbbbbbbb-1111-4111-8111-000000000002';

const MOCK_SUPABASE = 'http://127.0.0.1:9998';

export async function loginViaUi(page: Page, user: keyof typeof USERS): Promise<void> {
  await page.goto('/');
  await page.getByLabel('Email').fill(USERS[user].email);
  await page.getByLabel('Password').fill(USERS[user].password);
  await page.getByRole('button', { name: 'Login' }).click();
  await expect(page.getByRole('button', { name: 'Logout' })).toBeVisible();
}

export async function apiToken(request: APIRequestContext, user: keyof typeof USERS): Promise<string> {
  const response = await request.post(`${MOCK_SUPABASE}/auth/v1/token?grant_type=password`, {
    data: { email: USERS[user].email, password: USERS[user].password },
  });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  return body.access_token as string;
}

export function authHeaders(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

export async function createConversation(
  request: APIRequestContext,
  baseURL: string,
  token: string,
  projectId: string,
  title: string,
): Promise<string> {
  const response = await request.post(`${baseURL}/api/gateway/projects/${projectId}/conversations`, {
    headers: authHeaders(token),
    data: { title },
  });
  expect(response.status()).toBe(201);
  return (await response.json()).id as string;
}
