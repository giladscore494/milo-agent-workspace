const UUID =
  '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';

type GatewayRule = {
  method: 'GET' | 'POST';
  path: RegExp;
};

/**
 * The gateway allowlist is defense in depth, not the security boundary:
 * every proxied request still needs a valid Supabase token, and the backend
 * enforces membership authorization plus its own execution flags.
 *
 * Read routes are always proxied for authenticated users. Execution routes
 * (proposal mutations, run creation, cancellation) are additionally gated
 * by the server-side GATEWAY_ALLOW_EXECUTION_ROUTES flag, which stays OFF
 * by default so the deployed gateway keeps its read-only posture until an
 * operator deliberately enables the execution stage.
 */

const SAFE_RULES: GatewayRule[] = [
  { method: 'GET', path: /^\/health$/ },
  { method: 'GET', path: /^\/projects$/ },
  { method: 'GET', path: new RegExp(`^/projects/${UUID}$`, 'i') },
  { method: 'GET', path: new RegExp(`^/projects/${UUID}/conversations$`, 'i') },
  {
    method: 'POST',
    path: new RegExp(`^/projects/${UUID}/conversations$`, 'i'),
  },
  {
    method: 'GET',
    path: new RegExp(`^/conversations/${UUID}$`, 'i'),
  },
  { method: 'GET', path: new RegExp(`^/runs/${UUID}$`, 'i') },
  { method: 'GET', path: new RegExp(`^/runs/${UUID}/events$`, 'i') },
];

const EXECUTION_RULES: GatewayRule[] = [
  { method: 'POST', path: /^\/workflow-proposals$/ },
  { method: 'GET', path: new RegExp(`^/workflow-proposals/${UUID}$`, 'i') },
  {
    method: 'POST',
    path: new RegExp(`^/workflow-proposals/${UUID}/(approve|reject|revise)$`, 'i'),
  },
  { method: 'POST', path: new RegExp(`^/conversations/${UUID}/runs$`, 'i') },
  { method: 'POST', path: new RegExp(`^/workflow-proposals/${UUID}/runs$`, 'i') },
  { method: 'POST', path: new RegExp(`^/runs/${UUID}/cancel$`, 'i') },
];

const RUN_CREATION_RULES = [
  new RegExp(`^/conversations/${UUID}/runs$`, 'i'),
  new RegExp(`^/workflow-proposals/${UUID}/runs$`, 'i'),
];

export function executionRoutesEnabled(): boolean {
  return (process.env.GATEWAY_ALLOW_EXECUTION_ROUTES ?? '')
    .trim()
    .toLowerCase() === 'true';
}

function matches(rules: GatewayRule[], method: string, path: string): boolean {
  const normalizedMethod = method.toUpperCase();
  return rules.some(
    (rule) => rule.method === normalizedMethod && rule.path.test(path),
  );
}

export function isGatewayRequestAllowed(method: string, path: string): boolean {
  if (matches(SAFE_RULES, method, path)) return true;
  if (executionRoutesEnabled() && matches(EXECUTION_RULES, method, path)) {
    return true;
  }
  return false;
}

export function isRunCreationRequest(method: string, path: string): boolean {
  if (executionRoutesEnabled()) return false;
  return (
    method.toUpperCase() === 'POST' &&
    RUN_CREATION_RULES.some((rule) => rule.test(path))
  );
}
