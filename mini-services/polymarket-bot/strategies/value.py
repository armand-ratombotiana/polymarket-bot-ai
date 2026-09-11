"""
strategies/value.py — ML Fair-Value Mispricing Trader.

W19-6 — implements the unified strategy contract for the third of three
high-value strategies promoted from the PLANNED catalog.

Signal logic
------------
For every active market the strategy:
  1. Extracts the 38-dim feature vector via ``ml.features.extract_features``.
  2. Asks the ML ensemble (``ml.model.ml_model.predict``) for its
     calibrated ``p_yes`` estimate and a ``confidence`` score.
  3. Computes ``edge = p_yes - market_mid``.
  4. Fires a BUY signal when ``edge >= MIN_EDGE`` and
     ``confidence >= MIN_CONFIDENCE`` (model says market is underpriced).
  5. Fires a SELL signal when ``edge <= -MIN_EDGE`` and
     ``confidence >= MIN_CONFIDENCE`` (model says market is overpriced).

A ``MIN_EDGE`` of 5% keeps the strategy out of the noise band where
the model's edge is too small to overcome the bid-ask spread + slippage
+ maker/taker fees. A wide-spread regime filter (``spread >= 0.04``)
guards against acting on a model prediction when the underlying book
is too illiquid to actually fill.

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
from collections import OrderedDict
from dataclasses import dataclass

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import OrderBook, Side, store
from core.gamma_client import gamma_client
from ml.features import extract_features
from ml.model import ml_model
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
MIN_EDGE = 0.05             # 5% |model_p - market_mid| required to act
MIN_CONFIDENCE = 0.45       # ML confidence floor
WIDE_SPREAD_CUTOFF = 0.04   # Skip markets with spreads ≥ 4% (illiquid)
SCAN_INTERVAL = 30.0
FEATURE_CACHE_MAX = 200
MAX_SIGNALS_PER_SCAN = 3
STALE_ORDER_SECONDS = 300


@dataclass
class ValueSignal:
    """Signal value object emitted by ``evaluate``."""
    token_id: str
    slug: str
    direction: Side
    target_price: float
    size_usdc: float
    model_p: float
    market_mid: float
    edge: float
    confidence: float
    reason: str
    decision_id: str = ""


class ValueStrategy(BaseStrategy):
    """
    ML fair-value mispricing trader.

    BUY when ``model_p - market_mid >= MIN_EDGE`` (market underpriced).
    SELL when ``market_mid - model_p >= MIN_EDGE`` (market overpriced).
    """

    name = "value"

    def __init__(self) -> None:
        super().__init__()
        self._min_edge: float = MIN_EDGE
        self._min_confidence: float = MIN_CONFIDENCE
        self._wide_spread_cutoff: float = WIDE_SPREAD_CUTOFF
        self._base_size: float = float(getattr(settings, "signal_order_size_usdc", 1.5))
        self._active_signals: dict[str, str] = {}
        self._feature_cache: "OrderedDict[str, object]" = OrderedDict()
        self._market_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._interval: float = SCAN_INTERVAL

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await store.log_event(
            "💎 Value strategy active — scanning for ML mispricing signals"
        )
        log.info(
            "[value] Active (min_edge=%.2f%%, min_conf=%.2f, spread_cutoff=%.2f%%)",
            self._min_edge * 100,
            self._min_confidence,
            self._wide_spread_cutoff * 100,
        )

        while self._running:
            try:
                await self._scan_markets()
                await self._recycle_stale_orders()
            except Exception as e:
                log.error("[value] Scan error: %s", e)
            await asyncio.sleep(self._interval)

    # ── Signal evaluation (pure) ─────────────────────────────────────────────

    def evaluate(self, book: OrderBook, features) -> ValueSignal | None:
        """Compute ML fair value vs market mid and return a signal if
        the market is mispriced enough to act on.

        Returns ``None`` when:
          - ``features`` is None (extract_features failed for this book)
          - the ML model is not fitted yet (cold-start)
          - ``predict`` raises (model is in a broken state)
          - confidence is below ``MIN_CONFIDENCE``
          - ``|edge| < MIN_EDGE``
          - the book's spread is wider than ``WIDE_SPREAD_CUTOFF``
        """
        if features is None:
            return None
        if not ml_model.is_fitted:
            return None

        try:
            p_yes, confidence = ml_model.predict(features, token_id=book.token_id)
        except Exception as e:
            log.debug("[value] predict() failed: %s", e)
            return None

        mid = book.mid or 0.5
        spread = book.spread or 0.01
        edge = p_yes - mid

        # Confidence floor — the model must be sure enough of itself
        # before we act on its edge estimate.
        if confidence < self._min_confidence:
            return None

        # Edge floor — anything inside ±MIN_EDGE is noise (won't overcome
        # spread + slippage + fees after the trade lands).
        if abs(edge) < self._min_edge:
            return None

        # Wide-spread regime filter — a 4%+ spread means the book is too
        # illiquid to fill at the model's predicted fair value. Skip.
        if spread >= self._wide_spread_cutoff:
            return None

        if edge > 0:
            # Market is underpriced relative to model — BUY.
            direction = Side.BUY
            target_price = round(min(mid + 0.005, 0.98), 4)
            reason = (
                f"Value BUY: model_p={p_yes:.3f} vs mid={mid:.3f} "
                f"(edge=+{edge * 100:.2f}%, conf={confidence:.2f})"
            )
        else:
            # Market is overpriced relative to model — SELL.
            direction = Side.SELL
            target_price = round(max(mid - 0.005, 0.02), 4)
            reason = (
                f"Value SELL: model_p={p_yes:.3f} vs mid={mid:.3f} "
                f"(edge={edge * 100:.2f}%, conf={confidence:.2f})"
            )

        return ValueSignal(
            token_id=book.token_id,
            slug=store.market_slugs.get(book.token_id, book.token_id[:12]),
            direction=direction,
            target_price=target_price,
            size_usdc=self._base_size,
            model_p=p_yes,
            market_mid=mid,
            edge=edge,
            confidence=confidence,
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
                log.debug("[value] Gamma fallback failed: %s", e)
                return

        signals: list[ValueSignal] = []
        for tid, mkt in catalog_items:
            try:
                book = await store.get_order_book(tid)
                if book is None or book.mid is None:
                    book_poller.add_tokens([tid])
                    continue
                # Cache the features so the model's online-learner can be
                # updated later (mirrors signal_trader's pattern).
                features = extract_features(mkt, book)
                if features is not None:
                    self._feature_cache[tid] = features
                    self._market_cache[tid] = mkt
                    if len(self._feature_cache) > FEATURE_CACHE_MAX:
                        self._feature_cache.popitem(last=False)
                        self._market_cache.popitem(last=False)
                sig = self.evaluate(book, features)
                if sig is not None:
                    signals.append(sig)
            except Exception as e:
                log.debug("[value] Market eval error: %s", e)

        if not signals:
            return

        # Execute the highest-edge signals first — sorted by |edge|
        # descending so we trade the most mispriced markets before
        # exhausting the per-scan execution budget.
        signals.sort(key=lambda s: abs(s.edge), reverse=True)
        for sig in signals[:MAX_SIGNALS_PER_SCAN]:
            await self._act_on_signal(sig)

    # ── Execution ────────────────────────────────────────────────────────────

    async def _act_on_signal(self, sig: ValueSignal) -> None:
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
                f"💎 Value: {sig.direction.value} {sig.slug} "
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
                    f"♻️ Stale value order cancelled: "
                    f"{store.market_slugs.get(tid, tid[:12])}"
                )
