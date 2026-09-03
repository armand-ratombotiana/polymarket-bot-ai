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
from collections import OrderedDict
from dataclasses import dataclass

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

SCAN_INTERVAL = 15.0        # Scan interval in seconds (uses pre-polled store.order_books)
MODEL_SAVE_INTERVAL = 300   # Save model every 5 minutes
KELLY_FRACTION = 0.25       # Quarter-Kelly for conservative bankroll management
STALE_ORDER_SECONDS = 180   # Cancel unfilled signal orders after 3 minutes
FEATURE_CACHE_MAX = 500     # Bound feature cache to prevent unbounded memory growth
MIN_KELLY_NUMERATOR = 0.02  # Minimum raw Kelly f* numerator: (p*b - (1-p)) > 2%


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
    # R11 — Unified Decision Ledger linkage. Populated by _ml_signal() so the
    # downstream submit_order path can record RISK_APPROVED / RISK_REJECTED /
    # ORDER / FILL stages against the originating prediction chain.
    decision_id: str = ""


class SignalTraderStrategy(BaseStrategy):
    """
    ML-Driven Directional Prediction Market Trader.
    Evaluates order books and momentum signals to take high-conviction positions.
    """

    name = "signal_trader"

    def __init__(self) -> None:
        super().__init__()
        # Lowered confidence floor from 0.55 → 0.45 so moderately-confident ML
        # predictions can actually fire trades. The p_yes directional thresholds
        # (0.55 / 0.45) below still filter for genuine edge; the confidence gate
        # now only filters out low-certainty model outputs, not borderline signals.
        self._min_confidence = max(0.45, settings.signal_min_confidence)
        self._base_order_size = settings.signal_order_size_usdc
        self._active_signals: dict[str, str] = {}
        # Bounded OrderedDict: evicts oldest entries when capacity is reached
        self._feature_cache: OrderedDict = OrderedDict()
        self._market_cache: OrderedDict = OrderedDict()
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
        await self._recycle_stale_orders()

        # Use pre-indexed market_discovery catalog (800+ markets) instead of a
        # fresh Gamma API fetch — avoids redundant HTTP calls and covers the full
        # market universe already polled by book_poller.
        try:
            from core.market_discovery import market_discovery
            # Iterate (token_id, market_dict) tuples directly. The normalized
            # records in `market_discovery.catalog` carry `token_id` as a top-
            # level field but do NOT preserve the raw `tokens` array, so calling
            # `gamma_client.extract_token_ids(mkt)` on them returns `[]` and the
            # entire scan silently no-ops. Using the catalog key avoids that.
            catalog_items = list(market_discovery.catalog.items())
        except Exception:
            catalog_items = []

        # Fall back to Gamma API if catalog is empty (first startup race).
        # Raw Gamma markets DO have the `tokens` array, so we normalize them
        # into (token_id, mkt) tuples via extract_token_ids to keep the
        # downstream loop uniform.
        if not catalog_items:
            try:
                raw_markets = await gamma_client.get_markets(active=True, limit=60, order="volume24hr")
                catalog_items = []
                for m in raw_markets:
                    tids = gamma_client.extract_token_ids(m)
                    if tids:
                        catalog_items.append((tids[0], m))
            except Exception as e:
                log.debug("[signal_trader] Gamma fallback failed: %s", e)
                return

        signals: list[MarketSignal] = []
        for tid, mkt in catalog_items:
            try:
                sig = await self._evaluate_market(mkt, token_id=tid)
                if sig and sig.confidence >= self._min_confidence:
                    signals.append(sig)
            except Exception as e:
                log.debug("[signal_trader] Market evaluation error: %s", e)

        # U9 — Observability: best-effort scan telemetry (additive; never breaks the scan).
        try:
            from core.observability import record_metric
            record_metric("strategy", "signal_trader.evaluations", len(catalog_items))
            record_metric("strategy", "signal_trader.signals", len(signals))
            record_metric("strategy", "signal_trader.rejected", len(catalog_items) - len(signals))
        except: pass

        if not signals:
            return

        signals.sort(key=lambda s: s.confidence, reverse=True)
        # Execute top 3 highest-conviction signals per scan cycle
        for sig in signals[:3]:
            await self._act_on_signal(sig)

    async def _evaluate_market(self, mkt: dict, token_id: str | None = None) -> MarketSignal | None:
        # When called from the catalog scan path, `token_id` is supplied
        # directly (it's the catalog key) and we skip `extract_token_ids`
        # entirely — that helper returns `[]` for normalized records that lack
        # the raw `tokens` array, which was silently dropping every market.
        # When called without `token_id` (legacy / fallback path with raw
        # Gamma markets), we fall back to `extract_token_ids` as before.
        if token_id is None:
            token_ids = gamma_client.extract_token_ids(mkt)
            if not token_ids:
                return None
            yes_token = token_ids[0]
        else:
            yes_token = token_id

        slug = mkt.get("slug") or mkt.get("groupItemTitle") or yes_token[:12]
        store.market_slugs[yes_token] = slug

        book = await store.get_order_book(yes_token)
        if book is None:
            book_poller.add_tokens([yes_token])
            return None

        features = extract_features(mkt, book)
        if features is not None:
            # Bounded cache: evict oldest when full
            self._feature_cache[yes_token] = features
            if len(self._feature_cache) > FEATURE_CACHE_MAX:
                self._feature_cache.popitem(last=False)
            self._market_cache[yes_token] = mkt
            if len(self._market_cache) > FEATURE_CACHE_MAX:
                self._market_cache.popitem(last=False)
            return self._ml_signal(yes_token, slug, mkt, book, features)

        return None

    # ── Kelly Sizing & ML Scoring ─────────────────────────────────────────────

    @staticmethod
    def _emit_ledger(coro) -> None:
        """
        Fire-and-forget an async decision-ledger write.

        _ml_signal is synchronous (it returns a MarketSignal | None directly),
        but the decision ledger's writes are async. We schedule them on the
        running loop without awaiting so the strategy's scan cadence is never
        blocked by SQLite I/O. Any exception is swallowed by the ledger
        itself (it logs at error level), and a missing/Stopped loop is
        caught here so the strategy never crashes on ledger plumbing.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if not loop.is_running():
            return
        try:
            asyncio.ensure_future(coro, loop=loop)
        except Exception as e:
            log.debug("[signal_trader] ledger emit failed: %s", e)

    def _emit_rejection(
        self,
        token_id: str,
        decision_id: str,
        predicted_edge: float,
        confidence: float,
        reason: str,
        market_mid: float | None,
    ) -> None:
        """Best-effort fire-and-forget rejection record to the decision ledger."""
        try:
            from core.decision_ledger import decision_ledger
            self._emit_ledger(
                decision_ledger.record_rejection(
                    token_id=token_id,
                    strategy=self.name,
                    predicted_edge=predicted_edge,
                    confidence=confidence,
                    reason=reason,
                    market_mid=market_mid,
                    decision_id=decision_id,
                )
            )
        except Exception as e:
            log.debug("[signal_trader] decision_ledger import failed: %s", e)

    def _ml_signal(
        self, token_id: str, slug: str, mkt: dict, book: OrderBook, features
    ) -> MarketSignal | None:
        # R11 — generate the unified decision_id up-front so every stage
        # (PREDICTION, SIGNAL, RISK_*, ORDER, FILL) and every rejection path
        # share the same trace key.
        try:
            from core.decision_ledger import decision_ledger
            dec_id = decision_ledger.new_decision_id()
        except Exception as e:
            log.debug("[signal_trader] decision_ledger import failed: %s", e)
            decision_ledger = None  # type: ignore[assignment]
            dec_id = ""

        p_yes, confidence = ml_model.predict(features, token_id=token_id)

        mid = book.mid or 0.5
        spread = book.spread or 0.01
        predicted_edge = p_yes - mid

        # PREDICTION stage — recorded for every evaluated market, whether or
        # not the signal is later accepted, so rejected predictions still
        # leave a traceable chain in the ledger.
        if decision_ledger is not None and dec_id:
            self._emit_ledger(
                decision_ledger.record(
                    decision_id=dec_id,
                    stage="PREDICTION",
                    token_id=token_id,
                    strategy=self.name,
                    pnl=0.0,
                    p_yes=p_yes,
                    confidence=confidence,
                    market_mid=mid,
                    spread=spread,
                    predicted_edge=predicted_edge,
                )
            )

        if confidence < self._min_confidence:
            self._emit_rejection(
                token_id, dec_id, predicted_edge, confidence,
                "low_confidence", mid,
            )
            return None

        # Regime filter: skip directional signals in high-volatility / wide-spread regimes.
        # The ensemble is not calibrated for liquidation dynamics under extreme vol.
        if spread >= 0.04:
            self._emit_rejection(
                token_id, dec_id, predicted_edge, confidence,
                "wide_spread", mid,
            )
            return None

        # Raised thresholds: 0.52/0.48 → 0.55/0.45 — eliminates low-conviction noise trades
        if p_yes >= 0.55:
            direction = Side.BUY
            if book.best_ask is not None:
                target_price = round(min(book.best_ask + 0.001, 0.98), 4)
            else:
                target_price = round(min(mid + 0.01, 0.98), 4)
            win_prob = p_yes
            payout_ratio = (1.0 - target_price) / max(target_price, 0.01)
        elif p_yes <= 0.45:
            direction = Side.SELL
            if book.best_bid is not None:
                target_price = round(max(book.best_bid - 0.001, 0.02), 4)
            else:
                target_price = round(max(mid - 0.01, 0.02), 4)
            win_prob = 1.0 - p_yes
            payout_ratio = target_price / max(1.0 - target_price, 0.01)
        else:
            self._emit_rejection(
                token_id, dec_id, predicted_edge, confidence,
                "neutral_zone", mid,
            )
            return None

        # Fractional Kelly Position Sizing
        # Kelly: f* = (p * b - (1 - p)) / b
        kelly_numerator = win_prob * payout_ratio - (1.0 - win_prob)

        # Minimum edge guard: raw Kelly numerator must exceed 2% for the trade
        # to have genuine expected value after fees and slippage.
        if kelly_numerator <= MIN_KELLY_NUMERATOR:
            self._emit_rejection(
                token_id, dec_id, kelly_numerator, confidence,
                "insufficient_kelly_edge", mid,
            )
            return None

        kelly_f = max(0.0, kelly_numerator / max(payout_ratio, 0.01))
        kelly_f = min(0.3, kelly_f * KELLY_FRACTION)  # capped at 30% max

        # Scale against the USD 100 operating capital, hard-capped by the
        # per-market ceiling ($3) so Kelly never overrides dollar limits.
        size_usdc = max(0.5, min(float(MAX_POSITION_PER_MARKET), BANKROLL_BASELINE * kelly_f))

        reason_str = f"ML Prob={p_yes:.1%} (Kelly {kelly_f*100:.1f}%, edge={kelly_numerator*100:.1f}%)"

        # SIGNAL stage — recorded only for signals that survive all gates.
        if decision_ledger is not None and dec_id:
            self._emit_ledger(
                decision_ledger.record(
                    decision_id=dec_id,
                    stage="SIGNAL",
                    token_id=token_id,
                    strategy=self.name,
                    pnl=0.0,
                    direction=direction.value,
                    target_price=target_price,
                    size_usdc=size_usdc,
                    kelly_f=kelly_f,
                    kelly_numerator=kelly_numerator,
                    win_prob=win_prob,
                    payout_ratio=payout_ratio,
                    p_yes=p_yes,
                    confidence=confidence,
                    market_mid=mid,
                    reason=reason_str,
                )
            )

        return MarketSignal(
            token_id=token_id,
            slug=slug,
            direction=direction,
            confidence=confidence,
            target_price=target_price,
            size_usdc=size_usdc,
            reason=reason_str,
            ml_score=p_yes,
            source="ml",
            decision_id=dec_id,
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _recycle_stale_orders(self) -> None:
        """Cancel unfilled orders after STALE_ORDER_SECONDS so tokens free up."""
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
                    f"♻️ Stale signal order cancelled: {store.market_slugs.get(tid, tid[:12])}"
                )

    async def _act_on_signal(self, sig: MarketSignal) -> None:
        if sig.token_id in self._active_signals:
            oid = self._active_signals[sig.token_id]
            if oid in store.open_orders:
                return

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
        # R11 — propagate the decision_id so submit_order can record
        # RISK_APPROVED / RISK_REJECTED against the originating chain.
        order = await self.submit_order(args, decision_id=sig.decision_id)
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
