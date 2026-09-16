import { describe, expect, it } from 'vitest';
import { ApiError } from '../lib/api';
import { AuthFailure, CLASSIFIED_ERROR_CODES, classifyError, safeErrorText } from '../lib/errorText';
import { API_KEY_SENTINEL } from './secretSentinels';

/**
 * The error surface shows locally authored copy, or the caller's fallback.
 * Never upstream text.
 *
 * The first version of this module asked whether a message LOOKED unsafe and
 * displayed anything that did not. Every string below passes that test — no
 * credential, no URL, no stack frame, no markup — and every one of them is a
 * sentence about infrastructure the user does not operate, written by a system
 * nobody here controls. They are the reason "looks harmless" is not
 * authorization.
 */
const UPSTREAM_PROSE = [
  'OpenRouter upstream quota exhausted for provider account',
  'Moonshot request rejected by upstream',
  'PostgREST connection pool exhausted',
  // A Cloud Run / provider diagnostic that is perfectly clean-looking.
  'Container instance exceeded its memory limit and was recycled',
  'The model returned an empty completion after 3 attempts',
  'connection reset while reading the response body',
];

describe('no upstream prose reaches the rendered UI', () => {
  it('refuses provider, repository and infrastructure prose behind a known code', () => {
    for (const message of UPSTREAM_PROSE) {
      const shown = safeErrorText(new ApiError(502, 'REPOSITORY_ERROR', message), 'Run creation failed.');
      expect(shown, message).toBe('Run creation failed.');
      expect(shown, message).not.toContain(message);
    }
  });

  it('refuses the same prose behind an UNKNOWN code of the right shape', () => {
    // A SCREAMING_SNAKE code is a shape, not a classification.
    for (const message of UPSTREAM_PROSE) {
      const shown = safeErrorText(new ApiError(500, 'SOME_FUTURE_BACKEND_CODE', message), 'Run creation failed.');
      expect(shown, message).toBe('Run creation failed.');
    }
  });

  it('refuses the same prose from a plain Error', () => {
    for (const message of UPSTREAM_PROSE) {
      const shown = safeErrorText(new Error(message), 'Failed to load projects.');
      expect(shown, message).toBe('Failed to load projects.');
    }
  });

  it('refuses a credential even when the code is one we classify', () => {
    const shown = safeErrorText(
      new ApiError(403, 'EXECUTION_SURFACE_DISABLED', `upstream said ${API_KEY_SENTINEL}`),
      'Run creation failed.',
    );
    // The authored copy is shown; the upstream string is not consulted at all.
    expect(shown).toContain('turned off at the current activation stage');
    expect(shown).not.toContain(API_KEY_SENTINEL);
  });

  it('never returns an error message, whatever it contains', () => {
    // The property, stated once over a spread of shapes.
    const messages = [...UPSTREAM_PROSE, 'ordinary operational prose', 'run creation is disabled', ''];
    for (const message of messages) {
      for (const error of [new Error(message), new ApiError(500, 'UNKNOWN_CODE_HERE', message)]) {
        const shown = safeErrorText(error, 'Fallback sentence.');
        if (message !== '') expect(shown, message).not.toContain(message);
      }
    }
  });

  it('falls back for a non-error value', () => {
    expect(safeErrorText('a bare string', 'Failed to load projects.')).toBe('Failed to load projects.');
    expect(safeErrorText(undefined, 'Failed to load projects.')).toBe('Failed to load projects.');
    expect(safeErrorText({ message: 'nope' }, 'Failed to load projects.')).toBe('Failed to load projects.');
  });
});

describe('approved classifications keep their actionable meaning', () => {
  it('EXECUTION_SURFACE_DISABLED still tells the user why, and names itself', () => {
    const shown = safeErrorText(
      new ApiError(403, 'EXECUTION_SURFACE_DISABLED', 'conversation run creation is disabled'),
      'Run creation failed.',
    );
    expect(shown).toBe('This action is turned off at the current activation stage. (EXECUTION_SURFACE_DISABLED)');
  });

  it('JOB_LAUNCH_UNKNOWN says the run is parked and nothing retries it', () => {
    const shown = safeErrorText(
      new ApiError(502, 'JOB_LAUNCH_UNKNOWN', 'worker launch outcome is unknown; the run is parked'),
      'Run creation failed.',
    );
    // The meaning is preserved from OUR copy, not from the upstream sentence.
    expect(shown).toContain('parked for operator reconciliation');
    expect(shown).toContain('will not be relaunched automatically');
    expect(shown).toContain('(JOB_LAUNCH_UNKNOWN)');
    expect(shown).not.toContain('worker launch outcome is unknown; the run is parked');
  });

  it('JOB_LAUNCH_FAILED is distinguishable from JOB_LAUNCH_UNKNOWN', () => {
    const failed = safeErrorText(new ApiError(502, 'JOB_LAUNCH_FAILED', 'x'), 'Run creation failed.');
    expect(failed).toContain('still queued');
    expect(failed).toContain('can be retried');
    expect(failed).not.toContain('parked');
  });

  it('keeps the distinctions that matter without repeating upstream words', () => {
    const cases: [string, string][] = [
      ['IDEMPOTENCY_CONFLICT', 'already used with different content'],
      ['USER_CONCURRENCY_LIMIT', 'as many runs in flight as this stage allows'],
      ['DAILY_USER_BUDGET_REACHED', 'daily budget'],
      ['RATE_LIMITED', 'Too many requests'],
      ['RUN_ALREADY_FINISHED', 'already finished'],
      ['PROPOSAL_NOT_APPROVABLE', 'cannot be approved'],
      ['PROJECT_NOT_FOUND', 'not available to your account'],
      ['AUTHENTICATION_REQUIRED', 'Sign in again'],
    ];
    for (const [code, expected] of cases) {
      const shown = safeErrorText(new ApiError(400, code, 'upstream prose that must not appear'), 'It failed.');
      expect(shown, code).toContain(expected);
      expect(shown, code).toContain(`(${code})`);
      expect(shown, code).not.toContain('upstream prose');
    }
  });

  it('shows gateway HTTP classifications without inventing a support code', () => {
    // `HTTP_429` is how lib/api.ts labels a gateway body, not a code anyone can
    // look up, so the copy appears and the label does not.
    const shown = safeErrorText(new ApiError(429, 'HTTP_429', 'Too many requests.'), 'Request failed.');
    expect(shown).toBe('Too many requests. Wait a moment and try again.');
    expect(shown).not.toContain('HTTP_429');
  });

  it('classifies only codes on the list, by value', () => {
    expect(classifyError(new ApiError(403, 'EXECUTION_SURFACE_DISABLED', 'x'))).toBe('EXECUTION_SURFACE_DISABLED');
    expect(classifyError(new ApiError(500, 'REPOSITORY_ERROR', 'x'))).toBeUndefined();
    expect(classifyError(new ApiError(500, 'CATALOG_PROMOTION_UNVERIFIED', 'x'))).toBeUndefined();
    expect(classifyError(new Error('x'))).toBeUndefined();
    // Nothing on the list is a sentence, a URL or a credential.
    for (const code of CLASSIFIED_ERROR_CODES) {
      expect(code, code).toMatch(/^[A-Z][A-Z0-9_]{1,63}$/);
    }
  });
});

describe('authentication stays understandable without SDK prose', () => {
  it('names the failure in our own words', () => {
    expect(safeErrorText(new AuthFailure('invalid_credentials'), 'Authentication failed.'))
      .toBe('That email and password combination was not accepted.');
    expect(safeErrorText(new AuthFailure('rate_limited'), 'Authentication failed.'))
      .toContain('Too many sign-in attempts');
    expect(safeErrorText(new AuthFailure('expired'), 'Authentication failed.'))
      .toContain('session has expired');
    expect(safeErrorText(new AuthFailure('not_configured'), 'Authentication failed.'))
      .toContain('not configured');
    expect(safeErrorText(new AuthFailure('unavailable'), 'Authentication failed.'))
      .toContain('temporarily unavailable');
  });

  it('carries no Supabase message, because it never holds one', () => {
    // The classification is built from the SDK error's STATUS only
    // (lib/supabaseClient.ts). There is no field here to leak.
    const failure = new AuthFailure('invalid_credentials');
    expect(safeErrorText(failure, 'Authentication failed.')).not.toContain('Invalid login credentials');
    expect(safeErrorText(failure, 'Authentication failed.')).not.toContain('AuthApiError');
  });
});
