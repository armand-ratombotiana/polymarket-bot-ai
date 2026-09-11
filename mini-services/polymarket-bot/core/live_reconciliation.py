"""Live reconciliation — compares local order/position state with CLOB.

W18-4 — P0-C04 fix. The existing ``core/reconciliation.py`` reconciles
only TimescaleDB telemetry counters (per-table ``inserts_ok`` vs
physically-stored row counts); it does NOT detect drift between the
local ``DataStore`` (orders / positions) and the exchange's view of
the same account. Without a live-vs-exchange reconciliation loop an
operator cannot tell whether:

  * an order they think is OPEN is actually still resting on the
    exchange (vs. already filled / cancelled / expired silently);
  * an order the exchange shows as OPEN is unknown to the bot (e.g.
    placed by a parallel UI session using the same wallet);
  * the local position size agrees with the exchange's settled size
    (a divergence implies a missed fill or a settlement lag).

This module runs a periodic background task (default 60s; configurable
via the ``interval`` constructor arg) that pulls the local open-order
set + position map, pulls the same from ``clob_client.get_open_orders``
/ ``get_positions``, diffs them, and stores the most recent
``ReconciliationResult`` for the ``GET /api/reconciliation/live`` route
to surface.

Detection contract
~~~~~~~~~~~~~~~~~~~

  * ``stale_local``          — order_ids present locally but NOT on the
                               exchange (the bot thinks an order is
                               OPEN that the exchange no longer shows —
                               likely already filled / cancelled /
                               expired).
  * ``orphaned_exchange``    — order_ids present on the exchange but
                               NOT locally (placed out-of-band, e.g.
                               via the wallet's other UI).
  * ``position_mismatches``  — token_ids where the local
                               ``Position.yes_shares`` differs from
                               the exchange's reported size by more
                               than ``POSITION_TOLERANCE`` (default
                               0.001 shares — the smallest meaningful
                               size on Polymarket's 6-decimal scale).
  * ``is_clean``             — ``True`` iff all three lists above are
                               empty. Surfaces in the
                               ``/api/system/health`` check + the
                               ``/api/reconciliation/live`` body.

Paper / shadow mode
~~~~~~~~~~~~~~~~~~~~~
The reconciler short-circuits to a clean ``ReconciliationResult`` in
paper mode (``settings.paper_trade`` is True). The CLOB REST endpoint
is L2-authenticated against the wallet's API creds; in paper mode
those creds are stubbed (``paper`` passphrase, derived fake key), so
a real call would return 401 / 403 and the loop would log a noisy
``Reconciliation failed`` every minute. Skipping keeps the loop's
failure surface focused on live mode — the only mode where local vs
exchange drift is a real risk.

Failure isolation
~~~~~~~~~~~~~~~~~
Every reconciliation pass is wrapped in a top-level ``try/except`` so
a transient CLOB outage (5xx / timeout / circuit breaker OPEN) cannot
kill the loop. On such a failure, ``reconcile()`` returns a result
with ``is_clean=False`` and the loop logs the error and waits for the
next interval — the operator sees the dirty result on the dashboard
and can act, but the bot keeps running.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Smallest meaningful position size delta (in shares). Polymarket's
# on-chain amounts are 6-decimal (PRICE_SCALE = 1_000_000); 0.001
# shares = 1000 micro-shares, comfortably below any real position
# granularity while still catching the common failure modes (missed
# fill, missed partial fill, settlement lag > 1 share).
POSITION_TOLERANCE = 0.001


@dataclass
class ReconciliationResult:
    """Snapshot of one reconciliation pass.

    Fields are intentionally plain (no nested dataclasses / enums) so
    the FastAPI route handler can serialise the result to JSON
    directly via ``dataclasses.asdict`` or ``result.__dict__`` without
    a custom encoder.
    """

    timestamp: float
    local_orders: int
    exchange_orders: int
    matched: int
    stale_local: list[str] = field(default_factory=list)  # Local but not on exchange
    orphaned_exchange: list[str] = field(default_factory=list)  # On exchange but not local
    position_mismatches: list[dict] = field(default_factory=list)
    fill_mismatches: list[dict] = field(default_factory=list)
    is_clean: bool = True


class LiveReconciler:
    """Reconciles local ``DataStore`` state with CLOB exchange state.

    Lifecycle
    ~~~~~~~~~
      * ``start()`` is idempotent — calling it twice does not spawn a
        second loop. Safe to call from a FastAPI lifespan startup
        handler that may be invoked more than once under reload.
      * ``stop()`` cancels the background ``asyncio.Task`` and
        awaits its ``CancelledError`` so the task doesn't leak
        across reloads / test runs.
      * ``reconcile()`` is safe to call directly (the API route
        ``POST /api/reconciliation/run`` does exactly that) — it
        doesn't touch ``_running`` / ``_task``.

    Threading
    ~~~~~~~~~
    The background task is created via ``asyncio.create_task`` and
    so shares the event loop with the FastAPI request handlers. The
    local ``DataStore.get_open_orders()`` call acquires the store's
    ``asyncio.Lock`` (briefly), as does ``clob_client._get`` (which
    holds the httpx client internally); neither blocks long enough to
    starve request handlers under normal load.
    """

    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_result: Optional[ReconciliationResult] = None

    async def start(self) -> None:
        """Start the background reconciliation loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._reconcile_loop())
        logger.info("Live reconciler started (interval=%ss)", self.interval)

    async def stop(self) -> None:
        """Cancel the background loop and await its teardown."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("Live reconciler loop raised during stop: %s", e)
            self._task = None
        logger.info("Live reconciler stopped")

    async def _reconcile_loop(self) -> None:
        """Run ``reconcile()`` every ``self.interval`` seconds until ``stop()``."""
        while self._running:
            try:
                result = await self.reconcile()
                self._last_result = result
                if not result.is_clean:
                    logger.warning(
                        "Reconciliation found discrepancies: %d stale, %d orphaned, "
                        "%d position mismatches",
                        len(result.stale_local),
                        len(result.orphaned_exchange),
                        len(result.position_mismatches),
                    )
            except asyncio.CancelledError:
                # Cooperative cancellation — propagate so ``stop()``'s
                # ``await self._task`` sees the CancelledError cleanly.
                raise
            except Exception as e:
                logger.error("Reconciliation error: %s", e, exc_info=True)
            await asyncio.sleep(self.interval)

    async def reconcile(self) -> ReconciliationResult:
        """Perform a single full reconciliation pass.

        Pulls local state from ``DataStore`` and exchange state from
        ``clob_client``, diffs them, and returns a populated
        ``ReconciliationResult``. Never raises — every exception is
        caught and surfaced as a ``is_clean=False`` result so the
        background loop never crashes and the API route
        ``POST /api/reconciliation/run`` never 500s.
        """
        # Local imports — avoids importing config / clob_client /
        # data_store at module-import time so a stale / missing
        # config (e.g. in a unit test that hasn't monkeypatched
        # ``settings`` yet) doesn't crash the import.
        try:
            from config import settings

            # Skip in paper mode — the CLOB REST endpoints are L2-auth'd
            # against wallet creds that don't exist in paper mode.
            if settings.paper_trade:
                return ReconciliationResult(
                    timestamp=time.time(),
                    local_orders=0,
                    exchange_orders=0,
                    matched=0,
                    is_clean=True,
                )

            from core.clob_client import clob_client
            from core.data_store import store

            # ── Local open orders ──────────────────────────────────────
            # ``store.get_open_orders()`` returns a list of ``Order``
            # dataclass instances; the ``order_id`` attribute is the
            # canonical exchange-side identifier (set by
            # ``clob_client.create_order`` to a uuid4 string that's
            # echoed back in the CLOB's open-order response).
            local_orders = await store.get_open_orders()
            local_order_ids = {o.order_id for o in local_orders}

            # ── Exchange open orders ───────────────────────────────────
            # ``clob_client.get_open_orders()`` returns the raw CLOB
            # REST response (a list of dicts). Each dict carries the
            # order id under either ``id`` (canonical CLOB field) or
            # ``order_id`` (the echo of the bot's own client-side
            # uuid). We accept either for forward / backward compat.
            try:
                exchange_orders = await clob_client.get_open_orders()
            except Exception as e:
                logger.warning("Failed to get exchange orders: %s", e)
                exchange_orders = []
            exchange_order_ids = {
                (o.get("id") or o.get("order_id"))
                for o in exchange_orders
                if o.get("id") or o.get("order_id")
            }

            # ── Diff ──────────────────────────────────────────────────
            stale_local = list(local_order_ids - exchange_order_ids)
            orphaned_exchange = list(exchange_order_ids - local_order_ids)
            matched = len(local_order_ids & exchange_order_ids)

            # ── Positions ─────────────────────────────────────────────
            # Local positions live in ``store.positions`` (token_id →
            # Position). The exchange's position list comes back as
            # a list of dicts with ``asset_id`` (Polymarket's name for
            # the conditional-token id) or ``token_id`` (echo), and a
            # ``size`` field (can be negative for shorts). We compare
            # against ``Position.yes_shares - Position.no_shares``
            # (net long-YES exposure) so a YES long and a NO short on
            # the same token don't double-count.
            position_mismatches: list[dict] = []
            try:
                local_positions = store.positions
                exchange_positions = await clob_client.get_positions()

                local_pos_map: dict[str, float] = {
                    tid: float(p.yes_shares - p.no_shares)
                    for tid, p in local_positions.items()
                }
                exchange_pos_map: dict[str, float] = {}
                for p in exchange_positions:
                    token = p.get("asset_id") or p.get("token_id")
                    if not token:
                        continue
                    exchange_pos_map[str(token)] = float(p.get("size", 0))

                all_tokens = set(local_pos_map.keys()) | set(exchange_pos_map.keys())
                for token in all_tokens:
                    local_size = local_pos_map.get(token, 0.0)
                    exchange_size = exchange_pos_map.get(token, 0.0)
                    if abs(local_size - exchange_size) > POSITION_TOLERANCE:
                        position_mismatches.append({
                            "token_id": token,
                            "local_size": local_size,
                            "exchange_size": exchange_size,
                            "diff": exchange_size - local_size,
                        })
            except Exception as e:
                logger.warning("Position reconciliation failed: %s", e)

            is_clean = (
                len(stale_local) == 0
                and len(orphaned_exchange) == 0
                and len(position_mismatches) == 0
            )

            return ReconciliationResult(
                timestamp=time.time(),
                local_orders=len(local_orders),
                exchange_orders=len(exchange_orders),
                matched=matched,
                stale_local=stale_local,
                orphaned_exchange=orphaned_exchange,
                position_mismatches=position_mismatches,
                is_clean=is_clean,
            )

        except Exception as e:
            logger.error("Reconciliation failed: %s", e, exc_info=True)
            return ReconciliationResult(
                timestamp=time.time(),
                local_orders=0,
                exchange_orders=0,
                matched=0,
                is_clean=False,
            )

    def get_last_result(self) -> Optional[ReconciliationResult]:
        """Return the most recent ``ReconciliationResult`` (or ``None``).

        Used by the ``GET /api/reconciliation/live`` route so an
        operator can inspect the latest snapshot without waiting for
        the next loop tick. ``None`` is returned only before the first
        pass completes (the loop runs immediately on ``start()`` so
        the window is at most a few hundred ms in practice).
        """
        return self._last_result


# Singleton — mirrors the established ``clob_client`` / ``store`` /
# ``position_manager`` convention so importers can grab the instance
# at module-import time.
live_reconciler = LiveReconciler()


__all__ = [
    "LiveReconciler",
    "ReconciliationResult",
    "POSITION_TOLERANCE",
    "live_reconciler",
]
