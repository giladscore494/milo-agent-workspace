#!/usr/bin/env bash
# READ-ONLY (PR-OBS): does the deploy Workload Identity provider admit a GitHub
# environment -- by default production-backup, the scheduled Supabase backup's
# -- while still pinning this repository and refs/heads/main?
#
# The pool and provider are the ones scripts/ops/setup-wif.sh creates (their
# ids are read from that script, not repeated here). Prints exactly ONE line
# and ALWAYS exits 0 -- a missing environment is a GAP for the operator, never
# a reason to stop a deploy:
#   PASS <detail>        admitted, repository and main still pinned
#   GAP <detail>         not admitted yet: run scripts/ops/setup-wif.sh --apply
#   UNREADABLE <detail>  this identity cannot read the provider
#
#   check-wif-environment.sh PROJECT_ID [ENVIRONMENT]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_WIF="${SCRIPT_DIR}/../ops/setup-wif.sh"
PROJECT_ID="${1:?usage: check-wif-environment.sh PROJECT_ID [ENVIRONMENT]}"
ENVIRONMENT_NAME="${2:-production-backup}"
REPOSITORY="${MILO_GITHUB_REPOSITORY:-giladscore494/milo-agent-workspace}"
POOL_ID="$(sed -n 's/^POOL_ID="\([a-z0-9-]*\)"$/\1/p' "$SETUP_WIF")"
PROVIDER_ID="$(sed -n 's/^PROVIDER_ID="\([a-z0-9-]*\)"$/\1/p' "$SETUP_WIF")"
if [[ -z "$POOL_ID" || -z "$PROVIDER_ID" ]]; then
  printf 'UNREADABLE the pool/provider ids could not be read from scripts/ops/setup-wif.sh\n'
  exit 0
fi

if ! condition="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
      --workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID" \
      --format='value(attributeCondition)' 2> /dev/null)"; then
  printf 'UNREADABLE provider %s/%s could not be read with this identity (it needs iam.workloadIdentityPoolProviders.get); verify with scripts/ops/setup-wif.sh --plan\n' \
    "$POOL_ID" "$PROVIDER_ID"
  exit 0
fi

verdict="$(python3 -c '
import re, sys
condition, repository, environment = sys.argv[1:4]
q = chr(39)
clauses = [c.strip() for c in condition.split("&&")]
envs = re.fullmatch(r"assertion\.environment in \[(.*)\]", clauses[-1] if clauses else "")
listed = re.findall(q + "([^" + q + "]*)" + q, envs.group(1)) if envs else []
pinned = (len(clauses) == 3 and clauses[0] == "assertion.repository == " + q + repository + q
          and clauses[1] == "assertion.ref == " + q + "refs/heads/main" + q)
print("PASS" if pinned and environment in listed else ("UNPINNED" if not pinned else "GAP"))' \
  "$condition" "$REPOSITORY" "$ENVIRONMENT_NAME")"

case "$verdict" in
  PASS)
    printf 'PASS provider %s admits %s (repository %s and refs/heads/main still pinned)\n' \
      "$PROVIDER_ID" "$ENVIRONMENT_NAME" "$REPOSITORY" ;;
  GAP)
    printf 'GAP provider %s does not admit %s yet: the scheduled backup cannot authenticate until the operator runs scripts/ops/setup-wif.sh --apply\n' \
      "$PROVIDER_ID" "$ENVIRONMENT_NAME" ;;
  *)
    printf 'GAP provider %s condition is not the one setup-wif.sh writes (repository/main clauses differ): re-run scripts/ops/setup-wif.sh --apply\n' \
      "$PROVIDER_ID" ;;
esac
exit 0
