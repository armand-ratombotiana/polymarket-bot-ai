"""Backtest report generator — produces JSON + PDF reports.

PDF uses reportlab. Reports include:
- Summary metrics (return, Sharpe, max drawdown, win rate)
- Equity curve chart
- Monthly returns heatmap
- Trade distribution
- Risk metrics

The ``generate_report`` entry point accepts the dict-shape returned by
``backtesting.engine.run_realistic_backtest`` (a dict with ``trades`` +
``equity_curve`` + ``metrics`` keys) OR the simpler dict-shape returned
by ``backtesting.engine.BacktestEngine.run_backtest().to_dict()``
(an ``equity_curve`` list of per-step snapshots + a flat ``trades``
list). The equity curve normaliser (``_normalise_equity``) handles both
forms — a list of plain floats OR a list of per-step snapshot dicts
(``{"step": ..., "equity": ..., "drawdown": ...}``) — so the same
report generator works against either engine variant.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class BacktestReport:
    """Immutable snapshot of a single backtest run's performance analytics.

    The dataclass shape is the wire format returned by
    ``POST /api/backtest/report`` (``report_to_json`` is a thin
    ``dataclasses.asdict`` wrapper). Every numeric field is a primitive
    ``float`` / ``int`` (never ``np.float64``) so JSON serialisation
    is direct — no custom encoder required.
    """

    # Metadata
    report_id: str
    created_at: float
    strategy: str
    period_start: float
    period_end: float

    # Performance metrics
    total_return: float
    annualized_return: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    max_drawdown_duration_days: int
    volatility: float
    downside_deviation: float

    # Trade metrics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    avg_hold_time_hours: float

    # Risk metrics
    var_95: float  # 95% Value at Risk
    cvar_95: float  # Conditional VaR (Expected Shortfall)
    beta: float
    alpha: float
    correlation: float

    # Equity curve
    equity_curve: list[float]
    drawdown_curve: list[float]

    # Trade list (anonymised, capped at 100)
    trades: list[dict]

    # Monthly returns
    monthly_returns: dict[str, float]  # {"2024-01": 0.05, ...}


def generate_report(
    backtest_result: dict, strategy_name: str = "unknown"
) -> BacktestReport:
    """Generate a comprehensive report from backtest results.

    Accepts either:
      * ``run_realistic_backtest(...)`` output — ``{"trades": [...],
        "equity_curve": [...], "metrics": {...}, "look_ahead_bias": {...}}``
        where ``equity_curve`` is a list of per-step snapshot dicts.
      * ``BacktestEngine.run_backtest(...).to_dict()`` output — flat dict
        with ``equity_curve`` (list of per-step snapshots) + ``trades``.
      * A minimal dict ``{"equity_curve": [1.0, 1.01, ...],
        "trades": [{"pnl": 10, "timestamp": 1700000000}, ...]}``.

    Returns an empty placeholder report (zeroed metrics, ``equity_curve=[1.0]``)
    when the input has fewer than 2 equity points — there's not enough
    data to compute returns-based metrics on a single point.
    """
    # Extract equity curve
    raw_equity = backtest_result.get("equity_curve", [1.0])
    equity = _normalise_equity(raw_equity)
    trades = backtest_result.get("trades", [])

    # Compute metrics
    if len(equity) < 2:
        return _empty_report(strategy_name)

    returns = np.diff(equity) / np.asarray(equity[:-1], dtype=float)

    total_return = (equity[-1] / equity[0] - 1) if equity[0] > 0 else 0
    # Annualized (assume 252 trading days, ~1 trade per day)
    n_periods = len(returns)
    annualized = (
        (1 + total_return) ** (252 / max(n_periods, 1)) - 1 if n_periods > 0 else 0
    )

    # Sharpe
    sharpe = (
        float(np.mean(returns) / (np.std(returns) + 1e-8) * np.sqrt(252))
        if n_periods > 0
        else 0
    )

    # Sortino
    downside = returns[returns < 0]
    sortino = (
        float(np.mean(returns) / (np.std(downside) + 1e-8) * np.sqrt(252))
        if len(downside) > 0
        else 0
    )

    # Max drawdown
    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - np.asarray(equity, dtype=float)) / peak
    max_dd = float(np.max(drawdowns))
    max_dd_duration = _compute_drawdown_duration(drawdowns)

    # Calmar
    calmar = annualized / (max_dd + 1e-8) if max_dd > 0 else 0

    # Volatility
    vol = float(np.std(returns) * np.sqrt(252))
    downside_dev = (
        float(np.std(downside) * np.sqrt(252)) if len(downside) > 0 else 0
    )

    # Trade metrics
    winning = [t for t in trades if t.get("pnl", 0) > 0]
    losing = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = len(winning) / len(trades) if trades else 0
    avg_win = float(np.mean([t["pnl"] for t in winning])) if winning else 0
    avg_loss = float(np.mean([t["pnl"] for t in losing])) if losing else 0
    profit_factor = (
        abs(
            sum(t["pnl"] for t in winning)
            / (sum(t["pnl"] for t in losing) + 1e-8)
        )
        if losing
        else float("inf")
    )
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))

    # VaR and CVaR
    var_95 = float(np.percentile(returns, 5)) if n_periods > 0 else 0
    cvar_95 = (
        float(np.mean(returns[returns <= var_95])) if n_periods > 0 else 0
    )

    # Monthly returns
    monthly = _compute_monthly_returns(trades)

    report_id = hashlib.md5(
        f"{strategy_name}{time.time()}".encode()
    ).hexdigest()[:12]

    return BacktestReport(
        report_id=report_id,
        created_at=time.time(),
        strategy=strategy_name,
        period_start=_extract_timestamp(trades[0]) if trades else 0,
        period_end=_extract_timestamp(trades[-1]) if trades else time.time(),
        total_return=float(total_return),
        annualized_return=float(annualized),
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=float(calmar),
        max_drawdown=max_dd,
        max_drawdown_duration_days=max_dd_duration,
        volatility=vol,
        downside_deviation=downside_dev,
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate=float(win_rate),
        avg_win=float(avg_win),
        avg_loss=float(avg_loss),
        profit_factor=float(profit_factor) if profit_factor != float("inf") else 999.0,
        expectancy=float(expectancy),
        avg_hold_time_hours=_avg_hold_time(trades),
        var_95=var_95,
        cvar_95=cvar_95,
        beta=0.0,  # Needs benchmark
        alpha=0.0,
        correlation=0.0,
        equity_curve=[float(e) for e in equity],
        drawdown_curve=[float(d) for d in drawdowns],
        trades=list(trades[:100]),  # Cap at 100 for report size
        monthly_returns=monthly,
    )


def _normalise_equity(raw_equity: Any) -> list[float]:
    """Coerce an equity curve into a list of floats.

    Handles three shapes:
      * ``list[float]`` — already the desired form; values pass through.
      * ``list[dict]`` — per-step snapshots like ``{"step": ..., "equity":
        ..., "drawdown": ...}`` (the shape returned by both
        ``BacktestEngine.run_backtest`` and ``run_realistic_backtest``);
        the ``equity`` key is extracted from each.
      * ``list`` mixing dict + float — defensive fallback for malformed
        inputs; dict entries yield their ``equity``, non-dict entries
        are coerced via ``float(...)``.

    A trailing ``float(...)`` cast guarantees JSON serialisability
    (no ``np.float64`` leaks into the report dict, which would break
    ``json.dumps`` without a custom encoder).
    """
    if not isinstance(raw_equity, (list, tuple)) or len(raw_equity) == 0:
        return [1.0]
    out: list[float] = []
    for pt in raw_equity:
        if isinstance(pt, dict):
            val = pt.get("equity", pt.get("value", 1.0))
        else:
            val = pt
        try:
            out.append(float(val))
        except (TypeError, ValueError):
            # Skip malformed points rather than crash the report —
            # the metric computation downstream tolerates shortfalls
            # by falling back to ``_empty_report`` when ``len(equity)
            # < 2``.
            continue
    return out if out else [1.0]


def _extract_timestamp(trade: dict) -> float:
    """Pull a Unix timestamp out of a trade dict, tolerating either a
    raw float (``timestamp`` key) or an ISO-8601 string (``ts`` key as
    emitted by ``run_realistic_backtest``). Returns 0 on failure so
    ``period_start`` / ``period_end`` degrade gracefully without
    crashing the report builder."""
    ts = trade.get("timestamp")
    if ts is None:
        ts = trade.get("ts")
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        # ISO-8601 string (e.g. "2025-01-01T00:00:00")
        import datetime as _dt

        return _dt.datetime.fromisoformat(str(ts)).timestamp()
    except (ValueError, TypeError):
        return 0


def _compute_drawdown_duration(drawdowns: np.ndarray) -> int:
    """Compute max drawdown duration in days.

    A day is counted as any step where the drawdown exceeds 1%
    (``dd > 0.01``); consecutive qualifying steps extend the run.
    Returns the longest run encountered.
    """
    max_duration = 0
    current = 0
    for dd in drawdowns:
        if dd > 0.01:  # >1% drawdown
            current += 1
        else:
            max_duration = max(max_duration, current)
            current = 0
    return max(max_duration, current)


def _avg_hold_time(trades: list) -> float:
    """Mean trade hold-time in hours, ignoring trades with no hold time
    recorded (``hold_time_hours`` absent or zero). Returns 0 when no
    trade carries a hold-time field — common for the synthetic
    archetype engine whose trades are instantaneous."""
    hold_times = [
        t.get("hold_time_hours", 0)
        for t in trades
        if t.get("hold_time_hours")
    ]
    return float(np.mean(hold_times)) if hold_times else 0


def _compute_monthly_returns(trades: list) -> dict[str, float]:
    """Aggregate per-trade P&L by calendar month.

    Returns ``{"YYYY-MM": pnl_sum, ...}``. Trades without a usable
    timestamp or with zero P&L are skipped (zero-P&L trades don't move
    the monthly aggregate and would inflate the dict size without
    adding signal)."""
    import datetime

    monthly: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.get("timestamp")
        if ts is None:
            ts = t.get("ts")
        pnl = t.get("pnl", 0)
        if ts is None or not pnl:
            continue
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.datetime.fromtimestamp(float(ts))
            else:
                dt = datetime.datetime.fromisoformat(str(ts))
        except (ValueError, TypeError, OSError):
            continue
        key = f"{dt.year}-{dt.month:02d}"
        monthly[key] += pnl
    return dict(monthly)


def _empty_report(strategy_name: str) -> BacktestReport:
    """Return a zeroed placeholder report for the degenerate cases
    (equity curve too short, no trades, etc.). Keeps the same dataclass
    shape as a populated report so downstream consumers don't need a
    separate code path for empty inputs."""
    return BacktestReport(
        report_id=hashlib.md5(
            f"{strategy_name}{time.time()}".encode()
        ).hexdigest()[:12],
        created_at=time.time(),
        strategy=strategy_name,
        period_start=0,
        period_end=time.time(),
        total_return=0,
        annualized_return=0,
        sharpe_ratio=0,
        sortino_ratio=0,
        calmar_ratio=0,
        max_drawdown=0,
        max_drawdown_duration_days=0,
        volatility=0,
        downside_deviation=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0,
        avg_win=0,
        avg_loss=0,
        profit_factor=0,
        expectancy=0,
        avg_hold_time_hours=0,
        var_95=0,
        cvar_95=0,
        beta=0,
        alpha=0,
        correlation=0,
        equity_curve=[1.0],
        drawdown_curve=[0],
        trades=[],
        monthly_returns={},
    )


def report_to_json(report: BacktestReport) -> dict:
    """Serialise a BacktestReport to a JSON-safe dict.

    ``dataclasses.asdict`` recurses into nested dataclasses / lists /
    dicts but does NOT coerce ``np.float64`` → ``float``. ``generate_report``
    already casts every numeric to a primitive at construction time, so
    the result is directly ``json.dumps``-able without a custom encoder.
    """
    return asdict(report)


def report_to_pdf(report: BacktestReport, output_path: Path) -> Path:
    """Generate a PDF report at ``output_path`` using reportlab.

    Layout (single-page A4 by default — multiple pages if the trade
    list + monthly returns overflow):
      1. Title — strategy name + report id.
      2. Summary table — 14 headline metrics (return, Sharpe, Sortino,
         Calmar, max DD, volatility, win rate, profit factor,
         expectancy, VaR/CVaR 95, total trades).
      3. Equity curve chart — matplotlib line chart embedded as a PNG
         (skipped silently if matplotlib is unavailable).
      4. Monthly returns table — month → P&L rows.

    Returns the same ``output_path`` for chaining. Raises ``ImportError``
    if ``reportlab`` is not installed — the caller (API route) catches
    that and returns a 503.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
            Paragraph,
            PageBreak,
            Image as RLImage,
        )
    except ImportError:
        logger.error("reportlab not installed — cannot generate PDF")
        raise ImportError("pip install reportlab")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(str(output_path), pagesize=A4)
    styles = getSampleStyleSheet()
    elements: list = []

    # Title
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=18,
        spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "ReportSub",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=9,
        textColor=colors.grey,
        spaceAfter=12,
    )
    elements.append(Paragraph(f"Backtest Report: {report.strategy}", title_style))
    generated_dt = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(report.created_at)
    )
    elements.append(
        Paragraph(
            f"Report ID: <b>{report.report_id}</b> &nbsp;&nbsp; "
            f"Generated: {generated_dt}",
            sub_style,
        )
    )
    elements.append(Spacer(1, 12))

    # Summary table
    summary_data = [
        ["Metric", "Value"],
        ["Total Return", f"{report.total_return * 100:.2f}%"],
        ["Annualized Return", f"{report.annualized_return * 100:.2f}%"],
        ["Sharpe Ratio", f"{report.sharpe_ratio:.3f}"],
        ["Sortino Ratio", f"{report.sortino_ratio:.3f}"],
        ["Calmar Ratio", f"{report.calmar_ratio:.3f}"],
        ["Max Drawdown", f"{report.max_drawdown * 100:.2f}%"],
        ["Max DD Duration (days)", str(report.max_drawdown_duration_days)],
        ["Volatility", f"{report.volatility * 100:.2f}%"],
        ["Downside Deviation", f"{report.downside_deviation * 100:.2f}%"],
        ["Win Rate", f"{report.win_rate * 100:.1f}%"],
        ["Profit Factor", f"{report.profit_factor:.2f}"],
        ["Expectancy ($/trade)", f"${report.expectancy:.4f}"],
        ["VaR (95%)", f"{report.var_95 * 100:.2f}%"],
        ["CVaR (95%)", f"{report.cvar_95 * 100:.2f}%"],
        ["Avg Win ($)", f"${report.avg_win:.2f}"],
        ["Avg Loss ($)", f"${report.avg_loss:.2f}"],
        ["Avg Hold (hours)", f"{report.avg_hold_time_hours:.2f}"],
        ["Total Trades", str(report.total_trades)],
        ["Winning / Losing", f"{report.winning_trades} / {report.losing_trades}"],
    ]

    table = Table(summary_data, colWidths=[2.2 * inch, 2.2 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.whitesmoke, colors.white]),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 16))

    # Equity curve chart (best-effort; skip silently if matplotlib is
    # unavailable — the report still contains the summary table + monthly
    # returns below).
    chart_path = _render_equity_chart(report)
    if chart_path is not None:
        elements.append(Paragraph("Equity Curve", styles["Heading2"]))
        elements.append(Spacer(1, 6))
        elements.append(RLImage(str(chart_path), width=6 * inch, height=3 * inch))
        elements.append(Spacer(1, 12))

    # Monthly returns
    if report.monthly_returns:
        elements.append(Paragraph("Monthly Returns", styles["Heading2"]))
        elements.append(Spacer(1, 6))
        monthly_data: list[list[str]] = [["Month", "P&L ($)"]]
        for month, pnl in sorted(report.monthly_returns.items()):
            monthly_data.append([month, f"{pnl:.2f}"])
        mtable = Table(monthly_data, colWidths=[1.5 * inch, 1.5 * inch])
        mtable.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.whitesmoke, colors.white]),
                ]
            )
        )
        elements.append(mtable)
        elements.append(Spacer(1, 12))

    # Trade distribution summary (count + average P&L bands)
    if report.trades:
        elements.append(Paragraph("Trade Distribution", styles["Heading2"]))
        elements.append(Spacer(1, 6))
        pnls = [float(t.get("pnl", 0)) for t in report.trades]
        dist_rows = [
            ["Band", "Count"],
            ["Winners (> $0)", str(sum(1 for p in pnls if p > 0))],
            ["Losers (< $0)", str(sum(1 for p in pnls if p < 0))],
            ["Break-even (= $0)", str(sum(1 for p in pnls if p == 0))],
            ["Avg P&L", f"${sum(pnls) / max(len(pnls), 1):.2f}"],
            ["Max P&L", f"${max(pnls):.2f}"],
            ["Min P&L", f"${min(pnls):.2f}"],
        ]
        dtable = Table(dist_rows, colWidths=[2 * inch, 1.5 * inch])
        dtable.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.whitesmoke, colors.white]),
                ]
            )
        )
        elements.append(dtable)

    doc.build(elements)
    return output_path


def _render_equity_chart(report: BacktestReport) -> Optional[Path]:
    """Render the equity curve to a temp PNG using matplotlib.

    Returns ``None`` (skip the chart) when:
      * matplotlib is not installed.
      * the equity curve has fewer than 2 points (nothing to plot).

    The chart is written to a temp file under ``/tmp`` (or
    ``$TMPDIR``) — the caller (``report_to_pdf``) embeds it into the
    PDF, after which the file is no longer referenced. The temp file
    is left on disk; the OS reaps ``/tmp`` periodically and the file
    is small (< 50 KB typical).
    """
    if len(report.equity_curve) < 2:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")  # non-interactive backend — must be set
        # before ``pyplot`` is imported.
        import matplotlib.pyplot as plt
        import tempfile
    except ImportError:
        return None

    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    xs = list(range(len(report.equity_curve)))
    ax.plot(xs, report.equity_curve, linewidth=1.5, color="#2c3e50")
    ax.fill_between(
        xs,
        report.equity_curve,
        report.equity_curve[0],
        where=[e >= report.equity_curve[0] for e in report.equity_curve],
        color="#27ae60",
        alpha=0.15,
    )
    ax.fill_between(
        xs,
        report.equity_curve,
        report.equity_curve[0],
        where=[e < report.equity_curve[0] for e in report.equity_curve],
        color="#c0392b",
        alpha=0.15,
    )
    ax.axhline(
        report.equity_curve[0],
        color="#7f8c8d",
        linewidth=0.5,
        linestyle="--",
    )
    ax.set_title(
        f"Equity Curve — {report.strategy} "
        f"(return: {report.total_return * 100:+.2f}%)",
        fontsize=11,
    )
    ax.set_xlabel("Step")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    tmp = Path(tempfile.gettempdir()) / f"pmbot_report_{report.report_id}.png"
    fig.savefig(str(tmp), format="png")
    plt.close(fig)
    return tmp
