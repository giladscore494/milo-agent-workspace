import { gatewayPosture } from '@/lib/server/gatewayPolicy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * What THIS deployment is serving, for the operator's read-only website check
 * (`scripts/deploy/website-execution-check.sh`). Three facts and nothing else:
 *
 * - `execution_ui`: NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI as it was inlined at
 *   BUILD time. It is referenced literally so Next.js inlines it here exactly
 *   as it does in the browser bundle: a Vercel variable changed without a
 *   rebuild does not move this answer, just as it does not move the composer.
 * - `gateway_execution_routes`: whether the running gateway proxies the
 *   execution routes (a Mapping Plan write), computed by the policy itself.
 * - `gateway_run_start_routes`: whether it proxies a run START (conversation
 *   run, proposal run, Mapping Plan batch) -- the separate, last-opened
 *   permission (GATEWAY_ALLOW_RUN_START_ROUTES), computed the same way.
 * - `commit_sha`: the Git commit Vercel built, when Vercel states one.
 *
 * Three booleans and a commit id: no identity, no project, no URL and no secret.
 * It is a read; it proxies nothing and reaches no backend. The check never
 * trusts it alone -- the gateway's observable behaviour is probed as well.
 */
export function GET(): Response {
  const executionUi = (process.env.NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI ?? '')
    .trim()
    .toLowerCase() === 'true';
  const sha = (process.env.VERCEL_GIT_COMMIT_SHA ?? '').trim().toLowerCase();
  const posture = gatewayPosture();
  return Response.json(
    {
      contract: 'milo-website-deployment/1',
      execution_ui: executionUi,
      gateway_execution_routes: posture.executionRoutes,
      gateway_run_start_routes: posture.runStartRoutes,
      commit_sha: /^[0-9a-f]{40}$/.test(sha) ? sha : null,
    },
    { headers: { 'cache-control': 'no-store' } },
  );
}
