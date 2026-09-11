"""tests/test_advanced_backtest.py — Unit tests for
``backtesting/advanced.py`` (W13-8 walk-forward + Monte Carlo).

Scope: pure-Python verification of the walk-forward ML backtest and
Monte-Carlo bootstrap modules added by W13-8. Both functions are
synchronous and self-contained (numpy + sklearn only — no DB / network
/ FastAPI), so the tests can run hermetically without fixtures.

Twelve tests, grouped by concern:

  Walk-forward analysis:
    1. ``test_walk_forward_basic``         — happy path: synthetic
                                             logistic-regression-flavored
                                             dataset yields > 1 window,
                                             aggregate AUC > 0.5, Brier < 0.25.
    2. ``test_walk_forward_window_count``  — number of windows matches
                                             the expected sliding-window
                                             arithmetic for known params.
    3. ``test_walk_forward_empty_data``    — too-small dataset returns
                                             ``n_windows=0`` with an error
                                             marker rather than crashing.
    4. ``test_walk_forward_train_failure_skipped`` — a ``model_factory``
                                             that raises ``fit`` errors
                                             causes the bad window to be
                                             skipped, not the whole run.
    5. ``test_walk_forward_sorts_by_timestamp`` — passing shuffled-order
                                             data with monotonic timestamps
                                             still produces the same window
                                             partition as pre-sorted data.

  Equity simulator:
    6. ``test_simulate_equity_all_wins``   — every prediction correct →
                                             equity monotonically grows,
                                             drawdown = 0, Sortino/Sharpe ≥ 0.
    7. ``test_simulate_equity_all_losses`` — every prediction wrong →
                                             equity monotonically shrinks,
                                             max_drawdown > 0.
    8. ``test_simulate_equity_metrics_finite`` — Sharpe/Sortino/Calmar/MDD
                                             are all finite floats.

  Monte Carlo:
    9. ``test_monte_carlo_basic``          — 1000 sims on a known
                                             positive-mean return series
                                             yields expected_return > 0
                                             and best_case > worst_case.
   10. ``test_monte_carlo_empty``          — empty ``trade_returns``
                                             returns the zeroed-out
                                             ``MonteCarloResult``.
   11. ``test_monte_carlo_probability_of_ruin`` — a guaranteed-losing
                                             return series (-1.0) produces
                                             ``probability_of_ruin == 1.0``;
                                             a guaranteed-winning series
                                             (+0.10) produces 0.0.
   12. ``test_monte_carlo_percentiles``    — percentiles are correctly
                                             ordered (p5 ≤ p25 ≤ p50 ≤ p75
                                             ≤ p95) and ``expected_return``
                                             is the mean of ``final_returns``.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_engine.py``) so
# a sibling test file invoked directly
# (``python -m pytest tests/test_advanced_backtest.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_adv_backtest_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``core.*``, ``ml.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from backtesting.advanced import (  # noqa: E402
    MonteCarloResult,
    WalkForwardResult,
    _simulate_equity,
    monte_carlo_simulation,
    walk_forward_analysis,
)


# ── Shared model_factory ────────────────────────────────────────────────────
# A small RandomForest so the test suite stays fast (< 1 s per test). The
# factory pattern (returning a fresh unfitted model per call) is what the
# walk-forward routine expects — every window gets its own model so no
# leakage can occur between folds.
def _rf_factory():
    from sklearn.ensemble import RandomForestClassifier

    return RandomForestClassifier(
        n_estimators=20,
        max_depth=6,
        random_state=42,
        n_jobs=-1,
    )


# ── Synthetic dataset fixture ───────────────────────────────────────────────
# Logistic-regression-flavored binary classification problem so the model
# can actually learn signal (AUC > 0.5). ``module`` scope: deterministic
# given the seed, computing it once and reusing across the read-only
# assertions below is fast and reproducible.
@pytest.fixture(scope="module")
def synthetic_dataset() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.RandomState(42)
    n = 2000
    f = 5
    X = rng.uniform(-1.0, 1.0, (n, f)).astype(np.float32)
    ts = np.arange(n, dtype=np.float64)
    log_odds = (
        2.0 * X[:, 0]
        + 1.0 * X[:, 1]
        - 0.5 * X[:, 2]
        + rng.normal(0.0, 0.5, n)
    )
    probs = 1.0 / (1.0 + np.exp(-log_odds))
    y = (rng.uniform(0.0, 1.0, n) < probs).astype(np.int32)
    return X, y, ts


# ── (1) Walk-forward happy path ────────────────────────────────────────────
def test_walk_forward_basic(synthetic_dataset) -> None:
    """``walk_forward_analysis`` on a learnable dataset produces > 1 window
    with mean AUC > 0.5 (better than random) and mean Brier < 0.25
    (better than the 0.25 baseline of always predicting 0.5)."""
    X, y, ts = synthetic_dataset
    result = walk_forward_analysis(
        X, y, ts, _rf_factory,
        train_window=500, test_window=100, step=100,
    )

    # Type contract.
    assert isinstance(result, WalkForwardResult), (
        f"expected WalkForwardResult, got {type(result).__name__}"
    )
    assert isinstance(result.windows, list)
    assert len(result.windows) > 1, (
        f"expected > 1 window, got {len(result.windows)}"
    )

    # Every window carries the documented per-window keys.
    for w in result.windows:
        assert {"window", "train_start", "train_end", "test_start",
                "test_end", "n_train", "n_test", "auc", "brier",
                "mean_prediction", "actual_positive_rate"}.issubset(w.keys())

    # Aggregate metrics — signal > random.
    assert result.aggregate["n_windows"] == len(result.windows)
    assert 0.0 <= result.aggregate["mean_auc"] <= 1.0
    assert result.aggregate["mean_auc"] > 0.5, (
        f"mean_auc {result.aggregate['mean_auc']:.4f} should beat random (0.5)"
    )
    assert 0.0 <= result.aggregate["mean_brier"] <= 0.25, (
        f"mean_brier {result.aggregate['mean_brier']:.4f} should beat baseline (0.25)"
    )

    # Equity curve is non-empty and starts at $1.0.
    assert isinstance(result.equity_curve, list)
    assert len(result.equity_curve) > 0
    assert math.isclose(result.equity_curve[0], 1.0, abs_tol=1e-6), (
        f"equity_curve[0] should be 1.0, got {result.equity_curve[0]}"
    )

    # Risk metrics present and finite.
    for m in (result.max_drawdown, result.sharpe_ratio,
              result.sortino_ratio, result.calmar_ratio):
        assert isinstance(m, float)
        assert math.isfinite(m), f"non-finite risk metric: {m!r}"


# ── (2) Walk-forward window-count arithmetic ───────────────────────────────
def test_walk_forward_window_count(synthetic_dataset) -> None:
    """For ``N=2000, train=500, test=100, step=100`` the windows should
    number exactly::

        floor((N - train - test) / step) + 1
        = floor((2000 - 500 - 100) / 100) + 1
        = floor(1400 / 100) + 1
        = 15

    Asserting the exact count guards against off-by-one regressions in
    the sliding-window loop (e.g. ``<=`` vs ``<`` in the while condition).
    """
    X, y, ts = synthetic_dataset
    n = len(X)
    train, test, step = 500, 100, 100
    result = walk_forward_analysis(
        X, y, ts, _rf_factory,
        train_window=train, test_window=test, step=step,
    )
    expected = (n - train - test) // step + 1
    assert len(result.windows) == expected, (
        f"expected {expected} windows, got {len(result.windows)}"
    )


# ── (3) Walk-forward empty / too-small data ───────────────────────────────
def test_walk_forward_empty_data() -> None:
    """A dataset smaller than (train_window + test_window) yields zero
    windows and an aggregate error marker — NOT a crash."""
    rng = np.random.RandomState(0)
    X = rng.uniform(-1.0, 1.0, (50, 3)).astype(np.float32)
    y = rng.randint(0, 2, 50).astype(np.int32)
    ts = np.arange(50, dtype=np.float64)

    result = walk_forward_analysis(
        X, y, ts, _rf_factory,
        train_window=500, test_window=100, step=100,
    )
    assert len(result.windows) == 0
    assert result.aggregate["n_windows"] == 0
    assert "error" in result.aggregate, (
        f"aggregate should carry an error marker, got {result.aggregate}"
    )
    # Equity curve falls back to the trivial ``[1.0]`` baseline.
    assert result.equity_curve == [1.0]


# ── (4) Walk-forward train failure is skipped, not fatal ──────────────────
def test_walk_forward_train_failure_skipped(synthetic_dataset) -> None:
    """A ``model_factory`` whose ``fit`` raises should cause the bad
    window to be skipped (window_num still advances, ``start`` still
    steps forward), not abort the whole walk-forward run."""
    X, y, ts = synthetic_dataset

    # Factory that fails on the FIRST fit call, then succeeds thereafter.
    # Uses a closure-captured mutable to track invocations across windows.
    call_log = {"n": 0}

    def _flaky_factory():
        from sklearn.ensemble import RandomForestClassifier

        class _Flaky:
            __slots__ = ("_inner",)

            def __init__(self) -> None:
                self._inner = RandomForestClassifier(
                    n_estimators=5, max_depth=3, random_state=42,
                )

            def fit(self, X, y):
                call_log["n"] += 1
                if call_log["n"] == 1:
                    raise RuntimeError("simulated fit failure on window 0")
                self._inner.fit(X, y)
                return self

            def predict_proba(self, X):
                return self._inner.predict_proba(X)

        return _Flaky()

    result = walk_forward_analysis(
        X, y, ts, _flaky_factory,
        train_window=500, test_window=100, step=100,
    )
    # The first window was attempted (``call_log["n"]`` >= 1) and failed.
    assert call_log["n"] >= 1
    # Some windows succeeded (all but the first), so we still have results.
    # Expected windows = 15; the first is skipped → 14 survivors.
    assert len(result.windows) == 14, (
        f"expected 14 surviving windows (1 of 15 failed), got {len(result.windows)}"
    )
    # Window numbering is preserved (the skipped window's number does NOT
    # appear in the results).
    window_numbers = {w["window"] for w in result.windows}
    assert 0 not in window_numbers, (
        f"window 0 should have been skipped, got {sorted(window_numbers)[:3]}..."
    )


# ── (5) Walk-forward sorts by timestamp ────────────────────────────────────
def test_walk_forward_sorts_by_timestamp(synthetic_dataset) -> None:
    """Passing shuffled-order rows with monotonic timestamps must produce
    the SAME per-window results as passing pre-sorted rows — the routine
    re-sorts by ``timestamps`` internally. This guards against a future
    refactor that assumes the input is already sorted."""
    X, y, ts = synthetic_dataset

    # Shuffle the rows but keep the timestamps attached to the same rows
    # (so walk-forward's ``argsort(timestamps)`` should restore the original
    # order).
    rng = np.random.RandomState(123)
    perm = rng.permutation(len(X))
    X_shuf = X[perm]
    y_shuf = y[perm]
    ts_shuf = ts[perm]

    result_sorted = walk_forward_analysis(
        X, y, ts, _rf_factory,
        train_window=500, test_window=100, step=100,
    )
    result_shuffled = walk_forward_analysis(
        X_shuf, y_shuf, ts_shuf, _rf_factory,
        train_window=500, test_window=100, step=100,
    )

    # Same number of windows, same per-window AUC (because the data is
    # identical after re-sorting).
    assert len(result_sorted.windows) == len(result_shuffled.windows)
    aucs_sorted = [w["auc"] for w in result_sorted.windows]
    aucs_shuffled = [w["auc"] for w in result_shuffled.windows]
    np.testing.assert_allclose(
        aucs_shuffled, aucs_sorted, rtol=1e-6,
        err_msg="walk-forward should re-sort by timestamp internally",
    )


# ── (6) Equity simulator — all wins ───────────────────────────────────────
def test_simulate_equity_all_wins() -> None:
    """Every prediction correct → equity monotonically grows from $1.0,
    max_drawdown == 0.0, and Sharpe/Sortino are non-negative."""
    preds = np.array([0.7, 0.8, 0.6, 0.9, 0.55, 0.65], dtype=np.float64)
    actuals = np.array([1, 1, 1, 1, 1, 1], dtype=np.int32)

    equity, metrics = _simulate_equity(preds, actuals)

    # Equity starts at $1.0 and grows by +1 per win.
    assert math.isclose(equity[0], 1.0, abs_tol=1e-9)
    for i in range(1, len(equity)):
        assert equity[i] > equity[i - 1], (
            f"equity should be monotonic on all-wins, got {equity}"
        )
    # Final equity = 1.0 + N_wins = 1 + 6 = 7.0.
    assert math.isclose(equity[-1], 7.0, abs_tol=1e-6), (
        f"final equity should be 7.0, got {equity[-1]}"
    )

    # No drawdown when equity only ever goes up.
    assert metrics["max_drawdown"] == 0.0, (
        f"max_drawdown should be 0.0 on all-wins, got {metrics['max_drawdown']}"
    )
    # Sharpe / Sortino should be positive (mean return > 0 with zero downside).
    assert metrics["sharpe"] >= 0.0
    # Sortino divides by downside std; with no downside returns, the
    # routine returns 0.0 (per spec) rather than +inf.
    assert metrics["sortino"] >= 0.0


# ── (7) Equity simulator — all losses ─────────────────────────────────────
def test_simulate_equity_all_losses() -> None:
    """Every prediction wrong → equity monotonically shrinks from $1.0,
    max_drawdown > 0."""
    preds = np.array([0.7, 0.8, 0.6, 0.9], dtype=np.float64)
    actuals = np.array([0, 0, 0, 0], dtype=np.int32)

    equity, metrics = _simulate_equity(preds, actuals)

    # Equity starts at $1.0 and shrinks by -1 per loss.
    assert math.isclose(equity[0], 1.0, abs_tol=1e-9)
    for i in range(1, len(equity)):
        assert equity[i] < equity[i - 1], (
            f"equity should shrink on all-losses, got {equity}"
        )
    # Final equity = 1.0 - N_losses = 1 - 4 = -3.0.
    assert math.isclose(equity[-1], -3.0, abs_tol=1e-6), (
        f"final equity should be -3.0, got {equity[-1]}"
    )
    # Max drawdown > 0 — equity went from $1.0 down to -$3.0.
    assert metrics["max_drawdown"] > 0.0


# ── (8) Equity simulator — finite metrics ─────────────────────────────────
def test_simulate_equity_metrics_finite() -> None:
    """All four risk metrics must be finite floats for any non-empty
    prediction/actual pair (no NaN / Inf leaking into the API response)."""
    rng = np.random.RandomState(7)
    preds = rng.uniform(0.0, 1.0, 50)
    actuals = rng.randint(0, 2, 50).astype(np.int32)

    _, metrics = _simulate_equity(preds, actuals)

    for k in ("max_drawdown", "sharpe", "sortino", "calmar"):
        assert k in metrics, f"missing metric {k!r}"
        v = metrics[k]
        assert isinstance(v, float), (
            f"metric {k!r} must be float, got {type(v).__name__}"
        )
        assert math.isfinite(v), f"metric {k!r} is non-finite: {v!r}"


# ── (9) Monte Carlo happy path ────────────────────────────────────────────
def test_monte_carlo_basic() -> None:
    """1000 sims on a known positive-mean return series yields
    ``expected_return > 0``, ``best_case > worst_case``, and
    ``probability_of_ruin < 1.0``."""
    np.random.seed(0)
    # Mean return per trade = +1% → compounded growth.
    returns = np.array(
        [0.05, -0.03, 0.04, -0.02, 0.06, -0.01, 0.03, -0.04, 0.05, -0.02],
        dtype=np.float64,
    )

    result = monte_carlo_simulation(
        returns, n_simulations=1000, initial_capital=100.0, ruin_threshold=0.5,
    )

    assert isinstance(result, MonteCarloResult)
    assert result.n_simulations == 1000
    assert len(result.final_returns) == 1000
    assert len(result.max_drawdowns) == 1000

    # Positive mean return → expected_return > 0.
    assert result.expected_return > 0.0, (
        f"expected_return should be > 0, got {result.expected_return}"
    )
    # Best-case strictly better than worst-case.
    assert result.best_case > result.worst_case, (
        f"best_case {result.best_case} should exceed worst_case {result.worst_case}"
    )
    # Probability of ruin on a positive-mean, low-variance series should be
    # well below 1.0 (and, given the small downside here, plausibly 0).
    assert 0.0 <= result.probability_of_ruin < 1.0

    # Max drawdowns are bounded in [0, 1].
    for dd in result.max_drawdowns:
        assert 0.0 <= dd <= 1.0 + 1e-9, (
            f"max_drawdown out of [0, 1]: {dd}"
        )


# ── (10) Monte Carlo empty data ────────────────────────────────────────────
def test_monte_carlo_empty() -> None:
    """Empty ``trade_returns`` returns a zeroed-out ``MonteCarloResult``
    with ``n_simulations=0`` and an empty percentile dict — NOT a crash."""
    result = monte_carlo_simulation(
        np.array([], dtype=np.float64),
        n_simulations=100, initial_capital=100.0, ruin_threshold=0.5,
    )

    assert result.n_simulations == 0
    assert result.final_returns == []
    assert result.max_drawdowns == []
    assert result.percentiles == {}
    assert result.probability_of_ruin == 0.0
    assert result.expected_return == 0.0
    assert result.worst_case == 0.0
    assert result.best_case == 0.0


# ── (11) Monte Carlo probability-of-ruin ──────────────────────────────────
def test_monte_carlo_probability_of_ruin() -> None:
    """A guaranteed-losing return series (every trade returns -1.0) →
    final_value hits 0 after the first trade → ruin →
    ``probability_of_ruin == 1.0``.

    A guaranteed-winning series (+0.10 per trade, never crossing below
    initial_capital) → ``probability_of_ruin == 0.0``.

    Both with ``ruin_threshold=0.5`` (lose 50% = ruin)."""
    np.random.seed(0)

    # Guaranteed total loss per trade. ``equity[i+1] = equity[i] * 0`` →
    # the first trade wipes the account to zero, and every subsequent
    # trade keeps it at zero. Final_value (0) < initial_capital * 0.5.
    ruinous = np.array([-1.0, -1.0, -1.0, -1.0, -1.0], dtype=np.float64)
    result_ruin = monte_carlo_simulation(
        ruinous, n_simulations=50, initial_capital=100.0, ruin_threshold=0.5,
    )
    assert result_ruin.probability_of_ruin == 1.0, (
        f"probability_of_ruin should be 1.0 on guaranteed-loss series, "
        f"got {result_ruin.probability_of_ruin}"
    )

    # Guaranteed +10% per trade — equity monotonically grows, never crosses
    # below initial_capital * 0.5.
    winning = np.array([0.10, 0.10, 0.10, 0.10, 0.10], dtype=np.float64)
    result_win = monte_carlo_simulation(
        winning, n_simulations=50, initial_capital=100.0, ruin_threshold=0.5,
    )
    assert result_win.probability_of_ruin == 0.0, (
        f"probability_of_ruin should be 0.0 on guaranteed-win series, "
        f"got {result_win.probability_of_ruin}"
    )
    # Every final value is exactly (1.10)**5 = 1.61051 → expected_return
    # matches it.
    expected_final_return = (1.10 ** 5) - 1.0
    assert math.isclose(
        result_win.expected_return, expected_final_return, abs_tol=1e-9,
    ), (
        f"expected_return should equal (1.10)**5 - 1 = {expected_final_return}, "
        f"got {result_win.expected_return}"
    )


# ── (12) Monte Carlo percentile ordering ───────────────────────────────────
def test_monte_carlo_percentiles() -> None:
    """Percentiles must be monotonically ordered:
    ``p5 ≤ p25 ≤ p50 ≤ p75 ≤ p95``. And ``expected_return`` must equal the
    mean of the per-simulation ``final_returns`` array."""
    np.random.seed(42)
    returns = np.array(
        [0.05, -0.02, 0.03, -0.04, 0.06, 0.01, -0.03, 0.04, -0.01, 0.02],
        dtype=np.float64,
    )
    result = monte_carlo_simulation(
        returns, n_simulations=500, initial_capital=100.0, ruin_threshold=0.5,
    )

    p = result.percentiles
    assert set(p.keys()) == {"p5", "p25", "p50", "p75", "p95"}, (
        f"unexpected percentile keys: {sorted(p.keys())}"
    )
    assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"], (
        f"percentiles not monotonically ordered: {p}"
    )
    # Median should be approximately equal to the deterministic compounded
    # return of the underlying series (no randomness in the *underlying*
    # mean — only in the bootstrap resampling).
    deterministic_compound = np.prod(1.0 + returns) - 1.0
    assert abs(p["p50"] - deterministic_compound) < 0.10, (
        f"p50 {p['p50']:.4f} should be near deterministic compound "
        f"{deterministic_compound:.4f} (±0.10)"
    )

    # expected_return must equal mean(final_returns) — guards against a
    # future bug that computes it from the underlying series instead.
    final_arr = np.array(result.final_returns)
    assert math.isclose(
        result.expected_return, float(np.mean(final_arr)), abs_tol=1e-9,
    ), (
        f"expected_return {result.expected_return} should equal "
        f"mean(final_returns) {float(np.mean(final_arr))}"
    )

    # worst_case / best_case must equal min / max of final_returns.
    assert math.isclose(result.worst_case, float(np.min(final_arr)), abs_tol=1e-9)
    assert math.isclose(result.best_case, float(np.max(final_arr)), abs_tol=1e-9)
