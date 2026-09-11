"""
paper/simulator.py — Paper trading simulator.
Simulates order fills against live order book data without touching real funds.
Activated when PAPER_TRADE=true in .env.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
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
        self, args: OrderArgs, strategy: str = "", decision_id: str = "",
        order_id: str | None = None,
    ) -> Order:
        """
        Create a simulated order, add it to the data store, and return it.
        No real API call is made.

        ``decision_id`` (R11) is propagated to the resulting ``Order`` so the
        downstream fill loop can record a FILL stage against the originating
        PREDICTION → SIGNAL → RISK_APPROVED chain in the decision ledger.

        ``order_id`` (W18-1) lets the caller (``BaseStrategy.submit_order``)
        pre-mint the canonical order_id so the OSM audit trail and the
        in-memory ``Order`` share one identity. When omitted (legacy
        callers — e.g. ``core/position_manager.py`` TP/SL exits) a fresh
        ``paper-{uuid}`` id is auto-generated, matching the pre-W18-1
        behaviour.

        W18-1 — records the canonical OSM lifecycle
        (CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN) on the
        same call so paper orders show up in the order-state audit trail
        the same way live orders do. The OSM calls are ADDITIVE: wrapped
        in ``try/except`` so a persistence failure never breaks the paper
        trade flow (mirrors the fail-soft contract of every other audit
        singleton in the codebase). If the caller pre-created the OSM
        entry (passed ``order_id`` matching an existing OSM snapshot),
        only the SUBMITTED → ACKNOWLEDGED → OPEN transitions are
        recorded; otherwise a fresh CREATED snapshot is minted first.
        """
        # Pre-mint order_id if the caller didn't supply one so the OSM
        # entry and the production Order share the same canonical id.
        if order_id is None:
            order_id = f"paper-{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
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
        # W18-1 — record the OSM lifecycle for the paper order. If the
        # caller (BaseStrategy.submit_order) pre-created the OSM entry in
        # the CREATED + VALIDATED states, only the SUBMITTED → ACKNOWLEDGED
        # → OPEN hops land here; otherwise we mint the full chain so a
        # legacy caller (position_manager TP/SL exit, manual
        # ``paper_sim.create_order`` test) still gets an audit trail.
        try:
            from core.order_state_machine import OrderState, osm
            existing = osm.get_order(order.order_id)
            if existing is None:
                osm.create_order(
                    strategy=strategy or "paper",
                    token_id=args.token_id,
                    side=args.side.value,
                    price=args.price,
                    size=args.size,
                    decision_id=decision_id,
                    order_id=order.order_id,
                    metadata={"paper": True},
                )
                osm.transition(order.order_id, OrderState.VALIDATED)
            # The order has been accepted by the (paper) exchange and is
            # now resting on the book — record SUBMITTED → ACKNOWLEDGED
            # → OPEN in one sequence. Each transition is its own
            # try/except so a failure mid-sequence still leaves the
            # prior state persisted (append-only audit trail).
            for state in (
                OrderState.SUBMITTED,
                OrderState.ACKNOWLEDGED,
                OrderState.OPEN,
            ):
                try:
                    osm.transition(order.order_id, state)
                except Exception as e:
                    log.debug(
                        "[paper_sim] OSM %s transition failed for %s: %s",
                        state.value, order.order_id, e,
                    )
        except Exception as e:
            log.debug(
                "[paper_sim] OSM wiring on create_order failed for %s: %s",
                order.order_id, e,
            )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        order = await store.update_order(order_id, status=OrderStatus.CANCELLED)
        # V13 / W18-1 — record the CANCELLED transition in the order state
        # machine audit trail (best-effort: a state-machine failure must
        # never break the paper-trade cancel flow). Local import keeps the
        # simulator decoupled from core.order_state_machine at
        # module-load time — the same pattern used by the decision_ledger
        # hook in create_order and the execution_quality hook in
        # _execute_fill. W18-1 fixes the broken V13 call signature (was
        # ``transition(order_id, OrderState.CANCELLED, reason=...)`` —
        # the module-level ``transition`` takes an ``Order`` instance, not
        # an ``order_id`` str; the kwargs ``reason=`` was never accepted;
        # the call silently raised + was swallowed by the bare ``except``).
        # The fix uses the W18-1 ``osm.transition(order_id, state)``
        # convenience helper which loads + transitions + persists in one
        # call and re-raises ``InvalidTransition`` so callers can react
        # (best-effort callers wrap in try/except).
        try:
            from core.order_state_machine import OrderState, osm
            osm.transition(order_id, OrderState.CANCELLED)
        except Exception as e:
            log.debug(
                "[paper_sim] OSM CANCELLED transition failed for %s: %s",
                order_id, e,
            )
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

        # ── W24-6 — duplicate-fill prevention ───────────────────────────────
        # A paper order can only be filled ONCE (the OSM transition to
        # FILLED is terminal). If ``_execute_fill`` is re-entered for the
        # same order (e.g. the periodic ``_try_fill_orders`` loop races a
        # manual ``paper_sim.cancel_order`` that fires after the order's
        # size_remaining was already zeroed), the second call would
        # double-record the fill — a phantom Trade, a phantom FILL ledger
        # stage, a phantom closed_position. Dedup by ``order.order_id``
        # (the canonical per-order identity) so a second call within the
        # 1h TTL window returns silently. Best-effort: a registry
        # exception must NEVER break the fill path (mirrors the
        # decision_ledger / execution_quality fire-and-forget contract).
        try:
            from core.dedup import dedup_registry
            fill_key = f"paper:{order.order_id}"
            if not dedup_registry.check_and_add("fill", fill_key, ttl_seconds=3600):
                log.debug(
                    "[paper_sim] Duplicate fill blocked by dedup registry: %s",
                    order.order_id,
                )
                return
        except Exception as e:  # noqa: BLE001 — dedup must never break fills
            log.debug("[paper_sim] dedup_registry check failed (continuing): %s", e)

        # ── W18-7 — snapshot pre-fill position state ───────────────────────
        # ``store.record_fill`` mutates ``Position`` in place (``yes_shares``
        # decremented, ``total_invested`` reduced, ``realised_pnl``
        # accumulated). To detect a *closing* SELL — one that brings
        # ``yes_shares`` to 0 — we capture the entry-side fields before
        # the fill is booked so the closed-positions journal can record
        # the full round-trip (entry price, holding period, entry strategy).
        # Pre-fix: ``closed_positions.db`` had 0 rows despite 143 EXIT
        # audit events because only ``core/settlement.py`` (market
        # resolution) recorded closes; the TP/SL/manual exit path that
        # routes through ``position_manager → paper_sim.create_order →
        # _execute_fill`` was silently dropping the round-trip.
        pos_before = store.positions.get(order.token_id)
        _entry_shares_before = pos_before.yes_shares if pos_before else 0.0
        _entry_price_before = pos_before.avg_entry_price if pos_before else 0.0
        _entry_opened_at = pos_before.opened_at if pos_before else time.time()
        _entry_strategy = (
            (pos_before.strategy if pos_before else "") or order.strategy or ""
        )

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
            # W23-2 — record the fill timestamp against the latency tracker
            # so the signal→order→fill pipeline latency is measurable per
            # correlation_id. ``order.decision_id`` is the same identifier
            # threaded through the chain (alias for the tracker's
            # ``correlation_id``). Placed inside the ``if order.decision_id:``
            # block so the latency record is only created for orders that
            # also have a decision-ledger chain — keeps the by-strategy
            # breakdown meaningful. Best-effort: wrapped in its own
            # try/except so a tracker hiccup never blocks the fill path
            # (mirrors the ledger / execution_quality fire-and-forget
            # contract).
            try:
                from core.latency_tracker import latency_tracker
                latency_tracker.record_fill(correlation_id=order.decision_id)
            except Exception as e:
                log.debug(
                    "[paper_sim] latency_tracker.record_fill failed: %s", e
                )
            # W19-3 — record the POSITION stage immediately after the FILL
            # so the chain shows the actual exposure the bot took on this
            # decision. The Position object was mutated in place by
            # ``store.record_fill`` above; we snapshot the post-fill state
            # here so the dashboard can answer "what position did this
            # decision result in?" without a separate query against
            # ``store.positions``. ADDITIVE: best-effort try/except so a
            # ledger hiccup never breaks the fill flow.
            try:
                from core.decision_ledger import decision_ledger
                pos_snapshot = store.positions.get(order.token_id)
                if pos_snapshot is not None:
                    await decision_ledger.record_position(
                        correlation_id=order.decision_id,
                        token_id=order.token_id,
                        strategy=order.strategy,
                        position={
                            "yes_shares": float(getattr(pos_snapshot, "yes_shares", 0.0)),
                            "avg_entry_price": float(getattr(pos_snapshot, "avg_entry_price", 0.0)),
                            "total_invested": float(getattr(pos_snapshot, "total_invested", 0.0)),
                            "opened_at": float(getattr(pos_snapshot, "opened_at", 0.0)),
                            "strategy": getattr(pos_snapshot, "strategy", "") or order.strategy,
                            "paper": True,
                            "fill_price": float(fill_price),
                            "fill_size": float(fill_size),
                            "side": order.side.value,
                            # The closing-SELL realised P&L for this fill
                            # (0.0 for opening BUYs) — promoted to the row's
                            # ``pnl`` column by record_position.
                            "pnl": float(pnl or 0.0),
                        },
                    )
            except Exception as e:
                log.debug("[paper_sim] ledger POSITION record failed: %s", e)
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

        # ── W18-7 — record closed_position for fully-closing SELL fills ──
        # When a SELL fill brings ``yes_shares`` from > 0 to exactly 0, the
        # position has been fully closed (TP / SL / manual exit). Mirror the
        # round-trip into the closed-positions journal so the 7-dimension
        # P&L attribution, the live-safety-gate's ``closed_trades`` check
        # (≥30 closed positions), and the §9 execution-quality framework
        # have the row they need. ``core/settlement.py`` already records
        # market-resolution closes; this hook covers every other exit path.
        #
        # Idempotency: ``position_id`` is derived from ``order.order_id``
        # (``fill-<order_id>``) so a replayed fill cannot duplicate the row
        # (``closed_positions.position_id`` is UNIQUE with
        # ``INSERT OR IGNORE`` semantics).
        #
        # Fire-and-forget: every step is wrapped in ``try/except`` so a
        # journal hiccup can never break a paper fill (mirrors the
        # ``decision_ledger`` / ``execution_quality`` fire-and-forget
        # contract).
        try:
            if (
                order.side == Side.SELL
                and _entry_shares_before > 0.0
                and fill_size > 0.0
            ):
                pos_after = store.positions.get(order.token_id)
                shares_after = pos_after.yes_shares if pos_after else 0.0
                if shares_after <= 0.0:
                    # Position fully closed by this fill — record the
                    # round-trip. ``strategy`` is the ENTRY strategy (the
                    # strategy that opened the position, captured for
                    # attribution); ``exit_reason`` is the EXIT order's
                    # strategy (e.g. ``position_manager_tp`` / ``_sl``)
                    # round-tripped via ``metadata_json``.
                    from core.closed_positions import closed_positions
                    holding_seconds = max(0.0, time.time() - _entry_opened_at)
                    await closed_positions.record_closed_position(
                        token_id=order.token_id,
                        strategy=_entry_strategy,
                        entry_price=_entry_price_before,
                        exit_price=fill_price,
                        shares=fill_size,
                        pnl=pnl,
                        holding_seconds=holding_seconds,
                        model_version="",
                        # Attribution-dimension kwargs (promoted to
                        # first-class columns by record_closed_position).
                        direction="BUY",  # long-YES closed via SELL
                        decision_id=order.decision_id or "",
                        # Idempotency key — stable per fill.
                        position_id=f"fill-{order.order_id}",
                        # Non-attribution extras → metadata_json
                        # (decoded back as ``data`` on read).
                        exit_reason=order.strategy,
                        exit_order_id=order.order_id,
                        exit_trade_id=trade.trade_id,
                        paper=True,
                    )
                    # W19-4 — Mirror the closed round-trip into the ML
                    # economic-value tracker so God Mode §16's "ML value
                    # is unmeasured" gap closes. Fire-and-forget (the
                    # tracker swallows its own persistence errors), best-
                    # effort (defaults to 0 for confidence / predicted_edge
                    # when the entry decision didn't capture them — paper
                    # fills don't always carry an ML signal). Synchronous
                    # SQLite write mirrors the closed_positions contract;
                    # blocks the event loop for one indexed INSERT (~µs).
                    from ml.economic_value import ml_value_tracker
                    from ml.model import ml_model
                    ml_value_tracker.record_trade(
                        trade_id=f"fill-{order.order_id}",
                        token_id=order.token_id,
                        model_version=getattr(
                            ml_model, "_last_trained", "unknown"
                        ) or "unknown",
                        prediction=0.5,
                        confidence=0.0,
                        predicted_edge=0.0,
                        actual_pnl=pnl,
                        metadata={
                            "strategy": _entry_strategy,
                            "exit_strategy": order.strategy,
                            "holding_seconds": holding_seconds,
                            "decision_id": order.decision_id or "",
                            "paper": True,
                        },
                    )
        except Exception as e:
            log.debug("[paper_sim] closed_positions record failed: %s", e)
        # W18-1 — record the FILLED transition in the OSM audit trail so
        # the canonical lifecycle terminates with a FILLED snapshot (was
        # missing per the W17-2 P0-C-01 finding). Best-effort: a state-
        # machine failure (e.g. the order was already terminal because a
        # prior cancel race won) is logged and swallowed so a fill never
        # rolls back. ``filled_size`` is stamped on the snapshot so a
        # future ``/api/orders/{id}/state`` consumer can see the executed
        # quantity alongside the state.
        try:
            from core.order_state_machine import OrderState, osm
            osm.transition(
                order.order_id,
                OrderState.FILLED,
                filled_size=fill_size,
                metadata={"fill_price": fill_price, "pnl": pnl},
            )
        except Exception as e:
            log.debug(
                "[paper_sim] OSM FILLED transition failed for %s: %s",
                order.order_id, e,
            )


# Module-level singleton
paper_sim = PaperSimulator()
