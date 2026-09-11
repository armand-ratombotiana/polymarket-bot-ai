"""tests/test_performance_reporter.py — W24-5 honest performance reporter unit tests.

Scope: pure-Python verification of ``core/performance_reporter.py`` — the
module that reports backtest / walk-forward / paper / live performance
metrics SEPARATELY (never combined) with per-category 95% confidence
intervals + binomial-test p-values vs the 50% coin-flip null.

The reporter is fully self-contained (numpy + scipy.stats only — no DB /
network / FastAPI), so the metrics-computation tests run hermetically
without fixtures. The two API-route tests at the bottom drive the
production ``api.server.app`` through ``TestClient`` so the HTTP request
→ middleware → route → response cycle is exercised end-to-end.

Eighteen tests, grouped by concern:

  compute_metrics — happy path:
    1. ``test_compute_metrics_winning_trades`` — 3 winners, 1 loser →
       win_rate=75%, profit_factor > 1, positive expectancy + Sharpe.
    2. ``test_compute_metrics_losing_trades`` — 1 winner, 3 losers →
       win_rate=25%, profit_factor < 1, negative expectancy.
    3. ``test_compute_metrics_mixed_trades_hand_calc`` — hand-computed
       expected values for win_rate / profit_factor / expectancy /
       avg_win / avg_loss.
    4. ``test_compute_metrics_empty_returns_zeroed_metrics`` — empty
       trade list → ``_empty_metrics`` (n_trades=0, is_significant=False).
    5. ``test_closed_positions_schema_compat`` — the closed_positions
       row shape (``timestamp`` / ``holding_seconds`` / ``shares`` /
       ``exit_price``) maps to the right entry_time / exit_time / size
       / current_price.

  confidence interval:
    6. ``test_wilson_ci_contains_win_rate`` — win rate is always inside
       the 95% Wilson-score CI.
    7. ``test_wilson_ci_clamped_to_unit_interval`` — CI lower ≥ 0,
       upper ≤ 1 (Wilson can dip slightly negative at p ≈ 0).
    8. ``test_ci_widens_with_smaller_n`` — n=10 produces a wider CI
       than n=200 for the same win rate.

  statistical significance:
    9. ``test_significance_requires_30_trades_minimum`` — 7 of 7 wins
       is p < 0.05 but n < 30 → ``is_significant=False`` (the
       30-trade minimum guard prevents a small-sample fluke).
   10. ``test_significant_when_p_below_0_05_and_n_large_enough`` — 40
       of 50 wins is significant (p < 0.05 AND n ≥ 30).
   11. ``test_p_value_against_scipy_binomtest`` — the reporter's p-value
       matches ``scipy.stats.binomtest(k, n, 0.5).pvalue`` to 1e-9.
   12. ``test_p_value_one_for_empty_trades`` — no trades → p_value=1.0.

  profit factor:
   13. ``test_profit_factor_no_losses_returns_999_sentinel`` — all
       winning trades → profit_factor=999.0 (avoids ``inf`` in JSON).
   14. ``test_profit_factor_no_pnl_returns_0`` — all breakeven trades
       → profit_factor=0.0 (more honest than 999).

  Sharpe / Sortino:
   15. ``test_sharpe_positive_for_profitable_strategy`` — Sharpe > 0
       when mean P&L > 0; Sortino ≥ Sharpe when downside variance <
       total variance.
   16. ``test_sortino_zero_when_no_downside_variance`` — all losses
       equal → downside_std = 0 → Sortino = 0 (avoiding div-by-zero).

  max drawdown:
   17. ``test_max_drawdown_matches_hand_calc`` — a hand-computed
       equity-curve drawdown scenario yields the expected max DD.

  API routes (drive the production FastAPI app end-to-end):
   18. ``test_api_performance_report_returns_categories_separately`` —
       ``GET /api/performance/report`` returns 200 + a JSON payload
       whose top-level keys include ``paper_trading``,
       ``backtest``, ``walk_forward``, ``live``, ``disclaimer``.
   19. ``test_api_performance_paper_returns_metrics_dict`` —
       ``GET /api/performance/paper`` returns 200 + the
       ``PerformanceMetrics.to_dict`` shape (win_rate / profit_factor /
       sharpe_ratio / sortino_ratio / n_trades / p_value / ...).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_report.py``) so
# a sibling test file invoked directly
# (``python -m pytest tests/test_performance_reporter.py``) boots hermetic
# to ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_performance_reporter_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``api.*``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from core.performance_reporter import (  # noqa: E402
    PerformanceMetrics,
    PerformanceReporter,
    _binomial_pvalue,
    performance_reporter,
)

# NOTE on the asyncio mark: this module mixes sync unit tests
# (``compute_metrics`` pure-Python tests, no I/O) with async tests
# (``get_paper_trading_metrics`` / ``get_all_metrics`` / API routes
# that await the closed_positions SQLite fetch). We do NOT use the
# module-level ``pytestmark = pytest.mark.asyncio`` idiom (which would
# mark every test as async and trigger PytestWarnings on the sync
# ones); instead each ``async def test_...`` carries its own
# ``@pytest.mark.asyncio`` decorator below.


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — synthetic trade lists
# ═══════════════════════════════════════════════════════════════════════════


def _winners(n: int, *, pnl: float = 10.0, base_ts: float = 1_700_000_000.0) -> list[dict]:
    """``n`` winning trades at ``pnl`` each, 1h hold, 100 shares @ $0.50."""
    return [
        {
            "pnl": pnl,
            "entry_time": base_ts + i * 3600,
            "exit_time": base_ts + i * 3600 + 3600,
            "size": 100.0,
            "entry_price": 0.50,
            "current_price": 0.60,
            "slippage_bps": 3.0,
            "fees": 0.01,
            "status": "CLOSED",
        }
        for i in range(n)
    ]


def _losers(n: int, *, pnl: float = -8.0, base_ts: float = 1_700_000_000.0) -> list[dict]:
    """``n`` losing trades at ``pnl`` each (negative), 1h hold, 100 shares @ $0.50."""
    return [
        {
            "pnl": pnl,
            "entry_time": base_ts + i * 3600,
            "exit_time": base_ts + i * 3600 + 3600,
            "size": 100.0,
            "entry_price": 0.50,
            "current_price": 0.42,
            "slippage_bps": 5.0,
            "fees": 0.01,
            "status": "CLOSED",
        }
        for i in range(n)
    ]


def _mixed_trades(
    *, n_wins: int = 12, n_losses: int = 8, win_pnl: float = 10.0,
    loss_pnl: float = -8.0, base_ts: float = 1_700_000_000.0,
) -> list[dict]:
    """Deterministic alternating win/loss list — 12 winners + 8 losers by
    default (matches the ``test_backtest_report._synthetic_result``
    fixture's winning_rate=0.6 pattern)."""
    return _winners(n_wins, pnl=win_pnl, base_ts=base_ts) + _losers(
        n_losses, pnl=loss_pnl, base_ts=base_ts + n_wins * 3600
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1–5: compute_metrics — happy path + edge cases
# ═══════════════════════════════════════════════════════════════════════════


def test_compute_metrics_winning_trades() -> None:
    """3 winners + 1 loser → win_rate=75%, profit_factor > 1, positive
    expectancy + Sharpe."""
    trades = _winners(3, pnl=10.0) + _losers(1, pnl=-8.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert isinstance(m, PerformanceMetrics)
    assert m.category == "paper"
    assert m.n_trades == 4
    assert m.n_wins == 3
    assert m.n_losses == 1
    assert math.isclose(m.win_rate, 0.75, abs_tol=1e-9)
    # profit_factor = (3 * 10) / 8 = 3.75
    assert math.isclose(m.profit_factor, 30.0 / 8.0, abs_tol=1e-6)
    # expectancy = (30 - 8) / 4 = 5.5
    assert math.isclose(m.expectancy, 5.5, abs_tol=1e-6)
    # Sharpe must be positive on a profitable strategy.
    assert m.sharpe_ratio > 0
    # 4 trades < 30 → not statistically significant regardless of p.
    assert m.is_significant is False
    # Period window — first entry_time + last exit_time.
    assert m.period_start == trades[0]["entry_time"]
    # Last trade in the list is the loser; its exit_time is the latest
    # exit_time across the 4 trades.
    assert m.period_end == max(t["exit_time"] for t in trades)


def test_compute_metrics_losing_trades() -> None:
    """1 winner + 3 losers → win_rate=25%, profit_factor < 1, negative
    expectancy."""
    trades = _winners(1, pnl=8.0) + _losers(3, pnl=-10.0)
    m = performance_reporter.compute_metrics(trades, "backtest")

    assert m.n_trades == 4
    assert m.n_wins == 1
    assert m.n_losses == 3
    assert math.isclose(m.win_rate, 0.25, abs_tol=1e-9)
    # profit_factor = 8 / 30 = 0.2666...
    assert math.isclose(m.profit_factor, 8.0 / 30.0, abs_tol=1e-6)
    assert m.profit_factor < 1.0
    # expectancy = (8 - 30) / 4 = -5.5
    assert math.isclose(m.expectancy, -5.5, abs_tol=1e-6)
    # Sharpe must be negative on a losing strategy.
    assert m.sharpe_ratio < 0


def test_compute_metrics_mixed_trades_hand_calc() -> None:
    """Hand-computed expected values for a 12-winner / 8-loser fixture."""
    trades = _mixed_trades(n_wins=12, n_losses=8, win_pnl=10.0, loss_pnl=-8.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    # Win/loss counts + win rate.
    assert m.n_trades == 20
    assert m.n_wins == 12
    assert m.n_losses == 8
    assert math.isclose(m.win_rate, 12 / 20, abs_tol=1e-9)
    # Profit factor = (12 * 10) / (8 * 8) = 120 / 64 = 1.875
    assert math.isclose(m.profit_factor, 120.0 / 64.0, abs_tol=1e-6)
    # Expectancy = (120 - 64) / 20 = 56 / 20 = 2.8
    assert math.isclose(m.expectancy, 2.8, abs_tol=1e-4)
    # avg_win + avg_loss are hand-computed.
    assert math.isclose(m.avg_win, 10.0, abs_tol=1e-6)
    assert math.isclose(m.avg_loss, -8.0, abs_tol=1e-6)
    # Avg hold = 1h (every trade is 3600s).
    assert math.isclose(m.avg_hold_time_hours, 1.0, abs_tol=1e-6)
    # Avg slippage = mean of 12*3bps + 8*5bps = (36 + 40)/20 = 3.8 bps.
    assert math.isclose(m.avg_slippage_bps, 3.8, abs_tol=1e-6)
    # Total fees = 20 * $0.01 = $0.20.
    assert math.isclose(m.total_fees, 0.20, abs_tol=1e-6)


def test_compute_metrics_empty_returns_zeroed_metrics() -> None:
    """An empty trade list yields ``_empty_metrics`` (n_trades=0,
    is_significant=False, p_value=1.0)."""
    m = performance_reporter.compute_metrics([], "walk_forward")

    assert isinstance(m, PerformanceMetrics)
    assert m.category == "walk_forward"
    assert m.n_trades == 0
    assert m.n_wins == 0
    assert m.n_losses == 0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.expectancy == 0.0
    assert m.p_value == 1.0
    assert m.is_significant is False


def test_closed_positions_schema_compat() -> None:
    """The closed_positions row shape (``timestamp`` close time,
    ``holding_seconds``, ``shares``, ``exit_price``) maps cleanly to the
    reporter's trade-dict shape — the field-accessor fallbacks pick up
    the right entry_time / exit_time / size / current_price."""
    # Row shape mirrors ``closed_positions.get_closed_positions`` output.
    trades = [
        {
            "timestamp": 1_700_000_000.0,  # close time
            "holding_seconds": 3600.0,
            "shares": 100.0,
            "entry_price": 0.50,
            "exit_price": 0.60,
            "pnl": 10.0,
            "strategy": "ml_sig_v1",
        },
        {
            "timestamp": 1_700_001_000.0,
            "holding_seconds": 7200.0,
            "shares": 80.0,
            "entry_price": 0.55,
            "exit_price": 0.42,
            "pnl": -8.0,
            "strategy": "ml_sig_v1",
        },
    ]
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_trades == 2
    assert m.n_wins == 1
    assert m.n_losses == 1
    # Win rate = 1/2.
    assert math.isclose(m.win_rate, 0.5, abs_tol=1e-9)
    # Period start = min(entry_time) = min(ts - holding_seconds)
    #   trade 1: 1_700_000_000 - 3600 = 1_699_996_400
    #   trade 2: 1_700_001_000 - 7200 = 1_699_993_800
    #   → min = 1_699_993_800
    assert m.period_start == 1_700_001_000.0 - 7200.0
    # Period end = max(exit_time) = max(timestamp)
    #   trade 1: timestamp = 1_700_000_000
    #   trade 2: timestamp = 1_700_001_000
    #   → max = 1_700_001_000
    assert m.period_end == 1_700_001_000.0
    # Avg hold = (1h + 2h) / 2 = 1.5h.
    assert math.isclose(m.avg_hold_time_hours, 1.5, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════
# 6–8: Wilson-score confidence interval
# ═══════════════════════════════════════════════════════════════════════════


def test_wilson_ci_contains_win_rate() -> None:
    """The point-estimate win rate must always fall inside its 95%
    Wilson-score CI (a basic correctness property — Wilson intervals
    are centred on a Wilson-adjusted point but the original proportion
    still lies inside the interval for n ≥ 1)."""
    # 35 of 50 wins = 70% win rate, large enough for a non-trivial CI.
    trades = _winners(35, pnl=5.0) + _losers(15, pnl=-3.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_trades == 50
    assert math.isclose(m.win_rate, 0.7, abs_tol=1e-9)
    assert m.win_rate_ci_lower <= m.win_rate <= m.win_rate_ci_upper, (
        f"win_rate {m.win_rate} not inside CI "
        f"[{m.win_rate_ci_lower}, {m.win_rate_ci_upper}]"
    )
    # Sanity bounds.
    assert 0.0 <= m.win_rate_ci_lower <= 1.0
    assert 0.0 <= m.win_rate_ci_upper <= 1.0


def test_wilson_ci_clamped_to_unit_interval() -> None:
    """Wilson CIs can dip slightly negative at p ≈ 0 or slightly above
    1 at p ≈ 1 — the reporter must clamp to [0, 1] so the dashboard
    never renders "−3.2%" as a CI lower bound."""
    # All losers → win_rate = 0; raw Wilson lower bound would be 0
    # exactly, but the clamp guards against floating-point noise.
    trades = _losers(20, pnl=-5.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.win_rate == 0.0
    assert m.win_rate_ci_lower == 0.0
    assert m.win_rate_ci_upper >= 0.0
    assert m.win_rate_ci_upper <= 1.0

    # All winners → win_rate = 1; raw Wilson upper bound would be 1.
    trades = _winners(20, pnl=5.0)
    m = performance_reporter.compute_metrics(trades, "paper")
    assert m.win_rate == 1.0
    assert m.win_rate_ci_lower >= 0.0
    assert m.win_rate_ci_upper <= 1.0


def test_ci_widens_with_smaller_n() -> None:
    """For the same win rate, the 95% CI must be wider at n=10 than at
    n=200 — small samples carry more uncertainty."""
    # Both fixtures: 60% win rate. n=10 → 6 wins / 4 losses.
    small = _winners(6, pnl=5.0) + _losers(4, pnl=-3.0)
    # n=200 → 120 wins / 80 losses.
    large = _winners(120, pnl=5.0) + _losers(80, pnl=-3.0)

    m_small = performance_reporter.compute_metrics(small, "paper")
    m_large = performance_reporter.compute_metrics(large, "paper")

    assert math.isclose(m_small.win_rate, 0.6, abs_tol=1e-9)
    assert math.isclose(m_large.win_rate, 0.6, abs_tol=1e-9)

    small_width = m_small.win_rate_ci_upper - m_small.win_rate_ci_lower
    large_width = m_large.win_rate_ci_upper - m_large.win_rate_ci_lower
    assert small_width > large_width, (
        f"CI at n=10 ({small_width:.4f}) should be wider than CI at "
        f"n=200 ({large_width:.4f}) for the same 60% win rate"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 9–12: Statistical significance
# ═══════════════════════════════════════════════════════════════════════════


def test_significance_requires_30_trades_minimum() -> None:
    """A 7-of-7 win streak is highly significant by p-value but n < 30
    — the 30-trade minimum guard must mark it as ``is_significant=False``
    so the dashboard doesn't flag a small-sample fluke as "edge"."""
    trades = _winners(7, pnl=5.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_trades == 7
    assert m.n_wins == 7
    assert m.win_rate == 1.0
    # 7 of 7 is binomially significant vs p=0.5 (p ≈ 0.0156), BUT the
    # 30-trade minimum guard kicks in.
    assert m.p_value < 0.05, (
        f"7 of 7 wins should produce p < 0.05 (got {m.p_value}) — the "
        f"significance guard is the 30-trade minimum, not the p-value"
    )
    assert m.is_significant is False


def test_significant_when_p_below_0_05_and_n_large_enough() -> None:
    """40 of 50 wins: p < 0.05 AND n ≥ 30 → ``is_significant=True``."""
    trades = _winners(40, pnl=5.0) + _losers(10, pnl=-3.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_trades == 50
    assert m.n_wins == 40
    assert m.win_rate == 0.8
    assert m.p_value < 0.05, f"expected p < 0.05, got {m.p_value}"
    assert m.is_significant is True


def test_p_value_against_scipy_binomtest() -> None:
    """The reporter's p-value must match ``scipy.stats.binomtest`` to
    1e-9 (the reporter is a thin wrapper around the scipy API)."""
    from scipy import stats as _scipy_stats

    # 35 wins out of 50 — p_value vs the 50% coin-flip null.
    n_wins, n = 35, 50
    expected_p = float(_scipy_stats.binomtest(n_wins, n, 0.5).pvalue)
    actual_p = _binomial_pvalue(n_wins, n, 0.5)
    assert math.isclose(actual_p, expected_p, abs_tol=1e-9), (
        f"reporter p-value {actual_p} != scipy binomtest p-value {expected_p}"
    )


def test_p_value_one_for_empty_trades() -> None:
    """No trades → p_value=1.0 (we can't reject H0 with no evidence)."""
    assert _binomial_pvalue(0, 0, 0.5) == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 13–14: Profit factor
# ═══════════════════════════════════════════════════════════════════════════


def test_profit_factor_no_losses_returns_999_sentinel() -> None:
    """All winners, no losers → profit_factor = 999.0 (a finite sentinel
    that avoids ``inf`` in JSON serialisation — matches the
    ``backtesting.report`` convention)."""
    trades = _winners(5, pnl=10.0)
    m = performance_reporter.compute_metrics(trades, "backtest")

    assert m.n_trades == 5
    assert m.n_losses == 0
    assert m.profit_factor == 999.0


def test_profit_factor_no_pnl_returns_0() -> None:
    """All breakeven trades (pnl=0) → profit_factor = 0.0. This is more
    honest than 999 — a strategy with no P&L at all hasn't demonstrated
    any "edge", so reporting a saturating 999 would be misleading."""
    trades = [
        {
            "pnl": 0.0,
            "entry_time": 1_700_000_000.0 + i * 3600,
            "exit_time": 1_700_000_000.0 + i * 3600 + 3600,
            "size": 100.0,
            "entry_price": 0.50,
            "current_price": 0.50,
        }
        for i in range(5)
    ]
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_trades == 5
    assert m.n_wins == 0
    assert m.n_losses == 0
    assert m.profit_factor == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 15–16: Sharpe / Sortino
# ═══════════════════════════════════════════════════════════════════════════


def test_sharpe_positive_for_profitable_strategy() -> None:
    """Sharpe > 0 when mean P&L > 0. Sortino ≥ Sharpe's denominator
    when downside variance is a strict subset of total variance (the
    Sortino denominator is ≤ the Sharpe denominator, so the ratio
    Sortino/Sharpe ≥ 1 when both are positive)."""
    # 30 trades with varying wins + losses so downside_std > 0
    # (deterministic losses all equal would zero the downside std).
    rng = np.random.RandomState(42)
    trades = []
    base_ts = 1_700_000_000.0
    for i in range(20):
        # Winners: $5-$15
        trades.append(
            {
                "pnl": float(rng.uniform(5, 15)),
                "entry_time": base_ts + i * 3600,
                "exit_time": base_ts + i * 3600 + 3600,
                "size": 100.0,
                "entry_price": 0.5,
                "current_price": 0.6,
            }
        )
    for i in range(10):
        # Losers: -$3 to -$12 (varying so downside_std > 0)
        trades.append(
            {
                "pnl": float(rng.uniform(-12, -3)),
                "entry_time": base_ts + (20 + i) * 3600,
                "exit_time": base_ts + (20 + i) * 3600 + 3600,
                "size": 100.0,
                "entry_price": 0.5,
                "current_price": 0.45,
            }
        )
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.sharpe_ratio > 0, (
        f"profitable strategy should have positive Sharpe, got {m.sharpe_ratio}"
    )
    assert m.sortino_ratio > 0, (
        f"profitable strategy with downside variance should have positive "
        f"Sortino, got {m.sortino_ratio}"
    )
    # Sortino uses a smaller denominator (downside-only) than Sharpe (total
    # std), so for the same numerator Sortino ≥ Sharpe when both are
    # positive (this holds when returns are symmetric-ish — which the
    # 20-winner / 10-loser mixed fixture is).
    assert m.sortino_ratio >= m.sharpe_ratio, (
        f"Sortino ({m.sortino_ratio}) should be ≥ Sharpe ({m.sharpe_ratio}) "
        f"on a profitable strategy with asymmetric downside"
    )


def test_sortino_zero_when_no_downside_variance() -> None:
    """When all losses are equal (downside_std = 0), Sortino collapses to
    0 (the reporter avoids division-by-zero). The contract is: 0, NOT
    ``inf`` — a strategy that lost exactly $5 every loss hasn't
    demonstrated a meaningful downside profile."""
    # 10 winners + 5 identical losers (pnl = -5.0 exactly).
    trades = _winners(10, pnl=5.0) + _losers(5, pnl=-5.0)
    m = performance_reporter.compute_metrics(trades, "paper")

    assert m.n_losses == 5
    # Sharpe is still positive (mean > 0, std > 0 across wins+losses).
    assert m.sharpe_ratio > 0
    # Sortino collapses to 0 because downside_std == 0.
    assert m.sortino_ratio == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 17: Max drawdown
# ═══════════════════════════════════════════════════════════════════════════


def test_max_drawdown_matches_hand_calc() -> None:
    """A hand-computed equity-curve drawdown scenario yields the
    expected max DD. Trades (initial_capital=100):

        +5  → 105 (peak)
        +5  → 110 (peak)
        -10 → 100 (DD = 10/110 ≈ 9.09%)
        -10 →  90 (DD = 20/110 ≈ 18.18%)
        +5  →  95 (DD = 15/110 ≈ 13.64%)
        +5  → 100 (DD = 10/110 ≈ 9.09%)

    Max DD = 20/110 = 0.181818...
    """
    trades = [
        {"pnl": 5.0, "entry_time": 1_700_000_000.0, "exit_time": 1_700_000_000.0 + 3600},
        {"pnl": 5.0, "entry_time": 1_700_000_000.0 + 3600, "exit_time": 1_700_000_000.0 + 7200},
        {"pnl": -10.0, "entry_time": 1_700_000_000.0 + 7200, "exit_time": 1_700_000_000.0 + 10800},
        {"pnl": -10.0, "entry_time": 1_700_000_000.0 + 10800, "exit_time": 1_700_000_000.0 + 14400},
        {"pnl": 5.0, "entry_time": 1_700_000_000.0 + 14400, "exit_time": 1_700_000_000.0 + 18000},
        {"pnl": 5.0, "entry_time": 1_700_000_000.0 + 18000, "exit_time": 1_700_000_000.0 + 21600},
    ]
    m = performance_reporter.compute_metrics(trades, "backtest", initial_capital=100.0)

    # Max DD = 20 / 110 (peak) = 0.181818...
    assert math.isclose(m.max_drawdown, 20.0 / 110.0, abs_tol=1e-6), (
        f"max_drawdown {m.max_drawdown} != hand-computed {20.0/110.0:.6f}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 18–19: API routes (drive the production FastAPI app end-to-end)
# ═══════════════════════════════════════════════════════════════════════════


# Bearer token the conftest sets up (via ``API_TOKEN=test-token-conftest``).
_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_backtest_report.py``.

    The limiter is disabled in ``conftest.py`` so the ``READ_LIMIT``
    (120/min) decorator on the two new routes doesn't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture
def isolated_closed_positions(monkeypatch, tmp_path):
    """Patch ``core.closed_positions.closed_positions`` with a fresh
    ``ClosedPositionsStore`` whose SQLite file lives under ``tmp_path``.

    Without this fixture the closed_positions singleton would resolve
    to the conftest-redirected ``/tmp/pmbot_conftest_isolation/closed_positions.db``
    file, which is shared across every test in the session — any prior
    test that wrote a closed position would leak rows into the metrics
    computation and break the ``n_trades == 0`` assertion on an empty
    store. This fixture gives each test a hermetic DB.

    The patch targets the module-level ``closed_positions`` symbol so
    the reporter's local ``from core.closed_positions import
    closed_positions`` import resolves to the patched instance (Python
    re-resolves the attribute at import time, so a monkeypatch on the
    module attribute IS picked up by ``from ... import ...`` inside
    the function body).
    """
    from core.closed_positions import ClosedPositionsStore

    fresh = ClosedPositionsStore(tmp_path / "perf_reporter_isolated.db")
    monkeypatch.setattr("core.closed_positions.closed_positions", fresh)
    return fresh


@pytest.mark.asyncio
async def test_api_performance_report_returns_categories_separately(
    client, auth_headers, isolated_closed_positions
) -> None:
    """``GET /api/performance/report`` returns 200 + a JSON payload whose
    top-level keys include ``paper_trading`` (a populated metrics dict),
    ``backtest`` / ``walk_forward`` / ``live`` (pointers to the
    dedicated endpoints), and ``disclaimer`` (the honest-reporting
    reminder that backtest performance does NOT guarantee future
    results)."""
    response = client.get(
        "/api/performance/report",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/report returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    # Every category must be reported SEPARATELY — no merging allowed.
    for key in ("paper_trading", "backtest", "walk_forward", "live", "disclaimer"):
        assert key in data, f"missing category key {key!r}"
    # Paper-trading is computed inline (the canonical honest, current view).
    paper = data["paper_trading"]
    assert isinstance(paper, dict)
    for key in (
        "category",
        "win_rate",
        "win_rate_ci_95",
        "profit_factor",
        "expectancy",
        "max_drawdown",
        "sharpe_ratio",
        "sortino_ratio",
        "open_exposure",
        "capital_utilization",
        "avg_slippage_bps",
        "total_fees",
        "n_trades",
        "n_wins",
        "n_losses",
        "p_value",
        "is_statistically_significant",
    ):
        assert key in paper, f"paper_trading missing key {key!r}"
    # Backtest / walk-forward / live are pointers (strings), not dicts.
    assert isinstance(data["backtest"], str)
    assert isinstance(data["walk_forward"], str)
    assert isinstance(data["live"], str)
    # Disclaimer must call out the cardinal sin (backtest ≠ future).
    assert "NOT guarantee" in data["disclaimer"], (
        f"disclaimer should warn backtest ≠ future results; got: "
        f"{data['disclaimer']!r}"
    )
    # Paper-trading category label echoes back.
    assert paper["category"] == "paper"


@pytest.mark.asyncio
async def test_api_performance_paper_returns_metrics_dict(
    client, auth_headers, isolated_closed_positions
) -> None:
    """``GET /api/performance/paper`` returns 200 + the full
    ``PerformanceMetrics.to_dict`` shape — win_rate (with 95% CI) /
    profit_factor / expectancy / Sharpe / Sortino / max_drawdown /
    open_exposure / capital_utilization / avg_slippage_bps / total_fees
    / n_trades / p_value / is_statistically_significant / period_start /
    period_end. On an empty closed_positions store every numeric field
    is 0 (the graceful-degradation contract)."""
    response = client.get(
        "/api/performance/paper",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/paper returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert data["category"] == "paper"
    # Every PerformanceMetrics field is present in the dict.
    for key in (
        "category",
        "win_rate",
        "win_rate_ci_95",
        "profit_factor",
        "expectancy",
        "max_drawdown",
        "sharpe_ratio",
        "sortino_ratio",
        "open_exposure",
        "capital_utilization",
        "avg_slippage_bps",
        "total_fees",
        "n_trades",
        "n_wins",
        "n_losses",
        "avg_win",
        "avg_loss",
        "avg_hold_time_hours",
        "p_value",
        "is_statistically_significant",
        "period_start",
        "period_end",
    ):
        assert key in data, f"paper metrics missing key {key!r}"
    # On an empty store, n_trades=0 + is_statistically_significant=False.
    assert data["n_trades"] == 0
    assert data["is_statistically_significant"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Bonus — direct unit test against the singleton instance
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_paper_trading_metrics_async_returns_metrics(
    isolated_closed_positions,
) -> None:
    """``PerformanceReporter.get_paper_trading_metrics`` is async — it
    awaits ``closed_positions.get_closed_positions`` (whose SQLite read
    runs in a thread via ``asyncio.to_thread``) and returns a
    ``PerformanceMetrics`` instance. On an empty store it yields a
    zeroed-out metrics object (n_trades=0)."""
    metrics = await performance_reporter.get_paper_trading_metrics()
    assert isinstance(metrics, PerformanceMetrics)
    assert metrics.category == "paper"
    # The conftest redirects CLOSED_POSITIONS_DB_PATH to a tmp file so
    # the store is empty in this test process.
    assert metrics.n_trades == 0
    assert metrics.is_significant is False


@pytest.mark.asyncio
async def test_get_all_metrics_returns_dict_with_disclaimer(
    isolated_closed_positions,
) -> None:
    """``PerformanceReporter.get_all_metrics`` is async and returns the
    full category-separated dict — paper_trading is computed inline,
    backtest / walk_forward / live are pointers, disclaimer is a string."""
    result = await performance_reporter.get_all_metrics()
    assert isinstance(result, dict)
    assert "paper_trading" in result
    assert "backtest" in result
    assert "walk_forward" in result
    assert "live" in result
    assert "disclaimer" in result
    assert isinstance(result["paper_trading"], dict)
    assert isinstance(result["disclaimer"], str)
