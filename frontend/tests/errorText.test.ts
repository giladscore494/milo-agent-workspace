import { describe, expect, it } from 'vitest';
import { ApiError } from '../lib/api';
import { MAX_ERROR_MESSAGE_LENGTH, classifyErrorCode, classifyErrorMessage, safeErrorText } from '../lib/errorText';
import { API_KEY_SENTINEL, BEARER_SENTINEL, JWT_SENTINEL, SUPABASE_SECRET_SENTINEL } from './secretSentinels';

describe('an error message is shown only if it survives classification', () => {
  it('keeps an ordinary, actionable backend message', () => {
    expect(classifyErrorMessage('run creation is disabled')).toBe('run creation is disabled');
    expect(classifyErrorMessage('Invalid login credentials')).toBe('Invalid login credentials');
    expect(classifyErrorMessage('Run creation is disabled by the gateway safety policy.'))
      .toBe('Run creation is disabled by the gateway safety policy.');
  });

  it('refuses a message carrying credential-shaped material outright', () => {
    // Partial masking is not good enough for an operational string: if a
    // credential was in there, the rest of it is not product copy either.
    for (const sentinel of [JWT_SENTINEL, SUPABASE_SECRET_SENTINEL, API_KEY_SENTINEL, BEARER_SENTINEL]) {
      expect(classifyErrorMessage(`upstream rejected: ${sentinel}`)).toBeUndefined();
    }
    expect(classifyErrorMessage('authorization=abc123def456')).toBeUndefined();
  });

  it('refuses an internal URL or hostname', () => {
    expect(classifyErrorMessage('failed to reach https://milo-api-internal.a.run.app/runs')).toBeUndefined();
    expect(classifyErrorMessage('postgres://db.internal:5432 refused the connection')).toBeUndefined();
  });

  it('refuses a stack trace from either runtime', () => {
    expect(classifyErrorMessage('Traceback (most recent call last): ValueError')).toBeUndefined();
    expect(classifyErrorMessage('TypeError: x is not a function\n    at handler (/srv/app.js:12:5)')).toBeUndefined();
    expect(classifyErrorMessage('  File "/app/backend/main.py", line 214, in create_run')).toBeUndefined();
  });

  it('refuses markup rather than relying on escaping to make it safe', () => {
    // `safeText` would render this inert. Inert is not the same as appropriate:
    // an error message is never markup, so it is not shown at all.
    expect(classifyErrorMessage('<img src=x onerror=alert(1)>')).toBeUndefined();
  });

  it('collapses control characters so one error cannot reflow the surface', () => {
    expect(classifyErrorMessage('line one\n\n\tline two')).toBe('line one line two');
  });

  it('bounds a very long message', () => {
    const long = 'x'.repeat(MAX_ERROR_MESSAGE_LENGTH + 200);
    const classified = classifyErrorMessage(long) ?? '';
    expect(classified).toHaveLength(MAX_ERROR_MESSAGE_LENGTH);
    expect(classified.endsWith('…')).toBe(true);
  });

  it('refuses an empty, whitespace-only or non-string message', () => {
    expect(classifyErrorMessage('')).toBeUndefined();
    expect(classifyErrorMessage('   \n  ')).toBeUndefined();
    expect(classifyErrorMessage(undefined)).toBeUndefined();
    expect(classifyErrorMessage({ message: 'nope' })).toBeUndefined();
  });
});

describe('the code is kept because it is what makes an error actionable', () => {
  it('accepts the SCREAMING_SNAKE shape every AppError code uses', () => {
    expect(classifyErrorCode('EXECUTION_SURFACE_DISABLED')).toBe('EXECUTION_SURFACE_DISABLED');
    expect(classifyErrorCode('JOB_LAUNCH_UNKNOWN')).toBe('JOB_LAUNCH_UNKNOWN');
    expect(classifyErrorCode('HTTP_403')).toBe('HTTP_403');
  });

  it('refuses anything that could carry a sentence, a URL or a secret', () => {
    expect(classifyErrorCode('a message pretending to be a code')).toBeUndefined();
    expect(classifyErrorCode('https://internal.example/x')).toBeUndefined();
    expect(classifyErrorCode(API_KEY_SENTINEL)).toBeUndefined();
    expect(classifyErrorCode('X'.repeat(200))).toBeUndefined();
    expect(classifyErrorCode(42)).toBeUndefined();
  });
});

describe('safeErrorText', () => {
  it('keeps the message and the code when both survive', () => {
    const error = new ApiError(403, 'EXECUTION_SURFACE_DISABLED', 'run creation is disabled');
    expect(safeErrorText(error, 'Run creation failed.'))
      .toBe('run creation is disabled (EXECUTION_SURFACE_DISABLED)');
  });

  it('falls back to the caller sentence but KEEPS a safe code', () => {
    // The action stays identifiable even when the upstream text does not.
    const error = new ApiError(502, 'JOB_LAUNCH_UNKNOWN', `worker at https://internal/x said ${API_KEY_SENTINEL}`);
    expect(safeErrorText(error, 'Run creation failed.'))
      .toBe('Run creation failed. (JOB_LAUNCH_UNKNOWN)');
  });

  it('drops an unsafe code as well as an unsafe message', () => {
    const error = new ApiError(500, 'internal detail: /srv/app.js', 'Traceback (most recent call last):');
    expect(safeErrorText(error, 'Run creation failed.')).toBe('Run creation failed.');
  });

  it('handles a plain Error and a non-error alike', () => {
    expect(safeErrorText(new Error('403 gateway rejected'), 'Failed to load projects.'))
      .toBe('403 gateway rejected');
    expect(safeErrorText('a bare string', 'Failed to load projects.')).toBe('Failed to load projects.');
    expect(safeErrorText(undefined, 'Failed to load projects.')).toBe('Failed to load projects.');
  });
});
