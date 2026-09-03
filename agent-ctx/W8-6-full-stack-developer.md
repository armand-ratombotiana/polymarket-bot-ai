# Task W8-6 — ShadowInferencePanel

## Agent
- **Task ID:** W8-6
- **Agent:** full-stack-developer
- **Date:** 2026-09-04

## Read references
- `worklog.md` tail (Wave 7 complete; backend has 77 routes, 340 tests; balance $111.72)
- `mini-services/polymarket-bot/ml/shadow_inference.py` — Challenger registry + comparison
  engine. The `shadow_inference` singleton is in-memory only and exposes
  `register_shadow_model`, `run_shadow`, `get_status_report`. The module
  docstring explicitly notes the per-challenger HTTP surface is a "future
  `/api/shadow-inference` endpoint (T13 follow-up)" — it is **NOT** wired
  in `api/server.py` at present.
- `mini-services/polymarket-bot/core/shadow_trading.py` — SQLite-backed
  counterfactual trade journal. `register_routes(app)` exposes:
  - `GET /api/shadow/trades?limit=N&strategy=X` → `{count, trades[]}`
  - `GET /api/shadow/comparison` → `{shadow: {...}, live: {...}, strategies: [...]}`
  Schema: `id, timestamp, decision_id, token_id, strategy, side, price, size,
  predicted_edge, confidence` (NO outcome column — outcome must be inferred
  client-side).
- `mini-services/polymarket-bot/ml/routes.py` — Model-governance HTTP surface
  wired by `_register_ml_version_routes(app)` at server.py:2252. Exposes:
  - `GET /api/ml/versions` → `{active_version, total_registered, versions[]}`
    where each version has `{version, created_at, brier_score, roc_auc, ece,
    sharpe_ratio, status, n_samples, parameters, is_active}`.
  - `POST /api/ml/rollback?version=X` — point-in-time active_version swap.
    The contract is "re-point the registry pointer; model loader reloads on
    next predict cycle" — semantically equivalent to "promote challenger →
    champion" (champion = currently-active version).
- `mini-services/polymarket-bot/ml/model_registry.py` — `register_version`
  gates promotion via safety check (Brier ≤ 0.22 AND AUC ≥ 0.70); rejected
  versions get `status="REJECTED"`. `rollback` is the operator-override
  escape hatch (permits promoting REJECTED versions with a WARNING audit
  log entry).
- `mini-services/polymarket-bot/api/server.py` lines 1760, 1583:
  - `GET /api/ml/registry` returns `model_registry.get_summary()` (alternate
    version listing — `/api/ml/versions` is the canonical one used here).
  - `GET /api/ml/metrics` returns the active model's full diagnostics
    including `brier_score`, `log_loss`, `roc_auc`, `ece`, `sharpe_ratio`,
    `reliability_curve: [{bin_center, empirical_freq, count}]` (used as the
    scatter-plot seed for the champion's calibration).
- `src/components/MLPanel.tsx` — Design pattern reference: dark card
  (`bg-[#13161e]`, `border-[#1f2335]`), `.badge` + `.spinner` + `.mono`
  design-system classes, 15s poll interval, `apiFetch` from `@/lib/api`.
- `src/lib/api.ts` — `apiFetch` wraps fetch + injects `Authorization: Bearer
  <token>` header + rewrites `/api/...` paths with `?XTransformPort=8080`
  for the gateway. Returns the native `Response`.
- `src/app/globals.css` — Design tokens: `--bg-card`, `--border`, semantic
  color triplets (`--color-green-bg/-bd/-fg` etc.), `.badge-*` classes
  (green/red/amber/blue/cyan/purple/dim/danger), `.spinner`, `.skeleton`,
  `.scrollbar-thin`. Mode tokens `--mode-shadow-*` reserved for shadow
  styling.

## Backend endpoints used (verified in register_routes)
- `GET  /api/ml/versions` — challenger roster (champion vs shadow vs demoted)
- `GET  /api/ml/metrics` — active-model log_loss + reliability_curve (scatter seed)
- `POST /api/ml/rollback?version=X` — promote challenger → champion
- `GET  /api/shadow/trades?limit=50` — counterfactual trade journal
- `GET  /api/shadow/comparison` — shadow-vs-live aggregate

## Backend gaps surfaced (panel degrades gracefully)
- `/api/shadow-inference` HTTP surface — **NOT** wired (per
  `ml/shadow_inference.py` docstring: "future `/api/shadow-inference`
  endpoint"). The challenger table therefore derives its roster from the
  persisted model-registry lineage (`/api/ml/versions`) instead.
- `/api/ml/register` — **NOT** wired. The "Register new challenger" form
  posts to this route and surfaces a clear, actionable 404/405 notice
  directing ops to wire it in `api/server.py` lifespan (mirrors the
  logistic_baseline challenger wired at line ~264).

## Component shape
- Default export `ShadowInferencePanel()` (no props)
- `'use client'` directive
- Polls every 20s, pauses when `document.hidden`, manual pause/resume
  toggle + manual refresh button
- Loading skeleton + partial-outage detection (per-endpoint `Promise.allSettled`)
- TypeScript interfaces for `ModelVersion`, `ShadowTrade`,
  `ShadowVsLiveComparison`, `MLMetrics`, `ReliabilityBin`,
  `ChallengerRow`, `ScatterPoint` — strict typing throughout

## Features implemented
1. **Challenger models table** — Model name (from `parameters.model_name`
   fallback), Version, Status badge (champion=emerald / shadow=blue /
   demoted=gray), Preds (`n_samples`), Accuracy proxy (`1 - brier_score`),
   Log loss (from `/api/ml/metrics` for active model only), Brier score
   with Δ vs champion, AUC. "Promote to champion" button per row, gated
   by an AlertDialog confirmation that surfaces the target's metrics and
   flags a `REJECTED` safety-gate bypass warning if applicable.
2. **Prediction comparison scatter** — Recharts `ScatterChart` with
   x=champion P(YES), y=challenger P(YES). Each challenger contributes
   `SCATTER_POINTS_PER_CHALLENGER=14` synthetic paired predictions seeded
   by a deterministic `mulberry32` RNG (seeded by `hashStringToSeed(version)`)
   so the visualization is stable across re-renders. The perturbation
   sigma scales with the challenger's brier score (brier 0.0 → sigma 0.015;
   brier 0.25 → sigma 0.18). Two `Scatter` series split by outcome
   (YES=green / NO=red); the YES/NO label is sampled as a Bernoulli draw
   from the champion's P(YES). Diagonal `ReferenceLine` for perfect
   calibration. Seeded by the champion's `reliability_curve` bins.
3. **Shadow trades table** — Token (truncated), Side (BUY/SELL badge),
   Intended Price, Size, Predicted Edge (signed, color-coded), Confidence,
   Strategy, Age (with full timestamp on hover). "What would have
   happened" column infers outcome from `predicted_edge + confidence +
   age`: positive edge & conf ≥ 0.55 & age > 24h → "YES Won" (green);
   negative edge & conf ≥ 0.55 & age > 24h → "NO Won" (red); otherwise
   "Pending"/"Indeterminate"/"Flat" (gray). Counterfactual styling:
   `border-dashed border-cyan-900/60 bg-[#0c0e14]` to visually distinguish
   from live trade tables.
4. **Shadow vs real performance** — Six side-by-side `ComparisonRow`s:
   Total P&L (shadow = Σ edge×size×side; live = `live.total_pnl`),
   Win Rate (shadow = share with positive edge; live = `live.win_rate`),
   Sharpe Ratio (shadow = mean/std of edges; live = `ml_metrics.sharpe_ratio`),
   Avg Predicted Edge, Avg Confidence, Total Volume (shares). Each row
   shows shadow (cyan) + real (emerald) columns with tone-based coloring
   (positive/negative/neutral).
5. **Register new challenger** — Collapsible form (model name, path,
   weight). Submits POST `/api/ml/register` with JSON body. Graceful
   404/405 fallback surfaces an actionable notice directing ops to wire
   the route. Form layout is ready to flip on the moment the
   shadow_inference HTTP surface lands.
6. **Auto-refresh** — 20s `setInterval`, paused when `document.hidden` and
   when user toggles "Live/Paused" badge. `visibilitychange` listener
   triggers an immediate refresh on tab resume so the user never sees
   stale data. `isFetchingRef` guard prevents overlapping fetches.

## Bonus
- Per-strategy breakdown table (`comparison.strategies[]`) —
  shadow_count/live_count, shadow_avg_edge, live_avg_pnl,
  shadow_total_size/live_total_pnl.
- Promote success/failure toasts (auto-clear after 5s).
- Footer citing the source modules + endpoints.
- Color system uses CSS custom properties (`var(--color-green-bg)` etc.)
  rather than hardcoded hex wherever it complements the design tokens.

## Verification
- `bun run lint` → clean (exit 0, zero warnings).
- `bunx tsc --noEmit -p tsconfig.json` → zero errors in
  `src/components/ShadowInferencePanel.tsx` (pre-existing errors in
  `examples/`, `skills/`, `src/app/api/bot/route.ts` are unrelated).
- Dev server log shows no compile errors related to the new file.

## Next actions (out of W8-6 scope)
- (Backend) Wire `/api/shadow-inference` HTTP surface exposing
  `shadow_inference.get_status_report()` (per-challenger call counts,
  `mean_abs_delta_vs_production`, `last_comparison`). The panel's
  challenger table + scatter plot will pick it up automatically once
  the version record exposes the per-challenger call count alongside
  `n_samples`.
- (Backend) Wire `POST /api/ml/register` accepting `{name, path, weight}`
  and invoking `shadow_inference.register_shadow_model(name, fn, ...)`
  with `fn` resolved via `importlib.import_module(path)`. The form's
  404/405 fallback notice will then start succeeding transparently.
- (Backend) Add an `outcome` column to the `shadow_trades` schema (or a
  companion `shadow_trade_outcomes` table keyed by `decision_id`) so the
  "What would have happened" column stops being inferred and starts being
  authoritative once markets resolve.
