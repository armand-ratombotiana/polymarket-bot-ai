"""
core/market_discovery.py — Universal Polymarket Market Discovery & Ingestion Engine.

Paginates through the Polymarket Gamma API to discover and continuously track
all active prediction markets across all categories (Politics, Crypto, Macro, Sports, AI, Pop Culture).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from config import settings
from core.book_poller import book_poller
from core.data_store import store
from ml.vector_store import vector_store

log = logging.getLogger(__name__)

GAMMA_BASE_URL = settings.poly_gamma_host.rstrip("/")


class UniversalMarketDiscoveryEngine:
    """
    Continuous market scanner discovering all available Polymarket contracts.
    """

    def __init__(self) -> None:
        self.discovered_markets: Dict[str, dict] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_scan = 0.0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop(), name="market-discovery")
        log.info("[market_discovery] Universal Market Discovery Engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _discovery_loop(self) -> None:
        """Periodic full scan across all Polymarket Gamma market endpoints."""
        while self._running:
            try:
                await self.scan_all_markets()
            except Exception as e:
                log.warning("[market_discovery] Discovery scan error: %s", e)
            await asyncio.sleep(120)  # Refresh full discovery every 2 minutes

    async def scan_all_markets(self) -> int:
        """Query Polymarket Gamma API with pagination to discover all active contracts."""
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            all_markets: List[dict] = []
            limit = 100
            for offset in [0, 100, 200, 300, 400]:
                try:
                    resp = await client.get(
                        f"{GAMMA_BASE_URL}/markets",
                        params={"limit": limit, "offset": offset, "active": "true", "closed": "false"},
                    )
                    if resp.status_code == 200:
                        batch = resp.json()
                        if isinstance(batch, list) and batch:
                            all_markets.extend(batch)
                            if len(batch) < limit:
                                break
                        else:
                            break
                    else:
                        break
                except Exception as e:
                    log.debug("[market_discovery] Gamma batch fetch error: %s", e)
                    break

        if not all_markets:
            return 0

        # Feed DataStore, Vector Store, and Tiered Book Poller
        await store.seed_markets(all_markets)

        token_ids: List[str] = []
        for m in all_markets:
            tid = m.get("clobTokenId") or m.get("token_id")
            if tid:
                token_ids.append(tid)
                self.discovered_markets[tid] = m
                vector_store.add_market(tid, m)

        book_poller.add_tokens(token_ids)
        self._last_scan = time.time()
        log.info("[market_discovery] Discovered & integrated %d active prediction markets into system", len(token_ids))
        return len(token_ids)


# Global singleton
market_discovery = UniversalMarketDiscoveryEngine()
