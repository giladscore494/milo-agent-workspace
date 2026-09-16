/**
 * `redactSecretText` — the per-string redaction boundary.
 *
 * Tested directly, not only through the final-result surface, because it is a
 * security primitive: a gap here is a gap everywhere it is used, and a surface
 * test can only prove the positions it happens to cover.
 *
 * Every credential-shaped input is assembled at runtime. `scripts/secret_scan.py`
 * exists to keep key-shaped literals out of this repository, and a test file is
 * not an exemption from that rule.
 */

import { describe, expect, it } from 'vitest';
import { REDACTED, redactSecretText, redactSecrets } from '../lib/sanitize';
import {
  API_KEY_SENTINEL,
  BEARER_SENTINEL,
  JWT_SENTINEL,
  SUPABASE_PUBLISHABLE_PUBLIC,
  SUPABASE_SECRET_SENTINEL,
} from './secretSentinels';

describe('1. credential shapes are redacted', () => {
  const SHAPES: [string, string][] = [
    ['provider API key', API_KEY_SENTINEL],
    ['bearer token', BEARER_SENTINEL],
    ['legacy JWT / service-role', JWT_SENTINEL],
    ['modern Supabase server-side key', SUPABASE_SECRET_SENTINEL],
  ];

  it('1a. each shape is replaced by the marker when it stands alone', () => {
    for (const [name, secret] of SHAPES) {
      expect(redactSecretText(secret), name).toBe(REDACTED);
    }
  });

  it('1b. each shape is found when embedded in surrounding prose', () => {
    for (const [name, secret] of SHAPES) {
      const out = redactSecretText(`the recorded value is ${secret} as captured`);
      expect(out, name).not.toContain(secret);
      expect(out, name).toContain(REDACTED);
      expect(out, name).toContain('as captured');
    }
  });

  it('1c. a PEM block is redacted from its header', () => {
    // Assembled, not written: `scripts/secret_scan.py` blocks a committed PEM
    // private-key header literal, and a test file is not an exemption — which
    // is why this comment does not spell it out either.
    const dashes = '-'.repeat(5);
    const pem = `${dashes}BEGIN PRIVATE KEY${dashes}\nMIIabcdef\n${dashes}END PRIVATE KEY${dashes}`;
    expect(redactSecretText(pem)).toBe(REDACTED);
  });

  it('1d. a labelled secret keeps its label and loses its value', () => {
    const out = redactSecretText('service_role = hunter2hunter2hunter2');
    expect(out).toBe(`service_role = ${REDACTED}`);
  });

  it('1e. redaction is idempotent and safe to call repeatedly', () => {
    // The patterns are module-level and carry `g`, so a stale `lastIndex`
    // would silently skip the next string handed to them.
    for (let i = 0; i < 5; i += 1) {
      expect(redactSecretText(SUPABASE_SECRET_SENTINEL)).toBe(REDACTED);
    }
    expect(redactSecretText(redactSecretText(API_KEY_SENTINEL))).toBe(REDACTED);
  });
});

describe('2. the MODERN Supabase server-side key, specifically', () => {
  // `docs/deployment/cloud-run-production.md` mandates `sb_secret_` for
  // production and forbids it reaching the browser. It shares no prefix with
  // the legacy JWT, so the legacy sentinel proves nothing about it.

  it('2a. it is redacted, and the legacy JWT does not stand in for it', () => {
    expect(redactSecretText(SUPABASE_SECRET_SENTINEL)).toBe(REDACTED);
    expect(SUPABASE_SECRET_SENTINEL.startsWith('eyJ')).toBe(false);
    expect(JWT_SENTINEL.startsWith('sb_secret_')).toBe(false);
  });

  it('2b. it is caught behind its production variable name', () => {
    // Assembled so the literal env-var name is not committed either.
    const varName = ['SUPABASE', 'SECRET', 'KEY'].join('_');
    const out = redactSecretText(`${varName}=${SUPABASE_SECRET_SENTINEL}`);
    expect(out).not.toContain(SUPABASE_SECRET_SENTINEL);
    expect(out).toContain(REDACTED);
  });

  it('2c. PUBLIC sb_ configuration is left alone — the target is sb_secret_', () => {
    // A publishable/anon key is public configuration the browser is MEANT to
    // hold. Redacting it would hide a legitimate value and protect nothing.
    expect(redactSecretText(SUPABASE_PUBLISHABLE_PUBLIC)).toBe(SUPABASE_PUBLISHABLE_PUBLIC);
    const anon = ['sb', 'anon', 'E'.repeat(10) + 'F'.repeat(10) + '9012'].join('_');
    expect(redactSecretText(anon)).toBe(anon);
  });

  it('2d. a bare sb_ prefix with no secret marker is not a credential', () => {
    expect(redactSecretText('sb_region_eu_west_1')).toBe('sb_region_eu_west_1');
  });
});

describe('3. ordinary product data is never touched', () => {
  it('3a. real values, keys, codes and identifiers pass through unchanged', () => {
    // Over-redaction is the safe failure direction, but it must not be the
    // common one: a surface full of [REDACTED] reports nothing.
    const untouched = [
      'plug-in hybrid', '302', '2487', 'engine_displacement_cc', 'fuel_type',
      'government_record_2026', 'catalog_lookup', 'unresolved conflict',
      'EVIDENCE_REQUIREMENTS_UNMET', 'R5_GOV_RECORD_AMBIGUOUS', 'TASK_FAILED',
      'src-gov-1', 'claim-fuel', 'toyota_rav4_phev', 'IL', '',
    ];
    for (const value of untouched) {
      expect(redactSecretText(value), value).toBe(value);
    }
  });

  it('3b. a non-string input degrades safely rather than throwing', () => {
    expect(redactSecretText(undefined as unknown as string)).toBe('');
    expect(redactSecretText(null as unknown as string)).toBe('');
  });
});

describe('4. the V1 whole-payload redactor is a separate, unchanged function', () => {
  it('4a. redactSecrets still works over a structure, as V1 relies on', () => {
    const out = redactSecrets({ summary: 'V1 mocked output', note: API_KEY_SENTINEL }) as
      Record<string, unknown>;
    expect(out.summary).toBe('V1 mocked output');
    expect(JSON.stringify(out)).not.toContain(API_KEY_SENTINEL);
  });

  it('4b. the two are distinct exports with distinct jobs', () => {
    expect(typeof redactSecrets).toBe('function');
    expect(typeof redactSecretText).toBe('function');
    expect(redactSecrets).not.toBe(redactSecretText);
  });
});
