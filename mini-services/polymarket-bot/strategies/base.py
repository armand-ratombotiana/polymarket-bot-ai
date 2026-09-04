"""
strategies/base.py — Abstract base class for all trading strategies.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod

from config import settings
from core.clob_client import OrderArgs, clob_client
from core.data_store import Order, store
from paper.simulator import paper_sim
from risk.manager import risk_manager

log = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    All strategies inherit from this. Provides:
    - Unified order submission (paper or live)
    - Risk check integration
    - Lifecycle management (start/stop)
    """

    name: str = "base"

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._paper = settings.paper_trade

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"strategy-{self.name}")
        log.info("Strategy [%s] started (paper=%s)", self.name, self._paper)
        await store.log_event(f"▶ Strategy [{self.name}] started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Strategy [%s] stopped", self.name)
        await store.log_event(f"⏹ Strategy [{self.name}] stopped")

    @abstractmethod
    async def _run(self) -> None:
        """Main strategy loop. Override in subclasses."""
        ...

    # ── Order helpers ─────────────────────────────────────────────────────

    async def submit_order(self, args: OrderArgs, decision_id: str = "") -> Order | None:
        """
        Submit an order through the risk manager, then either:
        - Paper mode: record in PaperSimulator
        - Live mode: send to CLOB API
        Returns the Order object or None if rejected.

        ``decision_id`` (R11) links the resulting ORDER (and downstream FILL)
        stage to the originating PREDICTION → SIGNAL chain in the unified
        decision ledger. Defaults to "" for legacy / manual callers.

        W18-1 — Order State Machine (OSM) wiring (closes the P0-C-01 gap
        surfaced by the W17-2 Bot Execution Engine Assessment). Every
        submit-order path now records the canonical lifecycle
        (CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN) in the
        SQLite-backed OSM audit trail before / after the actual exchange
        (or paper sim) call. The OSM calls are ADDITIVE: wrapped in
        ``try/except`` so a persistence failure never breaks trading
        (mirrors the fail-soft contract of every other audit singleton
        in the codebase).

        Identity linking: for paper orders the canonical ``order_id``
        is pre-minted in this method and passed to ``paper_sim.create_order``
        so the OSM audit trail and the in-memory ``Order`` share one
        identity. For live orders the exchange assigns its own order_id
        (returned in the CLOB response); we pre-mint an OSM tracking id
        (``ord-...``) and stamp the exchange-assigned id into the OSM
        entry's ``metadata.exchange_order_id`` field so the two can be
        cross-referenced from the audit trail.
        """
        # W18-1 — pre-mint the canonical OSM tracking id so the OSM entry
        # and the production Order share one identity (paper path) or are
        # cross-referenceable (live path via metadata).
        side_str = (
            args.side.value if hasattr(args.side, "value") else str(args.side)
        )
        if self._paper:
            osm_order_id = f"paper-{uuid.uuid4().hex[:12]}"
        else:
            osm_order_id = f"ord-{uuid.uuid4().hex}"

        osm_order = None
        try:
            from core.order_state_machine import OrderState, osm
            osm_order = osm.create_order(
                strategy=self.name,
                token_id=args.token_id,
                side=side_str,
                price=args.price,
                size=args.size,
                decision_id=decision_id,
                order_id=osm_order_id,
                metadata={"paper": self._paper},
            )
        except Exception as e:
            log.debug(
                "[%s] OSM create_order failed (continuing without audit "
                "trail): %s", self.name, e,
            )

        # Build a provisional Order for risk checking
        provisional = Order(
            order_id=osm_order_id,
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            strategy=self.name,
            paper=self._paper,
            decision_id=decision_id,
        )

        allowed, reason = await risk_manager.check_order(provisional)
        if not allowed:
            log.debug("[%s] Order blocked by risk: %s", self.name, reason)
            await store.log_event(f"⚠ Risk block [{self.name}]: {reason}")
            # R11 — record the RISK_REJECTED stage so the decision chain shows
            # why the order never reached the book.
            try:
                from core.decision_ledger import decision_ledger
                await decision_ledger.record(
                    decision_id=decision_id,
                    stage="RISK_REJECTED",
                    token_id=args.token_id,
                    strategy=self.name,
                    pnl=0.0,
                    side=side_str,
                    price=args.price,
                    size=args.size,
                    reason=reason,
                )
            except Exception as e:
                log.debug("[%s] ledger RISK_REJECTED record failed: %s", self.name, e)
            # W18-1 — record the RISK_REJECTED transition so a rejected
            # order is visible in the OSM audit trail with the rejection
            # reason stamped in metadata. Pass the order_id str (not the
            # Order instance) so the latest snapshot is loaded — protects
            # against the stale-Order-instance race (the local
            # ``osm_order`` var still reflects the CREATED state from
            # ``create_order``; the persisted snapshot is the canonical
            # truth source).
            try:
                from core.order_state_machine import OrderState, osm
                if osm_order is not None:
                    osm.transition(
                        osm_order.order_id,
                        OrderState.VALIDATED,
                        metadata={"risk_rejected": True, "reason": reason},
                    )
                    osm.transition(
                        osm_order.order_id,
                        OrderState.REJECTED,
                        metadata={"reason": reason},
                    )
            except Exception as e:
                log.debug(
                    "[%s] OSM RISK_REJECTED transition failed: %s",
                    self.name, e,
                )
            return None

        # R11 — record RISK_APPROVED before the order leaves the strategy layer
        # (paper or live), so a missing downstream ORDER/FILL stage surfaces as
        # a gap rather than a silent no-op.
        try:
            from core.decision_ledger import decision_ledger
            await decision_ledger.record(
                decision_id=decision_id,
                stage="RISK_APPROVED",
                token_id=args.token_id,
                strategy=self.name,
                pnl=0.0,
                side=side_str,
                price=args.price,
                size=args.size,
            )
        except Exception as e:
            log.debug("[%s] ledger RISK_APPROVED record failed: %s", self.name, e)

        # W18-1 — risk check passed; transition the OSM entry to VALIDATED
        # (the SUBMITTED → ACKNOWLEDGED → OPEN hops are recorded by the
        # paper / live submission paths below so each mode controls its
        # own submission semantics). Pass the order_id str (not the Order
        # instance) so the latest snapshot is loaded.
        try:
            from core.order_state_machine import OrderState, osm
            if osm_order is not None:
                osm.transition(osm_order.order_id, OrderState.VALIDATED)
        except Exception as e:
            log.debug(
                "[%s] OSM VALIDATED transition failed: %s", self.name, e,
            )

        if self._paper:
            # W18-1 — pass the pre-minted ``osm_order_id`` so the OSM
            # entry and the in-memory Order share one identity.
            # ``paper_sim.create_order`` records the SUBMITTED →
            # ACKNOWLEDGED → OPEN hops on the same call.
            return await paper_sim.create_order(
                args,
                strategy=self.name,
                decision_id=decision_id,
                order_id=osm_order_id,
            )
        else:
            resp = await clob_client.create_order(args)
            if resp is None:
                # W18-1 — exchange rejected the order (signing failure,
                # HTTP error, exception inside the client). Record the
                # SUBMITTED → REJECTED hops so the audit trail shows the
                # order was attempted but rejected at the exchange.
                try:
                    from core.order_state_machine import OrderState, osm
                    if osm_order is not None:
                        osm.transition(osm_order.order_id, OrderState.SUBMITTED)
                        osm.transition(
                            osm_order.order_id,
                            OrderState.REJECTED,
                            metadata={"reason": "clob_client returned None"},
                        )
                except Exception as e:
                    log.debug(
                        "[%s] OSM REJECTED transition failed: %s",
                        self.name, e,
                    )
                return None
            order_id = resp.get("orderID") or resp.get("order_id", "unknown")
            order = Order(
                order_id=order_id,
                token_id=args.token_id,
                side=args.side,
                price=args.price,
                size=args.size,
                strategy=self.name,
                paper=False,
                decision_id=decision_id,
            )
            await store.add_order(order)
            # W18-1 — record the SUBMITTED → ACKNOWLEDGED → OPEN hops so
            # the live order shows up as OPEN in the OSM audit trail. The
            # exchange-assigned ``order_id`` is stamped into the OSM
            # entry's ``metadata.exchange_order_id`` so the two ids can
            # be cross-referenced from the audit trail. Pass the
            # order_id str (not the Order instance) so the latest
            # snapshot is loaded on each hop.
            try:
                from core.order_state_machine import OrderState, osm
                if osm_order is not None:
                    osm.transition(
                        osm_order.order_id,
                        OrderState.SUBMITTED,
                        metadata={"exchange_order_id": order_id},
                    )
                    osm.transition(osm_order.order_id, OrderState.ACKNOWLEDGED)
                    osm.transition(osm_order.order_id, OrderState.OPEN)
            except Exception as e:
                log.debug(
                    "[%s] OSM live OPEN transitions failed: %s",
                    self.name, e,
                )
            return order

    async def cancel_order(self, order_id: str) -> bool:
        if self._paper:
            return await paper_sim.cancel_order(order_id)
        else:
            ok = await clob_client.cancel_order(order_id)
            # W18-1 — record the CANCELLED transition in the OSM audit
            # trail after a successful live cancel. Best-effort: a state-
            # machine failure (e.g. the OSM entry doesn't exist for a
            # legacy order, or the order was already terminal) is logged
            # and swallowed so a cancel never rolls back.
            if ok:
                try:
                    from core.order_state_machine import OrderState, osm
                    osm.transition(order_id, OrderState.CANCELLED)
                except Exception as e:
                    log.debug(
                        "OSM CANCELLED transition failed for %s: %s",
                        order_id, e,
                    )
            return ok
