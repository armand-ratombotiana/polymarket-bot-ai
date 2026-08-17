# Baseline Metrics (Golden File)

Captured at end of Sprint 1 (M1–M2 containment), before M3 persistence work.
Serves as the regression baseline: any later change must be reported as a delta to this file.

| Metric | Baseline (Sprint 1 end) |
| --- | --- |
| DataStore rows (trades / positions / orders / audits) | 0 / 0 / 0 / 0 |
| Filled trades recorded | 0 |
| Audit events recorded | 0 (empty DB in fresh env) |
| Test count (pytest) | 40 passed (15 legacy + 25 containment) |
| Test suite runtime | ~10 s |
| Lint gate (`ruff check .`) | clean (E4/E7/E9/F/I, E501 ignored) |
| Live trading | DISABLED by default (`TRADING_MODE=paper`; live requires explicit flag + credentials) |
| Auth enforcement | Bearer token, fail-closed (503 when unconfigured) |
| Durable kill switch | `/app/data/kill_switch` (absent = no kill) |
| Tripwire auto-kill | Enabled by default (`tripwire_auto_kill: true`) |

## Fabricated / synthetic surfaces inventory (all labeled, none silent)

| Surface | Location | Status after Sprint 1 |
| --- | --- | --- |
| OHLCV candles | `/api/market/ohlcv` | labeled `synthetic: true`, `seeded_random_walk` |
| Backtest engine | `backtesting/engine.py`, `/api/backtest` | labeled `synthetic: true`, `monte_carlo_archetype` |
| ML training data | `ml/model.py`, health report | labeled `training_data_kind: synthetic_coinflip_seed` |
| ML features (momentum, rsi, volume_surge) | `ml/features.py` | labeled `synthetic: true` (existing) |
| News corpus (GDELT) | `core/fundamental_ingest.py`, `/api/analysis/news` | GDELT disabled (config-only); `is_seed` provenance on items; honest `sources_indexed` |
| Market catalog (seed) | Gamma market ingestion | real Gamma data via official API when reachable (no fabrication) |
| Latency in health | `/api/system/health` | `latency_ms: null` — never fabricated |

## Known-defect deltas closed this sprint (see docs/KNOWN_DEFECTS.md)

KD-01 (auth), KD-02 (permissive CORS), KD-03 (unauthenticated mutation),
KD-04 (GDELT fabrications), KD-05 (news counts), KD-06 (paper mode bypass of shadow gate —
now enforced in `risk_manager.check_order`), KD-07 (config-driven flags),
KD-12 (OHLCV fabrication), KD-14 (backtest fabrication), KD-15 (hardcoded health),
KD-16 (watchdog hardcoded status — superseded by `core/watchdog.py` tripwires),
KD-20 (kill switch not durable), KD-22 (test suite truthfulness).

## Mode & risk baseline

| Parameter | Value |
| --- | --- |
| `TRADING_MODE` | `paper` (default) |
| Bankroll baseline | $100.00 |
| Daily loss limit | $1.50 |
| Weekly loss limit | $5.00 (new: enforced in `risk_manager.check_order` + watchdog wr03) |
| Exposure ceiling | $200.00 |
| Max position group | 20% of ceiling |

## Data truth guardrails (enforced by tests)

- No API response may report health/mode/status values it cannot compute.
- Any synthetic data must carry `synthetic: true` + `synthetic_kind`.
- `mode` change without explicit env is impossible (`trading_mode` validated at config load).
