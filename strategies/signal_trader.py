"""
strategies/signal_trader.py — ML-Powered Directional Signal Trader with Kelly Sizing.

Features:
  - Random Forest + SGD Online Classifier Ensemble
  - Fractional Kelly Criterion position sizing:
      f* = (p * b - (1 - p)) / b
      size = Portfolio_Capital * f* * Kelly_Fraction
  - Online learning directly from market price discovery & resolution
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import BANKROLL_BASELINE, OrderBook, Side, store
from core.gamma_client import gamma_client
from ml.features import extract_features
from ml.model import ml_model
from risk.manager import MAX_POSITION_PER_MARKET
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

SCAN_INTERVAL = 60.0        # Scan interval in seconds
MODEL_SAVE_INTERVAL = 300   # Save model every 5 minutes
KELLY_FRACTION = 0.25       # Quarter-Kelly for conservative bankroll management


@dataclass
class MarketSignal:
    token_id: str
    slug: str
    direction: Side
    confidence: float
    target_price: float
    size_usdc: float
    reason: str
    ml_score: float
    source: str


class SignalTraderStrategy(BaseStrategy):
    """
    ML-Driven Directional Prediction Market Trader.
    Evaluates order books and momentum signals to take high-conviction positions.
    """

    name = "signal_trader"

    def __init__(self) -> None:
        super().__init__()
        self._min_confidence = max(0.55, settings.signal_min_confidence)
        self._base_order_size = settings.signal_order_size_usdc
        self._active_signals: Dict[str, str] = {}
        self._feature_cache: Dict[str, object] = {}
        self._market_cache: Dict[str, dict] = {}
        self._last_model_save = time.time()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await store.log_event("🧠 ML Signal Trader started — evaluating market signals")
        log.info("[signal_trader] Signal Trader active (ML model ready)")

        while self._running:
            try:
                await self._scan_markets()
                await self._maybe_save_model()
            except Exception as e:
                log.error("[signal_trader] Scan error: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _maybe_save_model(self) -> None:
        if time.time() - self._last_model_save > MODEL_SAVE_INTERVAL:
            await asyncio.to_thread(ml_model.save)
            self._last_model_save = time.time()

    # ── Market Scan ───────────────────────────────────────────────────────────

    async def _scan_markets(self) -> None:
        markets = await gamma_client.get_markets(active=True, limit=40, order="volume24hr")
        signals: List[MarketSignal] = []

        for mkt in markets:
            try:
                sig = await self._evaluate_market(mkt)
                if sig and sig.confidence >= self._min_confidence:
                    signals.append(sig)
            except Exception as e:
                log.debug("[signal_trader] Market evaluation error: %s", e)

        if not signals:
            return

        signals.sort(key=lambda s: s.confidence, reverse=True)
        # Execute top 2 highest-conviction signals per scan cycle
        for sig in signals[:2]:
            await self._act_on_signal(sig)

    async def _evaluate_market(self, mkt: dict) -> Optional[MarketSignal]:
        token_ids = gamma_client.extract_token_ids(mkt)
        if not token_ids:
            return None

        yes_token = token_ids[0]
        slug = mkt.get("slug") or mkt.get("groupItemTitle") or yes_token[:12]
        store.market_slugs[yes_token] = slug

        book = await store.get_order_book(yes_token)
        if book is None:
            book_poller.add_tokens([yes_token])
            return None

        features = extract_features(mkt, book)
        if features is not None:
            self._feature_cache[yes_token] = features
            self._market_cache[yes_token] = mkt
            return self._ml_signal(yes_token, slug, mkt, book, features)

        return None

    # ── Kelly Sizing & ML Scoring ─────────────────────────────────────────────

    def _ml_signal(
        self, token_id: str, slug: str, mkt: dict, book: OrderBook, features
    ) -> Optional[MarketSignal]:
        p_yes, confidence = ml_model.predict(features)

        if confidence < self._min_confidence:
            return None

        mid = book.mid or 0.5

        if p_yes >= 0.52:
            direction = Side.BUY
            target_price = round(min(mid + 0.01, 0.98), 4)
            win_prob = p_yes
            payout_ratio = (1.0 - target_price) / max(target_price, 0.01)
        elif p_yes <= 0.48:
            direction = Side.SELL
            target_price = round(max(mid - 0.01, 0.02), 4)
            win_prob = 1.0 - p_yes
            payout_ratio = target_price / max(1.0 - target_price, 0.01)
        else:
            return None

        # Fractional Kelly Position Sizing
        # Kelly: f* = (p * b - (1 - p)) / b
        kelly_f = max(0.0, (win_prob * payout_ratio - (1.0 - win_prob)) / max(payout_ratio, 0.01))
        kelly_f = min(0.3, kelly_f * KELLY_FRACTION)  # capped at 30% max

        # Scale against the USD 100 operating capital, hard-capped by the
        # per-market ceiling ($3) so Kelly never overrides dollar limits.
        size_usdc = max(0.5, min(float(MAX_POSITION_PER_MARKET), BANKROLL_BASELINE * kelly_f))

        return MarketSignal(
            token_id=token_id,
            slug=slug,
            direction=direction,
            confidence=confidence,
            target_price=target_price,
            size_usdc=size_usdc,
            reason=f"ML Prob={p_yes:.1%} (Kelly {kelly_f*100:.1f}%)",
            ml_score=p_yes,
            source="ml",
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _act_on_signal(self, sig: MarketSignal) -> None:
        if sig.token_id in self._active_signals:
            oid = self._active_signals[sig.token_id]
            if oid in store.open_orders:
                return

        size_shares = max(1.0, sig.size_usdc / sig.target_price)
        args = OrderArgs(
            token_id=sig.token_id,
            price=sig.target_price,
            side=sig.direction,
            size=size_shares,
        )
        order = await self.submit_order(args)
        if order:
            self._active_signals[sig.token_id] = order.order_id
            await store.log_event(
                f"🤖 ML Trade: {sig.direction.value} {sig.slug} @ {sig.target_price:.4f} "
                f"(${sig.size_usdc:.1f}) — {sig.reason}"
            )

    async def record_outcome(self, token_id: str, resolved_yes: bool) -> None:
        features = self._feature_cache.get(token_id)
        if features is not None:
            await asyncio.to_thread(ml_model.update, features, resolved_yes)
            await store.log_event(
                f"📚 ML model updated with resolved outcome for {store.market_slugs.get(token_id, token_id[:12])}"
            )
