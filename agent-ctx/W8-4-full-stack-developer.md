# Task W8-4 — ClosedPositionsPanel

## Read references
- `worklog.md` tail (Wave 7 complete; backend has 77 routes, 340 tests, balance $111.72)
- `mini-services/polymarket-bot/core/closed_positions.py` (store + register_routes)
- `src/components/PositionsPanel.tsx` (design pattern ref: dark card, `bg-[#13161e]`, `border-[#1f2335]`, `data-table`, KPI badges strip, `scrollbar-thin`, empty-state)
- `src/lib/api.ts` (`apiFetch` wraps fetch + injects Authorization header + `XTransformPort=8080` for non-`/api/bot` routes)
- `src/app/globals.css` (design tokens: `--bg-card`, `--border`, `--text-secondary`, semantic colors)
- `src/lib/design-tokens.ts` (`fmtUsd`, `fmtPnl`, `fmtPrice`, `fmtPct`, `fmtAge`, `fmtTime`)

## Backend endpoints (verified registered in `api/server.py` L2142)
- `GET /api/positions/closed?limit=50&strategy=...` → `{ count, positions: [] }`
- `GET /api/positions/closed/stats` → aggregate dict (`count`, `total_pnl`, `win_rate`, `wins`, `losses`, `breakeven`, `avg_holding_seconds`, `gross_profit`, `gross_loss`, `profit_factor`, `best_trade`, `worst_trade`, `avg_entry_price`, `avg_exit_price`, `total_volume_shares`, `strategies_count`, `median_pnl`, `avg_pnl`)

## ClosedPosition row shape (from store)
```ts
{
  id, timestamp, position_id, token_id, strategy,
  entry_price, exit_price, shares, pnl, holding_seconds,
  model_version, decision_id, direction,
  confidence, predicted_edge, p_yes, market_mid, liquidity,
  data: Record<string, any> | null  // decoded metadata_json (slug, side, exit_reason, opened_at, etc.)
}
```

## Exit reason inference
Backend schema has no dedicated `exit_reason` column. The `data` JSON payload (extras) is where it lives, written by the strategy layer at close time. Inference ladder used by the panel:
1. `data.exit_reason` (canonical)
2. `data.reason`
3. `data.close_reason`
4. Strategy-name fallback: `*_sl_*` → `SL`, `*_tp_*` → `TP`, `*_settle*` → `SETTLEMENT`
5. Otherwise `MANUAL`

## Side inference
- `direction === 'BUY'` (opening trade bought YES) → `LONG YES`
- `direction === 'SELL'` → `SHORT NO`
- `data.side` overrides if present

## P&L % calc
`pnl / (entry_price * shares) * 100` (guard divide-by-zero)

## Hold time formatting
`holding_seconds` → `Xh Ym` / `Xd Yh` / `Xs`

## Polling
- 30s interval
- Paused when `document.hidden`
- Manual refresh button

## Component shape
- Default export `ClosedPositionsPanel` (no props)
- Self-contained fetch + polling (uses `apiFetch`)
- Visual style mirrors PositionsPanel: dark card, KPI strip, filters row, sortable table, row expansion, donut + cumulative P&L chart above/below table

## Implementation status
- Created `/home/z/my-project/src/components/ClosedPositionsPanel.tsx` (997 lines, `'use client'`, default export, no props).
- ESLint clean (`bunx eslint` exit 0).
- `tsc --noEmit` shows no errors specific to this file (only pre-existing errors elsewhere).
- Worklog entry appended.

## Key design decisions
1. **Pure SVG donut + line chart** — avoids pulling a charting lib (Recharts/Chart.js) into the bundle; keeps the panel consistent with PositionsPanel.tsx's hand-rolled table styling.
2. **Exit-reason inference ladder** — backend has no dedicated `exit_reason` column, so we check `data.exit_reason` → `data.reason` → `data.close_reason` → strategy-name pattern → fallback to MANUAL/UNKNOWN.
3. **Side inference** — `data.side` (YES/NO/LONG/SHORT) takes precedence; falls back to `direction` column (BUY→LONG YES, SELL→SHORT NO).
4. **KPI client-side fallback** — if `/stats` 5xx or fails, KPIs are computed from the positions array so the panel never goes blank.
5. **Row expansion uses `<tr colSpan=11>`** — preserves table semantics, no modal needed.
6. **Auto-refresh pause on `document.hidden`** — uses `visibilitychange` listener + dynamic `setInterval` start/stop.

## Backend endpoints (confirmed wired at api/server.py:2142)
- `GET /api/positions/closed?limit=N&strategy=X` → `{ count, positions: ClosedPosition[] }`
- `GET /api/positions/closed/stats` → aggregate dict

## Files NOT modified (per task contract)
- Only `src/components/ClosedPositionsPanel.tsx` was created.
- `worklog.md` was appended (per task instructions).
- This context note was created under `/agent-ctx/`.
