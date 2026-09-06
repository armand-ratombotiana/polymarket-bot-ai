# W41-2 — full-stack-developer — Frontend performance optimization

## Task summary
Five-step performance pass across the workstation: re-render audit,
polling cadence, bundle splitting, CSS audit, performance hook.

## Files changed (additive + in-place edits)
- **EDIT** `src/app/page.tsx` — converted 11 conditionally-rendered
  panel imports to `lazyPanel()` (dynamic + ssr:false + skeleton);
  extracted 14 inline-lambda callbacks to stable `useCallback`
  references so they no longer bypass React.memo on memoised children.
- **EDIT** `src/components/AnalyticsPanel.tsx` — wrapped the inline
  binomial-test / CI / trend-arrow computations in `useMemo` so
  parent-driven re-renders (e.g. useBot snapshot ticks) skip the
  arithmetic. Hoisted above the early returns so the rules-of-hooks
  are satisfied.
- **EDIT** `src/lib/preferences.ts` — bumped default `refreshIntervalMs`
  from 2000 → 5000ms (WS is the primary transport; REST is heartbeat).
- **EDIT** `src/hooks/useBot.ts` — bumped the fallback default from
  2000 → 5000ms to mirror the preferences default.
- **EDIT** `src/lib/preferences.test.ts` — updated EXPECTED_DEFAULTS
  to reflect the new 5000ms default.
- **EDIT** `src/hooks/usePreferences.test.ts` — updated the
  persistence assertion to expect 5000ms instead of 2000ms.
- **EDIT** `src/app/globals.css` — added a W41-2 audit-trail comment
  block (no CSS rules added or removed; verification only).
- **NEW** `src/hooks/usePerformance.ts` — opt-in performance monitor
  (initial render time, re-render count, API call count).
- **NEW** `src/hooks/usePerformance.test.ts` — 7 tests covering the
  hook's contract.

## Verification
- `bun run lint` — EXIT 0, clean.
- `TMPDIR=/dev/shm/vitest-tmp bun run test -- --run` — 90 test files
  passed (1515 tests passed). The single unhandled-error message in
  `AIPredictionExplainerPanel.test.tsx` is PRE-EXISTING (present in
  the baseline before this task — `explanation.explanation.top_features`
  TypeError when the test mock omits the nested `explanation` prop).
  Not in scope for this task.
- Dev server: `dev.log` shows `✓ Compiled in X ms` with no errors
  after the edits.

## Re-render audit findings (Step 1)
- PositionsPanel — ✅ already optimized (memo + custom comparator +
  useCallback + useMemo for filteredPositions / showTimeHeldColumn /
  confirmingPosition / handleExportCsv / handleCloseClick). No changes
  needed.
- MarketsPanel — ✅ already optimized (memo + custom comparator +
  useMemo for filtered / sorted / avgSpreadCents / connStatus). The
  `handleSort` / `handleCopy` handlers are intentionally NOT memoised
  because they're called from inline arrow functions on native `<button>`
  DOM elements (no memoised child to bypass).
- TradesPanel — ✅ already optimized (memo + useCallback for
  handleExportCsv / copyToClipboard / handleViewAudit + useMemo for
  filteredTrades / displayedTrades / stats / totalFees / hasFees /
  avgSlippageBps).
- AnalyticsPanel — ⚠️ had memo but no useMemo on the binomial-test /
  CI / trend computations. Fixed in this task.
- page.tsx — ⚠️ 14 inline-lambda callbacks (`() => setChartMarket(m)`
  etc.) bypassed React.memo on any memoised child they were passed to.
  Extracted to stable useCallback references.

## Polling audit findings (Step 2)
- Default `refreshIntervalMs` was 2000ms; bumped to 5000ms in both
  `preferences.ts` (DEFAULTS) and `useBot.ts` (fallback default).
- Visibility-aware polling — verified already implemented in
  `useBot` (visibilitychange handler clears/restores the interval)
  and `useRealtimeData` (skips individual ticks when
  `document.hidden`).
- WebSocket is used as the primary transport via `useRealtimeData`
  (REST is heartbeat / fallback only).
- Batched API requests — verified already in `useBot.fetchRestSnapshot`
  via `Promise.all` over the 6 composite endpoints.

## Bundle audit findings (Step 3)
- Converted 11 static imports to `lazyPanel()` (dynamic + ssr:false +
  skeleton): MarketScreener, OrdersPanel, StrategyMatrix,
  ArbitrageMatrixView, DeepAnalysisView, AIMLCommandCenter,
  AICopilotPanel, LeaderboardPanel, BacktestLabView, SystemHealthView,
  DatabaseExplorerView.
- Kept static: panels rendered on the initial command view
  (EquityCurve, AnalyticsPanel, MLPanel, PositionsPanel, MarketsPanel,
  TradesPanel, CommandCenterDashboard, Sidebar, TopStatusBar,
  ConfirmationDialog, PanelErrorBoundary) — these would flash a
  loading skeleton on first paint if lazy-loaded.
- recharts already tree-shaken via named imports in every chart file
  (Sparkline, PnLBarChart, EquityCurveChart, etc.). No changes needed.

## CSS audit findings (Step 4)
- `backdrop-filter` usage — sparse:
  - TopStatusBar via `backdrop-blur-md` Tailwind class (single sticky
    element, intentional UX).
  - Modals (`.modal` + `.modal-backdrop`) — only mount when a modal is
    open.
  - Sonner toaster `[data-sonner-toast]` — only mounts when a toast is
    in flight.
  - `.data-table th` translucent + blur combo was already replaced
    with a solid surface by the W39-2 redesign.
- `will-change` usage — sparing: only `.card-hover` declares it, and
  that class isn't referenced by any component (dead CSS, kept as a
  utility for future KPI-card hover effects).
- `box-shadow` usage — contained: each shadow is a 1–3 layer
  declaration via the `--shadow-{xs,sm,md,lg,xl,card,popover}` token
  scale. No multi-layer composites on per-row elements (rows use a
  2px left border for hover affordance instead of a shadow).
- Layout thrashing — absent: panel components mutate refs on render
  (plain JS object mutations, not layout queries). No
  `getBoundingClientRect` / `offsetWidth` / `scrollTop` reads happen
  mid-render that would force a synchronous layout reflow.

## Performance hook (Step 5)
- `src/hooks/usePerformance.ts` — opt-in monitor (gated by
  `localStorage.polymarket_perf_monitor === '1'`).
- Returns `{ initialRenderMs, renderCount, apiCallCount, log }`.
- Per-name counters in a module-level Map (cumulative across
  remounts).
- API call counter wraps `window.fetch` idempotently (multiple
  `usePerformance` callers share the same counter).
- Publishes a `window.__perf__` snapshot on every render so the
  trader can inspect numbers from the dev console.
- 7 tests in `usePerformance.test.ts` cover the contract.

## Usage example
```ts
function PositionsPanel() {
  const perf = usePerformance('PositionsPanel')
  // perf.initialRenderMs / perf.renderCount / perf.apiCallCount
}
```
Enable via `localStorage.setItem('polymarket_perf_monitor', '1')`
and reload. Read aggregated numbers via `(window as any).__perf__`.
