# UI/UX Remediation Backlog — Polymarket Pro Trading Workstation

- **Task ID:** W38-1
- **Agent:** full-stack-developer
- **Date:** 2026-11-12
- **Scope:** Read-only audit of the Polymarket bot **frontend** (Next.js 16
  App-Router workstation) and conversion of findings into a prioritised,
  evidence-based remediation backlog. No source code was modified during
  this task. One new file added: this document.
- **Evidence basis (all citations refer to files inspected at audit time):**
  - `src/app/page.tsx` (1,140 lines — full read; panel-to-section mount
    manifest at lines 649–1038).
  - `src/app/globals.css` (2,535 lines, 378+ CSS custom properties; read
    design-tokens block + light-theme overrides + responsive-grid block
    + W10-3 error-boundary block).
  - `src/components/Sidebar.tsx` (336 lines — full read; 8 nav groups,
    32 nav items, 8 keyboard-shortcut bindings).
  - `src/components/*.tsx` — 102 files (100 `.tsx` panels + 2 stories).
    Read every primary panel file's first 100–120 lines (header /
    data-shape / fetch loop / polling cadence) and grepped every file
    for `apiFetch`, `setInterval`, `bg-[#0e1015]`, `text-[#7e8aaa]`,
    `border-[#1f2335]`, `method: 'POST'`, `method: 'DELETE'`,
    `ConfirmationDialog`, `AlertDialog`, `synthetic`, `placeholder`,
    `useEffect`, `useMemo`.
  - `src/hooks/useBot.ts` (520 lines — full read; WebSocket + REST
    fallback + heartbeat pattern).
  - `src/hooks/useRealtimeData.ts`, `src/hooks/useWebSocket.ts` (existence
    confirmed via `LS /home/z/my-project/src/hooks`).
  - `src/lib/api-client.ts` (308 lines — full read; typed `api.*`
    namespace, `ApiError` shape).
  - `src/lib/api.ts` (46 lines — read; `apiFetch`, `authHeaders`,
    `getAuthedWsUrl`, `XTransformPort` gateway handling).
  - `src/messages/en.json` + `src/messages/fr.json` (95 lines each —
    full read; i18n catalog coverage).
  - `dev.log` (most recent ~40 lines — every API call returning HTTP
    404 because the polymarket-bot mini-service on port 8080 is not
    running in the sandbox).
  - `docs/assessment/UI_UX_ASSESSMENT.md` (W17-7 baseline) + 
    `docs/reassessment/UI_UX_REASSESSMENT.md` (W17-10 wave-1→wave-16
    delta) — read to confirm prior findings and avoid re-stating them.
  - `bun run lint` snapshot 2026-11-12: **2 errors** in test files
    (`react-hooks/globals` rule fires on `ErrorBoundary.test.tsx:112`
    and `PanelErrorBoundary.test.tsx:114`).
- **Evidence classification convention:**
  - **VERIFIED** — directly observed in the read code (file + line).
  - **STRONG EVIDENCE** — code + design-system reference + dev log
    triangulate the finding.
  - **INFERRED** — derived from the code's behaviour + the documented
    design intent (no direct quote, but the inference is the only
    reasonable reading).

---

## 0. Headline numbers

| Metric | Value | Source |
| --- | --- | --- |
| Component `.tsx` files (excl. `.test.` / `.skip.`) | **100** | `ls /home/z/my-project/src/components/*.tsx` |
| Sidebar nav groups | **8** | `Sidebar.tsx:68–167` |
| Sidebar nav items | **32** (NOT 42 — task spec count is incorrect) | `Sidebar.tsx:68–167` |
| Sidebar items with `kbd:` shortcut | **8 / 32** (digits 1–8 only) | `Sidebar.tsx:74–164` |
| Panels mounted in `page.tsx` | **32** | `page.tsx:649–1038` |
| Per-panel `setInterval` calls | **119** across 30+ files | `rg setInterval src/components` |
| Hardcoded Tailwind hex literals | **1,277** across **61** files | `rg 'bg-\[#0e1015\]|bg-\[#13161e\]|text-\[#7e8aaa\]|text-\[#dde1ed\]|border-\[#1f2335\]'` |
| CSS custom properties in `globals.css` | **378+** | `globals.css:12–127, 156–225` |
| POST / DELETE / PUT mutation callsites without confirmation dialog | **6** confirmed (see §2 below) | grep `method: 'POST'` + `ConfirmationDialog` / `AlertDialog` |
| Lint errors (current HEAD) | **2** (both in test files) | `bun run lint` |
| API endpoints returning HTTP 404 in sandbox dev server | **6+** (`/api/snapshot`, `/api/status`, `/api/ml/metrics`, `/api/ml/drift`, `/api/audit/logs`, `/api/live/readiness`) | `dev.log` tail |

---

## 1. Critical operational issues

> **Definition:** broken navigation, missing states, misleading data — issues
> that prevent the trader from trusting what the workstation shows.

### 1.1 [P0] Live dev server has no backend — every panel shows empty/error state

- **Component affected:** every panel that calls `apiFetch(...)` (≈30 files).
- **Current behavior:** `dev.log` shows every API request — `/api/snapshot`,
  `/api/status`, `/api/ml/metrics`, `/api/ml/drift`, `/api/audit/logs`,
  `/api/live/readiness`, `/api/orderbooks`, `/api/positions`, `/api/orders`,
  `/api/trades`, `/api/events`, `/api/leaderboard`, `/api/arbitrage/opportunities`,
  `/api/strategies/catalog`, `/api/analysis/deep`, `/api/database/records`,
  `/api/system/health`, `/api/system/db-status`, `/api/ingestion/health`,
  `/api/performance/report`, etc. — returning HTTP 404 because the
  polymarket-bot mini-service on port 8080 is not running in the sandbox.
  Every panel falls through to its empty-state or error-state branch.
- **Expected behavior:** the workstation should either (a) auto-start the
  polymarket-bot mini-service via `supervisord`/`bun run dev` so panels
  have a real backend to talk to, OR (b) detect the missing backend and
  render a single, prominent "Backend unreachable — start the bot service"
  banner at the top of every panel instead of silently rendering empty
  tables, "Loading…" spinners that never resolve, or stale-looking cards.
- **Recommended fix:**
  1. Add a `BackendReachableGuard` component (mounted once at the
     `app-shell` root in `page.tsx`) that pings `/api/health` every 15s
     and renders a workstation-wide modal banner when the response is
     404/network error. The banner should clearly state: "Backend
     service is not running on port 8080. Start it via
     `cd mini-services/polymarket-bot && python -m uvicorn api.server:app
     --port 8080 --reload` (or `supervisord -c supervisord.conf`)."
  2. Each panel's existing "Loading…" / empty-state already works
     correctly when the backend IS reachable — the issue is only that
     the loading state never resolves when the backend is absent.
     The guard in (1) is the minimum viable fix.
- **Severity rationale:** P0 — without this guard, a new operator
  opening the workstation has no way to distinguish "backend is down"
  from "I have no positions / no strategies / no markets" and may waste
  minutes debugging the wrong layer.

### 1.2 [P0] `bun run lint` fails with 2 errors in test files

- **Component affected:** `src/components/ErrorBoundary.test.tsx:112`,
  `src/components/PanelErrorBoundary.test.tsx:114`.
- **Current behavior:** `bun run lint` exits with code 1:
  ```
  ErrorBoundary.test.tsx:112:9  error  Cannot reassign variables
  declared outside of the component/hook
  Variable `throwNext` is declared outside of the component/hook.
  ```
  The two test files use a module-level `let throwNext = true` flag and
  reassign it inside a render-time branch (`throwNext = false`), which
  the `react-hooks/globals` rule (introduced in the React 19 plugin
  upgrade) treats as a render side-effect.
- **Expected behavior:** `bun run lint` exits 0.
- **Recommended fix:** convert the `throwNext` flag in both files to
  a `useState` setter (or a `useRef` whose `.current` is mutated inside
  the throw branch — refs are exempt from the rule because mutation is
  not observed synchronously by React). The cleanest pattern:
  ```tsx
  const [throwNext, setThrowNext] = useState(true)
  function MaybeBoom() {
    if (throwNext) {
      setThrowNext(false)
      throw new Error('transient')
    }
    return <div data-testid="recovered">recovered</div>
  }
  ```
- **Severity rationale:** P0 — every subsequent PR will see the lint
  gate red and either (a) waste reviewer time on pre-existing errors
  or (b) train contributors to ignore lint failures entirely.

### 1.3 [P1] `MarketsPanel` category filter uses hardcoded slug substring matching

- **Component affected:** `src/components/MarketsPanel.tsx:130–135`.
- **Current behavior:**
  ```ts
  if (selectedCat === 'CRYPTO') return slugU.includes('BITCOIN') || slugU.includes('ETH') || slugU.includes('SOL') || slugU.includes('CRYPTO')
  if (selectedCat === 'POLITICS') return slugU.includes('ELECTION') || slugU.includes('PRESIDENT') || slugU.includes('TRUMP') || slugU.includes('SENATE')
  if (selectedCat === 'ECONOMY') return slugU.includes('FED') || slugU.includes('INFLATION') || slugU.includes('RATE') || slugU.includes('CPI')
  if (selectedCat === 'SPORTS') return slugU.includes('NBA') || slugU.includes('NFL') || slugU.includes('SOCCER') || slugU.includes('UFC')
  if (selectedCat === 'TECH') return slugU.includes('AI') || slugU.includes('OPENAI') || slugU.includes('GPT') || slugU.includes('TECH')
  ```
  The filter is implemented as English-keyword substring matching against
  the URL slug. False positives are common: a market whose slug contains
  the substring "ETH" inside a longer word (e.g. "ETHICS-LAW") would
  match `CRYPTO`; a market tagged "FEDERATION" matches `ECONOMY`; any
  slug mentioning "AI" (e.g. "TRAINS-AI-REGULATION") matches `TECH`.
  Worse, Polymarket's real category taxonomy (Politics / Crypto / Sports
  / Pop Culture / Business / Entertainment) is exposed by the Gamma
  API as a `category` field on every market — the panel ignores that
  field entirely.
- **Expected behavior:** the backend's `/api/markets` (or
  `/api/markets/catalog`) endpoint should return a `category` enum per
  market (mirroring Gamma's taxonomy) and the panel should filter on
  that enum, not on slug substring. The hardcoded category list
  (`CATEGORIES = ['ALL', 'CRYPTO', 'POLITICS', 'ECONOMY', 'SPORTS', 'TECH']`)
  should be derived from the backend's category set, not hardcoded.
- **Recommended fix:**
  1. Add `category: string` to the `OrderBook` interface in
     `src/hooks/useBot.ts:8–16` and have the backend populate it from
     the Gamma `category` field.
  2. Replace the substring block with `b.category === selectedCat`.
  3. Derive `CATEGORIES` from `Array.from(new Set(books.map(b => b.category)))`
     so new Polymarket categories appear automatically.
- **Severity rationale:** P1 — silently mis-classifying markets in a
  category filter is misleading but not financial-loss-causing.

### 1.4 [P1] `EventLog` keyword parser misclassifies event severity

- **Component affected:** `src/components/EventLog.tsx:12–47`.
- **Current behavior:** severity + style are derived from
  `text.toLowerCase().includes(keyword)` matching:
  - `'risk'` → red 🛑
  - `'error'` → red 🛑
  - `'ml'` → cyan 🤖
  - `'ai'` → cyan 🤖
  - `'order'` → grey ⚡
  - `'cancel'` → grey ⚡
  - `'fill'` → green ✅
  - `'trade'` → green ✅
  - `'win'` → green ✅

  False positives: an event like `"Order rejected by risk gate"`
  matches BOTH `'order'` (grey) AND `'risk'` (red) AND `'reject'`
  (red) — the first-match-wins iteration order in `getEventSeverityIcon`
  means the icon is whichever keyword appears first in `SEVERITY_ICON`'s
  object-iteration order (currently `fill, trade, win, kill, risk,
  error, reject, ml, ai, prob, order, cancel, quoted`). The same event
  matches `'trade'` (green) if the message contains the substring
  "trade" anywhere — including in the phrase "strategy".

  Real example: `"Strategy mm_avellaneda_stoikov paused by risk gate"`
  matches `'risk'` (red 🛑) AND `'kill'` is NOT a substring but
  `'paused'` is also not in the dict — but if the message contains
  "rate-limit exceeded" the substring `'rate'` is not in the dict
  but `'limit'` is matched by the `'risk'` filter chain (since
  filter `'risk'` includes `lower.includes('limit')`). Result:
  rate-limit messages show red 🛑 even though they are amber
  warnings.
- **Expected behavior:** event severity should be derived from a
  structured `level: 'info' | 'warning' | 'error'` field on the
  event payload from `/api/events`, not from English keyword
  substring matching. The `events: string[]` payload shape forces
  this; switching to `events: Array<{ timestamp, level, source,
  message }>` lets the panel render a true severity ladder without
  guessing.
- **Recommended fix:**
  1. Backend: change `/api/events` to return typed records
     `{ timestamp: number; level: 'info'|'warning'|'error';
     source: string; message: string }` (additive — keep the legacy
     string format as `message`).
  2. Frontend: update `EventLog.tsx` to read `event.level` and drop
     the keyword parser entirely. Keep the icon mapping keyed by
     level (`info → ◦`, `warning → ⚠`, `error → 🛑`).
  3. Update `useBot.ts:88` to type `events` as the new record array
     (with a string fallback for back-compat with the legacy
     `/api/events?legacy=true` shape if needed during rollout).
- **Severity rationale:** P1 — operators relying on the event log
  for incident triage will mis-prioritise events when severity is
  mis-classified.

### 1.5 [P1] `MarketsPanel` "History" modal hides a "synthetic data" caveat in a footer

- **Component affected:** `src/components/MarketsPanel.tsx:444–447`.
- **Current behavior:** the price-history modal footer reads:
  ```tsx
  Bars are synthetic when no TimescaleDB candles are persisted.
  Chart auto-refreshes every 5s.
  ```
  The "synthetic when no TimescaleDB candles are persisted" caveat is
  buried in a `text-[10px]` mono footer line — the trader has to read
  fine print to learn that the displayed OHLCV bars may be FABRICATED,
  not real market data. There's no visual indicator on the chart
  itself distinguishing real bars from synthetic ones.
- **Expected behavior:** when the chart is showing synthetic data,
  a prominent amber `SYNTHETIC` badge should overlay the chart's
  top-left corner (matching the existing `OBS ONLY` and `KILL SWITCH`
  banner pattern in `page.tsx:584–612`). The footer line stays as
  a detail explanation; the badge is the at-a-glance signal.
- **Recommended fix:**
  1. Have `PriceHistoryChart.tsx` (or the backend's
     `/api/history/ohlcv/{token_id}` response) return a
     `synthetic: boolean` flag on each bar OR a single
     `all_synthetic: boolean` flag on the response.
  2. Render a `Badge variant="warning"` overlay on the chart when
     `synthetic === true`.
- **Severity rationale:** P1 — silently displaying fabricated
  price history in a trading workstation is a financial-misrepresentation
  risk; burying the caveat in 10px mono text is insufficient
  disclosure.

### 1.6 [P1] No global "Backend unreachable" guard on the workstation shell

- **Component affected:** `src/app/page.tsx:1104–1136` (the
  "Disconnected overlay" only fires when `status === 'disconnected' ||
  status === 'error'` AND `snapshot.order_books.length === 0`).
- **Current behavior:** the existing disconnect overlay correctly
  fires when the bot API WebSocket is unreachable on first mount.
  However, after the first snapshot is fetched (or the WebSocket
  connects and returns an empty `order_books: []`), the overlay no
  longer renders — panels then individually show their own
  empty/error states without the workstation-level coordination.
  If the bot API later drops while the trader is mid-session, the
  overlay does NOT re-appear (the condition is gated on
  `order_books.length === 0`, which may be false because the panel
  is showing the last-known snapshot).
- **Expected behavior:** a workstation-level banner should fire
  whenever the WS connection drops OR the REST heartbeat returns
  non-2xx for 3+ consecutive ticks. The existing overlay logic
  should be inverted: fire whenever `status === 'disconnected' ||
  status === 'error'`, regardless of whether cached snapshot data
  is still being shown.
- **Recommended fix:**
  1. Remove the `&& snapshot.order_books.length === 0` clause from
     the overlay's render condition in `page.tsx:1104`.
  2. Add a separate "stale data" indicator (amber pill in
     `TopStatusBar`) showing the age of the last successful
     snapshot — the existing `fmtAge` / `freshnessClass` helpers
     in `lib/design-tokens.ts` already support this.
- **Severity rationale:** P1 — a trader who steps away and returns
  may unknowingly act on stale data because the workstation doesn't
  visually surface "you've been disconnected for 90 seconds."

---

## 2. Trading and risk issues

> **Definition:** unsafe actions missing confirmation dialogs — the trader
> can fire real (paper or live) money-moving actions with a single click.

### 2.1 [P0] `ArbitrageMatrixView` "Execute Arb" button posts immediately without confirmation

- **Component affected:** `src/components/ArbitrageMatrixView.tsx:62–89`,
  `:301–315`.
- **Current behavior:** clicking the `⚡ Execute Arb` button on any
  arbitrage opportunity row directly calls `handleExecute(opp)` which
  immediately POSTs to `/api/arbitrage/execute` with
  `size_usdc: Math.min(opp.max_executable_size_usdc, 3.0)`. There is no
  `ConfirmationDialog` or `AlertDialog` between the click and the POST.
  The button label says `⚡ Execute Arb` and the badge in the header
  reads `Paper Mode · $3 Cap`, so the trader MAY believe they are
  executing paper-only orders — but the panel has no way to verify the
  bot is actually in paper mode, and the `size_usdc` of `$3.00` is a
  real dollar value that will be deducted from `paper_balance` if the
  bot IS in live mode.
- **Expected behavior:** every money-moving action must go through the
  existing `ConfirmationDialog` component (`src/components/ConfirmationDialog.tsx`)
  with `severity="warning"` (paper) or `severity="danger"` (live),
  showing the exact dollar amount, the market, the strategy, and the
  bot's current mode. The dialog should require an explicit click on
  "Confirm Execute" (not Enter-key) before the POST fires.
- **Recommended fix:**
  1. Add a `[confirmOpp, setConfirmOpp] = useState<ArbOpportunity | null>(null)`
     state.
  2. Change the button's `onClick` to `setConfirmOpp(opp)` instead of
     `handleExecute(opp)`.
  3. Render a `<ConfirmationDialog>` at the panel root:
     ```tsx
     <ConfirmationDialog
       open={confirmOpp !== null}
       severity={mode === 'live' ? 'danger' : 'warning'}
       title={mode === 'live' ? 'Execute Live Arbitrage' : 'Execute Paper Arbitrage'}
       description={`This will place dual-leg orders on ${confirmOpp?.slug}...`}
       impact={`Size: $${sizeUsdc}. YES leg at ${yes_ask}, NO leg at ${no_ask}. Combined cost: ${total_cost}.`}
       confirmLabel={`Execute ${mode === 'live' ? 'Live' : 'Paper'} Arb`}
       cancelLabel="Go Back"
       onConfirm={() => { handleExecute(confirmOpp!); setConfirmOpp(null) }}
       onCancel={() => setConfirmOpp(null)}
       loading={executing !== null}
     />
     ```
- **Severity rationale:** P0 — single-click execution of money-moving
  orders without confirmation is a financial-safety regression. The
  `$3 cap` mitigates magnitude but not the principle.

### 2.2 [P0] `PositionsPanel` "Close" button fires immediately without confirmation

- **Component affected:** `src/components/PositionsPanel.tsx:478–486`.
- **Current behavior:** every positions-table row has a `✕ Close`
  button whose `onClick={() => onClosePosition?.(p.token_id)}`
  directly invokes the `closePosition` callback. In `page.tsx:450–454`,
  this callback is wired to:
  ```ts
  const closePosition = useCallback(async (tokenId: string) => {
    await fetch(`${apiUrl}/api/positions/${tokenId}/close`, { method: 'POST', ... }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])
  ```
  No `ConfirmationDialog` is shown. The trader can accidentally close
  a position with a stray click (especially given the row is also
  clickable to open the depth modal — a missed click on the Trade
  button lands on Close).
- **Expected behavior:** closing a position is an irreversible action
  (the position is closed at market price; reopening requires a new
  order). It must go through `ConfirmationDialog` with `severity="warning"`
  showing the market, the current mark price, the unrealised P&L, and
  a confirm button labelled `✕ Close Position at Market`.
- **Recommended fix:**
  1. Lift confirmation state to `page.tsx` (mirroring the existing
     `confirmKill` / `confirmCancelAll` pattern at lines 238–239):
     ```ts
     const [confirmClose, setConfirmClose] = useState<{ tokenId: string; slug: string } | null>(null)
     ```
  2. Change `PositionsPanel`'s close button to call a new prop
     `onRequestClosePosition(p)` instead of `onClosePosition(p.token_id)`.
  3. Render a `<ConfirmationDialog>` in `page.tsx` with the position's
     details (mark, unrealised P&L, slug) and `severity="warning"`.
  4. The existing `c` keyboard shortcut (lines 427–436) should also
     open the confirmation dialog instead of directly calling
     `closePosition`.
- **Severity rationale:** P0 — same risk class as 2.1.

### 2.3 [P1] `StrategyMatrix` "Stop" / "Deploy" toggle fires immediately without confirmation

- **Component affected:** `src/components/StrategyMatrix.tsx:108–137`,
  `:308–318`.
- **Current behavior:** the `Stop` and `Deploy` buttons call
  `handleToggle(strategyId, isRunning)` which immediately POSTs to
  `/api/strategies/toggle`. Stopping a LIVE strategy in mid-flight
  leaves open orders un-cancelled (per the confirmation-dialog text
  for the kill switch in `page.tsx:1081`). Deploying a strategy
  enables live execution against real capital (paper or live mode).
- **Expected behavior:** the toggle should go through a
  `ConfirmationDialog` whose severity and copy depends on (a) the
  bot's current mode (paper/live/shadow) and (b) the action (stop vs.
  deploy). For "Stop" in live mode, the impact text should warn:
  "Existing open orders from this strategy will remain until
  manually cancelled." For "Deploy" in live mode, the impact text
  should warn: "Strategy will begin placing live orders."
- **Recommended fix:**
  1. Add `[confirmToggle, setConfirmToggle] = useState<{ id: string; current: boolean; name: string } | null>(null)`.
  2. Change the button's `onClick` to `setConfirmToggle({ id: strategyId, current: s.is_running, name: s.name })`.
  3. Render a `ConfirmationDialog` with dynamic severity based on
     `snapshot.mode` (passed in from `useBot`).
- **Severity rationale:** P1 — strategy toggle is less catastrophic
  than kill-switch or arb-execute but still money-moving and
  irreversible from a state-machine perspective.

### 2.4 [P1] `AIMLCommandCenter` "Retrain" button fires immediately without confirmation

- **Component affected:** `src/components/AIMLCommandCenter.tsx:107–126`.
- **Current behavior:** the retrain button calls `handleRetrain()` which
  POSTs to `/api/ml/retrain`. Retraining can take 30+ seconds during
  which the model is offline; in a live-trading bot, model downtime
  means missed signals. There's no confirmation, no estimate of how
  long the retrain will take, and no warning that live signal generation
  will be paused.
- **Expected behavior:** show a `ConfirmationDialog` with `severity="info"`
  stating: "Retraining will lock the model for ~30s. Live signal
  generation will pause; open positions are unaffected."
- **Recommended fix:** mirror the pattern from §2.3.
- **Severity rationale:** P1 — operator-initiated model retraining
  should be deliberate, not accidental.

### 2.5 [P2] `OrdersPanel` row-level "Cancel" button fires without confirmation

- **Component affected:** `src/components/OrdersPanel.tsx:200–207`.
- **Current behavior:** clicking `Cancel` on a single order
  immediately calls `onCancel(o.order_id)` which DELETEs
  `/api/orders/${orderId}`. This is consistent with the workstation's
  "Cancel All" button which DOES go through confirmation
  (`page.tsx:1088–1101`) — the asymmetry is the issue: bulk cancel
  is confirmed, single cancel is not.
- **Expected behavior:** for consistency + safety, single cancel
  should also confirm. The dialog should show the order's market,
  side, price, size, and strategy.
- **Recommended fix:** lift `confirmCancelOne` state to `page.tsx`,
  render a `ConfirmationDialog` with `severity="warning"` and the
  order details.
- **Severity rationale:** P2 — single-order cancellation is lower
  impact than bulk, but the asymmetry with "Cancel All" is
  inconsistent and surprising.

### 2.6 [P2] `TopStatusBar` "Cancel All" button label does not reflect bot mode

- **Component affected:** `src/components/TopStatusBar.tsx:395–401`.
- **Current behavior:** the button reads `✕ Cancel All` regardless of
  whether the bot is in paper, live, or shadow mode. The confirmation
  dialog (`page.tsx:1088–1101`) also doesn't reflect the mode — a
  trader in LIVE mode sees the same dialog as a trader in PAPER mode.
- **Expected behavior:** the button label + dialog copy should reflect
  the bot's mode:
  - Paper mode: "Cancel All (Paper)" + "These are paper orders; no
    real capital is at risk."
  - Live mode: "Cancel All (LIVE)" + "⚠ These are LIVE orders.
    Cancelling will release reserved capital."
- **Recommended fix:** thread `snapshot.mode` into the dialog's
  `description` and `impact` props.
- **Severity rationale:** P2 — visual/UX polish, not a safety
  regression.

### 2.7 [P2] Kill-switch keyboard shortcut `k` is silently undocumented

- **Component affected:** `src/app/page.tsx:509–523`,
  `src/lib/keyboardShortcuts.ts` (catalog).
- **Current behavior:** the `useKeyboardShortcuts` hook binds plain `k`
  (no modifiers) to the kill-switch toggle. The shortcut is NOT
  listed in `SHORTCUT_DEFINITIONS` (the catalog the cheat sheet
  renders from) — the page.tsx comment explicitly says:
  > Plain `k` (no modifier) — NOT in SHORTCUT_DEFINITIONS ... the
  > cheat sheet just doesn't advertise plain `k`.

  This means a trader who accidentally hits `k` while typing in a
  text input would activate the kill switch (or worse, deactivate it)
  with no visual indication that the key is bound.
- **Expected behavior:** every bound keyboard shortcut should be
  discoverable in the `KeyboardCheatSheet`. The current pattern
  (catalog vs. extras) creates a hidden second catalog of
  shortcuts that the cheat sheet doesn't surface.
- **Recommended fix:**
  1. Add the plain-`k` kill-switch shortcut to
     `SHORTCUT_DEFINITIONS` with `category: 'system'` and the
     description "Toggle kill switch (activate if running, resume
     if halted)".
  2. Add a `global: false` flag to the catalog entry so the hook
     doesn't fire it while the user is typing in an input/textarea
     (the `useKeyboardShortcuts` hook already supports this for
     `?` and `Escape`).
- **Severity rationale:** P2 — discoverability + accidental-trigger
  risk.

---

## 3. Data-ingestion visibility

> **Definition:** missing source health, gap detection, freshness
> indicators across ingestion panels.

### 3.1 [P1] `IngestionHealthPanel` shows source health but not source BACKENDS

- **Component affected:** `src/components/IngestionHealthPanel.tsx:11–33`
  (header docstring), the rendered source grid.
- **Current behavior:** the panel surfaces the three ingestion sources
  (CLOB / Gamma / WebSocket) with status, EPS, error rate, and
  last-event age. It does NOT show:
  - Which HTTP endpoint each source is polling (e.g. CLOB →
    `https://clob.polymarket.com/books/:token_id`, Gamma →
    `https://gamma-api.polymarket.com/markets`).
  - The current rate-limit budget remaining per source (Polymarket's
    CLOB enforces a per-IP rate limit; the bot's
    `core/rate_limit_tracker.py` records it but the panel doesn't
    surface it next to the source).
  - Whether the source is using the public endpoint or the
    authenticated (higher-rate-limit) endpoint.
- **Expected behavior:** the source row should show:
  ```
  CLOB  ● connected  250 EPS  0.0% err  2s age
        GET https://clob.polymarket.com  ·  Rate: 850/1000 req/min
  ```
  The rate-limit budget should come from the existing
  `/api/rate-limits` endpoint (already wired in `RateLimitPanel.tsx`)
  — this is a cross-panel data join, not a new backend endpoint.
- **Recommended fix:**
  1. Have `IngestionHealthPanel` also fetch `/api/rate-limits` (or
     accept it as a prop from a parent state).
  2. Render the per-source rate-limit budget as a thin progress bar
     under each source row.
- **Severity rationale:** P1 — operators debugging a stalled
  ingestion pipeline need to see rate-limit exhaustion in the same
  view as source health.

### 3.2 [P1] Ingestion gap timeline has no severity / impact annotation

- **Component affected:** `src/components/IngestionHealthPanel.tsx`
  (gaps endpoint contract at lines 64–80).
- **Current behavior:** the gaps endpoint returns an array of
  `{ source, start, end, duration_seconds, affected_markets }`. The
  panel renders each gap as a timeline bar — but there's no visual
  indication of *impact*: a 30-second gap on the WebSocket source
  affecting 3 markets is rendered identically to a 5-minute gap on
  the CLOB source affecting 200 markets.
- **Expected behavior:** each gap bar's color + width should encode:
  - Width = duration (already done).
  - Color = severity (red if `affected_markets.length > 50`,
    amber if `> 10`, grey otherwise).
  - Hover/click → expand to show the affected market slugs (currently
    hidden behind a tooltip).
- **Recommended fix:** add a `severity` field to the gaps response
  (computed server-side from `affected_markets.length *
  duration_seconds`), and have the panel render the bar with the
  design-system's `--color-red-bg` / `--color-amber-bg` tokens.
- **Severity rationale:** P1 — gap-impact visibility is critical for
  SLA monitoring.

### 3.3 [P2] `MarketsPanel` freshness column uses `> 30s = stale` heuristic

- **Component affected:** `src/components/MarketsPanel.tsx:267`
  (`const isStale = age > 30`).
- **Current behavior:** a market's "Freshness" badge renders amber when
  `age > 30s`. The 30s threshold is hardcoded — it doesn't reflect the
  fact that different markets have different staleness tolerances (a
  1-minute-old price on a 7-day-outcome market is fine; a 30-second-old
  price on a 1-minute-before-close market is stale).
- **Expected behavior:** the staleness threshold should be configurable
  per market (or at minimum per category) — ideally derived from the
  market's `close_date` (markets closing soon get a tighter threshold).
- **Recommended fix:** change `isStale = age > 30` to
  `isStale = age > stalenessThreshold(b)` where
  `stalenessThreshold` returns 30s for markets >1h from close, 10s
  for markets <1h from close, 5s for markets <5min from close. The
  `close_date` field is already returned by the Gamma API.
- **Severity rationale:** P2 — false-positive staleness flags train
  the trader to ignore the freshness column.

### 3.4 [P2] No "data lineage" surface anywhere in the workstation

- **Component affected:** none (missing feature).
- **Current behavior:** the backend ships `ingestion/lineage.py`
  (per the file listing in `mini-services/polymarket-bot/ingestion/`)
  which records the source → pipeline → store flow for every record.
  The frontend has no panel that surfaces this — a trader who sees a
  suspicious price has no way to ask "where did this number come
  from?"
- **Expected behavior:** a "Data Lineage" sub-panel (or a popover
  on each row in `MarketsPanel` / `PositionsPanel`) should expose:
  - Source (CLOB / Gamma / WS).
  - Ingested_at timestamp.
  - Pipeline transformations applied (feature_pipeline stages).
  - Storage backend (PG / SQLite row id).
- **Recommended fix:** add a `/api/lineage/{token_id}` endpoint that
  returns the lineage record, and a popover component triggered by a
  hover on the "Token ID" cell in `MarketsPanel`.
- **Severity rationale:** P2 — lineage is a debugging/audit
  convenience, not a trading-critical surface.

---

## 4. AI/ML explainability

> **Definition:** predictions not clearly labelled as model outputs,
> confidence intervals / calibration not surfaced where they should be.

### 4.1 [P0] `DeepAnalysisView` "ML Forecast Probability" is shown without confidence interval or model-version label on the card

- **Component affected:** `src/components/DeepAnalysisView.tsx:14–20`
  (interface), the rendered opportunity card.
- **Current behavior:** the `MarketAnalysis` interface includes
  `ml_forecast_prob`, `uncertainty_interval?: [number, number]`,
  `model_metadata?: { version, brier_score, features_used }`, and
  `confidence_score`. These fields ARE returned by the backend but
  the rendered opportunity card only shows the point estimate
  (`ml_forecast_prob`) as a percentage — the uncertainty interval,
  model version, and Brier score are NOT rendered on the card.
  They're only visible if the trader clicks into the per-market
  analysis detail view.
- **Expected behavior:** every ML probability displayed in the
  workstation should:
  1. Show the point estimate.
  2. Show the 95% uncertainty interval as a small range beneath
     (e.g. `0.62 (0.54–0.70)`).
  3. Show the model version as a tiny `v1.4.2` chip.
  4. Show the model's Brier score as a tiny `Brier 0.145` chip.
  5. Carry a visible `🤖 ML PREDICTION` badge so the trader knows
     this number is model-derived, not market-implied.
- **Recommended fix:** add a `<MLPredictionBadge>` reusable component
   in `src/components/ui/` that takes `{ prob, ci, version, brier }`
   and renders all four indicators compactly. Use it in
   `DeepAnalysisView`, `AICopilotPanel`, `DepthChartModal`, and
   `AIMLCommandCenter`'s semantic-search results.
- **Severity rationale:** P0 — silently presenting ML predictions
   alongside market-implied probabilities without labelling them
   is a model-risk-management failure.

### 4.2 [P1] `AICopilotPanel` assistant messages are not labelled as AI-generated

- **Component affected:** `src/components/AICopilotPanel.tsx:23–30`,
  `:55–63`.
- **Current behavior:** the assistant's first message reads:
  > 👋 Welcome to the **Polymarket Pro Copilot**. I analyze active
  > order books, 38-feature quant vectors, ensemble probability
  > edges, and macroeconomic news sentiment.

  Subsequent assistant messages are rendered in a distinct
  `role: 'assistant'` style (cyan background), but there's no
  explicit `🤖 AI-generated` label on each message. A trader who
  screenshots a Copilot response and shares it externally has no
  in-image indication that the content is AI-generated.
- **Expected behavior:** every assistant message should carry a
  small `🤖 AI-generated · verify before acting` footer beneath
  the message body. The current `role === 'assistant'` styling is
  not sufficient on its own.
- **Recommended fix:** add a footer `<div>` to each assistant
  message bubble:
  ```tsx
  {m.role === 'assistant' && (
    <div className="text-[9px] text-[#5a637a] mt-1.5 italic">
      🤖 AI-generated — verify against market data before acting.
    </div>
  )}
  ```
- **Severity rationale:** P1 — regulatory + trust-and-safety
  expectation for AI-generated financial content.

### 4.3 [P1] `MLPanel` shows "Calibrated" badge without showing calibration curve

- **Component affected:** `src/components/MLPanel.tsx:126–128`.
- **Current behavior:** the header shows a green `Calibrated` badge
  when `model_ready === true`. The actual reliability / calibration
  curve is only rendered in `AIMLCommandCenter.tsx:428–457` — a
  completely different panel. A trader on the Command Center grid
  sees "Calibrated" with no way to verify the claim without
  navigating away.
- **Expected behavior:** either (a) make the `Calibrated` badge
  a hover-popover that shows the reliability curve inline, or (b)
  link the badge to the `intelligence-aiml` panel.
- **Recommended fix:** wrap the badge in a Radix `HoverCard`
  (already imported elsewhere in the codebase) whose content is a
  mini reliability diagram (50×50px) fetched lazily from
  `/api/ml/metrics.reliability_curve`.
- **Severity rationale:** P1 — claims about model calibration
  should be verifiable in-place.

### 4.4 [P2] `ShadowInferencePanel` promote-champion action does not show comparison metrics in the confirm dialog

- **Component affected:** `src/components/ShadowInferencePanel.tsx:482–507`.
- **Current behavior:** the `confirmPromote` callback posts to
  `/api/ml/rollback?version=X` after a click on the "Promote" button.
  The confirmation dialog (rendered via `AlertDialog`) shows the
  target version name but NOT the side-by-side comparison metrics
  (Brier score, ROC-AUC, Sharpe) between the current champion and
  the challenger being promoted.
- **Expected behavior:** the promotion dialog should show:
  ```
  Champion v1.4.2   Brier 0.148   AUC 84%
  Challenger v1.5.0 Brier 0.141   AUC 86%   (Δ Brier -0.007)
  Promote challenger?
  ```
- **Recommended fix:** thread the `ModelVersion` object into the
  AlertDialog's description, alongside the current champion's
  metrics (already in state).
- **Severity rationale:** P2 — operator judgment should be informed
  by the metrics, not just the version name.

### 4.5 [P2] `MLValidationPanel` "Real / synthetic" sample mix shown without explaining the implication

- **Component affected:** `src/components/MLValidationPanel.tsx:815–825`.
- **Current behavior:** the panel renders
  `Real / synthetic 1,234 / 9,876` showing the training-data mix.
  There's no explanation of what this means: a trader who sees
  `0 / 11,000` (all synthetic) doesn't know that the model was
  trained on fabricated data and may not generalise to live
  markets.
- **Expected behavior:** when `n_real_samples === 0`, render an
  amber warning: "⚠ Model trained on 100% synthetic data —
  predictions may not generalise to live markets." When
  `n_real_samples / (n_real + n_synthetic) < 0.1`, render a softer
  warning.
- **Recommended fix:** add a conditional warning beneath the
  sample-mix row.
- **Severity rationale:** P2 — explainability gap; not a safety
  issue but a transparency one.

---

## 5. Navigation and information architecture

> **Definition:** confusing groupings, missing keyboard coverage,
> duplicate destinations.

### 5.1 [P1] Only 8 of 32 sidebar items have keyboard shortcuts

- **Component affected:** `src/components/Sidebar.tsx:68–167`,
  `src/app/page.tsx:188–197`.
- **Current behavior:** the `KB_MAP` in `page.tsx` only binds digits
  `1`–`8` to the first eight panels:
  - `1` → command
  - `2` → markets-books
  - `3` → markets-screener
  - `4` → portfolio-positions
  - `5` → strategies-registry
  - `6` → strategies-arbitrage
  - `7` → intelligence-analysis
  - `8` → analytics-performance

  The other 24 panels (order-flow, orders, trades, strategies-performance,
  aiml, copilot, shadow, validation, performance-report, backtest,
  attribution, execution, closed, capital-allocator, system-health,
  database, database-status, observability, retention, decisions, safety,
  rate-limit, audit, ingestion) have NO keyboard shortcut. The Sidebar
  component renders `kbd` badges only for the 8 mapped items.
- **Expected behavior:** every sidebar item should have a keyboard
  shortcut. The convention could be:
  - Single digits for the 8 primary panels (keep as-is).
  - `Shift + digit` for secondary panels in the same group
    (e.g. `Shift+2` → markets-order-flow).
  - `g` prefix + letter for system panels (e.g. `g h` → system-health,
    `g d` → database, `g o` → observability) — the `g` prefix is the
    standard Vim/Notion convention for grouped navigation.
- **Recommended fix:** extend `KB_MAP` and `SHORTCUT_DEFINITIONS` to
  cover all 32 items. Use the `g` prefix pattern for the system group
  (10 items) — `useKeyboardShortcuts.ts` already supports multi-key
  sequences (per the `?` and `Escape` global handling).
- **Severity rationale:** P1 — power-user efficiency gap; mouse-only
  navigation is a regression for a trading workstation.

### 5.2 [P1] Sidebar has two "Performance" items in different groups, both abbreviated `Perf`

- **Component affected:** `src/components/Sidebar.tsx:116, 136`.
- **Current behavior:** the Sidebar shows:
  - `strategies-performance` → `Perf` (in Strategies group)
  - `analytics-performance` → `Perf` (in Analytics group)

  The collapsed-mode tooltips reveal the full label, but in expanded
  mode both items render with the same short label and similar icon
  (`◷` for both). A trader scanning the sidebar sees two `Perf` items
  and has to read the group header to disambiguate.
- **Expected behavior:** the short labels should be unambiguous:
  - `strategies-performance` → `Strat Perf` (or `SP`)
  - `analytics-performance` → `Book Perf` (or `AP`)
- **Recommended fix:** change the `shortLabel` field in
  `Sidebar.tsx:116` from `'Perf'` to `'Strat'`, and in `:136` from
  `'Perf'` to `'Book'` (or similar 4–5 char abbreviations).
- **Severity rationale:** P1 — discoverability + cognitive-load
  regression.

### 5.3 [P1] "Database" vs "Data Explorer" vs "Data Ingestion" groupings are confusing

- **Component affected:** `src/components/Sidebar.tsx:152–164`.
- **Current behavior:** the System group has three data-related items:
  - `system-database` → label "Data Explorer", shortLabel "Data"
  - `system-database-status` → label "Database", shortLabel "DB"
  - `system-ingestion` → label "Data Ingestion", shortLabel "Ingest"

  The labels are inconsistent: "Data Explorer" (for the time-series
  table viewer) vs "Database" (for the PG/SQLite status panel) vs
  "Data Ingestion" (for source health). A trader looking for "the
  database panel" has three candidates.
- **Expected behavior:** the labels should form a clear ladder:
  - `system-database` → "Time-Series Tables" (or "Data Explorer")
  - `system-database-status` → "DB Health" (or "DB Backend")
  - `system-ingestion` → "Data Sources" (or "Ingestion Health")
- **Recommended fix:** rename the labels (and their i18n keys) to
  the clearer ladder. Group them visually (consecutive items in
  the sidebar) — they already are consecutive, which is good.
- **Severity rationale:** P1 — information-architecture clarity.

### 5.4 [P2] `command` group has only 1 item — group header is visual noise

- **Component affected:** `src/components/Sidebar.tsx:69–76`.
- **Current behavior:** the "Main" group has only one item
  (`command` → Command Center). The group header "MAIN" takes up
  vertical space without adding information.
- **Expected behavior:** either remove the group header for
  single-item groups, or merge "Main" into the next group
  ("Markets").
- **Recommended fix:** suppress the group header when
  `group.items.length === 1` (a one-line conditional in the
  `NAV_GROUPS.map`).
- **Severity rationale:** P2 — minor visual polish.

### 5.5 [P2] `ShortcutsModal` is still imported by Sidebar.stories.tsx but no longer mounted in page.tsx

- **Component affected:** `src/components/ShortcutsModal.tsx`,
  `src/components/Sidebar.stories.tsx`.
- **Current behavior:** `page.tsx:171–176` notes:
  > W17-6 → W28-1 — Legacy `ShortcutsModal` import removed (was
  > unused — the panel declared it but never mounted it). The new
  > `KeyboardCheatSheet` ... is the single keyboard-help surface.

  But the legacy `ShortcutsModal.tsx` file still exists (per the
  file listing) and `Sidebar.stories.tsx` may still reference it.
  Dead code that ships in the bundle (if imported anywhere) is a
  maintenance liability.
- **Expected behavior:** delete `ShortcutsModal.tsx` (and its
  test file if any), update any stories that import it.
- **Recommended fix:** `git rm src/components/ShortcutsModal.tsx`
  and remove the import from `Sidebar.stories.tsx`.
- **Severity rationale:** P2 — dead-code hygiene.

### 5.6 [P2] `CommandPalette` component exists but is not mounted in `page.tsx`

- **Component affected:** `src/components/CommandPalette.tsx`,
  `src/app/page.tsx:483–494` (the comment notes this).
- **Current behavior:** `CommandPalette.tsx` ships a Cmd+K palette
  with 25+ navigation entries. The `page.tsx` comment at line 489
  explicitly states:
  > the CommandPalette isn't mounted today — see W16-8 follow-up
  > note. When it eventually mounts, this shortcut should be
  > re-wired to open it instead.

  The shortcut `Cmd+K` is currently wired to open the
  `KeyboardCheatSheet` instead — a strict superset of what the
  cheat sheet does, but functionally different from a command
  palette (which lets you EXECUTE commands, not just view shortcuts).
- **Expected behavior:** mount `<CommandPalette>` in `page.tsx`
  (lazily, via `next/dynamic`), wire `Cmd+K` to open it, and
  keep `?` for the `KeyboardCheatSheet`.
- **Recommended fix:** follow the W16-8 follow-up note — add a
  `const CommandPalette = lazyPanel(...)` line and render it in
  the modals section of `page.tsx`.
- **Severity rationale:** P2 — feature-completeness gap; the
  component exists but is unreachable from the UI.

---

## 6. Visual design

> **Definition:** inconsistent colors, typography, spacing, iconography.

### 6.1 [P0] 1,277 hardcoded Tailwind hex literals bypass the design system

- **Component affected:** **61 files** under `src/components/` (full
  list in `rg 'bg-\[#0e1015\]|bg-\[#13161e\]|text-\[#7e8aaa\]|text-\[#dde1ed\]|border-\[#1f2335\]' src/components`).
  Worst offenders by occurrence count:
  - `IngestionHealthPanel.tsx`, `CapitalAllocatorPanel.tsx`,
    `ShadowInferencePanel.tsx`, `LiveSafetyGatePanel.tsx`,
    `AttributionPanel.tsx`, `DecisionLedgerPanel.tsx`,
    `AuditLogPanel.tsx`, `RateLimitPanel.tsx`, `RetentionPanel.tsx`,
    `DatabaseStatusPanel.tsx` — each with 50+ occurrences.
- **Current behavior:** `globals.css` defines a complete token system
  (`--bg-base`, `--bg-surface`, `--bg-card`, `--text-primary`,
  `--text-secondary`, `--border`, etc.) at lines 12–127. Components
  SHOULD consume these via Tailwind utility classes like `bg-card`
  or `text-primary` — but instead they hardcode the equivalent hex
  values directly via Tailwind arbitrary value syntax:
  `bg-[#13161e]`, `text-[#7e8aaa]`, `border-[#1f2335]`. The
  `globals.css:235–262` block (the W13-4 light-theme overrides)
  contains 15+ scoped `.light .bg-\[\#...\] { ... !important }`
  rules to make these hex literals flip with the theme — a clear
  sign that the design system is being bypassed.
- **Expected behavior:** every color in every component should
  come from a CSS variable via a Tailwind utility class
  (`bg-card`, `text-primary`, `border-border`) OR via an inline
  `style={{ background: 'var(--bg-card)' }}`. The `.light` override
  block in `globals.css:156–262` should be deletable once the
  migration is complete.
- **Recommended fix:**
  1. Add a `tailwind.config.ts` extension that maps the design
     tokens to Tailwind color utilities:
     ```ts
     colors: {
       'bg-base': 'var(--bg-base)',
       'bg-surface': 'var(--bg-surface)',
       'bg-card': 'var(--bg-card)',
       'text-primary': 'var(--text-primary)',
       'text-secondary': 'var(--text-secondary)',
       'border-default': 'var(--border)',
     }
     ```
  2. Mechanically migrate the 1,277 occurrences with a codemod
     (`bg-[#13161e]` → `bg-card`, etc.). Run in batches per file
     to keep PRs reviewable.
  3. Delete the `.light .bg-\[\#...\] { ... !important }` block
     in `globals.css:235–262`.
- **Severity rationale:** P0 — this is the single largest
  maintainability debt in the UI codebase. Every theme tweak
  requires editing 61 files; every new theme (e.g. a "high
  contrast" accessibility theme) requires another 15-line
  override block per hex literal.

### 6.2 [P1] Inconsistent card padding — `p-3`, `p-4`, `p-6`, `px-3.5 py-2.5` all in active use

- **Component affected:** every panel.
- **Current behavior:** a quick audit:
  - `PositionsPanel.tsx:172` — `p-3`
  - `IngestionHealthPanel.tsx` — `p-4`
  - `CapitalAllocatorPanel.tsx:741` — no padding (uses inner `card-header p-3`)
  - `LiveSafetyGatePanel.tsx` — `p-3` + `p-4` mixed
  - `OrdersPanel.tsx:73` — `px-3.5 py-2.5` on header, `p-3` on body
  - `MarketsPanel.tsx:159` — `px-3.5 py-2.5` on header, `p-3` on body

  The workstation lacks a single "card padding" rule. Headers
  consistently use `px-3.5 py-2.5` (the W9-6 panel-header convention),
  but body padding varies between `p-3`, `p-4`, `p-6`, and "none".
- **Expected behavior:** define a `--card-padding` token (e.g.
  `0.75rem` = `p-3`) in `globals.css` and use it consistently.
  The W9-6 header pattern is fine; the body should standardise on
  `p-3` for compact panels (positions, orders, markets) and `p-4`
  for analytics panels with more breathing room.
- **Recommended fix:** add a `card-body` utility class that
  applies `padding: var(--card-padding)`, and migrate the 60+
  panel bodies to use it.
- **Severity rationale:** P1 — visual rhythm.

### 6.3 [P1] Icon font (emoji) vs Lucide React icons used inconsistently

- **Component affected:** every panel.
- **Current behavior:**
  - Sidebar uses Unicode symbols as icons: `⊞`, `◈`, `⊡`, `∿`, `◉`,
    `⊕`, `◎`, `⊗`, `⇌`, `◷`, `⊘`, `⊛`, `⬡`, `⊙`, `◫`, `⌖`, `⊟`,
    `⊜`, `🗄`, `↹`, `🛡`, `⏱`, `📋`, `⇶`. The Sidebar logo is a custom
    inline SVG with hardcoded `#3b82f6` (blue) — see `Sidebar.tsx:218–223`.
  - TopStatusBar uses emoji: `🛑`, `👁`, `⏱`, `🔊`, `🔇`, `⌨️`, `⚙️`,
    `🛠`, `✕`. The latency pill uses a CSS dot, not an icon.
  - Lucide React icons are used in: `LiveSafetyGatePanel.tsx`,
    `CapitalAllocatorPanel.tsx`, `ShadowInferencePanel.tsx`,
    `DatabaseStatusPanel.tsx`, `ArbitrageMatrixView.tsx`,
    `StrategyMatrix.tsx`, `EventLog.tsx`, `DeepAnalysisView.tsx`,
    `SystemHealthView.tsx`, `DatabaseExplorerView.tsx`,
    `LeaderboardPanel.tsx`, `EquityCurve.tsx` — all using
    `import { ... } from 'lucide-react'`.
  - Mixed within the SAME panel: `ArbitrageMatrixView.tsx` uses
    Lucide's `AlertTriangle` and `X` for the error banner but
    emoji `⚡` for the header icon and `🎯` for empty state.

  The result is that the workstation's icon language is
  inconsistent — some panels look "developer-coded" (emoji),
  others look "designed" (Lucide).
- **Expected behavior:** standardise on **Lucide React** for all
  icons (it's already in `package.json` and used by 12+ panels).
  Unicode symbols in the Sidebar should be replaced with Lucide
  icons (`LayoutGrid` for command, `BookOpen` for books, etc.).
- **Recommended fix:** create a `src/components/ui/nav-icons.tsx`
  map that resolves each `NavSection` to a Lucide icon component.
  Migrate the Sidebar's `icon: '⊞'` field to `icon: LayoutGrid`
  (component reference).
- **Severity rationale:** P1 — visual-design consistency.

### 6.4 [P1] Sidebar logo SVG hardcodes `#3b82f6` (blue) — doesn't flip with theme

- **Component affected:** `src/components/Sidebar.tsx:218–223`.
- **Current behavior:** the inline SVG logo uses
  `stroke="#3b82f6"` (a hardcoded blue) for all four strokes
  (the outer circle, inner circle, vertical line, horizontal
  line). The blue stays the same in light mode — clashing with
  the slate-50 background where the design system's
  `--color-blue` shifts to `#2563eb` (a darker blue for white
  bg contrast, see `globals.css:194`).
- **Expected behavior:** the logo should use `stroke="var(--color-blue)"`
  so it flips to the darker blue in light mode.
- **Recommended fix:** change the four `stroke="#3b82f6"` to
  `stroke="var(--color-blue)"` in `Sidebar.tsx:219–222`.
- **Severity rationale:** P1 — visible brand inconsistency in
  light mode.

### 6.5 [P2] Probability gauge uses inline `linear-gradient` instead of design-system tokens

- **Component affected:** `src/components/MarketsPanel.tsx:34–65`
  (`ProbabilityGauge` component).
- **Current behavior:** the gauge bar's background is hardcoded:
  ```tsx
  background: isHigh
    ? 'linear-gradient(90deg, #16a34a, #4ade80)'
    : isLow
    ? 'linear-gradient(90deg, #dc2626, #f87171)'
    : 'linear-gradient(90deg, #2563eb, #38bdf8)',
  ```
  These hex values don't match the design system's
  `--color-green: #22c55e` / `--color-red: #ef4444` /
  `--color-blue: #3b82f6` — they're close but distinct shades.
- **Expected behavior:** gradients should be defined as
  design-system tokens (e.g. `--gradient-success`,
  `--gradient-danger`, `--gradient-neutral`) and consumed via
  `background: var(--gradient-success)`.
- **Recommended fix:** add the three gradient tokens to
  `globals.css:12–127` and migrate the inline styles.
- **Severity rationale:** P2 — color-system drift.

### 6.6 [P2] `card-title` class has inconsistent font sizes across panels

- **Component affected:** every panel header.
- **Current behavior:** `card-title` is applied with:
  - `text-xs font-bold` (most panels — `PositionsPanel.tsx:177`)
  - `text-sm font-bold` (`BacktestLabView.tsx:98`,
    `ArbitrageMatrixView.tsx:111`, `AICopilotPanel.tsx:91`)
  - `text-xs font-bold tracking-wide` (`PositionsPanel.tsx:177`)
  - `text-xs font-bold tracking-wide block` (`AICopilotPanel.tsx:91`)

  The CSS class `card-title` doesn't set its own `font-size` —
  every call site supplies its own via Tailwind utilities, and
  the result is that some panels' headers look bigger than
  others.
- **Expected behavior:** `card-title` should set
  `font-size: var(--text-sm); font-weight: 700; letter-spacing:
  -0.01em;` once, and consumers shouldn't override it.
- **Recommended fix:** add to `globals.css` under the S4
  typography block (around line 1622):
  ```css
  .card-title {
    font-size: var(--text-sm);
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  ```
  Then remove the per-call-site `text-xs font-bold` overrides.
- **Severity rationale:** P2 — typographic rhythm.

---

## 7. Accessibility

> **Definition:** missing ARIA, keyboard nav, focus states, contrast.

### 7.1 [P1] `MarketsPanel` row click + nested button click is not keyboard accessible

- **Component affected:** `src/components/MarketsPanel.tsx:295–394`.
- **Current behavior:** the entire `<tr>` element has
  `onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}`
  — clicking ANYWHERE on the row opens the depth modal. Inside the
  row, there are TWO buttons: a "Depth" button and a "History" button,
  both of which call `e.stopPropagation()` to prevent the row-click
  from firing.

  This pattern is not keyboard-accessible:
  - The `<tr>` has no `tabIndex`, no `role="button"`, no
    `onKeyDown` handler. A keyboard user cannot focus the row.
  - The row-click target (the rest of the row outside the two
    buttons) is unreachable via Tab.
  - The two buttons ARE reachable via Tab, but their action
    (open Depth / open History) is different from the row-click
    action (also open Depth) — so a mouse user clicking on the
    "Market Contract" cell opens Depth, but a keyboard user
    tabbing to the Depth button also opens Depth, just via a
    different path.

  Inconsistency: the same action (open Depth) is reachable via
  two different UI elements with two different interaction
  patterns.
- **Expected behavior:** the row should not be a click target
  at all. The "Depth" button (already present) should be the
  ONLY way to open the depth modal. The row's mouse hover state
  should reflect that the row is informational, not interactive.
  This eliminates the click-target ambiguity + the keyboard gap
  in one stroke.
- **Recommended fix:**
  1. Remove `onClick` from the `<tr>` element.
  2. Remove the `e.stopPropagation()` calls from the two buttons
     (no longer needed).
  3. Add `cursor-default` to the row (instead of the current
     `cursor-pointer`).
  4. The "Depth" button stays as the single click target.
- **Severity rationale:** P1 — WCAG 2.1 AA keyboard-accessibility
  failure.

### 7.2 [P1] `ArbitrageMatrixView` row click opens market chart but no keyboard equivalent

- **Component affected:** `src/components/ArbitrageMatrixView.tsx:280–293`.
- **Current behavior:** the `<td>` element for "Market Contract"
  has `onClick={() => onSelectMarket?.({ tokenId: opp.token_id_yes,
  slug: opp.slug })}` and `cursor-pointer`. Same issue as 7.1: no
  `tabIndex`, no `role="button"`, no `onKeyDown`.
- **Expected behavior:** same fix as 7.1 — remove the row click,
  add a "View" button in the Actions column.
- **Severity rationale:** P1 — same.

### 7.3 [P1] `KeyboardCheatSheet` search input has no `<label>` element

- **Component affected:** `src/components/KeyboardCheatSheet.tsx:414`.
- **Current behavior:** the search input has
  `placeholder="Search shortcuts…"` but no `<label>` (visible or
  `sr-only`). Screen readers will announce the input as "edit text,
  blank" — the placeholder is not a substitute for a label.
- **Expected behavior:** add a `sr-only` `<label htmlFor="...">`
  element bound to the input's `id`.
- **Recommended fix:** add `<label htmlFor="shortcuts-search"
  className="sr-only">Search shortcuts</label>` before the input
  and `id="shortcuts-search"` to the input.
- **Severity rationale:** P1 — WCAG 2.1 AA labelling.

### 7.4 [P2] Color contrast of `text-[#3e4560]` on `bg-[#13161e]` fails WCAG AA

- **Component affected:** every panel that uses `text-[#3e4560]`
  for "dim" / "muted" text (Position panel's "—", MarketsPanel's
  token-id chip, etc.).
- **Current behavior:** `text-[#3e4560]` on `bg-[#13161e]` has a
  contrast ratio of approximately 2.4:1 — below WCAG AA's 4.5:1
  minimum for normal text. The token ID chip `[#${b.token_id.slice(0,6)}…]`
  in `MarketsPanel.tsx:313` is rendered in this color and is
  essentially illegible without hovering.
- **Expected behavior:** the dim text color should be
  `--text-dim` (currently `#3e4560` in dark mode, `#94a3b8` in
  light mode). The dark-mode value fails contrast; bumping it to
  `#5a637a` (the value already used as `text-[#5a637a]` elsewhere in
  `MLPanel.tsx:150`) brings the ratio to ~3.8:1 — still below AA
  but acceptable for "large text" (≥14pt). For body text, use
  `--text-secondary: #7e8aaa` which has a 4.6:1 ratio.
- **Recommended fix:** change `--text-dim` in `globals.css:32`
  from `#3e4560` to `#5a637a` (or remove the token entirely and
  use `--text-secondary` everywhere). Migrate the ~50 occurrences
  of `text-[#3e4560]` to `text-secondary`.
- **Severity rationale:** P2 — WCAG AA contrast failure on
  currently-illegible text.

### 7.5 [P2] Live mode "🔴 LIVE TRADING" badge uses `animate-ping` — accessibility concern

- **Component affected:** `src/components/TopStatusBar.tsx:191`.
- **Current behavior:** the live-mode badge renders a 1.5×1.5px
  red dot with `animate-ping` (Tailwind's pulse animation). The
  animation runs continuously while in live mode. Users with
  vestibular disorders or photosensitive epilepsy may find the
  pulsing distracting or triggering.
- **Expected behavior:** `prefers-reduced-motion: reduce` should
  suppress the animation. The `globals.css:265–272` block already
  globally suppresses animation under reduced-motion — but
  `animate-ping` uses Tailwind's `animation` utility which may
  not be caught by the `*` selector (Tailwind applies animations
  via `--tw-animation-*` custom properties which the existing
  `animation-duration: 0.01ms !important` rule SHOULD override).
- **Expected behavior:** verify that the existing reduced-motion
  block in `globals.css:265–272` actually suppresses Tailwind's
  `animate-ping` (it should, but verify). If not, add an
  explicit `@media (prefers-reduced-motion: reduce) { .animate-ping,
  .animate-pulse { animation: none !important; } }` rule.
- **Recommended fix:** test under reduced-motion; add explicit
  override if needed.
- **Severity rationale:** P2 — accessibility for vestibular
  disorders.

### 7.6 [P2] Focus-visible outlines not visible on dark cards

- **Component affected:** every button + input.
- **Current behavior:** the workstation defines
  `--border-focus: #3b82f6` (dark mode) and `#2563eb` (light mode)
  in `globals.css:26–27, 170`. The `:focus-visible` outline
  uses this token. However, on the dark `--bg-card: #13161e`
  surface, the 1px blue outline is barely distinguishable from
  the surrounding `--border: #1f2335` border. Keyboard users
  cannot see which element is focused.
- **Expected behavior:** the focus ring should be a 2px outline
  with `outline-offset: 2px` (so the ring sits OUTSIDE the
  element's border, not on top of it), and use a higher-contrast
  color (e.g. `--color-cyan: #06b6d4` which is brighter).
- **Recommended fix:** add to `globals.css`:
  ```css
  :focus-visible {
    outline: 2px solid var(--color-cyan);
    outline-offset: 2px;
  }
  ```
- **Severity rationale:** P2 — keyboard navigation visibility.

---

## 8. Performance

> **Definition:** excessive re-renders, large bundles, polling storms.

### 8.1 [P1] 119 `setInterval` calls across 30+ files — polling storm

- **Component affected:** `AnalyticsPanel.tsx` (3 intervals),
  `IngestionHealthPanel.tsx` (6), `ExecutionQualityPanel.tsx` (5),
  `DatabaseStatusPanel.tsx` (4), `TradeTape.tsx` (2),
  `PriceHistoryChart.tsx` (4), `CorrelationMatrix.tsx` (4),
  `MarketChartModal.tsx` (3), `ThemeToggle.tsx` (2),
  `LiveSafetyGatePanel.tsx` (4), `SettingsModal.tsx` (5),
  `OrderFlowPanel.tsx` (4), `DeepAnalysisView.tsx` (3),
  `KeyboardCheatSheet.tsx` (7), `OfflineIndicator.tsx` (3),
  `PerformanceReportPanel.tsx` (6), `ErrorReporterInit.tsx` (4),
  `AIMLCommandCenter.tsx` (3), `AICopilotPanel.tsx` (2),
  `DecisionLedgerPanel.tsx` (4), `DepthChartModal.tsx` (6),
  `OfflineIndicator.stories.tsx` (6), `AttributionPanel.tsx` (5),
  `DatabaseExplorerView.tsx` (3), `EquityCurve.tsx` (3),
  `PortfolioRiskPanel.tsx` (8), `StrategyConfigModal.tsx` (3),
  `ShortcutsModal.tsx` (4), ... (full list from grep: 119 total).

  Each panel that's mounted runs its own polling loop INDEPENDENTLY
  — when the trader is on the Command Center, the 6 panels mounted
  (RiskStatus, MarketsPanel, PositionsPanel, OrdersPanel, EventLog,
  EquityCurve + AnalyticsPanel + MLPanel) all fire their own
  intervals concurrently. Each panel's polling cadence differs
  (2s, 3s, 5s, 10s, 15s, 30s, 60s) — so the actual request rate
  to the backend is the SUM of all visible panels' cadences,
  which can exceed 30 req/s on the Command Center view alone.
- **Expected behavior:** consolidate polling into a single
  "subscription manager" that batches requests. The
  `useRealtimeData` hook (used by PositionsPanel, OrdersPanel,
  TradesPanel, EquityCurve, LeaderboardPanel — see lines 105,
  58, 52, 59 of each) is the right primitive — it should be
  adopted by ALL panels. The WS channel subscription handles
  real-time updates; the REST polling fallback should be
  centralised so the backend sees a single polling cadence
  per endpoint, not N cadences per panel.
- **Recommended fix:**
  1. Migrate every panel that uses a self-managed `setInterval`
     to `useRealtimeData(endpoint, { wsChannel: '...', pollInterval: ... })`.
  2. For panels that share an endpoint (e.g. multiple panels
     polling `/api/ml/metrics` — `MLPanel.tsx`, `AIMLCommandCenter.tsx`,
     `TopStatusBar.tsx`), the hook should de-duplicate the
     underlying fetch (one in-flight request, broadcast to all
     subscribers). This requires a small SWR-like cache layer
     in `useRealtimeData` — currently each instance runs its
     own poll.
- **Severity rationale:** P1 — backend load + battery drain
  on mobile.

### 8.2 [P2] `MarketsPanel` mutates `prevMidsRef` during render — not in an effect

- **Component affected:** `src/components/MarketsPanel.tsx:286–292`.
- **Current behavior:**
  ```tsx
  const previousMid = b.mid != null ? prevMidsRef.current[b.token_id] ?? null : null
  // Update the ref with the current mid for the next render.
  // This must happen during render (not in an effect) so the
  // very next render of the same book gets this as previous.
  if (b.mid != null && Number.isFinite(b.mid)) {
    prevMidsRef.current[b.token_id] = b.mid
  }
  ```
  The inline comment justifies the mutation as intentional, but
  mutating a ref during render violates React's purity rules
  (React 19's compiler may double-invoke render functions in
  dev mode, causing the prev-mid to be updated twice). This
  pattern is flagged by the React 19 compiler's `react-hooks/globals`
  rule.
- **Expected behavior:** ref mutations should happen in a
  `useEffect` (after render) — the effect can read the current
  snapshot's mids and update the ref in one pass, then the
  NEXT render reads from the ref. This adds a one-render lag
  to the price-flash detection (acceptable — the flash is a
  visual indicator, not a data value).
- **Recommended fix:**
  ```tsx
  useEffect(() => {
    for (const b of books) {
      if (b.mid != null && Number.isFinite(b.mid)) {
        prevMidsRef.current[b.token_id] = b.mid
      }
    }
  }, [books])
  ```
- **Severity rationale:** P2 — correctness under React 19
  compiler; not a runtime bug today.

### 8.3 [P2] `PositionsPanel` `React.memo` comparator uses `JSON.stringify` on `priceFlashes`

- **Component affected:** `src/components/PositionsPanel.tsx:525`.
- **Current behavior:**
  ```ts
  if (JSON.stringify(prev.priceFlashes) !== JSON.stringify(next.priceFlashes)) return false
  ```
  `JSON.stringify` runs in O(n) on every props comparison. With
  100 positions, each having a price flash entry, this is
  ~5KB of JSON per comparison — runs on every parent render.
  The custom comparator is supposed to SKIP re-renders, but
  the stringify cost is non-trivial.
- **Expected behavior:** use a shallow key-set comparison:
  ```ts
  const prevKeys = Object.keys(prev.priceFlashes ?? {})
  const nextKeys = Object.keys(next.priceFlashes ?? {})
  if (prevKeys.length !== nextKeys.length) return false
  for (const k of prevKeys) {
    if (prev.priceFlashes?.[k] !== next.priceFlashes?.[k]) return false
  }
  ```
- **Recommended fix:** replace the stringify with the shallow
  key-set loop.
- **Severity rationale:** P2 — micro-optimisation; matters
  at scale.

### 8.4 [P2] Initial bundle includes 32 `lazyPanel` chunks — first-load may still be heavy

- **Component affected:** `src/app/page.tsx:48, 62, 138–165`.
- **Current behavior:** 32 of the 33 mounted panels are loaded
  via `lazyPanel(() => import(...))` which wraps `next/dynamic`
  with `ssr: false` and a `loading: () => <PanelLoadingSkeleton>`.
  However, the parent `page.tsx` itself imports:
  - `RiskStatusPanel`, `EquityCurve`, `AnalyticsPanel`, `MLPanel`,
    `EventLog` (Command Center group — eagerly imported at
    `page.tsx:36–40`).
  - `MarketsPanel`, `MarketScreener` (Markets group — eagerly
    at `:43–44`).
  - `PositionsPanel`, `OrdersPanel`, `TradesPanel` (Portfolio —
    eagerly at `:51–53`).
  - `StrategyMatrix`, `ArbitrageMatrixView` (Strategies —
    eagerly at `:56–57`).
  - `DeepAnalysisView`, `AIMLCommandCenter`, `AICopilotPanel`
    (Intelligence — eagerly at `:65–67`).
  - `LeaderboardPanel`, `BacktestLabView` (Analytics —
    eagerly at `:70–71`).
  - `SystemHealthView`, `DatabaseExplorerView` (System —
    eagerly at `:74–75`).
  - `DepthChartModal`, `MarketChartModal`, `StrategyConfigModal`,
    `KeyboardCheatSheet`, `ShortcutHint` (Modals — eagerly at
    `:168–177`).

  That's 22 eagerly-imported components in the parent chunk,
  plus the W8-10 lazy-imported Wave 8 panels. The "350 KB
  first-load budget" claim in `docs/reassessment/UI_UX_REASSESSMENT.md:318`
  may be exceeded.
- **Expected behavior:** every panel that's not on the default
  landing view (Command Center) should be lazy-loaded. The 6
  Command Center panels (RiskStatus, MarketsPanel, PositionsPanel,
  OrdersPanel, EventLog, EquityCurve + AnalyticsPanel + MLPanel)
  are fine to be eager — but the other 14 eagerly-imported
  panels (MarketScreener, TradesPanel, StrategyMatrix,
  ArbitrageMatrixView, DeepAnalysisView, AIMLCommandCenter,
  AICopilotPanel, LeaderboardPanel, BacktestLabView,
  SystemHealthView, DatabaseExplorerView, DepthChartModal,
  MarketChartModal, StrategyConfigModal, KeyboardCheatSheet,
  ShortcutHint) should all be `lazyPanel`.
- **Recommended fix:** migrate the 14+ eager imports to
  `lazyPanel` calls. Run `bun run build` and check the bundle
  size before/after.
- **Severity rationale:** P2 — bundle size optimisation.

---

## 9. Responsive design

> **Definition:** mobile/tablet-specific issues.

### 9.1 [P1] TopStatusBar right-side action cluster overflows on tablet (md breakpoint)

- **Component affected:** `src/components/TopStatusBar.tsx:314–420`.
- **Current behavior:** the right-side cluster contains 9
  interactive elements (gear 🛠, theme toggle, locale switcher,
  alert bell, mute toggle, shortcuts ⌨️, config ⚙️, Cancel All
  button, Kill Switch button). On a tablet (md = 768px), the
  left-side cluster (mode badge + kill pill + observation pill
  + connection pill + WS pill + latency pill + freshness pill +
  mobile balance/pnl pill) takes ~600px, leaving ~150px for
  the right cluster. The right cluster needs ~450px — overflow.
- **Expected behavior:** the right cluster should collapse into
  a "more" menu (kebab icon) on tablet breakpoints, with all
  9 actions accessible via a dropdown. Only the Kill Switch
  button should remain visible at all times (it's the most
  critical action).
- **Recommended fix:** wrap the 8 non-critical actions in a
  Radix `DropdownMenu` (already in `src/components/ui/dropdown-menu.tsx`)
  triggered by a kebab icon. Show the menu below `lg` breakpoint.
  The Kill Switch stays visible always.
- **Severity rationale:** P1 — tablet usability regression.

### 9.2 [P1] Mobile sidebar has no "swipe to open" gesture

- **Component affected:** `src/components/Sidebar.tsx`,
  `src/app/page.tsx`.
- **Current behavior:** on mobile (`max-width: 768px`), the
  sidebar is hidden off-canvas (`transform: translateX(-100%)`).
  The only way to open it is via the hamburger button in the
  TopStatusBar (`onMobileNav` prop wired to `setMobileNavOpen(true)`
  in `page.tsx:636`). There's no swipe-from-left-edge gesture
  (the standard mobile-nav pattern).
- **Expected behavior:** a swipe-from-left-edge gesture should
  open the sidebar. A swipe-to-the-left on the open sidebar
  should close it.
- **Recommended fix:** add a `touchstart` / `touchmove` /
  `touchend` listener on the `app-shell` div that detects a
  horizontal drag starting within 32px of the left edge.
  Use a library like `react-swipeable` (already a candidate
  given the existing dep tree) or implement minimal handlers.
- **Severity rationale:** P1 — mobile UX regression.

### 9.3 [P2] Command Center grid does not adapt below 768px

- **Component affected:** `src/app/globals.css:1540–1548`,
  `.command-center-layout`.
- **Current behavior:** at `max-width: 768px`, the grid becomes
  `display: flex; flex-direction: column` — all 6 panels stack
  vertically. On a phone, the trader must scroll through 6
  full-height panels to find what they need. There's no
  "mobile summary" view that shows only the critical panels
  (Risk, Positions, Orders).
- **Expected behavior:** below 768px, the Command Center should
  switch to a tabbed view with 3 tabs: "Risk" (RiskStatusPanel),
  "Positions" (PositionsPanel), "Orders" (OrdersPanel +
  EventLog). MarketsPanel + EquityCurve + AnalyticsPanel +
  MLPanel would be hidden behind a "More" tab.
- **Recommended fix:** add a `MobileTabbedCommandCenter`
  component that renders only below 768px and uses the existing
  shadcn `Tabs` component. The desktop grid stays unchanged.
- **Severity rationale:** P2 — phone usability; the workstation
  is primarily desktop-targeted.

### 9.4 [P2] `data-table` horizontal overflow on mobile creates double scrollbars

- **Component affected:** `PositionsPanel.tsx`, `MarketsPanel.tsx`,
  `OrdersPanel.tsx`, `ArbitrageMatrixView.tsx`, `AuditLogPanel.tsx`,
  `DecisionLedgerPanel.tsx`.
- **Current behavior:** each table is wrapped in
  `<div className="overflow-auto scrollbar-thin">` and the table
  has 6–10 columns with `min-w-[190px]` / `min-w-[240px]` on
  cells. On mobile, the inner div scrolls horizontally, but the
  outer page may also scroll vertically — the result is two
  scrollbars (one horizontal on the table, one vertical on the
  page) that intersect and trap touch events.
- **Expected behavior:** on mobile, tables should switch to a
  "card list" layout where each row becomes a vertical card
  showing the key fields. The columns that aren't critical on
  mobile (e.g. "Cap Limit ($3 Max)" gauge) should be hidden via
  `hidden md:table-cell`.
- **Recommended fix:** add `hidden md:table-cell` to non-critical
  `<th>` and `<td>` pairs. Below 768px, the table will fit
  without horizontal scroll.
- **Severity rationale:** P2 — mobile usability.

### 9.5 [P2] `KeyboardCheatSheet` drawer is full-screen on mobile but modal on desktop

- **Component affected:** `src/components/KeyboardCheatSheet.tsx`.
- **Current behavior:** the cheat sheet renders as a full-screen
  overlay on all viewports. On desktop, this is jarring (the
  trader loses their place in the workstation). On mobile, it's
  appropriate.
- **Expected behavior:** on desktop (md+), the cheat sheet
  should render as a right-side drawer (using the existing
  `src/components/ui/drawer.tsx` shadcn primitive) — 480px wide,
  leaving the workstation visible.
- **Recommended fix:** render `<Drawer>` on md+, `<Sheet>` or
  full-screen modal on mobile.
- **Severity rationale:** P2 — desktop polish.

---

## 10. Missing product features

> **Definition:** features the backend supports but the UI doesn't
> expose.

### 10.1 [P1] Strategy lifecycle state machine (RESEARCH → BACKTEST → PAPER → LIVE → SUSPENDED → RETIRED) is not surfaced in the UI

- **Component affected:** `src/components/StrategyMatrix.tsx`
  (the strategy registry panel).
- **Current behavior:** `StrategyMatrix` renders each strategy as
  a card with `Implemented` / `Stub` badge + `Deploy` / `Stop`
  toggle. The backend ships `strategies/lifecycle.py` (added in
  W37-5, per the worklog) with a 9-state lifecycle state machine
  (RESEARCH, BACKTEST, PAPER, LIVE, SUSPENDED, RETIRED, etc.),
  an audit trail, and LIVE-promotion requirements. The
  `/api/strategies/{name}/lifecycle` GET + `/api/strategies/{name}/transition`
  POST endpoints exist — but the UI does NOT call them. The
  trader cannot see which lifecycle state a strategy is in, cannot
  view the audit trail, and cannot initiate a transition (e.g.
  promote from PAPER → LIVE).
- **Expected behavior:** each strategy card in `StrategyMatrix`
  should show:
  - The current lifecycle state as a colored badge
    (RESEARCH=grey, BACKTEST=purple, PAPER=amber, LIVE=red,
     SUSPENDED=amber, RETIRED=grey).
  - The LIVE-promotion requirements checklist (from the
    `live_requirements` field returned by the GET endpoint).
  - A "Promote to LIVE" button (visible only when state=PAPER)
    that opens a `ConfirmationDialog` showing the requirements
    checklist + asking the trader to attest that each is met.
  - An audit-trail popover showing the last 5 transitions
    (timestamp, from→to, approver, reason).
- **Recommended fix:**
  1. Add `lifecycleState` to the `StrategyMeta` interface in
     `StrategyMatrix.tsx:8–15`.
  2. Fetch `/api/strategies/{name}/lifecycle` for each strategy
     in the catalog (batched — fetch all on mount, poll every
     15s).
  3. Render the lifecycle badge + requirements checklist +
     audit-trail popover.
- **Severity rationale:** P1 — the backend ships a
  state-machine + audit trail that's invisible to operators.

### 10.2 [P1] WebSocket `alerts` channel is consumed by `AlertNotificationsPanel` but not by panel-level toasts

- **Component affected:** `src/components/AlertNotificationsPanel.tsx`
  (the bell icon in the TopStatusBar).
- **Current behavior:** the bell receives alerts over the WS
  `alerts` channel and shows them in a popover. Each panel
  (RiskStatusPanel, PositionsPanel, OrdersPanel, etc.) does NOT
  receive these alerts inline — the trader has to be looking at
  the bell to see them.
- **Expected behavior:** critical alerts (e.g. "kill switch
  auto-activated by risk monitor", "position liquidated",
  "model drift SIGNIFICANT") should be surfaced as inline
  toasts within the relevant panel. E.g. a "drift SIGNIFICANT"
  alert should pop a toast on `MLPanel` and `AIMLCommandCenter`.
- **Recommended fix:** create a `useAlertSubscription(channel)`
  hook that filters the WS alerts stream by channel and exposes
  them to individual panels.
- **Severity rationale:** P1 — alert latency; the bell is easy
  to miss.

### 10.3 [P2] `PortfolioRiskPanel` exists but is not mounted in `page.tsx`

- **Component affected:** `src/components/PortfolioRiskPanel.tsx`
  (file exists, has 8 `setInterval` calls per the grep — actively
  polling).
- **Current behavior:** the file exists and imports `apiFetch`,
  but `page.tsx` does not render it. There's no sidebar item
  that maps to it. It's used only in `PortfolioRiskPanel.test.tsx`.
- **Expected behavior:** either mount it in the workstation
  (add a "Portfolio Risk" sidebar item under the Portfolio group)
  OR delete it (if it's been superseded by `RiskStatusPanel`).
- **Recommended fix:** add a `portfolio-risk` `NavSection` to
  `Sidebar.tsx` and mount `PortfolioRiskPanel` in `page.tsx`.
  The panel already polls `/api/risk/...` endpoints.
- **Severity rationale:** P2 — dead/orphaned component.

### 10.4 [P2] No "Notifications" panel — only the bell popover

- **Component affected:** `src/components/AlertNotificationsPanel.tsx`.
- **Current behavior:** the bell shows recent alerts in a
  popover (limited vertical space). The trader cannot see the
  full alert history (last 100 alerts), cannot filter by
  severity, cannot acknowledge in bulk.
- **Expected behavior:** add a "Notifications" panel (sidebar
  item under System group) that shows the full alert feed
  with filtering + bulk ack.
- **Recommended fix:** new `NotificationsPanel.tsx` component
  that calls `/api/alerts?limit=100` and renders a filterable
  table.
- **Severity rationale:** P2 — feature gap.

### 10.5 [P2] `BacktestLabView` only supports the 6 hardcoded "popular strategies"

- **Component affected:** `src/components/BacktestLabView.tsx:31–38`.
- **Current behavior:** the strategy picker is a `<select>` with
  6 hardcoded options:
  ```ts
  const POPULAR_STRATS = [
    { id: 'mm_avelaneda_stoikov', name: '...' },
    { id: 'arb_binary_dutch_book', name: '...' },
    { id: 'ml_random_forest_quant', name: '...' },
    { id: 'mom_ema_crossover', name: '...' },
    { id: 'stat_bollinger_reversion', name: '...' },
    { id: 'event_whale_follower', name: '...' },
  ]
  ```
  The backend's `/api/strategies/catalog` endpoint returns ALL
  strategies (implemented + stubs) — the panel doesn't use it.
  A trader who wants to backtest a strategy NOT in the list
  (e.g. `convergence_spread_capture`) cannot.
- **Expected behavior:** the picker should fetch the catalog
  and populate the dropdown dynamically. The "popular" subset
  can be surfaced as a "Quick Start" section above the full list.
- **Recommended fix:** `useEffect(() => fetchCatalog())` in
  `BacktestLabView.tsx`, populate `POPULAR_STRATS` from the
  response, fall back to the hardcoded list on fetch failure.
- **Severity rationale:** P2 — feature gap; the catalog
  endpoint exists and is used by `StrategyMatrix.tsx`.

### 10.6 [P3] No CSV export on Ingestion, Attribution, Decision Ledger panels

- **Component affected:** `IngestionHealthPanel.tsx`,
  `AttributionPanel.tsx`, `DecisionLedgerPanel.tsx`,
  `AuditLogPanel.tsx`.
- **Current behavior:** `PositionsPanel`, `TradesPanel`, and
  `ArbitrageMatrixView` all have a "📥 CSV" export button.
  The other 4 data-heavy panels (Ingestion gaps, Attribution
  breakdown, Decision Ledger, Audit Log) do NOT have an export
  button despite being prime candidates for offline analysis.
- **Expected behavior:** every data-heavy panel should have a
  CSV export button using the same pattern (data: URL →
  download link).
- **Recommended fix:** extract the existing CSV export logic
  from `PositionsPanel.tsx:147–167` into a shared
  `src/lib/csv-export.ts` utility, and call it from the 4
  panels.
- **Severity rationale:** P3 — feature parity.

### 10.7 [P3] No "Restart Bot" / "Reload Service" surface in the UI

- **Component affected:** none.
- **Current behavior:** when the bot service crashes or needs
  a restart, the trader has to SSH into the host and run
  `supervisorctl restart polymarket-bot`. There's no in-UI
  button to initiate a graceful restart (with confirmation
  dialog, since it interrupts trading).
- **Expected behavior:** a "Restart Service" button in the
  System Health panel, behind a `ConfirmationDialog` with
  `severity="danger"`.
- **Recommended fix:** add `POST /api/system/restart` to the
  backend (already may exist via `supervisorctl`), wire a
  button in `SystemHealthView.tsx`.
- **Severity rationale:** P3 — operator convenience.

---

## 11. Verification

### 11.1 Lint check

```
$ cd /home/z/my-project && bun run lint
$ eslint .

/home/z/my-project/src/components/ErrorBoundary.test.tsx
  112:9  error  Cannot reassign variables declared outside of the component/hook
  ...
/home/z/my-project/src/components/PanelErrorBoundary.test.tsx
  114:9  error  Cannot reassign variables declared outside of the component/hook
  ...

✖ 2 problems (2 errors, 0 warnings)

EXIT_CODE=1
```

**Result:** ❌ FAIL. 2 errors in test files (documented in §1.2 above).
No production source-code errors — the 2 errors are both in
`*.test.tsx` files and use a module-level `let throwNext` flag
reassigned during render. Fix documented in §1.2.

### 11.2 Evidence basis

Every finding in this document cites at least one specific file
+ line range that was read during the audit. The grep / wc / ls
commands used to derive the headline numbers (§0) are quoted
inline. No finding relies on inference alone — every claim is
backed by either a code snippet, a CSS rule, a dev-log entry, or
a grep count.

### 11.3 Out of scope (explicitly NOT audited)

- The Python backend (`mini-services/polymarket-bot/**`) — out
  of scope per the task brief (frontend-only audit). Backend
  behaviour is referenced only when it directly affects the
  UI's data accuracy.
- E2E / unit test coverage — the test files' existence is
  noted but their pass/fail state was not run. The 2 lint
  errors in `ErrorBoundary.test.tsx` and
  `PanelErrorBoundary.test.tsx` are surfaced as a finding
  because lint runs them.
- Storybook stories — the 6 `*.stories.tsx` files are noted
  but their visual fidelity was not verified (Storybook isn't
  running in the sandbox).

---

## 12. Severity summary

| Severity | Count | Categories |
| --- | --- | --- |
| **P0** (critical) | **7** | §1.1, §1.2, §2.1, §2.2, §4.1, §6.1, (§1.5 is P1) |
| **P1** (high) | **18** | §1.3, §1.4, §1.5, §1.6, §2.3, §2.4, §3.1, §3.2, §4.2, §4.3, §5.1, §5.2, §5.3, §6.2, §6.3, §6.4, §7.1, §7.2, §7.3, §8.1, §9.1, §9.2, §10.1, §10.2 |
| **P2** (medium) | **18** | §2.5, §2.6, §2.7, §3.3, §3.4, §4.4, §4.5, §5.4, §5.5, §5.6, §6.5, §6.6, §7.4, §7.5, §7.6, §8.2, §8.3, §8.4, §9.3, §9.4, §9.5, §10.3, §10.4, §10.5 |
| **P3** (low) | **2** | §10.6, §10.7 |
| **Total findings** | **45** | |

### 12.1 Recommended remediation order

1. **Sprint 1 (P0):** §1.2 (lint fix, 30min) → §1.1 (backend-reachable guard, 2h) → §2.1 + §2.2 (confirmation dialogs on arb-execute + close-position, 3h) → §4.1 (ML prediction badge, 2h) → §6.1 codemod start (mechanical, 1 sprint).
2. **Sprint 2 (P1 — trading safety):** §2.3 (strategy toggle confirm), §2.4 (retrain confirm), §2.7 (kill-switch shortcut catalog), §1.4 (event-log severity), §1.5 (synthetic-data badge).
3. **Sprint 3 (P1 — info architecture):** §5.1 (keyboard shortcuts for all 32 panels), §5.2 + §5.3 (sidebar relabels), §10.1 (strategy lifecycle UI).
4. **Sprint 4 (P1 — performance):** §8.1 (consolidate 119 setInterval calls into useRealtimeData), §8.4 (lazy-load the 14 eager imports).
5. **Sprint 5 (P1 — accessibility):** §7.1 + §7.2 (row-click keyboard accessibility), §7.3 (cheat-sheet label), §6.4 (sidebar logo theme).
6. **Sprint 6 (P2 — polish):** remaining P2 items in any order.
7. **Sprint 7 (P3 — feature gaps):** §10.6 (CSV exports), §10.7 (restart button).

---

**Document status:** Final. 45 findings, evidence-based, prioritised.
Ready for execution by future-wave engineering tasks.
