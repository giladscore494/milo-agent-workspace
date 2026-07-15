"""Static scan for unsafe execution/config defaults committed to the repo.

Fails if any execution flag is turned ON, paid execution is enabled, a
wildcard CORS origin is committed, or a NEXT_PUBLIC_* variable is assigned
secret material in tracked source, Docker, CI or deployment files. Text
scanning only; runtime validation lives in backend/production_config.py.

Test-only fixtures under e2e/ and backend/testing/ legitimately enable
flags for isolated stacks and are scoped out explicitly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

EXECUTION_FLAGS = [
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
    "GATEWAY_ALLOW_EXECUTION_ROUTES",
    "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI",
    # Test-only adapters must never be switched on outside the isolated
    # E2E stacks.
    "MILO_E2E_INPROCESS_WORKER",
]

TEST_ADAPTER_RE = re.compile(r"CLOUD_RUN_AUTH_MODE\s*[:=]\s*['\"]?e2e-test", re.I)

# Files/dirs that are allowed to enable flags because they are test-only
# isolated stacks, never a deployment surface. Release/operator scripts are
# deliberately NOT exempted: they must stay subject to these checks.
ALLOWED_PREFIXES = (
    "frontend/e2e/",
    "frontend/playwright.config.ts",
    "backend/testing/",
    "tests/",
    "frontend/tests/",
    "scripts/check_unsafe_defaults.py",
    "docs/",
)

# Config/deploy surfaces we care about most; everything tracked is scanned,
# but these globs must exist and be clean.
SCAN_SUFFIXES = (".py", ".ts", ".tsx", ".mjs", ".js", ".yml", ".yaml", ".sh", ".env", ".mjs", ".json", ".toml")
SKIP_DIRS = {".git", "node_modules", "legacy", "__pycache__", ".next", "test-results", "playwright-report"}

ENABLE_RE = {flag: re.compile(rf"{flag}\s*[:=]\s*['\"]?(1|true|yes|on)['\"]?", re.I) for flag in EXECUTION_FLAGS}
CORS_WILDCARD_RE = re.compile(r"ALLOWED_CORS_ORIGINS\s*[:=]\s*['\"]?\*")
PUBLIC_SECRET_RE = re.compile(r"NEXT_PUBLIC_[A-Z0-9_]*\s*[:=]\s*['\"]?[^'\"\n]*(service_role|sb_secret|secret_key|private_key)", re.I)


def _is_allowed(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def main() -> int:
    problems: list[str] = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(errors="ignore")
        except UnicodeDecodeError:
            continue
        if CORS_WILDCARD_RE.search(text):
            problems.append(f"{rel}: wildcard CORS origin committed")
        if PUBLIC_SECRET_RE.search(text):
            problems.append(f"{rel}: NEXT_PUBLIC_* assigned secret-looking material")
        if _is_allowed(rel):
            continue
        # The implementation and its validator legitimately name the value.
        enforcement_files = {"frontend/lib/server/cloudRunAuth.ts", "backend/production_config.py"}
        if rel not in enforcement_files and TEST_ADAPTER_RE.search(text):
            problems.append(f"{rel}: test-only CLOUD_RUN_AUTH_MODE=e2e-test configured outside the isolated E2E stack")
        for flag, pattern in ENABLE_RE.items():
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{rel}:{line}: execution flag {flag} is enabled by default")

    if problems:
        print("unsafe default check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("unsafe default check passed (all execution flags default-off, no wildcard CORS, no public secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
