// Server-side error reporting for the Next.js gateway (PR-OBS, OBS-5): a no-op
// unless the Vercel variable NEXT_PUBLIC_SENTRY_DSN is set. The SDK is imported
// only then. What an event may carry: lib/observability.ts.
import { configuredDsn, sentryOptions } from './lib/observability';

function options() {
  return sentryOptions({
    dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
    release: process.env.VERCEL_GIT_COMMIT_SHA,
    environment: process.env.VERCEL_ENV,
    tracesRate: process.env.NEXT_PUBLIC_MILO_SENTRY_TRACES_SAMPLE_RATE,
    service: 'milo-frontend-server',
  });
}

// Node.js runtime only: the gateway routes run there; the edge runtime (if
// any route ever used it) gets no SDK.
const NODE = () => process.env.NEXT_RUNTIME === 'nodejs';

export async function register(): Promise<void> {
  const config = options();
  if (!config || !NODE()) return;
  const Sentry = await import('@sentry/nextjs');
  // No default integrations beyond error capture: no HTTP/console
  // instrumentation that could attach request or response material.
  Sentry.init({
    ...config,
    defaultIntegrations: false,
    integrations: [Sentry.linkedErrorsIntegration(), Sentry.dedupeIntegration()],
  });
}

export async function onRequestError(...args: unknown[]): Promise<void> {
  if (!configuredDsn(process.env.NEXT_PUBLIC_SENTRY_DSN) || !NODE()) return;
  const Sentry = await import('@sentry/nextjs');
  // The request's path, method and headers the SDK attaches are reduced to
  // method + path by beforeSend (scrubEvent).
  (Sentry.captureRequestError as (...a: unknown[]) => void)(...args);
}
