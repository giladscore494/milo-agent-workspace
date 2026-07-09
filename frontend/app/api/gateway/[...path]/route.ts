import { NextRequest } from 'next/server';

import {
  getCloudRunIdToken,
  getCloudRunServiceUrl,
} from '@/lib/server/cloudRunAuth';
import {
  isGatewayRequestAllowed,
  isRunCreationRequest,
} from '@/lib/server/gatewayPolicy';

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

  if (!isGatewayRequestAllowed(method, backendPath)) {
    return Response.json(
      {
        error: 'This API route is not allowed by the gateway policy.',
      },
      { status: 403 },
    );
  }

  try {
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
