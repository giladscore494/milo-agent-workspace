/**
 * Selectors that turn durable run facts plus the Swarm V2 slice into the
 * view-model the UI renders.
 *
 * The view model has NO agent field, by design. "Task", "model call" and
 * "verifier batch" are distinct, separately sourced quantities:
 *
 *   logical tasks   <- distinct payload.task_id values in the event stream
 *   model calls     <- run.usage.model_calls (authoritative, never derived)
 *   verifier batches<- verification_batch_completed telemetry
 *   repairs         <- worker_output_repair_started telemetry
 *
 * The accepted production smoke run is 5 logical tasks and 7 model calls. There
 * is deliberately no code path that could turn that into "7 agents".
 */

import {
  NormalizedRunUsage,
  RunUsage,
  normalizeRunUsage,
} from './runUsage';
import {
  TerminalRunStatus,
  isFullySuccessfulRunStatus,
  isPartialSuccessRunStatus,
  isTerminalRunStatus,
} from './runStatus';
import {
  SwarmActivityItem,
  SwarmLifecyclePhase,
  SwarmPlanState,
  SwarmRunState,
  SwarmTaskState,
  SwarmVerificationState,
  initialSwarmRunState,
  isSwarmV2Workflow,
} from './swarmTypes';
import { Run } from './types';

export type SwarmTaskCounts = {
  total: number;
  pending: number;
  ready: number;
  running: number;
  completed: number;
  failed: number;
};

/**
 * Named, non-interchangeable execution quantities. Keeping them in one record
 * with distinct names is the structural guard against conflating them.
 */
export type SwarmExecutionScale = {
  /** Distinct logical tasks (payload.task_id). */
  logicalTasks: number;
  /** run.usage.model_calls, or undefined when the run reports no usage. */
  modelCalls?: number;
  /** Completed verifier batches (B4); many batches, still one verification. */
  verifierBatches: number;
  /** Bounded worker-output repairs (B3); extra model calls, no extra task. */
  workerRepairs: number;
};

export type SwarmRunViewModel = {
  workflowKey?: string;
  isSwarmV2: boolean;
  lifecycle: SwarmLifecyclePhase;
  lifecycleLabel: string;
  runStatus?: string;
  terminal: boolean;
  terminalStatus?: TerminalRunStatus;
  /** True only for `completed`. `partial_success` is terminal but not this. */
  fullySuccessful: boolean;
  partialSuccess: boolean;
  plan: SwarmPlanState;
  tasks: SwarmTaskState[];
  taskCounts: SwarmTaskCounts;
  verification: SwarmVerificationState;
  usage: NormalizedRunUsage;
  scale: SwarmExecutionScale;
  activity: SwarmActivityItem[];
  conflictClaimCount: number;
  evidenceClaimCount: number;
  unknownEventTypes: string[];
};

const LIFECYCLE_LABELS: Record<SwarmLifecyclePhase, string> = {
  idle: 'Idle',
  planning: 'Planning',
  plan_created: 'Plan created',
  running_tasks: 'Running tasks',
  replanning: 'Replanning',
  verifying: 'Verifying',
  completed: 'Completed',
  partial_success: 'Partial success',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timed_out: 'Timed out',
  budget_exhausted: 'Budget exhausted',
};

/**
 * Durable terminal statuses map one-to-one onto terminal lifecycle phases, so a
 * run that reached `timed_out` or `budget_exhausted` can never be rendered as
 * still running even when no matching event was ever appended.
 */
const TERMINAL_STATUS_LIFECYCLE: Record<TerminalRunStatus, SwarmLifecyclePhase> = {
  completed: 'completed',
  partial_success: 'partial_success',
  failed: 'failed',
  cancelled: 'cancelled',
  timed_out: 'timed_out',
  budget_exhausted: 'budget_exhausted',
};

export function swarmLifecycleLabel(phase: SwarmLifecyclePhase): string {
  return LIFECYCLE_LABELS[phase];
}

export function selectSwarmTasks(state: SwarmRunState): SwarmTaskState[] {
  return state.taskOrder
    .map((taskId) => state.tasks[taskId])
    .filter((task): task is SwarmTaskState => task !== undefined);
}

export function selectSwarmTaskCounts(state: SwarmRunState): SwarmTaskCounts {
  const counts: SwarmTaskCounts = {
    total: 0,
    pending: 0,
    ready: 0,
    running: 0,
    completed: 0,
    failed: 0,
  };
  for (const task of selectSwarmTasks(state)) {
    counts.total += 1;
    counts[task.status] += 1;
  }
  return counts;
}

/** Tasks running concurrently; two different task ids may both be running. */
export function selectRunningSwarmTaskIds(state: SwarmRunState): string[] {
  return selectSwarmTasks(state)
    .filter((task) => task.status === 'running')
    .map((task) => task.taskId);
}

export type SwarmRunViewModelInput = {
  run?: (Run & { usage?: RunUsage | null }) | null;
  swarm?: SwarmRunState;
  /** Trusted project identity; selects V2 vs V1 presentation. */
  workflowKey?: string;
};

export function buildSwarmRunViewModel(input: SwarmRunViewModelInput): SwarmRunViewModel {
  const swarm = input.swarm ?? initialSwarmRunState;
  const runStatus = input.run?.status;
  const terminalStatus = isTerminalRunStatus(runStatus) ? runStatus : undefined;

  // The durable run row wins over event-derived phase: events can be missing,
  // truncated or still in flight, the status column cannot.
  const lifecycle = terminalStatus
    ? TERMINAL_STATUS_LIFECYCLE[terminalStatus]
    : swarm.lifecycle;

  const usage = normalizeRunUsage(input.run?.usage);
  const tasks = selectSwarmTasks(swarm);

  return {
    workflowKey: input.workflowKey,
    isSwarmV2: isSwarmV2Workflow(input.workflowKey),
    lifecycle,
    lifecycleLabel: swarmLifecycleLabel(lifecycle),
    runStatus,
    terminal: terminalStatus !== undefined,
    terminalStatus,
    fullySuccessful: isFullySuccessfulRunStatus(runStatus),
    partialSuccess: isPartialSuccessRunStatus(runStatus),
    plan: swarm.plan,
    tasks,
    taskCounts: selectSwarmTaskCounts(swarm),
    verification: swarm.verification,
    usage,
    scale: {
      logicalTasks: tasks.length,
      modelCalls: usage.modelCalls,
      verifierBatches: swarm.verification.completedBatches,
      workerRepairs: tasks.reduce((total, task) => total + task.repairCount, 0),
    },
    activity: swarm.activity,
    conflictClaimCount: swarm.conflictClaimIds.length,
    evidenceClaimCount: swarm.evidenceClaimIds.length,
    unknownEventTypes: swarm.unknownEventTypes,
  };
}

/**
 * Compact, safe summary for the existing inspector. Counts and codes only: no
 * evidence text, no source text, no hashes, no prompts, no reasoning.
 */
export function summarizeSwarmRun(viewModel: SwarmRunViewModel): Record<string, unknown> {
  return {
    workflow: viewModel.workflowKey,
    lifecycle: viewModel.lifecycleLabel,
    run_status: viewModel.runStatus,
    terminal: viewModel.terminal,
    plan: {
      created: viewModel.plan.planCreated,
      graph_revision: viewModel.plan.graphRevision,
      replans: viewModel.plan.replanCount,
    },
    logical_tasks: viewModel.scale.logicalTasks,
    task_counts: viewModel.taskCounts,
    verification: {
      started: viewModel.verification.started,
      completed: viewModel.verification.completed,
      batches_completed: viewModel.verification.completedBatches,
      batch_count: viewModel.verification.batchCount,
      claim_count: viewModel.verification.claimCount,
      grounding_source_count: viewModel.verification.groundingSourceCount,
      missing_context_count: viewModel.verification.missingContextCount,
    },
    // Authoritative aggregate usage; never a sum over events.
    model_calls: viewModel.usage.modelCalls,
    total_tokens: viewModel.usage.totalTokens,
    actual_cost: viewModel.usage.actualCost,
    usage_reported: viewModel.usage.present,
    worker_repairs: viewModel.scale.workerRepairs,
    conflicts: viewModel.conflictClaimCount,
    evidence_claims: viewModel.evidenceClaimCount,
  };
}
