"""Live portfolio risk metrics — VaR, CVaR, exposure, concentration.

W20-5 — Live VaR / CVaR for the current portfolio.

The God Mode assessment (W17-8 §7.5 risk R6) found that VaR and CVaR
were computed only on the *backtest* equity curve (see
``backtesting/advanced.py`` — the W16-5 advanced-metrics block). The
*live* portfolio had no quantified tail-risk number an operator could
read off the dashboard before clicking the next trade. This module
fills that gap by computing VaR / CVaR / exposure / concentration
directly against the in-memory ``DataStore.positions`` snapshot, with
two fallback modes:

  (1) **Historical price history available** — VaR / CVaR are
      computed from the empirical distribution of portfolio returns
      derived from the per-token price history the caller passes in.
      This is the textbook non-parametric (historical) VaR.

  (2) **No price history available** (the common case for a freshly
      opened prediction-market book where the order book mid has only
      just been observed) — VaR / CVaR fall back to a parametric
      normal approximation against an assumed 5 % daily volatility.
      This is intentionally conservative (5 % daily vol ≈ 79 % annualised
      — high, but prediction-market books *are* event-driven and a 5 %
      single-day move on a binary outcome is a routine pre-resolution
      swing). The fallback makes the metric immediately useful before
      enough history accumulates, and is clearly labelled in the
      response payload via ``var_method`` so the operator doesn't
      confuse the two.

This module is intentionally pure-Python with no I/O — it operates only
on the ``positions`` list passed by the caller (each position is a
plain dict with ``token_id`` / ``size`` / ``avg_price`` /
``current_price`` / ``side`` keys, the same shape
``core.stress_test`` already standardises on). The singleton
``live_risk_metrics`` (constructed at import time against the
conservative defaults documented below) is the production entry point;
the ``GET /api/portfolio/risk-metrics`` HTTP endpoint is registered
through the same ``register_routes(app)`` pattern used by every other
``core.*`` feature module (see the W16-3 portfolio-optimizer block and
the W17-4 stress-tester block in ``api/server.py`` for the sibling
implementations).

Relationship to ``core/stress_test.py``
---------------------------------------
The W17-4 ``PortfolioStressTester`` answers "if scenario X happens,
what's my P&L?" — a *scenario-driven* what-if that returns the loss
under each named scenario. This module answers the complementary
question: "given the price history I've actually observed, what's the
loss I should expect on a normal bad day (95 % VaR) and on a really
bad day (99 % CVaR)?" — a *distribution-driven* tail-risk number.
The two together give the operator both the named-scenario bound
(stress test) and the empirical-distribution bound (live VaR/CVaR).

Relationship to ``backtesting/advanced.py``
-------------------------------------------
The W16-5 advanced-metrics block computes VaR / CVaR on the
*backtest* equity curve — a synthetic time-series the backtest engine
produces by replaying historical data. This module computes the same
metrics on the *live* portfolio — the positions the operator actually
holds right now, against the price history the live book poller has
observed. The math (empirical percentile + tail mean) is identical;
the input source is different.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class PortfolioRiskMetrics:
    """Real-time risk metrics for the current portfolio.

    Attributes:
        total_exposure: gross notional of every open position
            (sum of ``abs(size * price)``).
        net_exposure: signed long-minus-short notional, returned as
            absolute value so it can be compared like-for-like with
            ``total_exposure``.
        gross_exposure: alias for ``total_exposure`` (kept for
            compatibility with the canonical gross/net exposure
            vocabulary in institutional risk reporting).
        position_count: number of open positions (non-zero size).
        largest_position_pct: largest single position as a fraction
            of ``total_exposure`` (0..1). A value > 0.5 means a single
            position dominates the book.
        var_95: 95 % one-day Value at Risk (USD). The loss that will
            not be exceeded on 95 % of days, given the empirical
            distribution of portfolio returns (or the parametric
            fallback when no price history is available).
        var_99: 99 % one-day Value at Risk (USD).
        cvar_95: 95 % one-day Conditional VaR / Expected Shortfall
            (USD). The average loss on the worst 5 % of days.
        cvar_99: 99 % one-day Conditional VaR (USD).
        concentration_ratio: Herfindahl-Hirschman Index of the
            position-value shares (0 = perfectly diversified,
            1 = single position dominates).
        var_method: ``"historical"`` when computed from the supplied
            price history, ``"parametric"`` when the 5 %-vol fallback
            was used, ``"none"`` when the portfolio is empty.
        computed_at: unix timestamp the metrics were computed at.
    """

    total_exposure: float
    net_exposure: float
    gross_exposure: float
    position_count: int
    largest_position_pct: float  # % of portfolio
    var_95: float  # 95% Value at Risk
    var_99: float  # 99% Value at Risk
    cvar_95: float  # Conditional VaR (Expected Shortfall) at 95%
    cvar_99: float  # CVaR at 99%
    concentration_ratio: float  # Herfindahl index (0=diversified, 1=concentrated)
    var_method: str = "none"
    computed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (mirrors ``dataclasses.asdict`` but kept
        explicit so callers don't depend on the dataclass internals)."""
        return asdict(self)


# ── Default parametric-VaR assumptions ────────────────────────────────────────
# Used only when the caller doesn't supply a price history. The constants
# come from the standard normal Z-table:
#   Z(0.95) = 1.65   →  95 % VaR multiplier
#   Z(0.99) = 2.33   →  99 % VaR multiplier
#   CVaR(0.95) ≈ 2.06 →  expected shortfall in the worst 5 % tail (normal)
#   CVaR(0.99) ≈ 2.66 →  expected shortfall in the worst 1 % tail (normal)
# Source: standard risk-management references (e.g. Jorion, *Value at
# Risk*, 3rd ed., ch. 5). The 5 % daily volatility assumption is
# deliberately conservative for prediction-market binary outcomes —
# routine pre-resolution swings on a 50/50 market easily hit 5 %.
_DEFAULT_DAILY_VOL = 0.05
_Z_95 = 1.65
_Z_99 = 2.33
_CVAR_95 = 2.06
_CVAR_99 = 2.66


# ── Tester ──────────────────────────────────────────────────────────────────


class LiveRiskMetrics:
    """Computes real-time risk metrics for the current portfolio.

    The class is stateless across calls — ``compute`` reads only its
    arguments and returns a fresh :class:`PortfolioRiskMetrics` dataclass.
    The singleton ``live_risk_metrics`` (constructed at module-import
    time) is the production entry point; tests and the
    ``GET /api/portfolio/risk-metrics`` endpoint both call its
    ``compute`` method.
    """

    def __init__(self, lookback_days: int = 30, daily_vol: float = _DEFAULT_DAILY_VOL):
        self.lookback_days = lookback_days
        # The parametric fallback daily volatility — overridable per-
        # instance so an operator who wants a tighter / looser fallback
        # can swap it without touching the module default.
        self.daily_vol = daily_vol

    # ── Public entry point ───────────────────────────────────────────────

    def compute(
        self,
        positions: list[dict],
        price_history: Optional[dict[str, list[float]]] = None,
    ) -> PortfolioRiskMetrics:
        """Compute risk metrics for the current portfolio.

        Args:
            positions: List of position dicts with ``token_id``,
                ``side``, ``size``, ``avg_price``, ``current_price``.
                The same shape ``core.stress_test`` already
                standardises on, so the live ``DataStore.positions``
                snapshot mapped through ``stress_test.
                _positions_from_live_store`` is directly usable here.
            price_history: Optional ``{token_id: [prices]}`` dict.
                When supplied, VaR / CVaR are computed from the
                empirical distribution of portfolio returns derived
                from this history (non-parametric historical VaR).
                When omitted, the parametric fallback (5 % daily vol,
                normal distribution) is used.

        Returns:
            A :class:`PortfolioRiskMetrics` dataclass with exposure,
            concentration, and VaR / CVaR fields populated. An empty
            portfolio returns a zeroed dataclass with
            ``var_method="none"``.
        """
        # ── Empty portfolio short-circuit ────────────────────────────────
        # An empty book has no exposure, no VaR, and no concentration.
        # Return a zeroed dataclass so the dashboard renders a clean
        # "no positions" state rather than NaNs.
        if not positions:
            return PortfolioRiskMetrics(
                total_exposure=0.0,
                net_exposure=0.0,
                gross_exposure=0.0,
                position_count=0,
                largest_position_pct=0.0,
                var_95=0.0,
                var_99=0.0,
                cvar_95=0.0,
                cvar_99=0.0,
                concentration_ratio=0.0,
                var_method="none",
                computed_at=time.time(),
            )

        # ── Exposures ───────────────────────────────────────────────────
        # Per-position marked-to-market value (absolute — used for
        # concentration ratio and largest-position %).
        position_values: list[float] = []
        for p in positions:
            size = float(p.get("size", 0) or 0)
            price = float(
                p.get("current_price")
                if p.get("current_price") is not None
                else (p.get("avg_price", 0) or 0)
            )
            value = abs(size * price)
            position_values.append(value)

        total_exposure = float(sum(position_values))
        gross_exposure = total_exposure  # alias — see dataclass docstring

        # Net exposure (longs − shorts). LONG contributes +size*price,
        # SHORT contributes −size*price. Returned as an absolute value
        # so it can be compared like-for-like with ``total_exposure``
        # (a perfectly-hedged book has ``net_exposure`` ≈ 0 and
        # ``total_exposure`` ≈ 2 × the per-leg notional).
        net = 0.0
        for p in positions:
            size = float(p.get("size", 0) or 0)
            price = float(
                p.get("current_price")
                if p.get("current_price") is not None
                else (p.get("avg_price", 0) or 0)
            )
            side = (p.get("side") or "LONG").upper()
            net += size * price * (1.0 if side == "LONG" else -1.0)
        net_exposure = abs(net)

        # ── Largest position ────────────────────────────────────────────
        largest = max(position_values) if position_values else 0.0
        largest_pct = largest / total_exposure if total_exposure > 0 else 0.0

        # ── Concentration ratio (Herfindahl-Hirschman Index) ────────────
        # HHI = Σ(share_i)^2  where share_i = value_i / total_exposure.
        # Range: 1/N (perfectly equal) → 1.0 (single position dominates).
        # An HHI > 0.25 is conventionally "concentrated".
        if total_exposure > 0:
            shares = [v / total_exposure for v in position_values]
            hhi = float(sum(s * s for s in shares))
        else:
            hhi = 0.0

        # ── VaR / CVaR ─────────────────────────────────────────────────
        var_95, var_99, cvar_95, cvar_99, var_method = self._compute_var_cvar(
            positions, price_history, total_exposure
        )

        return PortfolioRiskMetrics(
            total_exposure=total_exposure,
            net_exposure=net_exposure,
            gross_exposure=gross_exposure,
            position_count=len(positions),
            largest_position_pct=largest_pct,
            var_95=var_95,
            var_99=var_99,
            cvar_95=cvar_95,
            cvar_99=cvar_99,
            concentration_ratio=hhi,
            var_method=var_method,
            computed_at=time.time(),
        )

    # ── VaR / CVaR core ─────────────────────────────────────────────────

    def _compute_var_cvar(
        self,
        positions: list[dict],
        price_history: Optional[dict[str, list[float]]],
        total_exposure: float,
    ) -> tuple[float, float, float, float, str]:
        """Compute VaR and CVaR from position returns.

        Returns a 5-tuple ``(var_95, var_99, cvar_95, cvar_99, method)``
        where ``method`` is ``"historical"`` when the empirical
        distribution was used or ``"parametric"`` when the normal-
        approximation fallback was used.

        Historical VaR (when ``price_history`` is supplied and has
        enough data):

          1. For each day ``i`` in the price history, compute the
             portfolio's daily return as the size-weighted sum of
             per-token returns (LONG side contributes +return, SHORT
             side contributes −return), then normalise by the
             portfolio's current marked-to-market value.

          2. VaR_95 = |percentile(returns, 5)| × current_value
             VaR_99 = |percentile(returns, 1)| × current_value

             (The 5th / 1st percentile of the return distribution is
             the worst-5 %-of-days / worst-1 %-of-days return; the
             absolute value gives the loss magnitude.)

          3. CVaR_95 = mean of returns ≤ percentile(returns, 5),
             times the current value (the expected loss given that
             the loss exceeds VaR). Same for CVaR_99 at the 1 % tail.

        Parametric VaR (fallback):

          VaR_p = total_value × Z_p × daily_vol
          CVaR_p = total_value × CVaR_Z_p × daily_vol
        """
        # ── Parametric fallback ─────────────────────────────────────────
        # Used when no price history is supplied, OR when the supplied
        # history is too short to compute a meaningful percentile
        # (need at least 20 observations to get a non-trivial 5th
        # percentile — otherwise the tail has 0 or 1 elements and the
        # CVaR mean degenerates).
        if not price_history or total_exposure <= 0:
            return self._parametric_var_cvar(total_exposure)

        # ── Build the daily portfolio-return series ─────────────────────
        portfolio_returns = self._portfolio_return_series(positions, price_history)

        # Need at least 20 returns so the 5th-percentile tail has ≥ 1
        # element and the 1st-percentile tail is computable (otherwise
        # ``np.percentile`` returns the minimum but the CVaR tail slice
        # is empty and we'd silently fall back to VaR).
        if len(portfolio_returns) < 20:
            logger.debug(
                "live_risk_metrics: only %d historical returns — falling back to parametric VaR",
                len(portfolio_returns),
            )
            return self._parametric_var_cvar(total_exposure)

        returns = np.array(portfolio_returns, dtype=float)

        # ── Historical VaR ──────────────────────────────────────────────
        # ``np.percentile(returns, 5)`` returns the return at the 5th
        # percentile (a negative number — the loss on a bad day). The
        # absolute value times ``total_exposure`` gives the USD loss.
        p5 = float(np.percentile(returns, 5))
        p1 = float(np.percentile(returns, 1))
        var_95 = abs(p5) * total_exposure
        var_99 = abs(p1) * total_exposure

        # ── Historical CVaR (Expected Shortfall) ────────────────────────
        # The mean of every return at or below the VaR threshold — i.e.
        # the average loss given that the loss exceeds VaR. An empty tail
        # (impossible with ≥ 20 returns, but defensive) falls back to
        # the VaR value itself.
        tail_95 = returns[returns <= p5]
        tail_99 = returns[returns <= p1]
        cvar_95 = abs(float(np.mean(tail_95))) * total_exposure if len(tail_95) > 0 else var_95
        cvar_99 = abs(float(np.mean(tail_99))) * total_exposure if len(tail_99) > 0 else var_99

        return float(var_95), float(var_99), float(cvar_95), float(cvar_99), "historical"

    def _parametric_var_cvar(self, total_exposure: float) -> tuple[float, float, float, float, str]:
        """Normal-approximation VaR / CVaR using ``self.daily_vol``.

        Used as the fallback when no price history is available. The
        formulas are:

          VaR_p  = total_value × Z_p × σ
          CVaR_p = total_value × CVaR_Z_p × σ

        where ``Z_p`` is the standard normal quantile at confidence
        ``p`` and ``CVaR_Z_p`` is the corresponding expected-shortfall
        Z-multiplier.
        """
        var_95 = total_exposure * _Z_95 * self.daily_vol
        var_99 = total_exposure * _Z_99 * self.daily_vol
        cvar_95 = total_exposure * _CVAR_95 * self.daily_vol
        cvar_99 = total_exposure * _CVAR_99 * self.daily_vol
        return float(var_95), float(var_99), float(cvar_95), float(cvar_99), "parametric"

    def _portfolio_return_series(
        self,
        positions: list[dict],
        price_history: dict[str, list[float]],
    ) -> list[float]:
        """Compute the daily portfolio-return series from per-token prices.

        For each day ``i`` (1-indexed into the price history), the
        portfolio return is the **value-weighted** sum of per-token
        returns::

            r_p(i) = Σ_pos  side_mult × (value_pos / total_value) × r_token(i)

        where ``r_token(i) = (price[i] - price[i-1]) / price[i-1]``,
        ``value_pos = abs(size × current_price)`` is the current marked-
        to-market USD value of the position, and ``side_mult`` is +1 for
        LONG, −1 for SHORT.

        The value-weighting (NOT share-weighting) is load-bearing: a
        position with 100 shares at $0.50 ($50 value) and a position
        with 50 shares at $1.00 ($50 value) should each contribute
        equally to the portfolio return — they have equal USD exposure.
        Share-weighting would give the first position 2x the weight of
        the second, which is incorrect.

        The divisor ``total_value`` is the current marked-to-market
        portfolio value (sum of ``abs(size * current_price)``) — this
        normalises the weighted P&L into a return so the percentile /
        tail-mean computations are scale-free, then we re-scale by
        ``total_value`` at the end (in :meth:`_compute_var_cvar`) to
        get USD losses.

        Tokens with no price history (or a too-short history for day
        ``i``) contribute zero to that day's return — equivalent to
        assuming their price didn't move that day.
        """
        if not price_history:
            return []

        # Current marked-to-market portfolio value — the divisor that
        # turns the weighted P&L into a return.
        total_value = sum(
            abs(float(p.get("size", 0) or 0)) * float(
                p.get("current_price")
                if p.get("current_price") is not None
                else (p.get("avg_price", 0) or 0)
            )
            for p in positions
        )
        if total_value <= 0:
            return []

        # The longest price series determines how many days we can
        # compute. Shorter series contribute zero to days beyond their
        # own length (the ``i < len(prices)`` guard below).
        max_len = 0
        for prices in price_history.values():
            if prices and len(prices) > max_len:
                max_len = len(prices)

        # Per-day portfolio return: Σ_pos  r_token(i) × side_mult × (value_pos / total_value).
        # The weight (value_pos / total_value) is the position's share of the
        # current marked-to-market portfolio value — this is the value-weighted
        # contribution to the portfolio's daily return. Tokens with no price
        # history (or a too-short history for day ``i``) contribute zero.
        returns: list[float] = []
        for i in range(1, max_len):
            daily_return = 0.0
            for p in positions:
                token_id = p.get("token_id", "")
                prices = price_history.get(token_id, [])
                if i >= len(prices):
                    continue
                prev = prices[i - 1]
                if not prev or prev <= 0:
                    continue
                asset_return = (prices[i] - prev) / prev
                size = float(p.get("size", 0) or 0)
                price = float(
                    p.get("current_price")
                    if p.get("current_price") is not None
                    else (p.get("avg_price", 0) or 0)
                )
                value = abs(size * price)
                if value <= 0:
                    continue
                side_mult = 1.0 if (p.get("side") or "LONG").upper() == "LONG" else -1.0
                daily_return += asset_return * side_mult * (value / total_value)
            returns.append(daily_return)
        return returns


# ── Singleton ──────────────────────────────────────────────────────────────

live_risk_metrics = LiveRiskMetrics()


# ── HTTP routes ─────────────────────────────────────────────────────────────


def _positions_from_live_store() -> list[dict]:
    """Best-effort snapshot of the live ``DataStore`` positions in the
    dict shape :meth:`compute` expects.

    Mirrors the identically-named helper in ``core/stress_test.py`` so
    both modules accept the same position shape (and a future refactor
    could share the helper between them). Returns an empty list if the
    store can't be imported (e.g. running in a test environment without
    the full app loaded) so the route handler degrades gracefully to a
    zeroed risk-metrics payload rather than crashing on import.
    """
    try:
        from core.data_store import store as _store  # local — avoids import cycle
    except Exception:  # pragma: no cover — defensive, exercised only in broken envs
        logger.debug("live_risk_metrics: data_store unavailable, returning empty positions")
        return []

    positions: list[dict] = []
    for token_id, pos in _store.positions.items():
        # Same LONG/SHORT mapping as stress_test._positions_from_live_store
        # — pick the larger leg as the dominant side; ties default to
        # LONG.
        if pos.yes_shares >= pos.no_shares and pos.yes_shares > 0:
            size = pos.yes_shares
            side = "LONG"
        elif pos.no_shares > 0:
            size = pos.no_shares
            side = "SHORT"
        else:
            # Flat position — skip (no exposure to risk).
            continue
        book = _store.order_books.get(token_id)
        mid = book.mid if book is not None else None
        current_price = mid if mid is not None else pos.avg_entry_price
        positions.append({
            "token_id": token_id,
            "size": size,
            "side": side,
            "avg_price": pos.avg_entry_price,
            "current_price": current_price,
        })
    return positions


def register_routes(app: Any) -> None:
    """Append the live risk-metrics endpoint to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/portfolio/risk-metrics   return the live portfolio risk
                                         metrics (VaR / CVaR / exposure /
                                         concentration) computed against
                                         the live ``DataStore`` positions;
                                         optional ``?price_history=<json>``
                                         query param is accepted but the
                                         dashboard typically calls
                                         without it (parametric fallback)
    """
    from fastapi import HTTPException  # local — FastAPI optional at module load

    @app.get("/api/portfolio/risk-metrics", tags=["portfolio"])
    async def _get_portfolio_risk_metrics():
        """Get live portfolio risk metrics (VaR, CVaR, exposure, concentration).

        Reads positions from the live ``DataStore`` — no body accepted.
        If the store has no open positions, returns a 200 with zeroed
        metrics and ``var_method="none"`` (the dashboard's "no
        positions to risk-assess" state).

        Uses the parametric VaR fallback (5 % daily vol, normal
        approximation) since the live book poller doesn't yet persist
        a price history long enough to compute historical VaR. When
        price history becomes available, the caller can pass it via
        the :meth:`LiveRiskMetrics.compute` argument; the HTTP route
        uses the parametric fallback for now.
        """
        positions = _positions_from_live_store()
        metrics = live_risk_metrics.compute(positions)
        return metrics.to_dict()


__all__ = [
    "PortfolioRiskMetrics",
    "LiveRiskMetrics",
    "live_risk_metrics",
    "register_routes",
]
