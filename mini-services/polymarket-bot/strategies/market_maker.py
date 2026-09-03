"""
strategies/market_maker.py — Avellaneda-Stoikov Market Maker with Inventory Skew.

Features:
  - Dynamic discovery of top liquid Polymarket markets via Gamma API
  - Avellaneda-Stoikov reservation price formula:
      r(s, q) = s - q * gamma * sigma^2
    where:
      s = mid price
      q = current inventory (shares)
      gamma = risk aversion parameter (0.1)
      sigma^2 = estimated price variance
  - Dynamic volatility-adjusted spread calculation
  - Submits continuous two-sided liquidity and auto-manages order lifecycle
"""
from __future__ import annotations

import asyncio
import logging
import time

from config import settings
from core.clob_client import OrderArgs
from core.data_store import Side, store
from core.gamma_client import gamma_client
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

MID_TOLERANCE = 0.004   # 0.4% move triggers re-quote
LOOP_SLEEP = 4.0        # Quote review interval (seconds)
MAX_MARKETS_TO_QUOTE = 8


class MarketMakerStrategy(BaseStrategy):
    """
    Advanced Avellaneda-Stoikov Market Maker.
    Maintains tight bid-ask spreads around reservation price with inventory skewing.
    """

    name = "market_maker"

    def __init__(self) -> None:
        super().__init__()
        self._base_spread_frac: float = max(0.01, settings.mm_spread_bps / 10_000)
        self._quote_size: float = settings.mm_quote_size_usdc
        self._max_inv: float = settings.mm_max_inventory_usdc
        self._gamma_risk_aversion: float = 0.08

        # token_id -> order_id per side
        self._quotes: dict[str, dict[str, str | None]] = {}
        # token_id -> last mid we quoted at
        self._last_mid: dict[str, float] = {}
        # token_id -> epoch seconds when non-zero YES inventory was first observed
        # (used by the inventory-flush path to dump stale inventory after 60s)
        self._inventory_since: dict[str, float] = {}

        # Markets we will quote
        self._token_ids: list[str] = list(settings.mm_token_ids_list)
        self._market_info: dict[str, dict] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await self._discover_markets()
        if not self._token_ids:
            log.warning("[market_maker] Waiting for liquid markets to quote...")
            await asyncio.sleep(5)
            await self._discover_markets()

        if not self._token_ids:
            log.warning("[market_maker] No markets available to quote")
            return

        for tid in self._token_ids:
            self._quotes[tid] = {"BUY": None, "SELL": None}

        await store.log_event(f"📊 Market Maker active — quoting {len(self._token_ids)} liquid market(s)")
        log.info("[market_maker] Running Avellaneda-Stoikov on %d markets", len(self._token_ids))

        loop_count = 0
        while self._running:
            try:
                # Periodically swap in markets whose books are actually quoteable
                # (initial picks may have one-sided/no books at startup).
                if loop_count % 10 == 0:
                    await self._refresh_markets()
                for token_id in list(self._token_ids):
                    await self._review_quotes(token_id)
                # U9 — Observability: count markets with at least one resting quote (additive).
                try:
                    from core.observability import record_metric
                    record_metric("strategy", "market_maker.quotes_active", sum(1 for q in self._quotes.values() if any(q.get(s) for s in ["BUY","SELL"])))
                except: pass
            except Exception as e:
                log.error("[market_maker] Error in quote loop: %s", e)
            loop_count += 1
            await asyncio.sleep(LOOP_SLEEP)

    async def _discover_markets(self) -> None:
        """Auto-select the most liquid markets from Gamma API if not manually set."""
        if self._token_ids:
            return
        try:
            markets = await gamma_client.get_markets(active=True, limit=20, order="volume24hr")
            for mkt in markets:
                if len(self._token_ids) >= MAX_MARKETS_TO_QUOTE:
                    break
                ids = gamma_client.extract_token_ids(mkt)
                if ids:
                    tid = ids[0]  # YES token
                    slug = mkt.get("slug") or mkt.get("groupItemTitle") or tid[:12]
                    self._token_ids.append(tid)
                    self._market_info[tid] = mkt
                    store.market_slugs[tid] = slug

            log.info("[market_maker] Auto-selected %d liquid markets for quoting", len(self._token_ids))
        except Exception as e:
            log.error("[market_maker] Discovery failed: %s", e)

    # ── Market refresh ────────────────────────────────────────────────────────

    @staticmethod
    def _is_quoteable(book) -> bool:
        """Book is usable for two-sided quoting (has a mid inside [0.02, 0.98])."""
        return book is not None and book.mid is not None and 0.02 <= book.mid <= 0.98

    async def _refresh_markets(self) -> None:
        """Replace unusable selections with markets whose books are quoteable."""
        if self._token_ids and settings.mm_token_ids_list:
            return  # manual override — never touch

        try:
            markets = await gamma_client.get_markets(active=True, limit=20, order="volume24hr")
        except Exception as e:
            log.warning("[market_maker] Market refresh failed: %s", e)
            return

        # Drop tokens whose books are missing / one-sided / extreme.
        self._token_ids = [t for t in self._token_ids if self._is_quoteable(store.order_books.get(t))]

        for mkt in markets:
            if len(self._token_ids) >= MAX_MARKETS_TO_QUOTE:
                break
            ids = gamma_client.extract_token_ids(mkt)
            if not ids:
                continue
            tid = ids[0]
            if tid in self._token_ids:
                continue
            if not self._is_quoteable(store.order_books.get(tid)):
                continue
            slug = mkt.get("slug") or mkt.get("groupItemTitle") or tid[:12]
            self._token_ids.append(tid)
            self._market_info[tid] = mkt
            store.market_slugs[tid] = slug
            self._quotes[tid] = {"BUY": None, "SELL": None}

        if self._token_ids:
            slugs = [store.market_slugs.get(t, t[:8]) for t in self._token_ids]
            log.info("[market_maker] Quoting %d market(s): %s", len(self._token_ids), slugs)
            await store.log_event(f"📊 Market Maker active — quoting {len(self._token_ids)} liquid market(s)")

    # ── Avellaneda-Stoikov Quoting ─────────────────────────────────────────────

    async def _review_quotes(self, token_id: str) -> None:
        book = await store.get_order_book(token_id)
        if book is None or book.mid is None:
            return

        # Inventory flush: if YES inventory has been held > 60s, dump it via a
        # marketable SELL at best_bid. When a flush fires, skip the normal
        # skewed-quote path for this cycle so the flush remains the only resting
        # SELL (we still re-stamp last_mid to avoid an immediate re-quote storm).
        if await self._flush_stale_inventory(token_id, book):
            self._last_mid[token_id] = book.mid
            return

        mid = book.mid
        last_mid = self._last_mid.get(token_id)

        # Check if mid price moved or if quotes are missing
        quotes = self._quotes.get(token_id, {})
        has_active_quotes = any(quotes.get(s) for s in ("BUY", "SELL"))

        # Re-quote if a stored quote was filled/cancelled and is no longer open
        quote_gone = any(
            oid and oid not in store.open_orders for oid in quotes.values()
        )

        needs_requote = (
            not has_active_quotes
            or quote_gone
            or last_mid is None
            or abs(mid - last_mid) / max(last_mid, 0.01) > MID_TOLERANCE
        )

        if needs_requote:
            await self._place_skewed_quotes(token_id, book)
            self._last_mid[token_id] = mid

    async def _place_skewed_quotes(self, token_id: str, book) -> None:
        """Calculate reservation price and place Avellaneda-Stoikov quotes."""
        await self._cancel_quotes(token_id)

        mid = book.mid or 0.5
        market_spread = book.spread or self._base_spread_frac

        # Current inventory
        pos = store.positions.get(token_id)
        q = (pos.yes_shares if pos else 0.0)
        invested = (pos.total_invested if pos else 0.0)

        # Approximate price volatility: blend spread-based estimate with ML rolling_volatility
        # feature (index 36 in the 38-feature vector) if available from the model.
        spread_sigma_sq = max(0.001, (market_spread ** 2) / 2.0)
        ml_adj, ml_skew = self._ml_spread_adjustment(token_id, book)
        # Try to use rolling_volatility feature from the model scaler for sigma
        sigma_sq = spread_sigma_sq  # fallback
        try:
            from ml.features import N_FEATURES, extract_features
            mkt = self._market_info.get(token_id, {})
            feats = extract_features(mkt, book)
            if feats is not None and len(feats) == N_FEATURES:
                # Feature index 36 = rolling_volatility (already [0,1] scaled)
                rolling_vol = float(feats[36])
                if rolling_vol > 0.01:
                    sigma_sq = max(spread_sigma_sq, rolling_vol ** 2)
        except Exception:
            pass

        # Reservation price (r) skews down with long inventory + toward ML fair value
        # r = s - q * gamma * sigma^2 + ml_skew
        # NOTE: the previous formula multiplied q by an extra 0.01 which made the
        # inventory skew negligible (0.01 * 0.08 = 0.0008). Removed so the A-S
        # skew term actually moves price as inventory grows.
        reservation_price = mid - (q * self._gamma_risk_aversion * sigma_sq) + ml_skew
        reservation_price = max(0.02, min(0.98, reservation_price))

        half_spread = max(self._base_spread_frac / 2.0, market_spread / 2.0) * ml_adj

        bid_price = round(max(0.01, reservation_price - half_spread), 4)
        ask_price = round(min(0.99, reservation_price + half_spread), 4)

        # Prevent crossing the top of the book
        if book.best_ask is not None and bid_price >= book.best_ask:
            bid_price = round(book.best_ask - 0.01, 4)
        if book.best_bid is not None and ask_price <= book.best_bid:
            ask_price = round(book.best_bid + 0.01, 4)

        # Each side is placed independently — a price-extreme market can still
        # get a one-sided quote (e.g. bid-only near 0.99 certainty markets).
        slug = store.market_slugs.get(token_id, token_id[:12])
        can_quote_bid = 0.01 < bid_price < 0.99
        can_quote_ask = 0.01 < ask_price < 0.99

        # Sizing with inventory cap
        can_buy = (invested + self._quote_size) <= self._max_inv
        can_sell = (q > 0.0) and (invested > 0.0)  # never sell shares we don't hold

        if can_buy and can_quote_bid:
            bid_size = max(1.0, self._quote_size / bid_price)
            bid_args = OrderArgs(
                token_id=token_id,
                price=bid_price,
                side=Side.BUY,
                size=bid_size,
            )
            order = await self.submit_order(bid_args)
            if order:
                self._quotes.setdefault(token_id, {})["BUY"] = order.order_id
                log.info("[MM] %s: quote BUY %.2f @ %.4f", slug, bid_size, bid_price)

        if can_sell and can_quote_ask:
            # Bound ask_size by actual inventory so we never list a SELL for
            # shares we do not hold (the previous max(1.0, ...) floor could
            # over-size the ask when quote_size/ask_price exceeded holdings).
            ask_size = min(max(1.0, self._quote_size / ask_price), q)
            ask_args = OrderArgs(
                token_id=token_id,
                price=ask_price,
                side=Side.SELL,
                size=ask_size,
            )
            order = await self.submit_order(ask_args)
            if order:
                self._quotes.setdefault(token_id, {})["SELL"] = order.order_id
                log.info("[MM] %s: quote SELL %.2f @ %.4f", slug, ask_size, ask_price)

        log.debug("[MM] %s: Bid=%.4f Ask=%.4f (mid=%.4f, inv=%.1f)", slug, bid_price, ask_price, mid, q)

    def _ml_spread_adjustment(self, token_id: str, book) -> tuple[float, float]:
        """
        Return (spread_multiplier, reservation_skew) driven by ML confidence and direction:

        spread_multiplier:
          - High confidence (> 0.7): tighten 15%  → 0.85  (better fill probability)
          - Low confidence  (< 0.3): widen  25%   → 1.25  (protect adverse selection)
          - Otherwise:               neutral       → 1.0

        reservation_skew:
          Shift the A-S reservation price toward the ML fair-value estimate.
          If ML predicts p_yes = 0.65 and market mid = 0.50, skew = +0.015 — the maker
          quotes a higher reservation and passively accumulates YES inventory at fair value.
          Capped at ±2.0% to avoid over-steering.
        """
        try:
            from ml.features import extract_features
            from ml.model import ml_model
            market = self._market_info.get(token_id, {})
            feats = extract_features(market, book)
            if feats is None:
                return 1.0, 0.0
            p_yes, confidence = ml_model.predict(feats, token_id=token_id)

            # Spread multiplier
            if confidence > 0.7:
                spread_mult = 0.85
            elif confidence < 0.3:
                spread_mult = 1.25
            else:
                spread_mult = 1.0

            # Directional reservation skew: (ML fair value - market mid) * damping
            mid = book.mid or 0.5
            price_edge = p_yes - mid
            # Damping 0.15: max ±1.5% skew even at 10% edge — conservative to avoid
            # over-steering the reservation away from the live mid in thin markets.
            reservation_skew = float(price_edge * 0.15)
            reservation_skew = max(-0.02, min(0.02, reservation_skew))

            return spread_mult, reservation_skew
        except Exception:
            return 1.0, 0.0

    async def _cancel_quotes(self, token_id: str) -> None:
        quotes = self._quotes.get(token_id, {})
        for side, order_id in list(quotes.items()):
            if order_id:
                await self.cancel_order(order_id)
                quotes[side] = None

    async def _flush_stale_inventory(self, token_id: str, book) -> bool:
        """
        Dump stale YES inventory with a marketable SELL at best_bid.

        Tracks the first epoch-second at which non-zero YES inventory was
        observed for this token_id. If that inventory persists longer than
        60s, cancel all resting quotes for the token and place a single SELL
        at the top-of-book bid (marketable) to flush the position.

        Returns True when a flush order was placed so the caller can skip the
        normal skewed-quote path for this cycle. Returns False otherwise (no
        inventory, inventory still within the 60s grace window, no bid to hit,
        or order submission failed).
        """
        pos = store.positions.get(token_id)
        q = (pos.yes_shares if pos else 0.0)
        now = time.time()

        if q <= 0.0:
            # No inventory — clear any stale timestamp.
            self._inventory_since.pop(token_id, None)
            return False

        since = self._inventory_since.get(token_id)
        if since is None:
            # First cycle we've seen this inventory — start the clock.
            self._inventory_since[token_id] = now
            return False

        held_seconds = now - since
        if held_seconds <= 60.0:
            return False

        # Inventory held > 60s — attempt flush at best_bid (marketable SELL).
        best_bid = book.best_bid
        if best_bid is None or best_bid <= 0.01:
            return False

        slug = store.market_slugs.get(token_id, token_id[:12])
        flush_size = min(max(1.0, self._quote_size / best_bid), q)

        # Cancel existing quotes so the flush is the only resting SELL.
        await self._cancel_quotes(token_id)

        flush_args = OrderArgs(
            token_id=token_id,
            price=best_bid,
            side=Side.SELL,
            size=flush_size,
        )
        order = await self.submit_order(flush_args)
        if order:
            self._quotes.setdefault(token_id, {})["SELL"] = order.order_id
            # Reset the clock so we don't immediately re-flush next cycle;
            # if the order doesn't fill, we'll retry in another 60s.
            self._inventory_since[token_id] = now
            log.info(
                "[MM] %s: FLUSH SELL %.2f @ %.4f (held %.1fs > 60s)",
                slug, flush_size, best_bid, held_seconds,
            )
            await store.log_event(
                f"\U0001F9F9 MM {slug}: FLUSH SELL {flush_size:.2f} @ {best_bid:.4f} "
                f"(inventory held {held_seconds:.0f}s > 60s)"
            )
            return True
        return False
