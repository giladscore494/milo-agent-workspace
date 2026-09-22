import { describe, expect, it } from 'vitest';
import { MAX_V1_MODELS, parseVehicleCatalogResult } from '../lib/vehicleCatalogResult';

const DOCUMENT = {
  manufacturer: 'Alpha', market: 'IL', period: '2019-2024', status: 'partial_success',
  models: [
    { canonical_model_name: 'Alpha One', model_name_he: 'אלפא 1', verification_status: 'verified', confidence: 'high',
      years: '2021-2024', fuel_type: 'petrol', power_hp: 150, sources: ['https://example.com/a', 'not a url'],
      secret_field: 'must not render' },
    { canonical_model_name: 'Alpha Two', verification_status: 'needs_review', trims: ['Base', 'Sport'] },
    { canonical_model_name: 'Alpha Three', verification_status: 'unheard_of' },
  ],
  needs_review: [{ name: 'Alpha Two' }, { name: 'Alpha Four (view only)' }],
  rejected: [],
  failed_agents: [{ agent: 'dimensions_safety_equipment_agent' }],
  pipeline_quality: { discovery: 'success', normalizer: 'success', technical_enrichment: 'partial', verifier: 'success', final_builder: 'success', data_depth: 'partial_technical', bogus: 'ignored' },
};

describe('the typed vehicle_catalog_v1 result reader', () => {
  it('reads the run envelope and the bare document alike', () => {
    const fromEnvelope = parseVehicleCatalogResult({ status: 'partial_success', result: DOCUMENT, summary: 'two models' });
    const fromDocument = parseVehicleCatalogResult(DOCUMENT);
    expect(fromEnvelope.state).toBe('result');
    expect(fromDocument.state).toBe('result');
    if (fromEnvelope.state !== 'result' || fromDocument.state !== 'result') throw new Error('unreachable');
    expect(fromEnvelope.result.summary).toBe('two models');
    expect(fromDocument.result.summary).toBeUndefined();
    expect(fromEnvelope.result.models.map((m) => m.name)).toEqual(['Alpha One', 'Alpha Two', 'Alpha Three']);
  });

  it('renders only the closed field list, bounded text and linkable sources', () => {
    const parsed = parseVehicleCatalogResult(DOCUMENT);
    if (parsed.state !== 'result') throw new Error('expected a result');
    const one = parsed.result.models[0];
    expect(one.nameHe).toBe('אלפא 1');
    expect(one.fields.map((f) => f.key)).toEqual(['years', 'fuel_type', 'power_hp']);
    expect(one.fields.find((f) => f.key === 'power_hp')?.value).toBe('150');
    expect(JSON.stringify(parsed)).not.toContain('must not render');
    expect(one.sources).toEqual([{ url: 'https://example.com/a', host: 'example.com' }]);
    expect(parsed.result.models[1].fields.find((f) => f.key === 'trims')?.value).toBe('Base, Sport');
  });

  it('counts verdicts the way the backend outcome reader does', () => {
    const parsed = parseVehicleCatalogResult(DOCUMENT);
    if (parsed.state !== 'result') throw new Error('expected a result');
    // An unknown verdict is read DOWNWARDS as partial, never as verified.
    expect(parsed.result.models[2].verification).toBe('partial');
    expect(parsed.result.counts.verified).toBe(1);
    // The needs_review VIEW is larger than the per-model count; the larger wins.
    expect(parsed.result.counts.needsReview).toBe(2);
    expect(parsed.result.counts.rejected).toBe(0);
    expect(parsed.result.counts.failedAgents).toBe(1);
    expect(parsed.result.quality.map((q) => `${q.stage}=${q.status}`)).toEqual([
      'discovery=success', 'normalizer=success', 'technical_enrichment=partial', 'verifier=success', 'final_builder=success',
    ]);
    expect(parsed.result.dataDepth).toBe('partial_technical');
    expect(parsed.result.declaredStatus).toBe('partial_success');
  });

  it('is absent for an empty payload and invalid for anything that is not a catalog document', () => {
    expect(parseVehicleCatalogResult(undefined)).toEqual({ state: 'absent' });
    expect(parseVehicleCatalogResult({})).toEqual({ state: 'absent' });
    expect(parseVehicleCatalogResult({ summary: 'E2E mocked output', artifacts: {} })).toEqual({ state: 'invalid', code: 'V1_NOT_A_CATALOG_DOCUMENT' });
    // A valid Swarm V2 payload is not a catalog document either.
    expect(parseVehicleCatalogResult({ status: 'success', result_kind: 'usable_result', fields: {}, needs_review: [] })).toEqual({ state: 'invalid', code: 'V1_NOT_A_CATALOG_DOCUMENT' });
    expect(parseVehicleCatalogResult({ ...DOCUMENT, models: [{ no_name: true }] })).toEqual({ state: 'invalid', code: 'V1_MODELS_MALFORMED' });
    expect(parseVehicleCatalogResult({ ...DOCUMENT, models: Array.from({ length: MAX_V1_MODELS + 1 }, () => ({ canonical_model_name: 'x' })) })).toEqual({ state: 'invalid', code: 'V1_TOO_MANY_MODELS' });
  });

  it('bounds free text before it can reach the screen', () => {
    const long = 'x'.repeat(5_000);
    const parsed = parseVehicleCatalogResult({ ...DOCUMENT, manufacturer: long, models: [{ canonical_model_name: long, verification_status: 'verified' }] });
    if (parsed.state !== 'result') throw new Error('expected a result');
    expect(parsed.result.manufacturer.length).toBeLessThanOrEqual(200);
    expect(parsed.result.models[0].name.length).toBeLessThanOrEqual(200);
  });
});
