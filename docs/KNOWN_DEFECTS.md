# Known Defects — Baseline (2026-08-17)

Captured during M1 baseline (first sprint). Each entry is a **defect to fix or a
fabricated claim to remove**; do not delete entries without evidence of resolution.
Tracked against `docs/STRATEGIC_IMPROVEMENT_AND_IMPLEMENTATION_PLAN.md`.

Legend: [F] fabricated claim in code/API/UI · [B] broken behavior (no-op/dead path) · [T] test asserts fabricated output · [S] security gap

| ID | Class | Where | Defect | Resolution milestone |
|---|---|---|---|---|
| KD-01 | [F] | `api/server.py:1111-1157` `/api/system/health` | `status` hardcoded "HEALTHY"; `latency_ms` hardcoded 42.5; `services` list hardcoded UP | M2 (P0-TRU-01) |
| KD-02 | [F] | `api/server.py:524-566` `/api/history/ohlcv` | OHLCV bars generated as seeded random walk, presented as real history | M2 (M4: real pipeline) |
| KD-03 | [F] | `core/fundamental_ingest.py` | `sources_indexed` = 105,048 constant vs 10 real seed items; "100,000+ sources" claims in API docstrings | M2 (P0-TRU-02), M4 |
| KD-04 | [F] | `backtesting/engine.py` | Backtests run on Monte-Carlo archetype paths, not recorded history | M2 (label), M8 |
| KD-05 | [T] | `tests/test_institutional_suite.py` `test_10` | Asserts `sources_indexed > 100000` — pins the fabricated value | M2 |
| KD-06 | [T] | `tests/test_institutional_suite.py` `test_06` | Backtest assertion runs Monte-Carlo and asserts "sensible" output | M8 |
| KD-07 | [B] | `strategies/registry.py:118-120` | `_execute_cycle = pass` — 47/50 catalog strategies are no-op stubs yet reported as running | M6 |
| KD-08 | [B] | `core/ws_client.py:52` | `subscribe()` has zero callers — WS market feed dead | M4 (D5: retire) |
| KD-09 | [B] | `core/timescale_db.py:217,257,296,335` | Write paths swallow exceptions; 0 rows persisted across all 4 tables after 8 h+ operation | M3 |
| KD-10 | [B] | `ml/model.py:287` | `load_or_create` never loads `model.pkl`; `fit_initial` trains on 3,000 synthetic coin-flip samples; `n_online_updates = 0` | M10 (D6) |
| KD-11 | [B] | `ml/model_registry.py` | Two model versions simultaneously ACTIVE (v1.554.0, v1.595.0) | M10 |
| KD-12 | [B] | `risk/manager.py:56` | `WEEKLY_LOSS_STOP` defined, never enforced; no weekly PnL tracked | M2 (P0-GOV-01), M5 |
| KD-13 | [B] | `risk/manager.py` + `execution/*` | TP/SL are logged only, not enforced as order controls | M5 |
| KD-14 | [S] | `api/server.py:309-315` | CORS `allow_origins=["*"]` + `allow_credentials=True` | M2 (P0-SEC-01) |
| KD-15 | [S] | All API routes | No authentication on any endpoint; private key empty in `.env` | M2 (P0-SEC-01) |
| KD-16 | [B] | `watchdog.py` | Watchdog only pings `/api/health` (hardcoded OK) — no subsystem tripwires | M2 (P0-SAF-01) |
| KD-17 | [B] | `core/settlement.py` | Settlement marks every trade `paper=True`; YES-only paths | M7 (D4) |
| KD-18 | [B] | `webui/src/hooks/useBot.ts:137` | `paper_balance ?? 100` fake fallback in UI | M13 |
| KD-19 | [F] | `ml/drift_detector.py` | PSI 3.3538 "SIGNIFICANT_DRIFT" vs uniform baseline — not real feature drift | M10 |
| KD-20 | [B] | `config.py:41`, `docker-compose.yml` `bot-live` | Live profile exists with real-money path gated only by env flags | M2 hardening, M16 |
| KD-21 | [B] | `check_snapshot.py` (root) | Stray diagnostic script; root-level duplicates of `server.py`/`market_maker.py`/`signal_trader.py`/`drift_detector.py` existed on remote deploy dir | M4 |
| KD-22 | [T] | `tests/test_institutional_suite.py` `test_02` | Docstring says "$4.00 daily loss stop"; code asserts $2.00 behavior — stale doc | M2 |
| KD-23 | [B] | `core/data_store.py:259` | Equity curve uses `BANKROLL_BASELINE + daily_pnl` while `paper_balance` is the accounting balance — two equity definitions visible to analytics (`/api/analytics` uses `store.paper_balance`) | M5 |
| KD-24 | [B] | `main.py:93`, `api/server.py:164` | WS client started unconditionally though it subscribes to nothing | M4 |
| KD-25 | [B] | `core/timescale_db.py:371` | `fetch_training_samples` fabricates labels via `np.random.uniform` draw — unverifiable training rows | M3 |
| KD-26 | [F] | `core/timescale_db.py:379-414` | `get_stats` always reads the SQLite file even when Timescale is the active backend — misleading telemetry | M3 |
| KD-27 | [B] | `core/timescale_db.py:313`, `ml/model.py` | `record_feature_vector` has no caller — `ml_feature_store` never accumulates rows | M3 (D6) |
| KD-28 | [B] | `core/timescale_db.py:27` | SQLite path hardcoded to `/app/data/market_intelligence.db`; ignores `MARKET_DB_PATH` | M3 |
| KD-29 | [B] | `api/server.py:1192-1210` | `/api/database/records` hardcodes SQLite reads + swallows errors, even when Timescale active | M3 |
| KD-30 | [B] | persistence layer (all) | No reconciliation job or daily report artifact comparing engine writes vs storage rows | M3 (P0-DAT-03) |