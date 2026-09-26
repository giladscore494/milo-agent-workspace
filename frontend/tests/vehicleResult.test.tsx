/**
 * Phase 2: the vehicle-centric view in the Final result panel.
 *
 * `fixtures/swarmV2VehicleResult.json` is committed output of the REAL
 * `FinalBuilder` on the run 6825eb96 replay (`tests/replay_6825eb96.py`).
 * `tests/test_vehicle_result_frontend_fixture.py` fails if it ever drifts from
 * what the backend builds. The older fixtures carry no vehicle view at all,
 * which is what every `/1` and `/2` export written before this change holds.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FinalResultPanel } from '../components/result/FinalResultPanel';
import { FinalResult, parseFinalResult } from '../lib/finalResult';
import { ACCEPTED_EXPORT_SCHEMA_VERSIONS, EXPORT_SCHEMA_VERSION,
  parseExportEnvelope } from '../lib/runExport';
import fixtures from './fixtures/swarmV2FinalResult.json';
import vehicleFixtures from './fixtures/swarmV2VehicleResult.json';

const RUN_ID = 'c1c1c1c1-1111-4111-8111-000000000001';

function replay(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(vehicleFixtures.replay_6825eb96));
}

function ok(output: unknown, runStatus = 'partial_success'): FinalResult {
  const parsed = parseFinalResult(output, { runStatus });
  if (parsed.state !== 'result') {
    throw new Error(`expected a result, got ${parsed.state}` +
      (parsed.state === 'invalid' ? ` (${parsed.code})` : ''));
  }
  return parsed.result;
}

function invalidCode(output: unknown): string {
  const parsed = parseFinalResult(output, { runStatus: 'partial_success' });
  return parsed.state === 'invalid' ? parsed.code : parsed.state;
}

function renderPanel(output: Record<string, unknown>, runStatus = 'partial_success') {
  return render(
    <FinalResultPanel visible runId={RUN_ID} runStatus={runStatus} connection="terminal"
      output={output} />,
  );
}

function envelope(schemaVersion: string, result: unknown) {
  return {
    schema_version: schemaVersion, run_id: RUN_ID, engine: 'swarm_v2',
    run_identity: { run_id: RUN_ID, workflow_key: 'swarm_v2', release_sha: 'a'.repeat(40) },
    terminal_status: 'partial_success', result_kind: 'partial_result',
    generated_at: '2026-09-26T00:00:00+00:00',
    government_provenance: { source_ids: [], datasets: [], present: false },
    result, usage: {}, error: null,
  };
}

describe('1. parsing the vehicle view', () => {
  it('1a. the 6825eb96 replay parses into 8 vehicles and one ambiguous group', () => {
    const view = ok(replay()).vehicleResult!;
    expect(view.vehicles.map((vehicle) => vehicle.key)).toEqual(
      ['37096', '37098', '37254', '37291', '37293', '37316', '37363', '37425']);
    expect(view.vehiclesResolved).toBe(8);
    expect(view.unresolvedAmbiguous).toBe(1);
    const [group] = view.unresolvedGroups;
    expect(group.outcome).toBe('unresolved_ambiguous');
    expect(group.recordIds).toEqual(['37350', '37439']);
    expect(group.taskIds).toEqual(['t04', 't05']);
  });

  it('1b. each vehicle carries its own trim and code, with its verified field count', () => {
    const vehicles = ok(replay()).vehicleResult!.vehicles;
    const sr5 = vehicles.find((vehicle) => vehicle.key === '37254')!;
    expect(sr5.identity.trim).toBe('SR5');
    expect(sr5.identity.officialModelCode).toBe('TRN285L-GKTSKA');
    expect(sr5.verifiedFieldCount).toBe(5);
    expect(sr5.review).toEqual([]);
    expect(sr5.fields.find((field) => field.key === 'trim')?.provenance)
      .toEqual([{ claimId: 'claim-t02-3', sourceId: 'source-t02', taskId: 't02' }]);
  });

  it('1c. the existing keys parse exactly as they did', () => {
    const withView = ok(replay());
    const without = replay();
    delete without.vehicles;
    delete without.unresolved_groups;
    delete without.summary;
    const plain = ok(without);
    expect(plain.vehicleResult).toBeUndefined();
    expect({ ...withView, vehicleResult: undefined }).toEqual({ ...plain, vehicleResult: undefined });
  });

  it('1d. a partial, malformed or inconsistent vehicle view fails closed', () => {
    const cases: ((payload: Record<string, unknown>) => void)[] = [
      (p) => { delete p.summary; },
      (p) => { p.vehicles = {}; },
      (p) => { (p.summary as Record<string, number>).vehicles_resolved = 7; },
      (p) => { (p.summary as Record<string, number>).unresolved_ambiguous = 0; },
      (p) => { (p.vehicles as Record<string, unknown>[])[0].extra = 'model text'; },
      (p) => { (p.unresolved_groups as Record<string, unknown>[])[0].outcome = 'resolved'; },
      (p) => { (p.unresolved_groups as Record<string, unknown>[])[0].record_ids = []; },
      (p) => { (p.unresolved_groups as Record<string, unknown>[])[0].task_ids = []; },
      (p) => {
        const vehicle = (p.vehicles as Record<string, Record<string, Record<string, unknown>>>[])[0];
        vehicle.fields.trim.verdict = 'rejected';
      },
      (p) => {
        const vehicle = (p.vehicles as Record<string, Record<string, unknown>>[])[0];
        vehicle.identity.trim = { nested: true };
      },
      (p) => {
        const vehicle = (p.vehicles as Record<string, unknown[]>[])[0];
        vehicle.needs_review = [{ code: 'FIELD_REJECTED', reason: 'free text' }];
      },
    ];
    for (const mutate of cases) {
      const payload = replay();
      mutate(payload);
      expect(invalidCode(payload)).toBe('VEHICLES_INVALID');
    }
  });
});

describe('2. the Final result panel', () => {
  it('2a. lists each vehicle with an identity line, verified field count and review badge', () => {
    renderPanel(replay());
    const section = screen.getByRole('region', { name: 'Final result' });
    expect(within(section).getByRole('heading', { name: 'Vehicles (8)' })).toBeInTheDocument();
    expect(within(section).getByText('TOYOTA 4RUNNER 2026 · SR5 · TRN285L-GKTSKA')).toBeInTheDocument();
    expect(within(section).getByText('TOYOTA 4RUNNER 2026 · PLATINUM · TRN285L-GKTPKA'))
      .toBeInTheDocument();
    expect(within(section).getAllByText('5 verified fields')).toHaveLength(8);
    expect(within(section).getAllByText('No review items')).toHaveLength(8);
  });

  it('2b. the unresolved group is listed apart, with its records and tasks and no values', () => {
    renderPanel(replay());
    expect(screen.getByRole('heading', { name: 'Unresolved candidates (1)' })).toBeInTheDocument();
    expect(screen.getByText('Ambiguous — matches 2 register rows')).toBeInTheDocument();
    expect(screen.getByText('37350, 37439')).toBeInTheDocument();
    expect(screen.getByText('t04, t05')).toBeInTheDocument();
    expect(screen.getByText('TOYOTA 4RUNNER 2026 · LIMITED · TZNA55L-GKZSZA')).toBeInTheDocument();
  });

  it('2c. a vehicle with review items carries a text badge, not only a colour', () => {
    const payload = replay();
    const vehicles = payload.vehicles as Record<string, unknown>[];
    vehicles[0].needs_review = [{ code: 'FIELD_NEEDS_REVIEW', field: 'trim' }];
    (payload.summary as Record<string, number>).vehicles_with_review = 1;
    renderPanel(payload);
    expect(screen.getByText('Needs review (1)')).toBeInTheDocument();
  });

  it('2d. an old payload without the view renders exactly as before, with no vehicle section', () => {
    renderPanel(fixtures.partial_result as Record<string, unknown>);
    expect(screen.getByText('Partial result')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /^Vehicles/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Unresolved candidates/)).not.toBeInTheDocument();
  });

  it('2e. a malformed vehicle view makes the result unavailable, never half-rendered', () => {
    const payload = replay();
    (payload.summary as Record<string, number>).vehicles_resolved = 99;
    renderPanel(payload);
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('VEHICLES_INVALID')).toBeInTheDocument();
    expect(screen.queryByText(/verified fields$/)).not.toBeInTheDocument();
  });
});

describe('3. export compatibility (the envelope stays milo-run-export/2)', () => {
  it('3a. the version this release writes is still /2, and /1 is still read', () => {
    expect(EXPORT_SCHEMA_VERSION).toBe('milo-run-export/2');
    expect([...ACCEPTED_EXPORT_SCHEMA_VERSIONS].sort())
      .toEqual(['milo-run-export/1', 'milo-run-export/2']);
  });

  it.each(['milo-run-export/1', 'milo-run-export/2'])(
    '3b. a %s envelope written before this change parses, and its result has no vehicle view',
    (version) => {
      const doc = envelope(version, fixtures.partial_result);
      expect(parseExportEnvelope(doc)?.schemaVersion).toBe(version);
      expect(ok(doc.result).vehicleResult).toBeUndefined();
    });

  it('3c. a /2 envelope carrying the vehicle view parses, and so does its result', () => {
    const doc = envelope('milo-run-export/2', replay());
    expect(parseExportEnvelope(doc)?.schemaVersion).toBe('milo-run-export/2');
    expect(ok(doc.result).vehicleResult?.vehiclesResolved).toBe(8);
  });
});
