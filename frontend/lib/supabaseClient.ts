import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js';

export type SupabaseSession = Session;

let client: SupabaseClient | undefined;
let testClient: SupabaseClient | undefined;

export function setSupabaseClientForTests(nextClient: SupabaseClient | undefined): void {
  testClient = nextClient;
  client = undefined;
}

export const supabaseConfig = {
  url: process.env.NEXT_PUBLIC_SUPABASE_URL,
  anonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
};

export function getSupabaseBrowserClient(): SupabaseClient | undefined {
  if (testClient) return testClient;
  if (client) return client;
  if (!supabaseConfig.url || !supabaseConfig.anonKey) return undefined;
  client = createClient(supabaseConfig.url, supabaseConfig.anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });
  return client;
}

export function isSessionExpired(session: SupabaseSession | null | undefined): boolean {
  if (!session?.access_token) return true;
  if (!session.expires_at) return false;
  return session.expires_at * 1000 <= Date.now() + 30_000;
}

export async function getCurrentSession(): Promise<SupabaseSession | null> {
  const supabase = getSupabaseBrowserClient();
  if (!supabase) return null;
  const { data, error } = await supabase.auth.getSession();
  if (error) throw new Error(error.message);
  const session = data?.session ?? null;
  return isSessionExpired(session) ? null : session;
}

export async function getCurrentAccessToken(): Promise<string | undefined> {
  return (await getCurrentSession())?.access_token;
}

export function onAuthStateChange(
  callback: (session: SupabaseSession | null) => void,
): () => void {
  try {
    const subscription = getSupabaseBrowserClient()?.auth.onAuthStateChange((_event, session) => {
      callback(isSessionExpired(session) ? null : session);
    }).data?.subscription;
    return () => subscription?.unsubscribe();
  } catch {
    callback(null);
    return () => undefined;
  }
}

export async function signInWithSupabase(email: string, password: string): Promise<SupabaseSession> {
  const supabase = getSupabaseBrowserClient();
  if (!supabase) throw new Error('Supabase auth is not configured.');
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw new Error(error.message);
  const session = data?.session ?? null;
  if (isSessionExpired(session) || !session) throw new Error('Supabase session is expired.');
  return session;
}

export async function signOutFromSupabase(): Promise<void> {
  const supabase = getSupabaseBrowserClient();
  if (!supabase) return;
  const { error } = await supabase.auth.signOut();
  if (error) throw new Error(error.message);
}
