"""Advanced backtesting: walk-forward analysis + Monte Carlo simulation.

Walk-forward: Split data into windows, train on window N, test on window N+1.
Monte Carlo: Randomly resample trade sequence to estimate return distribution.

These tools extend the existing ``backtesting/engine.py`` archetype simulator
(``run_realistic_backtest`` / ``BacktestEngine.run_backtest``) with two
**out-of-sample / distributional** analyses that the archetype simulator
cannot answer on its own:

* **Walk-forward analysis** trains a fresh ML model on each rolling window and
  evaluates AUC / Brier / equity-curve metrics on the immediately-following
  out-of-sample test window. This is the canonical guard against
  look-ahead bias — no future data ever leaks into a training fold because the
  walk-forward partition is strictly time-ordered (same contract as the
  80/20 time-ordered split in ``ml/model.py::fit_initial``).

* **Monte Carlo simulation** draws ``n_simulations`` bootstrap resamples of an
  observed trade-return series, computes the final-equity / max-drawdown
  distribution, and reports percentiles + probability-of-ruin. The point is
  not a single point estimate but a confidence interval: "how likely is this
  strategy to lose 50% of capital if its future trade distribution looks like
  the past?"

Both functions are pure-Python (numpy + sklearn) and synchronous, mirroring
the engine module's design — the API layer wraps each call in
``asyncio.to_thread`` to keep the event loop responsive (see the
``/api/backtest/walk-forward`` and ``/api/backtest/monte-carlo`` routes in
``api/server.py``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Walk-forward analysis ────────────────────────────────────────────────────


@dataclass
class WalkForwardResult:
    """Container for walk-forward analysis output.

    Attributes mirror the dict-shape returned by the API route, so the
    route handler can call ``dataclasses.asdict(result)`` and serialize
    directly. The ``equity_curve`` is the simulated cumulative P&L (starting
    at $1.0) across the concatenated out-of-sample test windows.
    """

    windows: list[dict]  # Per-window results
    aggregate: dict  # Aggregate metrics across windows
    equity_curve: list[float]
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float


def walk_forward_analysis(
    features: np.ndarray,
    labels: np.ndarray,
    timestamps: np.ndarray,
    model_factory,  # Callable that returns a fresh model
    train_window: int = 1000,
    test_window: int = 200,
    step: int = 200,
) -> WalkForwardResult:
    """Run walk-forward backtest.

    Walk-forward partitions the time-ordered dataset into a sequence of
    (train, test) window pairs: train on samples ``[start, start+train_window)``
    and test on the immediately-following ``test_window`` samples. The window
    then slides forward by ``step`` samples and the process repeats until the
    test window would exceed the data length.

    A fresh model is constructed (via ``model_factory()``) and fitted on
    each training fold — this guarantees no information leaks between
    windows and produces a true out-of-sample performance estimate.

    Args:
        features: (N, F) feature matrix
        labels: (N,) binary labels
        timestamps: (N,) timestamps for time-ordering
        model_factory: Function that returns a new unfitted model
        train_window: Number of samples to train on
        test_window: Number of samples to test on
        step: Step size between windows

    Returns:
        WalkForwardResult with per-window and aggregate metrics
    """
    # Sort by timestamp so the walk-forward partition is strictly
    # chronological (defends against a caller passing rows in arbitrary order).
    order = np.argsort(timestamps)
    features = features[order]
    labels = labels[order]

    n = len(features)
    windows: list[dict] = []
    all_predictions: list[float] = []
    all_actuals: list[float] = []

    start = 0
    window_num = 0

    while start + train_window + test_window <= n:
        train_end = start + train_window
        test_end = train_end + test_window

        X_train = features[start:train_end]
        y_train = labels[start:train_end]
        X_test = features[train_end:test_end]
        y_test = labels[train_end:test_end]

        # Train fresh model
        model = model_factory()
        try:
            model.fit(X_train, y_train)
        except Exception as e:
            logger.warning(f"Window {window_num} train failed: {e}")
            start += step
            window_num += 1
            continue

        # Predict on test set
        try:
            preds = model.predict_proba(X_test)
            if hasattr(preds, "shape") and len(preds.shape) > 1:
                preds = preds[:, 1]  # Take positive class
        except Exception:
            preds = np.full(len(X_test), 0.5)

        # Compute window metrics
        # Local imports so a missing sklearn.metrics doesn't break module import.
        from sklearn.metrics import brier_score_loss, roc_auc_score

        try:
            auc = roc_auc_score(y_test, preds)
        except Exception:
            # roc_auc_score raises ValueError when only one class is present
            # in y_test (e.g. all-YES or all-NO window). Treat as
            # uninformative (AUC = 0.5) rather than crashing the run.
            auc = 0.5
        try:
            brier = brier_score_loss(y_test, preds)
        except Exception:
            brier = 0.25

        window_result = {
            "window": window_num,
            "train_start": start,
            "train_end": train_end,
            "test_start": train_end,
            "test_end": test_end,
            "n_train": len(X_train),
            "n_test": len(X_test),
            "auc": float(auc),
            "brier": float(brier),
            "mean_prediction": float(np.mean(preds)),
            "actual_positive_rate": float(np.mean(y_test)),
        }
        windows.append(window_result)
        all_predictions.extend(preds.tolist())
        all_actuals.extend(y_test.tolist())

        start += step
        window_num += 1

    # Aggregate metrics
    if windows:
        aucs = [w["auc"] for w in windows]
        briers = [w["brier"] for w in windows]
        aggregate = {
            "n_windows": len(windows),
            "mean_auc": float(np.mean(aucs)),
            "std_auc": float(np.std(aucs)),
            "min_auc": float(np.min(aucs)),
            "max_auc": float(np.max(aucs)),
            "mean_brier": float(np.mean(briers)),
            "std_brier": float(np.std(briers)),
        }
    else:
        aggregate = {"n_windows": 0, "error": "No valid windows"}

    # Build equity curve (simulated P&L based on predictions)
    if all_predictions and all_actuals:
        equity_curve, metrics = _simulate_equity(
            np.array(all_predictions), np.array(all_actuals)
        )
    else:
        equity_curve, metrics = [1.0], {}

    return WalkForwardResult(
        windows=windows,
        aggregate=aggregate,
        equity_curve=equity_curve,
        max_drawdown=metrics.get("max_drawdown", 0.0),
        sharpe_ratio=metrics.get("sharpe", 0.0),
        sortino_ratio=metrics.get("sortino", 0.0),
        calmar_ratio=metrics.get("calmar", 0.0),
    )


def _simulate_equity(
    predictions: np.ndarray, actuals: np.ndarray
) -> tuple[list[float], dict]:
    """Simulate a simple equity curve from predictions.

    Bet $1 on each prediction. Win $1 if correct, lose $1 if wrong.
    Returns the per-step equity curve (starting at $1.0) plus a dict of
    standard risk metrics (max drawdown, Sharpe, Sortino, Calmar). The
    Sharpe / Sortino / Calmar ratios are annualised assuming 252 trading
    days per year (matches the convention in ``backtesting/engine.py``).
    """
    # Convert predictions to bets (YES if >0.5, NO if <0.5)
    bets = (predictions > 0.5).astype(int)
    wins = (bets == actuals).astype(int)
    pnl = 2 * wins - 1  # +1 for win, -1 for loss

    # Equity curve: prepend the $1.0 starting-capital baseline so
    # ``equity[0]`` is always the initial capital and ``equity[i]`` for
    # ``i >= 1`` is the equity AFTER trade ``i-1`` (cumulative P&L plus
    # the $1.0 seed). The bare ``cumsum(pnl) + 1.0`` would omit the
    # baseline and report ``equity[0] = 1.0 + pnl[0]`` — confusing because
    # the equity curve no longer reflects the starting capital the way
    # every other equity curve in this codebase (engine.py, paper/simulator)
    # does.
    equity = np.concatenate([[1.0], np.cumsum(pnl) + 1.0])
    equity_list = equity.tolist()

    # Compute metrics
    if len(equity) > 1:
        # Per-step returns. Equity can hit zero (or below) on a long losing
        # streak — guard against divide-by-zero by clipping the denominator.
        # The +1e-8 floor matches the convention used elsewhere in the
        # engine's risk-metric block.
        safe_denom = np.where(np.abs(equity[:-1]) < 1e-8, 1e-8, equity[:-1])
        returns = np.diff(equity) / safe_denom
    else:
        returns = np.array([0.0])

    max_drawdown = 0.0
    peak = equity[0]
    for val in equity:
        if val > peak:
            peak = val
        # Drawdown is only meaningful when the peak is positive; if the
        # equity ever went negative the drawdown ratio is undefined (clip).
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    sharpe = (
        float(np.mean(returns) / (np.std(returns) + 1e-8) * np.sqrt(252))
        if len(returns) > 0
        else 0.0
    )

    downside = returns[returns < 0]
    sortino = (
        float(np.mean(returns) / (np.std(downside) + 1e-8) * np.sqrt(252))
        if len(downside) > 0
        else 0.0
    )

    calmar = (
        float(np.mean(returns) * 252 / (max_drawdown + 1e-8))
        if max_drawdown > 0
        else 0.0
    )

    metrics = {
        "max_drawdown": float(max_drawdown),
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
    }
    return equity_list, metrics


# ── Monte Carlo simulation ──────────────────────────────────────────────────


@dataclass
class MonteCarloResult:
    """Container for Monte Carlo simulation output.

    ``final_returns`` is the per-simulation fractional return
    (``final_value / initial_capital - 1``); ``max_drawdowns`` is the
    per-simulation peak-to-trough drawdown fraction. ``percentiles`` is the
    pre-computed {p5, p25, p50, p75, p95} map over ``final_returns``.
    """

    n_simulations: int
    final_returns: list[float]
    max_drawdowns: list[float]
    percentiles: dict
    probability_of_ruin: float
    expected_return: float
    worst_case: float
    best_case: float


def monte_carlo_simulation(
    trade_returns: np.ndarray,
    n_simulations: int = 10000,
    initial_capital: float = 100.0,
    ruin_threshold: float = 0.5,  # Lose 50% = ruin
) -> MonteCarloResult:
    """Run Monte Carlo simulation by resampling trade returns.

    For each of ``n_simulations`` runs, the observed ``trade_returns``
    sequence is bootstrap-resampled (with replacement, same length as the
    input) and compounded into a fresh equity curve. The distribution of
    final-equity values and max drawdowns across all simulations is then
    summarised as percentiles + probability-of-ruin.

    Args:
        trade_returns: Array of per-trade returns (e.g., +0.05, -0.03)
        n_simulations: Number of simulations to run
        initial_capital: Starting capital
        ruin_threshold: Fraction of initial capital below which = ruin

    Returns:
        MonteCarloResult with distribution statistics
    """
    if len(trade_returns) == 0:
        return MonteCarloResult(
            n_simulations=0,
            final_returns=[],
            max_drawdowns=[],
            percentiles={},
            probability_of_ruin=0.0,
            expected_return=0.0,
            worst_case=0.0,
            best_case=0.0,
        )

    n_trades = len(trade_returns)
    final_values: list[float] = []
    max_drawdowns: list[float] = []
    ruins = 0

    for _ in range(n_simulations):
        # Resample with replacement
        sampled = np.random.choice(trade_returns, size=n_trades, replace=True)

        # Build equity curve
        equity = np.ones(n_trades + 1) * initial_capital
        for i, ret in enumerate(sampled):
            equity[i + 1] = equity[i] * (1 + ret)

        final_value = equity[-1]
        final_values.append(final_value / initial_capital - 1)

        # Max drawdown
        peak = np.maximum.accumulate(equity)
        # Guard against division by zero — peak starts at initial_capital > 0
        # and is monotonically non-decreasing, so this is always positive
        # in practice, but the guard keeps the code defensive against any
        # future change that could let equity go negative.
        drawdowns = np.where(peak > 0, (peak - equity) / peak, 0.0)
        max_dd = float(np.max(drawdowns))
        max_drawdowns.append(max_dd)

        # Check ruin
        if final_value < initial_capital * ruin_threshold:
            ruins += 1

    final_arr = np.array(final_values)

    return MonteCarloResult(
        n_simulations=n_simulations,
        final_returns=final_values,
        max_drawdowns=max_drawdowns,
        percentiles={
            "p5": float(np.percentile(final_arr, 5)),
            "p25": float(np.percentile(final_arr, 25)),
            "p50": float(np.percentile(final_arr, 50)),
            "p75": float(np.percentile(final_arr, 75)),
            "p95": float(np.percentile(final_arr, 95)),
        },
        probability_of_ruin=ruins / n_simulations,
        expected_return=float(np.mean(final_arr)),
        worst_case=float(np.min(final_arr)),
        best_case=float(np.max(final_arr)),
    )
