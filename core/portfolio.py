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