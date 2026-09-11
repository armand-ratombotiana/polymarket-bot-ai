# AI / ML Engine — Improvement Plan

- **Domain:** AI / ML pipeline (feature extraction, model training,
  calibration, drift detection, explainability, A/B testing,
  promotion gating)
- **Owning modules:** `ml/features.py`, `ml/feature_store.py`,
  `ml/model.py`, `ml/ensemble_meta_learner.py`, `ml/calibration.py`,
  `ml/drift_detector.py`, `ml/explainability.py`, `ml/ab_testing.py`,
  `ml/shadow_inference.py`, `ml/model_registry.py`,
  `ml/training_orchestrator.py`, `ml/validation.py`,
  `ml/label_backfill.py`, `ml/copilot.py`, `ml/routes.py`,
  `ml/vector_store.py`
- **Priority classification (per God Mode §64):**
  - P0 — shadow inference promotion gate (capital risk via model
    promotion).
  - P1 — feature store, drift detection, label backfill (model
    quality + reproducibility).
  - P2 — calibration, SHAP, A/B testing (analytics + research).
- **Status as of W17-9:** IN PROGRESS — see per-improvement status
  below.

This plan defines every improvement in the AI/ML engine using the
per-improvement field set required by God Mode §63. Each
improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement ML-1 — Feature Store Enhancements

- **Problem:** `ml/feature_store.py` (W16-2) implements feature
  versioning and a materialized feature table, but (a) the feature
  set is rebuilt from `ml/features.py::extract_features()` on every
  training run — there is no historical point-in-time lookup; (b)
  the store does not separate "online" features (used at inference
  time, sub-100 ms) from "offline" features (used at training time,
  minutes-scale); (c) feature schemas are not versioned — adding a
  new feature silently changes the schema, breaking older models
  trained on the prior schema.
- **Evidence:**
  - `tests/test_feature_store.py` (W16-2, 13 tests) — covers
    schema + version persistence but not point-in-time lookups.
  - `ml/model_registry.py` stores `feature_schema_hash` but no
    tooling exists to detect schema drift between training and
    inference.
  - `FINAL_SYSTEM_REASSESSMENT.md` §3.4 lists "feature store does
    not support point-in-time correctness" as a residual.
- **Current State:** Single `feature_vectors` table; no online/offline
  split; schema versioning exists but is reactive (records the hash
  post-extraction, does not enforce).
- **Desired State:**
  1. Two tables: `feature_vectors_offline` (full feature set + history)
     and `feature_vectors_online` (slim subset, sub-100 ms lookup).
  2. `FeatureSchema` dataclass — versioned, immutable. Adding a
     feature increments the schema version.
  3. `FeatureStore.get_features(token_id, timestamp, schema_version)`
     — point-in-time correct (no future leakage).
  4. Schema-drift detector compares training-time schema vs
     inference-time schema — fails closed if mismatched.
  5. `FeatureStore.online_get(token_id)` returns the slim subset
     from cache (Redis-backed if available, in-memory fallback).
- **Proposed Solution:**
  1. Add `feature_schemas` table: `(schema_version PRIMARY KEY,
     feature_names JSON, created_at, parent_schema_version)`.
  2. Add `feature_vectors_offline` + `feature_vectors_online` tables
     (replacing the single `feature_vectors` table via migration).
  3. `FeatureSchema` dataclass + `SchemaRegistry` class.
  4. `FeatureStore.get_features(token_id, timestamp, schema_version)`
     with point-in-time semantics (uses `as_of_timestamp` column).
  5. `SchemaDriftDetector` — runs at inference; emits
     `feature_schema_drift_total{direction}` Prometheus counter.
- **Architecture:**
  ```
  extract_features(token_id, as_of_timestamp)
    └─→ FeatureSchema.current_version() → v7
         └─→ FeatureStore.offline_write(token_id, v7, features, as_of)
              └─→ schema_drift_detector.validate(features, v7) → ok
  online inference
    └─→ FeatureStore.online_get(token_id) → slim subset (cached)
         └─→ schema_drift_detector.validate_inference(features, v7) → ok
  ```
- **Implementation:**
  1. Migration `0XX_feature_store_v2.sql`.
  2. Refactor `ml/feature_store.py` with two-table split + schema
     registry.
  3. `SchemaDriftDetector` class.
  4. Update `ml/model.py` to pass `schema_version` to inference.
  5. Tests for point-in-time, online/offline, drift detection.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/feature_store.py` (rewrite)
  - `mini-services/polymarket-bot/ml/features.py` (schema tagging)
  - `mini-services/polymarket-bot/ml/model.py` (schema param)
  - `mini-services/polymarket-bot/migrations/0XX_feature_store_v2.sql`
  - `mini-services/polymarket-bot/tests/test_feature_store.py`
    (expand from 13 → ~28 tests)
- **Dependencies:** DP-5 (feature versioning) — overlaps; this
  improvement supersedes the W16-2 minimal versioning.
- **Risk:** MEDIUM — touches training + inference. Mitigation: dual-
  write period (both old + new tables populated for 1 wave before
  cutover).
- **Priority:** P1 (model reproducibility).
- **Expected Benefit:**
  - Point-in-time correctness eliminates leakage in training.
  - Online/offline split brings inference latency to < 100 ms.
  - Schema versioning prevents silent model breakage on feature
    addition.
- **Tests:** +15 tests covering offline/online split, point-in-time,
  drift detection, schema-version migration, fallback when online
  cache misses.
- **Metrics:**
  - `feature_store_online_lookup_ms` histogram.
  - `feature_store_schema_drift_total{direction}` counter.
  - `feature_store_offline_rows` gauge.
- **Acceptance Criteria:**
  - Online lookup p95 < 100 ms.
  - Schema drift triggers a WARNING log within 1 s of detection.
  - All 28 feature-store tests pass.
- **Status:** IN PROGRESS.

---

## Improvement ML-2 — Model Calibration Improvements

- **Problem:** `ml/calibration.py` (Wave 6) implements Platt
  (logistic) + isotonic calibration, but (a) calibration is fit
  once at training time and never refreshed — a model trained on
  Monday is calibrating Friday's predictions against Monday's
  distribution; (b) calibration curves are not exposed in the UI
  (the ReliabilityDiagram chart primitive exists but is wired to
  the model's training-time curve only); (c) no per-bin calibration
  (Platt + isotonic fit a single monotonic function; binned
  calibration would let the operator see where the model
  over/under-predicts).
- **Evidence:**
  - `tests/test_calibration.py` (Wave 6) — 8 tests cover Platt +
    isotonic fit + apply.
  - `src/components/charts/ReliabilityDiagram.tsx` (W13-9) —
    renders training-time reliability curve only.
  - `docs/ARCHITECTURE.md` §6.6 documents Platt + isotonic but no
    refresh schedule.
- **Current State:** Calibration fit at training time; not
  refreshed. UI shows training-time curve.
- **Desired State:**
  1. Daily calibration refresh job (cron in `training_orchestrator`
     at 04:00 UTC) — fits Platt + isotonic on the last 7 days of
     `(prediction, outcome)` pairs.
  2. Calibration history table — every refresh writes a new row
     with the fitted curve (so we can plot the evolution).
  3. Per-bin calibration: 10 bins, each with its own
     `(empirical_frequency, predicted_mean, n_samples)` tuple.
  4. ReliabilityDiagram (UI) shows BOTH the training-time curve
     AND the most-recent-refresh curve.
  5. Drift-detector integration: if Brier score of the latest
     refresh > 0.22 (the existing drift threshold), trigger a
     retrain.
- **Proposed Solution:**
  1. `calibration_history` table: `(model_version, refresh_time,
     method, curve_json, brier_score, n_samples)`.
  2. `CalibrationRefresher` class in `ml/calibration.py` with a
     `refresh(model_version)` method.
  3. Cron wiring in `ml/training_orchestrator.py`.
  4. New endpoint `GET /api/ml/calibration/history?model_version=`.
  5. ReliabilityDiagram component updated to fetch + render both
     curves.
- **Architecture:**
  ```
  training_orchestrator (cron 04:00 UTC)
    └─→ CalibrationRefresher.refresh(model_version)
         ├─→ load last 7 days of (prediction, outcome) pairs
         ├─→ fit Platt + isotonic
         ├─→ write to calibration_history table
         └─→ if Brier > 0.22 → trigger_retrain(model_version)
  ReliabilityDiagram.tsx
    └─→ fetch /api/ml/calibration/history?model_version=v7
         └─→ render training-time + latest-refresh curves overlaid
  ```
- **Implementation:**
  1. Migration `0XX_calibration_history.sql`.
  2. `CalibrationRefresher` class.
  3. Cron schedule in `training_orchestrator.py`.
  4. Endpoint + UI updates.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/calibration.py` (extend)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (extend)
  - `mini-services/polymarket-bot/migrations/0XX_calibration_history.sql`
  - `mini-services/polymarket-bot/ml/routes.py` (new endpoint)
  - `src/components/charts/ReliabilityDiagram.tsx` (overlay)
  - `src/components/MLPanel.tsx` (refresh button + history dropdown)
  - `mini-services/polymarket-bot/tests/test_calibration.py`
    (expand from 8 → ~18 tests)
- **Dependencies:** ML-1 (feature store — calibration needs
  reproducible feature sets); ML-3 (SHAP — shares the model artifact
  interface).
- **Risk:** LOW — calibration refresh is additive (training-time
  calibration preserved as the baseline).
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Predictions stay calibrated as the underlying distribution
    shifts.
  - UI shows the calibration evolution (auditable).
  - Drift detector gets a finer-grained signal (Brier of latest
    refresh, not just rolling Brier).
- **Tests:** +10 tests covering refresh happy path, refresh on
  insufficient samples, history endpoint schema, UI overlay.
- **Metrics:**
  - `calibration_refresh_brier_score{model_version, method}` gauge.
  - `calibration_refresh_total{model_version, method}` counter.
  - `calibration_drift_triggered_total` counter.
- **Acceptance Criteria:**
  - Daily refresh runs at 04:00 UTC without operator intervention.
  - ReliabilityDiagram renders both curves.
  - All 18 calibration tests pass.
- **Status:** IN PROGRESS.

---

## Improvement ML-3 — SHAP Explainability Integration

- **Problem:** `ml/explainability.py` (W16-3) ships SHAP-style
  feature attribution for the LightGBM model, but (a) it is
  invoked only on explicit API calls (`POST /api/ml/explain`), not
  on every prediction; (b) the explanation is rendered only in
  `AICopilotPanel.tsx`, not in the per-trade decision ledger; (c)
  no aggregate explanation (over the last N predictions) — operators
  cannot ask "what features drove the last 100 BUY signals?".
- **Evidence:**
  - `tests/test_explainability.py` (W16-3, 9 tests) — covers
    per-prediction SHAP + feature importance ranking.
  - `core/decision_ledger.py::record_prediction()` does not store
    SHAP values (verified via Grep).
  - `src/components/AICopilotPanel.tsx` calls `/api/ml/explain`
    on demand but `DecisionLedgerPanel.tsx` does not show SHAP.
- **Current State:** SHAP computed on demand; not stored; not
  shown in decision ledger UI.
- **Desired State:**
  1. Every `record_prediction()` call computes + stores the top-5
     SHAP values in a new `decision_shap` table.
  2. `DecisionLedgerPanel.tsx` renders the SHAP bar chart next to
     every PREDICTION row.
  3. New endpoint `GET /api/ml/explain/aggregate?strategy=&since=`
     returns the average SHAP over the last N predictions for a
     given strategy.
  4. New panel `FeatureAttributionPanel.tsx` showing the aggregate
     SHAP + a "feature importance drift" sparkline.
- **Proposed Solution:**
  1. `decision_shap` table: `(decision_id PRIMARY KEY,
     feature_importances JSON, top_5_features JSON)`.
  2. `ml/explainability.py::compute_shap(model, features)` returns
     the top-5 (already there).
  3. `decision_ledger.record_prediction()` accepts an optional
     `shap: dict[str, float]` and persists it.
  4. `ml/model.py::predict()` computes SHAP inline (cost: ~5 ms
     per prediction — acceptable).
  5. New aggregate endpoint + new panel.
- **Architecture:**
  ```
  ml/model.py.predict(token_id)
    └─→ p_yes = ensemble.predict(X)
    └─→ shap = explainability.compute_shap(model, X) → top 5
    └─→ decision_ledger.record_prediction(token_id, p_yes, shap)
         └─→ INSERT into decision_shap (decision_id, top_5_features, ...)
  DecisionLedgerPanel.tsx (per-row)
    └─→ expand row → SHAP bar chart inline
  FeatureAttributionPanel.tsx (aggregate)
    └─→ fetch /api/ml/explain/aggregate?strategy=signal_trader&since=7d
         └─→ bar chart of avg |SHAP| per feature
         └─→ sparkline of feature-importance drift over time
  ```
- **Implementation:**
  1. Migration `0XX_decision_shap.sql`.
  2. `ml/explainability.py` extension (already-shipped compute_shap
     API stays; add `aggregate_shap(decision_ids)` helper).
  3. `decision_ledger.py` extension.
  4. `ml/model.py` inline SHAP.
  5. New endpoint + new panel + DecisionLedgerPanel inline SHAP.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/explainability.py` (extend)
  - `mini-services/polymarket-bot/ml/model.py` (inline SHAP)
  - `mini-services/polymarket-bot/core/decision_ledger.py` (extend)
  - `mini-services/polymarket-bot/migrations/0XX_decision_shap.sql`
  - `mini-services/polymarket-bot/ml/routes.py` (new endpoint)
  - `src/components/DecisionLedgerPanel.tsx` (inline SHAP)
  - `src/components/FeatureAttributionPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (add to Intelligence group)
  - `mini-services/polymarket-bot/tests/test_explainability.py`
    (expand from 9 → ~18 tests)
- **Dependencies:** ML-1 (feature store — SHAP needs the same
  feature set used at training).
- **Risk:** LOW — additive; inline SHAP adds ~5 ms per prediction.
  Mitigation: feature-flagged `INLINE_SHAP_ENABLED`.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Every trade decision is explainable.
  - Aggregate SHAP surfaces feature drift before it shows up in
    Brier score.
  - Operator can answer "why did the bot buy this token?" without
    reverse-engineering.
- **Tests:** +9 tests covering inline SHAP persistence, aggregate
  endpoint, UI rendering, fallback when SHAP unavailable.
- **Metrics:**
  - `ml_explainability_compute_ms` histogram.
  - `ml_explainability_inline_total` counter.
  - `ml_explainability_aggregate_total` counter.
- **Acceptance Criteria:**
  - Every PREDICTION ledger row has a corresponding
    `decision_shap` row (after the feature flag is on).
  - `FeatureAttributionPanel` renders within 500 ms of opening.
  - All 18 explainability tests pass.
- **Status:** IN PROGRESS.

---

## Improvement ML-4 — A/B Testing Framework Expansion

- **Problem:** `ml/ab_testing.py` (W14-5) ships a minimal A/B
  framework (variant assignment + outcome tracking + significance
  test), but (a) only 2 variants are supported per experiment
  (incumbent + challenger); (b) the framework is not wired into
  the live inference path — the operator must manually flip the
  variant; (c) no automatic stopping rule (experiment runs
  indefinitely until manually stopped); (d) no UI panel.
- **Evidence:**
  - `tests/test_ab_testing.py` (W14-5, 10 tests) — covers
    2-variant setup + significance.
  - `ml/ab_testing.py` API: `create_experiment(name, variants,
    metric)` — limited to 2 variants.
  - `api/server.py` registers `/api/ab-testing/*` endpoints but no
    UI panel exists for them.
- **Current State:** 2-variant experiments; manual variant flip; no
  stopping rule; no UI.
- **Desired State:**
  1. N-variant experiments (incumbent + up to 4 challengers).
  2. Auto-traffic-split: 50 % incumbent + 50 % / N challengers.
  3. Sequential testing stopping rule (always-valid p-values;
     mSPRT) — experiment auto-stops when one variant is
     significantly better OR after max-duration.
  4. UI panel `ABTestingPanel.tsx`: list of experiments, traffic
     split, live p-value, decision (winner / inconclusive / running).
  5. Auto-promotion: if a challenger wins, automatically promote
     via ML-7 (shadow inference promotion gate).
- **Proposed Solution:**
  1. Extend `ml/ab_testing.py::Experiment` to support N variants.
  2. Add `SequentialStoppingRule` class (mSPRT).
  3. Wire `Experiment.assign(token_id)` into `ml/model.py::predict`
     (the experiment name is configured per-strategy).
  4. New endpoints: `GET /api/ab-testing/experiments`,
     `POST /api/ab-testing/experiments`,
     `GET /api/ab-testing/experiments/{id}/decision`.
  5. `ABTestingPanel.tsx` + Sidebar entry.
- **Architecture:**
  ```
  strategy config
    └─→ experiment_id = "v7_vs_v8_p_yes_calibrated"
  ml/model.py.predict(token_id, strategy)
    └─→ experiment = ab_testing.get(experiment_id)
    └─→ variant = experiment.assign(token_id) → "incumbent" | "challenger_1" | ...
    └─→ p_yes = model_registry.get(variant.model_version).predict(X)
    └─→ record (token_id, variant, p_yes, outcome=later)
  SequentialStoppingRule.evaluate(experiment)
    └─→ if any variant has p < 0.01 → declare winner
         └─→ if elapsed > max_duration → declare inconclusive
  ```
- **Implementation:**
  1. Extend `ml/ab_testing.py` (N variants + stopping rule).
  2. Wire into `ml/model.py::predict`.
  3. New endpoints.
  4. New UI panel + Sidebar entry.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/ab_testing.py` (rewrite)
  - `mini-services/polymarket-bot/ml/model.py` (experiment wiring)
  - `mini-services/polymarket-bot/api/server.py` (new endpoints)
  - `src/components/ABTestingPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (entry)
  - `mini-services/polymarket-bot/tests/test_ab_testing.py`
    (expand from 10 → ~22 tests)
- **Dependencies:** ML-7 (shadow inference promotion gate — auto-
  promotion uses the gate).
- **Risk:** MEDIUM — auto-promotion can promote a bad model.
  Mitigation: the gate (ML-7) requires challenger to be
  statistically significantly better AND shadow-traded for >= 7
  days.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Operators can run multi-model bake-offs without manual flips.
  - Auto-promotion reduces operator toil.
  - Stopping rule prevents indefinite experiments.
- **Tests:** +12 tests covering N-variant, stopping rule, auto-
  promotion, UI panel.
- **Metrics:**
  - `ab_testing_experiments_active` gauge.
  - `ab_testing_variant_predictions_total{experiment, variant}` counter.
  - `ab_testing_decisions_total{experiment, decision}` counter.
- **Acceptance Criteria:**
  - All 22 A/B testing tests pass.
  - An experiment with a clear winner auto-stops within 1 hour of
    the stopping rule firing.
  - UI panel renders live p-values updated every 60 s.
- **Status:** IN PROGRESS.

---

## Improvement ML-5 — Drift Detection Improvements

- **Problem:** `ml/drift_detector.py` (Wave 3, refined Wave 6)
  monitors 3 signals (PSI, KS, Brier-rolling + EWMA), but (a) the
  PSI baseline is the model's own prediction distribution captured
  at warmup — if the warmup window was unrepresentative, the
  baseline is wrong forever; (b) drift is detected per-model, not
  per-feature — feature-level drift (e.g. `liquidity` distribution
  shift) is invisible until it shows up in PSI; (c) the operator
  cannot see the raw PSI / KS values per feature in the UI.
- **Evidence:**
  - `tests/test_drift_detector.py` (W6, 7 tests) — covers
    per-model drift.
  - `ml/drift_detector.py::compute_psi()` exists but is not
    called per-feature.
  - `src/components/MLPanel.tsx` shows model-level drift status only.
- **Current State:** Model-level drift detection (3 signals). No
  per-feature drift. UI shows model-level only.
- **Desired State:**
  1. Per-feature drift: PSI + KS for every feature in the
     `FeatureSchema` (computed every 50 predictions per feature).
  2. Baseline capture: PSI baseline is the training-set feature
     distribution (not the warmup window) — captured at training
     time, stored in `model_registry`.
  3. UI: per-feature drift table (feature, PSI, KS, status) +
    sparkline of the last 20 PSI values per feature.
  4. Aggregate drift: if > 50 % of features are in
    `MODERATE_SHIFT`, the model-level status escalates to
    `SIGNIFICANT_DRIFT`.
- **Proposed Solution:**
  1. `FeatureDriftDetector` class — wraps `ModelDriftDetector` for
     the per-feature case.
  2. `model_registry` stores `training_feature_distributions` JSON
     at training time.
  3. `FeatureDriftDetector.compute_psi_per_feature(predictions,
     training_distributions)` returns a dict.
  4. New endpoint `GET /api/ml/drift/features?model_version=`.
  5. UI panel extension.
- **Architecture:**
  ```
  training
    └─→ model_registry.register(model, training_feature_distributions)
  inference (every 50 predictions)
    └─→ ModelDriftDetector.compute_psi (model-level, as today)
    └─→ FeatureDriftDetector.compute_psi_per_feature
         └─→ for each feature in schema:
              psi = compute_psi(current_distribution, training_distribution)
              status = classify(psi)
         └─→ if > 50% MODERATE → escalate model-level status
  UI: MLPanel → per-feature table + sparkline
  ```
- **Implementation:**
  1. `FeatureDriftDetector` class.
  2. Extend `model_registry.py` to persist training distributions.
  3. Extend `ml/routes.py` with the per-feature endpoint.
  4. UI extension in `MLPanel.tsx`.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/drift_detector.py` (extend)
  - `mini-services/polymarket-bot/ml/model_registry.py` (extend)
  - `mini-services/polymarket-bot/ml/routes.py` (new endpoint)
  - `src/components/MLPanel.tsx` (per-feature table)
  - `mini-services/polymarket-bot/tests/test_drift_detector.py`
    (expand from 7 → ~18 tests)
- **Dependencies:** ML-1 (feature store — needs stable feature
  schema to iterate over).
- **Risk:** LOW — additive; model-level drift detection
  unchanged.
- **Priority:** P1 (model quality).
- **Expected Benefit:**
  - Operators see WHICH feature is drifting, not just "the model
    is drifting".
  - Earlier retrain triggers (per-feature drift precedes
    model-level drift).
  - Feature-engineering feedback loop (drifting features are
    candidates for re-engineering).
- **Tests:** +11 tests covering per-feature PSI, baseline from
  training, aggregate escalation, UI rendering.
- **Metrics:**
  - `ml_drift_feature_psi{feature}` gauge.
  - `ml_drift_feature_ks{feature}` gauge.
  - `ml_drift_feature_status{feature, status}` gauge.
- **Acceptance Criteria:**
  - Per-feature drift table renders in MLPanel.
  - When > 50 % of features are MODERATE_SHIFT, model-level status
    escalates within 5 minutes.
  - All 18 drift tests pass.
- **Status:** IN PROGRESS.

---

## Improvement ML-6 — Label Backfill Automation

- **Problem:** `core/label_backfill.py` (R5, Wave 1) implements
  label backfill from Gamma's resolved-markets endpoint, but (a)
  it must be triggered manually (`POST /api/ml/label-backfill`);
  (b) it does not retry failed markets; (c) it does not deduplicate
  — running it twice backfills the same markets twice; (d) it does
  not trigger a retrain after backfilling.
- **Evidence:**
  - `tests/test_label_backfill.py` (W5, 7 tests) — covers
    parsing + processing but not retry or scheduling.
  - `docs/ARCHITECTURE.md` §6.6 documents the manual flow.
  - Production: 4 970 labels backfilled (per V15 reassessment);
    the operator reports running it ~weekly.
- **Current State:** Manual trigger; no retry; no dedup; no
  auto-retrain.
- **Desired State:**
  1. Cron job in `training_orchestrator` runs backfill daily at
     03:00 UTC.
  2. Failed markets stored in `label_backfill_failures` table with
     retry count; retried with exponential backoff (1h, 6h, 24h).
  3. Deduplication: `INSERT OR IGNORE` on `(market_id, token_id)`.
  4. Auto-retrain: if backfill added > 100 new labels since the
     last training run, trigger a retrain.
  5. New endpoint `GET /api/ml/label-backfill/status` showing the
     last-run time, success/failure counts, retry queue.
- **Proposed Solution:**
  1. `label_backfill_failures` table.
  2. `LabelBackfillScheduler` class — runs daily, processes the
     failure queue first, then fresh markets.
  3. Cron wiring in `training_orchestrator.py`.
  4. Auto-retrain trigger via `training_orchestrator.trigger_retrain
     (reason="new_labels", n_new=...)`.
  5. New status endpoint.
- **Architecture:**
  ```
  training_orchestrator (cron 03:00 UTC)
    └─→ LabelBackfillScheduler.run()
         ├─→ process failure queue (exponential backoff)
         ├─→ fetch fresh resolved markets from Gamma
         ├─→ for each: INSERT OR IGNORE (dedup)
         ├─→ if n_new > 100 since last train → trigger_retrain
         └─→ write status row
  GET /api/ml/label-backfill/status
    └─→ { last_run, success_count, failure_count, retry_queue_size }
  ```
- **Implementation:**
  1. Migration `0XX_label_backfill_failures.sql`.
  2. `LabelBackfillScheduler` class.
  3. Cron schedule in `training_orchestrator.py`.
  4. New endpoint.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/label_backfill.py` (extend)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (extend)
  - `mini-services/polymarket-bot/migrations/0XX_label_backfill_failures.sql`
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `mini-services/polymarket-bot/tests/test_label_backfill.py`
    (expand from 7 → ~16 tests)
- **Dependencies:** DP-3 (data quality — backfill success depends
  on Gamma API availability, which the data-quality monitor
  tracks).
- **Risk:** LOW — additive; manual trigger preserved.
- **Priority:** P1 (model quality).
- **Expected Benefit:**
  - Model retrains on the freshest labels automatically.
  - Operators no longer need to remember to backfill.
  - Failed markets self-heal.
- **Tests:** +9 tests covering scheduler, retry, dedup, auto-
  retrain trigger, status endpoint.
- **Metrics:**
  - `label_backfill_total{status}` counter.
  - `label_backfill_retry_queue_size` gauge.
  - `label_backfill_retrain_triggered_total` counter.
- **Acceptance Criteria:**
  - Daily backfill runs without operator intervention.
  - Failed markets retry 3 times before giving up.
  - Auto-retrain triggers when n_new > 100.
  - All 16 label-backfill tests pass.
- **Status:** IN PROGRESS.

---

## Improvement ML-7 — Shadow Inference Promotion Gate

- **Problem:** `ml/shadow_inference.py` (T13, Wave 3) records
  challenger-model predictions alongside incumbent predictions,
  but there is NO automated promotion gate. An operator can
  promote a challenger to incumbent via `POST /api/ml/rollback`
  (which is mis-named — it's actually "set active model version"),
  but the decision is entirely manual. There is no requirement
  that the challenger be statistically significantly better, no
  minimum shadow period, no minimum sample size.
- **Evidence:**
  - `tests/test_shadow_inference.py` (W7, 6 tests) — covers
    challenger registration + shadow prediction; no promotion
    logic.
  - `core/live_safety_gate.py` check #10 `model_registered` checks
    "is_fitted AND not stale" — does NOT verify the promotion gate.
  - `docs/ARCHITECTURE.md` §6.8 documents shadow inference but no
    promotion gate.
- **Current State:** Shadow challenger records predictions;
  promotion is manual + unguarded.
- **Desired State:**
  1. `PromotionGate` class — evaluates a challenger against the
     incumbent on 4 criteria:
     a. **Statistical significance**: challenger's Brier score is
        significantly lower (paired t-test, p < 0.01).
     b. **Minimum shadow period**: challenger has been shadow-trading
        for >= 7 days.
     c. **Minimum sample size**: >= 500 shadow predictions.
     d. **No drift regression**: challenger's PSI < incumbent's PSI
        (the challenger isn't drifting harder).
  2. `POST /api/ml/promote` endpoint — refuses promotion if any
     criterion fails; returns a 409 with the failing criteria.
  3. `GET /api/ml/promotion/eligibility?challenger=` endpoint —
     returns the 4 criteria + their pass/fail + raw values.
  4. Auto-promotion hook from the A/B testing framework (ML-4) —
     when an A/B test declares a winner, the gate is consulted;
     promotion only fires if the gate passes.
  5. Live safety gate check #11 `promotion_gate_last_passed` —
     required for live trading.
- **Proposed Solution:**
  1. `PromotionGate` class in `ml/shadow_inference.py`.
  2. `evaluate(challenger_version)` method returning a dict of
     criteria.
  3. New endpoints.
  4. Live safety gate extension (check #11).
  5. Auto-promotion hook from `ml/ab_testing.py`.
- **Architecture:**
  ```
  ml/shadow_inference.py
    └─→ PromotionGate
         ├─→ evaluate(challenger_version)
         │    ├─→ paired_t_test(challenger_briers, incumbent_briers) → p_value
         │    ├─→ shadow_period = now - challenger.registered_at
         │    ├─→ n_predictions = shadow_inference.prediction_count(challenger)
         │    └─→ challenger_psi vs incumbent_psi
         ├─→ can_promote(challenger_version) → bool
         │    └─→ all criteria pass
         └─→ promote(challenger_version)
              └─→ if can_promote: model_registry.set_active(challenger_version)
                   else: raise PromotionGateError(failing_criteria)
  api/server.py
    └─→ POST /api/ml/promote { challenger_version }
         └─→ PromotionGate.promote(challenger_version)
              └─→ 200 on success / 409 with criteria on failure
  ```
- **Implementation:**
  1. `PromotionGate` class.
  2. `scipy.stats.ttest_rel` for paired t-test.
  3. New endpoints.
  4. Live safety gate extension.
  5. Tests for all 4 criteria + the gate's pass/fail logic.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/shadow_inference.py` (extend)
  - `mini-services/polymarket-bot/core/live_safety_gate.py`
    (new check #11)
  - `mini-services/polymarket-bot/api/server.py` (new endpoints)
  - `mini-services/polymarket-bot/tests/test_shadow_inference.py`
    (expand from 6 → ~18 tests)
  - `mini-services/polymarket-bot/tests/test_live_safety_gate.py`
    (expand for new check)
- **Dependencies:** ML-4 (A/B testing — auto-promotion hook).
  This is the P0 blocking item — without it, an operator can
  promote a worse model and lose capital.
- **Risk:** HIGH — gates the model promotion path. Mitigation:
  the gate is additive (manual promotions still work, they just
  emit a WARNING if the gate would have failed).
- **Priority:** P0 (capital risk — promoting a worse model = real
  loss).
- **Expected Benefit:**
  - No operator can promote a worse model by accident.
  - The 4 criteria are auditable (each promotion leaves a
    promotion_record with the criterion values).
  - Closes the last live-trading safety gap.
- **Tests:** +12 tests covering each criterion's pass/fail, the
  gate's combined logic, the endpoint's 409 path, the auto-
  promotion hook.
- **Metrics:**
  - `promotion_gate_evaluations_total{result}` counter.
  - `promotion_gate_criterion_value{criterion}` gauge.
  - `promotion_gate_last_passed_timestamp` gauge.
- **Acceptance Criteria:**
  - All 18 shadow-inference tests pass.
  - Live safety gate reports 5/10 → 6/10 (after the check #11
    addition).
  - No model can be promoted without the gate's blessing (except
    via the explicit `--force` flag, which logs a CRITICAL audit
    event).
- **Status:** TODO (not started — scheduled for W18; this is the
  AI/ML domain's only P0 item and the W18 critical path).
