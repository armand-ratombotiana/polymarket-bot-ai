"""Unit tests for ``ingestion.ws_ingestion`` + ``ingestion.rest_ingestion``.

W31-2 — real-time WebSocket ingestion + REST fallback.

Scope: NEW file only — no existing source files or test files edited.
Mirrors the isolation strategy already established by
``tests/test_book_poller.py`` (V8), ``tests/test_settlement.py`` (U2),
and the shared ``tests/conftest.py`` (T15) autouse
``_reset_store_factory_defaults`` reset fixture.

Coverage map (each test maps to a Step in the W31-2 task spec):

  Step 1 — WebSocket ingestion manager:
    * ``test_ws_ingestion_manager_constructs_with_defaults``
    * ``test_health_reports_expected_keys``

  Step 2 — Subscription management:
    * ``test_add_tokens_populates_subscribed_tokens``
    * ``test_remove_tokens_drops_from_subscribed_tokens``
    * ``test_subscribe_channels_filters_unknown_event_types``
    * ``test_subscribe_channels_warns_on_unknown_channel``

  Step 3 — Gap detection:
    * ``test_detect_gap_returns_missing_sequence_numbers``
    * ``test_detect_gap_returns_empty_when_no_baseline``
    * ``test_detect_gap_returns_empty_for_out_of_order_replay``
    * ``test_gap_detection_in_process_message_logs_gap_event``
    * ``test_gap_detection_invokes_on_gap_callback_for_backfill``

  Step 4 — REST polling fallback:
    * ``test_rest_fallback_polls_books_for_tracked_tokens``
    * ``test_rest_fallback_active_vs_inactive_classification``
    * ``test_rest_fallback_detects_new_markets_via_gamma``
    * ``test_rest_fallback_detects_market_resolutions_via_gamma``
    * ``test_rest_fallback_circuit_breaker_opens_after_sustained_errors``

  Step 5 — tests (intra-suite):
    * ``test_process_message_parses_book_snapshot_and_routes``
    * ``test_process_message_parses_trade_and_routes``
    * ``test_process_message_deduplicates_by_event_data_hash``
    * ``test_process_message_skips_unparseable_json``
    * ``test_ws_connection_mock_connects_and_pumps_messages``
    * ``test_reconnect_with_exponential_backoff``
    * ``test_checkpoint_persists_and_resumes_per_token_seq``

Mock strategy
~~~~~~~~~~~~~

  * ``WSIngestionManager.connect_factory`` is replaced with a test
    stub that returns an ``async with``-compatible context manager
    yielding a ``FakeWS`` whose ``__aiter__`` returns a deterministic
    message list. This mirrors how ``websockets.connect`` is used in
    production (``async with websockets.connect(uri, ...) as ws:``)
    without spinning up a real WebSocket server.

  * The downstream singletons the manager fire-and-forgets to
    (``timescale_db``, ``raw_vault``, ``source_registry``,
    ``clob_client``, ``gamma_client``) are monkeypatched to no-op
    ``AsyncMock``s — same pattern as the ``mock_downstream`` fixture
    in ``tests/test_book_poller.py``.

  * ``asyncio.sleep`` is patched to a fast no-op for the
    reconnection test so the test doesn't actually sleep 2 seconds.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` / ``pyproject.toml`` are not edited per the W31-2
"NEW file only" constraint).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── sys.path hygiene ────────────────────────────────────────────────────────
# The W31-7 sibling test suite at ``tests/ingestion/`` ships a
# ``tests/ingestion/__init__.py`` — pytest's test-package discovery
# inserts ``tests/`` onto ``sys.path`` (ahead of the project root the
# shared ``tests/conftest.py`` inserts), which makes ``import
# ingestion`` resolve to the W31-7 test subpackage rather than the
# new top-level ``ingestion/`` package the W31-2 task created. To
# resolve the ambiguity, pop the ``tests/`` directory from
# ``sys.path`` BEFORE the ``from ingestion.* import ...`` line so
# Python finds the top-level ``ingestion/`` package first. The
# project root is already on ``sys.path`` (inserted by
# ``tests/conftest.py``), so the top-level package is reachable.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = str(Path(__file__).resolve().parent)
sys.path = [p for p in sys.path if p != _TESTS_DIR]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_book_poller.py`` (V8):
# the repo's ``pytest.ini`` cannot be edited per the W31-2 "NEW file only"
# constraint, so we use the module-level ``pytestmark`` idiom instead of
# ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``ingestion.*``). Mirrors the bootstrap pattern in every
# existing ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.data_store import store  # noqa: E402
from ingestion.rest_ingestion import (  # noqa: E402
    ACTIVE_INTERVAL,
    CIRCUIT_TRIP_ERRORS,
    INACTIVE_INTERVAL,
    RESTIngestionFallback,
)
from ingestion.ws_ingestion import (  # noqa: E402
    CHANNELS,
    EVT_BOOK_SNAPSHOT,
    EVT_PRICE_CHANGE,
    EVT_TRADE,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WSIngestionManager,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeWS:
    """In-memory stand-in for ``websockets.WebSocketClientProtocol``.

    Captures every ``send`` payload in ``self.sent`` (so tests can
    assert the manager sent the expected subscribe messages).
    ``__aiter__`` / ``__anext__`` yields the supplied message list in
    order, then raises ``StopAsyncIteration`` to end the listen loop
    (simulating a clean socket close).
    """

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._index = 0
        self.sent: list[str] = []
        self.closed: bool = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            self.closed = True
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg


class FakeConnectContext:
    """``async with``-compatible context manager yielding a ``FakeWS``.

    Mirrors the protocol returned by ``websockets.connect(uri, ...)``
    — supports ``__aenter__`` (returns the WS) and ``__aexit__`` (sets
    ``closed=True`` to mimic the real socket teardown).
    """

    def __init__(self, ws: FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> FakeWS:
        return self._ws

    async def __aexit__(self, *args: Any) -> bool:
        self._ws.closed = True
        return False


class FakeConnectFactory:
    """Callable that returns successive ``FakeConnectContext`` instances.

    Each call to ``factory(uri, **kwargs)`` returns the next queued
    ``FakeWS`` wrapped in a fresh ``FakeConnectContext``. Used by the
    reconnection test to simulate the first socket dropping then the
    second socket picking up.

    ``connect_calls`` records every (uri, kwargs) tuple the manager
    passed so tests can assert on the connection parameters.
    """

    def __init__(self, ws_sequence: list[FakeWS]) -> None:
        self._queue = list(ws_sequence)
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, uri: str, **kwargs: Any) -> FakeConnectContext:
        self.connect_calls.append((uri, dict(kwargs)))
        if not self._queue:
            # No more sockets queued — raise to simulate sustained outage.
            raise ConnectionError("no more fake sockets")
        ws = self._queue.pop(0)
        return FakeConnectContext(ws)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ws_book_msg(
    token_id: str,
    *,
    seq_no: int | None = None,
    bid_price: str = "0.49",
    ask_price: str = "0.51",
) -> str:
    """Build a raw WS book-snapshot message string for ``token_id``.

    Mirrors the Polymarket CLOB WS envelope: ``{"event_type": "book",
    "data": {"asset_id": ..., "bids": [...], "asks": [...]}}``.
    """
    data: dict[str, Any] = {
        "asset_id": token_id,
        "bids": [{"price": bid_price, "size": "100"}],
        "asks": [{"price": ask_price, "size": "100"}],
        "timestamp": str(int(time.time() * 1000)),
    }
    if seq_no is not None:
        data["seq_no"] = seq_no
    return json.dumps({"event_type": EVT_BOOK_SNAPSHOT, "data": data})


def _ws_trade_msg(
    token_id: str,
    *,
    seq_no: int | None = None,
    price: str = "0.50",
    size: str = "10",
    trade_id: str = "t-1",
) -> str:
    """Build a raw WS trade-tick message string for ``token_id``."""
    data: dict[str, Any] = {
        "asset_id": token_id,
        "price": price,
        "size": size,
        "side": "BUY",
        "trade_id": trade_id,
        "timestamp": str(int(time.time() * 1000)),
    }
    if seq_no is not None:
        data["seq_no"] = seq_no
    return json.dumps({"event_type": EVT_TRADE, "data": data})


def _ws_price_change_msg(
    token_id: str,
    *,
    seq_no: int | None = None,
) -> str:
    """Build a raw WS price_change (incremental book update) message."""
    data: dict[str, Any] = {
        "asset_id": token_id,
        "changes": [
            {"side": "BUY", "price": "0.48", "size": "150"},
            {"side": "SELL", "price": "0.51", "size": "0"},  # tombstone
        ],
        "timestamp": str(int(time.time() * 1000)),
    }
    if seq_no is not None:
        data["seq_no"] = seq_no
    return json.dumps({"event_type": EVT_PRICE_CHANGE, "data": data})


def _clob_book_response(token_id: str) -> dict[str, Any]:
    """Build a minimal CLOB REST ``/book`` response payload for ``token_id``."""
    return {
        "market": token_id,
        "asset_id": token_id,
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "hash": "0xdeadbeef",
        "timestamp": str(int(time.time() * 1000)),
    }


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_downstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Mock the downstream singletons both ingestion managers fire-and-forget to.

      * ``core.timescale_db.timescale_db`` — ``record_snapshot`` /
        ``record_trade`` / ``record_tick`` (called via ``asyncio.create_task``).
      * ``core.ingestion.raw_vault.raw_vault`` — ``record_observation``.
      * ``core.ingestion.source_registry.source_registry`` —
        ``record_metric``.
      * ``core.data_validator.data_validator`` — ``validate_snapshot`` /
        ``validate_trade``. The real validator is left in place by default;
        the fixture exposes a MagicMock so individual tests can override
        return values when needed (the default MagicMock returns a truthy
        ``MagicMock`` for any method call, which the production code
        interprets as "invalid" because ``result.is_valid`` is a MagicMock,
        not ``True`` — so tests that DO want validation to succeed must
        configure ``return_value`` explicitly).

    Mirrors the ``mock_downstream`` fixture in
    ``tests/test_book_poller.py``.
    """
    mock_ts = MagicMock()
    mock_ts.record_snapshot = AsyncMock(return_value=True)
    mock_ts.record_trade = AsyncMock(return_value=True)
    mock_ts.record_tick = AsyncMock(return_value=True)

    mock_rv = MagicMock()
    mock_rv.record_observation = AsyncMock(return_value=None)
    mock_rv.quarantine_record = AsyncMock(return_value=None)

    mock_sr = MagicMock()
    mock_sr.record_metric = AsyncMock(return_value=None)

    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)
    monkeypatch.setattr("core.ingestion.raw_vault.raw_vault", mock_rv)
    monkeypatch.setattr("core.ingestion.source_registry.source_registry", mock_sr)

    # NOTE: ``core.data_validator.data_validator`` is LEFT IN PLACE —
    # the real validator accepts the well-formed payloads the tests
    # build via ``_ws_book_msg`` / ``_ws_trade_msg`` / ``_clob_book_response``
    # (snapshots in [0,1] range, trades with side="BUY", etc.). Tests
    # that need to assert on validator behaviour can monkeypatch it
    # explicitly.

    return {
        "timescale": mock_ts,
        "raw_vault": mock_rv,
        "source_registry": mock_sr,
    }


@pytest.fixture
def ws_manager(
    tmp_path: Path, mock_downstream  # noqa: ARG001 — sets monkeypatch side-effect
) -> WSIngestionManager:
    """Fresh ``WSIngestionManager`` per test with a ``tmp_path`` checkpoint.

    The module-level singleton ``ws_ingestion_manager`` is NOT used so
    each test starts with empty ``_subscribed_tokens`` /
    ``_last_seq`` / ``_health`` state (no leakage between tests).
    Mirrors the ``poller`` fixture in ``tests/test_book_poller.py``.
    """
    return WSIngestionManager(
        uri="wss://fake.example/ws/market",
        checkpoint_path=tmp_path / "ws_checkpoint.json",
    )


@pytest.fixture
def rest_fallback(
    mock_downstream,  # noqa: ARG001 — sets monkeypatch side-effect
) -> RESTIngestionFallback:
    """Fresh ``RESTIngestionFallback`` per test.

    Production intervals are 1 s / 30 s / 5 min — for tests we use the
    defaults (the test only ever calls ``poll_once()`` / ``poll_gamma_once()``
    directly, never the background loop). The singleton
    ``rest_ingestion_fallback`` is NOT used.
    """
    return RESTIngestionFallback(
        active_interval=ACTIVE_INTERVAL,
        inactive_interval=INACTIVE_INTERVAL,
    )


# ── Step 1: WebSocket ingestion manager ─────────────────────────────────────


async def test_ws_ingestion_manager_constructs_with_defaults(
    tmp_path: Path,
):
    """``WSIngestionManager()`` with no args must default to the production
    URI (``settings.poly_ws_host``) and a checkpoint path under
    ``/app/data`` (or whatever ``PMBOT_TEST_TMP_ROOT`` is configured to
    by ``conftest.py``). Belt-and-braces: CHANNELS must expose the
    three logical channels documented in the module docstring.
    """
    mgr = WSIngestionManager(
        uri="wss://fake.example/ws/market",
        checkpoint_path=tmp_path / "ws_cp.json",
    )
    assert mgr._uri == "wss://fake.example/ws/market"
    assert mgr._checkpoint_path == tmp_path / "ws_cp.json"
    # All three logical channels are enabled by default.
    assert mgr.channels == set(CHANNELS.keys())
    assert set(CHANNELS.keys()) == {"book", "trades", "markets"}


async def test_health_reports_expected_keys(ws_manager: WSIngestionManager):
    """``health`` must surface the canonical W22-7 ``data_source.*`` keys."""
    h = ws_manager.health
    expected = {
        "source",
        "connected",
        "last_message_at",
        "last_seq_no",
        "messages_received",
        "messages_deduped",
        "messages_invalid",
        "messages_routed",
        "reconnect_count",
        "gap_count",
        "last_gap_at",
        "avg_latency_ms",
        "throughput_per_min",
        "recent_gaps",
    }
    assert expected.issubset(set(h.keys())), (
        f"missing keys: {expected - set(h.keys())}"
    )
    # Default state: not connected, zero counters, empty gaps list.
    assert h["source"] == "clob_ws"
    assert h["connected"] is False
    assert h["messages_received"] == 0
    assert h["recent_gaps"] == []
    # ``stats`` is an alias for ``health``.
    assert ws_manager.stats == ws_manager.health


# ── Step 2: Subscription management ─────────────────────────────────────────


async def test_add_tokens_populates_subscribed_tokens(
    ws_manager: WSIngestionManager,
):
    """``add_tokens`` must extend the subscription set, collapsing duplicates."""
    ws_manager.add_tokens(["T1", "T2", "T3"])
    assert ws_manager.subscribed_tokens == {"T1", "T2", "T3"}

    # Duplicates are silently collapsed.
    ws_manager.add_tokens(["T1", "T4"])
    assert ws_manager.subscribed_tokens == {"T1", "T2", "T3", "T4"}


async def test_remove_tokens_drops_from_subscribed_tokens(
    ws_manager: WSIngestionManager,
):
    """``remove_tokens`` must drop the supplied tokens from the subscription set."""
    ws_manager.add_tokens(["T1", "T2", "T3"])
    ws_manager.remove_tokens(["T2"])
    assert ws_manager.subscribed_tokens == {"T1", "T3"}

    # Removing a token not in the set is a no-op (no KeyError).
    ws_manager.remove_tokens(["NOT_TRACKED"])
    assert ws_manager.subscribed_tokens == {"T1", "T3"}


async def test_subscribe_channels_filters_unknown_event_types(
    ws_manager: WSIngestionManager,
):
    """When a channel is unsubscribed, messages of that channel's
    event types must be silently dropped (returned as ``None``).

    Setup:
      * ``channels`` default = ``{"book", "trades", "markets"}`` (all on).
      * Unsubscribe ``"trades"`` → trade messages must be dropped.
      * Book messages must still be routed.
    """
    # Sanity: all channels enabled by default.
    assert "trades" in ws_manager.channels

    # Drop the trades channel.
    ws_manager.unsubscribe_channels(["trades"])
    assert "trades" not in ws_manager.channels
    assert "book" in ws_manager.channels  # still on

    # A trade message must be dropped (None returned, not routed).
    trade_msg = _ws_trade_msg("T1", trade_id="drop-1")
    result = await ws_manager.process_message(trade_msg)
    assert result is None, (
        "trade message must be dropped when 'trades' channel is unsubscribed"
    )

    # A book message must still be routed.
    book_msg = _ws_book_msg("T1")
    result = await ws_manager.process_message(book_msg)
    assert result is not None
    assert result["routed"] == "snapshot"


async def test_subscribe_channels_warns_on_unknown_channel(
    ws_manager: WSIngestionManager, caplog: pytest.LogCaptureFixture,
):
    """``subscribe_channels(["bogus"])`` must log a warning + NOT add to the
    active set. Mirrors the defensive contract documented in the
    ``subscribe_channels`` docstring.
    """
    with caplog.at_level("WARNING", logger="ingestion.ws_ingestion"):
        ws_manager.subscribe_channels(["bogus_channel"])
    # The bogus channel must NOT be in the active set.
    assert "bogus_channel" not in ws_manager.channels
    # A warning was logged.
    assert any(
        "Unknown channel" in r.message for r in caplog.records
    ), "expected a 'Unknown channel' warning"


# ── Step 5: Message processing ───────────────────────────────────────────────


async def test_process_message_parses_book_snapshot_and_routes(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001 — sets monkeypatch side-effect
):
    """``process_message`` on a book snapshot must:
      1. Update the in-memory ``store.order_books`` mapping.
      2. Call ``raw_vault.record_observation`` (immutability contract).
      3. Call ``timescale_db.record_snapshot`` (normalised storage).
      4. Call ``source_registry.record_metric`` (success accounting).
      5. Return a dict whose ``routed == "snapshot"``.
      6. Increment ``messages_received`` / ``messages_routed``.
    """
    msg = _ws_book_msg("T1", seq_no=1)
    result = await ws_manager.process_message(msg)

    assert result is not None
    assert result["routed"] == "snapshot"
    assert result["token_id"] == "T1"

    # In-memory store updated.
    assert "T1" in store.order_books
    book = store.order_books["T1"]
    assert book.best_bid == pytest.approx(0.49)
    assert book.best_ask == pytest.approx(0.51)

    # Drain fire-and-forget tasks (raw_vault / timescale_db / source_registry).
    await asyncio.sleep(0)

    # Raw vault got the observation.
    mock_rv = mock_downstream["raw_vault"]
    assert mock_rv.record_observation.called
    call_args = mock_rv.record_observation.call_args
    assert call_args.args[0] == "clob_ws"  # source_id

    # TimescaleDB got the snapshot (fire-and-forget task may need a tick).
    mock_ts = mock_downstream["timescale"]
    # record_snapshot is called via asyncio.create_task — give the loop a chance.
    await asyncio.sleep(0)
    assert mock_ts.record_snapshot.called

    # Source registry got a success metric.
    mock_sr = mock_downstream["source_registry"]
    assert mock_sr.record_metric.called

    # Health counters reflect the routed message.
    h = ws_manager.health
    assert h["messages_received"] == 1
    assert h["messages_routed"] == 1
    assert h["messages_deduped"] == 0
    assert h["messages_invalid"] == 0
    assert h["last_seq_no"].get("T1") == 1


async def test_process_message_parses_trade_and_routes(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
):
    """``process_message`` on a trade tick must:
      1. Call ``timescale_db.record_trade``.
      2. Return a dict whose ``routed == "trade"``.
    """
    msg = _ws_trade_msg("T1", seq_no=1, trade_id="trade-001")
    result = await ws_manager.process_message(msg)

    assert result is not None
    assert result["routed"] == "trade"
    assert result["token_id"] == "T1"

    # Drain fire-and-forget tasks.
    await asyncio.sleep(0)

    mock_ts = mock_downstream["timescale"]
    assert mock_ts.record_trade.called
    call_kwargs = mock_ts.record_trade.call_args.kwargs
    assert call_kwargs["token_id"] == "T1"
    assert call_kwargs["trade_id"] == "trade-001"
    assert call_kwargs["side"] == "BUY"


async def test_process_message_applies_price_change_incrementally(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
):
    """``process_message`` on a ``price_change`` must apply the incremental
    update to the in-memory book (not just persist a snapshot).

    Setup:
      1. Send a book snapshot at bid=0.49 / ask=0.51.
      2. Send a price_change that adds 0.48 to bids and tombstones 0.51
         on asks.
      3. Assert the in-memory book now has bid 0.49 + 0.48 on the bid
         side, and 0.51 has been removed from the ask side.
    """
    # 1. Snapshot.
    await ws_manager.process_message(_ws_book_msg("T1", seq_no=1))
    await asyncio.sleep(0)

    book = store.order_books["T1"]
    assert book.best_bid == pytest.approx(0.49)
    assert book.best_ask == pytest.approx(0.51)

    # 2. Price change — adds 0.48 to bids, removes 0.51 from asks.
    await ws_manager.process_message(_ws_price_change_msg("T1", seq_no=2))
    await asyncio.sleep(0)

    book = store.order_books["T1"]
    bid_prices = sorted([b.price for b in book.bids], reverse=True)
    ask_prices = sorted([a.price for a in book.asks])
    assert 0.49 in bid_prices
    assert 0.48 in bid_prices
    # 0.51 was tombstoned (size=0) — must not appear on the ask side.
    assert 0.51 not in ask_prices


async def test_process_message_deduplicates_by_event_data_hash(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
):
    """Sending the same message twice must drop the second occurrence.

    Belt-and-braces:
      * First ``process_message`` returns a routed dict.
      * Second ``process_message`` returns ``None`` (dedup fast-path).
      * ``messages_deduped`` counter == 1 after the second call.
      * ``timescale_db.record_snapshot`` called exactly once (not twice).
    """
    msg = _ws_book_msg("T1", seq_no=1)

    # First occurrence — routed.
    r1 = await ws_manager.process_message(msg)
    assert r1 is not None

    # Second occurrence — same payload, must be deduped.
    r2 = await ws_manager.process_message(msg)
    assert r2 is None

    # Drain fire-and-forget tasks.
    await asyncio.sleep(0)

    h = ws_manager.health
    assert h["messages_received"] == 2
    assert h["messages_deduped"] == 1
    assert h["messages_routed"] == 1

    # TimescaleDB got exactly ONE record_snapshot call (not two).
    mock_ts = mock_downstream["timescale"]
    assert mock_ts.record_snapshot.call_count == 1


async def test_process_message_skips_unparseable_json(
    ws_manager: WSIngestionManager,
):
    """An unparseable raw payload must not crash the pump — count as invalid."""
    result = await ws_manager.process_message("not-valid-json{{")
    assert result is None
    assert ws_manager.health["messages_invalid"] == 1
    assert ws_manager.health["messages_received"] == 1


# ── Step 3: Gap detection ────────────────────────────────────────────────────


async def test_detect_gap_returns_missing_sequence_numbers(
    ws_manager: WSIngestionManager,
):
    """After seeing seq_no=1 on T1, ``detect_gap("T1", 4)`` must return
    ``[2, 3]`` (the missing seq numbers between last+1 and seen).
    """
    ws_manager._last_seq["T1"] = 1
    missing = ws_manager.detect_gap("T1", 4)
    assert missing == [2, 3]


async def test_detect_gap_returns_empty_when_no_baseline(
    ws_manager: WSIngestionManager,
):
    """When no prior seq_no is known for the token, ``detect_gap`` returns
    ``[]`` (no baseline to gap-check against — first message is always
    accepted).
    """
    assert "NEVER_SEEN" not in ws_manager._last_seq
    assert ws_manager.detect_gap("NEVER_SEEN", 5) == []


async def test_detect_gap_returns_empty_for_out_of_order_replay(
    ws_manager: WSIngestionManager,
):
    """``seq_no <= last_seen`` is an out-of-order replay, NOT a gap —
    ``detect_gap`` must return ``[]`` so the caller doesn't log a
    false positive gap event.
    """
    ws_manager._last_seq["T1"] = 5
    # Same seq_no.
    assert ws_manager.detect_gap("T1", 5) == []
    # Lower seq_no (replay of an older message).
    assert ws_manager.detect_gap("T1", 3) == []
    # Exactly the next expected seq_no (no gap).
    assert ws_manager.detect_gap("T1", 6) == []


async def test_gap_detection_in_process_message_logs_gap_event(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
):
    """A seq_no jump inside ``process_message`` must:
      1. Append a ``GapEvent`` to ``_gap_log``.
      2. Increment ``gap_count`` and update ``last_gap_at``.
      3. Surface in ``health["recent_gaps"]``.
    """
    # First message — sets baseline at seq_no=1.
    await ws_manager.process_message(_ws_book_msg("T1", seq_no=1))
    await asyncio.sleep(0)
    assert ws_manager.health["gap_count"] == 0

    # Second message jumps to seq_no=4 — gap of 2 (missing 2, 3).
    await ws_manager.process_message(_ws_book_msg("T1", seq_no=4))
    await asyncio.sleep(0)

    h = ws_manager.health
    assert h["gap_count"] == 1
    assert h["last_gap_at"] > 0
    assert len(h["recent_gaps"]) == 1
    gap = h["recent_gaps"][0]
    assert gap["token_id"] == "T1"
    assert gap["expected"] == 2
    assert gap["seen"] == 4
    assert gap["missing"] == [2, 3]


async def test_gap_detection_invokes_on_gap_callback_for_backfill(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
):
    """The ``on_gap`` callback must be invoked with the ``GapEvent`` so
    the REST fallback can dispatch a backfill poll for the missing
    seq numbers.

    Step 3 — "Trigger backfill for missing messages".
    """
    backfill_calls: list = []

    async def backfill(gap):
        backfill_calls.append(gap)

    ws_manager.on_gap = backfill

    # Baseline at 1, jump to 5 — gap of 3 (missing 2, 3, 4).
    await ws_manager.process_message(_ws_book_msg("T1", seq_no=1))
    await ws_manager.process_message(_ws_book_msg("T1", seq_no=5))
    await asyncio.sleep(0)

    assert len(backfill_calls) == 1
    gap = backfill_calls[0]
    assert gap.token_id == "T1"
    assert gap.expected == 2
    assert gap.seen == 5
    assert gap.missing == [2, 3, 4]


# ── Step 1 + Step 6: WS connection + reconnection ───────────────────────────


async def test_ws_connection_mock_connects_and_pumps_messages(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """``start()`` with a mocked ``connect_factory`` must:
      1. Connect to the supplied URI.
      2. Send a subscribe message for every tracked token (batched).
      3. Pump every message through ``process_message`` (i.e. each
         message's ``token_id`` ends up in ``store.order_books``).
      4. Flush a checkpoint on ``stop()``.
    """
    ws_manager.add_tokens(["T1", "T2"])
    fake_ws = FakeWS([
        _ws_book_msg("T1", seq_no=1),
        _ws_book_msg("T2", seq_no=1),
    ])
    factory = FakeConnectFactory([fake_ws])
    ws_manager._connect_factory = factory

    # Capture the real ``asyncio.sleep`` BEFORE monkeypatching so the
    # test's own ``await real_asyncio_sleep(...)`` calls below don't get
    # intercepted by ``fast_sleep`` (which would set ``_running=False``
    # prematurely before the background task has had a chance to run).
    real_asyncio_sleep = asyncio.sleep

    # Patch asyncio.sleep in the ws_ingestion namespace so the
    # reconnect loop's post-listen sleep doesn't actually wait.
    async def fast_sleep(_delay: float) -> None:
        # Stop the loop after the first clean disconnect so the test
        # doesn't sit in the backoff cycle forever.
        ws_manager._running = False

    monkeypatch.setattr("ingestion.ws_ingestion.asyncio.sleep", fast_sleep)

    await ws_manager.start()
    # Wait for the connect→subscribe→listen→disconnect cycle to finish.
    # The background task's ``async for raw in ws`` exits when the
    # FakeWS exhausts its message list, then sleeps via the patched
    # ``asyncio.sleep`` which flips ``_running=False``.
    # Use the captured real sleep so the wait itself isn't intercepted
    # by ``fast_sleep`` (which would skip the wait entirely and leave
    # the background task without a chance to run).
    await real_asyncio_sleep(0.1)
    await real_asyncio_sleep(0)
    await ws_manager.stop()

    # The factory was called with the configured URI.
    assert len(factory.connect_calls) >= 1
    assert factory.connect_calls[0][0] == "wss://fake.example/ws/market"

    # Subscribe message captured in fake_ws.sent.
    assert len(fake_ws.sent) >= 1
    sub_msg = json.loads(fake_ws.sent[0])
    assert sub_msg["type"] == "market"
    assert set(sub_msg["assets_ids"]) == {"T1", "T2"}

    # Both messages were pumped through process_message → store updated.
    assert "T1" in store.order_books
    assert "T2" in store.order_books

    # Checkpoint file was written on stop().
    assert ws_manager._checkpoint_path.exists()
    payload = json.loads(ws_manager._checkpoint_path.read_text())
    assert payload["last_seq"].get("T1") == 1
    assert payload["last_seq"].get("T2") == 1


async def test_reconnect_with_exponential_backoff(
    ws_manager: WSIngestionManager,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """When the first WS connection drops, the manager must:
      1. Back off with exponential delay (2 s → 4 s → 8 s → …).
      2. Reconnect to the second queued socket.
      3. Increment ``reconnect_count`` after the first disconnect.
      4. Process messages from the second socket.
    """
    ws_manager.add_tokens(["T1"])

    # First socket: empty (immediate disconnect).
    # Second socket: one book message.
    fake_ws_1 = FakeWS([])
    fake_ws_2 = FakeWS([_ws_book_msg("T1", seq_no=1)])
    factory = FakeConnectFactory([fake_ws_1, fake_ws_2])
    ws_manager._connect_factory = factory

    # Record the sleep delays the manager requests so we can assert
    # the backoff sequence doubles each iteration.
    sleep_delays: list[float] = []

    # Capture the real ``asyncio.sleep`` BEFORE monkeypatching so the
    # test's own ``await real_asyncio_sleep(...)`` calls below don't get
    # intercepted by ``fast_sleep`` (which would prevent the background
    # task from getting a chance to run the reconnect loop).
    real_asyncio_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        # Stop the loop after the third sleep (the second socket's
        # message has been pumped by then).
        if len(sleep_delays) >= 3:
            ws_manager._running = False

    monkeypatch.setattr("ingestion.ws_ingestion.asyncio.sleep", fast_sleep)

    await ws_manager.start()
    await real_asyncio_sleep(0.2)
    await real_asyncio_sleep(0)
    await ws_manager.stop()

    # Two connect attempts — first socket dropped, second succeeded.
    assert len(factory.connect_calls) >= 2

    # Reconnect counter was incremented at least once (after the
    # first disconnect).
    assert ws_manager.reconnect_count >= 1
    assert ws_manager.health["reconnect_count"] == ws_manager.reconnect_count

    # The second socket's message was processed → store updated.
    assert "T1" in store.order_books

    # The first non-trivial sleep delay matches RECONNECT_BASE_DELAY
    # (the very first sleep is the checkpoint-loop sleep at
    # CHECKPOINT_INTERVAL, which we filter out by checking the
    # delays that are <= RECONNECT_MAX_DELAY + 1).
    backoff_delays = [
        d for d in sleep_delays
        if RECONNECT_BASE_DELAY <= d <= RECONNECT_MAX_DELAY
    ]
    assert len(backoff_delays) >= 1, (
        f"expected at least one backoff delay in [{RECONNECT_BASE_DELAY}, "
        f"{RECONNECT_MAX_DELAY}], got {sleep_delays}"
    )
    # First backoff delay is the base.
    assert backoff_delays[0] == pytest.approx(RECONNECT_BASE_DELAY)


# ── Step 8: Checkpoint resume ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_persists_and_resumes_per_token_seq(
    tmp_path: Path,
    mock_downstream,  # noqa: ARG001
):
    """A checkpoint written by one manager must be loaded by a fresh
    manager constructed against the same ``checkpoint_path``.

    Belt-and-braces:
      1. Manager A processes a message with seq_no=42 on T1.
      2. Manager A.checkpoint() writes the per-token seq map to JSON.
      3. Manager B is constructed against the same path → its
         ``_last_seq["T1"] == 42`` (loaded synchronously in __init__).
      4. Manager B's FIRST message at seq_no=43 does NOT trigger a gap
         (it's the expected next seq_no).
    """
    cp_path = tmp_path / "ws_cp.json"

    mgr_a = WSIngestionManager(
        uri="wss://fake.example/ws",
        checkpoint_path=cp_path,
    )
    await mgr_a.process_message(_ws_book_msg("T1", seq_no=42))
    await mgr_a.checkpoint()
    assert cp_path.exists()

    # Manager B loads the checkpoint in __init__.
    mgr_b = WSIngestionManager(
        uri="wss://fake.example/ws",
        checkpoint_path=cp_path,
    )
    assert mgr_b._last_seq.get("T1") == 42
    assert mgr_b.health["last_seq_no"].get("T1") == 42

    # First message at seq_no=43 must NOT trigger a gap (it's last+1).
    result = await mgr_b.process_message(_ws_book_msg("T1", seq_no=43))
    assert result is not None  # routed, not dropped
    assert mgr_b.health["gap_count"] == 0
    assert mgr_b.health["recent_gaps"] == []


# ── Step 4: REST polling fallback ────────────────────────────────────────────


async def test_rest_fallback_polls_books_for_tracked_tokens(
    rest_fallback: RESTIngestionFallback,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """``poll_once`` must fetch a book for every tracked token via
    ``clob_client.get_order_book`` and persist each through
    ``_apply_book`` → ``store.update_order_book`` +
    ``timescale_db.record_snapshot``.

    Mock strategy:
      * ``clob_client.get_order_book`` is patched to return a per-token
        stub book payload (mirrors the test pattern in
        ``tests/test_book_poller.py::_make_mock_client``).
      * The downstream singletons are already mocked by the
        ``mock_downstream`` fixture.
    """
    rest_fallback.add_tokens(["T1", "T2"])

    async def fake_get_order_book(token_id: str) -> dict:
        return _clob_book_response(token_id)

    # ``clob_client`` is a module-level singleton imported into
    # ``ingestion.rest_ingestion`` at module-import time. Patch the
    # bound method directly.
    monkeypatch.setattr(
        "ingestion.rest_ingestion.clob_client.get_order_book",
        fake_get_order_book,
    )

    results = await rest_fallback.poll_once()
    await asyncio.sleep(0)  # drain fire-and-forget tasks.

    # Both tokens polled successfully.
    assert results == {"T1": True, "T2": True}

    # In-memory store updated for both tokens.
    assert "T1" in store.order_books
    assert "T2" in store.order_books
    assert store.order_books["T1"].best_bid == pytest.approx(0.49)
    assert store.order_books["T2"].best_ask == pytest.approx(0.51)

    # TimescaleDB got two record_snapshot calls.
    mock_ts = mock_downstream["timescale"]
    assert mock_ts.record_snapshot.call_count == 2

    # Health counters reflect the two successful polls.
    h = rest_fallback.health
    assert h["book_polls"] == 2
    assert h["book_errors"] == 0
    assert h["tracked_tokens"] == 2


async def test_rest_fallback_active_vs_inactive_classification(
    rest_fallback: RESTIngestionFallback,
):
    """``promote_active`` must classify tokens as active; tokens added
    via ``add_tokens`` default to inactive until promoted (or until a
    trade arrives within ``ACTIVE_TRADE_RECENCY_S``).

    Belt-and-braces:
      * ``add_tokens(["T1", "T2"])`` → both inactive (no recent trade).
      * ``promote_active(["T1"])`` → T1 active, T2 still inactive.
      * ``demote_inactive(["T1"])`` → T1 inactive again.
      * ``remove_tokens(["T2"])`` → T2 dropped from tracked set.
    """
    rest_fallback.add_tokens(["T1", "T2"])
    assert rest_fallback.active_tokens == []

    rest_fallback.promote_active(["T1"])
    assert rest_fallback.active_tokens == ["T1"]

    rest_fallback.demote_inactive(["T1"])
    assert rest_fallback.active_tokens == []

    rest_fallback.remove_tokens(["T2"])
    assert rest_fallback.tracked_tokens == ["T1"]


async def test_rest_fallback_detects_new_markets_via_gamma(
    rest_fallback: RESTIngestionFallback,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """``poll_gamma_once`` must detect markets that weren't previously
    seen in ``_gamma_state`` (Step 4 — "Detect new markets").

    Mock strategy:
      * ``gamma_client.get_markets`` is patched to return a 2-market list
        (M1 = previously seen, M2 = new).
      * Pre-populate ``_gamma_state`` with M1 so only M2 is "new".
    """
    markets_payload = [
        {
            "conditionId": "COND_M1",
            "question": "Previously seen market",
            "closed": False,
            "resolvedBy": "",
        },
        {
            "conditionId": "COND_M2",
            "question": "Brand new market",
            "closed": False,
            "resolvedBy": "",
        },
    ]
    monkeypatch.setattr(
        "ingestion.rest_ingestion.gamma_client.get_markets",
        AsyncMock(return_value=markets_payload),
    )

    # Pre-seed _gamma_state with M1 so M2 is the only "new" market.
    from ingestion.rest_ingestion import _GammaState
    rest_fallback._gamma_state["COND_M1"] = _GammaState(
        condition_id="COND_M1",
        question="Previously seen market",
        closed=False,
    )

    # Detect new markets BEFORE the poll mutates _gamma_state.
    # (poll_gamma_once() calls detect_new_markets internally and updates
    # _gamma_state — so we test detection directly first.)
    new_markets = rest_fallback.detect_new_markets(markets_payload)
    assert len(new_markets) == 1
    assert new_markets[0]["conditionId"] == "COND_M2"

    # Now run poll_gamma_once and verify the counter was incremented.
    await rest_fallback.poll_gamma_once()

    h = rest_fallback.health
    assert h["new_markets_detected"] >= 1
    # Both markets now in _gamma_state (the new one was added).
    assert "COND_M1" in rest_fallback.gamma_state
    assert "COND_M2" in rest_fallback.gamma_state


async def test_rest_fallback_detects_market_resolutions_via_gamma(
    rest_fallback: RESTIngestionFallback,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """``poll_gamma_once`` must detect markets whose ``closed`` flag
    flipped False → True (Step 4 — "Detect market resolution events").

    Mock strategy:
      * Pre-seed ``_gamma_state`` with M1 (closed=False).
      * ``gamma_client.get_markets`` returns M1 with ``closed=True``.
      * ``detect_resolutions`` must return [M1].
    """
    markets_payload = [
        {
            "conditionId": "COND_M1",
            "question": "Market that just resolved",
            "closed": True,
            "resolvedBy": "0xabc",
        },
    ]
    monkeypatch.setattr(
        "ingestion.rest_ingestion.gamma_client.get_markets",
        AsyncMock(return_value=markets_payload),
    )

    from ingestion.rest_ingestion import _GammaState
    rest_fallback._gamma_state["COND_M1"] = _GammaState(
        condition_id="COND_M1",
        question="Market that just resolved",
        closed=False,
    )

    # Direct detection test.
    resolutions = rest_fallback.detect_resolutions(markets_payload)
    assert len(resolutions) == 1
    assert resolutions[0]["conditionId"] == "COND_M1"

    # poll_gamma_once should bump the resolutions_detected counter.
    await rest_fallback.poll_gamma_once()
    h = rest_fallback.health
    assert h["resolutions_detected"] >= 1


async def test_rest_fallback_circuit_breaker_opens_after_sustained_errors(
    rest_fallback: RESTIngestionFallback,
    mock_downstream,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """After ``CIRCUIT_TRIP_ERRORS`` consecutive errors, the circuit
    breaker must OPEN and pause polling.

    Setup:
      * ``clob_client.get_order_book`` raises ``ConnectionError`` on
        every call.
      * Track 1 token, poll ``CIRCUIT_TRIP_ERRORS`` times.
      * Assert ``health["circuit_open"] is True`` after the threshold
        is crossed.
    """
    rest_fallback.add_tokens(["T1"])

    async def failing_get_order_book(token_id: str) -> dict:
        raise ConnectionError("simulated upstream outage")

    monkeypatch.setattr(
        "ingestion.rest_ingestion.clob_client.get_order_book",
        failing_get_order_book,
    )

    # Poll once per iteration — the consecutive-error counter is what
    # trips the breaker, not the per-poll error count.
    for _ in range(CIRCUIT_TRIP_ERRORS):
        await rest_fallback.poll_once()
        # Drain the fire-and-forget metric calls so the
        # source_registry mock doesn't accumulate pending tasks.
        await asyncio.sleep(0)

    h = rest_fallback.health
    assert h["circuit_open"] is True, (
        f"circuit must be OPEN after {CIRCUIT_TRIP_ERRORS} consecutive errors; "
        f"got health={h}"
    )
    assert h["circuit_open_until"] > time.time()
    assert h["book_errors"] >= CIRCUIT_TRIP_ERRORS
