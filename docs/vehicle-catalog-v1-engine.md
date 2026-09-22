# vehicle_catalog_v1 extracted engine

Stage 2 extracts the preserved MILO vehicle catalog pipeline into `backend/engines/vehicle_catalog_v1/` so it can run outside the Streamlit UI while retaining the baseline behavior documented in `docs/current-milo-baseline.md`.

## Modules

- `core.py` preserves the original constants, prompts, schemas, tool loop, validators, discovery merge, normalizer, technical enrichment, verifier merge, deterministic Python final builder, and Hebrew summary functions.
  - **Search is the one deliberate departure.** The loop no longer answers a builtin `$web_search` tool call by echoing its arguments back to Moonshot (which is what asked the provider to run and bill the search). V1 offers MILO's own `web_search` function tool, and each invocation the model asks for is admitted, performed and accounted by the one provider authority before it runs — see `docs/production-readiness/PROVIDER_AUTHORITY.md`. Search still reaches the live internet and the results still return into the same conversation; only the number of searches became something MILO can refuse.
- `engine.py` provides normal Python orchestration with `VehicleCatalogRunConfig`, lifecycle event sink callbacks, token accounting, injectable model client factory, and injectable sleep function for retry tests.
- `adapter.py` exposes `VehicleCatalogV1Adapter` for worker usage. It is handed the run's `VehicleCatalogScope` (manufacturer, market, period) by the worker — the scope the API bound into the run from the project's configuration (`backend/vehicle_catalog_scope.py`, see `docs/website-integration.md` §1) — builds the configuration from it, invokes the engine, and returns a structured result. It reads no scope from run input and has no default: an adapter without a scope refuses with `VEHICLE_CATALOG_SCOPE_MISSING`. Direct callers of `VehicleCatalogEngine` keep `VehicleCatalogRunConfig`'s defaults.

## Unchanged behavior

The extracted core preserves the Moonshot base URL, `kimi-k2.6`, temperature `0.6`, disabled thinking payload, maximum tool rounds, Kimi concurrency semaphore, retry policy, token budgets, discovery and technical agent definitions, technical chunk size `4`, verifier chunk size `6`, mandatory web-search enforcement (now naming MILO's own `web_search` tool), truncation/loop detection, schema repairs, fallback behavior, partial-failure policies, deterministic Python final builder, and Hebrew summary behavior.

## Retained limitations

- The engine still depends on Kimi/Moonshot-compatible response shapes.
- Live web-search/model calls require an API key and must not be run by automated tests.
- Verifier and summary failures continue to degrade to partial output instead of aborting, matching the baseline.
- The core intentionally keeps the preserved single-file helper implementations to avoid silently replacing pipeline logic.

## Fake client usage

Tests can pass a fake `model_client_factory` to `VehicleCatalogEngine` or `VehicleCatalogV1Adapter` (the adapter additionally needs an explicit `scope=VehicleCatalogScope(...)`). The factory receives `(api_key, base_url)` and must return an object with `chat.completions.create(**kwargs)`. Retry timing can be controlled with `sleep_fn=lambda seconds: None`.
