"""Honest performance reporter — separates backtest, walk-forward, paper, and live metrics.

NEVER combines metrics across categories. Each category is reported independently
with its own confidence intervals and statistical significance assessment.

Categories
----------
1. **Backtest**       — historical replay performance (can be overfit).
2. **Walk-forward**   — out-of-sample walk-forward CV (more honest).
3. **Paper trading**  — real-time paper trading (honest, current).
4. **Live**           — real capital at risk (ground truth).

For each category the reporter derives:

* Win rate (with 95% confidence interval, Wilson score)
* Profit factor (gross profit / gross loss)
* Expectancy (mean per-trade P&L)
* Maximum drawdown
* Sharpe ratio (annualised, 252 trading days)
* Sortino ratio (annualised, downside-only denominator)
* Open exposure (sum of |size| × current price over still-open trades)
* Capital utilisation (avg invested capital / initial capital)
* Slippage and fees (avg bps + total fees)
* Number of trades
* Statistical significance (binomial p-value vs 50% win rate)

The reporter consumes a list of trade dicts whose schema is intentionally
forgiving — it accepts BOTH the ``backtesting.report`` shape
(``{pnl, timestamp, hold_time_hours, ...}``) AND the ``closed_positions``
row shape (``{pnl, timestamp, holding_seconds, shares, entry_price,
exit_price, ...}``) by reading either the explicit ``entry_time`` /
``exit_time`` / ``size`` / ``current_price`` keys OR falling back to
the ``closed_positions`` aliases. This lets the same reporter serve
backtest trades, paper-trading rows, walk-forward fold P&L series,
and live trade journals without per-category glue code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Statistical significance helper ─────────────────────────────────────────
# ``scipy.stats.binom_test`` was removed in scipy ≥ 1.12.0 (deprecated in
# 1.10.0). The modern replacement is ``scipy.stats.binomtest``, whose
# result object exposes the p-value via ``.pvalue``. We try the modern
# API first and fall back to the legacy one so the reporter works across
# scipy 1.6 → 1.14+ without a hard pin.
def _binomial_pvalue(n_wins: int, n: int, p: float = 0.5) -> float:
    """Two-sided binomial test p-value (vs ``p``).

    Returns ``1.0`` when ``n == 0`` so an empty trade list is never
    flagged as "statistically significant" by accident.
    """
    if n <= 0:
        return 1.0
    try:
        from scipy import stats  # local import — scipy is a transitive dep

        if hasattr(stats, "binomtest"):  # scipy ≥ 1.7
            return float(stats.binomtest(int(n_wins), int(n), p).pvalue)
        if hasattr(stats, "binom_test"):  # scipy < 1.12 (legacy)
            return float(stats.binom_test(int(n_wins), int(n), p))
    except Exception as e:  # pragma: no cover — defensive: scipy import / API drift
        logger.warning("[performance_reporter] binomial test failed: %s", e)
    # Final fallback: normal approximation to the binomial.
    # ``Z = (k - n*p) / sqrt(n*p*(1-p))`` → two-sided p-value.
    try:
        from scipy import stats as _stats

        z = (n_wins - n * p) / max(np.sqrt(n * p * (1 - p)), 1e-8)
        return float(2 * (1 - _stats.norm.cdf(abs(z))))
    except Exception:
        return 1.0


@dataclass
class PerformanceMetrics:
    """Immutable snapshot of one performance category's analytics.

    Every numeric field is a primitive ``float`` / ``int`` (never
    ``np.float64``) so ``to_dict`` is JSON-serialisable without a
    custom encoder — same convention as ``backtesting.report.BacktestReport``.
    """

    category: str  # "backtest", "walk_forward", "paper", "live"
    win_rate: float
    win_rate_ci_lower: float  # 95% confidence interval
    win_rate_ci_upper: float
    profit_factor: float
    expectancy: float  # Expected P&L per trade
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    open_exposure: float
    capital_utilization: float  # % of capital deployed
    avg_slippage_bps: float
    total_fees: float
    n_trades: int
    n_wins: int
    n_losses: int
    avg_win: float
    avg_loss: float
    avg_hold_time_hours: float
    p_value: float  # Statistical significance vs random (50% win rate)
    is_significant: bool  # p < 0.05 AND n >= 30
    period_start: float
    period_end: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-safe dict with human-readable formatting.

        Numeric fields are kept as primitives but pretty-printed via
        ``f-strings`` (e.g. ``"62.5%"`` for win_rate) so a dashboard
        can drop the values straight into a KPI tile without a
        per-field formatter. The raw numeric fields are still available
        on the dataclass instance for programmatic consumers.
        """
        return {
            "category": self.category,
            "win_rate": f"{self.win_rate * 100:.1f}%",
            "win_rate_ci_95": (
                f"[{self.win_rate_ci_lower * 100:.1f}%, "
                f"{self.win_rate_ci_upper * 100:.1f}%]"
            ),
            "profit_factor": f"{self.profit_factor:.2f}",
            "expectancy": f"${self.expectancy:.4f}",
            "max_drawdown": f"{self.max_drawdown * 100:.1f}%",
            "sharpe_ratio": f"{self.sharpe_ratio:.3f}",
            "sortino_ratio": f"{self.sortino_ratio:.3f}",
            "open_exposure": f"${self.open_exposure:.2f}",
            "capital_utilization": f"{self.capital_utilization * 100:.1f}%",
            "avg_slippage_bps": f"{self.avg_slippage_bps:.1f}",
            "total_fees": f"${self.total_fees:.2f}",
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "avg_win": f"${self.avg_win:.4f}",
            "avg_loss": f"${self.avg_loss:.4f}",
            "avg_hold_time_hours": f"{self.avg_hold_time_hours:.1f}h",
            "p_value": f"{self.p_value:.4f}",
            "is_statistically_significant": self.is_significant,
            "period_start": self.period_start,
            "period_end": self.period_end,
        }


# ── Trade-dict field accessors (tolerant of both backtest + closed_positions shapes) ──
def _trade_pnl(t: dict) -> float:
    return float(t.get("pnl", 0) or 0)


def _trade_size(t: dict) -> float:
    """Trade size in shares. Falls back to ``shares`` (closed_positions
    schema) when ``size`` is absent."""
    return float(t.get("size", t.get("shares", 0)) or 0)


def _trade_entry_time(t: dict) -> float:
    """Entry timestamp (epoch seconds). Falls back to
    ``timestamp - holding_seconds`` (closed_positions schema) when
    ``entry_time`` is absent."""
    if t.get("entry_time"):
        return float(t["entry_time"])
    ts = float(t.get("timestamp", 0) or 0)
    hold = float(t.get("holding_seconds", 0) or 0)
    return ts - hold


def _trade_exit_time(t: dict) -> float:
    """Exit timestamp. Falls back to ``timestamp`` (closed_positions
    schema — ``timestamp`` IS the close time)."""
    if t.get("exit_time"):
        return float(t["exit_time"])
    return float(t.get("timestamp", 0) or 0)


def _trade_entry_price(t: dict) -> float:
    return float(t.get("entry_price", 0) or 0)


def _trade_current_price(t: dict) -> float:
    """Current mark price. Falls back to exit_price (closed) or entry_price."""
    if t.get("current_price") is not None:
        return float(t.get("current_price") or 0)
    if t.get("exit_price") is not None:
        return float(t.get("exit_price") or 0)
    return _trade_entry_price(t)


def _trade_status(t: dict) -> str:
    """Trade status. Defaults to ``CLOSED`` (the only sensible default
    for closed_positions rows + backtest trades, which are always closed)."""
    return str(t.get("status", "CLOSED")).upper()


class PerformanceReporter:
    """Reports performance metrics separately for each category.

    The reporter is stateless — every public method takes its input data
    explicitly and returns a fresh ``PerformanceMetrics`` instance. The
    singleton ``performance_reporter`` exists purely as a convenience
    handle so callers don't have to thread an instance through every
    call site.
    """

    # ── Core metrics computation ──────────────────────────────────────────
    def compute_metrics(
        self,
        trades: list[dict],
        category: str,
        initial_capital: float = 100.0,
    ) -> PerformanceMetrics:
        """Compute performance metrics from a list of trades.

        Args:
            trades: list of trade dicts with ``pnl`` + optionally
                ``entry_time`` / ``exit_time`` / ``size`` / ``entry_price``
                / ``current_price`` / ``slippage_bps`` / ``fees`` /
                ``status``. Falls back to the ``closed_positions`` schema
                (``timestamp`` / ``holding_seconds`` / ``shares`` /
                ``exit_price``) when the explicit keys are absent.
            category: one of ``"backtest"``, ``"walk_forward"``,
                ``"paper"``, ``"live"``.
            initial_capital: starting capital for computing utilisation.

        Returns:
            A populated ``PerformanceMetrics`` snapshot. An empty ``trades``
            list yields a zeroed-out metrics object (``n_trades=0``,
            ``is_significant=False``) so the caller never has to handle
            ``None`` for a fresh deployment.
        """
        if not trades:
            return self._empty_metrics(category)

        pnls = [_trade_pnl(t) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        n = len(pnls)
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = n_wins / n if n > 0 else 0.0

        # ── 95% Wilson score confidence interval for the win rate ──────
        # Clopper-Pearson is more conservative; Wilson is the standard
        # choice for trading-system dashboards because it's bounded to
        # [0, 1] AND well-behaved at small n (unlike the naive normal
        # approximation which can produce CI bounds outside [0, 1] for
        # win rates near 0 or 1).
        z = 1.959963985  # z_{0.975} — 95% two-sided
        if n > 0:
            p = win_rate
            denom = 1.0 + z * z / n
            centre = (p + z * z / (2 * n)) / denom
            margin = (z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
            ci_lower = centre - margin
            ci_upper = centre + margin
        else:
            ci_lower = ci_upper = 0.0
        # Clamp to [0, 1] — Wilson can dip slightly negative at p ≈ 0.
        ci_lower = max(0.0, float(ci_lower))
        ci_upper = min(1.0, float(ci_upper))

        # ── Profit factor ─────────────────────────────────────────────────
        gross_profit = float(sum(wins)) if wins else 0.0
        gross_loss = float(abs(sum(losses))) if losses else 0.0
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            # All winners, no losers — saturate to a finite sentinel
            # (matches the ``backtesting.report`` convention) so JSON
            # serialisation never sees ``inf``.
            profit_factor = 999.0
        else:
            # No P&L at all (all breakeven) — profit factor of 0 is
            # more honest than 999 (which would suggest all-win).
            profit_factor = 0.0

        # ── Expectancy (mean per-trade P&L) ───────────────────────────────
        expectancy = float(np.mean(pnls)) if pnls else 0.0

        # ── Sharpe (annualised, 252 trading days) ────────────────────────
        # We treat each trade as one "period" — the per-trade Sharpe
        # annualised by sqrt(252) assumes ~1 trade/day. Walk-forward
        # folds with fewer trades will under-annualise, but the
        # cross-category comparison is still apples-to-apples.
        if len(pnls) > 1:
            returns = np.asarray(pnls, dtype=np.float64)
            std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
            sharpe = (
                float(np.mean(returns)) / (std + 1e-8) * np.sqrt(252)
                if std > 0
                else 0.0
            )
            downside = returns[returns < 0]
            downside_std = (
                float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
            )
            sortino = (
                float(np.mean(returns)) / (downside_std + 1e-8) * np.sqrt(252)
                if downside_std > 0
                else 0.0
            )
        else:
            sharpe = 0.0
            sortino = 0.0

        # ── Maximum drawdown (on the cumulative P&L equity curve) ────────
        equity = np.cumsum(pnls) + float(initial_capital)
        peak = np.maximum.accumulate(equity)
        # Guard against peak==0 (would happen if initial_capital==0 AND
        # every P&L is non-positive) — the division would produce inf.
        safe_peak = np.where(peak > 0, peak, 1.0)
        drawdowns = (peak - equity) / safe_peak
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
        # Clamp to [0, 1] — a non-positive starting capital can produce
        # drawdowns > 100% which would mislead the dashboard.
        max_dd = max(0.0, min(1.0, max_dd))

        # ── Capital utilisation ───────────────────────────────────────────
        # Mean invested capital per trade divided by initial capital.
        # ``size * entry_price`` is the notional at trade open; if the
        # trade dict carries neither ``size`` nor ``shares``, the
        # notional defaults to 0 (capital_utilization collapses to 0).
        invested_notional = sum(
            _trade_size(t) * _trade_entry_price(t) for t in trades
        )
        capital_util = (
            invested_notional / (float(initial_capital) * max(n, 1))
            if initial_capital > 0
            else 0.0
        )

        # ── Open exposure (mark-to-market of still-open trades) ───────────
        # Closed positions and backtest trades are by definition closed,
        # so this will be 0 unless the caller passes live trade dicts
        # whose ``status == "OPEN"``.
        open_exposure = float(
            sum(
                _trade_size(t) * _trade_current_price(t)
                for t in trades
                if _trade_status(t) == "OPEN"
            )
        )

        # ── Slippage + fees ───────────────────────────────────────────────
        slippages = [
            float(t["slippage_bps"])
            for t in trades
            if t.get("slippage_bps") is not None
        ]
        avg_slippage = float(np.mean(slippages)) if slippages else 0.0
        total_fees = float(sum(float(t.get("fees", 0) or 0) for t in trades))

        # ── Hold time (hours) ─────────────────────────────────────────────
        # Prefer the explicit ``hold_time_hours`` (backtest shape) when
        # present, else derive from entry/exit timestamps.
        hold_times: list[float] = []
        for t in trades:
            if t.get("hold_time_hours"):
                hold_times.append(float(t["hold_time_hours"]))
                continue
            entry = _trade_entry_time(t)
            exit_t = _trade_exit_time(t)
            if entry and exit_t and exit_t > entry:
                hold_times.append((exit_t - entry) / 3600.0)
        avg_hold = float(np.mean(hold_times)) if hold_times else 0.0

        # ── Statistical significance ──────────────────────────────────────
        # Two-sided binomial test against the null ``H0: p = 0.5`` (random
        # coin-flip). Significance requires BOTH ``p_value < 0.05`` AND
        # ``n >= 30`` — the latter guard prevents a 7-of-7 win streak
        # from being flagged as "significant" when the sample is too
        # small to draw any conclusion.
        p_value = _binomial_pvalue(n_wins, n, 0.5)
        is_significant = bool(p_value < 0.05 and n >= 30)

        # ── Period window ─────────────────────────────────────────────────
        # ``period_start`` = the earliest trade entry time (when the
        # trading period started); ``period_end`` = the latest trade
        # exit time (when the trading period closed). Conventionally a
        # trading period is measured from first entry to last exit —
        # using entry times for both would understate the period for
        # long-running positions (the last trade's entry time is
        # typically well before its exit time).
        entry_times = [
            _trade_entry_time(t)
            for t in trades
            if _trade_entry_time(t) > 0 or _trade_exit_time(t) > 0
        ]
        entry_times = [ts for ts in entry_times if ts > 0]
        exit_times = [
            _trade_exit_time(t)
            for t in trades
            if _trade_exit_time(t) > 0
        ]
        exit_times = [ts for ts in exit_times if ts > 0]
        period_start = float(min(entry_times)) if entry_times else 0.0
        period_end = float(max(exit_times)) if exit_times else 0.0

        return PerformanceMetrics(
            category=category,
            win_rate=float(win_rate),
            win_rate_ci_lower=ci_lower,
            win_rate_ci_upper=ci_upper,
            profit_factor=float(profit_factor),
            expectancy=float(expectancy),
            max_drawdown=float(max_dd),
            sharpe_ratio=float(sharpe),
            sortino_ratio=float(sortino),
            open_exposure=open_exposure,
            capital_utilization=float(capital_util),
            avg_slippage_bps=float(avg_slippage),
            total_fees=float(total_fees),
            n_trades=int(n),
            n_wins=int(n_wins),
            n_losses=int(n_losses),
            avg_win=float(np.mean(wins)) if wins else 0.0,
            avg_loss=float(np.mean(losses)) if losses else 0.0,
            avg_hold_time_hours=avg_hold,
            p_value=float(p_value),
            is_significant=is_significant,
            period_start=period_start,
            period_end=period_end,
        )

    def _empty_metrics(self, category: str) -> PerformanceMetrics:
        """Zeroed-out metrics object for an empty trade list."""
        return PerformanceMetrics(
            category=category,
            win_rate=0.0,
            win_rate_ci_lower=0.0,
            win_rate_ci_upper=0.0,
            profit_factor=0.0,
            expectancy=0.0,
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            open_exposure=0.0,
            capital_utilization=0.0,
            avg_slippage_bps=0.0,
            total_fees=0.0,
            n_trades=0,
            n_wins=0,
            n_losses=0,
            avg_win=0.0,
            avg_loss=0.0,
            avg_hold_time_hours=0.0,
            p_value=1.0,
            is_significant=False,
            period_start=0.0,
            period_end=0.0,
        )

    # ── Category-specific getters ────────────────────────────────────────────

    async def get_paper_trading_metrics(
        self, limit: int = 500
    ) -> PerformanceMetrics:
        """Get paper trading performance (real-time, honest).

        Pulls the ``limit`` most-recent closed positions from the
        ``core.closed_positions.closed_positions`` SQLite store and
        computes the full metrics suite against them. Because
        ``closed_positions.get_closed_positions`` is async (its
        ``@timed_query`` wrapper runs the SQLite read in a thread),
        this method is async too — callers must ``await`` it.

        The closed_positions schema maps cleanly to the trade-dict
        shape ``compute_metrics`` expects: ``pnl`` is signed,
        ``timestamp`` is the close time, ``holding_seconds`` lets us
        derive the entry time, and ``shares`` / ``entry_price`` /
        ``exit_price`` give us notional + capital utilisation.
        """
        from core.closed_positions import closed_positions

        positions: list[dict[str, Any]] = await closed_positions.get_closed_positions(
            limit=limit
        )
        return self.compute_metrics(positions, "paper")

    async def get_all_metrics(self) -> dict[str, Any]:
        """Get all performance categories reported SEPARATELY.

        Backtest, walk-forward, and live categories are reported as
        pointers to their dedicated endpoints rather than computed
        inline — each one has its own request shape (run_id for
        backtest, n_splits for walk-forward, etc.) and computing them
        speculatively would be wasteful. Paper-trading is the only
        category computed inline because it's the canonical "honest,
        current" view that the dashboard needs on every load.

        The ``disclaimer`` field is always present and reminds the
        reader that backtest performance does NOT guarantee future
        results — the cardinal sin this reporter exists to prevent.
        """
        paper_metrics = await self.get_paper_trading_metrics()
        return {
            "paper_trading": paper_metrics.to_dict(),
            "backtest": (
                "POST /api/backtest/report with a strategy_id + days "
                "to get backtest metrics"
            ),
            "walk_forward": (
                "POST /api/backtest/walk-forward with train_window / "
                "test_window / step to get walk-forward metrics"
            ),
            "live": "No live trading data (paper mode active)",
            "disclaimer": (
                "Metrics are reported SEPARATELY by category. Backtest "
                "performance does NOT guarantee future results. Only "
                "paper / live performance reflects actual system behaviour. "
                "Walk-forward CV is the most honest backtest-style metric "
                "because it evaluates out-of-sample; treat in-sample "
                "backtest metrics as upper-bound estimates."
            ),
        }


# ── Module-level singleton (mirrors ``closed_positions`` / ``decision_ledger``) ──
performance_reporter = PerformanceReporter()


__all__ = [
    "PerformanceMetrics",
    "PerformanceReporter",
    "performance_reporter",
]
