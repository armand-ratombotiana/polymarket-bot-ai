# Task W8-8 — ObservabilityPanel

## Read references
- `worklog.md` tail — Wave 7 complete (340 tests, 77 routes, balance $111.72).
- `mini-services/polymarket-bot/core/observability.py` — SQLite metric store,
  6 canonical categories (`data_source`, `bot`, `strategy`, `execution`, `ml`,
  `system`), `register_routes` exposes 2 endpoints:
    - `GET /api/observability` — structured health report (latest value per
      (category, name), bucketed under canonical categories, plus `other` for
      ad-hoc metrics; carries `metric_count`, `oldest_sample_age_seconds`,
      `newest_sample_age_seconds`, `generated_at`).
    - `GET /api/observability/history/{name}?limit=N` (1≤N≤1000) — most
      recent N samples for metric `name` (newest first). Each sample carries
      `timestamp`, `category`, `name`, `value`, `metadata`.
- `mini-services/polymarket-bot/core/observability_collector.py` — auto-
  collector emits ~23 metrics every 30s across data_source/bot/execution/ml/
  system (no `strategy` metrics in practice). Per-subsystem collectors each
  swallow their own errors so a single source failure never blocks others.
- `src/components/SystemHealthView.tsx` — design pattern reference: dark
  `bg-[#13161e]` surface, `border-[#1f2335]`, `text-[#dde1ed]`, KPI strip
  using `.kpi-card` / `.kpi-label` / `.kpi-value` / `.kpi-sub`, `.badge-*`
  for status chips, `scrollbar-thin` for overflow.
- `src/lib/api.ts` — `apiFetch()` injects `Authorization: Bearer <token>`
  + `XTransformPort=8080` for non-`/api/bot` paths.
- `src/app/globals.css` — design tokens (`--bg-card`, `--border`,
  `--text-secondary`, semantic colors); reusable classes `.card`,
  `.kpi-card`, `.badge`/`.badge-*`, `.input`/`.input-sm`, `.select`,
  `.skeleton-card`, `.skeleton-line`, `.skeleton-line-lg`, `.spinner`,
  `.banner-warning`, `.scrollbar-thin`, `.btn`/`.btn-ghost`/`.btn-sm`/`.btn-xs`.

## Backend endpoints used
- `GET /api/observability` — health report (polled every 30s).
- `GET /api/observability/history/{name}?limit=N` — sparkline data per
  metric (refetched when time range or report metric set changes).

## Key features
1. **5 collapsible category sections** (DATA, BOT, EXECUTION, ML, SYSTEM)
   using shadcn `Collapsible` primitive. Each section's left border is
   colour-coded per the spec (DATA=blue, BOT=violet, EXECUTION=amber,
   ML=emerald, SYSTEM=gray).
2. **Metric cards** showing name, formatted value, unit, category badge,
   last-updated timestamp (UTC clock + relative age). Colour-coded value
   by per-metric severity thresholds (`normal`/`warning`/`critical`/
   `unknown`).
3. **Sparklines** — 60×24 SVG polylines per metric, fetched from the
   history endpoint. Computes min/max and scales points to fit; renders a
   dashed baseline when <2 samples are available. Stroke colour follows
   the category colour.
4. **Search filter** — filters metrics by name (case-insensitive
   substring) across all visible categories.
5. **Category toggles** — clickable badges in the filter row toggle
   visibility per category; categories with no data are disabled.
6. **Time range selector** — `1h` / `6h` / `24h` / `7d` (shadcn `Select`)
   maps to backend `limit` (120/720/1000/1000). Changing the range
   triggers an immediate history refetch.
7. **Auto-refresh** — polls every 30s; pauses when `document.hidden`,
   resumes immediately on `visibilitychange`. Manual refresh button
   included. `fetchingRef` guard prevents overlapping fetches.
8. **Loading skeleton**, **hard error state** (with Retry button),
   **empty state** (when `metric_count === 0` — explains the collector
   cadence and offers a "Check again" button), and **soft error banner**
   (last refresh failed but previous data still shown).
9. **KPI strip** — Total Metrics, Newest Sample age, Oldest Sample age,
   Last Refresh clock.
10. **Responsive grid** — metric cards collapse 4→3→2→1 columns at
    `xl`/`lg`/`sm` breakpoints. Filter bar wraps on narrow viewports.

## Implementation notes
- Default export `export default function ObservabilityPanel()`.
- `'use client'` directive at top.
- TypeScript interfaces for `HealthReport`, `MetricEntry`, `HistorySample`,
  `HistoryResponse`, `Threshold`, `CategoryMeta`, `Severity`, `TimeRange`.
- Sparkline is a small inline SVG component (computes min/max, scales
  points to 60×24, draws polyline + last-point dot).
- Per-metric unit map drives value formatting: `count` → integer,
  `%` → 1dp, `$` → signed 2dp, `MB`/`s`/`ms` → humanised, `bool` →
  YES/NO, `score`/`PSI` → 4dp.
- Per-metric threshold map drives severity colour-coding; both
  `higher-bad` (cpu, latency, drift) and `lower-bad` (pnl, slippage,
  roc_auc) directions supported.
- Histories are fetched in parallel via `Promise.all` (23 metrics is
  well within browser concurrent budgets).
- Visual style mirrors `SystemHealthView.tsx` (dark card surface, KPI
  strip, `.badge-*` chips) but with richer per-metric cards.
- `apiFetch` from `@/lib/api` is used for all requests (auto-injects
  auth header + gateway port).
- Lint clean (`bun run lint` → exit 0). TypeScript clean (`tsc --noEmit`
  → no errors in ObservabilityPanel.tsx).

## Files created
- `src/components/ObservabilityPanel.tsx` — the panel component.
