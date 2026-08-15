"""
core/book_poller.py — REST-based order book poller.

Polls the Polymarket CLOB REST API for order book snapshots.
Used as primary data source when WebSocket is unavailable or restricted.
Endpoint: GET https://clob.polymarket.com/book?token_id=<token_id>
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import httpx

from config import settings
from core.data_store import OrderBook, PriceLevel, store

log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0       # seconds between full poll cycles
MAX_CONCURRENT = 10       # concurrent book fetches per cycle
TIMEOUT = 8.0


class BookPoller:
    """
    Fetches order books from the CLOB REST API for all tracked tokens.
    Runs as a background task and updates the shared data store.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._token_ids: List[str] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._base = settings.poly_clob_host.rstrip("/")
        self._success_count = 0
        self._error_count = 0

    def set_tokens(self, token_ids: List[str]) -> None:
        """Replace the full list of tokens to poll."""
        self._token_ids = list(dict.fromkeys(token_ids))  # dedupe, preserve order
        log.info("[book_poller] Tracking %d tokens", len(self._token_ids))

    def add_tokens(self, token_ids: List[str]) -> None:
        existing = set(self._token_ids)
        new = [t for t in token_ids if t not in existing]
        self._token_ids.extend(new)

    async def start(self) -> None:
        self._running = True
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=TIMEOUT,
            headers={"User-Agent": "polymarket-bot/2.0"},
            follow_redirects=True,
        )
        self._task = asyncio.create_task(self._poll_loop(), name="book-poller")
        log.info("[book_poller] Started — polling CLOB REST every %.0fs", POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        # Small initial delay so startup can finish seeding tokens
        await asyncio.sleep(2.0)
        while self._running:
            try:
                await self._poll_all()
            except Exception as e:
                log.debug("[book_poller] Poll cycle error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_all(self) -> None:
        tokens = self._token_ids[:]
        if not tokens:
            return

        # Semaphore limits concurrent in-flight requests
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def fetch_one(token_id: str) -> None:
            async with sem:
                await self._fetch_book(token_id)

        tasks = [asyncio.create_task(fetch_one(tid)) for tid in tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = sum(1 for r in results if isinstance(r, Exception))
        if errors:
            self._error_count += errors
            log.debug("[book_poller] %d/%d books errored this cycle", errors, len(tokens))

    async def _fetch_book(self, token_id: str) -> None:
        if not self._client or self._client.is_closed:
            return
        try:
            resp = await self._client.get("/book", params={"token_id": token_id})
            if resp.status_code == 200:
                await self._apply_book(token_id, resp.json())
                self._success_count += 1
            else:
                log.debug("[book_poller] %s → HTTP %d", token_id[:12], resp.status_code)
        except Exception as e:
            log.debug("[book_poller] fetch error for %s: %s", token_id[:12], e)
            raise

    async def _apply_book(self, token_id: str, data: dict) -> None:
        """Parse CLOB REST /book response into an OrderBook."""
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
            "tracked_tokens": len(self._token_ids),
            "success_count": self._success_count,
            "error_count": self._error_count,
        }


# Module-level singleton
book_poller = BookPoller()
