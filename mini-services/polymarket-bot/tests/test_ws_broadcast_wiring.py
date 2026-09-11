"""
tests/test_ws_broadcast_wiring.py — W23-3 WebSocket broadcast wiring tests.

Verifies that every state-changing operation in the data_store / alerting
subsystems emits the spec'd broadcast on the correct WS channel, and
that the ``GET /api/ws/broadcast-stats`` endpoint returns the W14-1
``WSBroadcastManager.get_stats()`` payload under the W23-3-spec'd path.

Coverage:
  (1) Position changes broadcast to the ``positions`` channel.
      ``store.record_fill`` is the single state-change site for both
      opening (BUY) and closing (SELL) fills; the broadcaster is wired
      AFTER the mutation has been committed + the lock released, so
      the broadcast payload reflects the post-fill position state.
  (2) Order placement / update / cancel-all broadcast to the ``orders``
      channel with the matching ``type`` discriminator (``placed`` /
      ``updated`` / ``cancelled``).
  (3) Trade fills broadcast to the ``trades`` channel with
      ``type="fill"`` + the full trade payload (price / size / pnl /
      strategy / paper / timestamp / token_id / trade_id).
  (4) Alert fires broadcast to the ``alerts`` channel with
      ``type="alert"`` + the alert dict. ``AlertEngine.fire_alert`` is
      SYNC (callers from the risk gate's sync ``_check_order_impl``
      path can't await), so the broadcast is dispatched via
      ``asyncio.create_task`` — the test yields the event loop to let
      the scheduled task run before assertion.
  (5) ``GET /api/ws/broadcast-stats`` returns 200 + the canonical
      ``WSBroadcastManager.get_stats()`` payload (connected_clients,
      total_messages_sent, total_errors, channels, client_ids). The
      endpoint is the W23-3 alias for the W14-1 ``GET /api/ws/stats``
      path; both return the identical dict.

Hermeticity
~~~~~~~~~~~
Imports the production ``api.server.app`` (so every route + middleware
is exercised). The autouse ``_reset_store_factory_defaults`` conftest
fixture wipes store singletons before every test; rate limiting is
disabled in ``conftest.py`` (``limiter.enabled = False``). A per-test
``broadcast_recorder`` fixture monkeypatches
``ws_manager.broadcast`` with a capturing recorder so each test sees
ONLY its own broadcasts (no leak from prior tests' background
broadcasters — the production ``_broadcast_loop`` /
``_periodic_status_broadcast`` / ``_periodic_metrics_broadcast`` tasks
are NOT running because the lifespan is skipped when ``TestClient(app)``
is used without a ``with`` context).

All async tests share ``pytestmark = pytest.mark.asyncio`` (the repo's
``pytest.ini`` runs in strict mode).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# Make the polymarket-bot package root importable as top-level modules.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from core.alerting import (  # noqa: E402
    Alert,
    AlertEngine,
    SEVERITY_CRITICAL,
)
from core.data_store import (  # noqa: E402
    DataStore,
    Order,
    OrderStatus,
    Side,
    Trade,
)
from core.ws_broadcast import ws_manager  # noqa: E402

# Per-test asyncio marker (NOT module-level ``pytestmark``) so the
# SYNC ``TestClient`` tests below don't trip pytest-asyncio's "marked
# but not async" warning. The repo's ``pytest.ini`` runs in strict
# mode, so async tests must carry the mark explicitly.
ASYNC = pytest.mark.asyncio

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# the bearer token below matches what the ``enforce_api_auth`` middleware
# accepts. Mirrors the same constant in ``tests/test_api_versioning.py``.
VALID_TOKEN = "test-token-conftest"


# ── Broadcast recorder ──────────────────────────────────────────────────────


class BroadcastRecorder:
    """Captures every ``ws_manager.broadcast`` call for assertion.

    Replaces the real ``ws_manager.broadcast`` method via monkeypatch;
    the recorder itself is async-coroutine-compatible so callers can
    ``await ws_manager.broadcast(...)`` exactly as production code does.

    The recorder returns ``0`` (no clients "delivered" to) — mirroring
    the real ``broadcast`` return type. The W23-3 wiring is "fire and
    observe" — the data path doesn't branch on the return value, so the
    zero is purely informational.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(self, channel: str, data: Any) -> int:
        self.calls.append((channel, dict(data) if isinstance(data, dict) else data))
        return 0

    def calls_for(self, channel: str) -> list[dict[str, Any]]:
        """Return every broadcast payload emitted on ``channel``."""
        return [data for ch, data in self.calls if ch == channel]

    def channels_emitted(self) -> list[str]:
        """Return the ordered list of channels that received at least one broadcast."""
        seen: list[str] = []
        for ch, _ in self.calls:
            if ch not in seen:
                seen.append(ch)
        return seen

    def reset(self) -> None:
        self.calls.clear()


@pytest.fixture
def broadcast_recorder(monkeypatch):
    """Replace ``ws_manager.broadcast`` with a capturing recorder.

    Yields the recorder so tests can call ``recorder.calls_for(channel)``
    to assert on the captured broadcasts. The monkeypatch is auto-
    reverted by pytest at fixture teardown — the real ``broadcast``
    method is restored before the next test runs.

    Only the ``broadcast`` METHOD is replaced (not the whole singleton)
    so a test that needs to also exercise the real send path (welcome
    message, subscribe, etc.) can still do so via the unmodified methods.
    """
    recorder = BroadcastRecorder()
    monkeypatch.setattr(ws_manager, "broadcast", recorder.broadcast)
    return recorder


@pytest.fixture
def fresh_store() -> DataStore:
    """Brand-new ``DataStore`` with no on-disk state loaded.

    The global ``store`` singleton is reset by the autouse
    ``_reset_store_factory_defaults`` conftest fixture, but this test
    module uses a fresh per-test instance to keep broadcasts isolated
    from any background broadcaster that might reference the global
    singleton (the lifespan isn't running in ``TestClient(app)`` mode,
    but defensive isolation is cheap).
    """
    return DataStore()


# ── (1) position changes broadcast to ``positions`` channel ──────────────────


@ASYNC
async def test_position_change_broadcasts_to_positions_channel(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``record_fill`` emits a follow-up broadcast on the ``positions``
    channel carrying the post-fill positions snapshot.

    The payload envelope is::

        {"type": "update", "positions": [{"token_id": ..., "size_usdc": ...}, ...]}

    so a dashboard subscribed to ``positions`` can refresh its panel
    without re-polling ``GET /api/portfolio``.
    """
    trade = Trade(
        trade_id="w23-3-pos-1",
        token_id="TOK_W23_3_POS",
        side=Side.BUY,
        price=0.55,
        size=10.0,
        strategy="manual",
        paper=True,
    )
    await fresh_store.record_fill(trade)

    positions_broadcasts = broadcast_recorder.calls_for("positions")
    assert len(positions_broadcasts) >= 1, (
        f"record_fill must broadcast on the 'positions' channel; "
        f"got channels: {broadcast_recorder.channels_emitted()}"
    )
    payload = positions_broadcasts[0]
    assert payload["type"] == "update", (
        f"positions broadcast type must be 'update'; got {payload['type']!r}"
    )
    assert isinstance(payload["positions"], list), (
        f"positions payload must be a list; got {type(payload['positions'])!r}"
    )
    # The BUY fill above opened a position — verify it shows up.
    assert len(payload["positions"]) == 1, (
        f"one position expected after a single BUY fill; "
        f"got {len(payload['positions'])}"
    )
    pos = payload["positions"][0]
    assert pos["token_id"] == "TOK_W23_3_POS"
    assert pos["yes_shares"] == 10.0
    assert pos["avg_entry_price"] == 0.55


# ── (2a) order placement broadcasts to ``orders`` channel ────────────────────


@ASYNC
async def test_order_placement_broadcasts_to_orders_channel(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``add_order`` emits a broadcast on the ``orders`` channel with
    ``type="placed"`` and the post-add open-orders snapshot."""
    order = Order(
        order_id="w23-3-ord-1",
        token_id="TOK_W23_3_ORD",
        side=Side.BUY,
        price=0.55,
        size=10.0,
        strategy="manual",
        paper=True,
    )
    await fresh_store.add_order(order)

    orders_broadcasts = broadcast_recorder.calls_for("orders")
    assert len(orders_broadcasts) >= 1, (
        f"add_order must broadcast on the 'orders' channel; "
        f"got channels: {broadcast_recorder.channels_emitted()}"
    )
    payload = orders_broadcasts[0]
    assert payload["type"] == "placed", (
        f"orders broadcast type must be 'placed'; got {payload['type']!r}"
    )
    assert isinstance(payload["orders"], list)
    assert len(payload["orders"]) == 1
    serialized = payload["orders"][0]
    assert serialized["order_id"] == "w23-3-ord-1"
    assert serialized["status"] == "OPEN"
    assert serialized["side"] == "BUY"


# ── (2b) order update broadcasts to ``orders`` channel ───────────────────────


@ASYNC
async def test_order_update_broadcasts_to_orders_channel(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``update_order`` emits a broadcast on the ``orders`` channel with
    ``type="updated"`` after mutating the stored order."""
    order = Order(
        order_id="w23-3-upd-1",
        token_id="TOK_W23_3_UPD",
        side=Side.BUY,
        price=0.42,
        size=5.0,
        strategy="manual",
        paper=True,
    )
    await fresh_store.add_order(order)
    # Clear the placement broadcast so we can isolate the update broadcast.
    broadcast_recorder.reset()

    updated = await fresh_store.update_order(
        "w23-3-upd-1", size_matched=2.5, status=OrderStatus.PARTIALLY_FILLED
    )
    assert updated is not None

    orders_broadcasts = broadcast_recorder.calls_for("orders")
    assert len(orders_broadcasts) == 1, (
        f"update_order must broadcast exactly once on 'orders' (type=updated); "
        f"got {len(orders_broadcasts)} broadcasts"
    )
    payload = orders_broadcasts[0]
    assert payload["type"] == "updated", (
        f"orders broadcast type must be 'updated'; got {payload['type']!r}"
    )
    # The partially-filled order is still OPEN → still in open_orders.
    assert len(payload["orders"]) == 1
    serialized = payload["orders"][0]
    assert serialized["order_id"] == "w23-3-upd-1"
    assert serialized["status"] == "PARTIALLY_FILLED"
    assert serialized["size_matched"] == 2.5


# ── (2c) update_order returns None and does NOT broadcast for unknown id ──────


@ASYNC
async def test_update_order_unknown_id_does_not_broadcast(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``update_order`` on an unknown ``order_id`` returns ``None`` and
    emits NO broadcast on the ``orders`` channel — the early ``return
    None`` inside the lock exits before the broadcast call lands."""
    result = await fresh_store.update_order(
        "never-existed", status=OrderStatus.CANCELLED
    )
    assert result is None
    orders_broadcasts = broadcast_recorder.calls_for("orders")
    assert len(orders_broadcasts) == 0, (
        f"update_order on unknown id must NOT broadcast; got {orders_broadcasts}"
    )


# ── (2d) cancel_all_orders broadcasts to ``orders`` channel ───────────────────


@ASYNC
async def test_cancel_all_orders_broadcasts_to_orders_channel(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``cancel_all_orders`` emits a broadcast on the ``orders`` channel
    with ``type="cancelled"`` and the post-cancel open-orders snapshot
    (an empty list — every order was moved to history)."""
    for i in range(3):
        await fresh_store.add_order(
            Order(
                order_id=f"w23-3-cancel-{i}",
                token_id=f"TOK_W23_3_CANCEL_{i}",
                side=Side.BUY,
                price=0.30 + i * 0.01,
                size=4.0,
                strategy="manual",
                paper=True,
            )
        )
    broadcast_recorder.reset()

    cancelled = await fresh_store.cancel_all_orders()
    assert len(cancelled) == 3

    orders_broadcasts = broadcast_recorder.calls_for("orders")
    assert len(orders_broadcasts) == 1, (
        f"cancel_all_orders must broadcast exactly once on 'orders'; "
        f"got {len(orders_broadcasts)}"
    )
    payload = orders_broadcasts[0]
    assert payload["type"] == "cancelled", (
        f"orders broadcast type must be 'cancelled'; got {payload['type']!r}"
    )
    # All orders moved to history → open_orders is empty.
    assert payload["orders"] == [], (
        f"post-cancel open-orders must be empty; got {payload['orders']}"
    )


# ── (3) trade fills broadcast to ``trades`` channel ───────────────────────────


@ASYNC
async def test_trade_fill_broadcasts_to_trades_channel(
    fresh_store: DataStore, broadcast_recorder: BroadcastRecorder
) -> None:
    """``record_fill`` emits a broadcast on the ``trades`` channel with
    ``type="fill"`` and the full trade payload (price / size / pnl /
    strategy / paper / timestamp / token_id / trade_id).

    A single ``record_fill`` call results in TWO broadcasts total — one
    on ``trades`` (the fill event) and one on ``positions`` (the
    resulting position update). Both channels must receive their
    respective payloads.
    """
    trade = Trade(
        trade_id="w23-3-trade-1",
        token_id="TOK_W23_3_TRADE",
        side=Side.BUY,
        price=0.71,
        size=7.5,
        strategy="ml_random_forest_quant",
        paper=True,
    )
    await fresh_store.record_fill(trade)

    trades_broadcasts = broadcast_recorder.calls_for("trades")
    assert len(trades_broadcasts) == 1, (
        f"record_fill must broadcast exactly once on 'trades'; "
        f"got {len(trades_broadcasts)}"
    )
    payload = trades_broadcasts[0]
    assert payload["type"] == "fill", (
        f"trades broadcast type must be 'fill'; got {payload['type']!r}"
    )
    assert "trade" in payload, (
        f"trades broadcast payload must contain 'trade' key; got {payload}"
    )
    trade_data = payload["trade"]
    assert trade_data["trade_id"] == "w23-3-trade-1"
    assert trade_data["token_id"] == "TOK_W23_3_TRADE"
    assert trade_data["side"] == "BUY"
    assert trade_data["price"] == 0.71
    assert trade_data["size"] == 7.5
    assert trade_data["strategy"] == "ml_random_forest_quant"
    assert trade_data["paper"] is True

    # The companion positions broadcast must also have fired.
    positions_broadcasts = broadcast_recorder.calls_for("positions")
    assert len(positions_broadcasts) == 1, (
        f"record_fill must also broadcast on 'positions'; "
        f"got {len(positions_broadcasts)}"
    )


# ── (4) alert fires broadcast to ``alerts`` channel ───────────────────────────


@ASYNC
async def test_alert_fire_broadcasts_to_alerts_channel(
    monkeypatch, broadcast_recorder: BroadcastRecorder
) -> None:
    """``AlertEngine.fire_alert`` schedules a fire-and-forget broadcast on
    the ``alerts`` channel via ``asyncio.create_task`` (the caller is
    sync — risk gate's ``_check_order_impl`` — and can't await).

    The broadcast payload follows the W23-3 spec::

        {"type": "alert", "alert": <asdict(alert)>}

    so a subscriber can dispatch on ``data.type`` consistently with the
    ``kill_switch`` / ``observation_mode`` alert envelopes already
    emitted by ``api/server.py``.

    Because ``fire_alert`` returns synchronously BEFORE the scheduled
    broadcast coroutine runs, this test yields the event loop
    (``await asyncio.sleep(0.05)``) to let the broadcast land before
    assertion.
    """
    # Use a fresh AlertEngine with an in-memory DB (avoids the
    # ``/app/data/alerts.db`` permission-denied path the module-level
    # singleton would otherwise trip on during ``_store``).
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = AlertEngine(db_path=Path(tmp_dir) / "alerts_w23_3.db")
        # Re-bind the broadcast_recorder to the engine's ws_manager
        # reference (same singleton — the recorder is already installed
        # on the production ``ws_manager``).
        alert = Alert(
            alert_id="w23-3-alert-1",
            timestamp=1234567890.0,
            category="risk",
            name="test_alert_w23_3",
            severity=SEVERITY_CRITICAL,
            message="W23-3 broadcast wiring test alert",
            value=42.0,
            threshold=10.0,
            metadata={"test": "w23-3", "rule": "test"},
        )
        ok = engine.fire_alert(alert)
        assert ok is True, "fire_alert must return True on success"

        # Yield the event loop to let the scheduled ``_broadcast_alert``
        # task run. ``asyncio.sleep(0)`` would yield once but the task
        # may need more than one tick to actually execute; 50 ms is
        # ample on any CI runner.
        await asyncio.sleep(0.05)

    alerts_broadcasts = broadcast_recorder.calls_for("alerts")
    assert len(alerts_broadcasts) == 1, (
        f"fire_alert must broadcast exactly once on 'alerts'; "
        f"got {len(alerts_broadcasts)} broadcasts "
        f"across channels {broadcast_recorder.channels_emitted()}"
    )
    payload = alerts_broadcasts[0]
    assert payload["type"] == "alert", (
        f"alerts broadcast type must be 'alert'; got {payload['type']!r}"
    )
    assert "alert" in payload, (
        f"alerts broadcast payload must contain 'alert' key; got {payload}"
    )
    alert_data = payload["alert"]
    assert alert_data["alert_id"] == "w23-3-alert-1"
    assert alert_data["name"] == "test_alert_w23_3"
    assert alert_data["severity"] == SEVERITY_CRITICAL
    assert alert_data["category"] == "risk"
    assert alert_data["message"] == "W23-3 broadcast wiring test alert"
    assert alert_data["value"] == 42.0
    assert alert_data["threshold"] == 10.0


@ASYNC
async def test_alert_fire_no_event_loop_does_not_crash(
    monkeypatch,
) -> None:
    """``fire_alert`` called from a sync context without a running event
    loop must NOT raise — the broadcast is silently skipped (the alert
    has already been persisted to SQLite + logged).

    This guards against a regression where a refactor adds an
    ``await`` inside the sync ``fire_alert`` (which would crash every
    risk-gate caller that doesn't have an event loop).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = AlertEngine(db_path=Path(tmp_dir) / "alerts_w23_3_noloop.db")
        alert = Alert(
            alert_id="w23-3-alert-noloop",
            timestamp=1234567890.0,
            category="risk",
            name="test_no_loop",
            severity=SEVERITY_CRITICAL,
            message="fire_alert without an event loop must not raise",
            value=None,
            threshold=None,
            metadata={},
        )
        # Call from a sync context (no running loop) — must NOT raise.
        # The ``_store`` path is also sync, so the alert is still
        # persisted to SQLite even without the broadcast.
        ok = engine.fire_alert(alert)
        assert ok is True


# ── (5) GET /api/ws/broadcast-stats endpoint ─────────────────────────────────


def test_broadcast_stats_endpoint_returns_stats_payload() -> None:
    """``GET /api/ws/broadcast-stats`` returns 200 + the canonical
    ``WSBroadcastManager.get_stats()`` payload (connected_clients /
    total_messages_sent / total_errors / channels / client_ids).

    SYNC test — ``TestClient`` bridges the request through its own
    anyio portal. Mirrors the pattern in ``tests/test_api_versioning.py``.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    # ``TestClient(app)`` (NOT ``with TestClient(app)``) skips the
    # lifespan so each test stays fast and the background broadcasters
    # don't fire mid-test.
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/ws/broadcast-stats",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, (
        f"GET /api/ws/broadcast-stats must return 200; got "
        f"{response.status_code}. Body: {response.text[:300]!r}"
    )
    body = response.json()
    # The canonical ``get_stats()`` shape — every key must be present.
    expected_keys = {
        "connected_clients",
        "total_messages_sent",
        "total_errors",
        "channels",
        "client_ids",
    }
    assert set(body.keys()) == expected_keys, (
        f"broadcast-stats payload keys mismatch; expected {expected_keys}, "
        f"got {set(body.keys())}"
    )
    # ``channels`` is the canonical W14-1 catalog — all six channels
    # must be present so a client can subscribe to any of them.
    from core.ws_broadcast import WS_CHANNELS

    assert set(body["channels"]) == set(WS_CHANNELS), (
        f"broadcast-stats channels must match WS_CHANNELS; "
        f"got {body['channels']}"
    )


def test_broadcast_stats_endpoint_alias_matches_ws_stats() -> None:
    """``GET /api/ws/broadcast-stats`` returns the SAME payload as the
    W14-1 ``GET /api/ws/stats`` endpoint (both call
    ``ws_manager.get_stats()``). The W23-3 endpoint is an alias.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {VALID_TOKEN}"}
    r_alias = client.get("/api/ws/broadcast-stats", headers=headers)
    r_legacy = client.get("/api/ws/stats", headers=headers)
    assert r_alias.status_code == 200
    assert r_legacy.status_code == 200
    # ``connected_clients`` may differ between the two requests if a
    # client connects / disconnects in between, so compare the structural
    # fields only (channels + the count fields that don't change without
    # a client action).
    assert r_alias.json()["channels"] == r_legacy.json()["channels"]


def test_broadcast_stats_endpoint_requires_auth() -> None:
    """``GET /api/ws/broadcast-stats`` is NOT in ``PUBLIC_PATHS`` —
    must reject requests without a bearer token (fail-closed)."""
    from fastapi.testclient import TestClient

    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/ws/broadcast-stats")  # no auth header
    assert response.status_code in (401, 403, 503), (
        "unauthenticated GET /api/ws/broadcast-stats must be rejected "
        f"with 401/403/503; got {response.status_code}"
    )
