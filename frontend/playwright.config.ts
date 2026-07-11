import { defineConfig } from '@playwright/test';

/**
 * Isolated E2E stack. Two complete frontend+backend pairs run side by side:
 *
 * - DISABLED stack (:3100 → :8100): every execution flag off — proves the
 *   default posture (403s, read-only workspace, no worker access).
 * - ENABLED stack (:3101 → :8101): execution flags on with a mocked
 *   in-process worker and mocked model adapters — proves the full
 *   lifecycle without any real Cloud Run, Supabase or paid model call.
 *
 * A mock Supabase Auth server (:9998) issues test sessions for both.
 */

const MOCK_SUPABASE = 'http://127.0.0.1:9998';
const REPO_ROOT = '..';

const sharedFrontendEnv = {
  NEXT_PUBLIC_SUPABASE_URL: MOCK_SUPABASE,
  NEXT_PUBLIC_SUPABASE_ANON_KEY: 'e2e-anon-key',
  CLOUD_RUN_AUTH_MODE: 'e2e-test',
  NEXT_TELEMETRY_DISABLED: '1',
  // Every E2E request comes from 127.0.0.1, so the per-IP/per-user gateway
  // limits must be raised for the suite (they are separately unit-tested).
  GATEWAY_RATE_LIMIT_UNAUTH_REQUESTS: '1000',
  GATEWAY_RATE_LIMIT_AUTH_PRESSURE_REQUESTS: '2000',
  GATEWAY_RATE_LIMIT_AUTHENTICATED_REQUESTS: '2000',
  GATEWAY_RATE_LIMIT_POLLING_REQUESTS: '2000',
  GATEWAY_RATE_LIMIT_RUN_CREATION_REQUESTS: '200',
  GATEWAY_RATE_LIMIT_CANCELLATION_REQUESTS: '200',
};

const backendEnv = {
  SUPABASE_URL: 'https://example.supabase.co',
  SUPABASE_SERVICE_ROLE_KEY: 'e2e-offline-placeholder',
  JOB_LAUNCHER: 'disabled',
};

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 2,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'line' : 'list',
  use: {
    trace: 'retain-on-failure',
    launchOptions: {
      // Environments with a pre-installed Chromium (e.g. this repo's remote
      // sandbox) can point here instead of downloading a matching build.
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    },
  },
  projects: [
    {
      name: 'disabled-stack',
      testMatch: /disabled\..*\.spec\.ts/,
      use: { baseURL: 'http://127.0.0.1:3100' },
    },
    {
      name: 'enabled-stack',
      testMatch: /enabled\..*\.spec\.ts/,
      use: { baseURL: 'http://127.0.0.1:3101' },
    },
  ],
  webServer: [
    {
      command: 'node e2e/mock-supabase.mjs',
      cwd: REPO_ROOT,
      url: `${MOCK_SUPABASE}/auth/v1/user`,
      ignoreHTTPSErrors: true,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      env: { MOCK_SUPABASE_PORT: '9998' },
    },
    {
      command: 'python3 -m uvicorn backend.testing.e2e_app:app --host 127.0.0.1 --port 8100',
      cwd: REPO_ROOT,
      url: 'http://127.0.0.1:8100/health',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { ...backendEnv },
    },
    {
      command: 'python3 -m uvicorn backend.testing.e2e_app:app --host 127.0.0.1 --port 8101',
      cwd: REPO_ROOT,
      url: 'http://127.0.0.1:8101/health',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        ...backendEnv,
        MILO_E2E_INPROCESS_WORKER: 'true',
        MILO_ENABLE_RUN_CREATION: 'true',
        MILO_ENABLE_PROPOSAL_MUTATIONS: 'true',
        MILO_ENABLE_PROPOSAL_READS: 'true',
        MILO_ENABLE_RUN_CANCELLATION: 'true',
        MILO_ENABLE_EXECUTION_CONTROL: 'true',
        MILO_WORKER_AUDIENCE: 'http://127.0.0.1:8101',
        MILO_APPROVED_WORKER_IDENTITIES: 'e2e-worker@example-project.iam.gserviceaccount.com',
        MILO_RATE_LIMIT_RUN_CREATION_USER: '100',
        MILO_RATE_LIMIT_RUN_CREATION_PROJECT: '200',
        MILO_RATE_LIMIT_CANCELLATION: '100',
      },
    },
    {
      command: 'npx next dev -p 3100',
      url: 'http://127.0.0.1:3100',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        ...sharedFrontendEnv,
        CLOUD_RUN_API_URL: 'http://127.0.0.1:8100',
        NEXT_DIST_DIR: '.next-e2e-disabled',
      },
    },
    {
      command: 'npx next dev -p 3101',
      url: 'http://127.0.0.1:3101',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        ...sharedFrontendEnv,
        CLOUD_RUN_API_URL: 'http://127.0.0.1:8101',
        NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI: 'true',
        GATEWAY_ALLOW_EXECUTION_ROUTES: 'true',
        NEXT_DIST_DIR: '.next-e2e-enabled',
      },
    },
  ],
});
