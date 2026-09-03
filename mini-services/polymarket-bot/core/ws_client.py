"""
core/ws_client.py — WebSocket client for real-time Polymarket CLOB data.
Handles automatic reconnection with exponential backoff.
Publishes events to a shared asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import websockets

from config import settings
from core.circuit_breaker import websocket_breaker
from core.data_store import OrderBook, PriceLevel, store

log = logging.getLogger(__name__)

# ── Event type constants ──────────────────────────────────────────────────────
EVT_BOOK_SNAPSHOT = "book"
EVT_PRICE_CHANGE = "price_change"
EVT_TRADE = "last_trade_price"
EVT_USER_FILL = "user_fill"
EVT_TICK_SIZE = "tick_size_change"

RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
PING_INTERVAL = 20.0


class WebSocketClient:
    """
    Subscribes to Polymarket WebSocket streams.
    Calls registered async handlers whenever an event arrives.
    """

    def __init__(self) -> None:
        self._uri = settings.poly_ws_host
        self._subscribed_tokens: set[str] = set()
        self._handlers: list[Callable] = []
        self._running = False
        self._ws = None
        self._task: asyncio.Task | None = None

    def register_handler(self, handler: Callable) -> None:
        """Register an async callable(event_type, data) handler."""
        self._handlers.append(handler)

    def subscribe(self, token_ids: list[str]) -> None:
        """Add token IDs to the subscription set (live or pending reconnect)."""
        self._subscribed_tokens.update(token_ids)

    async def start(self) -> None:
        """Start the WebSocket listener as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="ws-client")
        log.info("WebSocket client started, watching %d token(s)", len(self._subscribed_tokens))

    async def stop(self) -> None:
        """Gracefully stop the WebSocket listener."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("WebSocket client stopped")

    # ── Internal ──────────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while self._running:
            # W13-2 — circuit breaker: when the WebSocket endpoint has been
            # failing sustainedly, the breaker opens and we skip the connect
            # attempt (failing fast instead of burning through reconnects).
            # ``can_execute`` returns True while CLOSED — the steady state —
            # so this branch is a transparent no-op until a sustained run of
            # failures trips the breaker.
            if not websocket_breaker.can_execute():
                log.debug(
                    "WebSocket circuit OPEN — backing off %.0fs", delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                continue
            try:
                await self._connect_and_listen()
                websocket_breaker.record_success()
                delay = RECONNECT_BASE_DELAY  # reset on clean disconnect
            except asyncio.CancelledError:
                break
            except Exception as e:
                websocket_breaker.record_failure(e)
                log.debug("WebSocket error: %s — reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_and_listen(self) -> None:
        log.info("Connecting to %s", self._uri)
        async with websockets.connect(
            self._uri,
            ping_interval=PING_INTERVAL,
            ping_timeout=30,
            max_size=2**23,
        ) as ws:
            self._ws = ws
            await store.log_event("WebSocket connected")

            # Subscribe to market channel for all tracked tokens
            if self._subscribed_tokens:
                await self._send_subscription(ws, list(self._subscribed_tokens))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(raw)
                except Exception as e:
                    log.debug("Message parse error: %s", e)

    async def _send_subscription(self, ws, token_ids: list[str]) -> None:
        # Polymarket requires this exact format — send in batches of 100
        for i in range(0, len(token_ids), 100):
            batch = token_ids[i:i + 100]
            sub_msg = {
                "assets_ids": batch,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub_msg))
            log.debug("Subscribed to batch of %d market token(s)", len(batch))

    async def _handle_message(self, raw: str) -> None:
        """Parse a raw WebSocket message and route to handlers."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = msg.get("event_type") or msg.get("type") or ""
        data = msg.get("data") or msg

        # Update in-memory order book on book snapshot events
        if event_type == EVT_BOOK_SNAPSHOT:
            await self._apply_book_snapshot(data)
        elif event_type == EVT_PRICE_CHANGE:
            await self._apply_price_change(data)

        # Notify all registered handlers
        for handler in self._handlers:
            try:
                await handler(event_type, data)
            except Exception as e:
                log.debug("Handler error (%s): %s", event_type, e)

    async def _apply_book_snapshot(self, data: dict) -> None:
        """Parse a full order book snapshot and update the data store."""
        token_id = data.get("asset_id") or data.get("token_id", "")
        if not token_id:
            return

        bids = [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in sorted(data.get("bids", []), key=lambda x: -float(x["price"]))
        ]
        asks = [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in sorted(data.get("asks", []), key=lambda x: float(x["price"]))
        ]
        book = OrderBook(token_id=token_id, bids=bids, asks=asks)
        await store.update_order_book(book)

    async def _apply_price_change(self, data: dict) -> None:
        """Apply incremental price-level updates to the stored order book."""
        token_id = data.get("asset_id") or data.get("token_id", "")
        if not token_id:
            return

        changes = data.get("changes", [])
        book = await store.get_order_book(token_id)
        if book is None:
            book = OrderBook(token_id=token_id)

        for change in changes:
            side = change.get("side", "").upper()
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))

            levels = book.bids if side == "BUY" else book.asks
            # Remove existing entry at this price
            levels[:] = [lv for lv in levels if lv.price != price]
            if size > 0:
                levels.append(PriceLevel(price=price, size=size))

        book.bids.sort(key=lambda x: -x.price)
        book.asks.sort(key=lambda x: x.price)
        book.updated_at = time.time()
        await store.update_order_book(book)

    async def add_subscription(self, token_ids: list[str]) -> None:
        """Dynamically add more token IDs while already connected."""
        new = set(token_ids) - self._subscribed_tokens
        if not new:
            return
        self._subscribed_tokens.update(new)
        if self._ws and not self._ws.closed:
            await self._send_subscription(self._ws, list(new))


# Module-level singleton
ws_client = WebSocketClient()
