"""
strategies/base.py — Abstract base class for all trading strategies.
"""
from __future__ import annotations

import asyncio
import logging
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
        """
        # Build a provisional Order for risk checking
        provisional = Order(
            order_id="pre-check",
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
                    side=args.side.value,
                    price=args.price,
                    size=args.size,
                    reason=reason,
                )
            except Exception as e:
                log.debug("[%s] ledger RISK_REJECTED record failed: %s", self.name, e)
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
                side=args.side.value,
                price=args.price,
                size=args.size,
            )
        except Exception as e:
            log.debug("[%s] ledger RISK_APPROVED record failed: %s", self.name, e)

        if self._paper:
            return await paper_sim.create_order(args, strategy=self.name, decision_id=decision_id)
        else:
            resp = await clob_client.create_order(args)
            if resp is None:
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
            return order

    async def cancel_order(self, order_id: str) -> bool:
        if self._paper:
            return await paper_sim.cancel_order(order_id)
        else:
            return await clob_client.cancel_order(order_id)
