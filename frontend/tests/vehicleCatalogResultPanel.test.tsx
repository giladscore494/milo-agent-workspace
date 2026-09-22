import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LiveRunPanel } from '../components/run/LiveRunPanel';
import { VehicleCatalogResultPanel } from '../components/result/VehicleCatalogResultPanel';
import { buildLiveRunViewModel } from '../lib/liveRunViewModel';
import { parseProductOutcome } from '../lib/productOutcome';
import { reconstructRun } from '../lib/runReducer';
import { buildSwarmRunViewModel } from '../lib/swarmViewModel';
import { identityFor } from './fixtures/runIdentity';

const RUN_ID = '2c9e2c11-58c8-4b46-b7d5-3d8de9f4b7aa';
const OUTPUT = {
  status: 'partial_success',
  summary: 'Two models were catalogued.',
  result: {
    manufacturer: 'Alpha', market: 'IL', period: '2019-2024', status: 'partial_success',
    models: [
      { canonical_model_name: 'Alpha One', model_name_he: 'אלפא 1', verification_status: 'verified', fuel_type: 'petrol', power_hp: 150, sources: ['https://example.com/a'] },
      { canonical_model_name: 'Alpha Two', verification_status: 'needs_review', api_key: 'sk-should-never-render' },
    ],
    needs_review: [{ n: 1 }], rejected: [], failed_agents: [],
    pipeline_quality: { discovery: 'success', normalizer: 'success', technical_enrichment: 'partial', verifier: 'success', final_builder: 'success', data_depth: 'partial_technical' },
  },
};
const OUTCOME = parseProductOutcome({
  engine: 'vehicle_catalog_v1', semantic_status: 'partial', usability: 'partial', result_kind: 'partial_result',
  coverage: { produced: 1, outstanding: 1, ratio: 0.5 }, blocking: [{ code: 'OUTSTANDING_REVIEW_ITEMS', count: 1 }], payload: { present: true, byte_size: 900 },
});

describe('the typed Vehicle Catalog V1 result surface', () => {
  it('renders models, verdicts, quality and the canonical verdict — never a JSON dump', () => {
    render(<VehicleCatalogResultPanel visible runId={RUN_ID} runStatus="partial_success" connection="terminal" output={OUTPUT} outcome={OUTCOME} />);
    expect(screen.getByRole('heading', { name: 'Final result' })).toBeInTheDocument();
    expect(screen.getByText('Vehicle Catalog V1 product result')).toBeInTheDocument();
    expect(screen.getByText('Canonical verdict: Partial')).toBeInTheDocument();
    expect(screen.getByText('OUTSTANDING_REVIEW_ITEMS')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Models (2)' })).toBeInTheDocument();
    expect(screen.getByText('Alpha One')).toBeInTheDocument();
    expect(screen.getByText('אלפא 1')).toBeInTheDocument();
    expect(screen.getByText('Needs review')).toBeInTheDocument();
    expect(screen.getByText('petrol')).toBeInTheDocument();
    expect(screen.getByText('Partial technical detail')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'example.com' })).toHaveAttribute('href', 'https://example.com/a');
    expect(document.body.textContent).not.toContain('sk-should-never-render');
    expect(document.body.querySelector('pre')).toBeNull();
  });

  it('refuses a payload that is not a catalog document with a static code', () => {
    render(<VehicleCatalogResultPanel visible runId={RUN_ID} runStatus="completed" connection="terminal" output={{ summary: 'E2E mocked output', artifacts: {} }} outcome={undefined} />);
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('V1_NOT_A_CATALOG_DOCUMENT')).toBeInTheDocument();
    expect(screen.getByText('No canonical verdict recorded')).toBeInTheDocument();
    expect(screen.queryByText('E2E mocked output')).not.toBeInTheDocument();
  });

  it('says not finished while the run is live and nothing before the row loads', () => {
    const { rerender } = render(<VehicleCatalogResultPanel visible runId={RUN_ID} runStatus={undefined} connection="polling" />);
    expect(screen.getByText('Loading')).toBeInTheDocument();
    rerender(<VehicleCatalogResultPanel visible runId={RUN_ID} runStatus="running" connection="polling" />);
    expect(screen.getByText('Not finished')).toBeInTheDocument();
    expect(screen.queryByText(/Canonical verdict/)).not.toBeInTheDocument();
  });

  it('renders nothing when the caller has not selected it from the run identity', () => {
    const { container } = render(<VehicleCatalogResultPanel visible={false} runId={RUN_ID} runStatus="completed" connection="terminal" output={OUTPUT} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('the live execution panel', () => {
  it('states engine, phase, work, active workers, evidence, budget and finalization from durable facts', () => {
    const run = {
      id: RUN_ID, conversation_id: 'c', status: 'running', run_identity: identityFor('vehicle_catalog_v1', RUN_ID),
      usage: { model_calls: 12, actual_cost: 0.1, total_tokens: 40_000 },
      limits: { max_model_calls_per_run: 150, max_cost_per_run: 1, max_total_tokens_per_run: 600_000, max_run_duration_seconds: 1800 },
    } as any;
    const events = [
      { id: '1', run_id: RUN_ID, event_type: 'phase_started', phase: 'verification' },
      { id: '2', run_id: RUN_ID, event_type: 'agent_started', agent: 'source_verifier', message: 'Agent task started: source_verifier/verification', phase: 'verification' },
      { id: '3', run_id: RUN_ID, event_type: 'chunk_started', agent: 'source_verifier' },
      { id: '4', run_id: RUN_ID, event_type: 'source_recorded', payload: { id: 's1', title: 't', domain: 'd' } },
    ] as any[];
    const state = reconstructRun(run, events);
    const swarm = buildSwarmRunViewModel({ run, swarm: state.swarm, workflowKey: 'vehicle_catalog_v1' });
    const live = buildLiveRunViewModel({ runId: RUN_ID, state, swarm });
    render(<LiveRunPanel visible live={live} connection="polling" />);
    const region = screen.getByRole('region', { name: 'Live execution' });
    expect(region).toHaveTextContent('Vehicle Catalog V1');
    expect(region).toHaveTextContent('running · live');
    expect(region).toHaveTextContent('Verification');
    expect(region).toHaveTextContent('1 chunks · 0 queued · 1 active · 0 completed · 0 failed');
    expect(region).toHaveTextContent('source_verifier');
    expect(region).toHaveTextContent('12 / 150');
    expect(region).toHaveTextContent('$0.1000 / $1.00');
    expect(region).toHaveTextContent('Spend at 10% of the run\'s cost ceiling.');
    expect(region).toHaveTextContent('not yet finalized');
    // Nothing infrastructural: no lease, key or worker identity.
    expect(region.textContent).not.toMatch(/lease|service-account|Bearer|sk-/i);
  });

  it('never invents a quantity the backend has not stated', () => {
    const run = { id: RUN_ID, conversation_id: 'c', status: 'queued', run_identity: identityFor('swarm_v2', RUN_ID) } as any;
    const state = reconstructRun(run, []);
    const swarm = buildSwarmRunViewModel({ run, swarm: state.swarm, workflowKey: 'swarm_v2' });
    render(<LiveRunPanel visible live={buildLiveRunViewModel({ runId: RUN_ID, state, swarm })} connection="polling" />);
    const region = screen.getByRole('region', { name: 'Live execution' });
    expect(region).toHaveTextContent('Swarm V2');
    expect(region).toHaveTextContent('No usage has been recorded for this run yet.');
    expect(region).toHaveTextContent('not reported');
  });
});
