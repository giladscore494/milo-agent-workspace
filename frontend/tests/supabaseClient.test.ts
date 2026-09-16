import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  getCurrentAccessToken,
  getCurrentSession,
  isSessionExpired,
  onAuthStateChange,
  setSupabaseClientForTests,
  signInWithSupabase,
  signOutFromSupabase,
} from '../lib/supabaseClient';
import { AuthFailure } from '../lib/errorText';

function client(session: any) {
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session }, error: null }),
      onAuthStateChange: vi.fn((cb) => {
        cb('SIGNED_IN', session);
        return { data: { subscription: { unsubscribe: vi.fn() } } };
      }),
      signInWithPassword: vi.fn().mockResolvedValue({ data: { session }, error: null }),
      signOut: vi.fn().mockResolvedValue({ error: null }),
    },
  };
}

describe('Supabase browser auth client helpers', () => {
  afterEach(() => setSupabaseClientForTests(undefined));

  it('restores a current session and access token through auth.getSession', async () => {
    const session = { access_token: 'fresh', expires_at: Math.floor(Date.now() / 1000) + 3600, user: { email: 'u@example.com' } };
    const mock = client(session);
    setSupabaseClientForTests(mock as any);
    await expect(getCurrentSession()).resolves.toEqual(session);
    await expect(getCurrentAccessToken()).resolves.toBe('fresh');
    expect(mock.auth.getSession).toHaveBeenCalled();
  });

  it('treats expired sessions as unauthenticated', async () => {
    const expired = { access_token: 'old', expires_at: Math.floor(Date.now() / 1000) - 1 } as any;
    setSupabaseClientForTests(client(expired) as any);
    expect(isSessionExpired(expired)).toBe(true);
    await expect(getCurrentAccessToken()).resolves.toBeUndefined();
  });

  it('signs in and subscribes to auth state changes', async () => {
    const session = { access_token: 'fresh', expires_at: Math.floor(Date.now() / 1000) + 3600 };
    const mock = client(session);
    setSupabaseClientForTests(mock as any);
    const callback = vi.fn();
    onAuthStateChange(callback);
    await expect(signInWithSupabase('u@example.com', 'pw')).resolves.toEqual(session);
    expect(mock.auth.signInWithPassword).toHaveBeenCalledWith({ email: 'u@example.com', password: 'pw' });
    expect(callback).toHaveBeenCalledWith(session);
  });



  it('raises a classification for a session-restoration failure, never the SDK message', async () => {
    const broken = {
      auth: {
        getSession: vi.fn().mockResolvedValue({ data: {}, error: { message: 'invalid JSON in stored session', status: 500 } }),
        onAuthStateChange: vi.fn(),
        signInWithPassword: vi.fn(),
        signOut: vi.fn(),
      },
    };
    setSupabaseClientForTests(broken as any);
    // The SDK's own sentence is upstream prose about an upstream system. Only
    // the status is read, and only to choose our own copy.
    const error = await getCurrentSession().then(() => undefined, (e) => e);
    expect(error).toBeInstanceOf(AuthFailure);
    expect(error.reason).toBe('unavailable');
    expect(JSON.stringify(error.message)).not.toContain('invalid JSON in stored session');
  });

  it('maps a rejected sign-in to invalid_credentials without the SDK message', async () => {
    const rejecting = {
      auth: {
        getSession: vi.fn(),
        onAuthStateChange: vi.fn(),
        signInWithPassword: vi.fn().mockResolvedValue({ data: {}, error: { message: 'Invalid login credentials', status: 400 } }),
        signOut: vi.fn(),
      },
    };
    setSupabaseClientForTests(rejecting as any);
    const error = await signInWithSupabase('a@example.com', 'wrong').then(() => undefined, (e) => e);
    expect(error).toBeInstanceOf(AuthFailure);
    expect(error.reason).toBe('invalid_credentials');
    expect(error.message).not.toContain('Invalid login credentials');
  });

  it('maps a throttled sign-in to rate_limited', async () => {
    const throttled = {
      auth: {
        getSession: vi.fn(),
        onAuthStateChange: vi.fn(),
        signInWithPassword: vi.fn().mockResolvedValue({ data: {}, error: { message: 'over_request_rate_limit', status: 429 } }),
        signOut: vi.fn(),
      },
    };
    setSupabaseClientForTests(throttled as any);
    const error = await signInWithSupabase('a@example.com', 'x').then(() => undefined, (e) => e);
    expect(error).toBeInstanceOf(AuthFailure);
    expect(error.reason).toBe('rate_limited');
  });

  it('performs real Supabase sign-out through the auth client', async () => {
    const mock = client(null);
    setSupabaseClientForTests(mock as any);
    await signOutFromSupabase();
    expect(mock.auth.signOut).toHaveBeenCalled();
  });
});
