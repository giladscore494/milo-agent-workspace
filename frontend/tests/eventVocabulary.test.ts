/**
 * The browser's event vocabulary is DERIVED from the backend's, not copied.
 *
 * `lib/eventVocabulary.ts` used to hand-mirror two of the backend's sets and
 * hand-MAINTAIN a third (`SWARM_V2_EVENT_TYPES`) that had no backend
 * counterpart at all — and it had drifted: five types
 * `backend/engines/swarm_v2/engine.py` really emits were missing from it, so
 * they were filed as unknown here while the database recorded them, and eight
 * durable operational types were missing too.
 *
 * These tests are the browser half of the anti-drift pair. The backend's
 * `tests/test_event_registry.py` proves `eventRegistry.generated.json` is
 * exactly `backend/event_registry.py`; these prove this module is exactly that
 * manifest, and that recognising a type still grants only the projection its
 * group owns.
 */

import { describe, expect, it } from 'vitest';
import registry from '../lib/eventRegistry.generated.json';
import {
  ACCEPTED_EVENT_TYPES,
  CATALOG_EVENT_TYPES,
  EVENT_REGISTRY_VERSION,
  OPERATIONAL_EVENT_TYPES,
  SWARM_V2_EVENT_TYPES,
  V1_EVENT_TYPES,
  isKnownEventType,
  ownsAgentProjection,
  ownsCatalogProjection,
  ownsSpendTelemetry,
  ownsV1Projection,
} from '../lib/eventVocabulary';

const sorted = (set: ReadonlySet<string>) => [...set].sort();

describe('the generated manifest is the single source', () => {
  it('states its version and every group', () => {
    expect(EVENT_REGISTRY_VERSION).toBe(registry.registry_version);
    expect(Object.keys(registry.groups).sort()).toEqual([
      'capture_progress', 'catalog', 'operational', 'run_level', 'swarm_v2', 'v1',
    ]);
  });

  it('is what each exported set is built from', () => {
    expect(sorted(V1_EVENT_TYPES)).toEqual([...registry.groups.v1].sort());
    expect(sorted(SWARM_V2_EVENT_TYPES)).toEqual([...registry.groups.swarm_v2].sort());
    expect(sorted(CATALOG_EVENT_TYPES)).toEqual([...registry.groups.catalog].sort());
    expect(sorted(OPERATIONAL_EVENT_TYPES)).toEqual([...registry.groups.operational].sort());
    expect(sorted(ACCEPTED_EVENT_TYPES)).toEqual([...registry.accepted].sort());
  });

  it('recognises exactly what the backend will durably accept', () => {
    for (const type of registry.accepted) expect(isKnownEventType(type)).toBe(true);
    // The Government capture's progress names are declared but never durable,
    // so the browser must not recognise one as a run event.
    for (const type of registry.groups.capture_progress) {
      expect(isKnownEventType(type)).toBe(false);
    }
  });

  it('carries the Swarm V2 types that used to be missing from this file', () => {
    for (const type of ['correction_round_started', 'correction_round_blocked',
                        'correction_round_declined', 'correction_round_finalizing',
                        'conflict_resolution_recorded']) {
      expect(SWARM_V2_EVENT_TYPES.has(type)).toBe(true);
      expect(isKnownEventType(type)).toBe(true);
    }
  });
});

describe('membership still grants only its own projection', () => {
  it('never lets a swarm, catalog or operational type write the V1 projection', () => {
    for (const set of [SWARM_V2_EVENT_TYPES, CATALOG_EVENT_TYPES, OPERATIONAL_EVENT_TYPES]) {
      for (const type of set) {
        expect(ownsV1Projection(type)).toBe(false);
        expect(ownsAgentProjection(type)).toBe(false);
        expect(ownsSpendTelemetry(type)).toBe(false);
      }
    }
  });

  it('gives the operational types no projection at all', () => {
    expect(OPERATIONAL_EVENT_TYPES.size).toBeGreaterThan(0);
    for (const type of OPERATIONAL_EVENT_TYPES) {
      expect(ownsCatalogProjection(type)).toBe(false);
      expect(SWARM_V2_EVENT_TYPES.has(type)).toBe(false);
      expect(V1_EVENT_TYPES.has(type)).toBe(false);
    }
  });

  it('keeps run-level V1 types out of the agent projection', () => {
    for (const type of registry.groups.run_level) {
      expect(ownsV1Projection(type)).toBe(true);
      expect(ownsAgentProjection(type)).toBe(false);
    }
  });

  it('recognises by exact type, never by prefix', () => {
    expect(ownsCatalogProjection('catalog_variant_promoted')).toBe(true);
    expect(ownsCatalogProjection('catalog_variant_promoted_v2')).toBe(false);
    expect(isKnownEventType('task_started_v2')).toBe(false);
    expect(isKnownEventType('')).toBe(false);
  });
});
