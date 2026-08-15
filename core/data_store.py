"""
core/data_store.py — In-memory state store for order books, positions, orders, and P&L.
Thread-safe via asyncio locks.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


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
    bids: List[PriceLevel] = field(default_factory=list)   # best bid first
    asks: List[PriceLevel] = field(default_factory=list)   # best ask first
    updated_at: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
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

    @property
    def current_exposure(self) -> float:
        return self.total_invested


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
    """Central in-memory state store shared across all modules."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

        # Market state
        self.order_books: Dict[str, OrderBook] = {}
        self.market_slugs: Dict[str, str] = {}  # token_id -> slug

        # Order management
        self.open_orders: Dict[str, Order] = {}   # order_id -> Order
        self.order_history: List[Order] = []

        # Positions
        self.positions: Dict[str, Position] = {}  # token_id -> Position

        # Trades & P&L
        self.trades: List[Trade] = []
        self.daily_pnl: float = 0.0
        self.session_start: float = time.time()

        # Risk
        self.kill_switch_active: bool = False

        # Events log (max 500 entries)
        self.event_log: List[str] = []

    # ── Order Book ───────────────────────────────────────────────────────

    async def update_order_book(self, book: OrderBook) -> None:
        async with self._lock:
            self.order_books[book.token_id] = book

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        async with self._lock:
            return self.order_books.get(token_id)

    # ── Orders ───────────────────────────────────────────────────────────

    async def add_order(self, order: Order) -> None:
        async with self._lock:
            self.open_orders[order.order_id] = order

    async def update_order(self, order_id: str, **kwargs) -> Optional[Order]:
        async with self._lock:
            order = self.open_orders.get(order_id)
            if order is None:
                return None
            for k, v in kwargs.items():
                setattr(order, k, v)
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                self.order_history.append(order)
                del self.open_orders[order_id]
            return order

    async def cancel_all_orders(self) -> List[Order]:
        async with self._lock:
            cancelled = list(self.open_orders.values())
            for o in cancelled:
                o.status = OrderStatus.CANCELLED
                self.order_history.append(o)
            self.open_orders.clear()
            return cancelled

    async def get_open_orders(self) -> List[Order]:
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

    # ── Positions ────────────────────────────────────────────────────────

    async def record_fill(self, trade: Trade) -> None:
        async with self._lock:
            self.trades.append(trade)
            self.daily_pnl += trade.pnl

            pos = self.positions.setdefault(
                trade.token_id,
                Position(token_id=trade.token_id),
            )
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

    # ── Event Log ────────────────────────────────────────────────────────

    async def log_event(self, msg: str) -> None:
        async with self._lock:
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            self.event_log.append(entry)
            if len(self.event_log) > 500:
                self.event_log.pop(0)

    async def get_recent_events(self, n: int = 20) -> List[str]:
        async with self._lock:
            return list(self.event_log[-n:])


# Global singleton
store = DataStore()
