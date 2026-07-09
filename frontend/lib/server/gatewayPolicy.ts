const UUID =
  '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';

type GatewayRule = {
  method: 'GET' | 'POST';
  path: RegExp;
};

const SAFE_RULES: GatewayRule[] = [
  { method: 'GET', path: /^\/health$/ },
  { method: 'GET', path: /^\/projects$/ },
  { method: 'GET', path: new RegExp(`^/projects/${UUID}$`, 'i') },
  {
    method: 'POST',
    path: new RegExp(`^/projects/${UUID}/conversations$`, 'i'),
  },
  {
    method: 'GET',
    path: new RegExp(`^/conversations/${UUID}$`, 'i'),
  },
];

const RUN_CREATION_RULES = [
  new RegExp(`^/conversations/${UUID}/runs$`, 'i'),
  new RegExp(`^/workflow-proposals/${UUID}/runs$`, 'i'),
];

export function isGatewayRequestAllowed(method: string, path: string): boolean {
  const normalizedMethod = method.toUpperCase();

  return SAFE_RULES.some(
    (rule) => rule.method === normalizedMethod && rule.path.test(path),
  );
}

export function isRunCreationRequest(method: string, path: string): boolean {
  return (
    method.toUpperCase() === 'POST' &&
    RUN_CREATION_RULES.some((rule) => rule.test(path))
  );
}
