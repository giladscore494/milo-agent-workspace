import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('Supabase auth client dependency hygiene', () => {
  it('uses the installed package without remote runtime import loaders', () => {
    const source = readFileSync(resolve(process.cwd(), 'lib/supabaseClient.ts'), 'utf8');
    expect(source).toContain("from '@supabase/supabase-js'");
    expect(source).not.toContain('esm.sh');
    expect(source).not.toContain('https://');
    expect(source).not.toContain('new Function');
    expect(source).not.toContain('import(m)');
  });
});
