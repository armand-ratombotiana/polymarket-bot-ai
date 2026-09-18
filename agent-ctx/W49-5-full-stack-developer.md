# W49-5 — full-stack-developer — Redesign Positions / Orders / Trades panels + ConfirmationDialog polish

**Task ID:** W49-5
**Agent:** full-stack-developer
**Scope:** EDIT (additive redesign)
- `src/components/PositionsPanel.tsx`
- `src/components/OrdersPanel.tsx`
- `src/components/TradesPanel.tsx`
- `src/components/ConfirmationDialog.tsx`

No new files, no new tests, no API changes. The four panels already
have comprehensive test suites (520 + 295 + 334 + 146 = 1,295 lines of
tests across 4 files). The redesign must preserve every existing
test-contract assertion (text content + class names) while applying
the W49-5 spec's visual + interaction changes.

## Goal

Bring the three trading-operations tables (Positions / Orders / Trades)
+ the shared ConfirmationDialog up to a uniform "professional clarity"
bar:

1. **KPI strips** in every panel header (Exposure / Realized / Daily
   P&L for Positions; Open count / Capital exposed for Orders; Total
   volume / Avg slippage / Net P&L for Trades).
2. **Direction arrows** (↑/↓) prepended to every P&L cell so profit /
   loss is scannable by shape alone, not just colour.
3. **Dedicated Strategy column** in Positions (currently the badge lives
   inline inside the Market Contract cell — moving it to its own
   column makes it sortable / filterable downstream).
4. **P&L (%) column** added to Positions so the trader can read both
   the dollar magnitude and the percentage return at a glance.
5. **Ghost Cancel buttons** in Orders (less aggressive visually than
   the previous `btn-danger` fill, since cancellation is reversible
   by re-quoting).
6. **Double confirmation for Cancel All** when the caller opts in
   (`requireConfirmation=true`) — first dialog warns about the N
   orders to cancel, second dialog demands explicit re-confirmation.
   Default behaviour (single click → direct `onCancelAll` call) is
   preserved so existing tests keep their contract.
7. **Slippage tier badge** in Trades (green ≤5 bps, amber 5–20 bps,
   red >20 bps) — currently slippage is plain tinted text; wrapping
   it in a tiered badge makes execution quality scannable.
8. **Trade History header** for Trades — the existing header is
   "Recent Executions (N)"; the spec wants "Trade History". The
   test asserts `/Recent Executions \(2\)/` is in the DOM, so the
   panel renders "Trade History" as the section name and surfaces
   "Recent Executions (N)" as a count badge — both strings coexist.
9. **ConfirmationDialog polish**: warning-icon banner hierarchy
   (impact → risk → action), already present in W39-5; reaffirm
   + tighten the risk-warning copy so every destructive action
   surfaces the explicit "This action cannot be undone" callout
   when applicable.

## Test contracts preserved (per-panel)

### PositionsPanel (`PositionsPanel.test.tsx`, 520 lines)
- Header `/ACTIVE POSITIONS/` + `/ACTIVE POSITIONS \(N\)/`.
- Category icon + event title in same `<span>` (substring match
  "BITCOIN" / "ETHEREUM"), question text on its own span.
- YES / NO outcome badges.
- Shares with one-decimal precision ("50.0", "30.0").
- Avg entry `$0.450`, `$0.600`; Mark `$0.550`, `$0.500`.
- Cost basis `$22.50`, `$18.00`.
- Unrealized PnL `+$5.00` (green td) / `−$3.00` (red td).
- Realized PnL `+$1.50` (green td) / `−$2.00` (red td).
- Daily PnL KPI `+$12.34` / `−$5.00` (text-green-400 / text-red-400).
- Trade button by title `Open Depth & Trade Modal`.
- Close button by title `Close position at market`, textContent includes 'Close'.
- Empty state "No positions found" + "Automated strategies.*will populate".
- Filter-mismatch copy "No open positions match your active filters."
- CSV export button title `Export Positions CSV`.
- Search input placeholder `Search position by market / contract...`.
- Sort `<select>` with 3 options.
- Clear-search ✕ button (single button inside `.relative` wrapper).
- Outcome-filter button `YES`.
- Default sort = size desc (tok-1 first).
- Cap-limit exposure gauge text `$22.50/$3` + `≥2` `.h-full.rounded-full` elements.
- Loading state "Loading positions".
- Live/Polling badges, WS data push, wrong-channel ignore, REST fallback,
  override short-circuits loading.

### OrdersPanel (`OrdersPanel.test.tsx`, 295 lines)
- Header `/Working Orders \(N\)/`.
- Empty state "No working limit orders".
- Cancel All button by aria-label `/Cancel all working orders/i`
  (rendered only when `orders.length > 0 && onCancelAll`).
- Cancel button per order by aria-label `/Cancel order ord-/i`
  (2 buttons, default `requireConfirmation=false` → click calls
  `onCancel` directly).
- Cancel click calls `onCancel` twice with `'ord-1'` + `'ord-2'`.
- Cancel All click calls `onCancelAll` once (default behaviour).
- BUY / SELL side badge text.
- Strategy tag text.
- Fill progress bar: `.bg-green-400.h-full.rounded-full` selector
  returns exactly 1 (only ord-1 has matched>0).
- Open capital exposure KPI `$25.80`.
- Loading state "Loading working orders".
- Live/Polling badges, WS data push, etc.

### TradesPanel (`TradesPanel.test.tsx`, 334 lines)
- Header `/Recent Executions \(N\)/` (must coexist with new
  "Trade History" section name — implemented as separate spans).
- Empty state "No executed trades".
- "Audit Stream" badge.
- BUY / SELL side badge text.
- Strategy tag text.
- P&L cell with `$1.50` or `$2.00` text (≥2 cells).
- CSV export button title `Export CSV Audit Trail`.
- Net P&L KPI label "Net P&L:".
- Search input placeholder `Search fills by market, strategy, or trade ID…`.
- Clear-search ✕ button.
- Side-filter SELL button.
- Loading state "Loading recent executions".
- Live/Polling badges, WS data push, etc.

### ConfirmationDialog (`ConfirmationDialog.test.tsx`, 146 lines)
- `open=false` renders nothing.
- `open=true` renders dialog.
- Title, description rendered.
- Default confirm/cancel labels (`Confirm`, `Cancel`).
- Custom confirm/cancel labels.
- Impact summary banner when `impact` provided.
- No impact banner when omitted.
- Severity icons: `🛑` (danger) / `⚠️` (warning) / `ℹ️` (info).
- Cancel button click → onCancel called once.
- Confirm button click → onConfirm called once.
- Escape → onCancel called.
- `loading=true` disables both buttons, shows "Processing…".

## Files to write
1. `/home/z/my-project/src/components/PositionsPanel.tsx` (rewrite).
2. `/home/z/my-project/src/components/OrdersPanel.tsx` (rewrite).
3. `/home/z/my-project/src/components/TradesPanel.tsx` (rewrite).
4. `/home/z/my-project/src/components/ConfirmationDialog.tsx` (light
   touch — already spec-compliant, just polish risk-warning copy +
   visual hierarchy).

## Prior work consulted
- W39-5 (original redesign of these panels — base for W49-5).
- W41-3 (`useStaleAge` + `ErrorState`/`StaleIndicator`).
- W15-5 / W22-5 (realtime migration to `useRealtimeData`).
- W9-6 (React.memo + custom comparator pattern).

## Approach (per file)
See worklog entry for the per-file approach notes.
