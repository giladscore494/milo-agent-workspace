/**
 * PR-D3 — manufacturer normalisation on the Register page, validated before
 * anything renders (`backend/catalog/register/service.py`
 * `normalization_view`). A body that does not match is `undefined`.
 *
 * Source names (tozar) are kept EXACTLY as sent: an approval sends each
 * pending group back verbatim, and the server accepts it only when it is
 * exactly a group it proposed.
 */

export type NormalisationGroup = {
  canonical: string;
  members: string[];
  confidence: 'high' | 'low';
  reason: string;
  ruleId?: string;
  proposalId?: string;
  conflicting: boolean;
  bulkApprovable: boolean;
};

export type NormalisationView = {
  version: number;
  unmapped: number;
  canonical: { canonical: string; sources: string[] }[];
  pending: NormalisationGroup[];
  proposal?: { id: string; status: 'requested' | 'proposed' | 'refused'; reasonCode?: string };
  canNormalise: boolean;
  canApprove: boolean;
};

const CODE = /^[A-Z][A-Z0-9_]{2,79}$/;
const ID = /^[0-9a-f-]{36}$/;
const RULES: ReadonlySet<string> = new Set(['R1_SPELLING', 'R2_TOZERET_CD']);

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function names(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.length > 0
    && value.every((item) => typeof item === 'string' && item.length >= 1 && item.length <= 200)
    ? (value as string[]) : undefined;
}

function text(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length >= 1 && value.length <= max;
}

function group(value: unknown): NormalisationGroup | undefined {
  const raw = asObject(value);
  const members = names(raw.members);
  if (!text(raw.canonical, 120) || !members || (raw.confidence !== 'high' && raw.confidence !== 'low')
      || typeof raw.reason !== 'string' || raw.reason.length > 300
      || typeof raw.conflicting !== 'boolean' || typeof raw.bulk_approvable !== 'boolean') {
    return undefined;
  }
  const out: NormalisationGroup = { canonical: raw.canonical, members, confidence: raw.confidence,
    reason: raw.reason, conflicting: raw.conflicting, bulkApprovable: raw.bulk_approvable };
  if (typeof raw.rule_id === 'string' && RULES.has(raw.rule_id)) out.ruleId = raw.rule_id;
  if (typeof raw.proposal_id === 'string' && ID.test(raw.proposal_id)) out.proposalId = raw.proposal_id;
  // Exactly one provenance: a code-owned rule, or the model proposal.
  return (out.ruleId === undefined) !== (out.proposalId === undefined) ? out : undefined;
}

export function parseNormalisation(body: unknown): NormalisationView | undefined {
  const source = asObject(body);
  const version = source.version;
  const unmapped = source.unmapped;
  if (typeof version !== 'number' || !Number.isSafeInteger(version) || version < 0
      || typeof unmapped !== 'number' || !Number.isSafeInteger(unmapped) || unmapped < 0
      || !Array.isArray(source.canonical) || !Array.isArray(source.pending)
      || typeof source.can_normalise !== 'boolean' || typeof source.can_approve !== 'boolean') {
    return undefined;
  }
  const canonical = source.canonical.map((item) => {
    const raw = asObject(item);
    const sources = names(raw.sources);
    return text(raw.canonical, 120) && sources ? { canonical: raw.canonical, sources } : undefined;
  });
  const pending = source.pending.map(group);
  if (canonical.some((item) => item === undefined) || pending.some((item) => item === undefined)) return undefined;
  let proposal: NormalisationView['proposal'];
  if (source.proposal !== null && source.proposal !== undefined) {
    const raw = asObject(source.proposal);
    if (typeof raw.id !== 'string' || !ID.test(raw.id)
        || (raw.status !== 'requested' && raw.status !== 'proposed' && raw.status !== 'refused')) return undefined;
    proposal = { id: raw.id, status: raw.status };
    if (typeof raw.reason_code === 'string' && CODE.test(raw.reason_code)) proposal.reasonCode = raw.reason_code;
  }
  return { version, unmapped, canonical: canonical as NormalisationView['canonical'],
    pending: pending as NormalisationGroup[], proposal, canNormalise: source.can_normalise,
    canApprove: source.can_approve };
}

/** The approval body: each group exactly as the server proposed it. */
export function approvalGroups(groups: readonly NormalisationGroup[]) {
  return groups.map((item) => ({
    canonical: item.canonical, members: item.members,
    ...(item.ruleId ? { rule_id: item.ruleId } : { proposal_id: item.proposalId }),
  }));
}
