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
/**
 * The F4 product surface. `Final result` is a DIFFERENT surface from `Final
 * artifacts`: both markers are required, so the typed Swarm V2 result and the
 * V1 sanitized-output path can never be collapsed into one panel by a refactor.
 */
const requiredFinalResult = ['Final result', 'Verified fields', 'Outstanding items', 'Result unavailable', 'parseFinalResult', 'Provenance'];
/** Security and durable-contract behaviour that must stay wired into the UI. */
const requiredSecurity = ['safeText', 'redactSecrets', 'milo.activeRun.', 'Run finished with status', 'aria-expanded', 'aria-controls'];
/**
 * The F5 surfaces. `launch_unknown` must keep saying that nothing retries it,
 * and the cancellation confirmation must keep its accessible name — both are
 * behaviour a refactor can drop without breaking a render test.
 */
const requiredF5Ui = [
  'Confirm run cancellation',
  'operator reconciliation required',
  'will <b>not</b> be relaunched automatically',
  'safeErrorText',
];
/**
 * The CODE-2 catalog surface.
 *
 * Both the heading and the wording that a refusal is NOT a failed run are
 * required: dropping either would leave an operator with counts and no way to
 * read them correctly. `catalogRefusalLabel` is required in the tree because
 * rendering a reason code directly instead of resolving it through the closed
 * allowlist is precisely the regression this marker catches.
 */
const requiredCatalogUi = [
  'Catalog status',
  'not a failed run',
  // The tri-state resolver, not the bare code lookup: rendering a reason any
  // other way is how a stale or untrusted reason reaches the surface.
  'catalogRefusalReasonLabel',
  // A malformed field list must say so rather than claim zero.
  'Field count unavailable',
];
/**
 * Client state ownership, checked PER FILE.
 *
 * These are the guards that keep one selection's data out of another's. The
 * marker has to be present where it is actually applied, not merely somewhere
 * in the tree: a helper that still EXISTS while nothing calls it is exactly the
 * regression this is here to catch.
 */
const requiredOwnership = {
  'lib/ownership.ts': [
    'runBelongsToScope', 'eventBelongsToRun',
    'ownsSession', 'ownsProject', 'ownsConversation', 'ownsRun', 'nextSessionScope',
    // Owner-scoped pending state: a superseded request must not clear its
    // successor's busy flag, and a replacement session must not inherit one.
    'beginPending', 'settlePending',
  ],
  'lib/useRunRealtime.ts': ['runBelongsToScope', 'eventBelongsToRun'],
  'app/page.tsx': [
    'ownsSession', 'ownsProject', 'ownsConversation', 'ownsRun',
    'nextSessionScope', 'clearStoredRunIds', 'beginPending', 'settlePending',
  ],
  // Recognition before projection. An unknown event type must not be able to
  // manufacture an agent, a phase or a spend total.
  'lib/eventVocabulary.ts': ['V1_EVENT_TYPES', 'ownsV1Projection', 'ownsAgentProjection', 'ownsSpendTelemetry',
    // CODE-2: catalog recognition is its own closed set and its own gate. A
    // catalog type folded into V1_EVENT_TYPES would inherit the V1 projection.
    'CATALOG_EVENT_TYPES', 'ownsCatalogProjection'],
  'lib/runReducer.ts': ['ownsV1Projection', 'ownsAgentProjection', 'ownsSpendTelemetry'],
  // The catalog slice is written only through its own gated reducer.
  'lib/swarmReducer.ts': ['ownsCatalogProjection', 'reduceCatalogEvent'],
  'lib/catalogStatus.ts': ['CATALOG_REFUSAL_LABELS', 'UNKNOWN_CATALOG_REFUSAL_LABEL',
    'redactSecretText',
    // The refusal tri-state and the declared list bound. Dropping either
    // reintroduces a corrected defect: a stale "latest refusal", or an
    // unbounded array's length presented as a field count.
    'CatalogRefusalReason', 'UNKNOWN_CATALOG_REFUSAL', 'MAX_CANONICAL_FIELDS'],
  // No upstream text is ever rendered: copy is authored here, allowlisted by
  // classification value.
  'lib/errorText.ts': ['ERROR_COPY', 'AuthFailure', 'classifyError'],
  'lib/supabaseClient.ts': ['AuthFailure'],
};
const requiredReducer = ['some(e => e.id === event.id)', 'reconstructRun', 'tool_access_granted', 'source_recorded'];

const where = `${scanned.length} files under ${ROOTS.join(', ')}`;
for (const item of requiredUi) {
  if (!ui.includes(item)) throw new Error(`Missing UI marker: ${item} (searched ${where})`);
}
for (const item of requiredFinalResult) {
  if (!ui.includes(item)) throw new Error(`Missing final-result marker: ${item} (searched ${where})`);
}
for (const item of requiredSecurity) {
  if (!ui.includes(item)) throw new Error(`Missing UI security marker: ${item} (searched ${where})`);
}
for (const item of requiredF5Ui) {
  if (!ui.includes(item)) throw new Error(`Missing F5 marker: ${item} (searched ${where})`);
}
for (const item of requiredCatalogUi) {
  if (!ui.includes(item)) throw new Error(`Missing catalog marker: ${item} (searched ${where})`);
}
for (const [file, markers] of Object.entries(requiredOwnership)) {
  const source = readFileSync(file, 'utf8');
  for (const marker of markers) {
    if (!source.includes(marker)) {
      throw new Error(`Missing state-ownership marker: ${marker} (expected in ${file})`);
    }
  }
}
for (const item of requiredReducer) {
  if (!reducer.includes(item)) throw new Error(`Missing reducer marker: ${item}`);
}
/**
 * Strip comments so the construct scan below reads CODE, not prose.
 *
 * A doc comment that explains why a construct is forbidden must not itself
 * trip the check — otherwise the only way to document the rule is to avoid
 * naming the thing it forbids. Line comments are cut at `//` unless it is part
 * of a scheme-relative or absolute URL (`https://`), which would otherwise
 * swallow the rest of the line and could hide real code after it.
 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .split('\n')
    .map((line) => {
      const index = line.search(/(^|[^:])\/\//);
      if (index < 0) return line;
      return line.slice(0, line.indexOf('//', index));
    })
    .join('\n');
}

/**
 * The final-result surface renders the CLOSED contract, never the payload.
 * Serialising the durable payload into the markup, or injecting untrusted
 * HTML, would reintroduce exactly the raw-output display F4 replaced, so
 * either one in that directory is a hard failure.
 */
const FORBIDDEN_IN_FINAL_RESULT = ['JSON.stringify', 'dangerouslySetInnerHTML'];
const finalResultSources = scanned.filter((file) => file.includes('components/result'));
if (finalResultSources.length === 0) {
  throw new Error('Static UI check found no final-result surface under components/result');
}
for (const file of finalResultSources) {
  const code = stripComments(readFileSync(file, 'utf8'));
  for (const forbidden of FORBIDDEN_IN_FINAL_RESULT) {
    if (code.includes(forbidden)) {
      throw new Error(`Forbidden construct ${forbidden} in final-result surface: ${file}`);
    }
  }
}

console.log(`Static UI/reducer coverage markers found (${where}).`);
