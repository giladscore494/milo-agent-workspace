import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * What may reach the browser.
 *
 * Three separate questions, because passing one says nothing about the others:
 *
 *  1. does browser-bound SOURCE name a server-only credential? (the original
 *     check, unchanged in intent);
 *  2. does browser-bound source read a `NEXT_PUBLIC_*` variable that is not on
 *     the approved list? Next.js inlines those into the client bundle by
 *     definition, so adding one is a decision about what the browser holds,
 *     and it belongs in `docs/production-readiness/ENVIRONMENT_MATRIX.md`
 *     before it belongs in code;
 *  3. does the BUILT client bundle contain credential-shaped material or a
 *     server-only variable name? Source can be clean while configuration,
 *     a dependency or a build-time inline is not — and the bundle is the
 *     artifact that is actually served.
 *
 * (3) needs build output, which not every invocation has. A check that
 * silently degrades is worse than no check, so the absence is REPORTED, and
 * `MILO_REQUIRE_BUNDLE_SCAN=1` turns it into a failure — the same convention
 * `MILO_REQUIRE_PG_TESTS` uses for the PostgreSQL suite. CI sets it, because
 * CI always builds first.
 */

const SOURCE_ROOTS = ['app', 'components', 'lib'];
const SOURCE_FILE = /\.(ts|tsx|js|jsx|mjs)$/;

/**
 * Every `NEXT_PUBLIC_*` variable the browser is allowed to hold.
 * Source of truth: `docs/production-readiness/ENVIRONMENT_MATRIX.md`
 * (Browser-visible = yes). All three are public by design: the Supabase URL
 * and anon key are what the browser authenticates with, and the execution-UI
 * flag only decides what is rendered — it is never a security boundary.
 */
const APPROVED_PUBLIC_VARS = new Set([
  'NEXT_PUBLIC_SUPABASE_URL',
  'NEXT_PUBLIC_SUPABASE_ANON_KEY',
  'NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI',
]);

/** Server-only markers that must never appear in browser-bound source. */
const FORBIDDEN_IN_SOURCE = [
  /SUPABASE_SERVICE_ROLE/i, /KIMI_API_KEY/i, /MOONSHOT_API_KEY/i, /service_role/i,
  /sk-[A-Za-z0-9_-]{20,}/,
];

/**
 * Credential SHAPES and server-only variable NAMES that must not be in the
 * served client bundle. Shapes catch a value that was inlined; names catch a
 * server module that was pulled into a client chunk.
 */
const FORBIDDEN_IN_BUNDLE = [
  { name: 'modern Supabase server-side key', pattern: /sb_secret_[A-Za-z0-9_-]{8,}/ },
  { name: 'provider API key', pattern: /\bsk-[A-Za-z0-9_-]{20,}/ },
  { name: 'PEM private key block', pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/ },
  { name: 'Supabase service-role variable', pattern: /SUPABASE_SERVICE_ROLE_KEY|SUPABASE_SECRET_KEY/ },
  { name: 'provider key variable', pattern: /KIMI_API_KEY|MOONSHOT_API_KEY/ },
  { name: 'Redis credential variable', pattern: /UPSTASH_REDIS_REST_TOKEN/ },
  { name: 'private API address variable', pattern: /CLOUD_RUN_API_URL/ },
];

/** Build outputs this repository produces (`next.config.mjs` distDir). */
const BUNDLE_DIRS = ['.next', '.next-e2e-disabled', '.next-e2e-enabled'];
const BUNDLE_FILE = /\.(js|mjs|json|css|html|txt)$/;

let failed = false;

function fail(message) {
  console.error(message);
  failed = true;
}

function walk(dir, matcher, files = []) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, matcher, files);
    else if (matcher.test(path)) files.push(path);
  }
  return files;
}

// 1 + 2: browser-bound source.
const sourceFiles = SOURCE_ROOTS.filter(existsSync).flatMap((root) => walk(root, SOURCE_FILE));
if (sourceFiles.length === 0) {
  fail(`No browser-bound source found under: ${SOURCE_ROOTS.join(', ')}`);
}
for (const file of sourceFiles) {
  const text = readFileSync(file, 'utf8');
  for (const pattern of FORBIDDEN_IN_SOURCE) {
    if (pattern.test(text)) fail(`Forbidden secret marker ${pattern} in ${file}`);
  }
  for (const [name] of text.matchAll(/NEXT_PUBLIC_[A-Z0-9_]+/g)) {
    if (!APPROVED_PUBLIC_VARS.has(name)) {
      fail(
        `Unapproved browser variable ${name} in ${file}. Every NEXT_PUBLIC_* value is inlined `
        + 'into the client bundle; add it to ENVIRONMENT_MATRIX.md and to APPROVED_PUBLIC_VARS '
        + 'in this script, deliberately, or do not read it in browser-bound code.',
      );
    }
  }
}

// 3: the artifact that is actually served.
const requireBundleScan = (process.env.MILO_REQUIRE_BUNDLE_SCAN ?? '').trim() === '1';
const bundleRoots = BUNDLE_DIRS.map((dir) => join(dir, 'static')).filter(existsSync);

if (bundleRoots.length === 0) {
  const message = 'No client build output found (looked for '
    + `${BUNDLE_DIRS.map((d) => `${d}/static`).join(', ')}); run \`npm run build\` first.`;
  if (requireBundleScan) fail(`${message} MILO_REQUIRE_BUNDLE_SCAN=1 makes this a failure.`);
  else console.log(`Bundle scan NOT performed: ${message}`);
} else {
  let scanned = 0;
  for (const root of bundleRoots) {
    for (const file of walk(root, BUNDLE_FILE)) {
      scanned += 1;
      const text = readFileSync(file, 'utf8');
      for (const { name, pattern } of FORBIDDEN_IN_BUNDLE) {
        const match = text.match(pattern);
        if (match) fail(`Served client bundle carries a ${name} (${match[0].slice(0, 12)}…) in ${file}`);
      }
      for (const [variable] of text.matchAll(/NEXT_PUBLIC_[A-Z0-9_]+/g)) {
        if (!APPROVED_PUBLIC_VARS.has(variable)) {
          fail(`Served client bundle references unapproved browser variable ${variable} in ${file}`);
        }
      }
    }
  }
  console.log(`Bundle scan: ${scanned} files under ${bundleRoots.join(', ')}.`);
}

if (failed) process.exit(1);
console.log(`No browser secret markers found (${sourceFiles.length} source files, ${APPROVED_PUBLIC_VARS.size} approved public variables).`);
