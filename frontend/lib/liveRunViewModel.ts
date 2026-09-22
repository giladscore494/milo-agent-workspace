/**
 * ONE live-run view for both engines, derived from durable backend truth.
 *
 * Every number here comes from the run row (`status`, `usage`, `limits`,
 * `product_outcome`, `run_identity`) or from the already-reduced event
 * projections (`WorkspaceState` for V1, `SwarmRunViewModel` for V2). Nothing is
 * invented: a quantity the backend has not stated is `undefined` and renders as
 * "not reported", never as zero. A refresh or reconnect rebuilds the same view
 * from the same durable reads, so it can never show state that exists only in
 * the browser.
 *
 * Which engine's projection is read is decided by the run's immutable identity
 * (`runIdentityWorkflowKey`) and by nothing else — not the project's current
 * workflow and not the shape of the events.
 */

import { ProductOutcome, parseProductOutcome } from './productOutcome';
import { runIdentityWorkflowKey } from './runIdentity';
import { isTerminalRunStatus } from './runStatus';
import { NormalizedRunUsage, normalizeRunUsage } from './runUsage';
import { SWARM_V2_WORKFLOW_KEY, VEHICLE_CATALOG_V1_WORKFLOW_KEY } from './swarmTypes';
import { SwarmRunViewModel } from './swarmViewModel';
import { swarmLifecycleLabel } from './swarmViewModel';
import { Run, WorkspaceState } from './types';

export type LiveEngine = 'vehicle_catalog_v1' | 'swarm_v2';

export type RunLimits = {
  maxModelCalls?: number;
  maxTotalTokens?: number;
  maxCost?: number;
  maxDurationSeconds?: number;
  maxAgentSteps?: number;
};

export type WorkCounts = {
  /** What the unit of work is called for this engine, for the label. */
  unit: 'tasks' | 'chunks';
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
};

export type ActiveWorker = { name: string; doing: string };

export type EvidenceProgress = {
  sources?: number;
  claims?: number;
  conflicts?: number;
  /** V2 only: verifier batches completed so far. */
  verifierBatches?: number;
  verificationStarted?: boolean;
  verificationCompleted?: boolean;
};

export type ProviderPressure = {
  /** run.usage.provider_backpressure_events — the authoritative count. */
  backpressureEvents?: number;
  /** run.usage.retries — semantic retries charged to the run. */
  retries?: number;
  /** Durable operational pacing events observed in this run's stream. */
  pacingEventsObserved: number;
};

export type FinalizationView =
  | { state: 'live' }
  | { state: 'finalized'; outcome: ProductOutcome }
  | { state: 'terminal_without_outcome' };

export type LiveRunViewModel = {
  runId?: string;
  /** From the immutable identity; undefined until the run row loads or when it is untrustworthy. */
  engine?: LiveEngine;
  engineLabel: string;
  engineVersion?: string;
  status?: string;
  terminal: boolean;
  phaseLabel: string;
  work?: WorkCounts;
  active: ActiveWorker[];
  evidence: EvidenceProgress;
  provider: ProviderPressure;
  usage: NormalizedRunUsage;
  limits: RunLimits;
  /** actual (or estimated) cost over the cost ceiling, 0..1, when both are known. */
  spendRatio?: number;
  finalization: FinalizationView;
};

/** Operational pacing types (backend/event_registry.py OPERATIONAL_EVENT_TYPES). Exact membership only. */
const PACING_EVENT_TYPES = new Set(['provider_backpressure_wait', 'provider_rate_limited', 'provider_quota_paused']);

const ENGINE_LABELS: Record<LiveEngine, string> = {
  vehicle_catalog_v1: 'Vehicle Catalog V1',
  swarm_v2: 'Swarm V2',
};

const V1_PHASE_LABELS: Record<string, string> = {
  idle: 'Idle',
  reconnected: 'Reconnected',
  discovery: 'Discovery',
  normalizer: 'Normalization',
  technical_enrichment: 'Technical enrichment',
  verification: 'Verification',
  final_builder: 'Final assembly',
  summary: 'Summary',
  research: 'Research',
};

function nonNegative(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function parseRunLimits(raw: unknown): RunLimits {
  if (!raw || typeof raw !== 'object') return {};
  const record = raw as Record<string, unknown>;
  return {
    maxModelCalls: nonNegative(record.max_model_calls_per_run),
    maxTotalTokens: nonNegative(record.max_total_tokens_per_run),
    maxCost: nonNegative(record.max_cost_per_run),
    maxDurationSeconds: nonNegative(record.max_run_duration_seconds),
    maxAgentSteps: nonNegative(record.max_agent_steps),
  };
}

function humanStatus(status?: string): string {
  if (!status) return 'Loading';
  return status.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function v1Work(state: WorkspaceState): WorkCounts | undefined {
  let started = 0; let completed = 0; let failed = 0;
  for (const event of state.events) {
    if (event.event_type === 'chunk_started') started += 1;
    else if (event.event_type === 'chunk_completed') completed += 1;
    else if (event.event_type === 'chunk_failed') failed += 1;
  }
  const total = Math.max(started, completed + failed);
  if (total === 0) return undefined;
  return { unit: 'chunks', total, queued: 0, running: Math.max(0, started - completed - failed), completed, failed };
}

function v1Active(state: WorkspaceState): ActiveWorker[] {
  return Object.values(state.agents)
    .filter((agent) => agent.status === 'active')
    .slice(0, 12)
    .map((agent) => ({ name: agent.name, doing: agent.currentTask ?? 'working' }));
}

function finalization(run: Run | undefined, terminal: boolean): FinalizationView {
  if (!run || !terminal) return { state: 'live' };
  const outcome = parseProductOutcome((run as unknown as Record<string, unknown>).product_outcome);
  return outcome ? { state: 'finalized', outcome } : { state: 'terminal_without_outcome' };
}

export function buildLiveRunViewModel(input: {
  runId?: string;
  state: WorkspaceState;
  swarm: SwarmRunViewModel;
}): LiveRunViewModel {
  const { runId, state, swarm } = input;
  const run = state.run;
  const workflowKey = runIdentityWorkflowKey(run);
  const engine: LiveEngine | undefined =
    workflowKey === SWARM_V2_WORKFLOW_KEY ? 'swarm_v2'
      : workflowKey === VEHICLE_CATALOG_V1_WORKFLOW_KEY ? 'vehicle_catalog_v1'
        : undefined;
  const status = run?.status;
  const terminal = isTerminalRunStatus(status);
  const usage = normalizeRunUsage(run?.usage);
  const limits = parseRunLimits((run as unknown as Record<string, unknown> | undefined)?.limits);
  const cost = usage.actualCost ?? usage.estimatedCost;
  const spendRatio = cost !== undefined && limits.maxCost ? Math.min(1, cost / limits.maxCost) : undefined;
  let pacing = 0;
  for (const event of state.events) if (PACING_EVENT_TYPES.has(event.event_type)) pacing += 1;

  const base = {
    runId,
    engine,
    engineLabel: engine ? ENGINE_LABELS[engine] : 'Engine not stated',
    engineVersion: run?.run_identity?.engine_version,
    status,
    terminal,
    usage,
    limits,
    spendRatio,
    provider: { backpressureEvents: usage.providerBackpressureEvents, retries: usage.retries, pacingEventsObserved: pacing },
    finalization: finalization(run, terminal),
  };

  if (engine === 'swarm_v2') {
    const running = swarm.tasks.filter((task) => task.status === 'running');
    return {
      ...base,
      phaseLabel: terminal ? humanStatus(status) : swarmLifecycleLabel(swarm.lifecycle),
      work: {
        unit: 'tasks',
        total: swarm.taskCounts.total,
        queued: swarm.taskCounts.pending + swarm.taskCounts.ready,
        running: swarm.taskCounts.running,
        completed: swarm.taskCounts.completed,
        failed: swarm.taskCounts.failed,
      },
      active: running.slice(0, 12).map((task) => ({
        name: task.label,
        doing: task.toolCallCount > 0 ? `running · ${task.toolCallCount} tool call${task.toolCallCount === 1 ? '' : 's'}` : 'running',
      })),
      evidence: {
        claims: swarm.evidenceClaimCount,
        conflicts: swarm.conflictClaimCount,
        verifierBatches: swarm.scale.verifierBatches,
        verificationStarted: swarm.verification.started,
        verificationCompleted: swarm.verification.completed,
      },
    };
  }

  if (engine === 'vehicle_catalog_v1') {
    const phase = state.currentPhase;
    return {
      ...base,
      phaseLabel: terminal ? humanStatus(status) : (V1_PHASE_LABELS[phase] ?? humanStatus(phase)),
      work: v1Work(state),
      active: v1Active(state),
      evidence: {
        sources: state.sources.length,
        claims: state.claims.length,
        conflicts: state.conflicts.length,
      },
    };
  }

  // No trustworthy identity yet (or at all): only run-row facts are stated.
  return { ...base, phaseLabel: humanStatus(status), active: [], evidence: {} };
}
