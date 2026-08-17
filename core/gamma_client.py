"""
core/gamma_client.py — Async client for the Polymarket Gamma API.
Handles market discovery, metadata, and token ID / outcome parsing.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)


class GammaClient:
    """Async wrapper around gamma-api.polymarket.com."""

    def __init__(self) -> None:
        self._base = settings.poly_gamma_host.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=15.0,
                headers={"User-Agent": "polymarket-bot/2.0"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        client = await self._ensure_client()
        try:
            resp = await client.get(path, params=params or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.error("Gamma API HTTP error %s: %s", e.response.status_code, path)
            raise
        except Exception as e:
            log.error("Gamma API error: %s", e)
            raise

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict]:
        """Fetch markets sorted by volume. Returns list of market dicts."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if active:
            params["active"] = "true"
        if closed is not None:
            params["closed"] = str(closed).lower()

        data = await self._get("/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data) if isinstance(data, dict) else []

    async def get_market(self, condition_id: str) -> dict:
        """Fetch a single market by conditionId."""
        return await self._get(f"/markets/{condition_id}")

    async def get_market_by_slug(self, slug: str) -> dict | None:
        """Search for a market by slug."""
        markets = await self.get_markets(limit=200)
        for m in markets:
            if m.get("slug", "").lower() == slug.lower():
                return m
        return None

    async def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across market questions."""
        params = {"search": query, "limit": limit, "active": "true"}
        data = await self._get("/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []

    async def get_events(self, limit: int = 50) -> list[dict]:
        """Fetch active events (groups of related markets)."""
        params = {"limit": limit, "active": "true", "closed": "false"}
        data = await self._get("/events", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []

    async def get_resolved_markets(self, limit: int = 30) -> list[dict]:
        """Fetch recently resolved/closed markets."""
        return await self.get_markets(active=False, closed=True, limit=limit, order="updatedAt", ascending=False)

    # ── Universal Token & Outcome Parsers ─────────────────────────────────────

    @staticmethod
    def extract_token_ids(market: dict) -> list[str]:
        """
        Pull all token IDs from a market dict, handling tokens array,
        clobTokenIds JSON string, or clobTokenIds list.
        """
        # 1. Check tokens list
        tokens = market.get("tokens", [])
        if tokens and isinstance(tokens, list):
            ids = [t["token_id"] for t in tokens if isinstance(t, dict) and "token_id" in t]
            if ids:
                return ids

        # 2. Check clobTokenIds field
        raw_ids = market.get("clobTokenIds")
        if raw_ids:
            if isinstance(raw_ids, str):
                try:
                    parsed = json.loads(raw_ids)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed if x]
                except Exception:
                    pass
            elif isinstance(raw_ids, list):
                return [str(x) for x in raw_ids if x]

        return []

    @staticmethod
    def extract_binary_pair(market: dict) -> tuple[str, str] | None:
        """
        Extract (YES_token_id, NO_token_id) from a binary market.
        Returns None if market is not binary or tokens cannot be determined.
        """
        token_ids = GammaClient.extract_token_ids(market)
        if len(token_ids) < 2:
            return None

        # Check explicit tokens array with outcome names
        tokens = market.get("tokens", [])
        if isinstance(tokens, list) and len(tokens) >= 2:
            yes_t = next((t["token_id"] for t in tokens if isinstance(t, dict) and str(t.get("outcome", "")).upper() in ("YES", "1")), None)
            no_t = next((t["token_id"] for t in tokens if isinstance(t, dict) and str(t.get("outcome", "")).upper() in ("NO", "0")), None)
            if yes_t and no_t:
                return str(yes_t), str(no_t)

        # Standard Polymarket convention: index 0 is YES (or first outcome), index 1 is NO (or second outcome)
        return str(token_ids[0]), str(token_ids[1])


# Module-level singleton
gamma_client = GammaClient()
