import { spawnSync } from 'node:child_process';
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
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
    for (const marker of ['Live event stream', 'Final artifacts', 'Live run', 'Workflow proposal']) {
      expect(app).not.toContain(marker);
      expect(components).toContain(marker);
    }

    const result = spawnSync('node', [SCRIPT], { cwd: process.cwd(), encoding: 'utf8' });
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('app, components');
  });

  it('fails deterministically and names a marker that disappears from components', () => {
    const workspace = mkdtempSync(join(tmpdir(), 'milo-static-ui-'));
    try {
      for (const root of ['app', 'components', 'lib']) {
        cpSync(resolve(process.cwd(), root), join(workspace, root), { recursive: true });
      }
      rmSync(join(workspace, 'components/run/RunOutputPanel.tsx'));
      const result = spawnSync('node', [SCRIPT], { cwd: workspace, encoding: 'utf8' });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('Missing UI marker: Final artifacts');
    } finally {
      rmSync(workspace, { recursive: true, force: true });
    }
  });
});
