type SupabaseAuthClient = {
  auth: {
    getSession: () => Promise<{ data?: { session?: SupabaseSession | null }; error?: { message: string } | null }>;
    onAuthStateChange: (
      callback: (event: string, session: SupabaseSession | null) => void,
    ) => { data?: { subscription?: { unsubscribe: () => void } } };
    signInWithPassword: (credentials: { email: string; password: string }) => Promise<{ data?: { session?: SupabaseSession | null }; error?: { message: string } | null }>;
    signOut: () => Promise<{ error?: { message: string } | null }>;
  };
};

export type SupabaseSession = {
  access_token: string;
  expires_at?: number;
  user?: { id?: string; email?: string };
};

let clientPromise: Promise<SupabaseAuthClient | undefined> | undefined;

export function setSupabaseClientForTests(client: SupabaseAuthClient | undefined): void {
  clientPromise = Promise.resolve(client);
}

export const supabaseConfig = {
  url: process.env.NEXT_PUBLIC_SUPABASE_URL,
  anonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
};

export async function getSupabaseBrowserClient(): Promise<SupabaseAuthClient | undefined> {
  if (clientPromise) return clientPromise;
  if (!supabaseConfig.url || !supabaseConfig.anonKey) return undefined;
  const loader = new Function('m', 'return import(m)');
  clientPromise = loader('@supabase/supabase-js')
    .catch(() => {
      if (typeof window === 'undefined') return undefined;
      return loader('https://esm.sh/@supabase/supabase-js@2');
    })
    .then((module: any) => module?.createClient?.(supabaseConfig.url, supabaseConfig.anonKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    }))
    .catch(() => undefined);
  return clientPromise;
}

export function isSessionExpired(session: SupabaseSession | null | undefined): boolean {
  if (!session?.access_token) return true;
  if (!session.expires_at) return false;
  return session.expires_at * 1000 <= Date.now() + 30_000;
}

export async function getCurrentSession(): Promise<SupabaseSession | null> {
  const client = await getSupabaseBrowserClient();
  if (!client) return null;
  const { data, error } = await client.auth.getSession();
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
  let subscription: { unsubscribe: () => void } | undefined;
  getSupabaseBrowserClient().then((client) => {
    subscription = client?.auth.onAuthStateChange((_event, session) => {
      callback(isSessionExpired(session) ? null : session);
    }).data?.subscription;
  }).catch(() => callback(null));
  return () => subscription?.unsubscribe();
}

export async function signInWithSupabase(email: string, password: string): Promise<SupabaseSession> {
  const client = await getSupabaseBrowserClient();
  if (!client) throw new Error('Supabase auth is not configured.');
  const { data, error } = await client.auth.signInWithPassword({ email, password });
  if (error) throw new Error(error.message);
  const session = data?.session ?? null;
  if (isSessionExpired(session) || !session) throw new Error('Supabase session is expired.');
  return session;
}

export async function signOutFromSupabase(): Promise<void> {
  const client = await getSupabaseBrowserClient();
  if (!client) return;
  const { error } = await client.auth.signOut();
  if (error) throw new Error(error.message);
}
