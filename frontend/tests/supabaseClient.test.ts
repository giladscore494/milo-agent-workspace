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
    const expired = { access_token: 'old', expires_at: Math.floor(Date.now() / 1000) - 1 };
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



  it('surfaces Supabase session restoration errors safely', async () => {
    const broken = {
      auth: {
        getSession: vi.fn().mockResolvedValue({ data: {}, error: { message: 'invalid JSON in stored session' } }),
        onAuthStateChange: vi.fn(),
        signInWithPassword: vi.fn(),
        signOut: vi.fn(),
      },
    };
    setSupabaseClientForTests(broken as any);
    await expect(getCurrentSession()).rejects.toThrow('invalid JSON in stored session');
  });

  it('performs real Supabase sign-out through the auth client', async () => {
    const mock = client(null);
    setSupabaseClientForTests(mock as any);
    await signOutFromSupabase();
    expect(mock.auth.signOut).toHaveBeenCalled();
  });
});
