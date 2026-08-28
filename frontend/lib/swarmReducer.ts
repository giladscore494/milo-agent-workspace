/**
 * Deterministic Swarm V2 event reducer.
 *
 * Every mutation below is traceable to an event the backend actually emits on
 * the public path:
 *
 *   backend/main.py                       run_created
 *   backend/worker/main.py                run_started, run_resumed, run_completed,
 *                                         run_partial_success, run_failed,
 *                                         run_cancelled, cancellation_requested
 *   backend/budget.py                     budget_warning, budget_exhausted,
 *                                         token_limit_reached, run_timed_out
 *   swarm_v2/engine.py                    commander_plan_created, evidence_added,
 *                                         conflict_found, commander_replanned,
 *                                         grounding_context_resolved,
 *                                         verification_batch_completed,
 *                                         verification_completed
 *   swarm_v2/executor.py                  task_ready, task_started,
 *                                         task_completed, task_failed
 *   swarm_v2/worker.py                    tool_called, worker_output_repair_started
 *
 * Three structural rules hold for every branch:
 *
 *  1. A logical task is identified by `payload.task_id`. `event.agent` is never
 *     consulted: the swarm event sink's allowlist (engine.py `_emit`) does not
 *     even include an `agent` key, so swarm events reach the browser with
 *     `agent = null`.
 *  2. B3 repairs, tool calls and B4/B5 verifier batches update counters on an
 *     EXISTING logical task or on verification state. They can never introduce
 *     a new logical task and there is no agent registry for them to grow.
 *  3. Usage is not derived here. `run.usage` is the sole authority.
 *
 * Ordering: the backend returns events ordered by `id` ascending and the
 * `after_event_id` cursor only moves forward, so the slice folds strictly
 * increasing ids and ignores anything at or below the id it already folded.
 * That makes a replayed or duplicated event a no-op by construction.
 */

import { EventId, compareEventIds, normalizeEventId } from './eventId';
import {
  MAX_SWARM_ACTIVITY_ITEMS,
  SwarmActivityItem,
  SwarmLifecyclePhase,
  SwarmReplanDecision,
  SwarmRunState,
  SwarmTaskState,
  SwarmTaskStatus,
  humanizeTaskId,
  initialSwarmRunState,
} from './swarmTypes';
import { RunEvent } from './types';

const REPLAN_DECISIONS: ReadonlySet<string> = new Set([
  'ADD_TASKS',
  'REVISE_TASK',
  'REQUEST_VERIFICATION',
  'FINISH',
]);

/** Terminal lifecycle phases are sticky: no later event moves them. */
const TERMINAL_LIFECYCLE: ReadonlySet<SwarmLifecyclePhase> = new Set<SwarmLifecyclePhase>([
  'completed',
  'partial_success',
  'failed',
  'cancelled',
  'timed_out',
  'budget_exhausted',
]);

/** A task status may advance but never regress. */
const TASK_STATUS_RANK: Record<SwarmTaskStatus, number> = {
  pending: 0,
  ready: 1,
  running: 2,
  completed: 3,
  failed: 3,
};

function readPayload(event: RunEvent): Record<string, unknown> {
  const payload: unknown = event.payload;
  return payload && typeof payload === 'object' && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : {};
}

function readString(payload: Record<string, unknown>, key: string): string | undefined {
  const value = payload[key];
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

function readCount(payload: Record<string, unknown>, key: string): number | undefined {
  const value = payload[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function appendUnique(items: readonly string[], value: string): string[] {
  return items.includes(value) ? [...items] : [...items, value];
}

function defaultTask(taskId: string, eventId: EventId): SwarmTaskState {
  return {
    taskId,
    label: humanizeTaskId(taskId),
    status: 'pending',
    repairCount: 0,
    toolCallCount: 0,
    evidenceClaimIds: [],
    firstEventId: eventId,
    lastEventId: eventId,
  };
}

/**
 * Upsert the logical task addressed by `payload.task_id`.
 *
 * This is the ONLY place a logical task can come into existence, and its key is
 * always a task id. Repeated repair, tool and evidence events for one task id
 * therefore collapse onto one task rather than multiplying it.
 */
function withTask(
  state: SwarmRunState,
  taskId: string,
  eventId: EventId,
  mutate: (task: SwarmTaskState) => SwarmTaskState,
): SwarmRunState {
  const existing = state.tasks[taskId];
  const base = existing ?? defaultTask(taskId, eventId);
  const updated = mutate({ ...base });
  return {
    ...state,
    tasks: { ...state.tasks, [taskId]: { ...updated, lastEventId: eventId } },
    taskOrder: existing ? state.taskOrder : [...state.taskOrder, taskId],
  };
}

function advanceStatus(task: SwarmTaskState, status: SwarmTaskStatus): SwarmTaskState {
  return TASK_STATUS_RANK[status] >= TASK_STATUS_RANK[task.status]
    ? { ...task, status }
    : task;
}

function withLifecycle(state: SwarmRunState, lifecycle: SwarmLifecyclePhase): SwarmRunState {
  if (TERMINAL_LIFECYCLE.has(state.lifecycle)) return state;
  return state.lifecycle === lifecycle ? state : { ...state, lifecycle };
}

function withActivity(state: SwarmRunState, item: SwarmActivityItem): SwarmRunState {
  const activity = [...state.activity, item];
  return {
    ...state,
    activity:
      activity.length > MAX_SWARM_ACTIVITY_ITEMS
        ? activity.slice(activity.length - MAX_SWARM_ACTIVITY_ITEMS)
        : activity,
  };
}

/**
 * Fold one run event into the Swarm V2 slice.
 *
 * Safe for any event stream: a V1 event, an event without a payload and an
 * event type this release has never heard of all leave existing state intact.
 */
export function reduceSwarmEvent(state: SwarmRunState, event: RunEvent): SwarmRunState {
  const eventId = normalizeEventId(event.id);
  if (state.lastEventId !== undefined && compareEventIds(eventId, state.lastEventId) <= 0) {
    return state;
  }

  const payload = readPayload(event);
  const taskId = readString(payload, 'task_id');
  const code = readString(payload, 'code');
  const type = event.event_type;

  let next: SwarmRunState = { ...state, lastEventId: eventId };
  let recognized = true;

  switch (type) {
    case 'run_created':
    case 'run_started':
    case 'run_resumed':
      next = withLifecycle(next, 'planning');
      break;

    case 'commander_plan_created':
      next = {
        ...next,
        plan: {
          ...next.plan,
          planCreated: true,
          graphRevision: readCount(payload, 'graph_revision') ?? next.plan.graphRevision ?? 0,
        },
      };
      next = withLifecycle(next, 'plan_created');
      break;

    case 'commander_replanned': {
      const decision = readString(payload, 'decision');
      next = {
        ...next,
        plan: {
          ...next.plan,
          planCreated: true,
          // The backend supplies the authoritative revision; only fall back to
          // an increment when the payload omits it.
          graphRevision: readCount(payload, 'graph_revision') ?? next.plan.graphRevision + 1,
          replanCount: next.plan.replanCount + 1,
          lastReplanDecision:
            decision && REPLAN_DECISIONS.has(decision)
              ? (decision as SwarmReplanDecision)
              : next.plan.lastReplanDecision,
        },
      };
      next = withLifecycle(next, 'replanning');
      break;
    }

    case 'task_ready':
      if (taskId) next = withTask(next, taskId, eventId, (task) => advanceStatus(task, 'ready'));
      next = withLifecycle(next, 'running_tasks');
      break;

    case 'task_started':
      if (taskId) next = withTask(next, taskId, eventId, (task) => advanceStatus(task, 'running'));
      next = withLifecycle(next, 'running_tasks');
      break;

    case 'task_completed':
      if (taskId) next = withTask(next, taskId, eventId, (task) => advanceStatus(task, 'completed'));
      next = withLifecycle(next, 'running_tasks');
      break;

    case 'task_failed':
      if (taskId) {
        next = withTask(next, taskId, eventId, (task) => ({
          ...advanceStatus(task, 'failed'),
          status: 'failed',
          failureCode: code ?? task.failureCode,
        }));
      }
      next = withLifecycle(next, 'running_tasks');
      break;

    case 'tool_called':
      // Bounded per-task telemetry. Never a task, never an agent.
      if (taskId) {
        next = withTask(next, taskId, eventId, (task) => ({
          ...task,
          toolCallCount: task.toolCallCount + 1,
        }));
      }
      break;

    case 'worker_output_repair_started':
      // B3: one bounded repair of a structurally invalid worker completion.
      // It is an extra MODEL CALL on the SAME logical task; the reason code is
      // deliberately not stored, so no repair rationale can be rendered.
      if (taskId) {
        next = withTask(next, taskId, eventId, (task) => ({
          ...task,
          repairCount: task.repairCount + 1,
        }));
      }
      break;

    case 'evidence_added': {
      // claim/source identifiers only; fragment text never reaches the client.
      const claimId = readString(payload, 'claim_id');
      if (claimId) {
        next = { ...next, evidenceClaimIds: appendUnique(next.evidenceClaimIds, claimId) };
        if (taskId) {
          next = withTask(next, taskId, eventId, (task) => ({
            ...task,
            evidenceClaimIds: appendUnique(task.evidenceClaimIds, claimId),
          }));
        }
      }
      break;
    }

    case 'conflict_found': {
      const claimId = readString(payload, 'claim_id');
      if (claimId) {
        next = { ...next, conflictClaimIds: appendUnique(next.conflictClaimIds, claimId) };
      }
      break;
    }

    case 'grounding_context_resolved':
      // B5: counts only. No source id, no fragment, no hash, no prompt.
      next = {
        ...next,
        verification: {
          ...next.verification,
          started: true,
          groundingResolved: true,
          claimCount: readCount(payload, 'claim_count') ?? next.verification.claimCount,
          groundingSourceCount:
            readCount(payload, 'source_count') ?? next.verification.groundingSourceCount,
          missingContextCount:
            readCount(payload, 'missing_context_count') ?? next.verification.missingContextCount,
        },
      };
      next = withLifecycle(next, 'verifying');
      break;

    case 'verification_batch_completed': {
      // B4: one run legitimately has many verifier batches. They are progress
      // on ONE verification activity, not extra tasks and not extra agents.
      const batchIndex = readCount(payload, 'batch_index');
      next = {
        ...next,
        verification: {
          ...next.verification,
          started: true,
          completedBatches: next.verification.completedBatches + 1,
          completedBatchIndex:
            batchIndex !== undefined
              ? Math.max(next.verification.completedBatchIndex, batchIndex)
              : next.verification.completedBatchIndex,
          batchCount: readCount(payload, 'batch_count') ?? next.verification.batchCount,
          claimCount: readCount(payload, 'claim_count') ?? next.verification.claimCount,
        },
      };
      next = withLifecycle(next, 'verifying');
      break;
    }

    case 'verification_completed':
      next = {
        ...next,
        verification: { ...next.verification, started: true, completed: true },
      };
      next = withLifecycle(next, 'verifying');
      break;

    case 'run_completed':
      next = withLifecycle(next, 'completed');
      break;

    case 'run_partial_success':
      next = withLifecycle(next, 'partial_success');
      break;

    case 'run_failed':
      next = withLifecycle(next, 'failed');
      break;

    case 'run_cancelled':
      next = withLifecycle(next, 'cancelled');
      break;

    case 'run_timed_out':
      next = withLifecycle(next, 'timed_out');
      break;

    case 'budget_exhausted':
      next = withLifecycle(next, 'budget_exhausted');
      break;

    case 'cancellation_requested':
    case 'budget_warning':
    case 'token_limit_reached':
    case 'checkpoint_saved':
      // Observed and recorded, but not a lifecycle or task transition.
      break;

    default:
      recognized = false;
      break;
  }

  if (!recognized) {
    // A safe future event must not corrupt state or crash the reducer; it is
    // recorded as an observation and nothing else.
    next = { ...next, unknownEventTypes: appendUnique(next.unknownEventTypes, type) };
  }

  return withActivity(next, {
    eventId,
    eventType: type,
    taskId,
    code,
    createdAt: event.created_at,
  });
}

/** Fold a whole ordered stream; used by reconstruction and by tests. */
export function reduceSwarmEvents(
  events: readonly RunEvent[],
  state: SwarmRunState = initialSwarmRunState,
): SwarmRunState {
  return events.reduce(reduceSwarmEvent, state);
}
