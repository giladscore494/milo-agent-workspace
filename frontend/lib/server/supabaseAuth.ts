export type AuthenticatedSupabaseUser = {
  id: string;
  email?: string;
};

export class GatewayAuthError extends Error {
  status = 401;
}

export async function validateSupabaseAccessToken(
  authorization: string | null,
): Promise<AuthenticatedSupabaseUser> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  const match = authorization?.match(/^Bearer\s+(.+)$/i);

  if (!supabaseUrl || !anonKey) {
    throw new GatewayAuthError('Supabase auth is not configured.');
  }

  if (!match?.[1]) {
    throw new GatewayAuthError('Authentication required.');
  }

  let response: Response;
  try {
    response = await fetch(`${supabaseUrl.replace(/\/$/, '')}/auth/v1/user`, {
      headers: {
        apikey: anonKey,
        authorization: `Bearer ${match[1]}`,
      },
      cache: 'no-store',
    });
  } catch (error) {
    throw new GatewayAuthError('Unable to validate Supabase token.');
  }

  if (!response.ok) {
    throw new GatewayAuthError('Invalid or expired Supabase token.');
  }

  const user = await response.json();
  if (!user?.id || typeof user.id !== 'string') {
    throw new GatewayAuthError('Invalid Supabase user response.');
  }

  return { id: user.id, email: typeof user.email === 'string' ? user.email : undefined };
}
