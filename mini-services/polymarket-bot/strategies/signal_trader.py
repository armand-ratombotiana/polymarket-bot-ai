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
# V2 — Capital allocator sizing. The allocator is now the single source
# of truth for position size in `_ml_signal`; the previous inline Kelly
# sizing is preserved as a comment at the call site (see "Keep old Kelly
# as comment" in the V2 task spec). The allocator is a pure, stateless,
# synchronous function with no import-time side effects, so a top-level
# import here is safe (unlike `core.decision_ledger` /
# `core.market_discovery` which remain lazy-imported inside methods to
# defer their DB / singleton initialization).
from core.capital_allocator import allocate_capital
from core.clob_client import OrderArgs
from core.data_store import BANKROLL_BASELINE, OrderBook, Side, store
from core.gamma_client import gamma_client
from ml.features import extract_features
from ml.model import ml_model
from risk.manager import MAX_POSITION_PER_MARKET
from strategies.base import BaseStrategy, Signal

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
            asyncio.create_task(record_metric("strategy", "signal_trader.evaluations", len(catalog_items)))
            asyncio.create_task(record_metric("strategy", "signal_trader.signals", len(signals)))
            asyncio.create_task(record_metric("strategy", "signal_trader.rejected", len(catalog_items) - len(signals)))
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

        # W19-8 — A/B test assignment. Consult ``ab_test.assign_model`` to
        # decide which model version to invoke for this token, then look up
        # the actual callable via ``get_model_for_version``. When no
        # experiment is active OR the assigned version is the champion,
        # ``get_model_for_version`` falls back to the global ``ml_model``
        # (no behaviour change vs the pre-W19-8 path). When the assigned
        # version is a registered challenger, the challenger callable is
        # invoked INSTEAD — its prediction is what drives the downstream
        # signal + Kelly sizing + order submission. The prediction is then
        # recorded against its version via ``record_prediction`` so the A/B
        # framework can evaluate the arm once outcomes resolve. Bare
        # try/except so any A/B-framework hiccup falls back to the
        # champion path (zero production impact).
        model_version = "champion"
        try:
            from ml.ab_testing import ab_test
            model_version = ab_test.assign_model(token_id)
            active_model = ab_test.get_model_for_version(model_version, default=ml_model)
        except Exception as e:
            log.debug("[signal_trader] A/B test assign failed — champion fallback: %s", e)
            active_model = ml_model

        # W19-3 — record the pre-prediction snapshots (market / intelligence /
        # ML features) against the same ``dec_id`` so the 12-stage decision
        # chain can be reconstructed end-to-end. ADDITIVE: each emit is wrapped
        # in try/except (the existing ``_emit_ledger`` helper already
        # swallows fire-and-forget failures) so a ledger hiccup can never
        # break the strategy scan. These snapshots are the only record of
        # "what the model saw" at decision time — without them, a post-hoc
        # investigation of a bad trade would only have the PREDICTION stage's
        # p_yes/confidence fields, which are downstream of (lossy) feature
        # engineering.
        if decision_ledger is not None and dec_id:
            try:
                # MARKET_SNAPSHOT — the order-book state at decision time.
                self._emit_ledger(
                    decision_ledger.record_market_snapshot(
                        correlation_id=dec_id,
                        token_id=token_id,
                        strategy=self.name,
                        snapshot={
                            "mid": float(book.mid or 0.5),
                            "spread": float(book.spread or 0.0),
                            "best_bid": float(book.best_bid) if book.best_bid is not None else None,
                            "best_ask": float(book.best_ask) if book.best_ask is not None else None,
                            "bid_depth_top3": [
                                {"price": float(lv.price), "size": float(lv.size)}
                                for lv in (book.bids or [])[:3]
                            ],
                            "ask_depth_top3": [
                                {"price": float(lv.price), "size": float(lv.size)}
                                for lv in (book.asks or [])[:3]
                            ],
                        },
                    )
                )
                # INTELLIGENCE_SNAPSHOT — the market metadata available at
                # decision time (slug / volume / liquidity / outstanding
                # shares / active / closed / end_date). Captured here as a
                # best-effort "what we knew about the market" snapshot so
                # post-hoc attribution can correlate P&L with market
                # conditions.
                self._emit_ledger(
                    decision_ledger.record_intelligence_snapshot(
                        correlation_id=dec_id,
                        token_id=token_id,
                        strategy=self.name,
                        snapshot={
                            "slug": slug,
                            "market_slug": mkt.get("slug"),
                            "volume24hr": mkt.get("volume24hr"),
                            "volume_num": mkt.get("volumeNum"),
                            "liquidity": mkt.get("liquidity"),
                            "liquidity_num": mkt.get("liquidityNum"),
                            "outstanding_shares": mkt.get("outstandingShares"),
                            "active": mkt.get("active"),
                            "closed": mkt.get("closed"),
                            "end_date": mkt.get("endDate"),
                            "startDate": mkt.get("startDate"),
                        },
                    )
                )
                # FEATURE_SNAPSHOT — the ML feature vector at prediction time.
                # Convert numpy array → list (json.dumps can't serialise
                # ndarrays directly) and capture summary stats so a SHAP /
                # drift investigation can reconstruct "what the model saw".
                feat_list: list[float] | None = None
                n_features = 0
                try:
                    if hasattr(features, "tolist"):
                        feat_list = [float(x) for x in features.tolist()]
                        n_features = len(feat_list)
                    elif hasattr(features, "__iter__"):
                        feat_list = [float(x) for x in features]
                        n_features = len(feat_list)
                    elif features is not None:
                        n_features = 1
                except Exception:
                    feat_list = None
                self._emit_ledger(
                    decision_ledger.record_feature_snapshot(
                        correlation_id=dec_id,
                        token_id=token_id,
                        strategy=self.name,
                        snapshot={
                            "features": feat_list,
                            "n_features": n_features,
                            "feature_set_version": getattr(
                                ml_model, "feature_set_version", "unknown"
                            ),
                            "ab_model_version": model_version,
                        },
                    )
                )
            except Exception as e:
                log.debug(
                    "[signal_trader] W19-3 pre-prediction snapshot emit failed: %s", e
                )

        p_yes, confidence = active_model.predict(features, token_id=token_id)

        # W19-8 — record the prediction against its assigned arm. Defensive
        # try/except so a SQLite hiccup in the A/B store never blocks the
        # signal path; the prediction still propagates downstream.
        if model_version != "champion":
            try:
                from ml.ab_testing import ab_test as _ab_for_record
                _ab_for_record.record_prediction(
                    model_version=model_version,
                    prediction=float(p_yes),
                    token_id=token_id,
                )
            except Exception as e:
                log.debug("[signal_trader] A/B record_prediction skipped: %s", e)

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

        # V2 — Capital allocator sizing (replaces the inline Kelly below).
        # The allocator is the single source of truth for position size,
        # combining the Michaelis-Menten saturating edge curve with
        # smoothstep / saturating multipliers for confidence, calibration
        # (Brier), drawdown, existing exposure, performance, and book
        # liquidity. Returns 0.0 when any safety gate trips (no edge, no
        # liquidity, MDD breach, concentration breach, confidence below
        # floor) — in that case we record a rejection in the decision
        # ledger and bail so no order is submitted for an un-sized signal.
        #
        # OLD inline Kelly sizing — preserved as a comment per V2 spec
        # (do NOT remove; kept for diff-ability and as a fallback
        # reference if the allocator ever needs to be bypassed):
        #   size_usdc = max(0.5, min(float(MAX_POSITION_PER_MARKET), BANKROLL_BASELINE * kelly_f))
        size_usdc = allocate_capital(
            strategy=self.name,
            edge=kelly_numerator,
            confidence=confidence,
            liquidity=max(book.bids[0].size if book.bids else 0,
                          book.asks[0].size if book.asks else 0) * mid,
            existing_exposure=store.positions.get(
                token_id,
                type(store.positions.get(token_id, None)).__new__(
                    type(store.positions.get(token_id, None))
                ) if token_id in store.positions else None,
            ).total_invested if token_id in store.positions else 0.0,
            drawdown=max(0.0, store.peak_equity - (BANKROLL_BASELINE + store.daily_pnl)),
            strategy_performance={},
        )

        # V2 — Allocator-zero rejection path. When the allocator returns 0
        # the signal is not actionable under the current portfolio
        # conditions (no edge, no liquidity, MDD breach, existing-exposure
        # breach, sub-floor confidence, etc.). Record the rejection in the
        # decision ledger so the originating PREDICTION chain ends in a
        # documented "no trade" verdict rather than silently dropping, then
        # bail. The `<= 0.0` guard also catches any negative sentinel a
        # buggy allocator might emit; the canonical rejection sentinel is
        # exactly `0.0`.
        if size_usdc <= 0.0:
            self._emit_rejection(
                token_id, dec_id, kelly_numerator, confidence,
                "capital_allocator_zero", mid,
            )
            return None

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

    # ── W19-2 — Unified Strategy Contract (God Mode §26) ────────────────────
    # The 9 contract methods below are SYNC wrappers over the existing async
    # scan / signal / act machinery. They expose the strategy's signal-
    # generation, sizing, entry-decision, exit-decision, and diagnostics
    # surface to operators / dashboards / backtest engines without requiring
    # an event loop. The async ``_run`` / ``_scan_markets`` / ``_act_on_signal``
    # path remains the canonical live-trading loop; the contract methods are
    # the *introspection* surface.

    def metadata(self) -> dict:
        """Return strategy metadata for the catalog / dashboard."""
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "ML-Powered Directional Signal Trader with fractional Kelly "
                "sizing, online learning from market resolution, and "
                "capital-allocator-aware position sizing."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "sizing": "fractional_kelly_via_capital_allocator",
        }

    def configure(self, config: dict) -> None:
        """Apply runtime config overrides (min_confidence, base_order_size, ...).

        Recognised keys (all optional — unrecognised keys are stored in
        ``self.config`` for downstream introspection but not applied to
        typed strategy fields):

          * ``min_confidence`` (float ∈ [0, 1]) — overrides ``_min_confidence``.
          * ``base_order_size`` (float > 0)    — overrides ``_base_order_size``.
          * ``kelly_fraction`` (float ∈ (0, 1]) — overrides the module-level
            ``KELLY_FRACTION`` (used by ``_ml_signal``). NOTE: this is a
            module-level constant, so the override is recorded in
            ``self.config`` for visibility but NOT actually mutated on the
            module global — callers should not rely on it changing the live
            trade sizing; use ``base_order_size`` instead for sizing control.
        """
        super().configure(config)
        if "min_confidence" in config:
            mc = float(config["min_confidence"])
            # Clamp to [0, 1] so a misconfigured caller can't flip the gate
            # to a value > 1 (always-fire) or < 0 (always-reject).
            self._min_confidence = max(0.0, min(1.0, mc))
        if "base_order_size" in config:
            bos = float(config["base_order_size"])
            if bos > 0.0:
                self._base_order_size = bos

    def validate(self) -> tuple[bool, str]:
        """Validate strategy configuration post-construction.

        ``SignalTraderStrategy`` is valid when:
          * ``_min_confidence ∈ [0, 1]`` (a gate outside this range either
            always-fires or always-rejects — both are misconfigurations).
          * ``_base_order_size > 0`` (a non-positive base size would
            silently zero out every order).
          * The module-level ``KELLY_FRACTION ∈ (0, 1]`` (a zero or
            negative Kelly fraction would zero the size after capping).
        """
        if not (0.0 <= self._min_confidence <= 1.0):
            return False, (
                f"_min_confidence={self._min_confidence} outside [0, 1]"
            )
        if self._base_order_size <= 0.0:
            return False, (
                f"_base_order_size={self._base_order_size} must be > 0"
            )
        if not (0.0 < KELLY_FRACTION <= 1.0):
            return False, (
                f"KELLY_FRACTION={KELLY_FRACTION} outside (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Signal | None:
        """Build a contract ``Signal`` from a pre-computed market context.

        ``market_context`` is a caller-provided dict (NOT the live Gamma
        catalog). Recognised keys:

          * ``token_id`` (str, required)
          * ``slug`` (str, optional — defaults to ``token_id[:12]``)
          * ``direction`` (Side.BUY | Side.SELL, optional — defaults to
            BUY; real signals are produced by ``_ml_signal`` in the
            async scan path)
          * ``confidence`` (float ∈ [0, 1], optional — defaults to 0.0)
          * ``target_price`` (float ∈ (0, 1), optional — defaults to 0.5)
          * ``size_usdc`` (float > 0, optional — defaults to
            ``self._base_order_size``)
          * ``edge`` (float, optional — defaults to 0.0; the Kelly numerator
            from ``_ml_signal`` is the canonical edge estimate)
          * ``reason`` (str, optional — defaults to "manual")
          * ``decision_id`` (str, optional — propagated to ``metadata``
            so the contract Signal can be cross-referenced with the
            decision-ledger chain produced by the async scan path)

        Returns ``None`` when ``token_id`` is missing. The signal is
        marked ``action="HOLD"`` when confidence is below
        ``_min_confidence`` so callers can still observe (and dashboard)
        sub-threshold signals without acting on them.
        """
        token_id = market_context.get("token_id")
        if not token_id:
            return None

        slug = market_context.get("slug") or str(token_id)[:12]
        direction = market_context.get("direction", Side.BUY)
        confidence = float(market_context.get("confidence", 0.0))
        target_price = float(market_context.get("target_price", 0.5))
        size_usdc = float(
            market_context.get("size_usdc", self._base_order_size)
        )
        edge = float(market_context.get("edge", 0.0))
        reason = market_context.get("reason", "manual")
        decision_id = market_context.get("decision_id", "")

        # W19-2 — bump the signals counter so diagnostics() surfaces
        # how many contract-level signals have been produced.
        self._stats["signals"] = self._stats.get("signals", 0) + 1

        action = (
            direction.value if hasattr(direction, "value") else str(direction)
        ) if confidence >= self._min_confidence else "HOLD"

        return Signal(
            action=action,
            token_id=token_id,
            size=size_usdc,
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=reason,
            metadata={
                "slug": slug,
                "source": "ml",
                "decision_id": decision_id,
                "ml_score": market_context.get("ml_score", 0.0),
                "kelly_f": market_context.get("kelly_f", 0.0),
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Estimate expected P&L per dollar for the signal.

        For an ML directional trade, the edge is the Kelly numerator
        ``p * b - (1 - p)`` stored on the signal by ``generate_signal``.
        When the signal carries no pre-computed edge (e.g. a manual
        dashboard signal), we fall back to the confidence-weighted
        price deviation from 0.5 — a coarse but non-zero proxy.
        """
        if signal is None:
            return 0.0
        if signal.edge != 0.0:
            return signal.edge
        # Coarse fallback: confidence * |price - 0.5| — a noisy but
        # non-zero estimate for dashboard use only.
        price = signal.price if signal.price is not None else 0.5
        return signal.confidence * abs(price - 0.5)

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size the position using fractional Kelly + capital allocator.

        The canonical live-trading sizing path is in ``_ml_signal`` (which
        calls ``core.capital_allocator.allocate_capital`` with full portfolio
        context). This contract method is a *deterministic* sync fallback
        for dashboards / backtests: it applies quarter-Kelly sizing
        (``KELLY_FRACTION``) capped at ``risk_params.max_position_per_market``
        (default ``MAX_POSITION_PER_MARKET``) and floored at $0.50 to avoid
        dust trades.

        ``risk_params`` recognised keys (all optional):
          * ``max_position_per_market`` (float > 0)
          * ``kelly_fraction`` (float ∈ (0, 1])
          * ``bankroll_baseline`` (float > 0) — defaults to
            ``BANKROLL_BASELINE``.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_pos = float(
            risk_params.get("max_position_per_market", MAX_POSITION_PER_MARKET)
        )
        kelly_frac = float(risk_params.get("kelly_fraction", KELLY_FRACTION))
        bankroll = float(
            risk_params.get("bankroll_baseline", BANKROLL_BASELINE)
        )
        # Quarter-Kelly sizing: f* = edge * kelly_fraction, capped at 30%.
        edge = signal.edge if signal.edge > 0 else 0.02  # floor so a no-edge signal still sizes
        kelly_f = max(0.0, edge * kelly_frac)
        kelly_f = min(0.30, kelly_f)
        size = bankroll * kelly_f
        # Floor at $0.50 (avoid dust), cap at max_position_per_market.
        size = max(0.50, min(max_pos, size))
        # Never exceed available capital.
        return min(size, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution parameters mirroring ``_act_on_signal``.

        ``_act_on_signal`` constructs an ``OrderArgs`` with:
          * ``token_id`` = signal.token_id
          * ``price``    = signal.target_price
          * ``side``     = signal.direction
          * ``size``     = max(1.0, size_usdc / target_price)

        This contract method returns the same fields as a plain dict
        (plus ``time_in_force="GTC"`` and ``order_type="limit"``) so the
        dashboard / API / backtest engine can introspect the intended
        execution without re-implementing the math.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        target_price = signal.price if signal.price is not None else 0.5
        # Shares = max(1.0, size_usdc / price) — matches _act_on_signal.
        size_shares = max(1.0, signal.size / max(target_price, 0.01))
        return {
            "token_id": signal.token_id,
            "price": target_price,
            "side": signal.action,  # "BUY" / "SELL"
            "size": size_shares,
            "type": "limit",
            "time_in_force": "GTC",
            "decision_id": signal.metadata.get("decision_id", "") if signal.metadata else "",
        }

    def exit_logic(self, position: dict, market_context: dict) -> dict | None:
        """Determine whether to exit a position.

        ``SignalTraderStrategy``'s exit policy is encoded in
        ``_recycle_stale_orders`` (cancels unfilled signal orders after
        ``STALE_ORDER_SECONDS = 180``). This contract method surfaces the
        same rule as a plain dict so a dashboard / external trade manager
        can drive exits without poking the async loop.

        Returns ``{"action": "cancel", "order_id": ..., "reason": ...}``
        when the position's ``created_at`` is older than the stale window,
        ``None`` otherwise.
        """
        if not position:
            return None
        created_at = position.get("created_at")
        order_id = position.get("order_id")
        if created_at is None or order_id is None:
            return None
        now = market_context.get("now") or time.time()
        age_seconds = now - created_at
        if age_seconds > STALE_ORDER_SECONDS:
            return {
                "action": "cancel",
                "order_id": order_id,
                "reason": f"stale_order_age={age_seconds:.0f}s>{STALE_ORDER_SECONDS}s",
            }
        return None

    def diagnostics(self) -> dict:
        """Return strategy state + stats + model readiness.

        Surfaces the contract base fields (name / running / stats /
        last_error) plus the signal-trader-specific state: feature-cache
        size, market-cache size, active-signal count, ML model fitted
        flag, and the configured min_confidence / base_order_size.
        """
        base = super().diagnostics()
        base.update({
            "active_signals": len(self._active_signals),
            "feature_cache_size": len(self._feature_cache),
            "market_cache_size": len(self._market_cache),
            "min_confidence": self._min_confidence,
            "base_order_size": self._base_order_size,
            "model_is_fitted": getattr(ml_model, "is_fitted", False),
            "paper_mode": self._paper,
        })
        return base
