"""
core/book_poller.py — Tiered Adaptive Order Book Poller.

Polls Polymarket CLOB REST API:
- Tier 1 (High priority / actively quoted): polled every 2 seconds
- Tier 2 (Background / monitored): polled every 6 seconds
- Concurrent request rate limiting via asyncio.Semaphore
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Set

import httpx

from config import settings
from core.data_store import OrderBook, PriceLevel, store

log = logging.getLogger(__name__)

TIER1_INTERVAL = 2.0      # seconds for high priority markets
TIER2_INTERVAL = 6.0      # seconds for background markets
MAX_CONCURRENT = 12       # concurrent in-flight requests
TIMEOUT = 6.0


class BookPoller:
    """
    Fetches order books from CLOB REST API with tiered priority.
    """

    def __init__(self) -> None:
        self._running = False
        self._task1: Optional[asyncio.Task] = None
        self._task2: Optional[asyncio.Task] = None
        self._tier1_tokens: Set[str] = set()
        self._tier2_tokens: Set[str] = set()
        self._client: Optional[httpx.AsyncClient] = None
        self._base = settings.poly_clob_host.rstrip("/")
        self._success_count = 0
        self._error_count = 0

    def set_tokens(self, token_ids: List[str]) -> None:
        """Assign first 15 to Tier 1, rest to Tier 2."""
        tokens = list(dict.fromkeys(token_ids))
        self._tier1_tokens = set(tokens[:15])
        self._tier2_tokens = set(tokens[15:])
        log.info("[book_poller] Configured %d Tier-1 and %d Tier-2 tokens",
                 len(self._tier1_tokens), len(self._tier2_tokens))

    def add_tokens(self, token_ids: List[str]) -> None:
        for tid in token_ids:
            if tid not in self._tier1_tokens and tid not in self._tier2_tokens:
                if len(self._tier1_tokens) < 20:
                    self._tier1_tokens.add(tid)
                else:
                    self._tier2_tokens.add(tid)

    def prioritize_tokens(self, token_ids: List[str]) -> None:
        """Promote specific tokens (e.g. quoted markets) to Tier 1."""
        for tid in token_ids:
            self._tier2_tokens.discard(tid)
            self._tier1_tokens.add(tid)

    async def start(self) -> None:
        self._running = True
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=TIMEOUT,
            headers={"User-Agent": "polymarket-bot/2.2"},
            follow_redirects=True,
        )
        self._task1 = asyncio.create_task(self._poll_tier(1, TIER1_INTERVAL), name="poller-tier1")
        self._task2 = asyncio.create_task(self._poll_tier(2, TIER2_INTERVAL), name="poller-tier2")
        log.info("[book_poller] Adaptive Tiered Poller active (T1=%.1fs, T2=%.1fs)",
                 TIER1_INTERVAL, TIER2_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        for t in (self._task1, self._task2):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _poll_tier(self, tier: int, interval: float) -> None:
        await asyncio.sleep(1.0 if tier == 1 else 3.0)
        while self._running:
            try:
                tokens = list(self._tier1_tokens if tier == 1 else self._tier2_tokens)
                if tokens:
                    sem = asyncio.Semaphore(MAX_CONCURRENT)

                    async def fetch_one(tid: str) -> None:
                        async with sem:
                            await self._fetch_book(tid)

                    tasks = [asyncio.create_task(fetch_one(t)) for t in tokens]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    errors = sum(1 for r in results if isinstance(r, Exception))
                    if errors:
                        self._error_count += errors
            except Exception as e:
                log.debug("[book_poller] Tier-%d poll cycle error: %s", tier, e)
            await asyncio.sleep(interval)

    async def _fetch_book(self, token_id: str) -> None:
        if not self._client or self._client.is_closed:
            return
        try:
            resp = await self._client.get("/book", params={"token_id": token_id})
            if resp.status_code == 200:
                await self._apply_book(token_id, resp.json())
                self._success_count += 1
            else:
                log.debug("[book_poller] %s HTTP %d", token_id[:12], resp.status_code)
        except Exception:
            raise

    async def _apply_book(self, token_id: str, data: dict) -> None:
        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])

        bids = sorted(
            [PriceLevel(price=float(b["price"]), size=float(b["size"])) for b in raw_bids if float(b.get("size", 0)) > 0],
            key=lambda x: -x.price,
        )
        asks = sorted(
            [PriceLevel(price=float(a["price"]), size=float(a["size"])) for a in raw_asks if float(a.get("size", 0)) > 0],
            key=lambda x: x.price,
        )

        book = OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            updated_at=time.time(),
        )
        await store.update_order_book(book)

    @property
    def stats(self) -> dict:
        return {
            "tier1_tokens": len(self._tier1_tokens),
            "tier2_tokens": len(self._tier2_tokens),
            "total_tracked": len(self._tier1_tokens) + len(self._tier2_tokens),
            "success_count": self._success_count,
            "error_count": self._error_count,
        }


# Module-level singleton
book_poller = BookPoller()
