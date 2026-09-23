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
 * (proposal mutations, Mapping Plan writes, cancellation) are additionally
 * gated by the server-side GATEWAY_ALLOW_EXECUTION_ROUTES flag, which stays
 * OFF by default so the deployed gateway keeps its read-only posture until an
 * operator deliberately enables the execution stage.
 *
 * STARTING a run is a separate, later permission. The three run-start routes
 * (a conversation run, a proposal run, a Mapping Plan batch) need
 * GATEWAY_ALLOW_RUN_START_ROUTES as well. Plan authoring (Stage P) opens the
 * execution routes so a person can write a plan; nothing can start until the
 * operator has armed and read back the worker and the API, passed the
 * pre-open gate, and only then opened this flag -- the LAST step of the
 * activation. Every intermediate state therefore refuses a start at the
 * gateway, whatever the backend flags say at that moment.
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
  /**
   * The Mapping Plan's progress: where its batches stand, derived by the
   * server from durable run state. A membership-gated READ, GET only; the
   * three batch WRITES (start, pause, resume) are execution rules below.
   */
  {
    method: 'GET',
    path: new RegExp(`^/work-scopes/${UUID}/progress$`, 'i'),
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
  // Starting ONE batch of a prepared plan creates a run (so it is also a
  // RUN_CREATION rule below), and pausing / resuming the plan holds or
  // releases its next batch. The backend gates all three again
  // (MILO_ENABLE_WORK_SCOPE_BATCHES, and MILO_ENABLE_RUN_CREATION for a start).
  { method: 'POST', path: new RegExp(`^/work-scopes/${UUID}/runs$`, 'i') },
  { method: 'POST', path: new RegExp(`^/work-scopes/${UUID}/(pause|resume)$`, 'i') },
];

const RUN_CREATION_RULES = [
  new RegExp(`^/conversations/${UUID}/runs$`, 'i'),
  new RegExp(`^/workflow-proposals/${UUID}/runs$`, 'i'),
  new RegExp(`^/work-scopes/${UUID}/runs$`, 'i'),
];

export function executionRoutesEnabled(): boolean {
  return (process.env.GATEWAY_ALLOW_EXECUTION_ROUTES ?? '')
    .trim()
    .toLowerCase() === 'true';
}

/**
 * May the gateway proxy a request that STARTS a run? Only when the execution
 * routes are open AND the separate run-start flag is on: a run start is an
 * execution route, and never the only one open.
 */
export function runStartRoutesEnabled(): boolean {
  return executionRoutesEnabled()
    && (process.env.GATEWAY_ALLOW_RUN_START_ROUTES ?? '').trim().toLowerCase() === 'true';
}

function isRunStartPath(method: string, path: string): boolean {
  return method.toUpperCase() === 'POST' && RUN_CREATION_RULES.some((rule) => rule.test(path));
}

function matches(rules: GatewayRule[], method: string, path: string): boolean {
  const normalizedMethod = method.toUpperCase();
  return rules.some(
    (rule) => rule.method === normalizedMethod && rule.path.test(path),
  );
}

export function isGatewayRequestAllowed(method: string, path: string): boolean {
  if (matches(SAFE_RULES, method, path)) return true;
  if (isRunStartPath(method, path)) return runStartRoutesEnabled();
  if (executionRoutesEnabled() && matches(EXECUTION_RULES, method, path)) {
    return true;
  }
  return false;
}

/** A run start the gateway refuses right now (403, before authentication). */
export function isRunCreationRequest(method: string, path: string): boolean {
  return isRunStartPath(method, path) && !runStartRoutesEnabled();
}

/** Representative ids for {@link gatewayPosture}; nil UUIDs match no row. */
const NIL = '00000000-0000-4000-8000-000000000000';

/**
 * What THIS gateway would do, computed by the policy functions above on one
 * representative path of each kind -- the policy's behaviour, not a raw
 * environment read. `/api/deployment-status` reports it for the operator's
 * read-only website check.
 */
export function gatewayPosture(): { executionRoutes: boolean; runStartRoutes: boolean } {
  const planWrite = `/conversations/${NIL}/work-scopes`;
  const runStarts = [`/conversations/${NIL}/runs`, `/workflow-proposals/${NIL}/runs`, `/work-scopes/${NIL}/runs`];
  return {
    executionRoutes: isGatewayRequestAllowed('POST', planWrite),
    runStartRoutes: runStarts.every((path) =>
      isGatewayRequestAllowed('POST', path) && !isRunCreationRequest('POST', path)),
  };
}
