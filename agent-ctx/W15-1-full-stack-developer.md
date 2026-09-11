---
Task ID: W15-1
Agent: full-stack-developer
Task: Improve real-time price ticker animations and market depth visualization

Work Log:
- Read worklog tail (~200 lines — W14-2 rate limit tracker, W14-3 CLI tool).
  Reviewed existing MarketsPanel.tsx (350 lines, React.memo'd with
  custom comparator), DepthChartModal.tsx (482 lines, fetches
  /api/depth/{token_id} every 2s), charts/Sparkline.tsx (Recharts
  LineChart pattern), charts/EquityCurveChart.tsx (Recharts AreaChart
  with gradient + ReferenceLine), charts/theme.ts (chartTheme +
  tooltipStyle + axisProps + gridProps), useBot.ts (priceFlashes
  Record<token_id, 'up'|'down'> map derived from prevMidsRef),
  MarketChartModal.tsx (existing candlestick SVG + trade ticket,
  fetches /api/history/ohlcv/{token_id}?resolution=X&count=N). Confirmed
  `framer-motion@^12.23.2` is already in package.json dependencies.

### Step 1 — Created `src/components/PriceTicker.tsx` (~230 lines):
  * Pure display component: receives `price` (current mid), `previousPrice`
    (prior tick — null on first render), `bestBid`, `bestAsk`, `spread`
    (best_ask − best_bid), `compact` flag, `size` ('xs'|'sm'|'md'|'lg'),
    and `label` for aria.
  * `formatTickerPrice(v)` exported helper — adaptive decimal precision:
    `< 0.01 → 4dp` (e.g. 0.0042), `0.01–0.99 → 3dp` (e.g. 0.625,
    probabilities), `1–9.99 → 2dp` (e.g. 4.50), `≥10 → 2dp` (e.g. 42.50),
    `null/NaN/Infinity → "—"`.
  * `computeChange(current, previous)` exported helper — returns
    `{dir: 'up'|'down'|'flat', abs, pct}`. Guards against null/NaN/
    previous===0 (division by zero).
  * Visual layout (top-to-bottom):
    1. **Bid/Ask chip** (suppressed in compact mode): a small bordered
       chip showing the formatted best_bid (green) | divider | best_ask
       (red). Renders "—" placeholders when sides are null.
    2. **Animated price**: Framer Motion `motion.span` with
       `AnimatePresence mode="popLayout"`. The key includes both
       `price` and `previousPrice` so a tick that produces the same
       price still re-mounts the span and re-fires the flash transition.
       The `animate.color` resolves to chartTheme.colors.success (up),
       chartTheme.colors.danger (down), or chartTheme.colors.muted
       (flat / no prior). The motion.span exposes `data-direction` for
       testability.
    3. **Spread chip** (when spread is provided): `0.04 → "4.0¢"` —
       amber (chartTheme.colors.warning) when ≥3¢, muted otherwise.
    4. **Change-since-last-tick line** (suppressed in compact mode):
      "+5.00¢ (+10.00%)" for up ticks, "−5.00¢ (−10.00%)" for down
      (Unicode MINUS U+2212), or a dim em dash when previousPrice is
      null.
    5. **Subtle pulse background**: an absolutely-positioned radial
       gradient span keyed to the same animKey, fading 0.18 → 0 over
       500ms. `pointer-events-none` so it doesn't interfere with row
       clicks.
  * Wrapped in `React.memo` (default shallow compare) — the parent
    (MarketsPanel) re-renders on every order_books snapshot, so memo
    skips the ticker cell for any token whose book didn't tick.

### Step 2 — Created `src/components/charts/MarketDepthChart.tsx`
  (~330 lines):
  * Recharts `AreaChart` visualizing cumulative bid/ask depth.
  * Props: `bids: DepthLevel[]`, `asks: DepthLevel[]`, `mid?`,
    `bestBid?`, `bestAsk?`, `spread?`, `height=260`, `bidColor`
    (default chartTheme.colors.success), `askColor` (default
    chartTheme.colors.danger), `showMidLine=true`, `showSpreadChip=true`,
    `formatPrice` (3dp default), `formatSize` (k-suffix for ≥1000).
  * `buildChartData(bids, asks)` merges both ladders into a single
    array of `{ price, bidTotal, askTotal, bidSize, askSize }` rows.
    Each unique price point across both sides becomes one X-axis tick;
    sides that don't have an order at that price render `null` for
    their totals (Recharts breaks the area line, which correctly
    visualizes the spread valley).
  * Two `<Area>` series — `type="step"` for the staircase shape that
    depth charts conventionally use, with gradient fills (0.35 → 0.02
    opacity). Each area gets a deterministic gradient ID via
    `hashString(color)` so multiple charts on one page don't clash.
  * Mid-price `<ReferenceLine>` (dashed amber, label "mid 0.500").
  * Best bid + best ask reference lines (dashed, low opacity).
  * Spread chip overlay — top-right corner badge showing
    `Spread X.XX¢`, amber when ≥3¢, muted otherwise. Rendered as an
    absolutely-positioned div with `z-10` so it sits above the chart.
  * Custom `<Tooltip content={<DepthTooltip />}>` shows the price
    level, per-level size, and cumulative total for whichever side(s)
    have orders at the hovered price.
  * Empty state: "No order book depth available" centered message
    with `data-testid="depth-chart-empty"`.

### Step 3 — Created `src/components/charts/PriceHistoryChart.tsx`
  (~360 lines):
  * Recharts `ComposedChart` (line + area + reference dots).
  * Two modes:
    - **Self-fetch**: when `tokenId` is passed and no `bars` prop,
      polls `/api/history/ohlcv/{token_id}?resolution=X&count=N` every
      5s. Resolution selector drives re-fetch.
    - **Pre-fetched**: when `bars` prop is passed, the chart just
      renders them (no fetch loop). Used by tests.
  * Props: `tokenId?`, `bars?: PriceHistoryBar[]`,
    `resolution: HistoryResolution = '5m'` (one of 1m/5m/15m/1h/4h/1d),
    `count=60`, `height=280`, `showVolume=true`, `showMarkers=true`,
    `showRangeSelector=true`, `lineColor=chartTheme.colors.info`,
    `formatX` (HH:MM:SS default), `formatY` (3dp default),
    `onResolutionChange?`.
  * `coerceResolution(r)` helper — backend currently supports only
    `1m|5m|1h`, so 15m→5m, 4h→1h, 1d→1h. The UI still shows all 6
    range buttons (so the user picks the granularity they want), but
    the actual fetch uses the coerced resolution.
  * Visual layers (z-order back-to-front):
    1. **Volume bars**: a faint `Area` (type="bar") with vol-gradient
       (muted, 0.4 → 0.05). Volume is scaled to fit 35% of the Y-axis
       range so the price line dominates.
    2. **Price line + gradient area**: `<Area type="monotone"
       dataKey="close">` with `gradientId` fill (lineColor 0.35 → 0.02).
       `isAnimationActive=true` (400ms).
    3. **High marker**: green `<ReferenceDot>` at `(max-high timestamp,
       max-high value)` with label "H 0.625".
    4. **Low marker**: red `<ReferenceDot>` at the min-low point with
       "L 0.375" label.
  * Custom tooltip — `PriceTooltip` shows timestamp, close price,
    % change since first bar (green/red), and volume.
  * Y-domain computed from min-low to max-high with 5% padding,
    clamped to [0.001, 0.999] (probabilities).
  * Three render states:
    - **Loading** (`data-testid="price-history-loading"`): spinner +
      "Loading price history…" — shown when self-fetch is in-flight
      and no bars are yet available.
    - **Error** (`data-testid="price-history-error"`): red "⚠️ HTTP
      500" / "Network error" — `role="alert"`.
    - **Empty** (`data-testid="price-history-empty"`): "No price
      history available" — when bars array resolves to [].
  * Timestamps normalized: if a bar's timestamp > 1e12 (ms), divided
    by 1000 to convert to seconds (matches the server's epoch-seconds
    convention).
  * Time-range selector: 6 buttons (1m/5m/15m/1h/4h/1d), styled like
    the MarketChartModal selector — active button is blue-bg/black-text,
    inactive are dim with hover. Each exposes `aria-pressed` and
    `data-testid="range-btn-{r}"` for testability.

### Step 4 — Integrated into `src/components/MarketsPanel.tsx`:
  * Added `useRef` import + `prevMidsRef = useRef<Record<string,
    number>>({})` to track previous mid per token. Updated during
    render (NOT in useEffect) so the very next render of the same book
    gets the correct delta — `previousMid = prevMidsRef.current[
    token_id] ?? null` is snapshotted BEFORE the ref is mutated.
  * Replaced the static "Bid (YES)" + "Ask (YES)" + "Spread" columns
    with a single "Live Price (Bid / Ask / Δ)" column hosting
    `<PriceTicker>` per row. The PriceTicker shows mid (animated,
    directionally colored) + bid/ask chip + spread chip + change line
    — consolidating what was previously 3 columns of static text into
    one animated, info-dense cell.
  * Renamed the existing "Trade" button to "Depth" (still calls
    `onSelectMarket` → mounts `DepthChartModal`). Added a new
    "History" ghost button that sets local `historyMarket` state —
    which renders a modal hosting `<PriceHistoryChart>` for the
    row's tokenId.
  * New History modal — `modal-backdrop` + `modal modal-wide`,
    escape + backdrop-click close, hosts PriceHistoryChart with
    resolution="5m", count=60, height=320, plus an info banner noting
    bars are synthetic when no TimescaleDB candles are persisted.
  * Updated the React.memo comparator: still returns false on books/
    onSelectMarket/priceFlashes changes (unchanged). The new internal
    state (historyMarket, prevMidsRef) doesn't affect memo because
    state changes trigger re-render naturally — the memo comparator
    only gates re-renders from prop changes.

### Step 5 — Improved `src/components/DepthChartModal.tsx`:
  * Imported `MarketDepthChart` from `./charts/MarketDepthChart`.
  * Inserted a new section at the top of the modal body (above the
    existing 2-column bid/ask ladder) — a bordered card titled
    "📊 Cumulative Market Depth" with a `data-testid="…"`-free
    caption showing live bid/ask level counts, hosting the
    `<MarketDepthChart>` with `height={220}`.
  * Passes `data?.bids ?? []`, `data?.asks ?? []`, `data?.mid`,
    `data?.best_bid`, `data?.best_ask`, `data?.spread` directly from
    the existing 2s polling fetch — so the chart updates on every
    poll alongside the textual ladder.
  * The existing 2-column ladder + ML Edge panel + Quick Trade form
    are unchanged.

### Step 6 — Tests:
  * `src/components/PriceTicker.test.tsx` (29 tests, ~270 lines):
    - Mocks `framer-motion` — `motion.span` becomes a passthrough
      that applies `animate` props as inline style (so tests can
      assert on color); `AnimatePresence` becomes a fragment wrapper
      so both entering + exiting children render to the DOM.
    - `formatTickerPrice`: 4dp for <0.01, 3dp for 0.01–0.99, 2dp for
      ≥1, "—" for null/NaN/Infinity.
    - `computeChange`: up direction with correct abs+pct, down
      direction with negative abs+pct, flat when prices equal, flat
      when either is null, flat when previous is 0 (div-by-zero
      guard), flat when either is NaN.
    - Component: renders formatted price, "—" for null, up direction
      when price increased (verified via `data-direction` attribute
      on the motion.span), down direction when decreased, flat when
      equal, flat on first render (previousPrice null).
    - Change line: "+5.00¢ (+10.00%)" for up, "−5.00¢ (−10.00%)"
      (Unicode MINUS) for down, em dash for first render.
    - Bid/ask chip: renders formatted values when both sides given,
      "—" placeholders when null, suppressed in compact mode.
    - Spread chip: "4.0¢" when spread=0.04, suppressed when null,
      suppressed in compact mode (compact already disables the chip
      + change line).
    - Compact mode: no change line + no bid/ask chip.
    - Boundary formats: 0.0042 → "0.0042" (4dp), 4.5 → "4.50" (2dp).
    - Aria label: `${label}: ${formattedPrice}, +X.XX% since last tick`.
    - Re-render behaviour: direction updates across rerenders (up →
      down → up).
  * `src/components/charts/MarketDepthChart.test.tsx` (16 tests, ~250
    lines):
    - Mocks `recharts.ResponsiveContainer` as a passthrough div (same
      pattern as Charts.test.tsx) so AreaChart + Tooltip children
      render directly without a real ResizeObserver firing.
    - Renders without crashing with 5-bid + 5-ask mock data. Empty
      state when both sides empty. Renders with only one side
      populated.
    - Height prop applied to the outer wrapper.
    - Spread chip: "Spread 4.00¢" when spread=0.04, suppressed when
      null, suppressed when showSpreadChip=false. Amber colour when
      spread is wide (≥3¢, verified via text content), muted when
      narrow.
    - Mid reference line: no crash when mid is null, no crash when
      showMidLine is false even with mid set.
    - Custom color overrides + custom formatPrice/formatSize don't
      crash.
    - Single-level ladders render without crashing.
    - NaN total handled gracefully (the y-domain computation
      short-circuits at max=0 → falls back to 1).
    - ResponsiveContainer receives width="100%".
  * `src/components/charts/PriceHistoryChart.test.tsx` (19 tests,
    ~270 lines):
    - Mocks `recharts.ResponsiveContainer` (same passthrough pattern)
      AND `@/lib/api` (`getApiUrl` + `apiFetch` returns a fake
      Response with mock 5-bar OHLCV).
    - Time-range selector: all 6 buttons present (1m/5m/15m/1h/4h/1d).
    - Pre-fetched bars mode: chart renders immediately.
    - Empty state when bars=[] and no tokenId.
    - Loading state when tokenId is given and bars empty — spinner
      + "Loading price history…".
    - Self-fetch resolves: chart replaces loading state after
      `apiFetch` resolves (verified via waitFor).
    - Error state: apiFetch rejecting → "Network error"; apiFetch
      returning HTTP 500 → "HTTP 500".
    - showRangeSelector=false suppresses the range buttons.
    - showVolume=false / showMarkers=false don't crash.
    - Custom lineColor + custom formatX/formatY don't crash.
    - Range button click: aria-pressed flips (5m → 15m), and the
      `onResolutionChange` callback fires with the new resolution.
    - ms timestamps (>1e12) are normalized to seconds (no crash).
    - Single-bar dataset renders without crashing.
    - Bars with missing volume (volume field undefined) render
      without crashing even when showVolume=true.
    - ResponsiveContainer receives width="100%" + the height prop.

### Step 7 — Barrel exports:
  * `src/components/charts/index.ts` — added `MarketDepthChart`,
    `MarketDepthChartProps`, `DepthLevel`, `PriceHistoryChart`,
    `PriceHistoryChartProps`, `PriceHistoryBar`, `HistoryResolution`
    to the existing barrel so callers can use either direct path or
    `@/components/charts`.

### Verification:
  * `cd /home/z/my-project && bun run lint` — clean (0 errors, 1
    pre-existing warning in src/app/page.tsx:235 unused eslint-disable
    directive, unrelated to W15-1).
  * `cd /home/z/my-project && bun run test src/components/PriceTicker
    .test.tsx src/components/charts/MarketDepthChart.test.tsx
    src/components/charts/PriceHistoryChart.test.tsx` — 64/64 tests
    pass across the 3 new test files (29 + 16 + 19) in 14.58s.
  * `cd /home/z/my-project && bun run test` — 553 of 556 tests pass
    in 188.98s. The 3 failures are in `src/hooks/usePreferences
    .test.ts` (2) and `src/lib/preferences.test.ts` (1) — all pre-
    existing from a concurrent W15 task that modified preferences.ts
    (the `getDefaults()` reference-equality test fails because the
    factory now returns a shared reference). None are related to W15-1
    changes; verified by the fact that none of the failing tests
    import or reference any of the new components.
  * Dev server log (`/home/z/my-project/dev.log`): clean — Next.js
    16.1.3 (Turbopack) compiles `/` in 8.2s on first request, 25ms on
    subsequent. No runtime errors logged.

Stage Summary:
- Created src/components/PriceTicker.tsx (~230 lines — animated mid
  price with directional color flash + bid/ask chip + spread chip +
  change-since-last-tick line + subtle pulse background; Framer Motion
  AnimatePresence for smooth number transitions; exports
  formatTickerPrice + computeChange helpers).
- Created src/components/charts/MarketDepthChart.tsx (~330 lines —
  Recharts AreaChart with stepped bid/ask areas, gradient fills,
  mid-price reference line, best-bid/best-ask reference lines, top-right
  spread chip, custom tooltip showing price + size + cumulative).
- Created src/components/charts/PriceHistoryChart.tsx (~360 lines —
  Recharts ComposedChart with gradient-filled price line, faint volume
  bars, high/low reference dots, 6-button time-range selector
  1m/5m/15m/1h/4h/1d, self-fetch mode polling /api/history/ohlcv
  every 5s with loading/error/empty states).
- Modified src/components/MarketsPanel.tsx (replaced static Bid/Ask/
  Spread columns with single PriceTicker column; added prevMidsRef for
  previous-tick tracking; renamed "Trade" button → "Depth"; added
  "History" button + internal modal hosting PriceHistoryChart).
- Modified src/components/DepthChartModal.tsx (added MarketDepthChart
  visualization at the top of the modal body, above the existing
  2-column bid/ask ladder, fed by the existing 2s polling fetch).
- Modified src/components/charts/index.ts (added barrel exports for
  the 2 new chart components + their prop types).
- Created src/components/PriceTicker.test.tsx (29 tests, 270 lines).
- Created src/components/charts/MarketDepthChart.test.tsx (16 tests,
  250 lines).
- Created src/components/charts/PriceHistoryChart.test.tsx (19 tests,
  270 lines).
- Lint: clean. New tests: 64/64 pass. Full suite: 553/556 pass (3
  pre-existing failures in usePreferences/preferences tests from a
  concurrent W15 task — unrelated to W15-1).
