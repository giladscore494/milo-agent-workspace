import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { API_KEY_SENTINEL, SUPABASE_SECRET_SENTINEL } from './secretSentinels';

/**
 * The browser-bundle check, exercised against staged failures.
 *
 * A guard nobody has watched fail is a guard nobody knows the shape of. Each
 * case below stages the exact thing the check exists to catch and asserts both
 * the exit status and the sentence a reviewer would read.
 */

const SCRIPT = resolve(process.cwd(), 'scripts/no-secret-bundle-check.mjs');

type Options = { bundle?: boolean; env?: Record<string, string> };

/**
 * A scratch copy of the scanned roots, with an optional stand-in for build
 * output so the bundle half can be driven without running a real build.
 */
function withWorkspace(body: (workspace: string) => void, options: Options = {}): void {
  const workspace = mkdtempSync(join(tmpdir(), 'milo-bundle-check-'));
  try {
    for (const root of ['app', 'components', 'lib']) {
      cpSync(resolve(process.cwd(), root), join(workspace, root), { recursive: true });
    }
    if (options.bundle !== false) {
      mkdirSync(join(workspace, '.next/static/chunks'), { recursive: true });
      writeFileSync(join(workspace, '.next/static/chunks/app.js'), 'export const ok = 1;\n');
    }
    body(workspace);
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
}

function run(workspace: string, env: Record<string, string> = {}) {
  return spawnSync('node', [SCRIPT], {
    cwd: workspace,
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

describe('browser-bound source', () => {
  it('passes on the repository as it stands', () => {
    withWorkspace((workspace) => {
      const result = run(workspace);
      expect(result.status).toBe(0);
      expect(result.stdout).toContain('No browser secret markers found');
    });
  });

  it('rejects a server-only credential named in browser-bound source', () => {
    withWorkspace((workspace) => {
      writeFileSync(join(workspace, 'lib/leak.ts'), 'export const k = process.env.SUPABASE_SERVICE_ROLE_KEY;\n');
      const result = run(workspace);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Forbidden secret marker');
    });
  });

  it('rejects a NEXT_PUBLIC variable that was never approved', () => {
    // Next.js inlines every NEXT_PUBLIC_* value into the client bundle, so
    // adding one decides what the browser holds. That decision is documented
    // in ENVIRONMENT_MATRIX.md, not made incidentally in a component.
    withWorkspace((workspace) => {
      writeFileSync(join(workspace, 'lib/flag.ts'), 'export const f = process.env.NEXT_PUBLIC_MILO_NEW_FLAG;\n');
      const result = run(workspace);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Unapproved browser variable NEXT_PUBLIC_MILO_NEW_FLAG');
      expect(result.stderr).toContain('ENVIRONMENT_MATRIX.md');
    });
  });

  it('accepts the three approved browser variables', () => {
    withWorkspace((workspace) => {
      writeFileSync(
        join(workspace, 'lib/approved.ts'),
        'export const a = [process.env.NEXT_PUBLIC_SUPABASE_URL, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY, '
        + 'process.env.NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI];\n',
      );
      expect(run(workspace).status).toBe(0);
    });
  });
});

describe('the served client bundle', () => {
  it('rejects a credential-shaped value that reached a client chunk', () => {
    for (const sentinel of [SUPABASE_SECRET_SENTINEL, API_KEY_SENTINEL]) {
      withWorkspace((workspace) => {
        writeFileSync(join(workspace, '.next/static/chunks/leak.js'), `const k=${JSON.stringify(sentinel)};\n`);
        const result = run(workspace);
        expect(result.status, sentinel).not.toBe(0);
        expect(result.stderr).toContain('Served client bundle carries a');
      });
    }
  });

  it('rejects a server-only variable name that reached a client chunk', () => {
    withWorkspace((workspace) => {
      writeFileSync(join(workspace, '.next/static/chunks/leak.js'), 'const u=process.env.CLOUD_RUN_API_URL;\n');
      const result = run(workspace);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('private API address variable');
    });
  });

  it('rejects an unapproved browser variable that reached a client chunk', () => {
    withWorkspace((workspace) => {
      writeFileSync(join(workspace, '.next/static/chunks/leak.js'), 'const f="NEXT_PUBLIC_MILO_SOMETHING_ELSE";\n');
      const result = run(workspace);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('unapproved browser variable NEXT_PUBLIC_MILO_SOMETHING_ELSE');
    });
  });

  it('reports plainly when there is no build output to scan', () => {
    withWorkspace((workspace) => {
      const result = run(workspace);
      expect(result.status).toBe(0);
      expect(result.stdout).toContain('Bundle scan NOT performed');
    }, { bundle: false });
  });

  it('fails instead of skipping when the scan is required', () => {
    // CI builds first and sets this, so the bundle half can never quietly
    // stop running there.
    withWorkspace((workspace) => {
      const result = run(workspace, { MILO_REQUIRE_BUNDLE_SCAN: '1' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('MILO_REQUIRE_BUNDLE_SCAN=1 makes this a failure');
    }, { bundle: false });
  });
});
