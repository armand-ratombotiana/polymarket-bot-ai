"""Historical replay backtest engine — replays actual market data.

Unlike the synthetic MC engine in ``backtesting/engine.py`` (which draws
prices from an RNG seeded by the strategy name), this engine loads REAL
order book snapshots and price history from the SQLite
``market_snapshots`` / ``orderbook_ticks`` tables (see
``core/timescale_db.py::_init_sqlite_fallback`` and
``core/market_db.py::_init_db`` for the schema) and replays them through
the strategy + risk + execution pipeline.

The contract is intentionally minimal so any object exposing a
``generate_signal(context: dict) -> dict | None`` method can be plugged
in. The default :class:`SimpleStrategy` demonstrates the contract with a
basic mean-reversion rule that BUYs when ``mid`` drops below the rolling
N-step average and SELLs when it reverts back above it.

Schema adaptation
~~~~~~~~~~~~~~~~~

The canonical ``market_snapshots`` table (created by
``core/timescale_db.py::_init_sqlite_fallback`` and
``core/market_db.py::_init_db``) has these columns:

    id, timestamp, token_id, slug,
    best_bid, best_ask, mid, spread,
    volume_24h, liquidity

It does NOT have ``bid_size`` / ``ask_size`` / ``volume`` columns (the
W17-6 Backtest Engine Assessment documented that this microstructure
depth lives in the sibling ``orderbook_ticks`` table with columns
``best_bid_size, best_ask_size``). The loader therefore LEFT JOINs
``orderbook_ticks`` on ``(token_id, timestamp)`` and aliases
``volume_24h → volume`` so the rest of the engine can consume a single
uniform :class:`HistoricalSnapshot` dataclass regardless of the
underlying schema split.

If the ``orderbook_ticks`` table is empty / missing / unreachable, the
JOIN degrades gracefully to NULL → ``0.0`` defaults on
``bid_size`` / ``ask_size`` (the replay loop only reads these for
context; the order-sizing path uses ``size`` from the strategy signal,
not book depth, so a zero depth is non-fatal).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional, List

import numpy as np

logger = logging.getLogger(__name__)


# ── W37-3 — Bias / leakage detector (lazy import) ──────────────────────────
# ``backtesting.bias_detector`` imports nothing from ``historical_replay`` so
# the lazy ``from backtesting.bias_detector import bias_detector`` inside
# :meth:`HistoricalReplayEngine.replay` cannot create a circular import.
# The detector is invoked after every replay so a critical finding is logged
# at ``ERROR`` level and surfaced on the ``ReplayResult.bias_report`` field
# (a JSON-serialisable dict) — the caller decides whether to discard the
# result. Mirrors the post-backtest hook called out in the W37-3 task spec.


# ── Data containers ─────────────────────────────────────────────────────────


@dataclass
class HistoricalSnapshot:
    """One row from the ``market_snapshots`` table at replay time.

    ``bid_size`` / ``ask_size`` come from the ``orderbook_ticks`` LEFT
    JOIN; ``volume`` is the ``volume_24h`` column aliased. ``spread`` is
    materialised (``best_ask - best_bid``) at insert time by the
    snapshot recorder (see ``core/market_db.py::record_snapshot``).
    """

    timestamp: float
    token_id: str
    best_bid: float
    best_ask: float
    mid: float
    spread: float
    bid_size: float
    ask_size: float
    volume: float = 0.0


@dataclass
class ReplayResult:
    """Replay output: trades, equity curve, and headline risk metrics.

    All numeric fields are Python ``float`` (not ``np.float64``) so the
    dataclass round-trips through ``json.dumps`` without a ``default=``
    serializer.

    W37-3 — the optional ``bias_report`` field carries the
    :class:`backtesting.bias_detector.BiasReport.to_dict` payload for
    the post-replay bias / leakage scan. Default ``{}`` (no scan run /
    no findings) so the field is always JSON-serialisable; the
    :meth:`HistoricalReplayEngine.replay` method always populates it.
    """

    start_time: float
    end_time: float
    n_snapshots: int
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=lambda: [0.0])
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    # W37-3 — bias / leakage report. Populated by
    # :meth:`HistoricalReplayEngine.replay` after every run; carries
    # ``{findings, summary, has_critical, critical_findings}``. Empty
    # dict by default (no findings / no scan run).
    bias_report: dict[str, Any] = field(default_factory=dict)


# ── Default strategy ────────────────────────────────────────────────────────


class SimpleStrategy:
    """Default mean-reversion strategy used by the API route.

    BUYs when ``mid`` drops more than ``threshold`` below the rolling
    ``window``-snapshot moving average AND no position is currently open.
    SELLs (closes the long) when ``mid`` reverts back above the rolling
    average AND a position is currently open.

    The rolling average is computed from a per-instance deque so the
    strategy is stateful across the replay loop (each call to
    ``generate_signal`` sees the prior calls' mids). This is the minimal
    contract a real strategy would satisfy against the replay engine —
    pluggable strategies (ML model, market maker, arb scanner) can be
    substituted via the ``strategy=`` kwarg on :meth:`HistoricalReplayEngine.replay`.
    """

    def __init__(self, window: int = 20, threshold: float = 0.01) -> None:
        self.window = max(2, int(window))
        self.threshold = float(threshold)
        self._mids: list[float] = []

    def generate_signal(self, context: dict) -> Optional[dict]:
        mid = float(context.get("mid", 0.0))
        position = float(context.get("position", 0.0))
        self._mids.append(mid)
        # Only retain the last ``window`` mids to bound memory.
        if len(self._mids) > self.window:
            self._mids = self._mids[-self.window:]

        if len(self._mids) < self.window:
            return None

        avg = float(np.mean(self._mids[-self.window:]))
        # Mean reversion: BUY below avg, SELL above avg.
        if position == 0.0 and mid < avg - self.threshold:
            return {"action": "BUY", "size": 1.0}
        if position > 0.0 and mid > avg:
            return {"action": "SELL", "size": position}
        return None


# ── Engine ─────────────────────────────────────────────────────────────────


class HistoricalReplayEngine:
    """Replays historical market data through a strategy pipeline.

    The engine is intentionally synchronous (single pass through the
    snapshot list) so the API layer can wrap each call in
    ``asyncio.to_thread`` (mirrors the ``/api/backtest/run`` /
    ``/api/backtest/report`` routes in ``api/server.py``).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    # ── Snapshot loader ────────────────────────────────────────────────

    def load_snapshots(
        self,
        token_id: str,
        start_time: float,
        end_time: float,
    ) -> List[HistoricalSnapshot]:
        """Load historical order book snapshots from the database.

        Joins ``market_snapshots`` LEFT JOIN ``orderbook_ticks`` on
        ``(token_id, timestamp)`` so each returned :class:`HistoricalSnapshot`
        carries both the top-of-book quotes AND the depth at the best
        bid/ask. If the ``orderbook_ticks`` table is empty / missing /
        unreachable, the JOIN degrades to NULL → ``0.0`` defaults (see
        module docstring).

        Args:
            token_id: Market token to filter on.
            start_time: Inclusive lower bound on ``timestamp``.
            end_time: Inclusive upper bound on ``timestamp``.

        Returns:
            Chronologically-ordered list of :class:`HistoricalSnapshot`.
            Empty list if the query fails or no rows match.
        """
        snapshots: List[HistoricalSnapshot] = []
        if not token_id:
            logger.warning("load_snapshots called with empty token_id")
            return snapshots

        # Primary query path — assumes the canonical schema (both tables).
        # If ``orderbook_ticks`` is missing, the fallback retry uses the
        # ``market_snapshots``-only query below.
        joined_sql = """
            SELECT s.timestamp, s.token_id, s.best_bid, s.best_ask, s.mid, s.spread,
                   COALESCE(t.best_bid_size, 0.0) AS bid_size,
                   COALESCE(t.best_ask_size, 0.0) AS ask_size,
                   COALESCE(s.volume_24h, 0.0) AS volume
            FROM market_snapshots s
            LEFT JOIN orderbook_ticks t
                   ON t.token_id = s.token_id AND t.timestamp = s.timestamp
            WHERE s.token_id = ? AND s.timestamp >= ? AND s.timestamp <= ?
            ORDER BY s.timestamp ASC
        """
        # Fallback path — used when orderbook_ticks is absent (older
        # deployments / fresh test DBs that only ran the
        # ``market_snapshots`` migration).
        simple_sql = """
            SELECT timestamp, token_id, best_bid, best_ask, mid, spread,
                   0.0 AS bid_size, 0.0 AS ask_size,
                   COALESCE(volume_24h, 0.0) AS volume
            FROM market_snapshots
            WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
        """

        params = (token_id, float(start_time), float(end_time))
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(joined_sql, params).fetchall()
                except sqlite3.OperationalError as exc:
                    # The most common OperationalError here is "no such
                    # table: orderbook_ticks" — fall back to the
                    # market_snapshots-only query. Any other
                    # OperationalError is also covered by the fallback
                    # (if market_snapshots itself is missing, the
                    # fallback will raise and the outer try/except logs
                    # it).
                    logger.warning(
                        "Joined snapshot query failed (%s); falling back "
                        "to market_snapshots-only query.",
                        exc,
                    )
                    rows = conn.execute(simple_sql, params).fetchall()

                for row in rows:
                    snapshots.append(
                        HistoricalSnapshot(
                            timestamp=float(row["timestamp"]),
                            token_id=str(row["token_id"]),
                            best_bid=float(row["best_bid"] or 0.0),
                            best_ask=float(row["best_ask"] or 0.0),
                            mid=float(row["mid"] or 0.0),
                            spread=float(row["spread"] or 0.0),
                            bid_size=float(row["bid_size"] or 0.0),
                            ask_size=float(row["ask_size"] or 0.0),
                            volume=float(row["volume"] or 0.0),
                        )
                    )
        except Exception as exc:
            # The DB may not exist yet (fresh deployment with no
            # snapshots ingested). Log + return an empty list so the
            # caller's replay() short-circuits to the zero-trade result.
            logger.error(
                "Failed to load snapshots for token=%s from %s: %s",
                token_id, self._db_path, exc,
            )
            return snapshots

        logger.info(
            "Loaded %d snapshots for token=%s in [%s, %s]",
            len(snapshots), token_id, start_time, end_time,
        )
        return snapshots

    # ── Replay loop ────────────────────────────────────────────────────

    def replay(
        self,
        token_id: str,
        strategy,
        start_time: float,
        end_time: float,
        initial_capital: float = 100.0,
    ) -> ReplayResult:
        """Replay historical data through a strategy.

        Args:
            token_id: Market to replay.
            strategy: Object exposing ``generate_signal(context: dict)
                -> dict | None``. The returned dict (when not ``None``)
                must contain ``action`` (``"BUY"`` / ``"SELL"``) and
                ``size`` (positive float).
            start_time: Replay start timestamp (epoch seconds).
            end_time: Replay end timestamp (epoch seconds).
            initial_capital: Starting cash in USD.

        Returns:
            :class:`ReplayResult` with trades, equity curve, and risk
            metrics (total return, Sharpe, max drawdown, win rate,
            profit factor).
        """
        snapshots = self.load_snapshots(token_id, start_time, end_time)
        if not snapshots:
            return ReplayResult(
                start_time=float(start_time),
                end_time=float(end_time),
                n_snapshots=0,
                trades=[],
                equity_curve=[float(initial_capital)],
                total_return=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                win_rate=0.0,
                profit_factor=0.0,
            )

        capital = float(initial_capital)
        position = 0.0  # shares held
        entry_price = 0.0
        trades: list[dict] = []
        # Equity curve starts with the initial capital — every snapshot
        # appends one mark-to-market point, so the curve length is
        # ``len(snapshots) + 1`` (matches the convention in
        # ``backtesting/advanced.py::_simulate_equity``).
        equity_curve: list[float] = [capital]

        for i, snap in enumerate(snapshots):
            # Build the market context the strategy sees at this snapshot.
            context = {
                "token_id": snap.token_id,
                "best_bid": snap.best_bid,
                "best_ask": snap.best_ask,
                "mid": snap.mid,
                "spread": snap.spread,
                "volume": snap.volume,
                "bid_size": snap.bid_size,
                "ask_size": snap.ask_size,
                "timestamp": snap.timestamp,
                "position": position,
                "capital": capital,
                "snapshot_index": i,
            }

            # Get signal from strategy. Strategies that don't implement
            # ``generate_signal`` (or raise) are treated as no-signal —
            # the replay continues to the next snapshot rather than
            # crashing the whole run.
            signal = None
            if strategy is not None and hasattr(strategy, "generate_signal"):
                try:
                    signal = strategy.generate_signal(context)
                except Exception as exc:
                    logger.warning(
                        "Strategy error at snapshot %d (ts=%s): %s",
                        i, snap.timestamp, exc,
                    )
                    signal = None

            if signal:
                action = str(signal.get("action", "")).upper()
                size = float(signal.get("size", 1.0) or 0.0)
                if size <= 0.0:
                    # Zero / negative sizes are no-ops (defensive: a
                    # strategy that returns ``{"action": "BUY", "size": 0}``
                    # should not crash the replay).
                    pass
                elif action == "BUY" and position == 0.0:
                    # Enter long at the best ask (cross the spread —
                    # marketable buy).
                    cost = size * snap.best_ask
                    if cost <= capital:
                        position = size
                        entry_price = snap.best_ask
                        capital -= cost
                        trades.append({
                            "timestamp": snap.timestamp,
                            "action": "BUY",
                            "price": snap.best_ask,
                            "size": size,
                            "pnl": 0.0,
                        })
                elif action == "SELL" and position > 0.0:
                    # Exit at the best bid (cross the spread — marketable
                    # sell). Only close the open position (no shorting).
                    proceeds = position * snap.best_bid
                    pnl = (snap.best_bid - entry_price) * position
                    capital += proceeds
                    trades.append({
                        "timestamp": snap.timestamp,
                        "action": "SELL",
                        "price": snap.best_bid,
                        "size": position,
                        "pnl": pnl,
                    })
                    position = 0.0
                    entry_price = 0.0

            # Mark-to-market the open position at the snapshot mid.
            mtm = capital + (position * snap.mid if position > 0.0 else 0.0)
            equity_curve.append(mtm)

        # Force-close any position still open at the last snapshot so
        # ``total_return`` reflects the realised end-of-window P&L rather
        # than a hypothetical paper P&L. This mirrors the convention in
        # ``backtesting/engine.py::_simulate_realistic_trade``.
        if position > 0.0 and snapshots:
            last = snapshots[-1]
            proceeds = position * last.best_bid
            pnl = (last.best_bid - entry_price) * position
            capital += proceeds
            trades.append({
                "timestamp": last.timestamp,
                "action": "SELL",
                "price": last.best_bid,
                "size": position,
                "pnl": pnl,
            })
            position = 0.0
            # Replace the last equity-curve point with the post-close
            # realised capital (otherwise the curve's tail still shows
            # the mark-to-market mid, slightly inflating total_return).
            equity_curve[-1] = capital

        return self._attach_bias_report(
            self._compute_metrics(
                start_time=float(start_time),
                end_time=float(end_time),
                n_snapshots=len(snapshots),
                trades=trades,
                equity_curve=equity_curve,
            ),
            trades=trades,
            snapshots=snapshots,
            token_id=token_id,
        )

    # ── W37-3 — Post-replay bias / leakage scan ────────────────────────

    @staticmethod
    def _attach_bias_report(
        result: "ReplayResult",
        *,
        trades: list[dict],
        snapshots: List["HistoricalSnapshot"],
        token_id: str,
    ) -> "ReplayResult":
        """Run the W37-3 bias / leakage detector against the replay output.

        Called from :meth:`replay` after :meth:`_compute_metrics` returns
        so every historical-replay backtest automatically surfaces its
        bias findings (rule ids ``BL_01``..``BL_10``) on the
        :attr:`ReplayResult.bias_report` field. The detector is
        imported lazily so a broken ``backtesting.bias_detector`` import
        (e.g. a missing optional dep) degrades gracefully — the replay
        still returns its metrics, with ``bias_report = {}``.

        Critical findings (look-ahead / data leakage / hindsight / etc.)
        are logged at ``ERROR`` level so the operator can correlate
        via the X-Request-ID response header. The backtest itself is
        NOT aborted — the caller decides whether to discard the result
        based on the ``has_critical`` flag in :attr:`bias_report`.
        """
        try:
            from backtesting.bias_detector import bias_detector
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "bias_detector unavailable — ReplayResult.bias_report will "
                "be empty (import error: %s)", exc,
            )
            return result

        # Convert the snapshots list to the dict shape the bias detector
        # expects (``{timestamp, token_id, best_bid, best_ask}``).
        order_books: list[dict[str, Any]] = [
            {
                "timestamp": float(snap.timestamp),
                "token_id": str(snap.token_id),
                "best_bid": float(snap.best_bid),
                "best_ask": float(snap.best_ask),
            }
            for snap in snapshots
        ]

        backtest_payload: dict[str, Any] = {
            "token_id": token_id,
            "trades": trades,
            "order_books": order_books,
        }

        try:
            report = bias_detector.analyze(backtest_payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.error(
                "bias_detector.analyze() raised — ReplayResult.bias_report "
                "will be empty (error: %s)", exc, exc_info=True,
            )
            return result

        if report.has_critical:
            logger.error(
                "CRITICAL bias detected in historical replay (token=%s): "
                "%s — backtest is structurally unreliable",
                token_id,
                [f.rule for f in report.critical_findings],
            )

        result.bias_report = report.to_dict()
        return result

    # ── Risk metrics ───────────────────────────────────────────────────

    @staticmethod
    def _compute_metrics(
        start_time: float,
        end_time: float,
        n_snapshots: int,
        trades: list[dict],
        equity_curve: list[float],
    ) -> ReplayResult:
        """Compute total return / Sharpe / max drawdown / win rate / PF.

        Mirrors the metric block in ``backtesting/advanced.py::
        _simulate_equity`` (annualisation ``sqrt(252)``) so the values
        are directly comparable to the walk-forward Sharpe. ``equity``
        is converted to a ``np.ndarray`` up front so the diff/peak ops
        vectorise.
        """
        equity = np.asarray(equity_curve, dtype=float)
        # Total return: ``final / initial - 1``. Guard against a zero
        # initial equity (would only happen if ``initial_capital = 0``,
        # which the API route forbids via Pydantic ``ge=1.0`` but the
        # direct engine.replay() call doesn't).
        if equity.size >= 2 and equity[0] > 0:
            total_return = float(equity[-1] / equity[0] - 1.0)
        else:
            total_return = 0.0

        # Per-step returns for Sharpe.
        if equity.size >= 2:
            # ``np.diff(equity) / equity[:-1]`` is undefined when the
            # prior step is zero — guard with a positive floor (same
            # convention as ``advanced.py::_simulate_equity``).
            safe_denom = np.where(
                np.abs(equity[:-1]) < 1e-8, 1e-8, equity[:-1]
            )
            returns = np.diff(equity) / safe_denom
        else:
            returns = np.array([0.0])

        std = float(np.std(returns))
        if returns.size > 0 and std > 1e-12:
            sharpe = float(np.mean(returns) / (std + 1e-8) * np.sqrt(252))
        else:
            sharpe = 0.0

        # Max drawdown: ``max((peak - val) / peak)`` over the curve.
        peak = np.maximum.accumulate(equity)
        # Guard against a zero / negative peak (only possible if equity
        # went negative — clip to avoid divide-by-zero).
        safe_peak = np.where(peak > 0, peak, 1e-8)
        drawdowns = (peak - equity) / safe_peak
        max_dd = float(np.max(drawdowns)) if drawdowns.size > 0 else 0.0

        # Win rate + profit factor over CLOSED trades (BUY then SELL
        # counts as one closed trade; an unclosed BUY at end of window
        # was force-closed above so every BUY has a matching SELL).
        sell_trades = [t for t in trades if t.get("action") == "SELL"]
        wins = [t for t in sell_trades if float(t.get("pnl", 0.0)) > 0]
        losses = [t for t in sell_trades if float(t.get("pnl", 0.0)) < 0]
        win_rate = (len(wins) / len(sell_trades)) if sell_trades else 0.0

        gross_profit = float(sum(t["pnl"] for t in wins)) if wins else 0.0
        gross_loss = float(abs(sum(t["pnl"] for t in losses))) if losses else 0.0
        if gross_loss > 1e-12:
            profit_factor = gross_profit / (gross_loss + 1e-8)
        else:
            # No losing trades — convention: report ``999.0`` as a
            # sentinel "inf-like" value (matches the original spec).
            profit_factor = 999.0 if gross_profit > 0 else 0.0

        # Cap at 999.0 so downstream JSON serialisation never emits ``inf``
        # (which is invalid JSON).
        profit_factor = float(min(profit_factor, 999.0))

        return ReplayResult(
            start_time=start_time,
            end_time=end_time,
            n_snapshots=n_snapshots,
            trades=trades,
            equity_curve=[float(x) for x in equity_curve],
            total_return=total_return,
            sharpe=sharpe,
            max_drawdown=max_dd,
            win_rate=float(win_rate),
            profit_factor=profit_factor,
        )


# ── Module-level convenience ────────────────────────────────────────────────


def _default_db_path() -> str:
    """Resolve the default ``market_snapshots`` DB path from env.

    Mirrors the env-var resolution in ``core/timescale_db.py`` (``MARKET_DB_PATH``
    env var → ``/app/data/market_intelligence.db`` fallback) so the
    engine + API route agree on the canonical path.
    """
    return os.environ.get(
        "MARKET_DB_PATH", "/app/data/market_intelligence.db"
    )
