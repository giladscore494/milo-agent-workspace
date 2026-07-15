import { getVercelOidcToken } from '@vercel/oidc';
import { ExternalAccountClient } from 'google-auth-library';

const CLOUD_PLATFORM_SCOPE =
  'https://www.googleapis.com/auth/cloud-platform';

function requireEnvironmentVariable(name: string): string {
  const value = process.env[name]?.trim();

  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }

  return value;
}

export function getCloudRunServiceUrl(): string {
  return requireEnvironmentVariable('CLOUD_RUN_API_URL').replace(/\/+$/, '');
}

function isIsolatedE2eMode(): boolean {
  // Explicit test-only escape for the isolated E2E stack, where the
  // "Cloud Run" upstream is a local mock and no Google credentials exist.
  // Hard-disabled in production builds regardless of the variable.
  return (
    process.env.CLOUD_RUN_AUTH_MODE === 'e2e-test' &&
    process.env.NODE_ENV !== 'production' &&
    (process.env.VERCEL_ENV ?? '') !== 'production'
  );
}

export async function getCloudRunIdToken(): Promise<string> {
  if (isIsolatedE2eMode()) {
    return 'e2e-local-gateway-token';
  }
  const projectNumber = requireEnvironmentVariable('GCP_PROJECT_NUMBER');
  const poolId = requireEnvironmentVariable(
    'GCP_WORKLOAD_IDENTITY_POOL_ID',
  );
  const providerId = requireEnvironmentVariable(
    'GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID',
  );
  const serviceAccountEmail = requireEnvironmentVariable(
    'GCP_SERVICE_ACCOUNT_EMAIL',
  );
  const serviceUrl = getCloudRunServiceUrl();

  const workloadIdentityAudience =
    `//iam.googleapis.com/projects/${projectNumber}` +
    `/locations/global/workloadIdentityPools/${poolId}` +
    `/providers/${providerId}`;

  const externalAccountClient = ExternalAccountClient.fromJSON({
    type: 'external_account',
    audience: workloadIdentityAudience,
    subject_token_type: 'urn:ietf:params:oauth:token-type:jwt',
    token_url: 'https://sts.googleapis.com/v1/token',
    scopes: [CLOUD_PLATFORM_SCOPE],
    subject_token_supplier: {
      getSubjectToken: () =>
        getVercelOidcToken({
          expirationBufferMs: 60_000,
        }),
    },
  });

  if (!externalAccountClient) {
    throw new Error('Unable to initialize Google external account client');
  }

  const accessTokenResponse = await externalAccountClient.getAccessToken();
  const accessToken = accessTokenResponse.token;

  if (!accessToken) {
    throw new Error('Google STS did not return an access token');
  }

  const generateIdTokenUrl =
    'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' +
    `${encodeURIComponent(serviceAccountEmail)}:generateIdToken`;

  const response = await fetch(generateIdTokenUrl, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${accessToken}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      audience: serviceUrl,
      includeEmail: true,
    }),
    cache: 'no-store',
  });

  if (!response.ok) {
    throw new Error(
      `Google IAM generateIdToken failed with status ${response.status}`,
    );
  }

  const payload = (await response.json()) as { token?: string };

  if (!payload.token) {
    throw new Error('Google IAM did not return an ID token');
  }

  return payload.token;
}
