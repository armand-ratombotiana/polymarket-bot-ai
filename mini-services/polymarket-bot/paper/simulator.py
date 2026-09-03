"""
paper/simulator.py — Paper trading simulator.
Simulates order fills against live order book data without touching real funds.
Activated when PAPER_TRADE=true in .env.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid

from core.clob_client import OrderArgs
from core.data_store import (
    Order,
    OrderBook,
    OrderStatus,
    Side,
    Trade,
    store,
)

log = logging.getLogger(__name__)

# ── Slippage model constants ────────────────────────────────────────────────
# Polymarket minimum tick is 1¢ (0.01) for prices in [0.05, 0.95]; this is the
# granularity at which every microstructure penalty below is expressed.
TICK_SIZE = 0.01
# Shares of order size in excess of top-of-book depth that map to one 0.5-tick
# increment of size-impact slippage. Tuned so a 100-share order crossing a
# 0-share top of book pays 1.0 tick of size impact (0.5 * 2 buckets).
SLIPPAGE_DEPTH_BUCKET = 50.0
# Valid Polymarket price bounds — slipped fill prices are clamped into this
# range so the simulator never produces an out-of-market quote.
_MIN_PRICE = 0.01
_MAX_PRICE = 0.99


class PaperSimulator:
    """
    Intercepts order creation/cancellation when in paper-trade mode.
    Simulates fills by checking orders against the live order book.
    Runs a periodic fill-check loop in the background.
    """

    # Slippage model parameters (overridable per-instance for tests/sensitivity
    # analysis). Defaults mirror the module-level constants above.
    TICK_SIZE: float = TICK_SIZE
    SLIPPAGE_DEPTH_BUCKET: float = SLIPPAGE_DEPTH_BUCKET

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._virtual_balance_usdc: float = store.paper_balance  # persists via DataStore

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

    async def create_order(
        self, args: OrderArgs, strategy: str = "", decision_id: str = ""
    ) -> Order:
        """
        Create a simulated order, add it to the data store, and return it.
        No real API call is made.

        ``decision_id`` (R11) is propagated to the resulting ``Order`` so the
        downstream fill loop can record a FILL stage against the originating
        PREDICTION → SIGNAL → RISK_APPROVED chain in the decision ledger.
        """
        order = Order(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            strategy=strategy,
            paper=True,
            decision_id=decision_id,
        )
        await store.add_order(order)
        await store.log_event(
            f"📄 Paper order: {args.side.value} {args.size:.2f} @ {args.price:.4f} [{args.token_id[:8]}…]"
        )
        log.info(
            "Paper order created: %s %s %.2f @ %.4f",
            order.order_id, args.side.value, args.size, args.price,
        )
        # R11 — record the ORDER stage so the decision chain has an explicit
        # marker between RISK_APPROVED and FILL (and so cancelled / unfilled
        # paper orders are visible in the ledger).
        if decision_id:
            try:
                from core.decision_ledger import decision_ledger
                await decision_ledger.record(
                    decision_id=decision_id,
                    stage="ORDER",
                    token_id=order.token_id,
                    strategy=order.strategy,
                    pnl=0.0,
                    order_id=order.order_id,
                    side=order.side.value,
                    price=order.price,
                    size=order.size,
                    paper=True,
                )
            except Exception as e:
                log.debug("[paper_sim] ledger ORDER record failed: %s", e)
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
            raw_price = self._can_fill(order, book)
            if raw_price is None:
                continue
            # Apply realistic crossing/size/queue slippage before booking the
            # fill so recorded P&L reflects taker-style execution cost.
            fill_price = self._apply_slippage(order, raw_price, book)
            await self._execute_fill(order, fill_price)

    @staticmethod
    def _apply_slippage(order: Order, raw_price: float, book: OrderBook) -> float:
        """
        Model realistic execution slippage for a paper order crossing the book.

        Three additive components, all expressed in ticks of the raw fill price:
          1. Crossing penalty — flat 1 tick. Proxy for the taker fee / adverse
             selection paid when crossing the spread to lift the offer or hit
             the bid.
          2. Size impact — 0.5 tick per `SLIPPAGE_DEPTH_BUCKET` shares of order
             size in excess of the available top-of-book depth. Linear market-
             impact curve: orders small enough to be absorbed by the resting
             top level pay no size impact; deeper sweeps walk the book.
          3. Queue position — deterministic 0 or 1 tick derived from a stable
             SHA-256 hash of `order.order_id`, so a given order always sees
             the same queue penalty across simulator runs (reproducible P&L).

        Slippage is adverse to the order direction:
          - BUY  -> price increases (worse entry)
          - SELL -> price decreases (worse exit)

        The final price is clamped to the valid [0.01, 0.99] trading range.
        """
        tick = PaperSimulator.TICK_SIZE

        # 1. Crossing penalty: flat 1 tick.
        crossing_ticks = 1.0

        # 2. Size impact: depth available at top of book vs. order size.
        if order.side == Side.BUY:
            top_depth = book.asks[0].size if book.asks else 0.0
        else:
            top_depth = book.bids[0].size if book.bids else 0.0
        overflow = max(0.0, order.size - top_depth)
        size_impact_ticks = (overflow / PaperSimulator.SLIPPAGE_DEPTH_BUCKET) * 0.5

        # 3. Queue position: deterministic 0 or 1 tick from a stable order_id hash.
        order_hash_byte = hashlib.sha256(order.order_id.encode("utf-8")).digest()[0]
        queue_ticks = float(order_hash_byte & 0x01)  # 0 or 1

        total_slippage_ticks = crossing_ticks + size_impact_ticks + queue_ticks
        total_slippage = total_slippage_ticks * tick

        if order.side == Side.BUY:
            slipped = raw_price + total_slippage
        else:
            slipped = raw_price - total_slippage

        return max(_MIN_PRICE, min(_MAX_PRICE, slipped))

    def _can_fill(self, order: Order, book: OrderBook) -> float | None:
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

        # Compute simple P&L for SELL (revenue - cost basis)
        pos = store.positions.get(order.token_id)
        pnl = 0.0
        if order.side == Side.SELL and pos and pos.avg_entry_price > 0:
            pnl = (fill_price - pos.avg_entry_price) * fill_size

        # Report realized P&L to the risk engine for per-strategy attribution
        # and the per-trade-loss circuit breaker. Local import keeps the
        # simulator decoupled from risk.manager at module-load time.
        try:
            from risk.manager import risk_manager
            await risk_manager.report_trade_pnl(order.strategy, pnl)
        except Exception as e:
            log.debug("risk_manager.report_trade_pnl unavailable: %s", e)

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
        # record_fill updates daily_pnl and store.paper_balance (the source of truth).
        await store.record_fill(trade)
        self._virtual_balance_usdc = store.paper_balance
        await store.update_order(order.order_id, status=OrderStatus.FILLED, size_matched=order.size)
        # R11 — record the FILL stage with realised P&L so the decision chain
        # terminates with a P&L-attributed event for every filled order. A
        # missing decision_id (legacy / manual order) is skipped silently to
        # preserve the existing trade flow.
        if order.decision_id:
            try:
                from core.decision_ledger import decision_ledger
                await decision_ledger.record(
                    decision_id=order.decision_id,
                    stage="FILL",
                    token_id=order.token_id,
                    strategy=order.strategy,
                    pnl=pnl,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    side=order.side.value,
                    order_id=order.order_id,
                    trade_id=trade.trade_id,
                    paper=True,
                )
            except Exception as e:
                log.debug("[paper_sim] ledger FILL record failed: %s", e)
        await store.log_event(
            f"✅ Paper fill: {order.side.value} {fill_size:.2f} @ {fill_price:.4f} "
            f"[P&L: ${pnl:+.2f}] [{order.token_id[:8]}…]"
        )
        # S14 — record execution-quality metrics (signal_price vs fill_price,
        # best_bid/best_ask at fill, slippage in bps, latency, realized_edge).
        # Additive only: the existing fill logic above is untouched. The
        # try/except means a quality-recording failure can never break a
        # paper fill. signal_price defaults to order.price when the caller
        # doesn't track the signal-time price separately.
        try:
            from core.execution_quality import record_execution
            record_execution(order, fill_price, signal_price=order.price)
        except Exception:
            pass


# Module-level singleton
paper_sim = PaperSimulator()
