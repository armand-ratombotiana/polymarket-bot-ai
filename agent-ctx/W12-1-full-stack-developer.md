# W12-1 — Feature flags system

**Agent:** full-stack-developer
**Task ID:** W12-1
**Date:** 2026-09-04

## What was built

A runtime feature-flags system that lets an operator toggle features
without redeploying. Backend is SQLite-backed (in-memory cache + a
defensive singleton that doesn't crash on read-only filesystems);
frontend is a polling React hook with a 60s cadence and visibility-aware
pause.

## Files created

| File | Purpose |
|------|---------|
| `mini-services/polymarket-bot/core/feature_flags.py` | `FeatureFlagManager` + `DEFAULT_FLAGS` (13) + `register_routes(app)` registering 4 endpoints under `/api/flags`. |
| `mini-services/polymarket-bot/tests/test_feature_flags.py` | 21 tests: 11 unit (manager) + 10 API integration (TestClient). |
| `src/hooks/useFeatureFlags.ts` | React hook: fetches `/api/flags`, caches in state + ref, polls every 60s (configurable), visibility-aware, fail-safe `isEnabled`. |
| `src/hooks/useFeatureFlags.test.ts` | 8 vitest tests: loading, populate, isEnabled fail-safe, polling cadence, visibility, refresh, fetch-throw, non-200. |

## Files modified

| File | Change |
|------|--------|
| `mini-services/polymarket-bot/api/server.py` | Appended `_register_flag_routes(app)` block (lines ~3165–3178) using the alias-import pattern; additive, no existing routes touched. |
| `mini-services/polymarket-bot/tests/conftest.py` | Added `FLAGS_DB_PATH` to `_ENV_REDIRECTS` so the module-level singleton (`flag_manager = FeatureFlagManager()`) doesn't try to mkdir `/app/data` (read-only in sandbox) at import time. |

## Key implementation notes

### 1. `FlagUpdate` Pydantic model MUST be at module scope

The file uses `from __future__ import annotations` (PEP 563) — every
annotation is a string at runtime, and FastAPI resolves it by looking
up the handler's `__globals__` (the module namespace, NOT the
`register_routes` function's local namespace). Initially I declared
`FlagUpdate` inside `register_routes`; all 4 POST-route tests failed
with HTTP 422 "Field required" because FastAPI couldn't resolve the
annotation and fell back to treating `body` as a query parameter.
Moving the model to module scope fixed it.

### 2. Defensive singleton init mirrors `core.decision_ledger`

`FeatureFlagManager._init_db` swallows `OSError` (mkdir failure) and
`sqlite3.Error` (connect failure) with a `logger.warning` — so a
read-only `/app/data` doesn't crash module import. The cache stays
empty in that case; `is_enabled` returns `False` (fail-safe) until a
writable DB path is configured.

### 3. Frontend tests use REAL timers (not fake)

`waitFor` from `@testing-library/react` uses `setInterval` internally,
which fake timers pause — causing every polling-aware test to hang
until the 5s test timeout. Switched to real timers with
`pollIntervalMs: 100` and `await new Promise(r => setTimeout(r, 350))`
for the polling tests. Same caveat documented in
`useRealtimeData.test.ts`.

### 4. API tests swap `core.feature_flags.flag_manager` via monkeypatch

The route handlers close over the module global `flag_manager` (not a
snapshot), so swapping it with a fresh `FeatureFlagManager(db_path=tmp_path)`
is picked up by every handler at call time. Mirrors the `shadow_db`
fixture pattern in `tests/test_shadow_trading_api.py`.

## Verification

- Backend: `python -m pytest tests/test_feature_flags.py -v` → **21 passed in 0.56s**
- Frontend: `bun run test` → **274 passed** (was 274 pre-W12-1; +8 new useFeatureFlags tests; 0 failures)
- Frontend isolated: `bun run test src/hooks/useFeatureFlags.test.ts` → **8 passed in 1.38s**
- Lint: `bun run lint` → **exit 0**, no warnings
- Dev server: still healthy, `/` 200 in 28ms

## API surface added

```
GET  /api/flags                list all flags + their state/config
GET  /api/flags/{key}          get a single flag (404 if unknown)
POST /api/flags/{key}          update a flag (body: {enabled, config?})
POST /api/flags/{key}/reset    reset a flag to its default value
```

All four endpoints are auth-protected (not in `PUBLIC_PATHS`), tagged
`tags=["flags"]` for Swagger UI grouping.
