# W21-7 — Database Status Panel

**Agent:** full-stack-developer
**Task ID:** W21-7
**Date:** 2026-09-04
**Scope:** Additive — 1 new component (`DatabaseStatusPanel.tsx`) + 1 new
test file (`DatabaseStatusPanel.test.tsx`) + Sidebar `NavSection` /
`page.tsx` render case / i18n keys for both `en.json` and `fr.json`.

## What was done

### New component: `src/components/DatabaseStatusPanel.tsx`

A 660+ line dashboard panel that surfaces the live database backend
(PostgreSQL primary vs SQLite fallback) and PG-pool health stats so
the operator can see at-a-glance whether the system is running on
the primary PG store or has fallen back to the SQLite standby, plus
how many times the fallback has fired, the row count / on-disk size
of each persisted table, and the last 5 connection errors.

Implements all 7 features required by the W21-7 spec:

1. **Backend indicator** — Large `Badge` in the header showing
   `PostgreSQL` (green, `bg-green-500/15 text-green-400`) when the
   primary PG pool is active, or `SQLite` (amber, `bg-amber-500/15
   text-amber-300`) when the system has fallen back. Includes a status
   dot + the icon `Database` (lucide-react).
2. **Connection health** — PG health grid (`Card` with five columns:
   Status · Uptime % · Avg Latency · Pool In-Use · Consecutive
   Failures). Each cell is colour-coded (green ≥ 99% uptime, amber
   ≥ 90%, red < 90%). A `HealthBadge` sub-component renders
   `Healthy` / `Degraded` / `Unhealthy` / `Unknown` with the matching
   variant (`success` / `warning` / `destructive` / `secondary`).
3. **Fallback counter** — KPI card showing the SQLite fallback count
   with adaptive colour (green = 0, amber < 5, red ≥ 5).
4. **Database tables** — `shadcn/ui Table` showing each table's name,
   database badge (`PG` / `SQLite`), row count (formatted with
   `toLocaleString`), on-disk size (formatted with a bytes helper
   that adapts to B/KB/MB/GB), and a relative-time last-modified
   stamp. Max height 288px (`max-h-72`) with a custom-thin scrollbar
   so a long table list scrolls inside the card.
5. **Recent errors** — Last 5 connection errors rendered as a
   vertical list of red `XCircle` + error message + relative-time
   + backend + retry-attempt count. Empty state shows a green
   `CheckCircle2` "No connection errors recorded" message.
6. **Manual retry button** — `Button` that fires
   `POST /api/system/db-retry` and renders a success (✓ green) or
   failure (✗ red) banner with the backend message + relative-time
   stamp. Re-fetches the status immediately on completion so the
   operator sees the post-retry state without waiting for the next
   poll tick.
7. **Auto-refresh** — Polls `GET /api/system/db-status` every 15 s,
   paused when the document is hidden. Mirrors the
   visibility-aware polling pattern from `RateLimitPanel.tsx` +
   `ObservabilityPanel.tsx` (the tick itself re-checks
   `document.hidden` AND the `visibilitychange` listener
   `stopPolling()` / `startPolling()`s on tab switch).

Visual language matches `SystemHealthView.tsx` +
`DatabaseExplorerView.tsx` (dark `#13161e` panel surface, `#1f2335`
borders, `#dde1ed` primary text, `#7e8aaa` secondary text) but uses
shadcn/ui primitives (`Card` + `CardContent` + `CardHeader` +
`CardTitle`, `Badge` with the `success` / `warning` / `destructive`
variants, `Table` + `TableHeader` + `TableRow` + `TableHead` +
`TableBody` + `TableCell`, `Button` with `outline` / `default`
variants) per the W21-7 spec.

### Backend contract (designed — see file header for the canonical spec)

```ts
// GET /api/system/db-status
interface DatabaseStatusPayload {
  backend: 'postgresql' | 'sqlite'
  pg_health: {
    status: 'healthy' | 'degraded' | 'unhealthy' | 'unknown'
    uptime_pct: number
    avg_latency_ms: number
    last_check_epoch: number
    consecutive_failures: number
    pool_size: number
    pool_in_use: number
  } | null
  fallback_counter: number
  tables: Array<{
    name: string
    row_count: number
    size_mb: number
    database: 'pg' | 'sqlite'
    last_modified: number
  }>
  recent_errors: Array<{
    timestamp: number
    error: string
    retry_attempt: number
    backend: string
  }>
  generated_at: number
}

// POST /api/system/db-retry
interface DatabaseRetryResult {
  success: boolean
  backend: string
  message: string
  attempted_at: number
}
```

The panel gracefully handles the case where the backend hasn't yet
implemented these endpoints: a 500 / network-error fetch surfaces the
`ErrorState` component with a retry button (`aria-label="Retry
database status fetch"`). When `pg_health` is `null`, the PG
Connection Health card renders an informational "PostgreSQL pool is
not configured — operating on the SQLite standby backend" message.

### Sidebar wiring (`src/components/Sidebar.tsx`)

- Added `'system-database-status'` to the `NavSection` union type.
- Added a new `NavItem` in the `system` group with `id:
  'system-database-status'`, `labelKey: 'nav.database_status'`,
  `label: 'Database'`, `shortLabel: 'DB'`, `icon: '🗄'`.
- Positioned between `system-database` (Data Explorer) and
  `system-observability` so the two database-related panels sit
  next to each other.

### page.tsx wiring (`src/app/page.tsx`)

- Added `lazyPanel(() => import('@/components/DatabaseStatusPanel'),
  'Loading Database Status…')` alongside the other System-wave-8
  dynamic imports (mirrors the `ssr:false` + skeleton pattern).
- Added a new render case:
  ```tsx
  {activeSection === 'system-database-status' && (
    <PanelErrorBoundary label="Database Status">
      <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
        <DatabaseStatusPanel />
      </div>
    </PanelErrorBoundary>
  )}
  ```
- Positioned between `system-database` and `system-observability`
  to match the sidebar ordering.

### i18n wiring (`src/messages/en.json` + `src/messages/fr.json`)

- Added `nav.database_status` to both locale files:
  - `en.json`: `"database_status": "Database"`
  - `fr.json`: `"database_status": "Base de Données"`

The label resolves via the `useTranslation()` hook already wired in
`Sidebar.tsx`, so the sidebar item flips to the French label when
the trader's locale is set to `fr` (no Sidebar code change
required).

## Tests created: `src/components/DatabaseStatusPanel.test.tsx`

22 tests across the four contract surfaces required by the W21-7
spec. Mirrors the test strategy of `RateLimitPanel.test.tsx` +
`AnalyticsPanel.test.tsx`:

- Per-test `global.fetch` mock via
  `vi.mocked(fetch).mockImplementation`.
- Real timers + `waitFor` for initial-render assertions.
- `vi.useFakeTimers()` + `act(async () => await
  vi.advanceTimersByTimeAsync(N))` for polling assertions.
- `getAllByText` for values that appear in multiple DOM nodes (e.g.
  "SQLite" appears in the header badge + KPI card + per-table badge;
  uptime % appears in the KPI + health grid; "Healthy" appears in
  the HealthBadge + Status column).

### Test coverage breakdown

| Test | What it asserts |
|------|----------------|
| Initial loading skeleton | `Loading Database Status…` + spinner render before the first fetch resolves. |
| SQLite backend rendering (amber badge) | Header `BackendBadge` shows "SQLite" with `bg-amber-500` class; "No fallbacks recorded" KPI sub-label; PG-not-configured note; retry button present. |
| SQLite tables render | `market_snapshots` + `orderbook_ticks` rows render with row counts "1,245" + "8,421" formatted via `toLocaleString`; per-table SQLite badge appears ≥ 3 times. |
| SQLite no-errors empty state | Green `CheckCircle2` + "No connection errors recorded" message renders when `recent_errors: []`. |
| PostgreSQL backend rendering (green badge) | Header `BackendBadge` shows "PostgreSQL" with `bg-green-500` class; full 5-column PG health grid renders; uptime "99.85%" appears in both KPI and grid; latency "4.2ms"; pool "3/10"; failures "0"; HealthBadge "Healthy". |
| SQLite Fallbacks KPI = 2 | `fallback_counter: 2` renders as KPI value "2" with sub-label "Fallbacks to SQLite". |
| Recent errors list | Both error messages from `postgresPayload.recent_errors` render with the asyncpg + pool-exhausted text. |
| PG per-table badge | All 3 PG tables render with the "PG" per-table badge (`getAllByText('PG').length >= 3`). |
| Degraded health badge | `pg_health.status = 'degraded'` renders amber HealthBadge; `getAllByText('Degraded')` ≥ 1 (badge + grid Status column); uptime "92.30%" renders; fallback `7` (red threshold). |
| Empty tables state | `tables: []` renders "No table statistics available" + the "backend has not reported table-level row counts" description. |
| Hard-error state (500) | `fetch` returns 500 → "Database status endpoint unavailable" + retry button with `aria-label="Retry database status fetch"`. |
| Hard-error state (network) | `fetch` rejects → same error state + "Network error: ECONNREFUSED" message. |
| Retry button fires POST | Clicking the Retry PG button fires a `POST /api/system/db-retry` call (asserts `init.method === 'POST'` + URL contains `/api/system/db-retry`). |
| Retry success banner | After retry POST returns `success: true`, the success banner "PG pool re-armed" renders. |
| Retry failure banner | After retry POST returns `success: false`, the failure banner "PG still unreachable" renders. |
| Header Refresh button | The header Refresh button (aria-label "Refresh database status") is present. |
| Manual Refresh triggers fetch | Clicking the header Refresh button fires an additional `GET /api/system/db-status`. |
| 15s poll updates KPIs | First poll returns `fallback_counter: 0`; second poll (15s later) returns `3`; KPI value transitions from `0` → `3`; total fetch count increments by exactly 1. |
| Authorization header | Every fetch carries `Authorization: Bearer <token>` (the `apiFetch` wrapper contract). |
| Visibility-aware polling | Tab hidden → 60s advance fires 0 new polls; tab restored → immediate refresh + resumed polling fires ≥ 1 new fetch. |
| Unmount cleanup | Unmount clears the polling interval; advancing 60s after unmount fires 0 additional fetches (no leaked setState warnings). |
| 15s poll badge | Header renders the "15s poll" badge. |

## Verification

- `cd /home/z/my-project && bun run lint` — clean (`eslint .` exits 0,
  no warnings, no errors). Verified the 3 modified files (`Sidebar.tsx`,
  `page.tsx`, `DatabaseStatusPanel.tsx`) + 2 modified i18n files + the
  new test file all lint cleanly.
- `cd /home/z/my-project && bunx vitest run ./src/components/DatabaseStatusPanel.test.tsx`
  — **22 passed (22)** in 1.95s.
- `cd /home/z/my-project && bun run test` — **701 passed (701)** in
  62.56s across 33 test files. The new `DatabaseStatusPanel.test.tsx`
  adds 22 new tests to the suite (baseline was 679 tests in 32 files
  before this work). All pre-existing tests continue to pass — no
  regressions.
- Dev server log (`dev.log`) — clean. Next.js 16.1.3 / Turbopack
  compiled `/` in 4ms on subsequent requests (after the initial 7.5s
  first-compile). No runtime errors introduced by the new panel,
  sidebar item, or page render case.

## Files changed

| Path | Change |
|------|--------|
| `src/components/DatabaseStatusPanel.tsx` | **NEW** — 660-line panel component. |
| `src/components/DatabaseStatusPanel.test.tsx` | **NEW** — 22-test spec file. |
| `src/components/Sidebar.tsx` | Added `'system-database-status'` to `NavSection` union + nav item in the `system` group. |
| `src/app/page.tsx` | Added `DatabaseStatusPanel` lazy import + render case for `activeSection === 'system-database-status'`. |
| `src/messages/en.json` | Added `nav.database_status: "Database"`. |
| `src/messages/fr.json` | Added `nav.database_status: "Base de Données"`. |

## Known limitations / follow-ups

1. **Backend endpoint not yet implemented.** The
   `GET /api/system/db-status` and `POST /api/system/db-retry`
   endpoints are designed here but not yet implemented in the bot
   mini-service (`mini-services/polymarket-bot/api/server.py`). The
   panel gracefully degrades: a 500 / 404 / network-error response
   surfaces the `ErrorState` component with a retry button. When the
   backend implements the contract documented in the file header,
   the panel will render live data without any frontend change.
2. **PG pool shape assumed.** The `pg_health` block mirrors the
   fields exposed by `core/db_pool.py` (uptime_pct, avg_latency_ms,
   pool_size, pool_in_use, consecutive_failures). If the backend
   adds fields, they can be surfaced by extending the
   `PgHealthReport` interface + adding a grid column.
3. **No keyboard shortcut.** The new `system-database-status` nav
   item does NOT have a `kbd` shortcut (consistent with the other
   non-keyboard-mapped system panels like `system-observability`,
   `system-retention`, `system-decisions`, `system-safety`,
   `system-rate-limit`, `system-audit`). Navigation is via the
   sidebar click OR via the command palette once it's wired (see
   W16-8 known-limitation #1).
4. **Persisted `defaultPanel` preference.** `NAV_SECTION_KEYS` in
   `page.tsx` is built from `KB_MAP` only (the 8 keyboard-mapped
   sections), so a user who persists `defaultPanel:
   'system-database-status'` will fall through to the default
   `'command'` panel — this is consistent with the existing
   behaviour for every other non-keyboard-mapped system panel and
   is not a regression.
