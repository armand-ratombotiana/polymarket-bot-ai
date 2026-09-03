# Performance Strategy — Polymarket Pro Workstation

> **Status**: Living document. Update whenever a new pattern is adopted.
> **Scope**: Frontend (`src/app`, `src/components`, `src/hooks`). The
> Python backend is governed separately under `docs/reassessment/`.

This document explains the memoization, polling, and code-splitting
patterns currently in place, why they were chosen, and the pitfalls to
avoid when extending the dashboard. Every rule below is paired with the
file or pattern that implements it so future contributors can verify the
rule is still being followed.

---

## 1. Memoization Strategy

### 1.1 When to use `React.memo`

Use `React.memo` on a component **only when all of the following are
true**:

1. The component renders a non-trivial amount of DOM (tables, lists,
   multi-card grids — not single buttons or labels).
2. Its parent re-renders frequently (every WebSocket tick, every poll,
   every keystroke in a sibling input).
3. **The props it receives are stable across those parent re-renders** —
   either primitives (`number`, `string`), or reference-stable
   arrays/objects/functions (`useMemo`, `useCallback`, or constants).

If condition (3) is not met, `React.memo` is a no-op: the comparator
returns `false` on every parent render because a prop identity changed.
The wrapper still adds a tiny allocation cost (a new memo object on
each render of the wrapped definition) — usually negligible, but
misleading: contributors will see `React.memo` and assume it's working.

**Currently memoized (W9-6):**

| Component            | Comparator       | Why memoized                                                                 |
| -------------------- | ---------------- | ---------------------------------------------------------------------------- |
| `PositionsPanel`     | custom           | Renders ~50 rows × 9 columns. `priceFlashes` mutates every tick, so the     |
|                      |                  | comparator JSON-diffs the flash map instead of comparing identity.           |
| `MarketsPanel`       | custom           | Renders the largest table on the dashboard. Same `priceFlashes` treatment.  |
| `OrdersPanel`        | default shallow  | All three props (`orders`, `onCancel`, `onCancelAll`) are reference-stable  |
|                      |                  | when the parent uses `useCallback`.                                          |
| `TradesPanel`        | default shallow  | Single prop (`trades`); shallow compare is sufficient.                      |
| `AnalyticsPanel`     | default shallow  | No props. Skips re-renders driven purely by parent state changes.           |

**Custom comparator rules of thumb:**

- A custom comparator is `areEqual(prev, next) => boolean` — `true`
  means "skip re-render", `false` means "re-render".
- For `Record<string, T>` props that mutate on every parent render but
  whose contents are usually identical (e.g. `priceFlashes`), use
  `JSON.stringify(prev.x) !== JSON.stringify(next.x)` rather than
  identity. The cost of `JSON.stringify` on a small map (≤20 keys) is
  well below the cost of re-rendering the table.
- Always compare callback identities last — they're the cheapest check
  and catch the most common cause of memo failure.

### 1.2 When to use `useMemo`

Use `useMemo` for any computation that:

- Iterates over an array whose length can grow (positions, trades,
  order books).
- Is recomputed on every render but whose output only depends on a
  handful of inputs.
- Returns a new object/array reference each time (so downstream
  `useEffect` deps would fire on every render without memoization).

**Examples in this codebase:**

- `PositionsPanel.filteredPositions` — `.filter().sort()` chain over the
  full positions array. Memoized on `[positions, filterQuery,
  outcomeFilter, sortBy]`.
- `PositionsPanel.totalInvested` / `totalRealized` — `.reduce()` over
  positions. Memoized on `[positions]` only — they don't depend on the
  filter or sort.
- `MarketsPanel.filtered` / `sorted` / `avgSpreadCents` — three separate
  `useMemo` blocks so each can short-circuit independently when only
  one input changes.
- `TradesPanel.displayedTrades` — `.slice(0, 100)`. Cheap, but
  memoization prevents allocating a new array reference on every
  parent re-render.

**Anti-patterns to avoid:**

- Wrapping trivial expressions in `useMemo` (e.g. `useMemo(() => a + b,
  [a, b])`). The memo allocation costs more than the addition.
- Wrapping a function that returns a primitive in `useMemo` when
  `useCallback` would express the intent more clearly.
- Listing unstable values in the dependency array — e.g. `useMemo(() =>
  books.filter(b => b.slug.includes(search)), [books, search])` where
  `search` is a state variable. This is correct, but if `search` is a
  prop, the parent must memoize it (or pass a primitive).

### 1.3 When to use `useCallback`

Use `useCallback` for any function passed as a prop to a `React.memo`-wrapped
child. Without `useCallback`, the parent creates a new function identity
on every render, the memo's shallow compare sees a different reference,
and the child re-renders anyway.

**Examples in this codebase:**

- `useBot.ts` — all five mutation actions (`activateKillSwitch`,
  `deactivateKillSwitch`, `cancelAllOrders`, `cancelOrder`,
  `closePosition`) are wrapped in `useCallback`. Without this, every
  snapshot tick (every 2s) would propagate a new identity to
  `OrdersPanel.onCancel` and `PositionsPanel.onClosePosition`, defeating
  their `React.memo` wrappers.
- `page.tsx` — `handleSelectMarketForChart`,
  `handleSelectPositionForChart`, `handleOpenCancelAllDialog`. These
  were previously inline lambdas like `(tokenId, slug) =>
  setChartMarket({ tokenId, slug })` — each render allocated a new
  arrow function, bypassing `React.memo` on `MarketsPanel` /
  `PositionsPanel` / `OrdersPanel`.
- `AnalyticsPanel.tsx` — `fetchAnalytics` is wrapped in `useCallback`
  so the polling `useEffect` only re-runs on mount, not on every render
  triggered by the 4s poll updating state.

**Don't bother with `useCallback` when:**

- The function is only used inside the same component (not passed to a
  child).
- The function is wrapped in a per-row arrow lambda in JSX (e.g.
  `onClick={() => handleClick(id)}`). The arrow lambda creates a new
  identity anyway, so stabilizing the outer function has no effect.

---

## 2. Polling Best Practices

The dashboard polls the bot API on two cadences:

| Source            | Cadence | Pause on hidden? |
| ----------------- | ------- | ---------------- |
| `useBot` REST fallback | 2s | Yes (W9-6) |
| WebSocket primary       | event-driven | No (server pushes) |
| `AnalyticsPanel` self-poll | 4s | Yes (W9-6) |
| `MLValidationPanel`     | 30s | Yes (Wave 8) |
| `RetentionPanel`        | 60s | Yes (Wave 8) |

### 2.1 Pause on `document.hidden`

Every `setInterval`-based poller in the dashboard listens for
`visibilitychange` and clears its interval when `document.hidden`
becomes `true`. When the tab is re-shown, the poller:

1. Immediately fires one fetch (so the user sees fresh data without
   waiting up to the full interval).
2. Restarts the interval cadence.

This saves backend quota (a backgrounded trading dashboard shouldn't
hit `/api/analytics` 15 times a minute) and prevents React re-render
storms on tabs nobody is looking at.

**Reference implementation** (`AnalyticsPanel.tsx`):

```ts
const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
useEffect(() => {
  const start = () => { fetchAnalytics(); intervalRef.current = setInterval(...) }
  const stop  = () => { clearInterval(intervalRef.current); intervalRef.current = null }
  const onVis = () => { document.hidden ? stop() : start() }
  start()
  document.addEventListener('visibilitychange', onVis)
  return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
}, [fetchAnalytics])
```

The `useBot` REST poller uses the same pattern but the visibility
listener is registered in a separate effect (because the polling
interval is set up in the main connect effect).

### 2.2 Stale-while-revalidate

`useBot.fetchRestSnapshot` never sets a "loading" flag during a poll.
It preserves the existing `snapshot` state in the UI while the network
request is in-flight, then atomically swaps in the new data on
success. This avoids a flash of empty content every 2 seconds.

The `status` field is also gated: it only transitions
`connecting → connected` on the first successful connect. Subsequent
polls leave `status` untouched so the status bar doesn't flicker.

**Anti-patterns to avoid:**

- `setLoading(true)` at the start of every poll — the user sees a
  spinner every 2s and loses context.
- Throwing away the previous snapshot when a fetch starts — the
  dashboard would go blank between the fetch start and finish.
- Treating a single failed poll as a hard error — the WebSocket may
  still be live, and the next poll will likely succeed. `useBot`
  preserves the last good snapshot and only escalates to
  `disconnected` when both WebSocket and REST have failed.

### 2.3 Batching requests

`useBot.fetchRestSnapshot` falls back to a composite fetch when
`/api/snapshot` is unavailable. The six sub-endpoints (`/api/orderbooks`,
`/api/status`, `/api/events`, `/api/orders`, `/api/positions`,
`/api/trades`) are fetched in parallel via `Promise.all`, not
sequentially. Worst-case latency is `max(t_i)`, not `sum(t_i)`.

Each sub-fetch is also `.catch(() => null)`-wrapped so a single
sub-endpoint failure doesn't reject the whole composite — the panel
falls back to an empty array for that section but still renders the
others.

### 2.4 `useCallback` on fetchers

Every fetch function used inside a `useEffect` polling loop is wrapped
in `useCallback` so the effect's dependency array stays stable. Without
this, the effect would re-run on every render — clearing and restarting
the interval each time, leaking timers, and re-fetching in a tight
loop.

---

## 3. Code-Splitting Strategy

### 3.1 What gets dynamic-imported

Every panel that touches `window`, `localStorage`, or `matchMedia` at
module scope or during initial render is loaded with
`next/dynamic(..., { ssr: false })`. This prevents:

- Next.js from trying to evaluate the module on the server (which
  throws because `window` doesn't exist there).
- An SSR/client mismatch warning when the server-rendered HTML doesn't
  match the client's first render.

The following panels are currently dynamic-imported (Wave 8):

- `ShadowInferencePanel`, `MLValidationPanel`
- `AttributionPanel`, `ExecutionQualityPanel`, `ClosedPositionsPanel`
- `CapitalAllocatorPanel`
- `ObservabilityPanel`, `RetentionPanel`, `DecisionLedgerPanel`,
  `LiveSafetyGatePanel`

### 3.2 Loading skeletons (W9-6)

Every `dynamic()` call declares a `loading:` option that renders
`<PanelLoadingSkeleton label="Loading …" />` instead of `null`. The
skeleton:

- Matches the panel-card visual language (dark background, border,
  header with spinner).
- Prevents a flash of empty / unstyled content while the chunk
  downloads.
- Includes `role="status"` + `aria-live="polite"` so screen readers
  announce the loading state.

The `lazyPanel(loader, label)` helper in `page.tsx` collapses the
`{ ssr: false, loading: () => <PanelLoadingSkeleton label={label} /> }`
boilerplate into a one-liner, reducing the chance of forgetting the
skeleton on a future panel.

### 3.3 What stays static-imported

Panels that:

- Are on the command-center grid (always visible — no benefit to
  splitting).
- Don't touch `window` at module scope (so SSR works).
- Are small enough that splitting would add more overhead (a separate
  chunk + extra HTTP request) than it saves.

These stay as direct `import` statements at the top of `page.tsx`:

- `RiskStatusPanel`, `EquityCurve`, `AnalyticsPanel`, `MLPanel`,
  `EventLog`, `MarketsPanel`, `MarketScreener`, `PositionsPanel`,
  `OrdersPanel`, `TradesPanel`, `StrategyMatrix`,
  `ArbitrageMatrixView`, `DeepAnalysisView`, `AIMLCommandCenter`,
  `AICopilotPanel`, `LeaderboardPanel`, `BacktestLabView`,
  `SystemHealthView`, `DatabaseExplorerView`.

### 3.4 Bundle size targets

Run `./scripts/analyze-bundle.sh` after any panel addition or large dep
change. Targets:

| Route                  | First Load JS (target) | Notes                                   |
| ---------------------- | --------------------- | --------------------------------------- |
| `/`                    | < 250 KB              | Initial bundle: layout + command center |
| dynamic chunks         | < 30 KB each          | Per-panel overhead after split          |

A +50 KB jump on `/` is a regression — investigate before merging.

---

## 4. Common Performance Pitfalls

### 4.1 Inline lambdas as props to memoized children

```tsx
// ❌ Bad: new function identity every render, defeats React.memo
<PositionsPanel onSelectMarket={(m) => setChartMarket(m)} />

// ✅ Good: stable identity
const handleSelectMarket = useCallback((m: { tokenId: string; slug: string }) => {
  setChartMarket(m)
}, [])
<PositionsPanel onSelectMarket={handleSelectMarket} />
```

### 4.2 Object literals as props

```tsx
// ❌ Bad: new object every render
<Chart config={{ type: 'line', animate: true }} />

// ✅ Good: hoist to a constant or useMemo
const CHART_CONFIG = { type: 'line', animate: true } as const
<Chart config={CHART_CONFIG} />
```

### 4.3 `useEffect` with unstable dependencies

```tsx
// ❌ Bad: `data` is set every render → effect runs forever
useEffect(() => {
  setInterval(() => fetch(data.id), 1000)
}, [data])

// ✅ Good: extract the id primitive
useEffect(() => {
  const id = data.id
  const t = setInterval(() => fetch(id), 1000)
  return () => clearInterval(t)
}, [data.id])
```

### 4.4 Polling without `document.hidden` guard

Background tabs still run `setInterval`. A dashboard left open in a
background tab will poll indefinitely, burning backend quota and
CPU. Always add the `visibilitychange` listener (see §2.1).

### 4.5 Treating `React.memo` as a silver bullet

`React.memo` only helps if the props are stable. If a component
consumes context that changes every render, `React.memo` does nothing
— the component re-renders because context changed, not because props
changed. In that case, split the component so only the part that
depends on context re-renders.

### 4.6 `JSON.stringify` in a hot loop

The custom comparators in `PositionsPanel` and `MarketsPanel` use
`JSON.stringify` to diff `priceFlashes`. This is fine because the map
is small (≤20 keys) and only runs once per re-render attempt. It
would be a problem in a tight loop (e.g. inside `.map()`).

### 4.7 Forgetting `loading:` on `dynamic()`

Without `loading:`, the dynamic chunk renders `null` while
downloading. On slow networks or large chunks, the user sees a blank
pane for a second or two. Always declare `loading:` (use the
`lazyPanel` helper).

---

## 5. Verification Checklist

Before merging a new panel or hook change, verify:

- [ ] `bun run lint` passes with zero errors and zero warnings.
- [ ] `bunx tsc --noEmit` introduces no new errors (pre-existing errors
      in `src/app/api/bot/route.ts` are tracked separately).
- [ ] The dev server (`bun run dev`) compiles `/` without warnings.
- [ ] If a new panel was added: it's wrapped in `React.memo` if it's
      data-heavy, OR it's dynamic-imported if it touches `window`.
- [ ] If a new poller was added: it pauses on `document.hidden`.
- [ ] If new props are passed to a memoized panel: they're either
      primitives, `useMemo`'d, or `useCallback`'d.
- [ ] Run `./scripts/analyze-bundle.sh` after large changes and confirm
      the First Load JS on `/` hasn't regressed.
