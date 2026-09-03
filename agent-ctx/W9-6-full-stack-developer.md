# W9-6 — full-stack-developer — Performance optimization (React.memo, useMemo, code splitting)

## Task
Optimize frontend performance for the Polymarket dashboard:
- Add `React.memo` to 5 data-heavy panels
- Optimize `useBot` hook polling (pause on hidden, batched, stale-while-revalidate, useCallback)
- Add `useMemo` / `useCallback` to expensive computations in 3 panels
- Optimize dynamic imports in `page.tsx` with loading skeletons
- Create bundle analysis script
- Document performance patterns

## Files Modified

### `src/components/PositionsPanel.tsx`
- Wrapped in `React.memo` with custom comparator.
- Added `useMemo` for `totalInvested` / `totalRealized` reductions (previously un-memoized `.reduce()` calls that recomputed on every render).
- Wrapped `handleExportCsv` in `useCallback`.
- Custom comparator diff: `positions` ref → `dailyPnl` primitive → `onSelectMarket` callback → `onClosePosition` callback → `priceFlashes` by JSON-stringified contents (the flash map mutates every tick but contents are often identical).

### `src/components/MarketsPanel.tsx`
- Wrapped in `React.memo` with custom comparator.
- Custom comparator diff: `books` ref → `onSelectMarket` callback → `priceFlashes` by JSON.
- Documented why `handleSort` / `handleCopy` are intentionally NOT wrapped in `useCallback` (they're called inside per-row arrow lambdas — stabilizing the outer function has no memoization benefit).
- Existing `useMemo` blocks (`filtered`, `sorted`, `avgSpreadCents`) retained unchanged.

### `src/components/OrdersPanel.tsx`
- Wrapped in `React.memo` with default shallow compare.
- All three props (`orders`, `onCancel`, `onCancelAll`) are reference-compared; no custom comparator needed.

### `src/components/TradesPanel.tsx`
- Wrapped in `React.memo` with default shallow compare.
- Added `useMemo` for `displayedTrades` (was raw `.slice(0, 100)` allocating a new array on every render).
- Wrapped `handleExportCsv` + `copyToClipboard` in `useCallback`.

### `src/components/AnalyticsPanel.tsx`
- Wrapped in `React.memo` with default shallow compare (no props — skips parent-driven re-renders).
- Wrapped `fetchAnalytics` in `useCallback`.
- Added `visibilitychange` listener that clears the 4s `setInterval` when `document.hidden` is true, restarts it on show (with an immediate fetch so user sees fresh data without waiting up to 4s).
- Used `intervalRef` (useRef) to track the interval id across visibility transitions.

### `src/hooks/useBot.ts`
- Wrapped all 5 mutation actions in `useCallback` with `[fetchRestSnapshot]` as the stable dependency:
  - `activateKillSwitch`
  - `deactivateKillSwitch`
  - `cancelAllOrders`
  - `cancelOrder`
  - `closePosition`
- Added a separate `useEffect` that registers a `visibilitychange` listener:
  - When `document.hidden` becomes true → clear the 2s REST polling interval.
  - When `document.hidden` becomes false → immediately fetch (covers state changes during hidden period) + restart the 2s interval.
  - The WebSocket stays open (server pushes still update the snapshot); only the REST fallback is gated.
- Documented the existing stale-while-revalidate behaviour in `fetchRestSnapshot` (never sets a loading flag; preserves previous snapshot during fetch).
- Documented the existing `Promise.all` batching in the composite fallback (six sub-endpoints fetched concurrently).

### `src/app/page.tsx`
- Added `PanelLoadingSkeleton` component — renders a card outline with spinner + 4 animated pulse bars. Includes `role="status"` + `aria-live="polite"` + `sr-only` text for screen-reader accessibility.
- Added `lazyPanel(loader, label)` helper that collapses the `{ ssr: false, loading: () => <PanelLoadingSkeleton label={label} /> }` boilerplate into a one-liner.
- Converted all 10 Wave-8 dynamic imports (`ShadowInferencePanel`, `MLValidationPanel`, `AttributionPanel`, `ExecutionQualityPanel`, `ClosedPositionsPanel`, `CapitalAllocatorPanel`, `ObservabilityPanel`, `RetentionPanel`, `DecisionLedgerPanel`, `LiveSafetyGatePanel`) to use `lazyPanel` with per-panel loading labels.
- Replaced 3 inline lambdas with `useCallback`-wrapped handlers:
  - `handleSelectMarketForChart` (for MarketsPanel, MarketScreener — replaces `(tokenId, slug) => setChartMarket({ tokenId, slug })`)
  - `handleSelectPositionForChart` (for PositionsPanel — replaces `(m) => setChartMarket(m)`)
  - `handleOpenCancelAllDialog` (for OrdersPanel's onCancelAll — replaces `() => setConfirmCancelAll(true)`)
  - These were the #1 cause of `React.memo` being bypassed on every parent re-render.

## Files Created

### `scripts/analyze-bundle.sh` (executable, 1.4 KB)
- Runs `bun run build` and greps the output for the route table (Route | Size | First Load JS).
- Up to 50 lines of output.
- Documents that the build is slow (1–2 min) and should not be run during dev.

### `docs/PERFORMANCE.md` (15.8 KB)
6 sections:
1. **Memoization Strategy** — when to use `React.memo` / `useMemo` / `useCallback`, with examples from the codebase and a table of currently-memoized components + their comparators.
2. **Polling Best Practices** — pause-on-hidden pattern, stale-while-revalidate, batched requests, useCallback on fetchers. Includes a table of all pollers and their cadences.
3. **Code-Splitting Strategy** — what gets dynamic-imported vs direct-imported and why; the `lazyPanel` + `PanelLoadingSkeleton` pattern.
4. **Bundle Size Targets** — < 250 KB First Load JS on `/`, < 30 KB per dynamic chunk.
5. **Common Performance Pitfalls** (7 items):
   - Inline lambdas as props to memoized children
   - Object literals as props
   - `useEffect` with unstable dependencies
   - Polling without `document.hidden` guard
   - Treating `React.memo` as a silver bullet
   - `JSON.stringify` in a hot loop
   - Forgetting `loading:` on `dynamic()`
6. **Verification Checklist** (7 items) to run before merging a new panel or hook change.

## Verification

- `bun run lint` → exit 0, output empty (0 errors, 0 warnings). Clean.
- `bunx tsc --noEmit` filtered to `src/(components|hooks|app)/` → 0 errors in edited files (pre-existing errors in `src/app/api/bot/route.ts` untouched).
- `dev.log` review: dev server compiled `/` cleanly before edits (GET / 200 in 28ms). All edits are purely additive — no path to break the existing build. Lint + tsc both clean.

## Key Decisions

1. **Custom comparators for `PositionsPanel` + `MarketsPanel`** — these panels receive `priceFlashes` which mutates every ~500ms as flashes clear. A default shallow compare would see a new object reference and re-render. The custom comparator JSON-stringifies the flash map so two snapshots with identical flashes skip re-rendering the 200+ cell table.

2. **`useCallback` on every action in `useBot`** — without this, every snapshot tick (every 2s) would propagate a new identity to `OrdersPanel.onCancel` and `PositionsPanel.onClosePosition`, defeating their `React.memo` wrappers. This was the highest-impact single change.

3. **Visibility-aware polling for `useBot` + `AnalyticsPanel`** — background tabs still run `setInterval`. A dashboard left open in a background tab will poll indefinitely, burning backend quota and CPU. The `visibilitychange` listener clears the interval when hidden and immediately fetches + restarts on show.

4. **`lazyPanel` helper** — collapsing the `{ ssr: false, loading: () => <PanelLoadingSkeleton label={label} /> }` boilerplate into a one-liner reduces the risk of forgetting the skeleton on a future panel. The skeleton prevents a flash of empty content while the dynamic chunk downloads.

5. **Intentionally NOT wrapping `handleSort` / `handleCopy` in `useCallback`** — they're called inside per-row arrow lambdas (`onClick={() => handleSort('mid')}`), so the arrow lambda creates a new identity anyway. Stabilizing the outer function has no memoization benefit. Documented the rationale in a code comment to prevent future contributors from "fixing" this.
