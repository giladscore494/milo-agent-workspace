#!/usr/bin/env bash
# Exec-only launcher for MILO bootstrap v2. No provider commands, no
# mutation, no parsing, no failure aggregation, no planning, no audit
# logic, and no credential handling live here. All flags, including
# --help, are handled by the python cli this script execs.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/bootstrap-production-v2.py" "$@"
