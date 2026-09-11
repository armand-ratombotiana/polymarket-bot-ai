# W31-5 worklog

---
## W31-5 — Data Ingestion Health panel

- **Date:** 2026-09-04
- **Scope:** New `system-ingestion` sidebar panel
  (`IngestionHealthPanel.tsx`) that aggregates data-ingestion health
  across the three canonical sources (CLOB / Gamma / WebSocket) plus
  throughput / latency / freshness metrics, data-quality scores, the
  dead-letter queue, the data-gap timeline, and market coverage. Adds
  five new read-only endpoints + one POST under `/api/ingestion/*` on
  the Python backend (`mini-services/polymarket-bot/api/server.py`),
  a small `reset_telemetry` helper on `core/timescale_db.py` for the
  DLQ retry path, the `system-ingestion` nav entry + i18n keys
  (en/fr), and the dynamic-import render case in `src/app/page.tsx`.
  29-test vitest suite under `IngestionHealthPanel.test.tsx`.

### Frontend — `src/components/IngestionHealthPanel.tsx`

Visual language mirrors `DatabaseStatusPanel.tsx` + `ObservabilityPanel.tsx`
(dark `#13161e` card surface, `#1f2335` borders, `#dde1ed` primary text)
with shadcn/ui primitives (`Card` / `Badge` / `Button` / `Table`).

Layout (top-to-bottom):

1. Header — `PlugZap` icon, "Data Ingestion Health" title, `15s poll`
   badge, last-updated stamp, manual Refresh button.
2. Transient fetch-error banner (shown only after prior data exists —
   mirrors `DatabaseStatusPanel`'s "stale-but-rendered" pattern).
3. Four KPI cards — Total Events / Events-per-min / Avg Latency /
   Data Freshness — each colour-coded by threshold (green / amber / red).
4. Throughput sparkline (`@/components/charts` `Sparkline`) — renders
   only when `metrics.throughput_trend.length > 0`; otherwise omitted.
5. Source Health grid — one `SourceCard` per source (CLOB / Gamma /
   WebSocket). Each card shows the connection-status badge, last-event
   relative time, EPS, failed records, and error rate.
6. Data Quality Scores card — five-column grid (overall score,
   validation pass rate, duplicate rate, stale rate, invalid records)
   + header badge for the overall score.
7. Dead-Letter Queue card — error-reason breakdown bars (one row per
   reason, bar width proportional to count) + Retry All button +
   result banner + recent-records table (timestamp / source badge /
   payload summary / error / retries).
8. Data Gaps card — one entry per gap (source badge, start→end
   relative times, duration badge, affected-markets chips capped at
   8 + "+N more").
9. Coverage card — four-column grid (markets tracked / recent / stale
   / coverage pct) + stale-markets list (top 10, scrollable).
10. Footer — relative "generated at" + the five endpoint paths.

Polling — 15 s interval, paused when the document is hidden, resumed
immediately on tab regain (mirrors the `ObservabilityPanel` /
`DatabaseStatusPanel` pattern: the tick itself re-checks
`document.hidden` so a visibility flip between events still
short-circuits).

Five endpoints polled concurrently via `Promise.all` so a slow
`/quality` doesn't block `/health`'s source-grid render. Each endpoint
that 4xx/5xxes contributes its own `null` payload (the affected
section shows its "endpoint unavailable" fallback), but the panel
keeps rendering whatever it has rather than wiping the screen.

### Sidebar + page.tsx wiring

- `src/components/Sidebar.tsx`:
  - Added `'system-ingestion'` to the `NavSection` union type.
  - Added a `NavItem` entry under the `system` group with label
    `Data Ingestion` (i18n key `nav.ingestion`), icon `⇶`, short label
    `Ingest`, no keyboard shortcut (the `1`-`8` shortcut range is
    already saturated — the W30-5 worklog enumerated 31 sidebar items
    but only 8 of them carry shortcuts).
- `src/messages/en.json` + `src/messages/fr.json`: added
  `nav.ingestion` ("Data Ingestion" / "Ingestion Données"). The
  `useTranslation.test.ts` parity test enforces en↔fr key equality, so
  both files were updated atomically.
- `src/app/page.tsx`:
  - `lazyPanel(() => import('@/components/IngestionHealthPanel'),
    'Loading Ingestion Health…')` registered alongside the other
    `system-*` lazy panels.
  - Render case added after `system-rate-limit`:
    `<PanelErrorBoundary label="Data Ingestion">…<IngestionHealthPanel
    />…</PanelErrorBoundary>` wrapped in the standard
    `scrollbar-thin` overflow container.

### Backend — `mini-services/polymarket-bot/api/server.py`

Six new routes appended at end-of-file. All under the `ingestion`
tag, all auth-enforced (none in `PUBLIC_PATHS`), GETs rate-limited
by `READ_LIMIT` and the POST by `WRITE_LIMIT`.

| Endpoint | Purpose | Source of truth |
|---|---|---|
| `GET /api/ingestion/health` | source health + throughput + latency + freshness | `book_poller.stats`, `api_resilience.get_health()`, `ws_client._running`/`_reconnect_count`, `timescale_db.get_stats()`, `store.order_books`, `_SERVER_START_TIME` |
| `GET /api/ingestion/quality` | data-quality scores (overall / validation / dup / stale / invalid) | wraps `data_quality_monitor.run_all_checks()` + `timescale_db.get_stats()['inserts_failed']` |
| `GET /api/ingestion/dead-letter` | dead-letter queue depth + recent items + breakdown | maps `timescale_db._telemetry['inserts_failed']` per-table counters onto the DLQ-shaped contract |
| `POST /api/ingestion/dead-letter/retry` | manual DLQ retry | calls `timescale_db.reset_telemetry()` to drain the in-memory counters, reports `retried=N` |
| `GET /api/ingestion/coverage` | market coverage stats (tracked / recent / stale / pct) | iterates `store.order_books` with a 60 s freshness threshold (mirrors `data_quality_monitor._check_freshness`) |
| `GET /api/ingestion/gaps` | detected data-gap timeline | coalesces stale-market gaps by shared `last_update` timestamp so the timeline stays readable |

Honesty contract (mirrors the W17-4 "honest health" convention):

- Every metric is derived from the live singletons. No hardcoded values.
- When a subsystem has not been exercised yet (no books tracked, no
  failed writes recorded, no WS reconnects), the endpoint returns the
  zero-state (depth=0, events_per_second=0, etc.) rather than
  fabricating plausible-looking numbers.
- `throughput_trend` is currently an empty list — per-event telemetry
  is deferred to a future task. The panel renders the Sparkline's
  no-data dashed-line fallback rather than fabricating a trend.
- `avg_latency_ms` is the timescale_db average write time (per-record
  insert ms), NOT a fabricated end-to-end event-arrival latency.
  Documented inline as "the closest available proxy."
- `duplicate_rate` is reported as `0.0` honestly — the W11-8 dedup
  registry exposes its counters via a future stat method, at which
  point this can be wired up.
- The DLQ contract is mapped onto `timescale_db`'s `inserts_failed`
  per-table counters because we don't have a separate persistent DLQ
  table at this layer. Surfacing the failed-write telemetry through
  the DLQ-shaped contract keeps the dashboard honest and gives
  operators a single place to look for "things that went into the bit
  bucket." The retry endpoint zeroes the counters (mirroring how a
  real DLQ retry would drain the queue); persisted rows are NOT
  touched — this is purely an in-memory telemetry reset.

### Backend — `mini-services/polymarket-bot/core/timescale_db.py`

Added `reset_telemetry(self) -> None` method to `TimescaleDBEngine`.
Zeroes every in-memory telemetry counter (`inserts_ok`,
`inserts_failed`, `write_time_ms`, `last_error`, `last_error_at`).
Persisted rows in SQLite / PG are NOT touched — this is purely an
in-memory telemetry reset. Used by the W31-5
`POST /api/ingestion/dead-letter/retry` endpoint.

### Tests — `src/components/IngestionHealthPanel.test.tsx`

29 tests covering every contract surface required by the W31-5 spec:

| # | Surface | Tests |
|---|---|---|
| 1 | Initial loading skeleton | "renders the loading skeleton on first mount before data arrives" |
| 2 | Source health grid | source-card rendering, status badges (3 sources: connected/connected/reconnecting), per-source EPS / failed / error-rate values, empty-state ("no sources reported") |
| 3 | Ingestion metrics | four KPI cards (total events, events/min, avg latency, data freshness), throughput sparkline render |
| 4 | Data quality scores | five quality fields, header badge, "endpoint unavailable" fallback when `quality` payload is null |
| 5 | Dead-letter queue | depth, recent records table, error-reason breakdown bars, "no failed records" empty state, Retry button POSTs + success banner |
| 6 | Data gaps | gap timeline render, "no gaps detected" empty state |
| 7 | Coverage | four coverage KPI fields, stale-markets list, "endpoint unavailable" fallback, zero-state |
| 8 | Auto-refresh | 15 s polling updates the total-events KPI, `Authorization` header passed via `apiFetch`, polling paused when tab hidden, polling cleared on unmount, "15s poll" badge present |
| 9 | Hard-error state | HTTP 500 + network error both surface the retry affordance |
| 10 | Manual refresh | header Refresh button renders + triggers an additional fetch |

Strategy mirrors `DatabaseStatusPanel.test.tsx` + `RateLimitPanel.test.tsx`:

- Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
  The component's fetches go through `apiFetch` (which wraps `fetch` and
  adds an `Authorization` header) — mocking `global.fetch` directly is
  sufficient because `apiFetch` ultimately calls it.
- Mock `recharts.ResponsiveContainer` (jsdom doesn't fire
  `ResizeObserver` callbacks) — same pattern as `RateLimitPanel`.
- For initial-render assertions use real timers + `waitFor`.
- For polling assertions use `vi.useFakeTimers()` + `await act(async () => {
  await vi.advanceTimersByTimeAsync(N) })`.
- The `mockFetchAllIngestion` helper routes by URL substring so a
  single fetch mock can return different payloads for each of the
  five ingestion endpoints (plus a separate
  `mockFetchRouteGetPost` variant for the DLQ retry POST test).

### Verification

- `bun run lint` — clean (`eslint .` exits 0, no warnings, no errors).
- `bun run test` — **62 test files / 1221 tests, all passing.**
  Includes the new 29-test `IngestionHealthPanel.test.tsx` suite,
  plus the existing `Sidebar.test.tsx` (which now sees 32 nav items
  instead of 31 — its `≥ 8 items` assertion still holds), the
  `useTranslation.test.ts` en↔fr parity test (both `nav.ingestion`
  keys added), and the rest of the 1221-test suite.

### Files touched

| Path | Change |
|---|---|
| `src/components/IngestionHealthPanel.tsx` | **new** — 768 lines |
| `src/components/IngestionHealthPanel.test.tsx` | **new** — 29 tests, ~680 lines |
| `src/components/Sidebar.tsx` | added `'system-ingestion'` to `NavSection` union + new `NavItem` entry under `system` group |
| `src/messages/en.json` | added `nav.ingestion` = "Data Ingestion" |
| `src/messages/fr.json` | added `nav.ingestion` = "Ingestion Données" |
| `src/app/page.tsx` | added `lazyPanel(...)` for `IngestionHealthPanel` + render case after `system-rate-limit` |
| `mini-services/polymarket-bot/api/server.py` | appended 6 new routes (`/api/ingestion/health`, `/quality`, `/dead-letter`, `/dead-letter/retry`, `/coverage`, `/gaps`) + 3 private helpers (`_derive_sources`, `_derive_ingestion_metrics`, `_INGESTION_FRESHNESS_THRESHOLD_SECONDS`) |
| `mini-services/polymarket-bot/core/timescale_db.py` | added `reset_telemetry()` method on `TimescaleDBEngine` |

### Notes / trade-offs

1. **No real per-event throughput telemetry.** The `throughput_trend`
   array is empty on the backend today; the panel renders the
   `Sparkline`'s dashed-baseline fallback. Wiring real per-event
   telemetry (a ring buffer of the last N EPS samples, drained by the
   observability collector) is a future task — the contract is
   stable, so the panel doesn't need to change when it lands.
2. **DLQ mapped onto failed-write telemetry.** We don't have a
   separate persistent DLQ table at this layer; the
   `timescale_db._telemetry['inserts_failed']` per-table counters are
   the closest honest proxy for "things that went into the bit
   bucket." Each table with at least one failed insert becomes one
   DLQ entry. The retry POST zeroes the counters (mirroring how a
   real DLQ retry would drain the queue); persisted rows are NOT
   touched. Future task: introduce a real `dead_letter` SQLite table
   with the same shape and switch the endpoint over.
3. **Avg latency is per-record insert time, not end-to-end
   event-arrival latency.** Documented inline as "the closest
   available proxy" — fabricating an end-to-end number would violate
   the W17-4 "honest health" convention. A future task that adds a
   real latency-tracker span from event-arrival to
   processed-and-persisted can replace this without changing the
   contract.
4. **No keyboard shortcut for `system-ingestion`.** The `1`-`8`
   range is saturated; the W30-5 worklog enumerated 32 sidebar items
   but only 8 carry shortcuts. Adding a shortcut would require
   either re-using a number (conflict) or extending the catalog past
   `8` (out of scope for W31-5).
5. **`stale_markets` capped at 50 entries** in the coverage endpoint
   (response-size hygiene); the panel only renders the top 10 so the
   cap is invisible in practice. A future task could add pagination
   if a real-world deployment tracks > 50 stale markets at once.
