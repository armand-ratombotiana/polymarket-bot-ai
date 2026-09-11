"""
strategies/momentum.py — ROC (Rate of Change) Momentum Trader.

W19-6 — implements the unified strategy contract for the second of three
high-value strategies promoted from the PLANNED catalog.

Signal logic
------------
Maintains a rolling window of mid prices per token. On every cycle the
strategy computes the Rate of Change over the last ``ROC_PERIOD`` cycles::

    ROC = (price_now - price_n_periods_ago) / price_n_periods_ago

A BUY signal fires when ``ROC >= ROC_BUY_THRESHOLD`` (strong upward
momentum). A SELL signal fires when ``ROC <= ROC_SELL_THRESHOLD``
(strong downward momentum — momentum has reversed).

The thresholds are intentionally symmetric (±5% by default) so the
strategy is direction-agnostic. The same scan runs whether momentum
is rising or falling; only the direction of the resulting order
changes.

Order routing
-------------
Each signal is submitted via ``BaseStrategy.submit_order`` (the unified
contract): paper mode routes to ``paper_sim.create_order``, live mode
routes to ``clob_client.create_order``. The decision-ledger
``RISK_APPROVED`` / ``RISK_REJECTED`` / ``ORDER`` / ``FILL`` stages
are recorded automatically by the base class.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import OrderBook, Side, store
from core.gamma_client import gamma_client
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
ROC_PERIOD = 10              # 10-cycle ROC window
ROC_BUY_THRESHOLD = 0.05    # +5% ROC = strong upward momentum
ROC_SELL_THRESHOLD = -0.05   # -5% ROC = strong downward momentum
SCAN_INTERVAL = 30.0
HISTORY_MAX = 60
HISTORY_TOKEN_CAP = 200
MAX_SIGNALS_PER_SCAN = 3
STALE_ORDER_SECONDS = 300


@dataclass
class MomentumSignal:
    """Signal value object emitted by ``evaluate``."""
    token_id: str
    slug: str
    direction: Side
    target_price: float
    size_usdc: float
    roc: float
    reason: str
    decision_id: str = ""


class MomentumStrategy(BaseStrategy):
    """
    Rate-of-Change momentum trader.

    BUY when ``ROC >= ROC_BUY_THRESHOLD`` (momentum strongly positive).
    SELL when ``ROC <= ROC_SELL_THRESHOLD`` (momentum reversed).
    """

    name = "momentum"

    def __init__(self) -> None:
        super().__init__()
        self._roc_period: int = ROC_PERIOD
        self._buy_threshold: float = ROC_BUY_THRESHOLD
        self._sell_threshold: float = ROC_SELL_THRESHOLD
        self._base_size: float = float(getattr(settings, "signal_order_size_usdc", 1.5))
        self._active_signals: dict[str, str] = {}
        self._price_history: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._interval: float = SCAN_INTERVAL

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await store.log_event(
            "🚀 Momentum strategy active — scanning for ROC signals"
        )
        log.info(
            "[momentum] Active (period=%d, buy>=%.2f%%, sell<=%.2f%%)",
            self._roc_period,
            self._buy_threshold * 100,
            self._sell_threshold * 100,
        )

        while self._running:
            try:
                await self._scan_markets()
                await self._recycle_stale_orders()
            except Exception as e:
                log.error("[momentum] Scan error: %s", e)
            await asyncio.sleep(self._interval)

    # ── Price history ────────────────────────────────────────────────────────

    def _update_history(self, token_id: str, price: float) -> list[float]:
        """Append ``price`` to the rolling history for ``token_id``.

        Returns the history as a plain list (the caller needs ``__getitem__``
        on negative indices, which ``deque`` does support, but the
        conversion also lets us drop the live reference cleanly).
        """
        hist = self._price_history.get(token_id)
        if hist is None:
            hist = deque(maxlen=HISTORY_MAX)
            self._price_history[token_id] = hist
            if len(self._price_history) > HISTORY_TOKEN_CAP:
                self._price_history.popitem(last=False)
        hist.append(price)
        return list(hist)

    # ── Signal evaluation (pure) ─────────────────────────────────────────────

    def evaluate(self, book: OrderBook, prices: list[float]) -> MomentumSignal | None:
        """Compute ROC and return a signal if momentum is strong enough.

        Returns ``None`` when:
          - insufficient price history (``len(prices) < ROC_PERIOD + 1``)
          - the reference price ``ROC_PERIOD`` cycles ago is zero (div-by-zero)
          - ROC is in the neutral zone (between the buy and sell thresholds)
        """
        if len(prices) < self._roc_period + 1:
            return None
        current = prices[-1]
        past = prices[-self._roc_period - 1]
        if past <= 0.0:
            return None
        roc = (current - past) / past

        if roc >= self._buy_threshold:
            direction = Side.BUY
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"Momentum BUY: ROC={roc * 100:.2f}% "
                f"(≥ {self._buy_threshold * 100:.2f}%)"
            )
        elif roc <= self._sell_threshold:
            direction = Side.SELL
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"Momentum SELL: ROC={roc * 100:.2f}% "
                f"(≤ {self._sell_threshold * 100:.2f}%)"
            )
        else:
            # Neutral zone — momentum is not strong enough to act on.
            return None

        return MomentumSignal(
            token_id=book.token_id,
            slug=store.market_slugs.get(book.token_id, book.token_id[:12]),
            direction=direction,
            target_price=target_price,
            size_usdc=self._base_size,
            roc=roc,
            reason=reason,
        )

    # ── Market scan ──────────────────────────────────────────────────────────

    async def _scan_markets(self) -> None:
        try:
            from core.market_discovery import market_discovery
            catalog_items = list(market_discovery.catalog.items())
        except Exception:
            catalog_items = []

        if not catalog_items:
            try:
                raw_markets = await gamma_client.get_markets(
                    active=True, limit=60, order="volume24hr",
                )
                catalog_items = []
                for m in raw_markets:
                    tids = gamma_client.extract_token_ids(m)
                    if tids:
                        catalog_items.append((tids[0], m))
            except Exception as e:
                log.debug("[momentum] Gamma fallback failed: %s", e)
                return

        signals: list[MomentumSignal] = []
        for tid, mkt in catalog_items:
            try:
                book = await store.get_order_book(tid)
                if book is None or book.mid is None:
                    book_poller.add_tokens([tid])
                    continue
                prices = self._update_history(tid, book.mid)
                sig = self.evaluate(book, prices)
                if sig is not None:
                    signals.append(sig)
            except Exception as e:
                log.debug("[momentum] Market eval error: %s", e)

        if not signals:
            return

        # Sort by |ROC| descending — execute the strongest momentum signals
        # first so we don't waste the per-scan execution budget on a weak
        # signal when a stronger one is waiting.
        signals.sort(key=lambda s: abs(s.roc), reverse=True)
        for sig in signals[:MAX_SIGNALS_PER_SCAN]:
            await self._act_on_signal(sig)

    # ── Execution ────────────────────────────────────────────────────────────

    async def _act_on_signal(self, sig: MomentumSignal) -> None:
        if sig.token_id in self._active_signals:
            oid = self._active_signals[sig.token_id]
            if oid in store.open_orders:
                return
            self._active_signals.pop(sig.token_id, None)

        if sig.token_id in store.positions:
            return

        size_shares = max(1.0, sig.size_usdc / sig.target_price)
        args = OrderArgs(
            token_id=sig.token_id,
            price=sig.target_price,
            side=sig.direction,
            size=size_shares,
        )
        order = await self.submit_order(args, decision_id=sig.decision_id)
        if order:
            self._active_signals[sig.token_id] = order.order_id
            await store.log_event(
                f"🚀 Momentum: {sig.direction.value} {sig.slug} "
                f"@ {sig.target_price:.4f} (${sig.size_usdc:.1f}) — {sig.reason}"
            )

    async def _recycle_stale_orders(self) -> None:
        """Cancel unfilled signal orders after ``STALE_ORDER_SECONDS``."""
        now = time.time()
        for tid, oid in list(self._active_signals.items()):
            order = store.open_orders.get(oid)
            if order is None:
                self._active_signals.pop(tid, None)
                continue
            if now - order.created_at > STALE_ORDER_SECONDS:
                await self.cancel_order(oid)
                self._active_signals.pop(tid, None)
                await store.log_event(
                    f"♻️ Stale momentum order cancelled: "
                    f"{store.market_slugs.get(tid, tid[:12])}"
                )
