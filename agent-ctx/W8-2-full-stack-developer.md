# Task W8-2 — AttributionPanel (7-Dimension P&L Attribution)

## Read references
- `worklog.md` tail (Wave 7 complete; backend 77 routes, 340 tests,
  balance $111.72 profitable; X1-X15 added 70+ new tests for all
  untested modules; settlement deadlock fixed)
- `mini-services/polymarket-bot/core/attribution.py` — full 7-dimension
  attribution engine. Public fn `get_full_attribution()` returns
  dict shape: `summary`, `by_strategy`, `by_confidence_bucket`,
  `by_edge_bucket`, `by_probability_band`, `by_liquidity_level`,
  `by_holding_period`, `by_trade_direction`, `bucket_definitions`.
  Route registered via `register_routes(app)` → `GET /api/attribution`
  (auth-protected by caller's middleware).
- `src/components/AnalyticsPanel.tsx` — visual pattern reference:
  dark card `bg-[#13161e]` + `border-[#1f2335]`, KPI grid
  (`kpi-card`/`kpi-label`/`kpi-value`/`kpi-sub`), `card-header`
  with `card-title` + status `badge`, `banner-warning` for soft
  errors, `mono` numeric values, `spinner` for loading.
- `src/lib/api.ts` — `apiFetch(input, init)` wraps `fetch`,
  injects `Authorization: Bearer ${getApiToken()}` and
  `XTransformPort=8080` query param on relative `/api/*` URLs
  (skips `/api/bot` which is on the Next.js side).
- `src/app/globals.css` — full CSS design system: design tokens
  (`--bg-card`, `--border`, semantic colors), `.card`,
  `.card-header`, `.card-title`, `.kpi-card`, `.kpi-label`,
  `.kpi-value`, `.kpi-sub`, `.skeleton`/`.skeleton-card`,
  `.spinner`, `.data-table` (+ `.label-col`, sticky thead,
  row-hover), `.badge` + variants, `.banner-warning`,
  `.empty-state`, `.error-state`, `.exposure-bar`,
  `.scrollbar-thin`, `.table-container`, `.table-footer`,
  `.pnl-positive/.pnl-negative/.pnl-zero`, animations.
- `src/lib/design-tokens.ts` — `fmtUsd`, `fmtPnl`, `fmtPct`,
  `fmtInt`, `fmtAge`, `fmtTime` formatters.
- `src/components/ui/{card,badge,tabs,select,skeleton}.tsx` —
  confirmed shadcn/ui component APIs available.

## Backend endpoint (verified in attribution.py register_routes)
- `GET /api/attribution` → calls `get_full_attribution()`.
  Returns dict with `summary`, seven `by_*` arrays of bucket
  dicts, and `bucket_definitions` legend.
- Each bucket dict carries: `bucket` (label str), `count`,
  `total_pnl`, `avg_pnl`, `win_rate` (0..1), `wins`, `losses`,
  `avg_holding_seconds`, `gross_profit`, `gross_loss`,
  `profit_factor` (float | None), `capital_deployed`.
- Backend does NOT accept a `?range=` query param at present —
  the panel still sends one (the FastAPI route ignores unknown
  query params). The selector drives UX; backend returns all-time
  data today. If a future PR adds `range` filtering, the panel
  picks it up automatically with zero code changes.

## Component shape
- Default export `AttributionPanel` (no props).
- `'use client'` directive at top.
- Self-contained fetch + 30s polling using `apiFetch`.
- Polling pauses when `document.hidden`; resumes + refetches
  immediately on tab regain (so the user isn't waiting up to
  30s for fresh data on return).

## Visual structure
1. Header — PieChart icon + "Performance Attribution" title +
   `badge-cyan 7-DIMENSION` chip on the left; shadcn Select for
   time range + Refresh button (spinner + "Xs ago" freshness
   label) on the right.
2. Summary KPIs (4-card grid, responsive 2-col mobile → 4-col
   desktop):
   - Total P&L (`fmtPnl` colored green/red/gray)
   - Attributed (sum across 7 dimensions, divided by 7 for
     the average — surfaces the invariant that every dimension
     slices the same total)
   - Unattributed Residual (total − attributed; "Fully
     reconciled" tag when |residual| < $0.01)
   - Coverage % (|attributed| / |total| × 100, capped at 100%)
3. Tabs (shadcn/ui Tabs):
   - **Dimensions** — 7 clickable rows. Each row: chevron +
     Lucide icon + label + description on the left; horizontal
     bar (emerald for +, red for −) scaled to max-abs-PnL
     across dimensions; value, % of total, bucket-count badge
     on the right. Click expands an inline per-bucket list
     with best/worst callout row + one mini-bar per bucket
     showing bucket's share of the dimension's total.
   - **Waterfall** — CSS-only waterfall: for each dimension
     takes the BEST bucket's total_pnl as the dimension's
     "alpha source" and stacks cumulatively. Each row has the
     bar segment positioned at running-cumulative start
     offset, bucket label inside the bar, delta + cumulative
     total on the right. Bottom marker shows final cumulative
     total P&L.
   - **Strategies** — `data-table` with sticky thead, columns:
     Strategy / Trades / Win Rate / Total P&L / Avg P&L /
     Profit Factor (∞ when null) / Capital Deployed / Avg
     Hold. `table-footer` shows strategy count + Σ P&L.
4. Loading state — `kpi-card` grid skeleton + 7 dimension-row
   skeletons with shimmer.
5. Error state (hard) — `error-state` block with AlertCircle
   + Retry button.
6. Error state (soft) — `banner-warning` strip above the
   KPIs when stale cached data is still being shown but the
   refresh failed.

## Dimension metadata
| key | label | icon | accent |
|---|---|---|---|
| by_strategy | Strategy | Layers | blue |
| by_confidence_bucket | ML Confidence | Brain | purple |
| by_edge_bucket | Predicted Edge | Target | cyan |
| by_probability_band | Probability Band | Percent | amber |
| by_liquidity_level | Liquidity Level | Waves | green |
| by_holding_period | Holding Period | Clock | blue |
| by_trade_direction | Trade Direction | ArrowLeftRight | cyan |

## Color coding
- Positive P&L → emerald (`text-[#4ade80]`, `bg-emerald-500/70`)
- Negative P&L → red (`text-[#f87171]`, `bg-red-500/70`)
- Zero / neutral → gray (`text-[#7e8aaa]`, `bg-slate-600/50`)
- Win rate ≥ 50% → green text, else red.

## Helpers (module-local)
- `humanizeBucket(label)` — `snake_case` → `Title Case`
- `fmtHoldingSeconds(s)` — seconds → `Xs` / `Xm` / `Xh` / `Xd`
- `sumDimensionPnl(buckets)` — Σ `total_pnl`
- `bestBucket(buckets)` / `worstBucket(buckets)` — argmax/argmin
  of `total_pnl`

## Verification
- `bun run lint` → exit 0, no warnings or errors.
- No other files modified (per task constraint).

## Files created
- `/home/z/my-project/src/components/AttributionPanel.tsx`
  (default export `AttributionPanel`, ~620 lines)
