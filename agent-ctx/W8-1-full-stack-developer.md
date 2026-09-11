---
Task ID: W8-1
Agent: full-stack-developer
Task: Create DecisionLedgerPanel exposing the decision ledger backend

Work Log:
- Read `worklog.md` tail (~250 lines) for recent context: Wave 7 complete (340 tests, 77 routes, $111.72 balance, settlement deadlock fixed). Read prior agent's `agent-ctx/W8-4-full-stack-developer.md` to match agent-ctx style.
- Read `mini-services/polymarket-bot/core/decision_ledger.py` end-to-end. The module's `register_routes(app)` exposes only TWO HTTP endpoints:
    GET /api/decision/{token_id}?limit=50    — full stage-chain for a token
    GET /api/decisions/rejected?limit=50     — recent rejection rows (most recent first)
  Stages: PREDICTION, SIGNAL, RISK_APPROVED, RISK_REJECTED, ORDER, FILL.
  Rejection reasons: low_confidence, wide_spread, neutral_zone, insufficient_kelly_edge.
  Rejection rows carry: timestamp, decision_id, token_id, strategy, predicted_edge, confidence, reason, market_mid.
  Chain event rows carry: timestamp, decision_id, stage, token_id, strategy, pnl, data_json, data (decoded).
- Read `src/components/PositionsPanel.tsx` as the design pattern reference: `card` class with `bg-[#13161e] border border-[#1f2335] shadow-xl`, `card-header` + `card-title`, KPI chips strip, dark filter bar, `data-table`/`scrollbar-thin`, `empty-state`. Badges: `badge badge-green/red/amber/cyan/blue/purple`. KPI chip pattern: `bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md`.
- Read `src/lib/api.ts`: `apiFetch` auto-appends `XTransformPort=8080` to non-`/api/bot` routes and injects the auth bearer token. `getApiUrl()` returns `''` (relative paths). Pattern: `apiFetch(\`${getApiUrl()}/api/...\`)`.
- Read `src/hooks/useBot.ts` for hook pattern (visibility-aware polling not used there; REST polling every 2s).
- Read `src/app/globals.css` design tokens: `--bg-card`, `--border`, `--text-primary`, `--text-secondary`, `--text-dim`, semantic color tokens (green/red/amber/blue/cyan/purple). Verified `.empty-state`, `.empty-state-icon/title/desc`, `.table-footer`, `.spinner`, `.skeleton-line-lg`, `.scrollbar-thin`, `.badge-*`, `.btn-ghost`, `.card`, `.card-header`, `.card-title`, `.input` classes all exist.
- Read `src/lib/design-tokens.ts` (`fmtUsd`, `fmtPnl`, `fmtPrice`, `fmtPct`, `fmtAge`, `fmtTime`) and `src/components/TradesPanel.tsx` + `src/components/RiskStatusPanel.tsx` for additional patterns (self-fetch, polling, error/loading states, Kpi sub-component).
- Confirmed `lucide-react@0.525.0` is installed in `package.json`. Imported `Activity, AlertTriangle, ChevronRight, Clock, Filter, Loader2, RefreshCw, Search` (all verified against the existing usage in `src/components/ui/*` — e.g. `ChevronRight`, `Search`, `X` already imported elsewhere).
- Designed the component around the two exposed endpoints:
    * Primary list: `GET /api/decisions/rejected?limit=50` → drives the list view (most-recent-first rejections, each with predicted_edge / confidence / reason / market_mid).
    * Chain expansion (detail drawer): on row expand, `GET /api/decision/{token_id}?limit=50` → fetches ALL events for the token; client-side filters to the expanded rejection's `decision_id` to render the primary chain (PREDICTION → SIGNAL → RISK_REJECTED, or PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL where present). ALSO surfaces "Other Recent Decisions for this Token" — sibling decision_ids for the same token with stage counts + outcome badges (FILLED/REJECTED/PENDING) so the operator can see the full token-level decision history at a glance.
- Stage color-coding per spec: PREDICTION=blue, SIGNAL=cyan, RISK=amber (RISK_REJECTED slightly stronger amber to distinguish), ORDER=violet, FILL=green. Implemented via a STAGE_STYLE map with dot/text/bg/border classes.
- Filters: action filter (ALL/TRADE_LONG_YES/TRADE_SHORT_NO/REJECT_RISK/MONITOR) — REJECT_RISK = {wide_spread, insufficient_kelly_edge}, MONITOR = {low_confidence, neutral_zone}; TRADE_LONG_YES/TRADE_SHORT_NO match nothing on the rejection-list surface (they require a SIGNAL-stage `data.action` payload that's only visible after expansion) — documented in the empty-state copy. Outcome filter (ALL/REJECTED/FILLED/PENDING/EXPIRED) — only REJECTED rows are exposed by the backend list endpoint; non-REJECTED filters surface an empty-state message explaining the surface limitation. Token/strategy/decision_id/reason text search.
- Stats header: total decisions (rejection count), avg predicted edge (color-coded), avg confidence, top reason label, fill rate (computed from expanded chains — ratio of expanded tokens with ≥1 FILL event across any decision_id in their chain). Last-updated timestamp with `Clock` icon.
- Loading skeleton: 7 staggered `skeleton-line-lg` bars with varying widths (65–95%).
- Error state: full-card AlertTriangle + retry button. Polling auto-pauses on tab hide via `document.visibilityState` listener, restarts on visibility regain.
- Empty state: friendly 🧠 empty-state with copy that adapts to (a) no rows at all vs (b) non-REJECTED outcome filter active vs (c) other filters yielding empty.
- TypeScript: defined `DecisionEvent`, `RejectionRow`, `DecisionsResponse`, `ChainResponse`, `OutcomeFilter`, `ActionFilter` interfaces/types. StageName uses `string & {}` for forward-compat. `asNumber` helper safely coerces `unknown` payload values to `number | null`.
- Lint clean (`bun run lint` — zero errors/warnings). TypeScript check via `tsc --noEmit` — no errors in the new file (all reported errors are pre-existing in unrelated files: `examples/websocket/*`, `skills/*`, `src/app/api/bot/route.ts`).
- Dev server log confirms healthy compile: `Ready in 756ms`, `GET / 200 in 316ms` — no new compile errors introduced.

Stage Summary:
- Created `/home/z/my-project/src/components/DecisionLedgerPanel.tsx` (default export, `'use client'`).
- Backend endpoints used:
    * `GET /api/decisions/rejected?limit=50` (primary list, polled every 10s)
    * `GET /api/decision/{token_id}?limit=50` (per-row chain expansion, cached)
- Key features:
    * Expandable decision cards with full PREDICTION → SIGNAL → RISK → ORDER → FILL chain visualization (color-coded per spec).
    * Per-stage data extraction from the decoded `data_json` payload (P(YES)/edge/confidence/model_version for PREDICTION; action/reason for SIGNAL; size for RISK_APPROVED; reason/mid for RISK_REJECTED; side/price/size/status/order_id for ORDER; fill_price/slippage/pnl for FILL).
    * "Other Recent Decisions for this Token" sibling-decisions list (with outcome badges: FILLED/REJECTED/PENDING) gives token-level audit context.
    * Filters: action type (ALL/TRADE_LONG_YES/TRADE_SHORT_NO/REJECT_RISK/MONITOR), outcome (ALL/REJECTED/FILLED/PENDING/EXPIRED), token+strategy+decision_id+reason text search.
    * Stats header: total decisions, avg edge (signed, color-coded), avg confidence, top rejection reason, fill rate (token-level, derived from expanded chains), last-updated stamp.
    * Loading skeleton state, graceful error state with retry, friendly adaptive empty state.
    * Visibility-aware polling — pauses when tab hidden, restarts on regain.
    * Fully responsive — metrics hidden on mobile (sm: breakpoint), strategy pill hidden on small screens (md:), filter bar wraps.
    * Uses `apiFetch` (auto-injects auth + `XTransformPort=8080` gateway routing). All requests are relative paths.
    * Visual style mirrors `PositionsPanel.tsx`: `card` + `card-header` + `card-title`, KPI chip strip, `scrollbar-thin` overflow, `table-footer` summary, `empty-state` block, `badge-*` / `btn-ghost btn-sm` utility classes.
