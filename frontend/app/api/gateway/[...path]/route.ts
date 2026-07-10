import { NextRequest } from 'next/server';

import {
  getCloudRunIdToken,
  getCloudRunServiceUrl,
} from '@/lib/server/cloudRunAuth';
import {
  isGatewayRequestAllowed,
  isRunCreationRequest,
} from '@/lib/server/gatewayPolicy';
import { checkGatewayRateLimit } from '@/lib/server/rateLimit';
import { GatewayAuthError, validateSupabaseAccessToken } from '@/lib/server/supabaseAuth';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type RouteContext = {
  params: Promise<{
    path: string[];
  }>;
};

async function proxyRequest(
  request: NextRequest,
  context: RouteContext,
): Promise<Response> {
  const { path: pathSegments } = await context.params;
  const backendPath = `/${pathSegments.map(encodeURIComponent).join('/')}`;
  const method = request.method.toUpperCase();

  if (isRunCreationRequest(method, backendPath)) {
    return Response.json(
      {
        error: 'Run creation is disabled by the gateway safety policy.',
      },
      { status: 403 },
    );
  }

  if (!checkGatewayRateLimit(request.headers.get('x-forwarded-for') ?? 'anonymous')) {
    return Response.json({ error: 'Too many requests.' }, { status: 429 });
  }

  if (!isGatewayRequestAllowed(method, backendPath)) {
    return Response.json(
      {
        error: 'This API route is not allowed by the gateway policy.',
      },
      { status: 403 },
    );
  }

  try {
    const user = backendPath === '/health'
      ? undefined
      : await validateSupabaseAccessToken(request.headers.get('authorization'));
    const serviceUrl = getCloudRunServiceUrl();
    const idToken = await getCloudRunIdToken();
    const targetUrl = new URL(backendPath, `${serviceUrl}/`);

    request.nextUrl.searchParams.forEach((value, key) => {
      targetUrl.searchParams.append(key, value);
    });

    const headers = new Headers({
      authorization: `Bearer ${idToken}`,
      accept: request.headers.get('accept') ?? 'application/json',
    });

    if (user) {
      headers.set('x-milo-auth-user-id', user.id);
      if (user.email) headers.set('x-milo-auth-user-email', user.email);
    }

    let body: string | undefined;

    if (method !== 'GET' && method !== 'HEAD') {
      body = await request.text();

      if (body) {
        headers.set(
          'content-type',
          request.headers.get('content-type') ?? 'application/json',
        );
      }
    }

    const upstreamResponse = await fetch(targetUrl, {
      method,
      headers,
      body,
      cache: 'no-store',
      redirect: 'manual',
    });

    const responseHeaders = new Headers();
    const contentType = upstreamResponse.headers.get('content-type');

    if (contentType) {
      responseHeaders.set('content-type', contentType);
    }

    responseHeaders.set('cache-control', 'no-store');

    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders,
    });
  } catch (error) {
    if (error instanceof GatewayAuthError) {
      return Response.json({ error: error.message }, { status: error.status });
    }

    console.error('Private API gateway request failed', error);

    return Response.json(
      {
        error: 'Private API gateway request failed.',
      },
      { status: 502 },
    );
  }
}

export async function GET(
  request: NextRequest,
  context: RouteContext,
): Promise<Response> {
  return proxyRequest(request, context);
}

export async function POST(
  request: NextRequest,
  context: RouteContext,
): Promise<Response> {
  return proxyRequest(request, context);
}
