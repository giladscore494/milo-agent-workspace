# Project workspace foundation

This stage adds a FastAPI control-plane package in `backend/`, a Supabase migration, and a worker boundary. It does **not** integrate or execute the preserved MILO engine.

## Structure

- `backend/main.py` — FastAPI app and control API endpoints.
- `backend/config.py` — environment-driven settings.
- `backend/repository/supabase.py` — repository protocol and Supabase implementation.
- `backend/worker/` — Cloud Run Job worker entry point and `vehicle_catalog_v1` adapter boundary.
- `supabase/migrations/001_project_workspace.sql` — idempotent project workspace migration and MILO project seed.
- `tests/` — offline pytest suite using mocked repositories; no Supabase or paid API calls.

## Required environment variables

Do not commit values for these variables:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `RUN_ID` (worker only; may also be passed as `--run-id`)
- `ENVIRONMENT` (optional label; defaults to `local`)

## Local API start

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

## Worker start

```bash
python -m backend.worker.main --run-id <uuid>
```

Before engine integration, the worker validates the run, records `ENGINE_NOT_INTEGRATED`, and fails the run rather than marking it completed.

## Offline tests

```bash
pytest tests
```

The offline tests mock all repository/Supabase behavior and must not run `test_websearch.py`.
