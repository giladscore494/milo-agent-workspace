import { spawnSync } from 'node:child_process';
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

function read(dir: string): string {
  return readdirSync(dir)
    .sort()
    .map((entry) => {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) return entry.startsWith('.') ? '' : read(path);
      return /\.(ts|tsx|js|jsx|mjs)$/.test(entry) ? readFileSync(path, 'utf8') : '';
    })
    .join('\n');
}

const SCRIPT = resolve(process.cwd(), 'scripts/static-ui-check.mjs');

describe('static UI marker coverage after component extraction', () => {
  it('covers markers that now live only in extracted components', () => {
    const app = read('app');
    const components = read('components');
    // These markers moved out of app/page.tsx, so a page-only scan would miss them.
    for (const marker of ['Live event stream', 'Pipeline quality', 'Live run', 'Workflow proposal']) {
      expect(app).not.toContain(marker);
      expect(components).toContain(marker);
    }

    const result = spawnSync('node', [SCRIPT], { cwd: process.cwd(), encoding: 'utf8' });
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('app, components');
  });

  it('fails deterministically and names a marker that disappears from components', () => {
    withWorkspace((workspace) => {
      rmSync(join(workspace, 'components/result/VehicleCatalogResultPanel.tsx'));
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Missing final-result marker: Pipeline quality');
    });
  });
});

/** Copy the scanned roots into a scratch tree so a failure case can be staged. */
function withWorkspace(body: (workspace: string) => void): void {
  const workspace = mkdtempSync(join(tmpdir(), 'milo-static-ui-'));
  try {
    for (const root of ['app', 'components', 'lib']) {
      cpSync(resolve(process.cwd(), root), join(workspace, root), { recursive: true });
    }
    body(workspace);
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
}

describe('state-ownership and F5 markers', () => {
  it('fails when an ownership guard is removed from the page', () => {
    withWorkspace((workspace) => {
      const file = join(workspace, 'app/page.tsx');
      const body = readFileSync(file, 'utf8');
      // Removing the guard is exactly the regression that would let one
      // conversation's late answer render under another.
      writeFileSync(file, body.replace(/ownsConversation/g, 'alwaysTrue'));
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Missing state-ownership marker: ownsConversation');
    });
  });

  it('fails when the run-row verification is removed from the polling hook', () => {
    withWorkspace((workspace) => {
      const file = join(workspace, 'lib/useRunRealtime.ts');
      const body = readFileSync(file, 'utf8');
      writeFileSync(file, body.replace(/runBelongsToScope/g, 'trustTheServer'));
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Missing state-ownership marker: runBelongsToScope');
    });
  });

  it('fails when launch_unknown stops saying that nothing retries it', () => {
    withWorkspace((workspace) => {
      const file = join(workspace, 'components/run/LaunchStateNote.tsx');
      const body = readFileSync(file, 'utf8');
      writeFileSync(file, body.replace('will <b>not</b> be relaunched automatically', 'is being retried'));
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Missing F5 marker');
    });
  });
});

describe('final-result surface construct guard', () => {
  it('requires the final-result surface to exist at all', () => {
    withWorkspace((workspace) => {
      rmSync(join(workspace, 'components/result'), { recursive: true });
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      // It fails on the missing markers first; either way it never passes.
      expect(result.stderr).toMatch(/Missing final-result marker|no final-result surface/);
    });
  });

  for (const forbidden of ['JSON.stringify', 'dangerouslySetInnerHTML']) {
    it(`rejects ${forbidden} used as CODE in the final-result surface`, () => {
      withWorkspace((workspace) => {
        const file = join(workspace, 'components/result/FinalResultPanel.tsx');
        const body = readFileSync(file, 'utf8');
        writeFileSync(file, `${body}\nconst leak = ${forbidden};\n`);
        const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
        expect(result.status).not.toBe(0);
        expect(result.stderr).toContain(`Forbidden construct ${forbidden}`);
      });
    });
  }

  it('allows a doc comment to NAME the construct it forbids', () => {
    // Otherwise the only way to document the rule would be to avoid saying
    // what it forbids, which is how a rule quietly stops being understood.
    withWorkspace((workspace) => {
      const file = join(workspace, 'components/result/FinalResultPanel.tsx');
      const body = readFileSync(file, 'utf8');
      writeFileSync(file, `/* never JSON.stringify the payload */\n// and no dangerouslySetInnerHTML\n${body}`);
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).toBe(0);
    });
  });

  it('does not let a URL swallow real code on the same line', () => {
    withWorkspace((workspace) => {
      const file = join(workspace, 'components/result/FinalResultPanel.tsx');
      const body = readFileSync(file, 'utf8');
      writeFileSync(file, `${body}\nconst x = 'https://example.com'; const leak = JSON.stringify;\n`);
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Forbidden construct JSON.stringify');
    });
  });
});
