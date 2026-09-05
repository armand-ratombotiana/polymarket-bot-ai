# W34-2 worklog

## Goal
Add backfill progress tracking to IngestionHealthPanel: an active-jobs table
with progress %, ETA, and a per-job progress bar; a completed-backfill
summary (last 10 jobs); a Start-Backfill trigger form (type, optional
token_id, days) that POSTs to a new
`POST /api/ingestion/backfill/trigger` endpoint; and three new backend
endpoints (`/active`, `/history`, `/trigger`) that the panel polls.

## Plan
1. Backend `ingestion/backfill.py`:
   - Add `BackfillJob` dataclass (task_id, type, market_token, days,
     resolution, resume, started_at, ended_at, status, error_message,
     expected_total, stats) with `progress_pct` + `eta_seconds` properties
     computed live from `stats.total_processed`.
   - Add `BackfillEngine._active_jobs: dict[str, BackfillJob]` +
     `start_job(task_type, ...)` (kicks off asyncio task, registers job,
     auto-removes after 60s post-completion).
   - Add `list_active_jobs() -> list[dict]` + `list_history(limit=10)`.
   - Add `stats: BackfillStats | None = None` parameter to every
     `backfill_*` method + `run()` so the start_job task can pass a shared
     stats object whose `total_processed` is updated in place by the
     running backfill (live-readable progress).
2. Backend `api/server.py`: append three new routes
   (`GET /api/ingestion/backfill/active`,
   `GET /api/ingestion/backfill/history`,
   `POST /api/ingestion/backfill/trigger`).
3. Frontend `IngestionHealthPanel.tsx`: extend `fetchAll` to also poll
   the active + history endpoints; add a Backfill Progress section
   (active jobs table with progress bar + ETA, history table, trigger
   form with type/token/days inputs).
4. Tests: 6 new tests covering active-jobs render, progress bar,
   trigger POST, success banner, empty states.

## Result

### Backend — `mini-services/polymarket-bot/ingestion/backfill.py`
- Added `BackfillJob` dataclass with `progress_pct` / `eta_seconds` /
  `elapsed_seconds` properties derived live from the shared
  `BackfillStats` instance (mutated in place by the running backfill).
- Added `BackfillEngine._active_jobs: dict[str, BackfillJob]` field
  (single-threaded event loop — dict ops are atomic without a lock).
- Added `BackfillEngine._estimate_expected_total(bt)` — heuristic
  expected-total estimate per backfill type so progress % stays
  honest (≤ 100%) and ETA remains finite.
- Added `BackfillEngine.start_job(task_type, *, market_token, days,
  resolution, resume)` — registers a `BackfillJob` in `_active_jobs`,
  kicks off the asyncio task, auto-removes the job 60s after
  completion. The task passes a shared `BackfillStats` object via
  `stats_sink={bt.value: shared_stats}` so `list_active_jobs` can read
  live progress.
- Added `BackfillEngine.list_active_jobs() -> list[dict]` — returns
  job dicts ordered by `started_at` (oldest first).
- Added `BackfillEngine.list_history(limit=10)` — thin wrapper around
  `BackfillStore.list_runs`.
- Modified every `backfill_*` method (metadata / prices / trades /
  outcomes / snapshots) to accept `stats: BackfillStats | None = None`.
  When supplied, the method mutates that stats object in place rather
  than constructing a fresh one — backward-compatible with direct
  callers (the existing `POST /api/ingestion/backfill/markets` /
  `POST /api/ingestion/backfill/prices/{token_id}` fire-and-forget
  endpoints continue to work unchanged).
- Modified `BackfillEngine.run()` to accept `stats_sink:
  dict[str, BackfillStats] | None` and pass the matching stats object
  to each sub-method.
- For `BackfillType.ALL` in `start_job`, the shared stats object is
  passed to every phase so totals accumulate; each phase still calls
  `record_run` with its own correct type (set via `stats.type` at the
  top of each sub-method).

### Backend — `mini-services/polymarket-bot/api/server.py`
Three new routes appended after the existing
`GET /api/ingestion/backfill/status` endpoint. All under the
`ingestion` tag, GETs rate-limited by `READ_LIMIT`, POST by
`WRITE_LIMIT`.

| Endpoint | Purpose | Source of truth |
|---|---|---|
| `GET /api/ingestion/backfill/active` | active jobs (live progress + ETA) | `backfill_engine.list_active_jobs()` |
| `GET /api/ingestion/backfill/history?limit=10` | completed-backfill summary (ledger) | `backfill_engine.list_history(limit)` |
| `POST /api/ingestion/backfill/trigger?backfill_type=…&token_id=…&days=…` | kick off a tracked backfill job | `backfill_engine.start_job(...)` |

Trigger endpoint validates `backfill_type` via `BackfillType.parse`
→ HTTP 422 on unknown values (mirrors the existing error path used
by the W33 backfill CLI).

### Frontend — `src/components/IngestionHealthPanel.tsx`
- Added new types: `BackfillTypeValue`, `BackfillJobStatus`,
  `BackfillJob`, `BackfillActivePayload`, `BackfillHistoryEntry`,
  `BackfillHistoryPayload`, `BackfillTriggerResult` (mirror the
  backend JSON shapes).
- Added new constants: `BACKFILL_ACTIVE_ENDPOINT`,
  `BACKFILL_HISTORY_ENDPOINT`, `BACKFILL_TRIGGER_ENDPOINT`,
  `BACKFILL_TYPE_OPTIONS`.
- Extended `fetchAll` to also poll the two new GET endpoints (so
  the panel now polls 7 endpoints in parallel on the same 15s
  cadence — each endpoint that 4xx/5xxes is swallowed to `null`
  and the affected section shows its "endpoint unavailable"
  fallback).
- Added `handleTriggerBackfill` callback — POSTs to the trigger
  endpoint with the form state (type, optional token_id, days) as
  URL query params (matching the backend's `Query(...)` declaration),
  then re-fetches the active list so the new job appears
  immediately rather than waiting for the next poll tick.
- Added new state: `backfillActive`, `backfillHistory`,
  `triggerResult`, `triggerError`, `triggering`, `triggerType`,
  `triggerTokenId`, `triggerDays`.
- New imports: `Input`, `Select` (+ sub-components), `Play`, `History`,
  `Timer` icons.
- Added a "Backfill Progress" `SectionCard` placed right after the
  Source Health card. Layout:
  - Trigger form: `Select` for backfill type (metadata / prices /
    trades / outcomes / snapshots / all), `Input` for optional
    token_id, `Input` (number, 1–365) for days, "Trigger Backfill"
    button.
  - Trigger success / error banner inline (result includes the
    short task_id prefix + type so the operator can correlate).
  - Active jobs table: type badge (color-coded by status), market
    (truncated token_id), progress bar + % value, ETA, processed /
    expected, errors, started (relative). Per-row `data-testid`
    attributes (`backfill-active-row-{i}`,
    `backfill-active-progress-bar-{i}`,
    `backfill-active-progress-pct-{i}`,
    `backfill-active-status-{i}`).
  - Completed backfills summary: 5-column table (type, started→ended
    window with duration, added, skipped, errors). Color-coded
    error column (green / amber / red).
- Updated the footer to list the three new endpoints.

### Tests — `src/components/IngestionHealthPanel.test.tsx`
6 new tests appended within the existing `describe('IngestionHealthPanel')`
block, bringing the suite from 29 → 35 tests. Extended the existing
`mockFetchAllIngestion` and `mockFetchRouteGetPost` helpers with
optional `backfillActive?` / `backfillHistory?` payload fields so
existing tests that don't pass them continue to work (the new
endpoints 404 → the "endpoint unavailable" fallback renders).

| # | Test |
|---|---|
| 1 | "renders the backfill progress card with active jobs and history" — verifies the card renders, both active-job rows render, type badges show correct values, history list renders |
| 2 | "renders per-job progress bars and progress % values" — verifies the progress % values match the payload (41.7%, 15.0%) and the ETA renders in the row text |
| 3 | "renders the 'No active backfill jobs.' empty state when active list is empty" |
| 4 | "renders the 'No completed backfill runs yet.' empty state when history is empty" |
| 5 | "fires POST /api/ingestion/backfill/trigger when the Trigger button is clicked" — verifies the POST fires, URL contains `/api/ingestion/backfill/trigger`, and form state (default type=metadata, days=30) is encoded as query params |
| 6 | "shows the trigger success banner after the POST succeeds" — verifies the success banner renders with the task_id prefix and type |

### Verification
- `cd /home/z/my-project && bun run lint` — clean (exit 0, no warnings,
  no errors).
- `bun run test` — **64 test files / 1242 tests, all passing** (was
  64 / 1236 before W34-2 → +6 new tests in the existing
  IngestionHealthPanel.test.tsx suite).

### Files touched

| Path | Change |
|---|---|
| `mini-services/polymarket-bot/ingestion/backfill.py` | Added `BackfillJob` dataclass; added `BackfillEngine._active_jobs` field + `_estimate_expected_total` / `start_job` / `list_active_jobs` / `list_history` methods; added `stats: BackfillStats \| None = None` parameter to every `backfill_*` method + `stats_sink` parameter to `run()`. |
| `mini-services/polymarket-bot/api/server.py` | Appended 3 new routes (`GET /api/ingestion/backfill/active`, `GET /api/ingestion/backfill/history`, `POST /api/ingestion/backfill/trigger`) + a documentation header block. |
| `src/components/IngestionHealthPanel.tsx` | Added new types + constants + state + `handleTriggerBackfill` callback + extended `fetchAll` + added the "Backfill Progress" `SectionCard` (trigger form + active-jobs table + completed-backfills table) + updated footer. |
| `src/components/IngestionHealthPanel.test.tsx` | Extended `mockFetchAllIngestion` + `mockFetchRouteGetPost` with optional `backfillActive` / `backfillHistory` payload fields; added 5 new sample payloads; added 6 new tests. |

### Notes / trade-offs
1. **Heuristic expected_total.** The progress % is computed against a
   heuristic upper bound (`max_pages * page_size` for page-based
   backfills; `len(_collect_market_tokens())` for token-fan-out
   backfills). The actual number of markets / trades can be less, so
   the progress % can saturate at 100% before the backfill finishes.
   This is honest (we never exceed 100%); a future task could add a
   real "discovered count" telemetry hook so progress % reflects
   actual completion.
2. **Live progress via shared BackfillStats.** The active-jobs
   progress % is computed live from the shared `BackfillStats`
   object that the running `backfill_*` method mutates in place
   (via the new `stats` parameter). This avoids instrumenting every
   per-market / per-trade / per-page operation — the engine just
   mutates the same `stats.total_processed` counter it always has,
   and `list_active_jobs` reads it at poll time.
3. **60s post-completion cleanup window.** The active job stays in
   `_active_jobs` for ~60s after completion so the operator sees the
   final success/failure state in the UI before it disappears. The
   durable record lives in `backfill_runs` (read by
   `GET /api/ingestion/backfill/history`).
4. **Trigger endpoint uses Query params, not JSON body.** Matches the
   existing `POST /api/ingestion/backfill/markets` and
   `POST /api/ingestion/backfill/prices/{token_id}` conventions —
   consistent for an operator who already knows the existing surface.
5. **Existing fire-and-forget endpoints unchanged.** The
   `POST /api/ingestion/backfill/markets` /
   `POST /api/ingestion/backfill/prices/{token_id}` endpoints continue
   to work as before (they call `backfill_engine.backfill_metadata` /
   `backfill_prices` directly, not via `start_job`, so they don't
   register in `_active_jobs`). The new `trigger` endpoint is the
   primary surface for tracked backfills; the old endpoints are kept
   for backward compatibility.
6. **No new sidebar entry / page.tsx wiring.** The W34-2 task extends
   the existing `IngestionHealthPanel` (already wired by W31-5); no
   new route or nav item is needed.
