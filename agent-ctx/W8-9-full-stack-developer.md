# Task W8-9 — RetentionPanel + MLValidationPanel

## Agent
- **Task ID:** W8-9
- **Agent:** full-stack-developer
- **Date:** 2026-09-04

## Read references
- `worklog.md` tail (Wave 7 complete; backend has 77 routes, 340 tests;
  balance $111.72; settlement deadlock fixed in X8)
- `mini-services/polymarket-bot/core/retention.py` — Data retention pruning
  module. Defines four retention horizons:
  - `OBSERVABILITY_RETENTION_HOURS = 168` (7d, `metrics` table,
    `OBSERVABILITY_DB_PATH`)
  - `DECISION_LEDGER_RETENTION_HOURS = 720` (30d, `decision_events` +
    `decision_rejections` tables, `DECISION_LEDGER_DB_PATH`)
  - `EXECUTION_QUALITY_RETENTION_HOURS = 720` (30d, `execution_quality`
    table, `EXECUTION_QUALITY_DB_PATH`)
  - `AUDIT_EVENTS_RETENTION_HOURS = 2160` (90d, `audit_events` table,
    `AUDIT_DB_PATH`)
  - `register_routes(app)` exposes ONE endpoint:
    `POST /api/system/prune` body `{target: "all"|"observability"|
    "decision_ledger"|"execution_quality"|"audit_events"}` (default "all").
    Returns `{timestamp, results: {target: {pruned, max_age_hours,
    db_path, error}}, total_pruned, success}` for target=all OR
    `{target, pruned}` for a single target.
  - There is NO GET endpoint exposing the live policy / per-table sizes /
    prune history. Horizons are env-var-driven at boot; runtime update
    requires env override + restart (no PUT endpoint).
- `mini-services/polymarket-bot/ml/validation.py` — Walk-forward CV
  primitives: `time_series_cv`, `out_of_time_test`,
  `validate_no_leakage`. Register_routes exposes ONE endpoint:
  `POST /api/ml/validate` (requires feature matrix + labels in body; one-shot
  run, no persisted per-fold results). Per-fold metric suite:
  `{fold, train_size, val_size, train_end_index, val_start_index,
  val_end_index, n_samples, mean_pred, mean_actual, brier, auc, log_loss,
  accuracy}`. Aggregate: `{n_folds_evaluated, mean_brier, std_brier,
  mean_auc, std_auc, mean_log_loss, mean_accuracy, total_train_samples,
  total_val_samples, pooled}`.
- `mini-services/polymarket-bot/ml/drift_detector.py` — PSI/KS/Brier drift
  detector. `get_status_report()` returns `{psi, ks_stat, rolling_brier,
  ewma_brier, status, window_samples, outcome_samples, threshold_moderate_psi
  (0.10), threshold_critical_psi (0.25), threshold_moderate_ks (0.15),
  threshold_critical_ks (0.25), threshold_brier_drift (0.22), ewma_alpha
  (0.05), history: [last 10 PSI samples]}`. Each history entry has
  `{timestamp, psi, ks_stat, status, rolling_brier, ewma_brier}`.
  Status enum: `HEALTHY` / `MODERATE_SHIFT` / `SIGNIFICANT_DRIFT`.
- `mini-services/polymarket-bot/ml/routes.py` — `register_routes(app)`
  exposes `GET /api/ml/versions` (model lineage) and
  `POST /api/ml/rollback?version=X` (point-in-time active_version swap).
- `mini-services/polymarket-bot/ml/model_registry.py` — version record has
  `{version, created_at, brier_score, roc_auc, ece, sharpe_ratio, status,
  n_samples, parameters, is_active}`. Promotion gate: Brier ≤ 0.22 AND
  AUC ≥ 0.70 (else `status="REJECTED"`).
- `mini-services/polymarket-bot/api/server.py` — verified ML routes:
  - `GET /api/ml/metrics` (line 1583) → `{model_type, brier_score,
    roc_auc, log_loss, ece, sharpe_ratio, n_online_updates, last_trained,
    training_source, n_real_samples, n_synthetic_samples, adaptive_weights,
    meta_learner, drift, feature_importances: {name: float},
    reliability_curve: [{bin_center, empirical_freq, count} x10],
    model_ready, model_version, registry_summary}`.
  - `GET /api/ml/drift` (line 1631 and 1766 — duplicate registration,
    second wins) → `{...drift_detector.get_status_report(), meta_learner,
    orchestrator, model_version, brier_baseline, roc_auc}`.
  - `POST /api/ml/retrain` (line 1610) → `{status:"retrained", brier_score,
    roc_auc, log_loss, ece, model_version, meta_learner}`. Calls
    `ml_model.fit_initial` + `ml_model.save` then logs event.
  - `GET /api/system/health` (line 1942) → includes `market_db` block
    with `{db_backend, size_mb, snapshots_recorded, ticks_recorded,
    news_items_recorded, ml_feature_vectors}` and `checks` dict with
    subsystem health.
- `mini-services/polymarket-bot/ml/model.py` (lines 160-330) —
  `reliability_curve` is 10 bins of `{bin_center, empirical_freq, count}`
  populated during `_evaluate_calibration`. ECE = sum over bins of
  `(count/n_val) * |mean_pred - emp_freq|`.
- `src/components/MLPanel.tsx` — design-pattern reference. Uses raw divs
  styled with `bg-[#13161e]`, `border-[#1f2335]`, `.badge-*`, `.kpi-card`,
  `.mono`, `.spinner` from globals.css. Polls every 15s with setInterval.
- `src/components/SystemHealthView.tsx` — design-pattern reference. Uses
  `.kpi-card` / `.card` / `.badge` classes; polls every 3s.
- `src/lib/api.ts` — `apiFetch(input, init)` wraps `fetch` with auth header
  + gateway port transform (`?XTransformPort=8080`).
- `src/app/globals.css` — CSS design system. Verified classes: `.card`,
  `.card-header`, `.card-title`, `.kpi-card`, `.kpi-label`, `.kpi-value`,
  `.kpi-sub`, `.badge`, `.badge-green/red/amber/blue/cyan/purple/dim/danger`,
  `.data-table`, `.table-container`, `.skeleton`, `.empty-state`,
  `.error-state`, `.spinner`, `.mono`, `.scrollbar-thin`. CSS variables:
  `--bg-surface (#0e1015)`, `--bg-card (#13161e)`, `--border (#1f2335)`,
  `--text-primary/secondary/dim/mono`, `--color-green/red/amber/blue/cyan`
  + `-fg`/`-bg`/`-bd` variants, `--radius-sm/md/lg/xl`, `--space-1..6`,
  `--duration-fast`.

## Backend endpoints used (verified by reading route registrations)
1. `POST /api/system/prune` — retention prune trigger
   (core/retention.py:413). Body `{target}`. Used by RetentionPanel
   manual-prune button + AlertDialog confirmation.
2. `GET /api/system/health` — system health incl. market_db size_mb +
   per-subsystem checks dict. Used by RetentionPanel for table size KPIs.
3. `GET /api/ml/metrics` — model diagnostics incl. brier/auc/log_loss/ece,
   feature_importances, reliability_curve (10 bins), drift report, model
   version, training source, sample counts. Used by MLValidationPanel
   aggregate metric cards + reliability plot + feature importance.
4. `GET /api/ml/drift` — drift report (psi, ks, status, rolling/ewma brier,
   history[10]). Used by MLValidationPanel per-fold table + drift status.
5. `GET /api/ml/versions` — model version lineage. Used by
   MLValidationPanel active-version display + version comparison select.
6. `POST /api/ml/retrain` — trigger immediate retrain. Used by
   MLValidationPanel retrain button + result toast.

## Files created
1. `/home/z/my-project/src/components/RetentionPanel.tsx` (≈700 lines)
2. `/home/z/my-project/src/components/MLValidationPanel.tsx` (≈800 lines)

## No other files modified.

## Lint / type-check
- `bun run lint` → clean (no ESLint errors or warnings).
- `bunx tsc --noEmit | grep -E "RetentionPanel|MLValidationPanel"` → no
  matches (no TypeScript errors in either new file). Pre-existing TS errors
  in `examples/`, `skills/`, `src/app/api/bot/route.ts` are unrelated.

## Key features delivered

### RetentionPanel.tsx
- **Static retention policy display** (4 stores × horizon/tables/db_path/
  env_var/rationale) — sourced from `core/retention.py` module constants
  embedded as the source-of-truth (no GET endpoint exists at runtime).
- **Table sizes** — KPI cards display Market DB Size (MB), Snapshots
  count, Ticks count, Total Pruned count from `/api/system/health`'s
  `market_db` block. Per-store size for the four retention stores is
  honestly shown as "no probe" in the absence of a dedicated endpoint.
- **Per-store status badge** — pulls `checks.<store>` from
  `/api/system/health` (where available) and renders a UP/DEGRADED badge.
- **Prune history** — client-side log kept in `localStorage` under
  `polymarket:retention:prune_history` (max 25 entries). Each entry has
  timestamp, target, triggered_by, total_pruned, success, per_store detail,
  error. Rendered as a `.data-table` inside a max-h-72 scroll container.
- **Manual prune** — Select dropdown to choose target (all / per-store),
  AlertDialog confirmation showing what will be deleted per-target, then
  `POST /api/system/prune`. Result panel shows per-store pruned counts or
  error per store.
- **Config editor** — inline form with one Input per store (days). Local
  edits are staged in component state; the "Apply" button is intentionally
  disabled with a tooltip explaining there is no PUT endpoint (env-var
  override required at boot). Reset / Reset all buttons restore the
  canonical values from `core/retention.py`.
- **Auto-refresh** — polls `/api/system/health` every 60s via
  `setInterval`; polling is paused when `document.visibilityState ===
  'hidden'`; an `visibilitychange` listener triggers an immediate refresh
  on tab return.
- **Loading state** — skeleton rows (5×) before first payload lands.
- **Error state** — full-panel error card with Retry button when fetch
  fails.
- **Empty state** — for prune history when no operations are logged.
- **Footer** — sticky footer showing auto-refresh interval + last sync.

### MLValidationPanel.tsx
- **Aggregate metric cards** — Brier / ROC-AUC / Log-loss / ECE / Accuracy
  (the 5 per-fold metrics named in the spec). Each card color-codes the
  value (green/amber/red) against documented thresholds
  (Brier≤0.15/0.20, AUC≥0.80/0.70, log_loss≤0.45/0.55, ECE≤0.03/0.06).
  Accuracy is honestly shown as "not exposed" because the backend doesn't
  return it.
- **Per-fold table** — derived from the drift detector's `history` field
  (last 10 PSI samples from `/api/ml/drift`). Each row shows fold #,
  snapshot time, PSI, KS, Rolling Brier, EWMA Brier, and status badge.
  Aggregate row shows mean ± std across folds. (The walk-forward CV
  primitive in ml/validation.py is one-shot only via POST /api/ml/validate
  — there is no persisted per-fold endpoint, so drift history is the best
  available temporal validation signal.)
- **Calibration plot** — 10-bin reliability diagram rendered as inline SVG
  with the perfect-calibration diagonal reference, sample-count histogram
  bars as backdrop, calibration polyline + points (color-coded by |Δ|:
  green ≤0.03, amber ≤0.08, red >0.08). Below the SVG: a per-bin table
  with Pred / Actual / |Δ| / n.
- **Drift status** — PSI / KS / Rolling Brier / EWMA Brier cards with
  thresholds + color-coding. PSI sparkline (last 10 samples) rendered as
  an SVG polyline. Status badge maps HEALTHY→OK, MODERATE_SHIFT→WARNING,
  SIGNIFICANT_DRIFT→CRITICAL.
- **Feature importance** — top 20 features by importance (sorted desc),
  rendered as horizontal progress bars (cyan gradient) in a 2-column grid
  with index, name, bar, percentage.
- **Model version** — active model card showing version, status badge
  (ACTIVE/REJECTED), created_at, Brier, AUC, ECE, Sharpe, n_samples,
  feature count, training source (Real + Synthetic / Synthetic Only),
  real/synthetic sample counts.
- **Retrain button** — `POST /api/ml/retrain` with inline Loader2 spinner
  while in flight. On success: shows the new version + Brier/AUC/ECE in a
  result panel and dispatches a 5s auto-dismissing toast. On error:
  toast shows the failure message. Select dropdown lists all registered
  versions for "compare against" reference (and surfaces the rollback
  command for non-active selections).
- **Auto-refresh** — polls every 30s (parallel fetch of metrics, drift,
  versions); paused when document hidden; immediate refresh on tab return.
- **Loading / error / empty states** — skeleton rows, error card with
  retry, per-section empty states when data is missing.

## Design choices (rationale)
- **Visual style**: matches MLPanel/SystemHealthView exactly — dark
  `#13161e` card backgrounds, `#1f2335` borders, `.kpi-card` /
  `.badge-*` / `.data-table` classes from globals.css. Header icons in
  tinted square containers (amber for retention, cyan for ML validation)
  matching the existing panel color story.
- **shadcn/ui usage**: shadcn Button, Input, Select, AlertDialog, Table
  primitives used for interactive/complex widgets; raw divs + globals.css
  classes used for visual presentation. The shadcn components accept
  className overrides so I kept the existing dark palette via className
  prop while still using the radix primitives for accessibility (focus
  rings, ARIA semantics, escape-to-close on dialogs).
- **Color thresholds** (good/warn/critical): Brier 0.15/0.20 (matching
  ml/model_registry.py's promotion gate Brier ≤ 0.22). AUC 0.80/0.70
  (matching the gate's AUC ≥ 0.70). ECE 0.03/0.06. PSI 0.10/0.25
  (matching drift_detector.py's threshold_moderate_psi / threshold_critical_psi).
  Brier drift 0.22 (matching BRIER_DRIFT_THRESHOLD).
- **Honest "not exposed" states**: where the backend doesn't expose data
  (Accuracy metric, per-store sizes for retention stores, runtime
  horizon config updates, walk-forward per-fold CV persistence), the
  panels say so explicitly rather than fabricating values. This mirrors
  the existing system health panel's "latency_ms: None # not measured —
  never fabricated" convention.

## Verification
- `bun run lint` → clean.
- `bunx tsc --noEmit | grep -E "RetentionPanel|MLValidationPanel"` → 0
  matches (pre-existing TS errors in unrelated files unchanged).
- `tail dev.log` → dev server (Next.js 16.1.3 Turbopack) is healthy and
  has no compilation errors logged.
