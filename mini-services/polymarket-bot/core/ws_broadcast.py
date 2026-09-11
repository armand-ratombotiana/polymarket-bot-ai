"""WebSocket broadcast manager.

Manages connected clients and broadcasts real-time updates.
The frontend ``useWebSocket`` hook connects to ``/ws`` and receives
messages. The hybrid ``useRealtimeData`` hook filters incoming messages
by ``msg.channel === wsChannel`` so each panel can subscribe to a single
channel without paying the cost of the full snapshot.

Message format
--------------
Every broadcast emits a JSON envelope of the shape::

    {"channel": "positions", "data": {...}, "timestamp": 1234567890.0}

The welcome message sent on connect is the same envelope, with
``channel="system"`` and ``data={"type": "connected", ...}`` so a
client can confirm the round-trip and discover the channel catalog.

Channels
--------
Six channels are exposed by default (mirrors the W14-1 spec):

  * ``positions`` — open positions updates (mark-to-mid P&L, fills)
  * ``orders``    — open order state changes (place / cancel / fill)
  * ``trades``    — recent trade fills
  * ``metrics``   — ML model + drift + system metrics
  * ``alerts``    — alerting rule firings + kill-switch / observation-mode
  * ``system``    — periodic system status heartbeat (1s snapshot + 5s lean)

A client that connects WITHOUT specifying a ``channels`` set receives
every channel (broadcast fan-out is unconditional). A client that
sends a ``{"type": "subscribe", "channels": ["positions", "trades"]}``
message restricts delivery to only those channels — useful for a
narrow-purpose panel that doesn't want to be woken up by unrelated
broadcasts.

Concurrency
-----------
All client-registration operations (``connect`` / ``disconnect`` /
``subscribe``) acquire an ``asyncio.Lock`` so a client connecting at
the same instant another is disconnecting can't observe a half-removed
state. ``broadcast`` snapshots the client list under the lock, then
releases it before iterating — sending is I/O-bound and would
serialise every other connect/disconnect if the lock were held for the
duration. Per-client send failures are collected and cleaned up after
the loop under a second lock acquisition.

The singleton ``ws_manager`` is constructed at module-import time so
callers can grab it via ``from core.ws_broadcast import ws_manager``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


# ── Channels ─────────────────────────────────────────────────────────────────
# Exported as a module constant so callers (server.py, tests, future admin
# endpoints) can enumerate the catalog without poking the singleton.
WS_CHANNELS: frozenset[str] = frozenset(
    {"positions", "orders", "trades", "metrics", "alerts", "system"}
)


@dataclass
class ConnectedClient:
    """A single connected WebSocket client.

    ``channels`` is the set of channels this client is subscribed to.
    An EMPTY set means "all channels" — the broadcast fan-out delivers
    every channel to the client. A non-empty set restricts delivery to
    only the channels in the set. This matches the ``useRealtimeData``
    hook's contract: a panel that passes ``wsChannel="positions"``
    only wants ``positions`` messages, but the dashboard's central
    ``useBot`` hook wants everything.
    """

    websocket: Any
    client_id: str
    channels: Set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)


class WSBroadcastManager:
    """Manages WebSocket connections and broadcasts messages.

    Singleton instance: ``ws_manager`` at module level.

    Usage::

        from core.ws_broadcast import ws_manager

        # In the /ws endpoint:
        await websocket.accept()
        await ws_manager.connect(websocket, client_id)

        # From a state-change site:
        await ws_manager.broadcast("trades", {"type": "fill", ...})

        # From a periodic loop:
        await ws_manager.broadcast("system", {"balance": ..., "mode": ...})
    """

    def __init__(self) -> None:
        self._clients: Dict[str, ConnectedClient] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._message_count: int = 0
        self._error_count: int = 0
        self._channels: Set[str] = set(WS_CHANNELS)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def connect(
        self,
        websocket: Any,
        client_id: str,
        channels: Optional[Set[str]] = None,
    ) -> bool:
        """Register a new client connection.

        Sends a welcome message on the ``system`` channel so the client
        can confirm the round-trip and discover the channel catalog.
        Returns ``True`` on success (always — registration cannot fail
        unless the underlying websocket is already closed, in which
        case the welcome ``send_text`` raises and the caller surfaces
        the error).

        ``channels`` is the optional initial subscription set. ``None``
        or an empty set means "all channels" (broadcasts are
        unconditional). Use ``subscribe()`` to change the set later.
        """
        async with self._lock:
            client = ConnectedClient(
                websocket=websocket,
                client_id=client_id,
                channels=channels or set(),
            )
            self._clients[client_id] = client
            logger.info(
                "WS client connected: %s (total: %d)",
                client_id,
                len(self._clients),
            )

        # Send welcome message OUTSIDE the lock — ``send_text`` is I/O
        # and would serialise every other connect/disconnect if held.
        await self._send_to_client(
            client,
            {
                "channel": "system",
                "data": {
                    "type": "connected",
                    "client_id": client_id,
                    "channels": sorted(self._channels),
                },
                "timestamp": time.time(),
            },
        )
        return True

    async def disconnect(self, client_id: str) -> None:
        """Remove a client connection. No-op if the client_id is unknown."""
        async with self._lock:
            if client_id in self._clients:
                del self._clients[client_id]
                logger.info(
                    "WS client disconnected: %s (total: %d)",
                    client_id,
                    len(self._clients),
                )

    async def subscribe(self, client_id: str, channels: Set[str]) -> bool:
        """Update a client's channel subscriptions.

        Returns ``True`` if the client was found and updated, ``False``
        if the ``client_id`` is unknown (e.g. the client disconnected
        between the subscribe message arriving and this call). The
        return value is best-effort — callers can ignore it.
        """
        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return False
            # Filter to known channels so a client can't subscribe to a
            # non-existent channel and then silently miss broadcasts.
            client.channels = {c for c in channels if c in self._channels}
            logger.info(
                "Client %s subscribed to: %s", client_id, sorted(client.channels)
            )
            return True

    # ── Broadcast ───────────────────────────────────────────────────────────

    async def broadcast(self, channel: str, data: Any) -> int:
        """Broadcast a message to all clients subscribed to ``channel``.

        Returns the number of clients the message was delivered to
        (best-effort — a send that raises after the count is taken is
        still counted, but the dead client is cleaned up afterwards).

        Unknown channels are logged at WARNING and dropped — typos in
        channel names are a common source of "why isn't my panel
        updating" bugs, so we surface them loudly.

        Skips the (relatively expensive) ``json.dumps`` call when no
        clients are connected — the periodic broadcaster fires every
        5s regardless of client count, and the 1s snapshot loop fires
        every second; without the early return, a server with no
        clients connected would still serialize the snapshot to JSON
        on every tick.
        """
        if channel not in self._channels:
            logger.warning(
                "Unknown WS channel %r — message dropped (known: %s)",
                channel,
                sorted(self._channels),
            )
            return 0

        # Fast-path: no clients connected → skip serialization entirely.
        # The lock is acquired only to read the count, then released
        # before the (potentially expensive) json.dumps call.
        async with self._lock:
            if not self._clients:
                return 0
            clients = list(self._clients.values())

        message = {
            "channel": channel,
            "data": data,
            "timestamp": time.time(),
        }
        payload = json.dumps(message, default=str)

        dead_clients: list[str] = []
        delivered = 0
        for client in clients:
            # Empty channel set = all channels. Non-empty = explicit
            # subscription required.
            if client.channels and channel not in client.channels:
                continue
            try:
                await client.websocket.send_text(payload)
                delivered += 1
                self._message_count += 1
            except Exception as e:  # noqa: BLE001 — any send failure = dead client
                logger.debug(
                    "Failed to send to %s: %s — removing", client.client_id, e
                )
                dead_clients.append(client.client_id)
                self._error_count += 1

        # Clean up dead clients under a second lock acquisition.
        if dead_clients:
            async with self._lock:
                for cid in dead_clients:
                    self._clients.pop(cid, None)

        return delivered

    async def _send_to_client(self, client: ConnectedClient, message: dict) -> None:
        """Send a message to a single client (envelope-wrapped JSON)."""
        await client.websocket.send_text(json.dumps(message, default=str))

    # ── Introspection ───────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return a snapshot of broadcast manager state.

        Safe to call from sync context — the dict reads are atomic
        enough for a status endpoint. The ``client_ids`` list is a
        point-in-time snapshot (a client may disconnect between the
        ``len`` and the ``list(self._clients.keys())`` calls, but that
        only means the returned list may briefly include a now-gone
        ID — never that it misses a connected client).
        """
        return {
            "connected_clients": len(self._clients),
            "total_messages_sent": self._message_count,
            "total_errors": self._error_count,
            "channels": sorted(self._channels),
            "client_ids": list(self._clients.keys()),
        }

    def get_client_channels(self, client_id: str) -> Optional[Set[str]]:
        """Return the set of channels a client is subscribed to.

        ``None`` if the client_id is unknown. An EMPTY set means "all
        channels" (the client hasn't restricted its subscription).
        """
        client = self._clients.get(client_id)
        if client is None:
            return None
        return set(client.channels)


# Singleton — mirrors the convention used by ``core.data_store.store``,
# ``core.audit_logger.audit_logger``, ``core.alerting.alert_engine``, etc.
ws_manager = WSBroadcastManager()


__all__ = [
    "ConnectedClient",
    "WS_CHANNELS",
    "WSBroadcastManager",
    "ws_manager",
]
