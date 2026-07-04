from pathlib import Path

required = ["enable row level security", "on conflict", "run_invocations"]
text = "\n".join(p.read_text().lower() for p in sorted(Path("supabase/migrations").glob("*.sql")))
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit(f"migration check failed; missing: {missing}")
print("migration check passed")
