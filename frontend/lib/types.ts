import { EventId } from './eventId';
import { RunUsage } from './runUsage';
import { SwarmRunState } from './swarmTypes';

export type UUID = string;
export type InternetPolicy = 'forbidden'|'allowed'|'required'|'conditional'|'requested'|'approved'|'denied'|'active';
export type Project = { id: UUID; slug: string; name: string; description?: string; workflow_key: string };
export type Conversation = { id: UUID; project_id: UUID; title?: string };
export type LaunchState = 'pending'|'launching'|'launched'|'launch_failed'|'launch_unknown';
// `usage` mirrors BudgetTracker.snapshot() and is the authoritative aggregate.
// GET /runs/{id} returns it as a typed RunUsage object, or null when the run
// has settled no model call yet; see lib/runUsage.ts.
/**
 * The run's own immutable identity, as the server states it.
 *
 * It exists so a HISTORICAL run renders as the engine it actually was. The V2
 * vs V1 presentation used to be chosen from `project.workflow_key`, which is
 * what the project is TODAY — so a project switched from one engine to the
 * other re-rendered every earlier run of it as the wrong engine.
 *
 * `null` means the run was created before identities existed. That is not a
 * licence to guess: `lib/runIdentity.ts` returns `undefined` for it and the
 * caller falls back to the project, which is the behaviour such a run has
 * always had.
 */
export type RunIdentity = {
  identity_version: string;
  run_id: UUID;
  workflow_key: string;
  engine_version: string;
  policy_version: string;
  policy_fingerprint: string;
  release_sha: string;
  event_registry_version: string;
  event_registry_fingerprint: string;
};
/**
 * The canonical ProductOutcome as the API projects it from the terminal event
 * the finalizer wrote; `null` when none is recorded. Parsed, never trusted:
 * see lib/productOutcome.ts.
 */
export type ProductOutcomeRecord = Record<string, unknown>;
/** The per-run ceilings this deployment enforces; `null` when none is stated. */
export type RunLimitsRecord = Record<string, unknown>;
export type Run = { id: UUID; conversation_id: UUID; status: string; run_identity?: RunIdentity | null; started_at?: string; finished_at?: string; created_at?: string; output?: Record<string, unknown>; error?: Record<string, unknown>; launch_state?: LaunchState; launch_error_class?: string; launch_reconciliation_required?: boolean; usage?: RunUsage | null; product_outcome?: ProductOutcomeRecord | null; limits?: RunLimitsRecord | null };
/** One row of GET /conversations/{id}/runs: the run projection without payloads. */
export type RunSummary = { id: UUID; conversation_id: UUID; status: string; run_identity?: RunIdentity | null; started_at?: string; finished_at?: string; created_at?: string; launch_state?: LaunchState; usage?: RunUsage | null; product_outcome?: ProductOutcomeRecord | null };
// run_events.id is production bigint (not UUID); run_id remains UUID. It is
// carried as a canonical decimal string (lib/eventId.ts) so identity and
// ordering survive values above Number.MAX_SAFE_INTEGER.
export type RunEvent = { id: EventId; run_id: UUID; event_type: string; message?: string; payload?: any; agent?: string; phase?: string; progress?: Record<string, any>; created_at?: string };
export type AgentState = { name: string; responsibility: string; status: string; progress: number; currentTask?: string; internet: InternetPolicy; internetReason?: string; domains?: string[]; quota?: string; searchesUsed: number; sources: SourceRecord[]; startedAt?: string; finishedAt?: string; tokens: number; cost: number; retries: number; fallbacks: string[] };
export type SourceRecord = { id: string; title: string; domain: string; url?: string; source_type: string; source_strength: string; source_date?: string; retrieved_at?: string; claims?: string[]; agent?: string; query?: string; tool_operation?: string };
export type ConflictRecord = { id: string; entity_key: string; field_key: string; outcome: string; rationale?: string };
export type WorkspaceState = { run?: Run; events: RunEvent[]; lastEventId?: EventId; agents: Record<string, AgentState>; sources: SourceRecord[]; claims: any[]; conflicts: ConflictRecord[]; currentPhase: string; progress: number; tokens: number; cost: number; supervisor: string[]; validationErrors: any[]; checkpoints: any[]; rawErrors: any[]; swarm: SwarmRunState };
export type Proposal = { id: UUID; status: string; user_request: string; draft: any; task_spec: any; estimates: any; critiques: any[] };

// ---------------------------------------------------------------------------
// CODE-3 — the read-only catalog review contracts.
// ---------------------------------------------------------------------------
//
// The shapes the backend's `CatalogCanonicalPage` / `CatalogReviewPage`
// response models produce. They are what the server is EXPECTED to send, never
// what the UI trusts: `lib/catalogReview.ts` validates a response into UI state
// field by field, so a key outside these types can never be rendered.
//
// This is a different question from CODE-2's `lib/catalogStatus.ts`, and the
// two are deliberately not merged. `catalogStatus` answers "what did THIS RUN
// do to the catalog?" from that run's events. These answer "what durable
// catalog state EXISTS now?" from durable rows. A run that promoted nothing and
// a catalog that holds nothing are not the same fact.

/** Identity dimensions a row STATES. An unstated one is an absent key. */
export type CatalogIdentityDimensions = {
  body_style?: string;
  drivetrain?: string;
  engine_code?: string;
  fuel_type?: string;
  generation?: string;
  market?: string;
  propulsion_technology?: string;
  transmission?: string;
};

/**
 * Page metadata, as the server states it.
 *
 * `total` and `hasMore` are `null` for "the server did not state one". They are
 * never derived from the item count here, and `null` is never rendered as `0`
 * or `false`: an unknown total shown as zero is the claim that the catalog is
 * empty.
 */
export type CatalogPageMeta = {
  limit: number;
  offset: number;
  total: number | null;
  hasMore: boolean | null;
};

export type CanonicalCatalogItem = {
  canonicalKey: string;
  modelCanonicalKey?: string;
  manufacturer?: string;
  commercialModel?: string;
  modelYearStart?: number;
  modelYearEnd?: number;
  officialModelCode?: string;
  trim?: string;
  identityDimensions: CatalogIdentityDimensions;
  promotedAt?: string;
  revisedAt?: string;
};

export type CatalogReviewCandidateItem = {
  candidateKey: string;
  status: string;
  manufacturer?: string;
  commercialModel?: string;
  modelYearStart?: number;
  modelYearEnd?: number;
  officialModelCode?: string;
  trim?: string;
  identityDimensions: CatalogIdentityDimensions;
};

export type CatalogReviewSnapshot = {
  snapshotKey: string;
  resourceId?: string;
  packageId?: string;
  publisher?: string;
  datasetTitle?: string;
  datasetMarketScope?: string;
  upstreamVersion?: string;
  upstreamVersionKind?: string;
  activatedAt?: string;
  declaredRecordCount?: number;
  storedRecordCount?: number;
  normalizationContract?: string;
  normalizationIssueCount?: number;
};

export type CanonicalCatalogPage = {
  page: CatalogPageMeta;
  items: CanonicalCatalogItem[];
};

/**
 * A review page, or an honest statement that there is nothing to review from.
 *
 * `available: false` is NOT an empty catalog: `items` is empty, `snapshot` is
 * undefined and `page.total` is `null` rather than `0`, and `unavailableReason`
 * says which condition held.
 */
export type CatalogReviewPage = {
  available: boolean;
  unavailableReason?: CatalogUnavailableReason;
  status: string;
  snapshot?: CatalogReviewSnapshot;
  page: CatalogPageMeta;
  items: CatalogReviewCandidateItem[];
};

/** The closed vocabulary `REVIEW_UNAVAILABLE_REASONS` (backend) states. */
export type CatalogUnavailableReason =
  | 'no_active_snapshot'
  | 'snapshot_not_read'
  | 'snapshot_not_normalized'
  | 'snapshot_incomplete'
  | 'snapshot_state_invalid'
  | 'snapshot_unknown'
  | 'snapshot_unavailable';

export type { EventId } from './eventId';
export type { RunUsage } from './runUsage';
