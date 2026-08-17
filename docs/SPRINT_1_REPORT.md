# Sprint 1 Report — M1/M2 Containment & Governance

Date: 2026-08-17 · Scope: plan §29 (first sprint) · Baseline: `docs/BASELINE_METRICS.md`

## Exit criteria evidence

| Sprint item (plan §29) | Status | Evidence |
| --- | --- | --- |
| 1. Decision register D1–D8 | ✅ | `docs/DECISION_REGISTER.md` — all OPEN, defaults recorded, no fabricated decisions |
| 2. Git baseline | ✅ | repo initialized, baseline commit `Baseline commit: pre-first-sprint state`; `.gitattributes` (lf eol) |
| 3. Test harness + known defects | ✅ | `.venv` (py3.14) + pytest + ruff; `pytest.ini`; `docs/KNOWN_DEFECTS.md` KD-01…KD-24 |
| 4. Baseline golden file | ✅ | `docs/BASELINE_METRICS.md` |
| 5. P0-SEC-01 auth + CORS | ✅ | fail-closed bearer auth, 503 when unconfigured, CORS lockdown, WS token; webui token input |
| 6. P0-SAF-01 kill switch + tripwires | ✅ | `core/safety.py` durable switch, `core/watchdog.py` wr01–wr07, auto-kill wiring |
| 7. P0-TRU-01/02 honest surfaces | ✅ | health recomputed; OHLCV/backtest/ML/news labeled synthetic or honest zeros |
| 8. P0-GOV-01 mode flag | ✅ | canonical `TRADING_MODE`; live double-gate; mode audit; weekly-loss stop |
| 9. M2 exit review | ✅ | this report §Quality gates |
| 10. Sprint report | ✅ | this document |

## Quality gates (plan §19)

- **G-M1 (no new Critical/High)**: 40/40 tests pass (15 legacy + 25 containment); `ruff check .` clean
  (E4/E7/E9/F/I, E501 ignored); no Critical/High known defects against changed surfaces.
- **G-M2 (containment surfaces honest)**: no fabricated value returned without `synthetic: true`;
  health `latency_ms` is `null` when unmeasured; GDELT reports `connected: False`, 0 sources;
  news items carry `is_seed` provenance; backtest/OHLCV labeled.
- **G-AUTH**: every non-public endpoint (incl. WS) requires the token; public whitelist is only
  `/api/health`, `/docs`, `/redoc`, `/openapi.json`; 503 (not open) when token unconfigured.
- **G-KILL**: kill switch survives restart (durable file + reason), `check_order` blocks all sides
  in shadow mode and on durable kill; auto-kill active via watchdog on CRITICAL tripwires.
- **G-MODE**: `TRADING_MODE` validated (paper|shadow|live); live requires `LIVE_TRADING_ENABLED`
  + credentials at startup (double gate) and shadow is impossible without env change.

## Known-defect delta

Closed this sprint: KD-01, KD-02, KD-03, KD-04, KD-05, KD-06, KD-07, KD-12, KD-14, KD-15,
KD-16 (partial — legacy root `watchdog.py` still pings health; retained as liveness probe),
KD-20, KD-22.
Deferred to M3+: KD-08, KD-09, KD-10, KD-11, KD-13, KD-17, KD-18, KD-19, KD-21, KD-23, KD-24
(all persistence/data-pipeline/risk-accounting items, per plan phasing).

## Metrics (baseline captured)

- Tests: 15 → 40 (runtime ~10 s)
- Ruff findings on full tree: 0 (from 117 pre-existing after 425 auto-fixes)
- New/modified backend modules: config, api/server, risk/manager, core/{safety, watchdog,
  data_store, portfolio, settlement, fundamental_ingest}, backtesting/engine, ml/drift_detector,
  paper/simulator, main
- webui: lib/api.ts, useBot.ts, Header.tsx + 17 components converted to `apiFetch`

## Risks / open items

1. webui not buildable locally (no `node_modules`; Docker build is the verification path).
2. Legacy root `watchdog.py` coexists with `core/watchdog.py` (KD-16 fully resolved in M2 hardening).
3. `.env` on deployment host must add `API_TOKEN`, `CORS_ORIGINS` before restart (fail-closed 503
   until then) — `.env.example` documents this.
4. `tests/test_institutional_suite.py` still contains network-annotated tests; CI environment
   will skip them (env `POLY_*` unset).

## Next sprint (M3 — persistence integrity, P0-DAT-02/03)

- Schema versioning + migration path for DataStore state file; idempotent fills.
- TimescaleDB as authoritative store for market/OHLCV/trades with backfill labels.
- Kill-switch + auth enforcement in webui build verification; remove legacy watchdog ping.
- Re-baseline metrics after persistence work (delta vs this golden file).
