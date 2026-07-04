# Vercel frontend deployment

The production frontend is a Next.js application in `frontend/`. Keep Vercel configured as a normal Next.js project; do not configure it as a static `public` directory deployment and do not set a custom output directory.

## Required Vercel Dashboard settings

```text
Framework Preset: Next.js
Root Directory: frontend
Build Command: Default
Output Directory: Default / no override
Install Command: npm ci
Production Branch: main
Node.js Version: 20.x or 22.x
```

## Redeploy checklist

1. Save the Framework Preset as **Next.js**.
2. Redeploy the latest `main` commit.
3. Redeploy once without the previous build cache.
4. Verify the deployment-specific URL first.
5. Verify that the deployment status is `Ready`.
6. Verify that the production domain points to that successful production deployment.
7. Inspect build logs if the deployment is not `Ready`.
8. Never expose backend secrets in Vercel public environment variables.

## Environment variables

The browser bundle may use only public frontend variables, such as:

```text
NEXT_PUBLIC_API_URL
NEXT_PUBLIC_SUPABASE_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY
```

Do not add backend secrets to Vercel public environment variables. In particular, never expose `KIMI_API_KEY`, `SUPABASE_SECRET_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, Google service-account JSON, or `GOOGLE_APPLICATION_CREDENTIALS` to the frontend.

A missing `NEXT_PUBLIC_API_URL` can make API actions unavailable, but it must not prevent the `/` route from rendering or cause Vercel to serve `404: NOT_FOUND` for the frontend itself.
