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
        self._task1: asyncio.Task | None = None
        self._task2: asyncio.Task | None = None
        self._tier1_tokens: set[str] = set()
        self._tier2_tokens: set[str] = set()
        self._client: httpx.AsyncClient | None = None
        self._base = settings.poly_clob_host.rstrip("/")
        self._success_count = 0
        self._error_count = 0
        # Persistent semaphore — created once, reused across poll cycles
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        # Circuit-breaker rolling window (last 30 request results)
        self._result_window: list[bool] = []  # True=success, False=error
        self._circuit_open = False
        self._circuit_open_until: float = 0.0

    def set_tokens(self, token_ids: list[str]) -> None:
        """Assign first 50 to Tier 1, rest to Tier 2."""
        tokens = list(dict.fromkeys(token_ids))
        self._tier1_tokens = set(tokens[:50])
        self._tier2_tokens = set(tokens[50:])
        log.info("[book_poller] Configured %d Tier-1 and %d Tier-2 tokens",
                 len(self._tier1_tokens), len(self._tier2_tokens))

    def add_tokens(self, token_ids: list[str]) -> None:
        for tid in token_ids:
            if tid not in self._tier1_tokens and tid not in self._tier2_tokens:
                if len(self._tier1_tokens) < 50:
                    self._tier1_tokens.add(tid)
                else:
                    self._tier2_tokens.add(tid)

    def prioritize_tokens(self, token_ids: list[str]) -> None:
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
                # Circuit breaker: pause polling if sustained error rate > 80%
                import time
                if self._circuit_open:
                    if time.time() < self._circuit_open_until:
                        await asyncio.sleep(interval)
                        continue
                    else:
                        self._circuit_open = False
                        self._result_window.clear()
                        log.info("[book_poller] Circuit breaker CLOSED — resuming polling")

                tokens = list(self._tier1_tokens if tier == 1 else self._tier2_tokens)
                if tokens:
                    # Reuse persistent semaphore (not re-created every cycle)
                    async def fetch_one(tid: str) -> None:
                        async with self._sem:
                            await self._fetch_book(tid)

                    tasks = [asyncio.create_task(fetch_one(t)) for t in tokens]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        success = not isinstance(r, Exception)
                        self._result_window.append(success)
                        if success:
                            self._success_count += 1
                        else:
                            self._error_count += 1
                    # Keep only last 30 results for circuit breaker
                    if len(self._result_window) > 30:
                        self._result_window = self._result_window[-30:]
                    # Trip circuit breaker if error rate > 80%
                    if len(self._result_window) >= 10:
                        err_rate = self._result_window.count(False) / len(self._result_window)
                        if err_rate > 0.80 and not self._circuit_open:
                            self._circuit_open = True
                            self._circuit_open_until = time.time() + 30.0
                            log.warning("[book_poller] Circuit breaker OPEN — error rate %.0f%%, pausing 30s",
                                        err_rate * 100)
            except Exception as e:
                log.debug("[book_poller] Tier-%d poll cycle error: %s", tier, e)
            await asyncio.sleep(interval)

    async def _fetch_book(self, token_id: str) -> None:
        if not self._client or self._client.is_closed:
            return
        try:
            resp = await self._client.get("/book", params={"token_id": token_id})
            if resp.status_code == 200:
                data = resp.json()
                await self._apply_book(token_id, data)
                self._success_count += 1
                from core.ingestion.raw_vault import raw_vault
                from core.ingestion.source_registry import source_registry
                asyncio.create_task(raw_vault.record_observation("clob_rest", data))
                asyncio.create_task(source_registry.record_metric("clob_rest", True))
            else:
                log.debug("[book_poller] %s HTTP %d", token_id[:12], resp.status_code)
                from core.ingestion.source_registry import source_registry
                asyncio.create_task(source_registry.record_metric("clob_rest", False, f"HTTP {resp.status_code}"))
        except Exception as e:
            from core.ingestion.source_registry import source_registry
            asyncio.create_task(source_registry.record_metric("clob_rest", False, str(e)))
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

        # W24-4 — Ingestion-time validation gate. Runs the snapshot
        # through ``DataValidator.validate_snapshot`` (dedup by hash,
        # schema check, value-range check, staleness detection,
        # timestamp normalisation) BEFORE persisting to TimescaleDB.
        # A rejected snapshot is logged and the downstream
        # ``record_snapshot`` / ``record_tick`` calls are skipped so a
        # bad row never reaches the hypertable. Duplicates are
        # silently dropped at ``debug`` level (the CLOB legitimately
        # returns the same book on consecutive polls within a single
        # Tier-1 interval, so dedup is the expected steady state, not
        # an error).
        #
        # The in-memory ``store.order_books`` is updated ABOVE this
        # gate intentionally — the live trading path needs the freshest
        # top-of-book even if the snapshot is a duplicate (a duplicate
        # is the SAME data we already saw, so updating the store is a
        # no-op semantically; rejecting the DB write just avoids the
        # hypertable row inflation that drove the W24-4 task).
        from core.data_validator import data_validator

        raw_snapshot = {
            "token_id": token_id,
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "timestamp": data.get("timestamp") or book.updated_at,
            "source": "clob_rest",
        }
        if book.mid is not None:
            raw_snapshot["mid"] = book.mid
        if book.spread is not None:
            raw_snapshot["spread"] = book.spread

        result = data_validator.validate_snapshot(raw_snapshot)
        if not result.is_valid:
            if result.is_duplicate:
                log.debug(
                    "[book_poller] Skipping duplicate snapshot for %s",
                    token_id[:12],
                )
            else:
                log.warning(
                    "[book_poller] Invalid snapshot for %s: %s",
                    token_id[:12],
                    result.errors,
                )
            return

        # Ingest into TimescaleDB / PostgreSQL asynchronously (W21-5 —
        # pass the full bid / ask ladders through to ``record_snapshot``
        # so the JSON columns (``bids_json`` / ``asks_json``) and the
        # depth-10 summaries (``bid_depth_10`` / ``ask_depth_10``) are
        # persisted on both the PostgreSQL hypertable and the SQLite
        # fallback. The ladders are converted from ``PriceLevel``
        # dataclass instances to plain ``{"price": float, "size":
        # float}`` dicts — the shape
        # ``database_manager.get_order_book_depth`` parses back out of
        # the JSON column on read.)
        slug = store.market_slugs.get(token_id, token_id[:16])
        from core.timescale_db import timescale_db
        bids_payload = [{"price": b.price, "size": b.size} for b in bids]
        asks_payload = [{"price": a.price, "size": a.size} for a in asks]
        asyncio.create_task(
            timescale_db.record_snapshot(
                token_id=token_id,
                slug=slug,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                mid=book.mid,
                spread=book.spread,
                bids_json=bids_payload,
                asks_json=asks_payload,
            )
        )
        if bids and asks:
            best_b_size = bids[0].size
            best_a_size = asks[0].size
            ofi = (best_b_size - best_a_size) / max(best_b_size + best_a_size, 1.0)
            micro_p = (book.best_bid * best_a_size + book.best_ask * best_b_size) / max(best_b_size + best_a_size, 1.0) if (book.best_bid and book.best_ask) else (book.mid or 0.5)
            asyncio.create_task(
                timescale_db.record_tick(
                    token_id=token_id,
                    best_bid_size=best_b_size,
                    best_ask_size=best_a_size,
                    ofi=ofi,
                    micro_price=micro_p,
                )
            )

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
