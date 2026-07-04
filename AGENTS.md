# Repository instructions

- `legacy/milo-streamlit-v1` is an immutable reference implementation.
- Never modify files under `legacy/` unless the user explicitly authorizes a legacy bug fix.
- Preserve the current MILO pipeline logic, prompts, schemas, model settings, token budgets, chunk sizes, fallback behavior, concurrency limits, validation guards, deterministic merge logic, and partial-failure behavior.
- Future implementation must wrap or extract the current logic, not silently replace it.
- Never commit API keys, `.env` files, service-account JSON files, Supabase secret keys, or credentials.
- Never perform paid API calls unless explicitly authorized.
- Never run `test_websearch.py` automatically because it makes real Moonshot/Kimi API calls.
- Make changes through small, reviewable pull requests.
- Report every test command that was executed and its exact result.
- Do not claim that tests passed if they were not run successfully.
