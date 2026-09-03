"""
Unit tests for ``core.ws_broadcast.py``.

W14-1 — WebSocket broadcast system.

Coverage:
  (1) ``connect`` registers a client, increments the client count,
      and sends a welcome envelope on the ``system`` channel whose
      ``data.type == "connected"`` and ``data.client_id`` matches the
      registered id.
  (2) ``disconnect`` removes a client (idempotent — second call is a
      no-op, doesn't raise).
  (3) ``broadcast`` delivers the envelope to every connected client
      (default subscription = all channels).
  (4) ``broadcast`` skips clients whose subscription set does NOT
      include the broadcast channel.
  (5) ``broadcast`` cleans up dead clients (send_text raises) and
      increments ``total_errors``.
  (6) ``broadcast`` on an unknown channel is a no-op (returns 0,
      logs a warning, no client receives the message).
  (7) ``broadcast`` early-returns 0 when no clients are connected
      (skips ``json.dumps`` entirely).
  (8) ``subscribe`` updates a client's channel set so subsequent
      broadcasts respect the new filter.
  (9) ``subscribe`` filters out unknown channels (a typo can't
      silently subscribe a client to nothing).
  (10) ``subscribe`` on an unknown client_id returns False (no-op).
  (11) ``get_stats`` returns connected_clients / total_messages_sent /
       total_errors / channels / client_ids in the documented shape.
  (12) ``get_client_channels`` returns the live subscription set
       (None for an unknown client; empty set = "all channels").
  (13) The welcome message envelope shape matches what the frontend
       ``useRealtimeData`` hook expects: ``{channel, data, timestamp}``
       with ``data.type`` and ``data.channels`` keys.
  (14) Multiple clients can coexist — broadcast delivers to every one.
  (15) Broadcasting two different channels in sequence delivers each
       only to clients subscribed to that channel (cross-talk test).

Each test constructs a fresh ``WSBroadcastManager`` instance so there
is zero state leakage across tests (the module-level singleton
``ws_manager`` is left untouched).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Make the polymarket-bot package root importable as top-level modules.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from core.ws_broadcast import (  # noqa: E402
    WS_CHANNELS,
    ConnectedClient,
    WSBroadcastManager,
    ws_manager,
)

# All tests in this module are async (the WSBroadcastManager uses
# asyncio.Lock + awaitable send_text). ``pytestmark`` applies the
# asyncio marker to every ``async def test_...`` below without per-test
# decoration. The two "pure-Python" tests (dataclass defaults, singleton
# type-check) are also ``async def`` so they share the marker cleanly —
# the asyncio runner is fine running a no-await coroutine.
pytestmark = pytest.mark.asyncio


# ── Fake WebSocket ────────────────────────────────────────────────────────────


class FakeWebSocket:
    """In-memory stand-in for ``starlette.websockets.WebSocket``.

    Captures every ``send_text`` payload in ``self.sent`` (a list of
    raw JSON strings). ``fail_after`` simulates a dead client: once
    that many messages have been delivered, subsequent ``send_text``
    calls raise ``ConnectionClosed`` — mirrors the real WebSocket's
    behaviour when the underlying TCP socket has been closed by the
    peer. ``fail_after=None`` (default) means never fail.

    ``closed`` is set True when the simulated socket drops so a test
    can assert the manager cleaned up the client after a send failure.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._fail_after = fail_after
        self._sent_count = 0

    async def send_text(self, payload: str) -> None:
        if self._fail_after is not None and self._sent_count >= self._fail_after:
            self.closed = True
            raise ConnectionClosed("simulated dead client")
        self.sent.append(payload)
        self._sent_count += 1

    # ``send_bytes`` etc. are unused by WSBroadcastManager — included so
    # a future change to the manager doesn't break the fake's contract.
    async def accept(self) -> None:  # pragma: no cover — not called by manager
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:  # pragma: no cover
        self.closed = True


class ConnectionClosed(Exception):
    """Raised by FakeWebSocket.send_text when the simulated socket is dead."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_envelope(raw: str) -> dict[str, Any]:
    """Parse a broadcast envelope and assert the canonical shape.

    The envelope is ``{"channel": str, "data": Any, "timestamp": float}``.
    A test that fails to parse or that finds a missing key fails loudly
    rather than silently passing on a malformed broadcast.
    """
    msg = json.loads(raw)
    assert "channel" in msg, f"envelope missing 'channel' key: {msg!r}"
    assert "data" in msg, f"envelope missing 'data' key: {msg!r}"
    assert "timestamp" in msg, f"envelope missing 'timestamp' key: {msg!r}"
    assert isinstance(msg["timestamp"], (int, float)), (
        f"envelope 'timestamp' must be numeric, got {type(msg['timestamp'])!r}"
    )
    return msg


# ── (1) connect registers + sends welcome ─────────────────────────────────────


async def test_connect_registers_client_and_sends_welcome():
    """``connect`` adds the client to the registry and pushes a welcome envelope.

    The welcome envelope is on the ``system`` channel with
    ``data.type == "connected"`` and ``data.client_id`` matching the
    registered id, so a client can confirm the round-trip and discover
    the channel catalog (``data.channels``).
    """
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    ok = await mgr.connect(ws, "client-1")
    assert ok is True, "connect must return True on success"

    stats = mgr.get_stats()
    assert stats["connected_clients"] == 1
    assert "client-1" in stats["client_ids"]

    # The welcome message is the ONLY thing sent on connect.
    assert len(ws.sent) == 1, f"expected 1 welcome message, got {len(ws.sent)}"
    msg = _parse_envelope(ws.sent[0])
    assert msg["channel"] == "system"
    assert msg["data"]["type"] == "connected"
    assert msg["data"]["client_id"] == "client-1"
    # ``channels`` is the canonical catalog surfaced to the client.
    assert set(msg["data"]["channels"]) == set(WS_CHANNELS)


# ── (2) disconnect removes client (idempotent) ─────────────────────────────────


async def test_disconnect_removes_client():
    """``disconnect`` drops the client from the registry."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "client-1")
    assert mgr.get_stats()["connected_clients"] == 1

    await mgr.disconnect("client-1")
    assert mgr.get_stats()["connected_clients"] == 0
    assert "client-1" not in mgr.get_stats()["client_ids"]


async def test_disconnect_unknown_client_is_noop():
    """``disconnect`` on an unknown client_id is a silent no-op (no raise)."""
    mgr = WSBroadcastManager()
    # Should not raise.
    await mgr.disconnect("never-connected")
    assert mgr.get_stats()["connected_clients"] == 0


async def test_disconnect_is_idempotent():
    """Calling ``disconnect`` twice on the same client is safe."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "client-1")
    await mgr.disconnect("client-1")
    # Second call must not raise.
    await mgr.disconnect("client-1")
    assert mgr.get_stats()["connected_clients"] == 0


# ── (3) broadcast delivers to every connected client (default = all channels) ─


async def test_broadcast_delivers_to_all_clients_by_default():
    """A client with NO subscription set receives every broadcast."""
    mgr = WSBroadcastManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await mgr.connect(ws1, "c1")
    await mgr.connect(ws2, "c2")

    # Discard the welcome messages so we can assert on broadcast output alone.
    ws1.sent.clear()
    ws2.sent.clear()

    delivered = await mgr.broadcast("trades", {"type": "fill", "price": 0.55})
    assert delivered == 2, f"expected delivery to 2 clients, got {delivered}"

    for ws in (ws1, ws2):
        assert len(ws.sent) == 1, f"expected 1 broadcast, got {len(ws.sent)}"
        msg = _parse_envelope(ws.sent[0])
        assert msg["channel"] == "trades"
        assert msg["data"]["type"] == "fill"
        assert msg["data"]["price"] == 0.55


# ── (4) broadcast respects channel filtering ──────────────────────────────────


async def test_broadcast_skips_clients_not_subscribed_to_channel():
    """A client subscribed to ``["positions"]`` does NOT receive ``trades``."""
    mgr = WSBroadcastManager()
    ws_positions = FakeWebSocket()
    ws_trades = FakeWebSocket()
    await mgr.connect(ws_positions, "c-positions")
    await mgr.connect(ws_trades, "c-trades")

    # Restrict c-positions to the "positions" channel only.
    await mgr.subscribe("c-positions", {"positions"})

    # Clear welcomes.
    ws_positions.sent.clear()
    ws_trades.sent.clear()

    delivered = await mgr.broadcast("trades", {"type": "fill"})
    # Only c-trades (default subscription = all channels) should receive it.
    assert delivered == 1, f"expected delivery to 1 client, got {delivered}"
    assert len(ws_trades.sent) == 1
    assert len(ws_positions.sent) == 0, (
        "client subscribed to 'positions' must NOT receive 'trades' broadcasts"
    )


# ── (5) broadcast cleans up dead clients ──────────────────────────────────────


async def test_broadcast_removes_dead_clients():
    """A client whose ``send_text`` raises is removed from the registry.

    The failed send is counted in ``total_errors``; subsequent
    broadcasts skip the dead client (no further send attempts).
    """
    mgr = WSBroadcastManager()
    ws_dead = FakeWebSocket(fail_after=1)  # dies after the welcome message
    ws_alive = FakeWebSocket()
    await mgr.connect(ws_dead, "c-dead")
    await mgr.connect(ws_alive, "c-alive")

    # Clear welcomes (the welcome itself succeeds — fail_after=1 means
    # the FIRST broadcast after the welcome raises).
    ws_dead.sent.clear()
    ws_alive.sent.clear()

    delivered = await mgr.broadcast("trades", {"type": "fill"})
    # The dead client raised, the alive one succeeded.
    assert delivered == 1, f"expected 1 successful delivery, got {delivered}"
    assert ws_dead.closed is True, "dead client must be marked closed"
    assert len(ws_alive.sent) == 1

    stats = mgr.get_stats()
    assert stats["total_errors"] >= 1, "send failure must increment error counter"
    # The dead client must have been pruned from the registry.
    assert "c-dead" not in stats["client_ids"], (
        "dead client must be removed from the registry after a send failure"
    )
    assert stats["connected_clients"] == 1


# ── (6) unknown channel is a no-op ───────────────────────────────────────────


async def test_broadcast_unknown_channel_is_noop():
    """Broadcasting on a channel not in ``WS_CHANNELS`` returns 0 and sends nothing."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    ws.sent.clear()

    delivered = await mgr.broadcast("nonexistent", {"foo": "bar"})
    assert delivered == 0
    assert len(ws.sent) == 0, "unknown-channel broadcast must not deliver anything"


# ── (7) broadcast early-returns when no clients are connected ─────────────────


async def test_broadcast_returns_zero_when_no_clients():
    """``broadcast`` with zero connected clients returns 0 (and skips json.dumps)."""
    mgr = WSBroadcastManager()
    delivered = await mgr.broadcast("trades", {"type": "fill"})
    assert delivered == 0
    assert mgr.get_stats()["total_messages_sent"] == 0


# ── (8) subscribe updates a client's channel set ───────────────────────────────


async def test_subscribe_restricts_delivery():
    """After ``subscribe(client, {"positions"})``, only ``positions`` broadcasts land."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    await mgr.subscribe("c1", {"positions"})
    ws.sent.clear()

    # ``positions`` broadcast — delivered.
    await mgr.broadcast("positions", {"type": "update"})
    assert len(ws.sent) == 1
    msg = _parse_envelope(ws.sent[0])
    assert msg["channel"] == "positions"
    assert msg["data"]["type"] == "update"

    # ``trades`` broadcast — skipped (client is positions-only).
    ws.sent.clear()
    await mgr.broadcast("trades", {"type": "fill"})
    assert len(ws.sent) == 0, (
        "after subscribe({positions}), client must NOT receive trades broadcasts"
    )


async def test_subscribe_resets_to_all_channels_with_empty_set():
    """An empty subscription set means "all channels" (default).

    A client that previously restricted to {"positions"} and then
    subscribes to {} receives every channel again.
    """
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    await mgr.subscribe("c1", {"positions"})
    # Reset to all-channels.
    await mgr.subscribe("c1", set())
    ws.sent.clear()

    await mgr.broadcast("trades", {"type": "fill"})
    assert len(ws.sent) == 1, (
        "after subscribe({}), client must receive broadcasts on every channel"
    )


# ── (9) subscribe filters out unknown channels ────────────────────────────────


async def test_subscribe_drops_unknown_channels():
    """A typo in the subscribe request doesn't silently subscribe to nothing.

    ``subscribe("c1", {"positions", "typos"})`` results in the client
    being subscribed to ``{"positions"}`` only — the unknown
    ``"typos"`` channel is filtered out so the client still receives
    the channels it CAN subscribe to.
    """
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    await mgr.subscribe("c1", {"positions", "typos", "trades", "another_typo"})

    channels = mgr.get_client_channels("c1")
    assert channels == {"positions", "trades"}, (
        f"unknown channels must be filtered out; got {channels!r}"
    )


# ── (10) subscribe on unknown client returns False ────────────────────────────


async def test_subscribe_unknown_client_returns_false():
    """``subscribe`` on an unknown client_id returns False (no-op, no raise)."""
    mgr = WSBroadcastManager()
    ok = await mgr.subscribe("never-connected", {"positions"})
    assert ok is False


# ── (11) get_stats shape ──────────────────────────────────────────────────────


async def test_get_stats_shape():
    """``get_stats`` returns the documented dict shape with all keys present."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    await mgr.broadcast("trades", {"x": 1})  # one successful delivery
    await mgr.broadcast("nonexistent", {"y": 2})  # dropped (unknown channel)

    stats = mgr.get_stats()
    assert set(stats.keys()) == {
        "connected_clients",
        "total_messages_sent",
        "total_errors",
        "channels",
        "client_ids",
    }
    assert stats["connected_clients"] == 1
    assert stats["total_messages_sent"] == 1, (
        "only the successful 'trades' broadcast must be counted; "
        "the unknown-channel broadcast must not increment the counter"
    )
    assert stats["total_errors"] == 0
    assert set(stats["channels"]) == set(WS_CHANNELS)
    assert stats["client_ids"] == ["c1"]


# ── (12) get_client_channels ──────────────────────────────────────────────────


async def test_get_client_channels_returns_none_for_unknown():
    """``get_client_channels`` returns None for an unknown client_id."""
    mgr = WSBroadcastManager()
    assert mgr.get_client_channels("never-connected") is None


async def test_get_client_channels_returns_empty_set_for_default_subscription():
    """An empty set means "all channels" (the connect default)."""
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    channels = mgr.get_client_channels("c1")
    assert channels == set(), (
        "default subscription must be an empty set (= all channels); "
        f"got {channels!r}"
    )


# ── (13) envelope shape matches the frontend useRealtimeData contract ─────────


async def test_envelope_shape_matches_frontend_contract():
    """The broadcast envelope matches ``{channel, data, timestamp}``.

    The frontend ``useRealtimeData`` hook filters by ``msg.channel ===
    wsChannel`` and reads ``msg.data`` as the new state. The envelope
    MUST have ``channel`` (string), ``data`` (any JSON value), and
    ``timestamp`` (numeric) — anything else breaks the hook.
    """
    mgr = WSBroadcastManager()
    ws = FakeWebSocket()
    await mgr.connect(ws, "c1")
    ws.sent.clear()

    await mgr.broadcast("positions", {"positions": [], "daily_pnl": 1.23})

    msg = _parse_envelope(ws.sent[0])
    assert msg["channel"] == "positions"
    assert msg["data"]["daily_pnl"] == 1.23
    assert msg["timestamp"] <= time.time() + 1  # sanity bound


# ── (14) multiple clients coexist ─────────────────────────────────────────────


async def test_multiple_clients_coexist():
    """Three connected clients all receive the same broadcast."""
    mgr = WSBroadcastManager()
    sockets = [FakeWebSocket() for _ in range(3)]
    for i, ws in enumerate(sockets):
        await mgr.connect(ws, f"c{i}")

    for ws in sockets:
        ws.sent.clear()

    delivered = await mgr.broadcast("system", {"type": "status", "balance": 100.0})
    assert delivered == 3
    for ws in sockets:
        assert len(ws.sent) == 1


# ── (15) no cross-talk between channels ───────────────────────────────────────


async def test_no_cross_talk_between_channels():
    """Broadcasting on ``trades`` does NOT deliver to a positions-only client.

    Two clients — one subscribed to ``trades``, the other to
    ``positions``. Broadcasting ``trades`` then ``positions`` should
    deliver each message to exactly one client (no cross-talk).
    """
    mgr = WSBroadcastManager()
    ws_trades = FakeWebSocket()
    ws_positions = FakeWebSocket()
    await mgr.connect(ws_trades, "c-trades")
    await mgr.connect(ws_positions, "c-positions")
    await mgr.subscribe("c-trades", {"trades"})
    await mgr.subscribe("c-positions", {"positions"})

    ws_trades.sent.clear()
    ws_positions.sent.clear()

    delivered_trades = await mgr.broadcast("trades", {"type": "fill"})
    delivered_positions = await mgr.broadcast("positions", {"type": "update"})

    assert delivered_trades == 1
    assert delivered_positions == 1
    assert len(ws_trades.sent) == 1
    assert len(ws_positions.sent) == 1

    trades_msg = _parse_envelope(ws_trades.sent[0])
    positions_msg = _parse_envelope(ws_positions.sent[0])
    assert trades_msg["channel"] == "trades"
    assert positions_msg["channel"] == "positions"


# ── (16) Module-level singleton is a WSBroadcastManager ───────────────────────


async def test_ws_manager_singleton_is_a_broadcast_manager():
    """``ws_manager`` is the module-level singleton instance.

    Pure test — verifies the import contract without exercising any
    meaningful async paths (no awaits, no I/O). Declared ``async`` so
    it shares the module-level ``pytest.mark.asyncio`` marker cleanly.
    """
    assert isinstance(ws_manager, WSBroadcastManager)
    # ``get_stats`` is callable without an event loop because the
    # lock is only acquired for mutations, not reads.
    stats = ws_manager.get_stats()
    assert "channels" in stats
    assert set(stats["channels"]) == set(WS_CHANNELS)


# ── (17) ConnectedClient dataclass ────────────────────────────────────────────


async def test_connected_client_dataclass_defaults():
    """``ConnectedClient`` defaults to an empty channel set (all channels).

    Declared ``async`` to share the module-level asyncio marker — the
    body has no awaits.
    """
    ws = FakeWebSocket()
    client = ConnectedClient(websocket=ws, client_id="c1")
    assert client.channels == set()
    assert client.connected_at > 0


# ── (18) Concurrent connect + broadcast doesn't drop messages ────────────────


async def test_concurrent_connect_and_broadcast():
    """A client connecting concurrently with a broadcast still receives the next one.

    Race condition check: ``broadcast`` snapshots the client list
    under the lock, then releases it before iterating. A client
    connecting DURING the iteration won't be in the snapshot — that's
    expected (the broadcast was already in flight). The NEXT broadcast
    must deliver to the new client. This test exercises that contract
    by spawning connect + broadcast concurrently and verifying the
    second broadcast reaches the new client.
    """
    mgr = WSBroadcastManager()

    # Step 1: connect a client.
    ws1 = FakeWebSocket()
    await mgr.connect(ws1, "c1")
    ws1.sent.clear()

    # Step 2: concurrently (a) broadcast, (b) connect a second client.
    ws2 = FakeWebSocket()
    await asyncio.gather(
        mgr.broadcast("trades", {"seq": 1}),
        mgr.connect(ws2, "c2"),
    )

    # ``ws1`` may or may not have received the broadcast depending on
    # scheduling order — the contract is that the NEXT broadcast
    # reaches both. Clear and verify.
    ws1.sent.clear()
    ws2.sent.clear()

    delivered = await mgr.broadcast("trades", {"seq": 2})
    assert delivered == 2, (
        f"after concurrent connect+broadcast, the next broadcast must reach "
        f"both clients; got {delivered}"
    )
    assert len(ws1.sent) == 1
    assert len(ws2.sent) == 1
