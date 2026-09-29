// Browser error reporting (PR-OBS, OBS-5): a no-op unless the Vercel variable
// NEXT_PUBLIC_SENTRY_DSN is set at build time. The SDK is imported only then,
// so without it the browser never downloads it. What an event may carry:
// lib/observability.ts.
import { sentryOptions } from './lib/observability';

const options = sentryOptions({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  release: process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA,
  environment: process.env.NEXT_PUBLIC_VERCEL_ENV,
  tracesRate: process.env.NEXT_PUBLIC_MILO_SENTRY_TRACES_SAMPLE_RATE,
  service: 'milo-frontend-browser',
});

if (options) {
  void import('@sentry/nextjs')
    .then((Sentry) =>
      Sentry.init({
        ...options,
        // Error capture only: no breadcrumbs, no HTTP context (URL, headers),
        // no session tracking, no replay.
        defaultIntegrations: false,
        integrations: [
          Sentry.globalHandlersIntegration(),
          Sentry.linkedErrorsIntegration(),
          Sentry.dedupeIntegration(),
        ],
      }),
    )
    .catch(() => undefined);
}
