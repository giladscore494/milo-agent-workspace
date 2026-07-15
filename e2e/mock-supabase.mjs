// Test-only mock of the two Supabase Auth endpoints the app uses:
//   POST /auth/v1/token?grant_type=password|refresh_token
//   GET  /auth/v1/user
// Never deployed; wildcard CORS is acceptable only because this process
// exists solely inside the isolated E2E stack on localhost.
import { createServer } from 'node:http';

const PORT = Number(process.env.MOCK_SUPABASE_PORT || 9998);

const USERS = {
  'alice@example.com': {
    password: 'alice-Password-1',
    id: 'aaaaaaaa-1111-4111-8111-000000000001',
  },
  'bob@example.com': {
    password: 'bob-Password-1',
    id: 'aaaaaaaa-1111-4111-8111-000000000002',
  },
  'mallory@example.com': {
    password: 'mallory-Password-1',
    id: 'aaaaaaaa-1111-4111-8111-000000000003',
  },
};

const tokens = new Map(); // access_token -> user
const refreshTokens = new Map(); // refresh_token -> email

function issueSession(email) {
  const user = USERS[email];
  const accessToken = `e2e-access-${email}-${Math.random().toString(36).slice(2)}`;
  const refreshToken = `e2e-refresh-${Math.random().toString(36).slice(2)}`;
  tokens.set(accessToken, { id: user.id, email });
  refreshTokens.set(refreshToken, email);
  return {
    access_token: accessToken,
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: refreshToken,
    user: { id: user.id, aud: 'authenticated', role: 'authenticated', email },
  };
}

function send(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json',
    'access-control-allow-origin': '*',
    'access-control-allow-headers': '*',
    'access-control-allow-methods': 'GET,POST,OPTIONS',
  });
  res.end(JSON.stringify(body));
}

createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  if (req.method === 'OPTIONS') return send(res, 204, {});

  if (req.method === 'POST' && url.pathname === '/auth/v1/token') {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    let body = {};
    try {
      body = JSON.parse(raw || '{}');
    } catch {
      return send(res, 400, { error: 'invalid_request' });
    }
    const grant = url.searchParams.get('grant_type');
    if (grant === 'password') {
      const user = USERS[body.email];
      if (!user || user.password !== body.password) {
        return send(res, 400, {
          error: 'invalid_grant',
          error_description: 'Invalid login credentials',
        });
      }
      return send(res, 200, issueSession(body.email));
    }
    if (grant === 'refresh_token') {
      const email = refreshTokens.get(body.refresh_token);
      if (!email) return send(res, 400, { error: 'invalid_grant' });
      return send(res, 200, issueSession(email));
    }
    return send(res, 400, { error: 'unsupported_grant_type' });
  }

  if (req.method === 'GET' && url.pathname === '/auth/v1/user') {
    const auth = req.headers.authorization || '';
    const token = auth.replace(/^Bearer\s+/i, '');
    const user = tokens.get(token);
    if (!user) return send(res, 401, { message: 'invalid token' });
    return send(res, 200, { id: user.id, aud: 'authenticated', email: user.email });
  }

  if (req.method === 'POST' && url.pathname === '/auth/v1/logout') {
    return send(res, 204, {});
  }

  return send(res, 404, { message: 'not found' });
}).listen(PORT, () => {
  console.log(`mock-supabase listening on :${PORT}`);
});
