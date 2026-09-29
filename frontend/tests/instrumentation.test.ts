import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const init = vi.fn();
const captureRequestError = vi.fn();
vi.mock('@sentry/nextjs', () => ({
  init,
  captureRequestError,
  linkedErrorsIntegration: () => ({ name: 'LinkedErrors' }),
  dedupeIntegration: () => ({ name: 'Dedupe' }),
  globalHandlersIntegration: () => ({ name: 'GlobalHandlers' }),
  setTag: vi.fn(),
}));

const DSN = 'https://publickey@o1.ingest.sentry.io/42';

describe('Next.js instrumentation (PR-OBS)', () => {
  beforeEach(() => {
    init.mockClear();
    captureRequestError.mockClear();
    vi.resetModules();
  });
  afterEach(() => vi.unstubAllEnvs());

  it('initialises nothing without a DSN (server and browser)', async () => {
    vi.stubEnv('NEXT_PUBLIC_SENTRY_DSN', '');
    vi.stubEnv('NEXT_RUNTIME', 'nodejs');
    const server = await import('../instrumentation');
    await server.register();
    await server.onRequestError(new Error('x'), {}, {});
    await import('../instrumentation-client');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(init).not.toHaveBeenCalled();
    expect(captureRequestError).not.toHaveBeenCalled();
  });

  it('initialises the server with the private option set when a DSN is set', async () => {
    vi.stubEnv('NEXT_PUBLIC_SENTRY_DSN', DSN);
    vi.stubEnv('NEXT_RUNTIME', 'nodejs');
    vi.stubEnv('VERCEL_GIT_COMMIT_SHA', 'd'.repeat(40));
    const server = await import('../instrumentation');
    await server.register();
    expect(init).toHaveBeenCalledTimes(1);
    const options = init.mock.calls[0][0];
    expect(options.dsn).toBe(DSN);
    expect(options.sendDefaultPii).toBe(false);
    expect(options.defaultIntegrations).toBe(false);
    expect(options.maxBreadcrumbs).toBe(0);
    expect(options.tracesSampleRate).toBe(0);
    expect(options.release).toBe('d'.repeat(40));
    expect(options.initialScope.tags).toEqual({ service: 'milo-frontend-server' });
  });

  it('does nothing in the edge runtime', async () => {
    vi.stubEnv('NEXT_PUBLIC_SENTRY_DSN', DSN);
    vi.stubEnv('NEXT_RUNTIME', 'edge');
    const server = await import('../instrumentation');
    await server.register();
    await server.onRequestError(new Error('x'), {}, {});
    expect(init).not.toHaveBeenCalled();
    expect(captureRequestError).not.toHaveBeenCalled();
  });

  it('initialises the browser with error capture only when a DSN is set', async () => {
    vi.stubEnv('NEXT_PUBLIC_SENTRY_DSN', DSN);
    await import('../instrumentation-client');
    await vi.waitFor(() => expect(init).toHaveBeenCalledTimes(1));
    const options = init.mock.calls[0][0];
    expect(options.defaultIntegrations).toBe(false);
    expect(options.integrations.map((i: { name: string }) => i.name)).toEqual(['GlobalHandlers', 'LinkedErrors', 'Dedupe']);
    expect(options.initialScope.tags).toEqual({ service: 'milo-frontend-browser' });
  });
});
