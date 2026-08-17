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
from typing import Dict, List, Optional, Tuple

from config import settings
from core.clob_client import OrderArgs
from core.data_store import Order, OrderStatus, Side, store
from core.gamma_client import gamma_client
from core.ws_client import ws_client
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

MID_TOLERANCE = 0.004   # 0.4% move triggers re-quote
LOOP_SLEEP = 4.0        # Quote review interval (seconds)
MAX_MARKETS_TO_QUOTE = 4


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

        # token_id -> {side: order_id}
        self._quotes: Dict[str, Dict[str, Optional[str]]] = {}
        # token_id -> last mid we quoted at
        self._last_mid: Dict[str, float] = {}

        # Markets we will quote
        self._token_ids: List[str] = list(settings.mm_token_ids_list)
        self._market_info: Dict[str, dict] = {}

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

        # Approximate price volatility from spread
        sigma_sq = max(0.001, (market_spread ** 2) / 2.0)

        # Reservation price (r) skews down when holding long inventory
        # r = s - q * gamma * sigma^2
        reservation_price = mid - (q * 0.01 * self._gamma_risk_aversion * sigma_sq)
        reservation_price = max(0.02, min(0.98, reservation_price))

        # Dynamic optimal half-spread
        half_spread = max(self._base_spread_frac / 2.0, market_spread / 2.0)

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
            ask_size = max(1.0, self._quote_size / ask_price)
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

    async def _cancel_quotes(self, token_id: str) -> None:
        quotes = self._quotes.get(token_id, {})
        for side, order_id in list(quotes.items()):
            if order_id:
                await self.cancel_order(order_id)
                quotes[side] = None
