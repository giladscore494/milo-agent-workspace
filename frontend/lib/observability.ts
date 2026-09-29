/**
 * Frontend error reporting to Sentry (PR-OBS, OBS-5) -- OFF unless the Vercel
 * environment variable `NEXT_PUBLIC_SENTRY_DSN` is set. Without it nothing is
 * initialised and the SDK is never loaded (instrumentation.ts,
 * instrumentation-client.ts import it dynamically, behind the DSN check), so
 * the site builds and runs exactly as before.
 *
 * What an event may carry is decided here:
 *   - `sendDefaultPii: false`, no breadcrumbs, no session replay, traces off
 *     (NEXT_PUBLIC_MILO_SENTRY_TRACES_SAMPLE_RATE, capped at 0.05);
 *   - `scrubEvent` (beforeSend AND beforeSendTransaction) removes request
 *     bodies, headers, cookies and query strings, `user`, `extra`,
 *     breadcrumbs, stack-frame local variables, exception MESSAGES (they can
 *     quote a prompt, a model output, a tool result or a register payload),
 *     span data, and every context but runtime/os/browser/trace.
 *   - every event is tagged with release (the deployed commit), service and,
 *     when the page has one, run_id.
 */

export const MAX_TRACES_SAMPLE_RATE = 0.05;
export const REDACTED = '[redacted]';
const DISABLED_VALUES = new Set(['', 'disabled', 'off', 'none', 'false', '0']);
const SAFE_CONTEXTS = new Set(['runtime', 'os', 'browser', 'trace']);
const SAFE_TAGS = new Set(['service', 'run_id', 'error_code']);
const DSN_SHAPE = /^https:\/\/[^/@\s]+@[^/\s]+\/\d+$/;
const RUN_ID_SHAPE = /^[0-9a-fA-F-]{8,64}$/;
const PATH_RUN_ID = /\/runs\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\/|$)/;

type Json = Record<string, unknown>;

/** The DSN to use, or '' when reporting is off (empty, "disabled", or malformed). */
export function configuredDsn(value: string | undefined | null): string {
  const dsn = (value ?? '').trim();
  if (DISABLED_VALUES.has(dsn.toLowerCase())) return '';
  return DSN_SHAPE.test(dsn) ? dsn : '';
}

export function tracesSampleRate(value: string | undefined | null): number {
  const rate = Number((value ?? '').trim() || '0');
  if (!Number.isFinite(rate) || rate <= 0) return 0;
  return Math.min(rate, MAX_TRACES_SAMPLE_RATE);
}

export function releaseSha(value: string | undefined | null): string | undefined {
  const sha = (value ?? '').trim().toLowerCase();
  return /^[0-9a-f]{40}$/.test(sha) ? sha : undefined;
}

function stripQuery(url: unknown): string | undefined {
  if (typeof url !== 'string') return undefined;
  try {
    const parsed = new URL(url, 'http://relative.invalid');
    const origin = parsed.origin === 'http://relative.invalid' ? '' : parsed.origin;
    return `${origin}${parsed.pathname}`;
  } catch {
    return undefined;
  }
}

function scrubFrames(container: unknown): void {
  if (!container || typeof container !== 'object') return;
  const stacktrace = (container as Json).stacktrace as Json | undefined;
  const frames = stacktrace && Array.isArray(stacktrace.frames) ? stacktrace.frames : [];
  for (const frame of frames) {
    if (frame && typeof frame === 'object') delete (frame as Json).vars;
  }
}

/** Remove every field class that can carry request, user or model material. */
export function scrubEvent<T>(event: T): T {
  if (!event || typeof event !== 'object') return event;
  const e = event as unknown as Json;
  const request = e.request as Json | undefined;
  let pathRunId: string | undefined;
  if (request && typeof request === 'object' && typeof request.url === 'string') {
    const path = stripQuery(request.url) ?? '';
    pathRunId = PATH_RUN_ID.exec(path)?.[1]?.toLowerCase();
  }
  if (request && typeof request === 'object') {
    const kept: Json = {};
    if (typeof request.method === 'string') kept.method = request.method;
    const url = stripQuery(request.url);
    if (url) kept.url = url;
    e.request = kept;
  } else {
    delete e.request;
  }
  for (const key of ['user', 'extra', 'breadcrumbs', 'modules', 'server_name']) delete e[key];
  if (e.contexts && typeof e.contexts === 'object') {
    const contexts: Json = {};
    for (const [key, value] of Object.entries(e.contexts as Json)) {
      if (SAFE_CONTEXTS.has(key)) contexts[key] = value;
    }
    if (contexts.trace && typeof contexts.trace === 'object') delete (contexts.trace as Json).data;
    e.contexts = contexts;
  }
  if (e.tags && typeof e.tags === 'object' && !Array.isArray(e.tags)) {
    const tags: Json = {};
    for (const [key, value] of Object.entries(e.tags as Json)) {
      if (SAFE_TAGS.has(key)) tags[key] = value;
    }
    e.tags = tags;
  } else {
    delete e.tags;
  }
  if (pathRunId) {
    e.tags = { ...((e.tags as Json | undefined) ?? {}), run_id: pathRunId };
  }
  if (e.logentry && typeof e.logentry === 'object') {
    delete (e.logentry as Json).params;
    delete (e.logentry as Json).formatted;
  }
  const exception = e.exception as Json | undefined;
  const values = exception && Array.isArray(exception.values) ? exception.values : [];
  for (const value of values) {
    if (value && typeof value === 'object') {
      if ((value as Json).value) (value as Json).value = REDACTED;
      scrubFrames(value);
    }
  }
  const threads = e.threads as Json | undefined;
  for (const thread of threads && Array.isArray(threads.values) ? threads.values : []) scrubFrames(thread);
  if (Array.isArray(e.spans)) {
    for (const span of e.spans) {
      if (span && typeof span === 'object') {
        const s = span as Json;
        delete s.data;
        delete s.tags;
        if (typeof s.description === 'string') s.description = stripQuery(s.description) ?? REDACTED;
      }
    }
  }
  return event;
}

export type SentryOptions = {
  dsn: string;
  release?: string;
  environment: string;
  sendDefaultPii: false;
  tracesSampleRate: number;
  maxBreadcrumbs: 0;
  beforeBreadcrumb: () => null;
  beforeSend: <T>(event: T) => T;
  beforeSendTransaction: <T>(event: T) => T;
  initialScope: { tags: Record<string, string> };
};

/** The one option set both runtimes use; null when reporting is off. */
export function sentryOptions(env: {
  dsn?: string | null;
  release?: string | null;
  environment?: string | null;
  tracesRate?: string | null;
  service: string;
}): SentryOptions | null {
  const dsn = configuredDsn(env.dsn);
  if (!dsn) return null;
  return {
    dsn,
    release: releaseSha(env.release),
    environment: (env.environment ?? '').trim() || 'production',
    sendDefaultPii: false,
    tracesSampleRate: tracesSampleRate(env.tracesRate),
    maxBreadcrumbs: 0,
    beforeBreadcrumb: () => null,
    beforeSend: scrubEvent,
    beforeSendTransaction: scrubEvent,
    initialScope: { tags: { service: env.service } },
  };
}

/** Tag later events with the run the page is showing (a no-op when off). */
export function setRunIdTag(runId: string | null | undefined): void {
  if (!runId || !RUN_ID_SHAPE.test(runId)) return;
  if (!configuredDsn(process.env.NEXT_PUBLIC_SENTRY_DSN)) return;
  void import('@sentry/nextjs').then((Sentry) => Sentry.setTag('run_id', runId)).catch(() => undefined);
}
