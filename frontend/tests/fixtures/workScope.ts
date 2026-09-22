/** Server-shaped Mapping Plan bodies, shared by the work-scope tests. */

export const PROJECT = '677db6c2-b44c-41c1-b4e1-b51229d697df';
export const CONVERSATION = '1f90f4ce-7844-4031-91d6-b74e40e1884e';
export const PLAN = '11111111-2222-4333-8444-555555555555';
export const DIGEST = 'a'.repeat(64);
export const NEXT_DIGEST = 'b'.repeat(64);

export const CAPABILITIES = {
  available: true,
  reason: null,
  contract: 'milo-work-scope/1',
  directory_version: 'milo-manufacturer-directory/1',
  limits: {
    max_units: 39, max_items: 2000, default_max_items: 100, max_batch_size: 20,
    default_batch_size: 10, min_model_year: 1900, max_model_year: 2100, max_instruction_chars: 500,
  },
  can_prepare: false,
  can_start_batches: false,
};

export const DIRECTORY = {
  directory_version: 'milo-manufacturer-directory/1',
  origins: [{ key: 'japan', label: 'Japan' }, { key: 'south_korea', label: 'South Korea' }],
  entries: [
    { key: 'kia', name: 'Kia', name_he: 'קיה', origin: 'south_korea', register_marque: null,
      register_marque_verified: false, coverage: { state: 'unverifiable', canonical_variants: null } },
    { key: 'lexus', name: 'Lexus', name_he: 'לקסוס', origin: 'japan', register_marque: null,
      register_marque_verified: false, coverage: { state: 'unverifiable', canonical_variants: null } },
    { key: 'mazda', name: 'Mazda', name_he: 'מאזדה', origin: 'japan', register_marque: null,
      register_marque_verified: false, coverage: { state: 'unverifiable', canonical_variants: null } },
    { key: 'toyota', name: 'Toyota', name_he: 'טויוטה', origin: 'japan', register_marque: 'טויוטה',
      register_marque_verified: true, coverage: { state: 'known', canonical_variants: 0 } },
  ],
  coverage: { available: true, catalog_variants: 0, attributed_variants: 0 },
};

export function stateBody(overrides: Record<string, unknown> = {}) {
  return {
    work_scope_id: PLAN,
    conversation_id: CONVERSATION,
    project_id: PROJECT,
    status: 'draft',
    revision: 1,
    digest: DIGEST,
    current: true,
    plan: {
      contract: 'milo-work-scope/1', directory_version: 'milo-manufacturer-directory/1',
      units: ['toyota', 'lexus'], model_year_from: 2018, model_year_to: null,
      max_items: 800, batch_size: 10,
    },
    head: { revision: 1, digest: DIGEST, input_kind: 'instruction',
            instruction: 'Map Toyota and Lexus', notes: [], created_at: '2026-09-22T00:00:00Z' },
    history: [],
    created_at: '2026-09-22T00:00:00Z',
    updated_at: '2026-09-22T00:00:00Z',
    ...overrides,
  };
}
