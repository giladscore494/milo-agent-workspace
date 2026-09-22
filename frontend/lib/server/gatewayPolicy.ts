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
  /**
   * The canonical export of ONE finished run (`build_export_envelope` on the
   * server). A membership-gated READ of the same run the two rules above
   * read; GET only, and the backend refuses a live or unexportable run.
   */
  { method: 'GET', path: new RegExp(`^/runs/${UUID}/export$`, 'i') },
  /**
   * The conversation's durable run history. A bounded, membership-gated READ
   * (GET only; the POST to the same path is the run-creation EXECUTION rule
   * below and stays behind GATEWAY_ALLOW_EXECUTION_ROUTES). It is what lets a
   * completed result be reopened after session storage is gone.
   */
  { method: 'GET', path: new RegExp(`^/conversations/${UUID}/runs$`, 'i') },

  /**
   * CODE-3 — the read-only catalog review surface.
   *
   * SAFE rather than EXECUTION, deliberately. These are durable reads: they
   * write nothing, launch nothing and need no flag, and the backend gates them
   * on project membership exactly like every other read here. Putting them
   * behind `GATEWAY_ALLOW_EXECUTION_ROUTES` would hide the catalog whenever
   * the execution stage is off — which is precisely when an operator rolling
   * back most needs to see what the catalog holds.
   *
   * Two exact paths, not a `/catalog/*` prefix: a prefix rule would proxy any
   * catalog route a later release adds, including a mutating one, without
   * anyone revisiting this list. `GatewayRule.method` admits only GET and POST,
   * and only GET is named here, so PUT/PATCH/DELETE cannot match either — nor
   * can a POST to the same path.
   */
  {
    method: 'GET',
    path: new RegExp(`^/projects/${UUID}/catalog/canonical$`, 'i'),
  },
  {
    method: 'GET',
    path: new RegExp(`^/projects/${UUID}/catalog/review-candidates$`, 'i'),
  },

  /**
   * The Mapping Plan's three READS: whether the surface applies to the
   * project, the reviewed manufacturer directory with its coverage, and the
   * conversation's open plan. SAFE for the reason the CODE-3 reads are: they
   * write nothing and launch nothing, and the backend gates each on
   * membership. Three exact paths, GET only; the two WRITES are execution
   * rules below.
   */
  {
    method: 'GET',
    path: new RegExp(`^/projects/${UUID}/work-scope/capabilities$`, 'i'),
  },
  {
    method: 'GET',
    path: new RegExp(`^/projects/${UUID}/work-scope/directory$`, 'i'),
  },
  {
    method: 'GET',
    path: new RegExp(`^/conversations/${UUID}/work-scopes/open$`, 'i'),
  },
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
  // The Mapping Plan writes: create a conversation's plan, and revise it. A
  // plan is a draft that executes nothing, but it is durable state a browser
  // writes, so it waits for the execution stage like every other write here
  // (and the backend's MILO_ENABLE_WORK_SCOPE_MUTATIONS gates it again).
  { method: 'POST', path: new RegExp(`^/conversations/${UUID}/work-scopes$`, 'i') },
  { method: 'POST', path: new RegExp(`^/work-scopes/${UUID}/revisions$`, 'i') },
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
