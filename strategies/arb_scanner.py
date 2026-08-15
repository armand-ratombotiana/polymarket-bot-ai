"""
strategies/arb_scanner.py — High-Frequency Binary Arbitrage Scanner.

Scans Polymarket binary prediction markets for mispricing:
1. Long-side Dutch Book:
     Ask(YES) + Ask(NO) < 1.00 - MIN_PROFIT_BPS
   Action: Buy both YES and NO simultaneously. One of them is guaranteed to resolve
   to $1.00, securing a 100% risk-free arbitrage profit!
2. Short-side Overpriced:
     Bid(YES) + Bid(NO) > 1.00 + MIN_PROFIT_BPS
   Action: Sell/short both outcomes at a combined premium > $1.00.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import OrderBook, PriceLevel, Side, store
from core.gamma_client import gamma_client
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class ArbScannerStrategy(BaseStrategy):
    """
    Automated combinatorial arbitrage engine for binary prediction markets.
    """

    name = "arb_scanner"

    def __init__(self) -> None:
        super().__init__()
        self._min_profit_frac = max(0.003, settings.arb_min_profit_bps / 10_000)
        self._scan_interval = max(5, settings.arb_scan_interval_seconds)
        self._order_size = settings.arb_order_size_usdc

        # Track token-pair relationships: YES_token_id -> NO_token_id
        self._pairs: Dict[str, str] = {}
        self._market_slugs: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await self._build_market_pairs()
        if not self._pairs:
            log.info("[arb_scanner] Retrying pair discovery...")
            await asyncio.sleep(5)
            await self._build_market_pairs()

        all_tokens = list(self._pairs.keys()) + list(self._pairs.values())
        book_poller.add_tokens(all_tokens)

        await store.log_event(f"🔍 Arb Scanner active — scanning {len(self._pairs)} binary market pair(s)")
        log.info("[arb_scanner] Monitoring %d binary pairs for arbitrage", len(self._pairs))

        while self._running:
            try:
                await self._scan_for_arb()
            except Exception as e:
                log.error("[arb_scanner] Scan error: %s", e)
            await asyncio.sleep(self._scan_interval)

    async def _build_market_pairs(self) -> None:
        """Fetch markets from Gamma and build YES/NO token-pair mappings."""
        try:
            markets = await gamma_client.get_markets(active=True, limit=60, order="volume24hr")
            for mkt in markets:
                pair = gamma_client.extract_binary_pair(mkt)
                if pair:
                    yes_tid, no_tid = pair
                    self._pairs[yes_tid] = no_tid
                    slug = mkt.get("slug") or mkt.get("groupItemTitle") or yes_tid[:12]
                    self._market_slugs[yes_tid] = slug
                    self._market_slugs[no_tid] = slug
                    store.market_slugs[yes_tid] = slug
                    store.market_slugs[no_tid] = slug

            log.info("[arb_scanner] Discovered %d binary pairs", len(self._pairs))
        except Exception as e:
            log.error("[arb_scanner] Failed to build pairs: %s", e)

    # ── Arbitrage Scan ────────────────────────────────────────────────────────

    async def _scan_for_arb(self) -> None:
        opportunities: List[Tuple[str, str, float, float, float]] = []

        for yes_tid, no_tid in list(self._pairs.items()):
            opp = await self._check_pair(yes_tid, no_tid)
            if opp:
                opportunities.append((yes_tid, no_tid, *opp))

        if opportunities:
            opportunities.sort(key=lambda x: x[4], reverse=True)
            best = opportunities[0]
            yes_tid, no_tid, yes_price, no_price, profit = best
            await self._execute_arb(yes_tid, no_tid, yes_price, no_price, profit)

    async def _check_pair(
        self, yes_token: str, no_token: str
    ) -> Optional[Tuple[float, float, float]]:
        yes_book = await store.get_order_book(yes_token)
        no_book = await store.get_order_book(no_token)

        if yes_book is None or no_book is None:
            return None
        if yes_book.best_ask is None or no_book.best_ask is None:
            return None

        # Dutch Book Arbitrage: Buy YES + Buy NO for less than $1.00
        total_cost = yes_book.best_ask + no_book.best_ask
        profit_per_share = 1.00 - total_cost

        if profit_per_share >= self._min_profit_frac:
            slug = self._market_slugs.get(yes_token, yes_token[:12])
            log.info(
                "[arb_scanner] 🚀 ARB FOUND on %s: YES=%.4f + NO=%.4f = %.4f total (profit=%.4f/sh)",
                slug, yes_book.best_ask, no_book.best_ask, total_cost, profit_per_share,
            )
            return yes_book.best_ask, no_book.best_ask, profit_per_share

        return None

    async def _execute_arb(
        self,
        yes_token: str,
        no_token: str,
        yes_price: float,
        no_price: float,
        profit: float,
    ) -> None:
        slug = self._market_slugs.get(yes_token, yes_token[:12])
        size = max(5.0, self._order_size / max(yes_price, 0.05))

        await store.log_event(
            f"⚡ ARB Opportunity on {slug}: YES@{yes_price:.4f} + NO@{no_price:.4f} | +{profit*100:.2f}% profit"
        )

        yes_args = OrderArgs(
            token_id=yes_token, price=yes_price, side=Side.BUY,
            size=size, order_type="FOK",
        )
        no_args = OrderArgs(
            token_id=no_token, price=no_price, side=Side.BUY,
            size=size, order_type="FOK",
        )

        yes_order, no_order = await asyncio.gather(
            self.submit_order(yes_args),
            self.submit_order(no_args),
        )

        if yes_order and no_order:
            est_pnl = profit * size
            await store.log_event(
                f"✅ ARB executed on {slug}: Locked in ${est_pnl:.2f} expected profit"
            )
            log.info("[arb_scanner] ARB executed on %s — profit: $%.2f", slug, est_pnl)
