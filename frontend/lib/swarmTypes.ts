/**
 * Swarm V2 view-model types.
 *
 * These describe OBSERVABLE EXECUTION ONLY. Nothing here represents a model's
 * reasoning: there is no field for a Commander rationale, a verifier
 * explanation, a repair prompt, evidence fragment text or a content hash. The
 * reducer may only write what a public, bounded event payload states.
 *
 * There is deliberately NO agent concept. A logical task is not an agent and
 * is not a model call; the run's model-call count lives in `run.usage` alone.
 */

import { EventId } from './eventId';

export type SwarmLifecyclePhase =
  | 'idle'
  | 'planning'
  | 'plan_created'
  | 'running_tasks'
  | 'replanning'
  | 'verifying'
  | 'completed'
  | 'partial_success'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'budget_exhausted';

/** Only states the public event contract can actually establish. */
export type SwarmTaskStatus = 'pending' | 'ready' | 'running' | 'completed' | 'failed';

export type SwarmTaskState = {
  /** Logical identity: `payload.task_id`. Never `event.agent`. */
  taskId: string;
  /** Deterministic formatting of taskId; no lookup table, no model. */
  label: string;
  status: SwarmTaskStatus;
  /** Static failure code from `task_failed.payload.code`; never a message. */
  failureCode?: string;
  /** B3 telemetry: bounded worker-output repairs. Never a task or an agent. */
  repairCount: number;
  /** `tool_called` occurrences for this task. */
  toolCallCount: number;
  /** Distinct `evidence_added.claim_id` values attributed to this task. */
  evidenceClaimIds: string[];
  firstEventId: EventId;
  lastEventId: EventId;
};

export type SwarmReplanDecision = 'ADD_TASKS' | 'REVISE_TASK' | 'REQUEST_VERIFICATION' | 'FINISH';

export type SwarmPlanState = {
  planCreated: boolean;
  /** 0 before `commander_plan_created`; then the backend's graph_revision. */
  graphRevision: number;
  replanCount: number;
  lastReplanDecision?: SwarmReplanDecision;
};

export type SwarmVerificationState = {
  started: boolean;
  completed: boolean;
  /** Grounding counts only (B5). No source text ever enters this state. */
  groundingResolved: boolean;
  claimCount?: number;
  groundingSourceCount?: number;
  missingContextCount?: number;
  /** Last completed batch index (B4); 0 means no batch completed yet. */
  completedBatchIndex: number;
  batchCount?: number;
  /** Number of `verification_batch_completed` events observed. */
  completedBatches: number;
};

export type SwarmActivityItem = {
  eventId: EventId;
  eventType: string;
  taskId?: string;
  /** A static allowlisted code, never free text. */
  code?: string;
  createdAt?: string;
};

export type SwarmRunState = {
  lifecycle: SwarmLifecyclePhase;
  plan: SwarmPlanState;
  /** Keyed by task_id; the only logical-task registry. */
  tasks: Record<string, SwarmTaskState>;
  /** First-seen order, so rendering is deterministic. */
  taskOrder: string[];
  verification: SwarmVerificationState;
  /** Bounded ring of recent observable activity. */
  activity: SwarmActivityItem[];
  conflictClaimIds: string[];
  evidenceClaimIds: string[];
  /** Safe forward compatibility: unrecognized event types are recorded, not applied. */
  unknownEventTypes: string[];
  /** Highest event id folded in; the swarm slice only advances forward. */
  lastEventId?: EventId;
};

export const MAX_SWARM_ACTIVITY_ITEMS = 200;

export const initialSwarmPlanState: SwarmPlanState = {
  planCreated: false,
  graphRevision: 0,
  replanCount: 0,
};

export const initialSwarmVerificationState: SwarmVerificationState = {
  started: false,
  completed: false,
  groundingResolved: false,
  completedBatchIndex: 0,
  completedBatches: 0,
};

export const initialSwarmRunState: SwarmRunState = {
  lifecycle: 'idle',
  plan: initialSwarmPlanState,
  tasks: {},
  taskOrder: [],
  verification: initialSwarmVerificationState,
  activity: [],
  conflictClaimIds: [],
  evidenceClaimIds: [],
  unknownEventTypes: [],
};

/** Workflow keys the frontend knows about; anything else falls back to V1 rendering. */
export const SWARM_V2_WORKFLOW_KEY = 'swarm_v2';
export const VEHICLE_CATALOG_V1_WORKFLOW_KEY = 'vehicle_catalog_v1';

export function isSwarmV2Workflow(workflowKey?: string | null): boolean {
  return workflowKey === SWARM_V2_WORKFLOW_KEY;
}

/**
 * Deterministic, formatting-only label for a logical task id.
 *
 *     humanizeTaskId('catalog_health') === 'Catalog health'
 *
 * Separators become single spaces and the first character is upper-cased.
 * Nothing else is altered: no alias table, no dictionary, no model.
 */
export function humanizeTaskId(taskId: string): string {
  const spaced = String(taskId ?? '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (spaced === '') return String(taskId ?? '');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
