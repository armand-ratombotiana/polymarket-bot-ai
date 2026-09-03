# Observability — Improvement Plan

- **Domain:** Observability (metrics, audit, Prometheus/Grafana,
  alerting)
- **Owning modules:** `core/observability.py`,
  `core/observability_collector.py`, `core/audit_logger.py`,
  `core/immutable_audit.py`, `core/prometheus_metrics.py`,
  `core/alerting.py`, `core/rate_limit_tracker.py`,
  `core/profiling.py`, `core/ws_broadcast.py`,
  `grafana/*`, `prometheus.yml`
- **Source authority:** God Mode §54 (observability expansion),
  §55 (auditability improvements).
- **Priority classification (per God Mode §64):**
  - P1 — auditability (capital-protection), alerting.
  - P2 — observability expansion, Prometheus/Grafana.
- **Status as of W17-9:** IN PROGRESS — see per-improvement
  status below.

This plan defines every improvement in the observability domain
using the per-improvement field set required by God Mode §63.
Each improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement OB-1 — Observability Expansion (§54)

- **Problem:** `core/observability.py` (S13, Wave 2) +
  `core/observability_collector.py` (T7, Wave 3) collect 31
  metrics across 6 categories (system, ML, risk, execution,
  data, business). However, (a) no distributed tracing (OpenTelemetry);
  (b) no structured-log correlation (logs lack a trace ID); (c)
  no SLO dashboards (the Grafana dashboard shows raw metrics,
  not "is this SLO met?"); (d) no error-budget tracking (no
  burn-rate alerts when error budget is consumed too fast).
- **Evidence:**
  - `tests/test_observability.py` (T10, Wave 3) — 6 tests
    covering record + retrieve.
  - `tests/test_observability_collector.py` (W8, Wave 6) — 5
    tests covering the background collector.
  - `grafana/` directory exists with auto-provisioned
    dashboards (Wave 14, W14-7).
  - `prometheus.yml` exists (Wave 14, W14-6).
  - No OpenTelemetry integration; no SLO dashboards.
- **Current State:** 31 metrics; Grafana dashboard with raw
  metrics; no tracing; no SLOs; no error budgets.
- **Desired State:**
  1. **OpenTelemetry tracing** — every API request + every
    decision-ledger stage carries a trace ID; spans are exported
    to Jaeger (or to the OpenTelemetry collector in the same
    docker-compose).
  2. **Structured log correlation** — every JSON log line
    carries `trace_id` + `span_id` so logs can be filtered by
    trace.
  3. **SLO dashboards** — 4 SLOs: API availability (99.5 %),
    API p95 latency (< 500 ms), decision-ledger write success
    (99.9 %), order-placement success (99 %). Each SLO has a
    burn-rate alert.
  4. **Error-budget tracking** — every SLO has an error budget
    (e.g. 0.5 % for availability); the dashboard shows budget
    remaining + projected exhaustion date.
- **Proposed Solution:**
  1. Add `opentelemetry-distro` + `opentelemetry-instrumentation-fastapi`
    to requirements.txt.
  2. Wire OpenTelemetry in `api/server.py` startup.
  3. Add `trace_id` to every structured log line in
    `core/logging_config.py`.
  4. New SLO dashboards in `grafana/`.
  5. Burn-rate alerts in `core/alerting.py`.
- **Architecture:**
  ```
  FastAPI request
    └─→ OpenTelemetry middleware creates span
         └─→ span_id, trace_id propagated to:
              ├─→ structured logs (logging_config adds fields)
              ├─→ decision_ledger writes (trace_id column)
              └─→ Prometheus metrics (exemplars)
  OpenTelemetry collector (docker-compose service)
    └─→ exports to Jaeger (docker-compose service)
  Grafana
    └─→ SLO dashboard: 4 SLOs + burn-rate alerts
  core/alerting.py
    └─→ if burn_rate > 14.4x in 1h → page
    └─→ if burn_rate > 6x in 6h → warn
  ```
- **Implementation:**
  1. Add OpenTelemetry to `requirements.txt`.
  2. Wire in `api/server.py` startup.
  3. Extend `core/logging_config.py` to inject trace_id.
  4. Extend `core/decision_ledger.py` with `trace_id` column.
  5. New Grafana dashboards.
  6. Burn-rate alert logic.
- **Files Affected:**
  - `mini-services/polymarket-bot/requirements.txt` (add
    opentelemetry)
  - `mini-services/polymarket-bot/api/server.py` (wire OTel)
  - `mini-services/polymarket-bot/core/logging_config.py`
    (extend)
  - `mini-services/polymarket-bot/core/decision_ledger.py`
    (extend — trace_id column)
  - `mini-services/polymarket-bot/migrations/0XX_decision_events_trace_id.sql`
    (new)
  - `mini-services/polymarket-bot/core/alerting.py` (extend —
    burn-rate alerts)
  - `grafana/dashboards/slo.json` (new)
  - `docker-compose.yml` (add otel-collector + jaeger services)
  - `mini-services/polymarket-bot/tests/test_observability.py`
    (expand from 6 → ~14 tests)
- **Dependencies:** DP-1 (Postgres migration — trace_id column
  is more useful with faster queries).
- **Risk:** MEDIUM — OpenTelemetry adds latency. Mitigation:
  sampling (10 % of requests traced by default; 100 % for
  errors).
- **Priority:** P2 (observability polish).
- **Expected Benefit:**
  - Distributed tracing answers "why was this order slow?"
  - SLO dashboards surface "is the system meeting its contract?"
  - Burn-rate alerts catch SLO violations before they breach.
- **Tests:** +8 tests covering trace_id propagation, SLO
  evaluation, burn-rate alerting.
- **Metrics:**
  - `otel_spans_emitted_total` counter.
  - `slo_error_budget_remaining{slo}` gauge.
  - `slo_burn_rate{slo, window}` gauge.
- **Acceptance Criteria:**
  - All 14 observability tests pass.
  - A request's trace_id appears in the structured log + the
    decision_ledger row + the Grafana trace.
  - SLO dashboard renders 4 SLOs with budget tracking.
- **Status:** IN PROGRESS.

---

## Improvement OB-2 — Auditability Improvements (§55)

- **Problem:** `core/audit_logger.py` (Wave 5) +
  `core/immutable_audit.py` (Wave 5) implement append-only audit
  logging with hash-chaining (every entry's hash includes the
  previous entry's hash, so tampering is detectable). However,
  (a) the audit log is per-database (8 separate audit trails
  with no cross-DB integrity); (b) no tamper-detection cron (the
  hash chain is verifiable but no job verifies it); (c) no
  operator-facing audit-log browser (the `AuditLogPanel.tsx`
  exists but doesn't verify integrity); (d) no export to
  external WORM storage (write-once-read-many — required for
  some compliance regimes).
- **Evidence:**
  - `tests/test_audit_logger.py` — exists with coverage.
  - `src/components/AuditLogPanel.tsx` (W14-4) — renders the
    log with severity filter + CSV/JSON export; no integrity
    verification.
  - `FINAL_SYSTEM_REASSESSMENT.md` §3.6 lists "cross-DB audit
    integrity" as a residual risk.
- **Current State:** 8 per-DB audit trails; hash-chained; no
  tamper-detection cron; no integrity UI; no WORM export.
- **Desired State:**
  1. **Cross-DB integrity**: a top-level `audit_chain` table
     that links the 8 per-DB chains (every per-DB entry's hash
     is recorded in the top-level chain, so cross-DB tampering
     is detectable).
  2. **Tamper-detection cron**: hourly job recomputes every
     chain + alerts on any mismatch.
  3. **Integrity UI**: `AuditLogPanel.tsx` gains a "Verify
     Integrity" button + a green/red integrity status pill.
  4. **WORM export**: daily cron exports the day's audit entries
     to S3 (or local WORM storage) with a content hash.
- **Proposed Solution:**
  1. New `core/audit_chain.py` module — top-level chain.
  2. Extend `core/audit_logger.py` to record every entry in the
     top-level chain.
  3. New `AuditIntegrityChecker` class — runs hourly.
  4. Extend `AuditLogPanel.tsx` with verify button.
  5. New `AuditWORMExporter` class — daily cron.
- **Architecture:**
  ```
  audit_logger.record(entry)
    └─→ write to per-DB audit table
    └─→ audit_chain.append(entry_hash, db_name, timestamp)
         └─→ INSERT INTO audit_chain (hash, prev_hash, ...)
  AuditIntegrityChecker.run() (hourly cron)
    └─→ for each per-DB audit table:
         recompute every hash; compare to stored
         if mismatch → alerting.alert("audit_tamper_detected")
    └─→ for top-level audit_chain:
         recompute every hash; compare to stored
         if mismatch → alerting.alert("audit_chain_tamper_detected")
  AuditLogPanel.tsx
    └─→ "Verify Integrity" button → calls /api/audit/verify
         └─→ green pill if all hashes match; red pill if mismatch
  AuditWORMExporter.run() (daily cron)
    └─→ export yesterday's entries to S3 with content hash
    └─→ record the S3 object key in audit_chain
  ```
- **Implementation:**
  1. New module `core/audit_chain.py`.
  2. Extend `core/audit_logger.py`.
  3. New `AuditIntegrityChecker` + `AuditWORMExporter` classes.
  4. Hourly + daily cron wiring.
  5. UI verify button + status pill.
  6. New endpoint `POST /api/audit/verify`.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/audit_chain.py` (new)
  - `mini-services/polymarket-bot/core/audit_logger.py` (extend)
  - `mini-services/polymarket-bot/core/immutable_audit.py`
    (extend — top-level chain)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (cron)
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `mini-services/polymarket-bot/migrations/0XX_audit_chain.sql`
    (new)
  - `src/components/AuditLogPanel.tsx` (extend)
  - `mini-services/polymarket-bot/tests/test_audit_logger.py`
    (expand for chain + integrity)
  - `mini-services/polymarket-bot/tests/test_audit_chain.py`
    (new)
- **Dependencies:** BE-5 (idempotency — audit entries need
  stable keys).
- **Risk:** MEDIUM — touching the audit log (a compliance
  surface). Mitigation: existing per-DB audit log unchanged; the
  top-level chain is additive.
- **Priority:** P1 (capital protection — audit log tampering =
  silent capital loss).
- **Expected Benefit:**
  - Cross-DB tampering is detectable.
  - Hourly integrity check catches tampering within 1 h.
  - WORM export supports compliance regimes.
  - Operator-facing integrity status builds trust.
- **Tests:** +14 tests covering top-level chain, integrity
  checker, WORM exporter, endpoint, UI.
- **Metrics:**
  - `audit_chain_entries_total` counter.
  - `audit_integrity_check_total{result}` counter.
  - `audit_worm_export_total` counter.
- **Acceptance Criteria:**
  - All audit tests pass.
  - Simulated tampering is detected within 1 h.
  - The UI's verify button returns green when no tampering.
- **Status:** IN PROGRESS.

---

## Improvement OB-3 — Prometheus/Grafana Enhancements

- **Problem:** `core/prometheus_metrics.py` (W14-6, Wave 14)
  exposes `/metrics` with the standard counter/gauge/histogram
  primitives. The Grafana dashboard (W14-7) auto-provisions 6
  dashboards (overview, ML, risk, execution, data, business).
  However, (a) no per-strategy dashboards; (b) no alert-rule
  library (the operator must write PromQL by hand); (c) no
  long-term metrics storage (Prometheus default retention is 15
  days; backtests need 90+ days); (d) no metric cardinality
  guard (a label explosion can OOM Prometheus).
- **Evidence:**
  - `core/prometheus_metrics.py` — exposes ~40 metrics.
  - `grafana/dashboards/` — 6 dashboards.
  - No per-strategy dashboard.
  - `prometheus.yml` — single Prometheus instance, 15-day
    retention default.
- **Current State:** 40 metrics, 6 dashboards, no alerts, 15-d
  retention, no cardinality guard.
- **Desired State:**
  1. **Per-strategy dashboards** — one dashboard per strategy
    (signal_trader, market_maker, arb_scanner) with the
    strategy-specific metrics + a per-strategy alert pack.
  2. **Alert-rule library** — `grafana/alerts/` directory with
    pre-built alerts (high slippage, drift detected, breaker
    open, parity violation, audit tamper).
  3. **Long-term storage** — Prometheus remote-write to
    Mimir (or VictoriaMetrics) for 90+ day retention.
  4. **Cardinality guard** — `core/prometheus_metrics.py`
    rejects label sets exceeding 100 unique values per label.
- **Proposed Solution:**
  1. New per-strategy dashboards in `grafana/dashboards/`.
  2. New alert rules in `grafana/alerts/`.
  3. Add Mimir (or VictoriaMetrics) to `docker-compose.yml`.
  4. Cardinality guard in `core/prometheus_metrics.py`.
- **Architecture:**
  ```
  prometheus.yml
    └─→ remote_write:
         - url: http://mimir:9009/api/v1/push
  grafana/dashboards/
    ├─→ overview.json (existing)
    ├─→ ml.json (existing)
    ├─→ signal_trader.json (new)
    ├─→ market_maker.json (new)
    └─→ arb_scanner.json (new)
  grafana/alerts/
    ├─→ high_slippage.yml
    ├─→ drift_detected.yml
    ├─→ breaker_open.yml
    ├─→ parity_violation.yml
    └─→ audit_tamper.yml
  core/prometheus_metrics.py
    └─→ before recording a metric, check label cardinality
         └─→ if label has > 100 unique values → drop metric + log
  ```
- **Implementation:**
  1. New dashboards + alert rules.
  2. Add Mimir service to `docker-compose.yml`.
  3. Extend `prometheus.yml` with `remote_write`.
  4. Cardinality guard in `core/prometheus_metrics.py`.
- **Files Affected:**
  - `grafana/dashboards/signal_trader.json` (new)
  - `grafana/dashboards/market_maker.json` (new)
  - `grafana/dashboards/arb_scanner.json` (new)
  - `grafana/alerts/*.yml` (new — 5 alert files)
  - `docker-compose.yml` (add mimir)
  - `prometheus.yml` (extend remote_write)
  - `mini-services/polymarket-bot/core/prometheus_metrics.py`
    (extend — cardinality guard)
  - `mini-services/polymarket-bot/tests/test_prometheus.py`
    (expand)
- **Dependencies:** None.
- **Risk:** LOW — additive.
- **Priority:** P2 (polish).
- **Expected Benefit:**
  - Per-strategy dashboards surface strategy-specific health.
  - Alert library reduces operator toil (no more hand-written
    PromQL).
  - Long-term storage enables 90-day backtests against live
    metrics.
  - Cardinality guard prevents Prometheus OOM.
- **Tests:** +6 tests covering cardinality guard, alert rule
  loading, dashboard provisioning.
- **Metrics:** meta — `prometheus_cardinality_dropped_total`
  counter.
- **Acceptance Criteria:**
  - 3 new per-strategy dashboards render.
  - 5 new alert rules fire when their conditions are met.
  - Mimir receives remote-writes within 10 s.
  - Cardinality guard drops metrics with > 100 unique label
    values.
- **Status:** IN PROGRESS.

---

## Improvement OB-4 — Alerting System Improvements

- **Problem:** `core/alerting.py` (W16-1, Wave 16) ships an
  alerting system with severity tagging (INFO/WARN/ERROR/
  CRITICAL), ack/resolve workflow, and an alert history table.
  However, (a) no on-call routing (every alert goes to every
    operator); (b) no deduplication (the same alert fires every
    30 s until resolved); (c) no escalation policy (a CRITICAL
    alert that's un-acked for 5 min should page the next
    operator); (d) no UI for acking (the ack endpoint exists
    but the UI doesn't surface a button).
- **Evidence:**
  - `core/alerting.py` — 6 tests covering record + ack + resolve.
  - `src/components/AuditLogPanel.tsx` — renders the audit log;
    no dedicated alert panel.
  - No on-call schedule config.
- **Current State:** Severity + ack/resolve; no routing, no
  dedup, no escalation, no UI.
- **Desired State:**
  1. **On-call routing**: config file `config/on_call.yml` with
    per-severity routing rules (CRITICAL → Slack #oncall;
    ERROR → Slack #errors; WARN → daily digest email).
  2. **Deduplication**: alerts with the same fingerprint
    (hash of `severity + alert_type + key_fields`) within a 5-min
    window are deduplicated; the alert count is incremented
    instead.
  3. **Escalation policy**: a CRITICAL alert un-acked for 5 min
    escalates to the next on-call operator (config-driven).
  4. **Alert panel UI**: new `AlertsPanel.tsx` showing active
    alerts with ack buttons; integrated into the Command
    Center's alert banner (UI-1).
- **Proposed Solution:**
  1. Extend `core/alerting.py` with `OnCallRouter`,
    `AlertDeduplicator`, `EscalationPolicy` classes.
  2. New config file `config/on_call.yml`.
  3. New `AlertsPanel.tsx` component.
  4. Wire the panel into the Command Center.
- **Architecture:**
  ```
  alerting.alert(severity, alert_type, message, key_fields)
    └─→ fingerprint = hash(severity + alert_type + key_fields)
    └─→ AlertDeduplicator.check(fingerprint)
         └─→ if exists in last 5min: increment count, return
         └─→ else: write new alert
              └─→ OnCallRouter.route(severity, alert_type)
                   └─→ CRITICAL → Slack #oncall + email
                   └─→ ERROR → Slack #errors
                   └─→ WARN → daily digest
  EscalationPolicy.run() (cron every 1 min)
    └─→ for each un-acked CRITICAL alert older than 5 min:
         page next on-call operator
  AlertsPanel.tsx
    └─→ list of active alerts with Ack / Resolve buttons
    └─→ severity colour-coded
  Command Center AlertBanner (UI-1)
    └─→ shows latest un-acked alert with Ack button
  ```
- **Implementation:**
  1. Extend `core/alerting.py`.
  2. New config file.
  3. New `AlertsPanel.tsx`.
  4. Wire into Command Center.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/alerting.py` (extend)
  - `mini-services/polymarket-bot/config/on_call.yml` (new)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (escalation cron)
  - `src/components/AlertsPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (entry)
  - `src/components/AlertBanner.tsx` (shared with UI-1)
  - `mini-services/polymarket-bot/tests/test_alerting.py` (new
    + expand)
- **Dependencies:** UI-1 (Command Center alert banner uses the
  same alert API).
- **Risk:** LOW — additive.
- **Priority:** P1 (capital protection — un-acked alerts =
  silent capital risk).
- **Expected Benefit:**
  - Right alerts reach the right operators.
  - Deduplication prevents alert fatigue.
  - Escalation ensures no CRITICAL alert goes un-acked.
  - UI ack flow replaces CLI ack.
- **Tests:** +14 tests covering dedup, routing, escalation,
  UI ack flow.
- **Metrics:**
  - `alerts_active_total{severity}` gauge.
  - `alerts_deduplicated_total` counter.
  - `alerts_escalated_total` counter.
  - `alerts_ack_lag_seconds` histogram.
- **Acceptance Criteria:**
  - All 14 alerting tests pass.
  - A duplicate alert within 5 min increments count instead of
    creating a new row.
  - A CRITICAL alert un-acked for 5 min escalates to the next
    on-call.
  - The `AlertsPanel` shows the active alerts with ack buttons.
- **Status:** IN PROGRESS.
