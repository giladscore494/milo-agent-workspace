# vehicle_catalog_v1 extracted engine

Stage 2 extracts the preserved MILO vehicle catalog pipeline into `backend/engines/vehicle_catalog_v1/` so it can run outside the Streamlit UI while retaining the baseline behavior documented in `docs/current-milo-baseline.md`.

## Modules

- `core.py` preserves the original constants, prompts, schemas, Kimi/Moonshot `$web_search` loop, validators, discovery merge, normalizer, technical enrichment, verifier merge, deterministic Python final builder, and Hebrew summary functions.
- `engine.py` provides normal Python orchestration with `VehicleCatalogRunConfig`, lifecycle event sink callbacks, token accounting, injectable model client factory, and injectable sleep function for retry tests.
- `adapter.py` exposes `VehicleCatalogV1Adapter` for worker usage. It accepts run input, builds configuration, invokes the engine, and returns a structured result.

## Unchanged behavior

The extracted core preserves the Moonshot base URL, `kimi-k2.6`, temperature `0.6`, disabled thinking payload, maximum tool rounds, Kimi concurrency semaphore, retry policy, token budgets, discovery and technical agent definitions, technical chunk size `4`, verifier chunk size `6`, mandatory web-search enforcement, truncation/loop detection, schema repairs, fallback behavior, partial-failure policies, deterministic Python final builder, and Hebrew summary behavior.

## Retained limitations

- The engine still depends on Kimi/Moonshot-compatible response shapes.
- Live web-search/model calls require an API key and must not be run by automated tests.
- Verifier and summary failures continue to degrade to partial output instead of aborting, matching the baseline.
- The core intentionally keeps the preserved single-file helper implementations to avoid silently replacing pipeline logic.

## Fake client usage

Tests can pass a fake `model_client_factory` to `VehicleCatalogEngine` or `VehicleCatalogV1Adapter`. The factory receives `(api_key, base_url)` and must return an object with `chat.completions.create(**kwargs)`. Retry timing can be controlled with `sleep_fn=lambda seconds: None`.
