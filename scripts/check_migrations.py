"""Offline static migration check.

Validates that the migration files still contain the safety and
legacy-baseline reconciliation clauses required for the production Supabase
project (four pre-existing tables, empty migration history). This is text
matching only; executable validation lives in tests/test_migrations_postgres.py.
"""

from pathlib import Path

MIGRATIONS_DIR = Path("supabase/migrations")

REQUIRED_GLOBAL = [
    "enable row level security",
    "on conflict",
    "run_invocations",
]

# Reconciliation clauses that must never disappear before the first
# production apply. Keyed by migration filename.
REQUIRED_PER_FILE = {
    "001_project_workspace.sql": [
        # messages.sender_role -> messages.role reconciliation
        "sender_role",
        "rename column sender_role to role",
        "update public.messages set role = sender_role where role is null",
    ],
    "002_durable_runtime.sql": [
        # runs input/output/error/updated_at reconciliation
        "add column if not exists input jsonb",
        "add column if not exists output jsonb",
        "add column if not exists error jsonb",
        "add column if not exists updated_at timestamptz",
        "jsonb_build_object('content', user_prompt)",
        "set output = result",
        "error_message",
        "alter column user_prompt drop not null",
        "alter column input set not null",
        "alter column updated_at set not null",
        "runs_set_updated_at",
        # runs status check must not fail on legacy rows
        "not valid",
        # run_events integer progress preservation
        "rename column progress to progress_percent",
        "add column if not exists progress jsonb",
        "add column if not exists agent text",
        "add column if not exists phase text",
        "add column if not exists event_type text",
    ],
    "006_deployment_hardening.sql": [
        # stuck_runs depends on runs.updated_at added by 002
        "updated_at",
        "stuck_runs",
    ],
    "009_run_idempotency_lifecycle.sql": [
        # additive idempotency/lifecycle columns; status check stays defensive
        "add column if not exists requested_by",
        "add column if not exists launch_state",
        "create unique index if not exists runs_user_conversation_idempotency_uidx",
        "not valid",
    ],
}

FORBIDDEN_EVERYWHERE = [
    "drop table",
    "delete from public.conversations",
    "delete from public.messages",
    "delete from public.runs",
    "delete from public.run_events",
]


def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit("migration check failed; no migration files found")
    texts = {path.name: path.read_text().lower() for path in files}
    combined = "\n".join(texts.values())

    problems = []
    for item in REQUIRED_GLOBAL:
        if item not in combined:
            problems.append(f"missing globally: {item!r}")
    for name, clauses in REQUIRED_PER_FILE.items():
        if name not in texts:
            problems.append(f"missing migration file: {name}")
            continue
        for clause in clauses:
            if clause not in texts[name]:
                problems.append(f"{name}: missing required clause {clause!r}")
    for name, text in texts.items():
        for clause in FORBIDDEN_EVERYWHERE:
            if clause in text:
                problems.append(f"{name}: forbidden clause {clause!r}")

    if problems:
        raise SystemExit("migration check failed;\n  " + "\n  ".join(problems))
    print("migration check passed (static text validation only)")


if __name__ == "__main__":
    main()
