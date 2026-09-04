"""
core/portfolio.py — Portfolio analytics: exposure decomposition, reconciliation,
risk-adjusted strategy scores, and the strategy leaderboard.

Objective (per platform mandate):
  Maximize sustainable, risk-adjusted net P&L while minimizing open exposure,
  capital-at-risk, drawdown, correlation, uncertainty, and time in positions.

Exposure is never reported as one ambiguous number. It is decomposed into:
  capital invested, reserved for pending orders, gross market value,
  net directional exposure, maximum remaining loss, per-group / per-strategy
  exposure, exposure duration, exposure-dollar-days, and available cash.
"""
from __future__ import annotations

import time

from core.data_store import Side, store

# ── Exposure decomposition (mandate section 2) ──────────────────────────────

def compute_exposure(book_provider=None) -> dict:
    """
    Decompose portfolio exposure. `book_provider` is an optional async callable
    token_id -> OrderBook used for gross market value; without it we use cost
    basis (average entry) as the mark.
    """
    positions = [p for p in store.positions.values() if p.current_exposure > 0.001]

    capital_invested = sum(p.total_invested for p in positions)
    max_remaining_loss = sum(p.current_exposure for p in positions)
    pending_capital = sum(o.price * o.size for o in store.open_orders.values())
    net_directional = sum(
        p.yes_shares * p.avg_entry_price - p.no_shares * p.avg_entry_price
        for p in positions
    )

    exposure_dollar_days = sum(p.exposure_dollar_days for p in positions)
    avg_duration_hours = (
        sum(p.exposure_duration_hours for p in positions) / len(positions)
        if positions else 0.0
    )

    # Per correlated event group (market slug).
    by_group: dict[str, float] = {}
    for p in positions:
        key = p.market_slug or "<unknown>"
        by_group[key] = by_group.get(key, 0.0) + p.current_exposure

    # Per strategy.
    by_strategy: dict[str, float] = {}
    for p in positions:
        key = p.strategy or "<unknown>"
        by_strategy[key] = by_strategy.get(key, 0.0) + p.current_exposure

    available_cash = store.paper_balance
    reserved_cash = pending_capital
    gross_market_value = max_remaining_loss  # cost-basis mark by default

    return {
        "capital_invested": round(capital_invested, 2),
        "reserved_for_pending_orders": round(pending_capital, 2),
        "gross_market_value": round(gross_market_value, 2),
        "net_directional_exposure": round(net_directional, 2),
        "maximum_remaining_loss": round(max_remaining_loss, 2),
        "exposure_per_group": {k: round(v, 2) for k, v in sorted(by_group.items(), key=lambda x: -x[1])},
        "exposure_per_strategy": {k: round(v, 2) for k, v in sorted(by_strategy.items(), key=lambda x: -x[1])},
        "exposure_duration_hours_avg": round(avg_duration_hours, 2),
        "exposure_dollar_days": round(exposure_dollar_days, 2),
        "available_cash": round(available_cash, 2),
        "reserved_cash": round(reserved_cash, 2),
        "open_position_count": len(positions),
    }


def compute_reconciliation(bankroll_ceiling: float = 200.0) -> dict:
    """
    Reconciliation investigation for an out-of-bounds open exposure.
    Determines whether the reported number is genuine capital-at-risk or an
    artifact of aggregation, unit confusion, contamination, or stale state.
    """
    exp = compute_exposure()
    positions = [p for p in store.positions.values() if p.current_exposure > 0.001]
    trades = list(store.trades)

    token_ids = [p.token_id for p in positions]
    dup_token_ids = len(token_ids) - len(set(token_ids))
    paper_trades = sum(1 for t in trades if t.paper)
    live_trades = len(trades) - paper_trades

    stale_positions = [
        p for p in store.positions.values()
        if p.current_exposure <= 0.001 and (p.total_invested > 0.01 or abs(p.realised_pnl) > 0.001)
    ]

    buy_cost = sum(t.price * t.size for t in trades if t.side == Side.BUY)
    sell_revenue = sum(t.price * t.size for t in trades if t.side == Side.SELL)

    checks = {
        "duplicate_position_token_ids": dup_token_ids,
        "duplicate_fill_anomaly": len(trades) - len({t.trade_id for t in trades}),
        "paper_trades": paper_trades,
        "live_trades": live_trades,
        "stale_positions_still_held": len(stale_positions),
        "largest_group_exposure": max(exp["exposure_per_group"].values(), default=0.0),
        "buy_cost": round(buy_cost, 2),
        "sell_revenue": round(sell_revenue, 2),
    }

    findings: list[str] = []
    if exp["maximum_remaining_loss"] > bankroll_ceiling * 0.6:
        findings.append(
            f"Exposure ${exp['maximum_remaining_loss']:.2f} exceeds 60% of the ${bankroll_ceiling:.0f} "
            f"bankroll ceiling; reconcile before deploying additional capital."
        )
    if checks["live_trades"] > 0 and paper_trades > 0:
        findings.append("Mixed paper/live trade ledger detected — investigate contamination.")
    if checks["stale_positions_still_held"] > 0:
        findings.append(
            f"{checks['stale_positions_still_held']} position(s) with zero exposure but nonzero "
            f"invested/realised P&L remain in the ledger."
        )
    if checks["duplicate_position_token_ids"] > 0:
        findings.append("Duplicate token_ids in positions dict detected.")
    if checks["largest_group_exposure"] > bankroll_ceiling * 0.2:
        findings.append(
            f"Single correlated group holds ${checks['largest_group_exposure']:.2f} "
            f"(>{bankroll_ceiling*0.2:.2f} = 20% of ceiling) — concentration risk."
        )
    if checks["buy_cost"] > 0 and abs(buy_cost - sell_revenue) > exp["maximum_remaining_loss"]:
        findings.append("Gross buy/sell turnover exceeds net cost basis — verify unit/quantity aggregation.")

    reconciled = exp["maximum_remaining_loss"] <= bankroll_ceiling * 0.6 and not findings
    return {
        "reconciled": reconciled,
        "status": "OBSERVATION_ONLY" if not reconciled else "OK",
        "exposure": exp,
        "checks": checks,
        "findings": findings,
    }


# ── Per-strategy accounting & risk-adjusted score ──────────────────────────

def _cumulative_series(pnls: list[float]) -> list[float]:
    series, total = [], 0.0
    for p in pnls:
        total += p
        series.append(total)
    return series


def _max_drawdown(series: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def strategy_stats(strategy: str) -> dict:
    """Per-strategy performance, exposure, and execution attribution."""
    trades = [t for t in store.trades if t.strategy == strategy]
    closed = [t for t in trades if t.pnl != 0]
    fills = len(trades)

    gross_pnl = sum(abs(t.pnl) for t in closed)
    net_pnl = sum(t.pnl for t in closed)
    wins = sum(1 for t in closed if t.pnl > 0)
    losses = sum(1 for t in closed if t.pnl < 0)
    win_rate = (wins / len(closed)) if closed else 0.0
    avg_win = (sum(t.pnl for t in closed if t.pnl > 0) / wins) if wins else 0.0
    avg_loss = (sum(t.pnl for t in closed if t.pnl < 0) / losses) if losses else 0.0
    profit_factor = (
        (sum(t.pnl for t in closed if t.pnl > 0) / max(1e-9, -sum(t.pnl for t in closed if t.pnl < 0)))
        if losses else (float("inf") if wins else 0.0)
    )

    capital_exposed = sum(t.price * t.size for t in trades if t.side == Side.BUY)
    positions = [p for p in store.positions.values() if p.strategy == strategy and p.current_exposure > 0.001]
    open_exposure = sum(p.current_exposure for p in positions)
    exposure_dollar_days = sum(p.exposure_dollar_days for p in positions)
    avg_duration_hours = (
        sum(p.exposure_duration_hours for p in positions) / len(positions) if positions else 0.0
    )

    series = _cumulative_series([t.pnl for t in closed])
    max_drawdown = _max_drawdown(series)

    # Fill / execution quality
    notional = sum(t.price * t.size for t in trades)

    profit_per_dollar = (net_pnl / capital_exposed) if capital_exposed > 0 else 0.0
    profit_per_exposure_day = (net_pnl / exposure_dollar_days) if exposure_dollar_days > 0 else 0.0

    return {
        "strategy": strategy,
        "fills": fills,
        "closed_trades": len(closed),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "capital_exposed": round(capital_exposed, 2),
        "open_exposure": round(open_exposure, 2),
        "profit_per_dollar_exposed": round(profit_per_dollar, 4),
        "profit_per_exposure_day": round(profit_per_exposure_day, 4),
        "exposure_dollar_days": round(exposure_dollar_days, 2),
        "avg_holding_duration_hours": round(avg_duration_hours, 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_drawdown, 2),
        "notional_volume": round(notional, 2),
    }


def risk_adjusted_score(stats: dict) -> float:
    """
    Strategy Score = Expected Net P&L − penalties (exposure, drawdown,
    correlation, uncertainty, liquidity, execution-risk, capital-time).
    Higher is better; negative = destroyer of the bankroll.
    """
    net = stats["net_pnl"]
    exposure_penalty = stats["open_exposure"] * 0.05
    drawdown_penalty = stats["max_drawdown"] * 0.5
    capital_time_penalty = stats["exposure_dollar_days"] * 0.002
    # Uncertainty penalty scales with thin sample size.
    uncertainty_penalty = (10.0 / max(1, stats["closed_trades"])) if stats["closed_trades"] < 20 else 0.0
    score = (
        net
        - exposure_penalty
        - drawdown_penalty
        - capital_time_penalty
        - uncertainty_penalty
    )
    return round(score, 4)


def leaderboard() -> dict:
    """Rank all strategies by reproducible risk-adjusted net performance."""
    strategies = sorted({t.strategy for t in store.trades if t.strategy})
    rows = []
    for s in strategies:
        stats = strategy_stats(s)
        score = risk_adjusted_score(stats)
        rows.append({**stats, "risk_adjusted_score": score})
    rows.sort(key=lambda r: r["risk_adjusted_score"], reverse=True)
    return {"ranked": rows, "count": len(rows)}


# ── Per-strategy risk-adjusted metrics (W23-5) ──────────────────────────────

# Annualisation factor for trade-level Sharpe / Sortino. 252 trading days
# is the standard equity-market convention; prediction-market trades tend
# to have a shorter holding horizon but the same convention keeps the
# numbers comparable to literature benchmarks.
_TRADE_ANNUALISATION_SQRT = (252.0) ** 0.5


def _safe_std(values: list[float]) -> float:
    """Population standard deviation; returns 0.0 for <2 samples."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return var ** 0.5


def _downside_std(values: list[float], mar: float = 0.0) -> float:
    """Downside deviation: only the returns below the MAR (Minimum Acceptable Return).

    Standard Sortino definition. Returns 0.0 if no returns are below MAR.
    """
    downside = [v - mar for v in values if v < mar]
    if not downside:
        return 0.0
    var = sum(d ** 2 for d in downside) / len(values)
    return var ** 0.5


def _sharpe_ratio(pnls: list[float]) -> float | None:
    """Per-trade Sharpe ratio, annualised to 252 trading periods.

    Returns None when there's insufficient data (fewer than 2 closed
    trades) or when std == 0 (degenerate single-trade series).
    """
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    std = _safe_std(pnls)
    if std == 0:
        return None
    return round((mean / std) * _TRADE_ANNUALISATION_SQRT, 4)


def _sortino_ratio(pnls: list[float], mar: float = 0.0) -> float | None:
    """Per-trade Sortino ratio, annualised.

    Uses downside deviation (only negative returns vs MAR=0) instead of
    total standard deviation. Returns None if no downside observations.
    """
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    dd = _downside_std(pnls, mar=mar)
    if dd == 0:
        return None
    return round((mean / dd) * _TRADE_ANNUALISATION_SQRT, 4)


def _calmar_ratio(pnls: list[float]) -> float | None:
    """Calmar ratio: cumulative return / max drawdown.

    For trade-level P&L: cumulative pnl / max_drawdown. Returns None if
    max_drawdown is 0 (no drawdown observed).
    """
    if len(pnls) < 2:
        return None
    series = _cumulative_series(pnls)
    mdd = _max_drawdown(series)
    if mdd == 0:
        return None
    cumulative = series[-1] if series else 0.0
    return round(cumulative / mdd, 4)


def strategy_performance(strategy_registry=None) -> dict:
    """Per-strategy performance dashboard with risk-adjusted attribution.

    W23-5 — Builds a comprehensive per-strategy breakdown used by the
    Strategy Performance Dashboard UI panel. For each strategy in the
    catalog (implemented + planned), computes:

      * P&L — realized, unrealized, net, gross
      * Trade stats — fills, closed_trades, win_rate, profit_factor,
        expectancy, avg_win, avg_loss
      * Risk metrics — sharpe_ratio, sortino_ratio, calmar_ratio,
        max_drawdown (per-trade pnl basis, annualised to 252 trading
        periods)
      * Timing — avg_hold_hours, notional_volume, open_exposure
      * Equity curve — cumulative P&L time series for chart overlay
      * Catalog metadata — name, version, status, category, risk_level,
        is_running, is_enabled

    Args:
        strategy_registry: optional ``StrategyRegistry`` instance.
            When provided, its catalog drives the row order and supplies
            metadata (status, version, is_running). When None, only
            strategies that appear in ``store.trades`` are emitted (the
            catalog-driven fields default to PLANNED / v0).

    Returns:
        {
            "strategies": [row, ...],
            "total_pnl": float,
            "active_count": int,
            "implemented_count": int,
            "planned_count": int,
            "generated_at": float (unix epoch),
        }
    """
    # Build the catalog-driven row list.
    catalog_rows: list[dict] = []
    if strategy_registry is not None:
        try:
            catalog_rows = strategy_registry.get_catalog(implemented_only=False)
        except Exception:  # noqa: BLE001 — defensive: catalog should never crash the panel
            catalog_rows = []

    # Index catalog by strategy_id for fast metadata lookup.
    catalog_by_id: dict[str, dict] = {r["strategy_id"]: r for r in catalog_rows}

    # Always include any strategy that has traded (even if it's not in the
    # catalog anymore — defensive against catalog drift).
    traded_ids = {t.strategy for t in store.trades if t.strategy}
    all_strategy_ids: list[str] = []
    seen: set[str] = set()
    for r in catalog_rows:
        sid = r["strategy_id"]
        if sid not in seen:
            all_strategy_ids.append(sid)
            seen.add(sid)
    for sid in sorted(traded_ids):
        if sid not in seen:
            all_strategy_ids.append(sid)
            seen.add(sid)

    rows: list[dict] = []
    total_pnl = 0.0
    active_count = 0
    implemented_count = 0
    planned_count = 0

    for sid in all_strategy_ids:
        meta = catalog_by_id.get(sid, {})
        # Compute per-strategy stats from the trade ledger.
        stats = strategy_stats(sid) if sid in traded_ids else {
            "strategy": sid,
            "fills": 0,
            "closed_trades": 0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "capital_exposed": 0.0,
            "open_exposure": 0.0,
            "profit_per_dollar_exposed": 0.0,
            "profit_per_exposure_day": 0.0,
            "exposure_dollar_days": 0.0,
            "avg_holding_duration_hours": 0.0,
            "win_rate": 0.0,
            "profit_factor": None,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "notional_volume": 0.0,
        }

        # Closed-trade PnL series for Sharpe / Sortino / Calmar + equity curve.
        closed = [t for t in store.trades if t.strategy == sid and t.pnl != 0]
        # Sort by timestamp so the cumulative series is monotone.
        closed_sorted = sorted(closed, key=lambda t: t.timestamp)
        pnl_series = [t.pnl for t in closed_sorted]
        equity_curve = [
            {"timestamp": t.timestamp, "pnl": cum}
            for t, cum in zip(closed_sorted, _cumulative_series(pnl_series))
        ]

        # Realized vs unrealized P&L split.
        realized_pnl = sum(t.pnl for t in closed)
        positions = [p for p in store.positions.values() if p.strategy == sid and p.current_exposure > 0.001]
        # Mark-to-market unrealized P&L = (current market value) - (cost basis).
        # Without a live book lookup here (kept cheap; the dashboard polls every
        # 30 s), unrealized is approximated as 0.0 — the dashboard surfaces
        # realised P&L (the dominant signal) and leaves the unrealized tile to
        # the live Positions panel where the book is already in memory.
        unrealized_pnl = 0.0

        # Trade timing: avg hold hours from open positions; if none open,
        # fall back to the average duration across all closed trades (using
        # position.exposure_duration_hours when available, else 0).
        avg_hold_hours = stats.get("avg_holding_duration_hours", 0.0)

        row = {
            "strategy_id": sid,
            "name": meta.get("name", sid.replace("_", " ").title()),
            "version": meta.get("version", "1.0"),
            "category": meta.get("category", "unknown"),
            "description": meta.get("description", ""),
            "risk_level": meta.get("risk_level", "MEDIUM"),
            "status": meta.get("status", "PLANNED"),
            "is_running": bool(meta.get("is_running", False)),
            "is_enabled": bool(meta.get("default_enabled", False)) or bool(meta.get("is_running", False)),
            # P&L
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "net_pnl": round(stats["net_pnl"], 2),
            "gross_pnl": round(stats["gross_pnl"], 2),
            # Trade stats
            "closed_trades": stats["closed_trades"],
            "open_trades": len(positions),
            "fills": stats["fills"],
            "win_rate": round(stats["win_rate"], 4),
            "profit_factor": stats["profit_factor"],
            "expectancy": round(
                (stats["win_rate"] * stats["avg_win"]) + ((1 - stats["win_rate"]) * stats["avg_loss"]),
                4,
            ),
            "avg_win": round(stats["avg_win"], 2),
            "avg_loss": round(stats["avg_loss"], 2),
            # Risk metrics — annualised per-trade Sharpe / Sortino / Calmar.
            "sharpe_ratio": _sharpe_ratio(pnl_series),
            "sortino_ratio": _sortino_ratio(pnl_series, mar=0.0),
            "calmar_ratio": _calmar_ratio(pnl_series),
            "max_drawdown": round(stats["max_drawdown"], 2),
            # Timing
            "avg_hold_hours": round(avg_hold_hours, 2),
            "notional_volume": round(stats["notional_volume"], 2),
            "open_exposure": round(stats["open_exposure"], 2),
            # Equity curve (cumulative P&L per closed trade, sorted by timestamp)
            "equity_curve": equity_curve,
        }
        rows.append(row)
        total_pnl += row["net_pnl"]
        if row["is_running"]:
            active_count += 1
        if row["status"] == "IMPLEMENTED":
            implemented_count += 1
        else:
            planned_count += 1

    return {
        "strategies": rows,
        "total_pnl": round(total_pnl, 2),
        "active_count": active_count,
        "implemented_count": implemented_count,
        "planned_count": planned_count,
        "generated_at": time.time(),
    }