#!/usr/bin/env bash
# Install the PostgreSQL CLIENT tools of exactly one major version on a GitHub
# runner (Ubuntu), from the PostgreSQL project's apt repository (PGDG), and
# prove the installed pg_dump / pg_restore / psql are that major.
#
# Used by .github/workflows/backup-supabase-scheduled.yml: pg_dump must match
# the server's major (a newer server is refused by an older pg_dump, and the
# backup tool refuses any mismatch), and the restore test needs pg_restore of
# the backup's major. The tools land in /usr/lib/postgresql/<major>/bin.
#
#   install-pg-client.sh <major>
set -euo pipefail

major="${1:-}"
if [[ ! "$major" =~ ^[0-9]{2}$ ]]; then
  printf 'FAIL: usage: install-pg-client.sh <major, e.g. 17>\n' >&2
  exit 2
fi
bin_dir="/usr/lib/postgresql/${major}/bin"

if [[ ! -x "${bin_dir}/pg_dump" ]]; then
  keyring=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  sudo install -d "$(dirname "$keyring")"
  sudo curl -fsSL -o "$keyring" https://www.postgresql.org/media/keys/ACCC4CF8.asc
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME}")"
  printf 'deb [signed-by=%s] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' "$keyring" "$codename" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq --no-install-recommends "postgresql-client-${major}" > /dev/null
fi

for tool in pg_dump pg_restore psql; do
  version="$("${bin_dir}/${tool}" --version 2> /dev/null || true)"
  if [[ ! "$version" =~ \(PostgreSQL\)\ ${major}(\.|$) ]]; then
    printf 'FAIL: %s is not PostgreSQL %s (%s)\n' "${bin_dir}/${tool}" "$major" "${version:-missing}" >&2
    exit 1
  fi
done
printf 'PASS PostgreSQL %s client tools: %s\n' "$major" "$("${bin_dir}/pg_dump" --version)"
