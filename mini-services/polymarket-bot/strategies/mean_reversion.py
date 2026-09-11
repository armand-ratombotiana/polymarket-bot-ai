"""
strategies/mean_reversion.py — Bollinger Bands Mean-Reversion Trader.

W19-6 — implements the unified strategy contract for the third of three
high-value strategies promoted from the PLANNED catalog (alongside
``momentum.py`` and ``value.py``).

Signal logic
------------
Maintains a rolling window of mid prices per token. On every cycle the
strategy computes the simple moving average (MA) and standard deviation
(sigma) over the window, then derives Bollinger Bands::

    upper = MA + k * sigma
    lower = MA - k * sigma

A BUY signal fires when the current mid touches or breaches the lower
band (price is "stretched down" — expect mean reversion to MA). A SELL
signal fires when the current mid touches or breaches the upper band.

A ``MIN_DEVIATION`` floor prevents acting on tiny sigma regimes (where
a 2-sigma breach might be only a few basis points below the MA — not
a genuine reversion opportunity). The strategy never stacks orders
on a token that already has an open order or an open position.

Order routing
-------------
Each signal is submitted via ``BaseStrategy.submit_order`` (the
unified contract): paper mode routes to ``paper_sim.create_order``,
live mode routes to ``clob_client.create_order``. The decision-ledger
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
WINDOW = 20                # 20-cycle SMA window (industry-standard Bollinger length)
K_SIGMA = 2.0              # 2-sigma bands (industry standard; original stub said 2.5)
MIN_DEVIATION = 0.02       # 2% — |price - MA| must exceed this to fire a signal
SCAN_INTERVAL = 30.0       # Re-scan every 30 s
HISTORY_MAX = 60           # Rolling price-history cap (per token)
HISTORY_TOKEN_CAP = 200    # Bound the number of tracked tokens
MAX_SIGNALS_PER_SCAN = 3
STALE_ORDER_SECONDS = 300  # Cancel unfilled signal orders after 5 min


@dataclass
class MeanReversionSignal:
    """Signal value object emitted by ``evaluate``."""
    token_id: str
    slug: str
    direction: Side
    target_price: float
    size_usdc: float
    upper_band: float
    lower_band: float
    ma: float
    sigma: float
    reason: str
    decision_id: str = ""


class MeanReversionStrategy(BaseStrategy):
    """
    Bollinger Bands mean-reversion trader.

    BUY when price breaches the lower band (stretched down — expect
    reversion to MA). SELL when price breaches the upper band.
    """

    name = "mean_reversion"

    def __init__(self) -> None:
        super().__init__()
        self._window: int = WINDOW
        self._k_sigma: float = K_SIGMA
        self._min_deviation: float = MIN_DEVIATION
        self._base_size: float = float(getattr(settings, "signal_order_size_usdc", 1.5))
        self._active_signals: dict[str, str] = {}
        self._price_history: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._interval: float = SCAN_INTERVAL

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await store.log_event(
            "📉 Mean Reversion strategy active — scanning for Bollinger Band signals"
        )
        log.info(
            "[mean_reversion] Active (window=%d, k=%.1f, min_dev=%.2f%%)",
            self._window, self._k_sigma, self._min_deviation * 100,
        )

        while self._running:
            try:
                await self._scan_markets()
                await self._recycle_stale_orders()
            except Exception as e:
                log.error("[mean_reversion] Scan error: %s", e)
            await asyncio.sleep(self._interval)

    # ── Price history ────────────────────────────────────────────────────────

    def _update_history(self, token_id: str, price: float) -> list[float]:
        """Append ``price`` to the rolling history for ``token_id``.

        Returns the in-memory history as a plain list (the caller needs
        indexing on it; converting once avoids leaking the deque).
        """
        hist = self._price_history.get(token_id)
        if hist is None:
            hist = deque(maxlen=HISTORY_MAX)
            self._price_history[token_id] = hist
            # Bound the token set so a 10 000-market catalog can't OOM us.
            if len(self._price_history) > HISTORY_TOKEN_CAP:
                self._price_history.popitem(last=False)
        hist.append(price)
        return list(hist)

    # ── Signal evaluation (pure) ─────────────────────────────────────────────

    def evaluate(self, book: OrderBook, prices: list[float]) -> MeanReversionSignal | None:
        """Compute Bollinger Bands and return a signal if the current
        price has deviated enough from the MA.

        Returns ``None`` when:
          - insufficient price history (``len(prices) < window``)
          - sigma collapsed (zero vol — no signal possible)
          - ``|price - MA| < MIN_DEVIATION`` (no meaningful stretch)
          - price is inside the bands (no breach)
        """
        if len(prices) < self._window:
            return None
        window = prices[-self._window:]
        ma = sum(window) / self._window
        variance = sum((p - ma) ** 2 for p in window) / self._window
        sigma = variance ** 0.5

        # Zero-vol regime: bands collapse to the MA — no edge to trade.
        if sigma < 1e-6:
            return None

        upper = ma + self._k_sigma * sigma
        lower = ma - self._k_sigma * sigma
        current = prices[-1]
        deviation = current - ma

        # A "stretched" price that's only 0.5% from MA isn't a reversion
        # signal — it's noise. Require at least MIN_DEVIATION of stretch.
        if abs(deviation) < self._min_deviation:
            return None

        if current <= lower:
            direction = Side.BUY
            # Sit slightly inside the band to improve fill odds.
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"Bollinger BUY: price={current:.4f} ≤ lower={lower:.4f} "
                f"(MA={ma:.4f}, σ={sigma:.4f})"
            )
        elif current >= upper:
            direction = Side.SELL
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"Bollinger SELL: price={current:.4f} ≥ upper={upper:.4f} "
                f"(MA={ma:.4f}, σ={sigma:.4f})"
            )
        else:
            # Inside the bands — no breach, no signal.
            return None

        return MeanReversionSignal(
            token_id=book.token_id,
            slug=store.market_slugs.get(book.token_id, book.token_id[:12]),
            direction=direction,
            target_price=target_price,
            size_usdc=self._base_size,
            upper_band=upper,
            lower_band=lower,
            ma=ma,
            sigma=sigma,
            reason=reason,
        )

    # ── Market scan ──────────────────────────────────────────────────────────

    async def _scan_markets(self) -> None:
        try:
            from core.market_discovery import market_discovery
            catalog_items = list(market_discovery.catalog.items())
        except Exception:
            catalog_items = []

        # Fall back to a fresh Gamma API fetch when the catalog is empty
        # (first-startup race: ``market_discovery`` hasn't been seeded).
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
                log.debug("[mean_reversion] Gamma fallback failed: %s", e)
                return

        signals: list[MeanReversionSignal] = []
        for tid, mkt in catalog_items:
            try:
                book = await store.get_order_book(tid)
                if book is None or book.mid is None:
                    # Subscribe to future book updates; we'll see this
                    # token again next scan once book_poller has populated
                    # its order_books cache.
                    book_poller.add_tokens([tid])
                    continue
                prices = self._update_history(tid, book.mid)
                sig = self.evaluate(book, prices)
                if sig is not None:
                    signals.append(sig)
            except Exception as e:
                log.debug("[mean_reversion] Market eval error: %s", e)

        if not signals:
            return

        # Execute the top ``MAX_SIGNALS_PER_SCAN`` signals — no prioritisation
        # here (mean-reversion signals don't have a natural ordering), so we
        # take them in catalog order which is deterministic.
        for sig in signals[:MAX_SIGNALS_PER_SCAN]:
            await self._act_on_signal(sig)

    # ── Execution ────────────────────────────────────────────────────────────

    async def _act_on_signal(self, sig: MeanReversionSignal) -> None:
        # If there's already a resting signal order for this token, skip
        # until it either fills or is recycled as stale.
        if sig.token_id in self._active_signals:
            oid = self._active_signals[sig.token_id]
            if oid in store.open_orders:
                return
            self._active_signals.pop(sig.token_id, None)

        # One directional position per market at a time — never stack.
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
                f"📉 Mean Reversion: {sig.direction.value} {sig.slug} "
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
                    f"♻️ Stale mean-reversion order cancelled: "
                    f"{store.market_slugs.get(tid, tid[:12])}"
                )
