import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Deterministic UI / security marker scan.
 *
 * The workspace markup lives in extracted components now, so no single file
 * can be scanned on its own. This walks every hand-written source file under
 * `app/` and `components/` in sorted order, ignoring generated output and
 * dependencies, and asserts the markers against the union of that tree.
 */

const ROOTS = ['app', 'components'];
const SOURCE_FILE = /\.(ts|tsx|js|jsx|mjs)$/;
const IGNORED_DIRS = new Set(['node_modules', 'dist', 'build', 'coverage', 'playwright-report', 'test-results']);

function isIgnoredDir(name) {
  // `.next`, `.next-e2e-disabled`, `.next-e2e-enabled` and friends are generated.
  return IGNORED_DIRS.has(name) || name.startsWith('.');
}

function collectSourceFiles(dir) {
  const files = [];
  for (const entry of readdirSync(dir).sort()) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (!isIgnoredDir(entry)) files.push(...collectSourceFiles(path));
    } else if (SOURCE_FILE.test(entry)) {
      files.push(path);
    }
  }
  return files;
}

const scanned = ROOTS.flatMap((root) => (existsSync(root) ? collectSourceFiles(root) : []));
if (scanned.length === 0) {
  throw new Error(`Static UI check found no source files under: ${ROOTS.join(', ')}`);
}
const ui = scanned.map((file) => readFileSync(file, 'utf8')).join('\n');
const reducer = readFileSync('lib/runReducer.ts', 'utf8');

/** User-visible surfaces that must survive any refactor of the workspace. */
const requiredUi = ['Projects', 'Conversations', 'Workflow proposal', 'Live run', 'Live event stream', 'Final artifacts', 'Agents', 'Workflow', 'Sources', 'Claims', 'Conflicts', 'Costs', 'Developer', 'forbidden', 'approved', 'active'];
/** Security and durable-contract behaviour that must stay wired into the UI. */
const requiredSecurity = ['safeText', 'redactSecrets', 'milo.activeRun.', 'Run finished with status', 'aria-expanded', 'aria-controls'];
const requiredReducer = ['some(e => e.id === event.id)', 'reconstructRun', 'tool_access_granted', 'source_recorded'];

const where = `${scanned.length} files under ${ROOTS.join(', ')}`;
for (const item of requiredUi) {
  if (!ui.includes(item)) throw new Error(`Missing UI marker: ${item} (searched ${where})`);
}
for (const item of requiredSecurity) {
  if (!ui.includes(item)) throw new Error(`Missing UI security marker: ${item} (searched ${where})`);
}
for (const item of requiredReducer) {
  if (!reducer.includes(item)) throw new Error(`Missing reducer marker: ${item}`);
}
console.log(`Static UI/reducer coverage markers found (${where}).`);
