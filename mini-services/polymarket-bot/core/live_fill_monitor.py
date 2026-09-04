"""
core/live_fill_monitor.py — Live fill acknowledgement loop.

Background task that polls the Polymarket CLOB REST API for new fills on
our open orders and reconciles them with the local order/position state.

WHY THIS MODULE EXISTS
~~~~~~~~~~~~~~~~~~~~~~~~
`paper/simulator.py::_fill_loop` already simulates fills for paper orders,
but its loop explicitly skips non-paper orders (`if not order.paper: continue`).
Live (real-funds) orders therefore never received a fill acknowledgement:
the local `data_store.open_orders` dict would hold them in the OPEN state
indefinitely, `store.positions` / `store.daily_pnl` / `equity_history` would
never reflect the realised fill, the `decision_ledger` FILL stage would never
fire for live trades, and the `core.execution_quality` ledger would never
record the realised edge of a live execution. This was P0-C02 in the
W17-2 Bot & Execution Engine Assessment.

The fill acknowledgement loop closes that gap:

  1. Polls ``clob_client.get_trades()`` every ``poll_interval`` seconds.
  2. For every unseen trade, looks up the corresponding local order
     (by ``taker_order_id`` / ``client_order_id`` / ``maker_orders[].order_id``).
  3. Records the fill in ``data_store.record_fill`` (positions, daily_pnl,
     equity_history) + ``data_store.update_order`` (open_orders → FILLED).
  4. Transitions the order in ``core.order_state_machine`` (OPEN → FILLED)
     so the SQLite audit trail reflects the lifecycle, and saves the new
     snapshot.
  5. Records execution-quality metrics via ``core.execution_quality.record_execution``.
  6. Records a FILL stage in the decision ledger (best-effort) so the
     live PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL chain is
     reconstructable per ``decision_id``.

PAPER MODE
~~~~~~~~~~
When ``settings.paper_trade`` is True the monitor short-circuits — paper
fills are already handled by ``paper/simulator._fill_loop``. The monitor
only runs when live trading is enabled, and only over the authenticated
``/data/trades`` endpoint (which is L2-auth-scoped to the caller's wallet).

DEDUPLICATION
~~~~~~~~~~~~~
Every observed ``trade_id`` (or ``id`` fallback) is added to an in-memory
``_last_trade_ids`` set so a re-poll of the same trade never re-records a
fill. The set is bounded at ``_MAX_SEEN_TRADE_IDS`` entries; on overflow
it's trimmed to the most recent ``_KEEP_SEEN_TRADE_IDS`` to keep memory
growth capped over a long-running session.

ERROR CONTRACT
~~~~~~~~~~~~~~
The monitor must NEVER crash the trading pipeline. ``_poll_loop`` wraps
every iteration in a ``try/except`` that logs at ``error`` level with
``exc_info=True``; ``_check_for_new_fills`` additionally wraps each
individual trade's recording in a ``try/except`` so a single malformed
trade dict can't poison the rest of the batch. The OSM transition and
the decision-ledger / execution-quality side-effects are each wrapped
individually so a failure in one subsystem can't block the others.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Cap on the size of the in-memory seen-trade-id set. The set is checked
# on every poll (O(1) membership test) so it's the natural place to bound
# long-running-session memory growth. When the set exceeds this threshold
# it's rebuilt from the most recent ``_KEEP_SEEN_TRADE_IDS`` entries — the
# oldest entries (which are the least likely to be re-observed) are
# discarded, accepting a small probability of duplicate processing for a
# very old trade that the CLOB happens to replay.
_MAX_SEEN_TRADE_IDS = 1000
_KEEP_SEEN_TRADE_IDS = 500


class LiveFillMonitor:
    """Background task that polls the CLOB for fill confirmations.

    The monitor is idempotent: ``start()`` is a no-op if already running;
    ``stop()`` is a no-op if not running. Polling continues until ``stop()``
    is called (which sets ``_running = False`` and cancels the polling task).

    Attributes:
        poll_interval: seconds between CLOB ``/data/trades`` polls (default 2.0).
        _running: whether the polling loop is currently active.
        _task: the asyncio Task running ``_poll_loop``, or ``None`` when stopped.
        _last_trade_ids: set of CLOB trade ids already processed (dedup set).
    """

    def __init__(self, poll_interval: float = 2.0) -> None:
        self.poll_interval: float = poll_interval
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_trade_ids: set[str] = set()

    async def start(self) -> None:
        """Start the fill monitor (idempotent — no-op if already running)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="live-fill-monitor")
        logger.info(
            "Live fill monitor started (interval=%.2fs)", self.poll_interval
        )

    async def stop(self) -> None:
        """Stop the fill monitor (idempotent — no-op if not running)."""
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("Live fill monitor task raised on stop: %s", e)
            self._task = None
        logger.info("Live fill monitor stopped")

    # ── Polling loop ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop — runs until ``_running`` is flipped to False.

        Each iteration is wrapped in a top-level ``try/except`` so a single
        poll failure (network blip, CLOB 5xx, transient JSON parse error)
        can never crash the loop. Errors are logged at ``error`` level
        with ``exc_info=True`` so the traceback is captured in the audit
        trail. The ``asyncio.sleep`` runs unconditionally between iterations
        so a hung ``_check_for_new_fills`` can't starve the scheduler.
        """
        while self._running:
            try:
                await self._check_for_new_fills()
            except asyncio.CancelledError:
                # Explicit re-raise so ``stop()``'s ``task.cancel()`` propagates.
                raise
            except Exception as e:
                logger.error("Fill monitor error: %s", e, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _check_for_new_fills(self) -> None:
        """Poll the CLOB for new trades and reconcile any unseen ones.

        Paper-mode short-circuit: when ``settings.paper_trade`` is True,
        paper fills are already handled by ``paper/simulator._fill_loop``
        — this monitor would just duplicate the work. Returning early also
        means the CLOB ``/data/trades`` call (which requires L2 auth) is
        never made in paper mode, so a paper-only operator with no API keys
        configured never sees spurious auth warnings.
        """
        from config import settings

        # Paper mode — the paper simulator's _fill_loop handles fills.
        if settings.paper_trade:
            return

        from core.clob_client import clob_client

        # Fetch recent trades. The CLOB ``/data/trades`` endpoint is
        # L2-auth-scoped to the caller's wallet, so the response already
        # contains only the trades we participated in (as maker or taker).
        try:
            trades = await clob_client.get_trades()
        except Exception as e:
            logger.warning("CLOB get_trades() failed: %s", e)
            return

        if not trades:
            return

        for trade in trades:
            try:
                await self._process_trade(trade)
            except Exception as e:
                # A single malformed trade dict must not poison the rest of
                # the batch — log at warning level and continue.
                logger.warning(
                    "Failed to process trade %s: %s",
                    trade.get("id") or trade.get("trade_id", "<unknown>"),
                    e,
                    exc_info=True,
                )

    async def _process_trade(self, trade: dict[str, Any]) -> None:
        """Reconcile a single CLOB trade dict against local state.

        Steps (each wrapped in its own try/except so a failure in one
        subsystem can't block the others):
          1. Dedup by trade_id (skip if already seen).
          2. Update the local ``data_store.Order`` to FILLED + record the
             fill (positions / daily_pnl / equity_history).
          3. Transition the order in ``core.order_state_machine`` and
             persist the new snapshot (best-effort).
          4. Record execution-quality metrics (best-effort).
          5. Record a FILL stage in the decision ledger (best-effort).
          6. Bound the in-memory seen-id set.
        """
        from core.data_store import Order as DSOrder, OrderStatus, Side, Trade, store

        # ── Dedup ─────────────────────────────────────────────────────────
        trade_id = (
            trade.get("id")
            or trade.get("trade_id")
            or trade.get("trade_hash")
            or ""
        )
        if not trade_id:
            # Without a stable id we can't dedup — skip rather than risk
            # double-counting the same fill on every poll.
            logger.debug("Skipping trade with no id/trade_id: %s", trade)
            return
        if trade_id in self._last_trade_ids:
            return
        self._last_trade_ids.add(trade_id)

        # ── Extract fill fields (defensive — CLOB shapes vary) ───────────
        token_id = (
            trade.get("asset_id")
            or trade.get("token_id")
            or trade.get("market")
            or ""
        )
        side_raw = str(trade.get("side") or "").upper()
        try:
            side = Side(side_raw) if side_raw in {"BUY", "SELL"} else Side.BUY
        except Exception:
            side = Side.BUY
        try:
            price = float(trade.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            size = float(trade.get("size") or 0.0)
        except (TypeError, ValueError):
            size = 0.0

        # Resolve the local order_id (try every CLOB field that might
        # carry it: taker_order_id for taker fills, client_order_id /
        # order_id for maker fills, and the maker_orders[] array for
        # maker fills where the taker order is someone else's).
        order_id = self._resolve_order_id(trade)

        # Strategy / decision_id passthrough — best-effort lookup from
        # the local data store so the recorded Trade carries the right
        # attribution. If we can't find the local order, we fall back to
        # empty strings (the trade is still recorded; only attribution
        # is degraded).
        strategy = ""
        decision_id = ""
        signal_price = price  # default to fill price if local order is gone
        local_order: Optional[DSOrder] = None
        if order_id:
            local_order = store.open_orders.get(order_id)
            if local_order is None:
                # Already moved to history (e.g. a prior poll processed it).
                # Best-effort lookup in the order_history list.
                for hist in store.order_history:
                    if hist.order_id == order_id:
                        local_order = hist
                        break
        if local_order is not None:
            strategy = local_order.strategy
            decision_id = local_order.decision_id
            signal_price = float(local_order.price)

        # ── Step 2: record fill in the data store ─────────────────────────
        trade_record = Trade(
            trade_id=trade_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
            paper=False,  # this monitor only runs in live mode
            pnl=0.0,  # realised P&L computed when an exit fill closes a position
            timestamp=time.time(),
        )
        # record_fill updates store.positions / daily_pnl / paper_balance
        # (which is misnamed for live mode but is the same field used by
        # the risk engine as the equity baseline) / equity_history.
        try:
            await store.record_fill(trade_record)
        except Exception as e:
            logger.warning("store.record_fill failed for trade %s: %s", trade_id, e)

        # Update the local open_orders entry to FILLED. ``update_order``
        # moves the order out of ``open_orders`` into ``order_history``
        # when the status is FILLED / CANCELLED.
        if order_id:
            try:
                await store.update_order(
                    order_id,
                    status=OrderStatus.FILLED,
                    size_matched=size,
                )
            except Exception as e:
                logger.warning(
                    "store.update_order failed for order %s: %s", order_id, e
                )

        # ── Step 3: OSM transition (OPEN → FILLED) ────────────────────────
        if order_id:
            try:
                from core.order_state_machine import (
                    InvalidTransition,
                    OrderState,
                    order_state_machine,
                    transition,
                )

                snapshot = order_state_machine.load(order_id)
                if snapshot is None:
                    # The order was never tracked in the OSM (C-01 — the
                    # OSM is not yet wired into the production submit
                    # path). Log at debug rather than warning so a noisy
                    # log doesn't appear on every live fill until C-01
                    # is fixed.
                    logger.debug(
                        "OSM has no snapshot for order %s — skipping transition",
                        order_id,
                    )
                else:
                    if not _is_terminal_state(snapshot.state):
                        new_snapshot = transition(snapshot, OrderState.FILLED)
                        order_state_machine.save(new_snapshot)
            except InvalidTransition as e:
                # Already terminal (FILLED / CANCELLED / REJECTED / EXPIRED)
                # — common when the CLOB replays a trade. Debug-level so
                # the log isn't noisy.
                logger.debug(
                    "OSM transition to FILLED rejected for order %s (%s)",
                    order_id,
                    e,
                )
            except Exception as e:
                logger.warning(
                    "OSM transition failed for order %s: %s", order_id, e
                )

        # ── Step 4: execution-quality ─────────────────────────────────────
        try:
            from core.execution_quality import record_execution

            # record_execution is duck-typed: it needs an object with
            # ``token_id``, ``side`` (with ``.value`` or ``str()``-able),
            # ``price``, ``size``, ``created_at``, ``strategy``, ``paper``,
            # ``decision_id``, ``order_id`` attributes. Construct a
            # SimpleNamespace from the local order (if available) or
            # synthesise one from the trade dict.
            if local_order is not None:
                record_execution(local_order, price, signal_price=signal_price)
            else:
                synth = SimpleNamespace(
                    order_id=order_id or f"live-{uuid.uuid4().hex[:8]}",
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    created_at=time.time(),
                    strategy=strategy,
                    paper=False,
                    decision_id=decision_id,
                )
                record_execution(synth, price, signal_price=signal_price)
        except Exception as e:
            logger.warning(
                "Execution quality recording failed for trade %s: %s",
                trade_id,
                e,
            )

        # ── Step 5: decision ledger FILL stage ────────────────────────────
        if decision_id:
            try:
                from core.decision_ledger import decision_ledger

                await decision_ledger.record(
                    decision_id=decision_id,
                    stage="FILL",
                    token_id=token_id,
                    strategy=strategy,
                    pnl=0.0,
                    fill_price=price,
                    fill_size=size,
                    side=side.value,
                    order_id=order_id,
                    trade_id=trade_id,
                    paper=False,
                )
            except Exception as e:
                logger.debug(
                    "[live_fill_monitor] ledger FILL record failed: %s", e
                )

        logger.info(
            "Live fill confirmed: %s %.4f @ %.4f (order=%s trade=%s)",
            side.value,
            size,
            price,
            order_id or "<unknown>",
            trade_id,
        )

        # ── Step 6: bound the seen-id set ─────────────────────────────────
        if len(self._last_trade_ids) > _MAX_SEEN_TRADE_IDS:
            # Keep only the most recent entries. ``set`` is unordered, so
            # we can't truly keep "the most recent" — but the bound still
            # caps memory growth, and the dedup miss rate after a trim is
            # negligible (the CLOB doesn't replay trades older than a few
            # polls). The cast to ``list`` + slice + ``set`` rebuild is
            # the cheapest way to trim a set in stdlib.
            self._last_trade_ids = set(
                list(self._last_trade_ids)[-_KEEP_SEEN_TRADE_IDS:]
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_order_id(trade: dict[str, Any]) -> str:
        """Best-effort lookup of the local order_id from a CLOB trade dict.

        Tries every field that might carry our local order_id:
          - ``taker_order_id`` (we were the taker)
          - ``order_id`` / ``client_order_id`` (generic fallbacks)
          - ``maker_orders[].order_id`` (we were the maker — the trade
            dict's top-level ``taker_order_id`` is then someone else's;
            our local order_id is in the per-maker sub-list)
          - ``maker_orders[].owner`` (Polymarket sometimes populates
            ``owner`` instead of ``order_id`` for the maker leg)

        Returns the first non-empty value found, or ``""`` if none.
        """
        for key in ("taker_order_id", "order_id", "client_order_id"):
            val = trade.get(key)
            if val:
                return str(val)
        maker_orders = trade.get("maker_orders")
        if isinstance(maker_orders, list):
            for mo in maker_orders:
                if not isinstance(mo, dict):
                    continue
                for key in ("order_id", "client_order_id", "owner"):
                    val = mo.get(key)
                    if val:
                        return str(val)
        return ""


def _is_terminal_state(state: Any) -> bool:
    """Return True if ``state`` is a terminal OSM state.

    Accepts an ``OrderState`` enum value or a plain ``str`` (compares
    against the canonical terminal state names: FILLED / CANCELLED /
    REJECTED / EXPIRED). Avoids importing ``is_terminal`` from
    ``core.order_state_machine`` at module load time so a transient
    import error doesn't break the monitor's startup.
    """
    try:
        from core.order_state_machine import is_terminal

        return bool(is_terminal(state))
    except Exception:
        # Fallback: literal-name compare. Defensive — the import should
        # never fail, but if it does we still want a sensible answer.
        if hasattr(state, "value"):
            state = state.value
        return str(state).upper() in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


# Module-level singleton (mirrors ``paper_sim`` / ``book_poller`` /
# ``watchdog`` convention so importers can grab the instance at module
# import time and so the lifespan startup/shutdown hooks in
# ``api/server.py`` can reference the same singleton).
live_fill_monitor = LiveFillMonitor()


__all__ = ["LiveFillMonitor", "live_fill_monitor"]
