#!/usr/bin/env bash
# Link the checkout's frontend/ to the production Vercel project and put a
# `vercel` command on PATH for the kill switch (scripts/deploy/kill-switch.sh
# runs every vercel command in the linked directory and checks `vercel whoami`).
#
# NON-secret identifiers come from repository variables (VERCEL_ORG_ID,
# VERCEL_PROJECT_ID, VERCEL_CLI_VERSION). The token is the environment secret
# VERCEL_TOKEN, read by the wrapper at call time and passed to the CLI as its
# --token argument: it is never written to a file and never printed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

for name in VERCEL_ORG_ID VERCEL_PROJECT_ID VERCEL_CLI_VERSION; do
  if [[ -z "${!name:-}" ]]; then
    printf 'FAIL: the repository variable %s is not set\n' "$name" >&2
    exit 2
  fi
done
[[ "$VERCEL_ORG_ID" =~ ^[A-Za-z0-9_-]+$ && "$VERCEL_PROJECT_ID" =~ ^[A-Za-z0-9_-]+$ ]] \
  || { printf 'FAIL: VERCEL_ORG_ID / VERCEL_PROJECT_ID are not Vercel ids\n' >&2; exit 2; }
[[ "$VERCEL_CLI_VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] \
  || { printf 'FAIL: VERCEL_CLI_VERSION must be a pinned version such as 48.1.0\n' >&2; exit 2; }

mkdir -p "${REPO_ROOT}/frontend/.vercel"
printf '{"orgId":"%s","projectId":"%s"}\n' "$VERCEL_ORG_ID" "$VERCEL_PROJECT_ID" \
  > "${REPO_ROOT}/frontend/.vercel/project.json"

bin_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/milo-vercel-bin"
mkdir -p "$bin_dir"
cat > "${bin_dir}/vercel" << WRAPPER
#!/usr/bin/env bash
set -euo pipefail
: "\${VERCEL_TOKEN:?the environment secret VERCEL_TOKEN is not bound to this step}"
exec npx --yes "vercel@${VERCEL_CLI_VERSION}" "\$@" --token "\$VERCEL_TOKEN"
WRAPPER
chmod 755 "${bin_dir}/vercel"
if [[ -n "${GITHUB_PATH:-}" ]]; then
  printf '%s\n' "$bin_dir" >> "$GITHUB_PATH"
fi
printf 'Vercel project linked in frontend/ and the CLI (vercel@%s) is on PATH.\n' "$VERCEL_CLI_VERSION"
