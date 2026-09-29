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
    # The independent catalog switch. It must never be committed as enabled:
    # the Government tool and the canonical promotion pipeline are built from
    # it, and a repository default is not a deliberate operator decision.
    "MILO_ENABLE_CATALOG_EXECUTION",
    # Read and promotion are separate capabilities under that switch, and
    # neither may be committed as enabled.
    "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
    "MILO_ENABLE_CATALOG_PROMOTION",
    # Mapping Plan writes. Drafts only, and still never enabled by a default.
    "MILO_ENABLE_WORK_SCOPE_MUTATIONS",
    # Mapping Plan batch runs: starting one batch creates a paid run, so it is
    # never enabled by a default either.
    "MILO_ENABLE_WORK_SCOPE_BATCHES",
    # Mapping Plan preparation: scoped Government captures. Capture job only,
    # turned on per execution by an explicit operator command, never a default.
    "MILO_ENABLE_WORK_SCOPE_PREPARATION",
    # E': the website's Prepare route (executes the capture job once for one
    # plan revision). Never a default; one explicit activation step turns it on.
    "MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS",
    # PR-D1: register capture from the website (API), and the capture job's
    # per-execution register switch. Never a default.
    "MILO_ENABLE_REGISTER_CAPTURE",
    "MILO_ENABLE_REGISTER_CAPTURE_JOB",
    "GATEWAY_ALLOW_EXECUTION_ROUTES",
    "GATEWAY_ALLOW_RUN_START_ROUTES",
    "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI",
    # Test-only adapters must never be switched on outside the isolated
    # E2E stacks.
    "MILO_E2E_INPROCESS_WORKER",
    # PR-Y: the replay capture keeps provider outputs on a run's checkpoints.
    # Never committed on; an operator turns it on explicitly for one job.
    "MILO_CAPTURE_REPLAY",
]

# PR-Y: the replay capture must be PINNED OFF by every deploy script, on every
# surface it deploys, through the shared contract array (or literally). A
# script that stops pinning it fails this scan.
REPLAY_CAPTURE_FLAG = "MILO_CAPTURE_REPLAY"
REPLAY_CAPTURE_PIN_ARRAY = "MILO_REPLAY_CAPTURE_PINNED_OFF"
REPLAY_CAPTURE_PINS = {
    # script -> how many surfaces it must pin (each is one reference)
    "scripts/deploy/deployment-contract.sh": 1,
    "scripts/deploy/cloud-run.sh": 2,
    "scripts/deploy/staging-cloud-run.sh": 2,
    "scripts/deploy/website-execution-activate.sh": 2,
    "scripts/catalog/government-production-capture.sh": 1,
    "scripts/release/generate-deployment-plan.sh": 1,
}
REPLAY_CAPTURE_KILL_SWITCH = "scripts/deploy/kill-switch.sh"

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
    # The Stage B staging deployer enables run creation/cancellation for the
    # mocked zero-cost lifecycle in the ISOLATED staging project only: it
    # hard-refuses the production project id, pins paid execution off, and
    # binds no provider key (require_staging_isolation /
    # require_no_provider_key_in_staging_config in the script itself).
    "scripts/deploy/staging-cloud-run.sh",
    "scripts/staging/",
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


def replay_capture_pin_problems(repo: Path = REPO) -> list[str]:
    """Every deploy script that no longer pins MILO_CAPTURE_REPLAY off."""
    problems: list[str] = []
    contract = (repo / "scripts/deploy/deployment-contract.sh").read_text(errors="ignore")
    block = contract.split(f"{REPLAY_CAPTURE_PIN_ARRAY}=(", 1)[-1].split(")", 1)[0]
    if f"{REPLAY_CAPTURE_FLAG}=false" not in block or "=true" in block:
        problems.append("scripts/deploy/deployment-contract.sh: "
                        f"{REPLAY_CAPTURE_PIN_ARRAY} must pin {REPLAY_CAPTURE_FLAG}=false")
    for rel, surfaces in REPLAY_CAPTURE_PINS.items():
        text = (repo / rel).read_text(errors="ignore")
        found = (text.count(f"${{{REPLAY_CAPTURE_PIN_ARRAY}[@]}}")
                 + text.count(f"${{{REPLAY_CAPTURE_PIN_ARRAY}[*]}}")
                 + text.count(f"{REPLAY_CAPTURE_FLAG}=false"))
        if found < surfaces:
            problems.append(f"{rel}: {REPLAY_CAPTURE_FLAG} is not pinned off on every "
                            f"surface ({found} of {surfaces})")
    kill = (repo / REPLAY_CAPTURE_KILL_SWITCH).read_text(errors="ignore")
    if kill.count("$MILO_REPLAY_CAPTURE_FLAG_NAME") < 2:
        problems.append(f"{REPLAY_CAPTURE_KILL_SWITCH}: the kill switch must close "
                        f"{REPLAY_CAPTURE_FLAG} on the API and the worker")
    return problems


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

    problems.extend(replay_capture_pin_problems())
    if problems:
        print("unsafe default check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("unsafe default check passed (all execution flags default-off, no wildcard CORS, no public secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
