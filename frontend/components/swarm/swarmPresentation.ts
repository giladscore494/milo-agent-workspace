/**
 * Pure presentation mapping for the Swarm V2 run card.
 *
 * Everything here is a total function of the F1 `SwarmRunViewModel`. It adds no
 * state, reads no event stream and invents no quantity: it only decides how an
 * already-established fact is worded, ordered and formatted.
 *
 * Two rules are structural rather than stylistic:
 *
 *  1. There is no progress percentage. Structural stage state is derived from
 *     positive evidence (a plan was created, a task exists, verification
 *     started, the run reached a durable terminal status) — never from a ratio
 *     that would imply the backend reported a completion fraction it never
 *     sent.
 *  2. Absent is not zero. A usage field the backend did not report renders as
 *     "Not reported"; an optional verification count that is undefined is
 *     omitted entirely rather than shown as 0.
 */

import { SwarmLifecyclePhase, SwarmTaskStatus } from '@/lib/swarmTypes';
import { SwarmRunViewModel, swarmLifecycleLabel } from '@/lib/swarmViewModel';

/** The four user-facing stages. Backend lifecycle phases map onto these. */
export const SWARM_STAGES = ['Planning', 'Executing', 'Verifying', 'Finished'] as const;
export type SwarmStage = (typeof SWARM_STAGES)[number];

export type SwarmStageState = 'pending' | 'active' | 'complete';

export type SwarmStagePresentation = {
  stage: SwarmStage;
  state: SwarmStageState;
  /** The stage the run is positioned at right now; at most one is true. */
  current: boolean;
  /** Drives colour AND icon, so meaning never rests on colour alone. */
  tone: SwarmStageTone;
  icon: string;
  /** Explicit wording for this cell: "Done", "Ended", "Failed", ... */
  stateLabel: string;
};

/**
 * A stage cell's meaning.
 *
 * `success` is the ONLY tone that renders a green checkmark, and the Finished
 * cell can only reach it on `completed`. A run that failed, was cancelled,
 * timed out, exhausted its budget or only partly succeeded never shows a
 * success mark anywhere in the track — the stages it did reach report `neutral`
 * ("Ended"), because "this stage is over" is a fact and "this stage succeeded"
 * is not.
 */
export type SwarmStageTone = 'pending' | 'active' | 'success' | 'warning' | 'failure' | 'neutral';

/** The six durable terminal outcomes, as lifecycle phases. */
export type SwarmTerminalPhase =
  | 'completed'
  | 'partial_success'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'budget_exhausted';

export type SwarmOutcomeTone = 'success' | 'warning' | 'failure' | 'neutral';

/**
 * How each terminal outcome is allowed to look.
 *
 * Keyed by lifecycle phase rather than by run status, because the view model's
 * `lifecycle` is the resolved outcome: a durable terminal status wins over
 * event-derived state there, and a terminal event still resolves while the run
 * row is briefly behind.
 */
const TERMINAL_OUTCOME: Record<SwarmTerminalPhase, { tone: SwarmOutcomeTone; icon: string }> = {
  completed: { tone: 'success', icon: '✓' },
  partial_success: { tone: 'warning', icon: '⚠' },
  failed: { tone: 'failure', icon: '✕' },
  cancelled: { tone: 'neutral', icon: '⊘' },
  timed_out: { tone: 'warning', icon: '⚠' },
  budget_exhausted: { tone: 'warning', icon: '⚠' },
};

function isTerminalPhase(phase: SwarmLifecyclePhase): phase is SwarmTerminalPhase {
  return phase in TERMINAL_OUTCOME;
}

/** Icon and wording for the three non-terminal stage states. */
const STAGE_STATE_PRESENTATION: Record<SwarmStageState, { tone: SwarmStageTone; icon: string; label: string }> = {
  pending: { tone: 'pending', icon: '○', label: 'Not started' },
  active: { tone: 'active', icon: '◐', label: 'In progress' },
  complete: { tone: 'success', icon: '✓', label: 'Done' },
};

/** A reached stage on a run that did not fully succeed: over, not successful. */
const ENDED_STAGE = { tone: 'neutral' as const, icon: '●', label: 'Ended' };

export type SwarmLifecyclePresentation = {
  /** One of the four stage names, or the idle wording. */
  headline: string;
  /** Terminal outcome, or the `plan_created` hand-off wording. */
  detail?: string;
  /** `replanning` and every later phase keep a visible "Plan adjusted" state. */
  planAdjusted: boolean;
  /** Drives live decoration only; a finished run is never live. */
  live: boolean;
  /**
   * The run reached a Finished stage: either a durable terminal run status or a
   * terminal lifecycle event. Cancellation and live decoration both stop here.
   */
  finished: boolean;
  /**
   * The canonical terminal outcome, present exactly when `finished`.
   *
   * This is the ONE value every terminal label in the card must use. The run
   * row can lag a terminal event by a poll or two — `run_completed` is written
   * before `runs.status` settles — so `run.status` is not a safe source for a
   * finished run's wording: it would read "finished with status running".
   * `viewModel.lifecycle` has already reconciled the two, with the durable
   * status winning whenever it is itself terminal.
   */
  outcome?: SwarmTerminalPhase;
  /** Human label for `outcome`, e.g. "Partial success". */
  outcomeLabel?: string;
  outcomeTone?: SwarmOutcomeTone;
  stages: SwarmStagePresentation[];
};

/**
 * Backend lifecycle phase -> user-facing stage.
 *
 * `plan_created` deliberately stays on Planning: the plan exists but no task
 * has been observed, so claiming execution has begun would be a fabrication.
 * Every terminal phase maps to Finished and keeps its exact outcome in
 * `detail`, so partial success, cancellation, timeout and budget exhaustion can
 * never be dressed up as a completed run.
 */
const LIFECYCLE_STAGE: Record<SwarmLifecyclePhase, SwarmStage | undefined> = {
  idle: undefined,
  planning: 'Planning',
  plan_created: 'Planning',
  replanning: 'Planning',
  running_tasks: 'Executing',
  verifying: 'Verifying',
  completed: 'Finished',
  partial_success: 'Finished',
  failed: 'Finished',
  cancelled: 'Finished',
  timed_out: 'Finished',
  budget_exhausted: 'Finished',
};

export const IDLE_HEADLINE = 'Waiting to start';
export const PLAN_CREATED_DETAIL = 'Planning complete · preparing execution';

/**
 * A run is finished when the durable run status is terminal, or when the
 * backend appended a terminal lifecycle event. Both are backend facts; the UI
 * never moves a run into a terminal state on its own.
 */
export function isSwarmRunFinished(viewModel: SwarmRunViewModel): boolean {
  return viewModel.terminal || LIFECYCLE_STAGE[viewModel.lifecycle] === 'Finished';
}

/** Positive evidence that a stage was actually entered. */
function stageReached(stage: SwarmStage, viewModel: SwarmRunViewModel, finished: boolean): boolean {
  switch (stage) {
    case 'Planning':
      return viewModel.plan.planCreated || viewModel.taskCounts.total > 0 || viewModel.verification.started;
    case 'Executing':
      return viewModel.taskCounts.total > 0;
    case 'Verifying':
      return viewModel.verification.started || viewModel.verification.completed;
    case 'Finished':
      return finished;
  }
}

export function describeSwarmLifecycle(viewModel: SwarmRunViewModel): SwarmLifecyclePresentation {
  const lifecycle = viewModel.lifecycle;
  const finished = isSwarmRunFinished(viewModel);
  const currentStage = LIFECYCLE_STAGE[lifecycle];
  const currentIndex = currentStage ? SWARM_STAGES.indexOf(currentStage) : -1;

  const outcome = finished && isTerminalPhase(lifecycle) ? lifecycle : undefined;
  const outcomeStyle = outcome ? TERMINAL_OUTCOME[outcome] : undefined;
  const succeeded = outcome === 'completed';

  const stages = SWARM_STAGES.map((stage, index): SwarmStagePresentation => {
    const reached = stageReached(stage, viewModel, finished);
    let state: SwarmStageState;
    if (finished) {
      // A finished run never animates and never claims a stage it skipped.
      state = stage === 'Finished' || reached ? 'complete' : 'pending';
    } else if (index === currentIndex) {
      state = lifecycle === 'plan_created' ? 'complete' : 'active';
    } else if (index < currentIndex) {
      state = reached ? 'complete' : 'pending';
    } else {
      // Replanning steps the position back to Planning while execution is
      // genuinely underway; the evidence wins over the position.
      state = reached ? 'active' : 'pending';
    }

    const current = index === currentIndex;

    // The Finished cell carries the exact outcome, in text and in shape.
    if (stage === 'Finished' && outcomeStyle && outcome) {
      return {
        stage,
        state,
        current,
        tone: outcomeStyle.tone,
        icon: outcomeStyle.icon,
        stateLabel: swarmLifecycleLabel(outcome),
      };
    }
    // An earlier stage of a run that did not fully succeed is over, not
    // successful: it keeps a neutral mark instead of a green checkmark.
    if (state === 'complete' && finished && !succeeded) {
      return { stage, state, current, ...ENDED_STAGE, stateLabel: ENDED_STAGE.label };
    }
    const style = STAGE_STATE_PRESENTATION[state];
    return { stage, state, current, tone: style.tone, icon: style.icon, stateLabel: style.label };
  });

  return {
    headline: currentStage ?? IDLE_HEADLINE,
    // The exact terminal outcome always travels with the Finished headline, so
    // partial success, cancellation, timeout and budget exhaustion can never
    // read as a plain success.
    detail: finished
      ? (outcome ? swarmLifecycleLabel(outcome) : viewModel.lifecycleLabel)
      : lifecycle === 'plan_created'
        ? PLAN_CREATED_DETAIL
        : undefined,
    planAdjusted: viewModel.plan.replanCount > 0,
    live: !finished && lifecycle !== 'idle',
    finished,
    outcome,
    outcomeLabel: outcome ? swarmLifecycleLabel(outcome) : undefined,
    outcomeTone: outcomeStyle?.tone,
    stages,
  };
}

export type SwarmCommanderPresentation = {
  /** Observable Commander activity. Never a rationale, never a prompt. */
  status: string;
  /** `revision N`, only when the backend actually reported a graph revision. */
  revisionLabel?: string;
  /** `N replans`, only once at least one replan was observed. */
  replanLabel?: string;
  /** Backend enum, shown as a subdued code with no invented explanation. */
  decisionCode?: string;
};

export function describeSwarmCommander(viewModel: SwarmRunViewModel): SwarmCommanderPresentation {
  const { plan, lifecycle } = viewModel;
  const status = plan.replanCount > 0
    ? 'Plan adjusted'
    : plan.planCreated
      ? 'Plan created'
      : lifecycle === 'planning'
        ? 'Planning task graph…'
        : 'No plan reported yet';

  return {
    status,
    revisionLabel: plan.graphRevision > 0 ? `revision ${plan.graphRevision}` : undefined,
    replanLabel: plan.replanCount > 0
      ? `${plan.replanCount} ${plan.replanCount === 1 ? 'replan' : 'replans'}`
      : undefined,
    decisionCode: plan.lastReplanDecision,
  };
}

/** Textual status plus a shape, so status never depends on colour alone. */
export const TASK_STATUS_PRESENTATION: Record<SwarmTaskStatus, { label: string; icon: string }> = {
  pending: { label: 'Pending', icon: '○' },
  ready: { label: 'Ready', icon: '◇' },
  running: { label: 'Running', icon: '◐' },
  completed: { label: 'Completed', icon: '✓' },
  failed: { label: 'Failed', icon: '✕' },
};

function groupDigits(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** Deterministic grouping, independent of the browser locale. */
export function formatCount(value: number): string {
  const rounded = Math.round(value);
  const grouped = groupDigits(String(Math.abs(rounded)));
  return rounded < 0 ? `-${grouped}` : grouped;
}

/**
 * Six decimals keep sub-cent model pricing exact (`$0.019178`); trailing zeros
 * collapse to two so a whole-cent total is not padded into false precision.
 */
export function formatCost(value: number): string {
  const negative = value < 0;
  const fixed = Math.abs(value).toFixed(6).replace(/(\.\d{2}\d*?)0+$/, '$1');
  const [whole, fraction] = fixed.split('.');
  return `${negative ? '-' : ''}$${groupDigits(whole)}.${fraction}`;
}

export const UNKNOWN_USAGE_VALUE = 'Not reported';

export type SwarmUsageEntry = {
  key: string;
  label: string;
  value: string;
  /** False renders the "unknown" treatment; it never renders a zero. */
  known: boolean;
};

/**
 * The compact usage strip.
 *
 * Logical tasks come from the task graph, every other number comes from
 * `run.usage` — the authoritative aggregate. They are separate quantities with
 * separate labels, so 5 tasks and 7 model calls stay 5 and 7.
 */
export function describeSwarmUsage(viewModel: SwarmRunViewModel): SwarmUsageEntry[] {
  const { usage, scale } = viewModel;
  const entries: SwarmUsageEntry[] = [
    {
      key: 'logical-tasks',
      label: 'Logical tasks',
      value: formatCount(scale.logicalTasks),
      known: true,
    },
    {
      key: 'model-calls',
      label: 'Model calls',
      value: usage.modelCalls === undefined ? UNKNOWN_USAGE_VALUE : formatCount(usage.modelCalls),
      known: usage.modelCalls !== undefined,
    },
    {
      key: 'tokens',
      label: 'Tokens',
      value: usage.totalTokens === undefined ? UNKNOWN_USAGE_VALUE : formatCount(usage.totalTokens),
      known: usage.totalTokens !== undefined,
    },
    {
      key: 'actual-cost',
      label: 'Actual cost',
      value: usage.actualCost === undefined ? UNKNOWN_USAGE_VALUE : formatCost(usage.actualCost),
      known: usage.actualCost !== undefined,
    },
  ];
  // Retries are only meaningful once the backend has reported them.
  if (usage.retries !== undefined) {
    entries.push({ key: 'retries', label: 'Retries', value: formatCount(usage.retries), known: true });
  }
  return entries;
}

export type SwarmVerificationEntry = { key: string; label: string; value?: string };

/**
 * Verification progress. Many verifier batches are progress within ONE
 * verification activity: they are never logical tasks and never agents.
 * Optional counts the backend did not send are omitted, not zeroed.
 */
export function describeSwarmVerification(viewModel: SwarmRunViewModel): SwarmVerificationEntry[] {
  const verification = viewModel.verification;
  const entries: SwarmVerificationEntry[] = [];
  if (verification.completed) entries.push({ key: 'completed', label: 'Verification completed' });
  else if (verification.started) entries.push({ key: 'started', label: 'Verification started' });
  if (verification.groundingResolved) entries.push({ key: 'grounding', label: 'Grounding resolved' });
  if (verification.completedBatches > 0) {
    entries.push({
      key: 'batches',
      label: 'Verifier batches',
      value: verification.batchCount === undefined
        ? formatCount(verification.completedBatches)
        : `${formatCount(verification.completedBatches)} of ${formatCount(verification.batchCount)}`,
    });
  }
  if (verification.claimCount !== undefined) {
    entries.push({ key: 'claims', label: 'Claims', value: formatCount(verification.claimCount) });
  }
  if (verification.groundingSourceCount !== undefined) {
    entries.push({ key: 'sources', label: 'Grounding sources', value: formatCount(verification.groundingSourceCount) });
  }
  if (verification.missingContextCount !== undefined) {
    entries.push({ key: 'missing', label: 'Missing context', value: formatCount(verification.missingContextCount) });
  }
  return entries;
}
