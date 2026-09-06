/**
 * Behavioural coverage for the Swarm V2 run card.
 *
 * Every view model here is produced by the real F1 pipeline — the shipped
 * reducer folded over the shipped fixture, then the shipped selector — so the
 * card is asserted against the same shape production hands it. There is no
 * second event-state implementation in this file.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SwarmRunCard } from '../components/swarm/SwarmRunCard';
import { formatCost, formatCount } from '../components/swarm/swarmPresentation';
import { reduceSwarmEvents } from '../lib/swarmReducer';
import { SwarmLifecyclePhase } from '../lib/swarmTypes';
import { SwarmRunViewModel, buildSwarmRunViewModel } from '../lib/swarmViewModel';
import { RunUsage } from '../lib/runUsage';
import { Run, RunEvent } from '../lib/types';
import {
  SMOKE_RUN_ID,
  SMOKE_TASK_IDS,
  SMOKE_USAGE,
  resetSmokeSequence,
  smokeEventStream,
  swarmEvent,
} from './swarmV2Fixture';

type ViewModelOptions = {
  events?: RunEvent[];
  status?: string;
  usage?: RunUsage | null;
  workflowKey?: string;
};

function viewModel(options: ViewModelOptions = {}): SwarmRunViewModel {
  const run: Run = {
    id: SMOKE_RUN_ID,
    conversation_id: 'ffffffff-1111-4111-8111-000000000001',
    status: options.status ?? 'running',
    usage: options.usage,
  };
  return buildSwarmRunViewModel({
    run,
    swarm: reduceSwarmEvents(options.events ?? []),
    workflowKey: options.workflowKey ?? 'swarm_v2',
  });
}

function renderCard(swarm: SwarmRunViewModel, overrides: Partial<React.ComponentProps<typeof SwarmRunCard>> = {}) {
  return render(
    <SwarmRunCard
      swarm={swarm}
      runId={SMOKE_RUN_ID}
      connection="polling"
      confirmingCancel={false}
      cancelReason=""
      cancelError=""
      onCancelReasonChange={() => {}}
      onRequestCancel={() => {}}
      onConfirmCancel={() => {}}
      onKeepRunning={() => {}}
      {...overrides}
    />,
  );
}

function taskRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.swarm-task'));
}

function taskStatuses(container: HTMLElement): string[] {
  return taskRows(container).map((row) => row.querySelector('.swarm-task-status-text')?.textContent ?? '');
}

function usageValue(container: HTMLElement, label: string): string | undefined {
  const item = Array.from(container.querySelectorAll('.swarm-usage-item')).find(
    (node) => node.querySelector('dt')?.textContent === label,
  );
  return item?.querySelector('dd')?.textContent ?? undefined;
}

/**
 * The headline names the stage the run is at, and the stage track lists that
 * same stage: the word appears twice on purpose, so these read the element
 * they mean instead of the first text match.
 */
function headlineText(container: HTMLElement): string | undefined {
  return container.querySelector('.swarm-headline-text')?.textContent ?? undefined;
}

function detailText(container: HTMLElement): string | undefined {
  return container.querySelector('.swarm-detail')?.textContent ?? undefined;
}

function stageState(container: HTMLElement, stage: string): string | undefined {
  const node = Array.from(container.querySelectorAll<HTMLElement>('.swarm-stage')).find(
    (candidate) => candidate.querySelector('.swarm-stage-name')?.textContent === stage,
  );
  return node?.dataset.state;
}

/** The plan plus two dependency-free tasks that genuinely start together. */
function concurrentStartEvents(): RunEvent[] {
  resetSmokeSequence();
  return [
    swarmEvent('run_created', {}),
    swarmEvent('commander_plan_created', { graph_revision: 1 }),
    swarmEvent('task_ready', { task_id: 'env_check' }),
    swarmEvent('task_ready', { task_id: 'list_catalog' }),
    swarmEvent('task_started', { task_id: 'env_check' }),
    swarmEvent('task_started', { task_id: 'list_catalog' }),
  ];
}

describe('1. successful Swarm V2 run with five logical tasks', () => {
  it('renders exactly the five logical tasks the run reported, all completed', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );

    expect(taskRows(container)).toHaveLength(5);
    for (const taskId of SMOKE_TASK_IDS) {
      expect(within(container).getByText(taskId)).toBeInTheDocument();
    }
    expect(taskStatuses(container)).toEqual(['Completed', 'Completed', 'Completed', 'Completed', 'Completed']);
    expect(usageValue(container, 'Logical tasks')).toBe('5');
    expect(headlineText(container)).toBe('Finished');
    expect(detailText(container)).toBe('Completed');
  });

  it('never invents a progress percentage and never leaks the raw event stream', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );
    const text = container.textContent ?? '';
    expect(text).not.toMatch(/%/);
    expect(text).not.toContain('commander_plan_created');
    expect(text).not.toContain('verification_batch_completed');
    expect(text).not.toContain('task_started');
    expect(container.querySelector('progress')).toBeNull();
  });

  it('reports verification separately from the logical tasks', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );
    expect(screen.getByText('Verification completed')).toBeInTheDocument();
    expect(screen.getByText('Grounding resolved')).toBeInTheDocument();
    const batches = Array.from(container.querySelectorAll('.swarm-fact')).map((node) => node.textContent);
    // Two verifier batches, and still five logical tasks.
    expect(batches).toContain('Verifier batches2 of 2');
    expect(batches).toContain('Missing context0');
    expect(taskRows(container)).toHaveLength(5);
  });
});

describe('2. concurrent logical tasks', () => {
  it('shows two tasks running at the same time', () => {
    const { container } = renderCard(viewModel({ events: concurrentStartEvents() }));
    expect(taskStatuses(container)).toEqual(['Running', 'Running']);
    expect(screen.getByText('2 tasks running concurrently')).toBeInTheDocument();
    expect(headlineText(container)).toBe('Executing');
  });

  it('does not imply a single worker slot: both rows stay independent', () => {
    const { container } = renderCard(viewModel({ events: concurrentStartEvents() }));
    const running = taskRows(container).filter((row) => row.dataset.status === 'running');
    expect(running).toHaveLength(2);
    expect(running.map((row) => row.querySelector('.identifier')?.textContent)).toEqual(['env_check', 'list_catalog']);
  });
});

describe('3. replanning with graph revision 2', () => {
  function replanEvents(): RunEvent[] {
    resetSmokeSequence();
    return [
      swarmEvent('run_created', {}),
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_ready', { task_id: 'env_check' }),
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('task_completed', { task_id: 'env_check', status: 'completed' }),
      swarmEvent('commander_replanned', { graph_revision: 2, decision: 'ADD_TASKS' }),
    ];
  }

  it('shows a visible plan-adjusted state with the backend revision', () => {
    const { container } = renderCard(viewModel({ events: replanEvents() }));
    expect(headlineText(container)).toBe('Planning');
    // The stage keeps its own visible "Plan adjusted" chip, beside the fuller
    // Commander line.
    expect(container.querySelector('.swarm-adjusted')?.textContent).toBe('Plan adjusted');
    const commander = container.querySelector('.swarm-commander');
    expect(commander?.textContent).toContain('Plan adjusted');
    expect(commander?.textContent).toContain('revision 2');
    expect(commander?.textContent).toContain('1 replan');
  });

  it('shows the replan decision as a subdued backend code with no invented explanation', () => {
    const { container } = renderCard(viewModel({ events: replanEvents() }));
    const decision = container.querySelector('.swarm-decision');
    expect(decision?.textContent).toContain('ADD_TASKS');
    expect(decision?.querySelector('.identifier')?.textContent).toBe('ADD_TASKS');
    expect(container.textContent).not.toMatch(/because/i);
  });

  it('keeps the already-executed work visible while the plan is being adjusted', () => {
    const { container } = renderCard(viewModel({ events: replanEvents() }));
    expect(taskStatuses(container)).toEqual(['Completed']);
    expect(stageState(container, 'Planning')).toBe('active');
    expect(stageState(container, 'Executing')).toBe('active');
  });
});

describe('4. failed logical task with a safe failure code', () => {
  it('renders the code as-is and marks only that task failed', () => {
    resetSmokeSequence();
    const events = [
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('task_completed', { task_id: 'env_check', status: 'completed' }),
      swarmEvent('task_started', { task_id: 'get_details' }),
      swarmEvent('task_failed', { task_id: 'get_details', code: 'WORKER_OUTPUT_INVALID' }),
    ];
    const { container } = renderCard(viewModel({ events }));
    expect(taskStatuses(container)).toEqual(['Completed', 'Failed']);
    expect(screen.getByText('Failure code WORKER_OUTPUT_INVALID')).toBeInTheDocument();
    const failed = taskRows(container).filter((row) => row.dataset.status === 'failed');
    expect(failed).toHaveLength(1);
  });
});

describe('5-9. terminal outcomes stay distinguishable', () => {
  const TERMINAL_CASES: Array<[string, string]> = [
    ['completed', 'Completed'],
    ['partial_success', 'Partial success'],
    ['failed', 'Failed'],
    ['cancelled', 'Cancelled'],
    ['timed_out', 'Timed out'],
    ['budget_exhausted', 'Budget exhausted'],
  ];

  it.each(TERMINAL_CASES)('renders %s as Finished with its exact outcome', (status, label) => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status, usage: SMOKE_USAGE }),
    );
    expect(headlineText(container)).toBe('Finished');
    expect(detailText(container)).toBe(label);
    expect(screen.getByText(/Run finished with status/)).toHaveTextContent(status);
  });

  it.each(TERMINAL_CASES)('stops every live affordance for %s', (status) => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status, usage: SMOKE_USAGE }),
    );
    // No pulsing element survives a terminal run, and the card says so in data.
    expect(container.querySelector('.swarm-pulse')).toBeNull();
    expect(container.querySelector('.swarm-card')?.getAttribute('data-live')).toBe('false');
    expect(stageState(container, 'Finished')).toBe('complete');
    // The final task graph, verification and usage all survive.
    expect(taskRows(container)).toHaveLength(5);
    expect(screen.getByText('Verification completed')).toBeInTheDocument();
    expect(usageValue(container, 'Model calls')).toBe('7');
  });

  it.each(TERMINAL_CASES.filter(([status]) => status !== 'completed'))(
    '%s never reads as a successful run',
    (status, label) => {
      const { container } = renderCard(viewModel({ events: smokeEventStream(), status, usage: SMOKE_USAGE }));
      expect(container.querySelector('.swarm-detail')?.textContent).toBe(label);
      expect(container.querySelector('.swarm-detail')?.textContent).not.toBe('Completed');
    },
  );

  it('spells out that partial success is not a completed run', () => {
    renderCard(viewModel({ events: smokeEventStream(), status: 'partial_success', usage: SMOKE_USAGE }));
    expect(screen.getByText(/Partial success is not a completed run/)).toBeInTheDocument();
  });

  it('does not treat a cancellation request as a cancelled run', () => {
    const { container } = renderCard(
      viewModel({ events: concurrentStartEvents(), status: 'cancellation_requested' }),
    );
    expect(container.querySelector('.swarm-detail')).toBeNull();
    expect(screen.queryByText('Cancelled')).not.toBeInTheDocument();
    expect(screen.getByText(/Cancellation requested\./)).toBeInTheDocument();
    expect(container.querySelector('.swarm-card')?.getAttribute('data-live')).toBe('true');
  });
});

describe('10. reconnecting', () => {
  it('shows a compact reconnecting state without dropping known task rows', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), usage: SMOKE_USAGE }),
      { connection: 'reconnecting' },
    );
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(taskRows(container)).toHaveLength(5);
    expect(usageValue(container, 'Model calls')).toBe('7');
    expect(screen.getByText('Verification completed')).toBeInTheDocument();
  });

  it('stays visually silent while polling normally', () => {
    renderCard(viewModel({ events: smokeEventStream(), usage: SMOKE_USAGE }), { connection: 'polling' });
    expect(screen.queryByText('Reconnecting…')).not.toBeInTheDocument();
    expect(screen.queryByText(/polling/i)).not.toBeInTheDocument();
  });
});

describe('11. missing and partial usage', () => {
  it('reports absent usage as unknown rather than as zero', () => {
    const { container } = renderCard(viewModel({ events: smokeEventStream(), usage: null }));
    expect(usageValue(container, 'Model calls')).toBe('Not reported');
    expect(usageValue(container, 'Tokens')).toBe('Not reported');
    expect(usageValue(container, 'Actual cost')).toBe('Not reported');
    // Logical tasks come from the task graph, not from usage, so they stay known.
    expect(usageValue(container, 'Logical tasks')).toBe('5');
    // Retries are omitted entirely rather than invented.
    expect(usageValue(container, 'Retries')).toBeUndefined();
    expect(container.textContent).not.toContain('$0.00');
  });

  it('renders only the usage fields the backend actually reported', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), usage: { model_calls: 7, retries: 0 } }),
    );
    expect(usageValue(container, 'Model calls')).toBe('7');
    expect(usageValue(container, 'Retries')).toBe('0');
    expect(usageValue(container, 'Tokens')).toBe('Not reported');
    expect(usageValue(container, 'Actual cost')).toBe('Not reported');
  });

  it('formats the accepted smoke aggregate deterministically', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );
    expect(usageValue(container, 'Tokens')).toBe('10,370');
    expect(usageValue(container, 'Actual cost')).toBe('$0.019178');
    expect(usageValue(container, 'Retries')).toBe('0');
    expect(formatCount(10_370)).toBe('10,370');
    expect(formatCost(0.019178)).toBe('$0.019178');
    expect(formatCost(12.5)).toBe('$12.50');
    expect(formatCost(0)).toBe('$0.00');
  });
});

describe('12. model calls are not agents and not tasks', () => {
  it('keeps 5 logical tasks and 7 model calls as separate, separately labelled quantities', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );
    expect(taskRows(container)).toHaveLength(5);
    expect(usageValue(container, 'Logical tasks')).toBe('5');
    expect(usageValue(container, 'Model calls')).toBe('7');

    const text = container.textContent ?? '';
    expect(text).not.toMatch(/7 agents/i);
    expect(text).not.toMatch(/7 tasks/i);
    expect(text).not.toMatch(/7 logical tasks/i);
    expect(text).not.toMatch(/\d+\s+agents? (?:running|active|completed)/i);
    // The only place "agents" may appear is the note that says they are not.
    expect(screen.getByText(/not logical tasks and not agents/)).toBeInTheDocument();
  });

  it('does not count verifier batches or repairs as logical tasks', () => {
    const { container } = renderCard(
      viewModel({ events: smokeEventStream(), status: 'completed', usage: SMOKE_USAGE }),
    );
    // Two verifier batches and one bounded worker repair, still five tasks.
    expect(taskRows(container)).toHaveLength(5);
    const repaired = taskRows(container).find((row) => row.querySelector('.identifier')?.textContent === 'get_details');
    expect(repaired?.querySelector('.swarm-task-counts')?.textContent).toContain('1 repair');
    expect(screen.getByText(/never extra tasks/)).toBeInTheDocument();
    expect(screen.getByText(/not separate tasks/)).toBeInTheDocument();
  });
});

describe('lifecycle stage mapping', () => {
  function lifecycleCard(events: RunEvent[], status = 'running') {
    return renderCard(viewModel({ events, status }));
  }

  it('idle waits to start', () => {
    const { container } = lifecycleCard([], 'queued');
    expect(headlineText(container)).toBe('Waiting to start');
    expect(stageState(container, 'Planning')).toBe('pending');
    expect(container.querySelector('.swarm-pulse')).toBeNull();
    expect(screen.getByText('No logical tasks have been reported yet.')).toBeInTheDocument();
  });

  it('planning is live before a plan exists', () => {
    resetSmokeSequence();
    const { container } = lifecycleCard([swarmEvent('run_started', {})]);
    expect(headlineText(container)).toBe('Planning');
    expect(stageState(container, 'Planning')).toBe('active');
    expect(container.querySelector('.swarm-commander')?.textContent).toBe('Planning task graph…');
    expect(container.querySelector('.swarm-pulse')).not.toBeNull();
  });

  it('plan_created reads as planning complete and preparing execution', () => {
    resetSmokeSequence();
    const { container } = lifecycleCard([
      swarmEvent('run_started', {}),
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
    ]);
    expect(headlineText(container)).toBe('Planning');
    expect(detailText(container)).toBe('Planning complete · preparing execution');
    expect(stageState(container, 'Planning')).toBe('complete');
    expect(stageState(container, 'Executing')).toBe('pending');
    expect(container.querySelector('.swarm-commander')?.textContent).toContain('Plan created');
  });

  it('running_tasks reads as executing', () => {
    const { container } = lifecycleCard(concurrentStartEvents());
    expect(headlineText(container)).toBe('Executing');
    expect(stageState(container, 'Executing')).toBe('active');
    expect(stageState(container, 'Planning')).toBe('complete');
  });

  it('verifying is its own stage after execution', () => {
    resetSmokeSequence();
    const { container } = lifecycleCard([
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('task_completed', { task_id: 'env_check', status: 'completed' }),
      swarmEvent('grounding_context_resolved', { claim_count: 2, source_count: 2, missing_context_count: 0 }),
    ]);
    expect(headlineText(container)).toBe('Verifying');
    expect(stageState(container, 'Executing')).toBe('complete');
    expect(stageState(container, 'Verifying')).toBe('active');
    expect(screen.getByText('Verification started')).toBeInTheDocument();
  });

  it('exposes exactly the four presentation stages, in order', () => {
    const { container } = lifecycleCard(concurrentStartEvents());
    const names = Array.from(container.querySelectorAll('.swarm-stage-name')).map((node) => node.textContent);
    expect(names).toEqual(['Planning', 'Executing', 'Verifying', 'Finished']);
  });

  it('announces lifecycle politely and nothing else', () => {
    const { container } = lifecycleCard(smokeEventStream(), 'completed');
    const live = container.querySelectorAll('[aria-live]');
    expect(live).toHaveLength(1);
    expect(live[0].getAttribute('aria-live')).toBe('polite');
    // Task rows and counters are deliberately outside the announced region.
    expect(live[0].textContent).toBe('FinishedCompleted');
  });

  it('maps every backend lifecycle phase to a stage without throwing', () => {
    const phases: SwarmLifecyclePhase[] = [
      'idle', 'planning', 'plan_created', 'running_tasks', 'replanning', 'verifying',
      'completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted',
    ];
    for (const phase of phases) {
      const { container, unmount } = renderCard({ ...viewModel(), lifecycle: phase });
      expect(container.querySelectorAll('.swarm-stage')).toHaveLength(4);
      unmount();
    }
  });
});

describe('15. cancellation', () => {
  it('offers cancellation while the run is active', () => {
    const onRequestCancel = vi.fn();
    renderCard(viewModel({ events: concurrentStartEvents() }), { onRequestCancel });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
    expect(onRequestCancel).toHaveBeenCalledTimes(1);
  });

  it('keeps the existing confirm-with-optional-reason interaction', () => {
    const onConfirmCancel = vi.fn();
    const onKeepRunning = vi.fn();
    renderCard(viewModel({ events: concurrentStartEvents() }), {
      confirmingCancel: true,
      onConfirmCancel,
      onKeepRunning,
    });
    expect(screen.getByLabelText('Cancellation reason')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancellation' }));
    expect(onConfirmCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Keep running' }));
    expect(onKeepRunning).toHaveBeenCalledTimes(1);
  });

  it.each(['completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted'])(
    'withdraws cancellation once the run reached %s',
    (status) => {
      renderCard(viewModel({ events: smokeEventStream(), status, usage: SMOKE_USAGE }), {
        confirmingCancel: true,
      });
      expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Confirm cancellation' })).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Cancellation reason')).not.toBeInTheDocument();
    },
  );

  it('surfaces a cancellation error without changing the run state', () => {
    const { container } = renderCard(viewModel({ events: concurrentStartEvents() }), {
      cancelError: 'cancellation is disabled (EXECUTION_SURFACE_DISABLED)',
    });
    expect(screen.getByRole('alert')).toHaveTextContent('EXECUTION_SURFACE_DISABLED');
    expect(container.querySelector('.swarm-detail')).toBeNull();
  });
});

describe('secondary run detail', () => {
  it('keeps run identity, launch state and reconciliation subordinate to execution', () => {
    const { container } = renderCard(viewModel({ events: concurrentStartEvents() }), {
      launchState: 'launch_unknown',
      launchReconciliationRequired: true,
    });
    const foot = container.querySelector('.swarm-card-foot');
    expect(foot?.textContent).toContain(SMOKE_RUN_ID);
    expect(foot?.textContent).toContain('launch_unknown');
    expect(foot?.textContent).toContain('reconciliation required');
    // The headline still belongs to the lifecycle, not to the launcher.
    expect(container.querySelector('.swarm-headline-text')?.textContent).toBe('Executing');
  });

  it('escapes backend-supplied text rather than rendering markup', () => {
    resetSmokeSequence();
    const { container } = renderCard(
      viewModel({ events: [swarmEvent('task_failed', { task_id: '<img src=x>', code: '<script>' })] }),
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('‹img src=x›');
  });
});

/* ---------------------------------------------------------------------- */
/* 13/14. Workflow routing at the page level.                              */
/* ---------------------------------------------------------------------- */

let mockSession: any = { access_token: 'fresh', user: { email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  api: {
    projects: vi.fn(),
    conversations: vi.fn(),
    createConversation: vi.fn(),
    createProposal: vi.fn(),
    decideProposal: vi.fn(),
    reviseProposal: vi.fn(),
    startRun: vi.fn(),
    run: vi.fn(),
    events: vi.fn(),
    cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => true,
  newIdempotencyKey: () => 'swarm-card-test-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) {
      super(message);
    }
  },
}));

const V1_PROJECT = { id: 'bbbbbbbb-1111-4111-8111-000000000001', slug: 'vehicle-catalog', name: 'Vehicle Catalog', workflow_key: 'vehicle_catalog_v1' };
const V2_PROJECT = { id: 'bbbbbbbb-1111-4111-8111-000000000002', slug: 'swarm', name: 'Swarm Project', workflow_key: 'swarm_v2' };
const CONVERSATION = { id: 'cccccccc-1111-4111-8111-000000000001', project_id: V2_PROJECT.id, title: 'Kickoff' };

describe('13/14. Swarm V1 keeps the existing run panel', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { email: 'u@example.com' } };
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, SMOKE_RUN_ID);
  });

  async function openProject(project: typeof V1_PROJECT) {
    const { default: Page } = await import('../app/page');
    apiMocks.api.projects.mockResolvedValue([project]);
    const view = render(<Page />);
    fireEvent.click(await screen.findByText(project.name));
    fireEvent.click(await screen.findByText('Kickoff'));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(SMOKE_RUN_ID));
    return view;
  }

  it('13. a Swarm V1 project still renders CurrentRunPanel', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running' });
    await openProject(V1_PROJECT);
    expect(await screen.findByText('Live run')).toBeInTheDocument();
    // The V1 panel's own facts grid is what is on screen.
    expect(screen.getByText('Phase')).toBeInTheDocument();
    expect(screen.getByText('Connection')).toBeInTheDocument();
  });

  it('14. a Swarm V1 project never renders SwarmRunCard', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running' });
    const { container } = await openProject(V1_PROJECT);
    expect(screen.queryByText('Swarm run')).not.toBeInTheDocument();
    expect(container.querySelector('.swarm-card')).toBeNull();
    expect(container.querySelectorAll('.swarm-stage')).toHaveLength(0);
  });

  it('a Swarm V2 project renders exactly one run card and drops the V1 panel', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running', usage: SMOKE_USAGE });
    apiMocks.api.events.mockResolvedValue(smokeEventStream());
    const { container } = await openProject(V2_PROJECT);
    expect(await screen.findByText('Swarm run')).toBeInTheDocument();
    expect(container.querySelectorAll('.swarm-card')).toHaveLength(1);
    expect(screen.queryByText('Live run')).not.toBeInTheDocument();
    await waitFor(() => expect(taskRows(container)).toHaveLength(5));
    expect(usageValue(container, 'Model calls')).toBe('7');
  });

  it('keeps the raw event stream in the inspector, not in the run card', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running', usage: SMOKE_USAGE });
    apiMocks.api.events.mockResolvedValue(smokeEventStream());
    const { container } = await openProject(V2_PROJECT);
    await screen.findByText('Swarm run');
    expect(screen.getByText('Live event stream')).toBeInTheDocument();
    await waitFor(() => expect(container.querySelectorAll('.event').length).toBeGreaterThan(0));
    expect(container.querySelector('.swarm-card')?.textContent).not.toContain('task_completed');
  });

  it('says the legacy agent view does not apply to Swarm V2', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running', usage: SMOKE_USAGE });
    apiMocks.api.events.mockResolvedValue(smokeEventStream());
    await openProject(V2_PROJECT);
    expect(await screen.findByText(/Not applicable to Swarm V2/)).toBeInTheDocument();
    expect(screen.queryByText('No agents are running.')).not.toBeInTheDocument();
  });

  it('15. cancels a live Swarm V2 run and withdraws the control once terminal', async () => {
    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'running', usage: SMOKE_USAGE });
    apiMocks.api.events.mockResolvedValue(smokeEventStream().slice(0, 6));
    apiMocks.api.cancel.mockResolvedValue({ run_id: SMOKE_RUN_ID, status: 'cancellation_requested' });
    const { unmount } = await openProject(V2_PROJECT);
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'wrong task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancellation' }));
    await waitFor(() => expect(apiMocks.api.cancel).toHaveBeenCalledWith(SMOKE_RUN_ID, 'wrong task'));
    unmount();

    apiMocks.api.run.mockResolvedValue({ id: SMOKE_RUN_ID, conversation_id: CONVERSATION.id, status: 'completed', usage: SMOKE_USAGE });
    await openProject(V2_PROJECT);
    expect(await screen.findByText(/Run finished with status/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
  });
});
