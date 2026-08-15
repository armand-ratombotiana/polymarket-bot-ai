"""
paper/simulator.py — Paper trading simulator.
Simulates order fills against live order book data without touching real funds.
Activated when PAPER_TRADE=true in .env.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from core.data_store import Order, OrderBook, OrderStatus, Side, Trade, store
from core.clob_client import OrderArgs

log = logging.getLogger(__name__)


class PaperSimulator:
    """
    Intercepts order creation/cancellation when in paper-trade mode.
    Simulates fills by checking orders against the live order book.
    Runs a periodic fill-check loop in the background.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._virtual_balance_usdc: float = 10000.0  # starting paper capital ($10,000 USD account)

    @property
    def virtual_balance(self) -> float:
        return self._virtual_balance_usdc

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._fill_loop(), name="paper-sim")
        log.info("Paper trading simulator started (virtual balance: $%.2f)", self._virtual_balance_usdc)
        await store.log_event(f"📄 Paper mode — virtual balance ${self._virtual_balance_usdc:,.2f}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def create_order(self, args: OrderArgs, strategy: str = "") -> Order:
        """
        Create a simulated order, add it to the data store, and return it.
        No real API call is made.
        """
        order = Order(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            strategy=strategy,
            paper=True,
        )
        await store.add_order(order)
        await store.log_event(
            f"📄 Paper order: {args.side.value} {args.size:.2f} @ {args.price:.4f} [{args.token_id[:8]}…]"
        )
        log.info(
            "Paper order created: %s %s %.2f @ %.4f",
            order.order_id, args.side.value, args.size, args.price,
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        order = await store.update_order(order_id, status=OrderStatus.CANCELLED)
        if order:
            await store.log_event(f"📄 Paper cancel: {order_id}")
        return order is not None

    async def cancel_all(self) -> int:
        cancelled = await store.cancel_all_orders()
        await store.log_event(f"📄 Paper cancel-all: {len(cancelled)} orders cancelled")
        return len(cancelled)

    # ── Fill simulation loop ──────────────────────────────────────────────

    async def _fill_loop(self) -> None:
        """Check open paper orders against the live book every second."""
        while self._running:
            try:
                await self._try_fill_orders()
            except Exception as e:
                log.debug("Fill-loop error: %s", e)
            await asyncio.sleep(1.0)

    async def _try_fill_orders(self) -> None:
        open_orders = await store.get_open_orders()
        for order in open_orders:
            if not order.paper:
                continue
            book = await store.get_order_book(order.token_id)
            if book is None:
                continue
            filled = self._can_fill(order, book)
            if filled:
                await self._execute_fill(order, filled)

    def _can_fill(self, order: Order, book: OrderBook) -> Optional[float]:
        """
        Return the fill price if the order can be matched, else None.
        BUY orders fill if best_ask <= order.price
        SELL orders fill if best_bid >= order.price
        """
        if order.side == Side.BUY:
            if book.best_ask is not None and book.best_ask <= order.price:
                return book.best_ask
        else:
            if book.best_bid is not None and book.best_bid >= order.price:
                return book.best_bid
        return None

    async def _execute_fill(self, order: Order, fill_price: float) -> None:
        fill_size = order.size_remaining
        revenue_or_cost = fill_price * fill_size

        # Update virtual balance
        if order.side == Side.BUY:
            self._virtual_balance_usdc -= revenue_or_cost
        else:
            self._virtual_balance_usdc += revenue_or_cost

        # Compute simple P&L for SELL (revenue - cost basis)
        pos = store.positions.get(order.token_id)
        pnl = 0.0
        if order.side == Side.SELL and pos and pos.avg_entry_price > 0:
            pnl = (fill_price - pos.avg_entry_price) * fill_size

        trade = Trade(
            trade_id=f"paper-fill-{uuid.uuid4().hex[:8]}",
            token_id=order.token_id,
            side=order.side,
            price=fill_price,
            size=fill_size,
            strategy=order.strategy,
            paper=True,
            pnl=pnl,
        )
        await store.record_fill(trade)
        await store.update_order(order.order_id, status=OrderStatus.FILLED, size_matched=order.size)
        await store.log_event(
            f"✅ Paper fill: {order.side.value} {fill_size:.2f} @ {fill_price:.4f} "
            f"[P&L: ${pnl:+.2f}] [{order.token_id[:8]}…]"
        )


# Module-level singleton
paper_sim = PaperSimulator()
