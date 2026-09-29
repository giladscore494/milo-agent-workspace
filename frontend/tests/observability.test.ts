import { describe, expect, it } from 'vitest';
import {
  MAX_TRACES_SAMPLE_RATE,
  REDACTED,
  configuredDsn,
  releaseSha,
  scrubEvent,
  sentryOptions,
  tracesSampleRate,
} from '../lib/observability';

const SENTINEL = 'PROMPT-SENTINEL-do-not-ship';
const DSN = 'https://publickey@o1.ingest.sentry.io/42';

describe('frontend error reporting (PR-OBS, OBS-5)', () => {
  it('is off without a DSN, and the test environment has none', () => {
    expect(configuredDsn(undefined)).toBe('');
    expect(configuredDsn('')).toBe('');
    expect(configuredDsn('disabled')).toBe('');
    expect(configuredDsn(`not-a-dsn-${SENTINEL}`)).toBe('');
    expect(sentryOptions({ dsn: undefined, service: 'x' })).toBeNull();
    expect(configuredDsn(process.env.NEXT_PUBLIC_SENTRY_DSN)).toBe('');
  });

  it('builds a private option set when a DSN is configured', () => {
    const options = sentryOptions({ dsn: DSN, release: 'A'.repeat(40), environment: 'production', service: 'milo-frontend-browser' });
    expect(options).not.toBeNull();
    expect(options!.sendDefaultPii).toBe(false);
    expect(options!.maxBreadcrumbs).toBe(0);
    expect(options!.beforeBreadcrumb()).toBeNull();
    expect(options!.tracesSampleRate).toBe(0);
    expect(options!.release).toBe('a'.repeat(40));
    expect(options!.initialScope.tags).toEqual({ service: 'milo-frontend-browser' });
  });

  it('keeps traces off by default and never above the cap', () => {
    expect(tracesSampleRate(undefined)).toBe(0);
    expect(tracesSampleRate('0.01')).toBe(0.01);
    expect(tracesSampleRate('1')).toBe(MAX_TRACES_SAMPLE_RATE);
    expect(tracesSampleRate('junk')).toBe(0);
    expect(tracesSampleRate('-1')).toBe(0);
  });

  it('accepts only a full commit sha as the release', () => {
    expect(releaseSha('abc')).toBeUndefined();
    expect(releaseSha('b'.repeat(40))).toBe('b'.repeat(40));
  });

  it('removes every sensitive field class', () => {
    const event = scrubEvent({
      message: 'boom',
      request: {
        method: 'POST', url: `https://milo.example/api/runs?token=${SENTINEL}`,
        query_string: `q=${SENTINEL}`, data: { prompt: SENTINEL },
        headers: { Authorization: `Bearer ${SENTINEL}` }, cookies: { s: SENTINEL },
      },
      user: { email: `${SENTINEL}@example.com` },
      extra: { modelOutput: SENTINEL, toolResult: SENTINEL },
      breadcrumbs: [{ message: SENTINEL }],
      contexts: { browser: { name: 'Chrome' }, response: { body: SENTINEL }, payload: { row: SENTINEL } },
      tags: { service: 'milo-frontend-browser', url: SENTINEL },
      exception: { values: [{ type: 'TypeError', value: `bad ${SENTINEL}`,
        stacktrace: { frames: [{ function: 'f', vars: { prompt: SENTINEL } }] } }] },
      spans: [{ description: `GET https://milo.example/api?x=${SENTINEL}`, data: { q: SENTINEL } }],
    });
    expect(JSON.stringify(event)).not.toContain(SENTINEL);
    expect(event.request).toEqual({ method: 'POST', url: 'https://milo.example/api/runs' });
    expect(event.exception.values[0].type).toBe('TypeError');
    expect(event.exception.values[0].value).toBe(REDACTED);
    expect(event.tags).toEqual({ service: 'milo-frontend-browser' });
    expect(Object.keys(event.contexts)).toEqual(['browser']);
    expect('user' in event).toBe(false);
    expect('extra' in event).toBe(false);
    expect('breadcrumbs' in event).toBe(false);
  });

  it('survives odd shapes', () => {
    expect(scrubEvent(null)).toBeNull();
    const odd = scrubEvent({ request: 'x', tags: ['a'] } as Record<string, unknown>);
    expect('request' in odd).toBe(false);
    expect('tags' in odd).toBe(false);
  });
});

describe('review follow-ups', () => {
  it('tags a run named in the request path and drops logentry parameters', () => {
    const event = scrubEvent({
      request: { method: 'GET', url: 'https://milo.example/api/gateway/runs/0F8FAD5B-D9CB-469F-A165-70867728950E/events?after=3' },
      logentry: { message: 'x', params: [SENTINEL], formatted: SENTINEL },
    }) as Record<string, any>;
    expect(event.tags).toEqual({ run_id: '0f8fad5b-d9cb-469f-a165-70867728950e' });
    expect(JSON.stringify(event)).not.toContain(SENTINEL);
  });
});
