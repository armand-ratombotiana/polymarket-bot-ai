"""
core/data_store.py — In-memory state store with atomic disk persistence.
Thread-safe via asyncio locks. Tracks books, orders, positions, trades, events, and equity curve.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE = Path(os.environ.get("STORE_STATE_PATH", "/app/data/store_state.json"))

# Single source of truth for the operating bankroll/equity baseline so
# accounting, risk limits, and paper simulation all reference the same
# starting capital. Operating capital: USD 100.00; hard ceiling: USD 200.00
# (never auto-increased). Automated sizing operates from USD 100 only.
BANKROLL_BASELINE = 100.0


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[PriceLevel] = field(default_factory=list)   # best bid first
    asks: list[PriceLevel] = field(default_factory=list)   # best ask first
    updated_at: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Order:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    size_matched: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = field(default_factory=time.time)
    strategy: str = ""
    paper: bool = False
    # R11 — Unified Decision Ledger linkage. Empty string for legacy / manual
    # orders; populated by strategies/signal_trader → strategies/base →
    # paper/simulator so the full PREDICTION → SIGNAL → RISK_* → ORDER → FILL
    # chain can be reconstructed via core/decision_ledger.
    decision_id: str = ""

    @property
    def size_remaining(self) -> float:
        return self.size - self.size_matched

    @property
    def cost_usdc(self) -> float:
        return self.price * self.size


@dataclass
class Position:
    token_id: str
    market_slug: str = ""
    yes_shares: float = 0.0   # positive = long YES
    no_shares: float = 0.0    # positive = long NO
    avg_entry_price: float = 0.0
    total_invested: float = 0.0
    realised_pnl: float = 0.0
    strategy: str = ""
    opened_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    @property
    def current_exposure(self) -> float:
        """Capital at risk = cost basis of remaining shares."""
        return self.yes_shares * self.avg_entry_price

    @property
    def exposure_duration_hours(self) -> float:
        return max(0.0, (self.last_updated - self.opened_at) / 3600.0)

    @property
    def exposure_dollar_days(self) -> float:
        return self.current_exposure * (self.exposure_duration_hours / 24.0)


@dataclass
class Trade:
    trade_id: str
    token_id: str
    side: Side
    price: float
    size: float
    fee: float = 0.0
    timestamp: float = field(default_factory=time.time)
    strategy: str = ""
    paper: bool = False
    pnl: float = 0.0


class DataStore:
    """Central state store shared across all modules with disk persistence."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

        # Market state
        self.order_books: dict[str, OrderBook] = {}
        self.market_slugs: dict[str, str] = {}  # token_id -> slug

        # Order management
        self.open_orders: dict[str, Order] = {}   # order_id -> Order
        self.order_history: list[Order] = []

        # Positions
        self.positions: dict[str, Position] = {}  # token_id -> Position

        # Trades & P&L ($10,000 USD Institutional Bankroll)
        self.trades: list[Trade] = []
        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.week_window_started_at: float = time.time()
        self.paper_balance: float = BANKROLL_BASELINE
        self.peak_equity: float = BANKROLL_BASELINE
        self.session_start: float = time.time()

        # Equity curve time-series (timestamp, equity, pnl)
        self.equity_history: list[dict[str, float]] = [
            {"timestamp": time.time(), "equity": BANKROLL_BASELINE, "pnl": 0.0}
        ]

        # Risk
        self.kill_switch_active: bool = False

        # Events log (max 500 entries)
        self.event_log: list[str] = []

        # W22-7 - bot-level error / action time-series for the canonical
        # bot.errors / bot.actions observability metrics (God Mode §54).
        # Each entry is the unix timestamp of the event so a windowed count
        # is a single list comprehension. Cap at 10k entries guards against
        # a runaway loop filling memory if the collector stops.
        self._errors: list[float] = []
        self._actions: list[float] = []
        self._ERROR_CAP: int = 10_000
        self._ACTION_CAP: int = 10_000

    # ── Order Book ───────────────────────────────────────────────────────

    async def update_order_book(self, book: OrderBook) -> None:
        async with self._lock:
            self.order_books[book.token_id] = book

    async def get_order_book(self, token_id: str) -> OrderBook | None:
        async with self._lock:
            return self.order_books.get(token_id)

    # ── Orders ───────────────────────────────────────────────────────────

    async def add_order(self, order: Order) -> None:
        async with self._lock:
            self.open_orders[order.order_id] = order
        # W23-3 — broadcast the placement on the ``orders`` WS channel so
        # any dashboard / monitor subscribed to "orders" sees the new
        # open order immediately rather than waiting for the next 1s
        # snapshot tick. Defensive: a broadcast failure must never break
        # the data path (the order has already been recorded).
        await self._broadcast_orders("placed")

    async def update_order(self, order_id: str, **kwargs) -> Order | None:
        async with self._lock:
            order = self.open_orders.get(order_id)
            if order is None:
                return None
            for k, v in kwargs.items():
                setattr(order, k, v)
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                self.order_history.append(order)
                del self.open_orders[order_id]
        # W23-3 — lock released before the broadcast so the I/O doesn't
        # serialise other mutations. Only the success path (order was
        # found and mutated) reaches here — the ``return None`` above
        # exits the coroutine before this line.
        await self._broadcast_orders("updated")
        return order

    async def cancel_all_orders(self) -> list[Order]:
        async with self._lock:
            cancelled = list(self.open_orders.values())
            for o in cancelled:
                o.status = OrderStatus.CANCELLED
                self.order_history.append(o)
            self.open_orders.clear()
        # W23-3 — lock released before the broadcast so the I/O doesn't
        # serialise other mutations. ``cancelled`` is captured under the
        # lock so the returned list reflects the pre-clear state.
        await self._broadcast_orders("cancelled")
        return cancelled

    async def get_open_orders(self) -> list[Order]:
        async with self._lock:
            return list(self.open_orders.values())

    async def open_order_count(self) -> int:
        async with self._lock:
            return len(self.open_orders)

    async def total_exposure(self) -> float:
        async with self._lock:
            return sum(p.current_exposure for p in self.positions.values())

    async def exposure_for_market(self, token_id: str) -> float:
        async with self._lock:
            p = self.positions.get(token_id)
            return p.current_exposure if p else 0.0

    # W20-4 — portfolio-optimizer helper. Returns the current open positions
    # as a list of plain dicts (``{"token_id": str, "size_usdc": float}``) so
    # the live rebalance endpoint (``GET /api/portfolio/rebalance/live``) and
    # the in-process ``signal_trader._process_signals`` path can pass them
    # straight to :meth:`PortfolioOptimizer.suggest_rebalance` /
    # :meth:`PortfolioOptimizer.optimize` without re-shaping. ``size_usdc``
    # uses the position's ``current_exposure`` (cost basis of remaining
    # shares), which is the same figure the optimizer treats as the
    # ``size_usdc`` field of its ``add`` / ``reduce`` / ``close`` /
    # ``hold`` output.
    async def get_positions(self) -> list[dict]:
        """Return the live open positions as ``portfolio_optimizer``-shaped dicts.

        Each entry is ``{"token_id": str, "size_usdc": float}``. The list is
        a snapshot taken under the store lock so concurrent fills during the
        iteration cannot mutate the underlying dict mid-loop. Empty when no
        positions are open.
        """
        async with self._lock:
            return [
                {
                    "token_id": tid,
                    "size_usdc": float(p.current_exposure),
                    "strategy": p.strategy,
                    "avg_entry_price": float(p.avg_entry_price),
                    "yes_shares": float(p.yes_shares),
                    "no_shares": float(p.no_shares),
                    "total_invested": float(p.total_invested),
                    "realised_pnl": float(p.realised_pnl),
                }
                for tid, p in self.positions.items()
            ]

    # ── Positions & Trades ───────────────────────────────────────────────

    async def record_fill(self, trade: Trade) -> None:
        async with self._lock:
            self.trades.append(trade)
            self.daily_pnl += trade.pnl
            self.weekly_pnl += trade.pnl
            self.paper_balance += (
                trade.price * trade.size * (1.0 if trade.side == Side.SELL else -1.0)
            )

            pos = self.positions.setdefault(
                trade.token_id,
                Position(token_id=trade.token_id),
            )
            if not pos.strategy and trade.strategy:
                pos.strategy = trade.strategy
            pos.last_updated = time.time()
            if trade.side == Side.BUY:
                cost = trade.price * trade.size
                total_shares = pos.yes_shares + trade.size
                if total_shares > 0:
                    pos.avg_entry_price = (
                        pos.avg_entry_price * pos.yes_shares + cost
                    ) / total_shares
                pos.yes_shares = total_shares
                pos.total_invested += cost
            else:
                revenue = trade.price * trade.size
                pos.yes_shares = max(0.0, pos.yes_shares - trade.size)
                pos.total_invested = max(0.0, pos.total_invested - revenue)
                pos.realised_pnl += trade.pnl

            # Record point in equity history
            current_eq = BANKROLL_BASELINE + self.daily_pnl
            self.peak_equity = max(self.peak_equity, current_eq)
            self.equity_history.append({
                "timestamp": time.time(),
                "equity": round(current_eq, 2),
                "pnl": round(self.daily_pnl, 2),
            })
            if len(self.equity_history) > 300:
                self.equity_history = self.equity_history[-300:]
        # W23-3 — broadcast the trade fill on the ``trades`` channel AND
        # the resulting position update on the ``positions`` channel.
        # Lazy-import + try/except so a broadcast failure (no event loop,
        # ``ws_broadcast`` import broken, …) can NEVER break the data
        # path — the trade has already been booked by this point. The
        # snapshots are taken AFTER the lock is released so the broadcast
        # I/O doesn't serialise other mutations; the position / trade
        # snapshots are read back under a fresh lock acquisition so the
        # broadcast reflects the post-fill state.
        await self._broadcast_trade_fill(trade)
        await self._broadcast_positions("update")

    # ── W23-3 — WebSocket broadcast helpers ──────────────────────────────
    #
    # Each helper snapshots the relevant state under ``self._lock`` and
    # pushes it on the matching ``ws_manager`` channel. They are
    # DEFENSIVE: a lazy-import failure or send error is swallowed at
    # debug level so a broadcast hiccup never breaks the data path.
    # Called from ``add_order`` / ``update_order`` / ``cancel_all_orders``
    # / ``record_fill`` AFTER the mutation has been committed and the
    # lock released.
    #
    # The lazy ``from core.ws_broadcast import ws_manager`` keeps the
    # data_store module importable even when the broadcast subsystem
    # is unavailable (it never is in practice — same package — but the
    # pattern is consistent with how every other cross-subsystem import
    # in ``core.data_store`` is handled, e.g. ``core.decision_ledger``
    # inside ``paper/simulator._execute_fill``).

    async def _broadcast_orders(self, event_type: str = "update") -> None:
        """Broadcast the current open-orders state on the ``orders`` channel.

        ``event_type`` is one of ``placed`` / ``updated`` / ``cancelled``
        / ``filled`` — surfaced in the envelope's ``data.type`` field so
        a subscriber can distinguish a new-placement from a cancellation
        without diffing the order list.
        """
        try:
            from core.ws_broadcast import ws_manager

            async with self._lock:
                orders_payload = [
                    {
                        "order_id": o.order_id,
                        "token_id": o.token_id,
                        "side": o.side.value,
                        "price": float(o.price),
                        "size": float(o.size),
                        "size_matched": float(o.size_matched),
                        "status": o.status.value,
                        "strategy": o.strategy,
                        "paper": bool(o.paper),
                        "created_at": float(o.created_at),
                        "decision_id": o.decision_id,
                    }
                    for o in self.open_orders.values()
                ]
            await ws_manager.broadcast(
                "orders",
                {"type": event_type, "orders": orders_payload},
            )
        except Exception as e:  # noqa: BLE001 — broadcast must never break the data path
            log.debug("[data_store] orders broadcast failed: %s", e)

    async def _broadcast_positions(self, event_type: str = "update") -> None:
        """Broadcast the current positions state on the ``positions`` channel.

        Reuses the same dict shape as :meth:`get_positions` (the
        portfolio-optimizer contract) so a subscriber can parse the
        payload without a per-channel adapter.
        """
        try:
            from core.ws_broadcast import ws_manager

            positions_payload = await self.get_positions()
            await ws_manager.broadcast(
                "positions",
                {"type": event_type, "positions": positions_payload},
            )
        except Exception as e:  # noqa: BLE001 — broadcast must never break the data path
            log.debug("[data_store] positions broadcast failed: %s", e)

    async def _broadcast_trade_fill(self, trade: Trade) -> None:
        """Broadcast a single trade fill on the ``trades`` channel.

        The payload mirrors the shape emitted by the existing
        ``_build_snapshot`` ``recent_trades`` entry so a subscriber can
        reuse the same parser for both the 1s snapshot and the per-fill
        push.
        """
        try:
            from core.ws_broadcast import ws_manager

            trade_payload = {
                "trade_id": trade.trade_id,
                "token_id": trade.token_id,
                "slug": self.market_slugs.get(trade.token_id, trade.token_id[:14]),
                "side": trade.side.value,
                "price": float(trade.price),
                "size": float(trade.size),
                "pnl": float(trade.pnl),
                "strategy": trade.strategy,
                "paper": bool(trade.paper),
                "timestamp": float(trade.timestamp),
            }
            await ws_manager.broadcast(
                "trades",
                {"type": "fill", "trade": trade_payload},
            )
        except Exception as e:  # noqa: BLE001 — broadcast must never break the data path
            log.debug("[data_store] trades broadcast failed: %s", e)

    # ── Weekly Loss Window (P0-GOV-01) ─────────────────────────────────

    def roll_weekly_window(self) -> None:
        """Roll the 7-day PnL window; resets weekly_pnl when the window expires."""
        WEEK_SECONDS = 7 * 24 * 3600
        now = time.time()
        if now - self.week_window_started_at >= WEEK_SECONDS:
            self.weekly_pnl = 0.0
            self.week_window_started_at = now

    def weekly_pnl_snapshot(self) -> dict:
        self.roll_weekly_window()
        return {
            "weekly_pnl": round(self.weekly_pnl, 2),
            "window_started_at": self.week_window_started_at,
            "window_remaining_seconds": max(0.0, 7 * 24 * 3600 - (time.time() - self.week_window_started_at)),
        }

    # ── Event Log ────────────────────────────────────────────────────────

    async def log_event(self, msg: str) -> None:
        async with self._lock:
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            self.event_log.append(entry)
            if len(self.event_log) > 500:
                self.event_log.pop(0)

    async def get_recent_events(self, n: int = 20) -> list[str]:
        async with self._lock:
            return list(self.event_log[-n:])

    # ── W22-7 - Bot error / action counters ─────────────────────────────

    async def record_error(self) -> None:
        """Append time.time() to the bot-level error timestamp series.

        Used by the observability collector (via get_error_count_since)
        to emit the canonical bot.errors metric (God Mode §54). The cap
        (_ERROR_CAP = 10k entries) drops the oldest 10% when reached.
        """
        async with self._lock:
            self._errors.append(time.time())
            if len(self._errors) > self._ERROR_CAP:
                drop = max(1, self._ERROR_CAP // 10)
                self._errors = self._errors[drop:]

    async def record_action(self) -> None:
        """Append time.time() to the bot-level action timestamp series.

        Used by the observability collector (via get_action_count_since)
        to emit the canonical bot.actions metric (God Mode §54). An
        "action" is any meaningful bot-level decision (signal / order /
        fill). Same cap policy as record_error.
        """
        async with self._lock:
            self._actions.append(time.time())
            if len(self._actions) > self._ACTION_CAP:
                drop = max(1, self._ACTION_CAP // 10)
                self._actions = self._actions[drop:]

    def get_error_count_since(self, since_ts: float) -> int:
        """Count error events with timestamp >= since_ts.

        Sync - pure list-filter read, no I/O. Lock-free is safe because
        the worst race is a concurrent record_error appending a single
        entry that we may or may not see (both outcomes are valid
        counts; the next collector cycle picks up the new entry).
        """
        return sum(1 for ts in self._errors if ts >= since_ts)

    def get_action_count_since(self, since_ts: float) -> int:
        """Count action events with timestamp >= since_ts."""
        return sum(1 for ts in self._actions if ts >= since_ts)

    # ── Disk Persistence ─────────────────────────────────────────────────

    def save_to_disk(self) -> None:
        """Atomic write of portfolio state and equity history to disk."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = STATE_FILE.with_suffix(".tmp")
        try:
            data = {
                "bankroll_baseline": BANKROLL_BASELINE,
                "daily_pnl": self.daily_pnl,
                "paper_balance": self.paper_balance,
                "peak_equity": self.peak_equity,
                "equity_history": self.equity_history,
                "positions": {
                    tid: {
                        "token_id": p.token_id,
                        "market_slug": p.market_slug,
                        "yes_shares": p.yes_shares,
                        "avg_entry_price": p.avg_entry_price,
                        "total_invested": p.total_invested,
                        "realised_pnl": p.realised_pnl,
                        "strategy": p.strategy,
                        "opened_at": p.opened_at,
                        "last_updated": p.last_updated,
                    }
                    for tid, p in self.positions.items()
                },
                "trades": [
                    {
                        "trade_id": t.trade_id,
                        "token_id": t.token_id,
                        "side": t.side.value,
                        "price": t.price,
                        "size": t.size,
                        "pnl": t.pnl,
                        "strategy": t.strategy,
                        "paper": t.paper,
                        "timestamp": t.timestamp,
                    }
                    for t in self.trades
                ],
            }
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(STATE_FILE)
            log.debug("Saved DataStore state to %s", STATE_FILE)
        except Exception as e:
            log.error("Failed to save DataStore state: %s", e)

    def load_from_disk(self) -> None:
        """Load persistent state on boot if present."""
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.daily_pnl = float(data.get("daily_pnl", 0.0))
            self.paper_balance = float(data.get("paper_balance", BANKROLL_BASELINE))
            raw_peak = float(data.get("peak_equity", 0.0))
            persisted_baseline = float(data.get("bankroll_baseline", BANKROLL_BASELINE))
            # If the bankroll baseline scale changed (operating capital re-approval),
            # the high-water mark must be re-based to the current equity; otherwise
            # the legacy $10k/$200-era peak would fabricate a false drawdown.
            if persisted_baseline != BANKROLL_BASELINE:
                self.peak_equity = BANKROLL_BASELINE + self.daily_pnl
                log.warning(
                    "[data_store] Bankroll baseline changed $%.2f -> $%.2f — high-water mark "
                    "re-based to current equity $%.2f (no fabricated drawdown).",
                    persisted_baseline, BANKROLL_BASELINE, self.peak_equity,
                )
            else:
                self.peak_equity = (
                    max(raw_peak, BANKROLL_BASELINE + self.daily_pnl)
                    if raw_peak > 0.0
                    else BANKROLL_BASELINE + self.daily_pnl
                )
            self.equity_history = data.get("equity_history", self.equity_history)

            raw_pos = data.get("positions", {})
            for tid, pdict in raw_pos.items():
                self.positions[tid] = Position(
                    token_id=tid,
                    market_slug=pdict.get("market_slug", ""),
                    yes_shares=float(pdict.get("yes_shares", 0.0)),
                    avg_entry_price=float(pdict.get("avg_entry_price", 0.0)),
                    total_invested=float(pdict.get("total_invested", 0.0)),
                    realised_pnl=float(pdict.get("realised_pnl", 0.0)),
                    strategy=pdict.get("strategy", ""),
                    opened_at=float(pdict.get("opened_at", time.time())),
                    last_updated=float(pdict.get("last_updated", time.time())),
                )

            raw_trades = data.get("trades", [])
            for tdict in raw_trades:
                self.trades.append(Trade(
                    trade_id=tdict["trade_id"],
                    token_id=tdict["token_id"],
                    side=Side.BUY if tdict["side"].upper() == "BUY" else Side.SELL,
                    price=float(tdict["price"]),
                    size=float(tdict["size"]),
                    pnl=float(tdict.get("pnl", 0.0)),
                    strategy=tdict.get("strategy", ""),
                    paper=tdict.get("paper", True),
                    timestamp=float(tdict.get("timestamp", time.time())),
                ))

            # Recompute ledger-derived figures from the trade log only when the
            # persisted cumulative values are absent (e.g. brand-new state). The
            # persisted daily_pnl / paper_balance are the authoritative cumulative
            # figures; if the in-memory trade log no longer sums to them (legacy
            # truncation), we keep the authoritative values and flag divergence
            # for the reconciliation report instead of silently rewriting P&L.
            sum_trade_pnl = sum(t.pnl for t in self.trades)
            if self.trades and abs(self.daily_pnl) < 1e-9 and abs(sum_trade_pnl) > 1e-9:
                self.daily_pnl = sum_trade_pnl
                self.paper_balance = BANKROLL_BASELINE + sum(
                    t.price * t.size * (1.0 if t.side == Side.SELL else -1.0)
                    for t in self.trades
                )
            if self.trades and abs(sum_trade_pnl - self.daily_pnl) > 0.01:
                log.warning(
                    "[data_store] Trade log pnl ($%.2f) diverges from persisted daily_pnl ($%.2f) — legacy "
                    "truncated history; authoritative cumulative P&L retained.",
                    sum_trade_pnl, self.daily_pnl,
                )

            log.info("Loaded DataStore state from disk: daily_pnl=$%.2f, %d positions, %d trades",
                     self.daily_pnl, len(self.positions), len(self.trades))
        except Exception as e:
            log.warning("Could not load DataStore state from %s: %s", STATE_FILE, e)


# Global singleton
store = DataStore()
store.load_from_disk()
