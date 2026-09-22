import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { LiveRunPanel } from '../components/run/LiveRunPanel';
import { LiveRunViewModel } from '../lib/liveRunViewModel';

function view(overrides: Partial<LiveRunViewModel> = {}): LiveRunViewModel {
  return {
    engine: 'swarm_v2',
    engineLabel: 'Swarm V2',
    engineVersion: 'swarm_v2.1',
    status: 'running',
    terminal: false,
    phaseLabel: 'Executing',
    work: undefined,
    active: [],
    evidence: {},
    provider: { pacingEventsObserved: 0 },
    usage: { present: false } as LiveRunViewModel['usage'],
    limits: {
      maxCost: 1,
      concurrency: {
        v1TechnicalParallelism: 4, v2MaxActiveWorkers: 8, providerMaxConcurrency: 2,
        providerOrganizationCeiling: 32, providerEffectiveConcurrency: 2,
        v1ProviderAdmitted: 2, v2ProviderAdmitted: 2,
        maxConcurrentRunsPerUser: 1, maxConcurrentRunsPerProject: 1,
        searchBasicQps: 1, searchProQps: 1, searchQpsVerified: false, paidPosture: false,
      },
    },
    spendRatio: undefined,
    finalization: { state: 'live' },
    ...overrides,
  } as LiveRunViewModel;
}

describe('LiveRunPanel effective concurrency', () => {
  it('distinguishes the logical engine width from the provider-admitted width, from server numbers', () => {
    render(<LiveRunPanel visible live={view()} connection="polling" />);
    const facts = screen.getByTestId('live-concurrency');
    expect(facts).toHaveTextContent('V2 active workers (logical)8');
    expect(facts).toHaveTextContent('V2 provider-admitted2');
    expect(facts).toHaveTextContent('Provider concurrency (this process)2 of 32 organization ceiling');
    expect(facts).toHaveTextContent('Concurrent runs per user / project1 / 1');
    expect(facts).toHaveTextContent('Search QPS basic / pro1 / 1 (unverified fallback)');
    // A V2 run does not show V1's width.
    expect(facts).not.toHaveTextContent('V1 technical parallelism');
  });

  it('shows V1 widths for a V1 run', () => {
    render(<LiveRunPanel visible live={view({ engine: 'vehicle_catalog_v1', engineLabel: 'Vehicle Catalog V1' })} connection="polling" />);
    const facts = screen.getByTestId('live-concurrency');
    expect(facts).toHaveTextContent('V1 technical parallelism (logical)4');
    expect(facts).toHaveTextContent('V1 provider-admitted2');
    expect(facts).not.toHaveTextContent('V2 active workers');
  });

  it('states plainly when the server resolved no concurrency', () => {
    render(<LiveRunPanel visible live={view({ limits: { maxCost: 1 } })} connection="polling" />);
    expect(screen.getByText('The server stated no resolvable concurrency for this deployment.')).toBeInTheDocument();
    expect(screen.queryByTestId('live-concurrency')).toBeNull();
  });
});
