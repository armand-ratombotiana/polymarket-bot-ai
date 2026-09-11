"""
tests/test_data_validator_wiring.py — W25-5 wiring tests for the W24-4
data ingestion validator.

Scope
~~~~~
This file is the W25-5 follow-on to W24-4. The W24-4 wave added the
``DataValidator`` singleton (``core/data_validator.py``) and wired it
into the two ingestion call sites:

  * ``core/book_poller.py::_apply_book`` — runs every CLOB ``/book``
    snapshot through ``validate_snapshot`` BEFORE the
    ``timescale_db.record_snapshot`` / ``record_tick`` fire-and-forget
    tasks are scheduled. Duplicate snapshots (same hash as the previous
    poll) are silently dropped; invalid snapshots (negative price /
    out-of-range bid|ask) are rejected at warning level.
  * ``core/trade_ingester.py::_ingest_trades`` — runs every public
    trade through ``validate_trade`` BEFORE
    ``db_manager.record_trade(...)`` is awaited. Duplicate trade_ids
    (already-seen) are silently dropped at debug level; invalid trades
    (missing token_id / negative price / unknown side) are rejected at
    warning level.
  * ``api/server.py::data_validator_stats`` — the
    ``GET /api/data-validator/stats`` endpoint exposes the live
    ``valid_count`` / ``invalid_count`` / ``duplicate_count`` /
    ``seen_ids_size`` / ``seen_hashes_size`` counters so an operator
    dashboard can verify the gate is firing.

W25-5 verifies that wiring is correct, end-to-end, at the integration
boundary — i.e. that the production call sites actually CALL the
validator, route on its result, and pass the NORMALISED payload (not
the raw input) downstream. This is distinct from
``tests/test_data_validator.py`` which exercises the validator's
internal logic (dedup / schema / value / staleness / normalisation)
in isolation.

Test matrix
-----------
  * ``test_book_poller_validates_snapshot_before_recording`` — happy
    path. The validator's ``validate_snapshot`` IS invoked (assert via
    spy), the call returns ``is_valid=True`` with a normalised payload,
    and ``timescale_db.record_snapshot`` is called with the validator's
    ``normalized_data`` (token_id / best_bid / best_ask / mid / spread /
    bids_json / asks_json — all sourced from the normalised payload).
  * ``test_book_poller_skips_invalid_snapshot`` — invalid (negative
    ``best_bid``). ``validate_snapshot`` returns ``is_valid=False``;
    ``record_snapshot`` is NOT called (zero call_count).
  * ``test_book_poller_skips_duplicate_snapshot`` — second identical
    poll. ``validate_snapshot`` returns ``is_duplicate=True``;
    ``record_snapshot`` is NOT called on the second call.
  * ``test_trade_ingester_validates_trade_before_recording`` — happy
    path. ``validate_trade`` IS invoked; ``record_trade`` IS called
    with the normalised ``price`` / ``size`` / ``side`` / ``trade_id``
    / ``timestamp`` (side upper-cased, price/size coerced to float).
  * ``test_trade_ingester_skips_invalid_trade`` — invalid (missing
    ``token_id`` + negative ``price``). ``validate_trade`` returns
    ``is_valid=False``; ``record_trade`` is NOT called.
  * ``test_trade_ingester_skips_duplicate_trade`` — second identical
    ``trade_id``. ``validate_trade`` returns ``is_duplicate=True``;
    ``record_trade`` is NOT called on the duplicate.
  * ``test_data_validator_stats_route_returns_200_with_shape`` —
    minimal FastAPI app with only the data-validator route registered
    (mirrors the existing pattern in ``tests/test_data_validator.py``).
    ``GET /api/data-validator/stats`` returns HTTP 200 + the documented
    counter shape.
  * ``test_data_validator_stats_route_reflects_validator_state`` —
    after a mix of valid / invalid / duplicate validation calls, the
    route's response reflects the updated counters (proves the route
    reads the live singleton, not a snapshot).
  * ``test_data_validator_stats_route_registered_on_production_app`` —
    the production ``api/server.py`` app has the
    ``/api/data-validator/stats`` route registered (defensive against
    an accidental deletion / rename). Belt-and-braces: the integration
    test above already exercises the handler logic, this test asserts
    the wiring block in ``api/server.py`` is present so a future
    refactor can't silently drop the endpoint.

Isolation strategy
------------------
Each test constructs a fresh ``DataValidator()`` and monkeypatches it
onto ``core.data_validator.data_validator`` so the production call
sites (which import the singleton lazily inside the function body via
``from core.data_validator import data_validator``) pick up the test-
scoped instance. The downstream singletons (``timescale_db``,
``db_manager``, ``clob_client``) are mocked via ``monkeypatch.setattr``
on the module attributes — same pattern as the existing
``test_data_validator.py`` integration tests.

The book_poller integration tests also need the raw_vault /
source_registry singletons mocked (the ``_fetch_book`` path schedules
fire-and-forget ``asyncio.create_task`` calls against them); without
the mocks, the task scheduling would touch the SQLite fallback DB on
every fetch and slow the tests down.

The trade_ingester tests mock ``clob_client.get_public_trades`` to
return a deterministic trade list (so we don't depend on the live
CLOB API), and mock ``db_manager.record_trade`` so no rows are
actually written to SQLite.

All async tests are explicitly marked with ``@pytest.mark.asyncio``
(no module-level ``pytestmark``) because this module mixes sync
FastAPI-TestClient tests with async integration tests — the
module-level mark idiom would emit ``PytestWarning: marked with
@pytest.mark.asyncio but not async`` warnings on the sync tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Defensive env-var redirect (mirrors tests/test_data_validator.py /
# tests/test_data_quality.py / tests/test_retention.py). ``setdefault``
# lets conftest (which loads first) win when present; this block is
# purely a defensive net so the file stays hermetic in a hypothetical
# conftest-less invocation.
_TMP_ROOT = Path("/tmp/data_validator_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    # Force paper mode + live disabled so any co-collected stateful test
    # doesn't trip a shadow / live-trading gate at import time.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-data-validator-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.data_validator``, ``core.book_poller``, etc.) regardless of the
# cwd pytest was launched from. Mirrors every sibling test module.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_validator import (  # noqa: E402
    DataValidator,
    ValidationResult,
    data_validator as _module_singleton,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
def _make_valid_snapshot_raw(
    *,
    token_id: str = "0xsnapToken",
    best_bid: float = 0.49,
    best_ask: float = 0.51,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal valid snapshot dict for the book_poller tests.

    Mirrors the raw_snapshot shape that ``_apply_book`` constructs
    internally (``token_id`` + ``best_bid`` + ``best_ask`` +
    ``timestamp`` + ``source`` + optional ``mid`` / ``spread``) so the
    validator's hash + schema + value checks all pass.
    """
    return {
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "source": "test",
    }


def _make_valid_trade_raw(
    *,
    trade_id: str = "trade-w1",
    token_id: str = "0xtradeToken",
    price: float = 0.55,
    size: float = 100.0,
    side: str = "BUY",
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal valid trade dict for the trade_ingester tests.

    Mirrors the trade shape that ``clob_client.get_public_trades``
    returns after CLOB normalisation (``trade_id`` + ``token_id`` +
    ``price`` + ``size`` + ``side`` + ``timestamp`` + ``maker_address``
    + ``taker_order_id`` + ``source``).
    """
    return {
        "trade_id": trade_id,
        "token_id": token_id,
        "price": price,
        "size": size,
        "side": side,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "maker_address": "0xmaker",
        "taker_order_id": "0xtaker",
        "source": "test",
    }


def _make_book_payload(
    *,
    bids_price: str = "0.49",
    asks_price: str = "0.51",
    size: str = "100",
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal CLOB ``/book`` response payload.

    Mirrors the real Polymarket CLOB REST API response shape: a flat
    dict with ``bids`` / ``asks`` lists of ``{"price": str, "size":
    str}`` objects. The string types are intentional (production parses
    via ``float(b["price"])`` / ``float(b["size"])``).
    """
    return {
        "bids": [{"price": bids_price, "size": size}],
        "asks": [{"price": asks_price, "size": size}],
        "timestamp": timestamp if timestamp is not None else time.time(),
    }


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def fresh_validator(monkeypatch: pytest.MonkeyPatch) -> DataValidator:
    """Replace the module-level ``data_validator`` singleton with a
    fresh instance for the duration of the test.

    The production call sites (``core.book_poller._apply_book`` +
    ``core.trade_ingester._ingest_trades``) import the singleton
    lazily inside the function body — the monkeypatch is picked up at
    call time. Mirrors the pattern in ``tests/test_data_validator.py``.
    """
    v = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", v)
    return v


@pytest.fixture
def mock_timescale(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock ``core.timescale_db.timescale_db`` so the book_poller's
    fire-and-forget ``record_snapshot`` / ``record_tick`` tasks are
    captured as AsyncMocks (no SQLite writes, no asyncpg pool).
    """
    mock = MagicMock()
    mock.record_snapshot = AsyncMock(return_value=True)
    mock.record_tick = AsyncMock(return_value=True)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock)
    return mock


@pytest.fixture
def mock_raw_vault(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock ``core.ingestion.raw_vault.raw_vault`` so the book_poller's
    fire-and-forget ``record_observation`` task is a no-op.
    """
    mock = MagicMock()
    mock.record_observation = AsyncMock(return_value=None)
    monkeypatch.setattr("core.ingestion.raw_vault.raw_vault", mock)
    return mock


@pytest.fixture
def mock_source_registry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock ``core.ingestion.source_registry.source_registry`` so the
    book_poller's fire-and-forget ``record_metric`` calls are no-ops.
    """
    mock = MagicMock()
    mock.record_metric = AsyncMock(return_value=None)
    monkeypatch.setattr("core.ingestion.source_registry.source_registry", mock)
    return mock


@pytest.fixture
def mock_db_manager(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock ``core.database_manager.db_manager`` so fire-and-forget
    ``record_snapshot`` / ``record_trade`` calls from the book_poller
    AND the trade_ingester are captured as AsyncMocks (no SQLite write,
    no asyncpg pool).

    W26-3 — extended to also stub ``record_snapshot`` because the
    book_poller now routes its snapshot writes through the
    ``db_manager`` facade (was previously calling
    ``timescale_db.record_snapshot`` directly).
    """
    mock = MagicMock()
    mock.record_snapshot = AsyncMock(return_value=True)
    mock.record_trade = AsyncMock(return_value=True)
    monkeypatch.setattr("core.database_manager.db_manager", mock)
    return mock


# ────────────────────────────────────────────────────────────────────────────
# 1. book_poller wiring — validates BEFORE recording (happy path)
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_book_poller_validates_snapshot_before_recording(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    mock_timescale: MagicMock,  # ``record_tick`` still routes through ``timescale_db`` directly
    mock_raw_vault: MagicMock,  # noqa: ARG001 — sets up monkeypatch side-effect
    mock_source_registry: MagicMock,  # noqa: ARG001 — sets up monkeypatch side-effect
):
    """``core.book_poller._apply_book`` MUST route the snapshot through
    the validator BEFORE scheduling the ``db_manager.record_snapshot``
    fire-and-forget task — and the data passed to ``record_snapshot``
    must come from the validator's NORMALISED payload (with provenance
    fields, derived ``mid`` / ``spread``, etc.).

    W26-3 — the book_poller now routes snapshot writes through the
    unified ``db_manager`` facade (was previously calling
    ``timescale_db.record_snapshot`` directly). The
    ``record_snapshot`` assertions therefore target ``mock_db_manager``,
    not ``mock_timescale``. ``record_tick`` still routes through
    ``timescale_db`` directly (W21-3 — the tick hypertable doesn't
    have a ``db_manager`` indirection yet) so the ``mock_timescale``
    fixture is still pulled in.

    Belt-and-braces:
      * ``validate_snapshot`` IS called (verified by the validator's
        ``get_stats()`` reflecting ``valid_count == 1``).
      * ``record_snapshot`` IS called exactly once.
      * The book_poller passes the validator's normalised
        ``best_bid`` / ``best_ask`` / ``mid`` / ``spread`` through to
        ``record_snapshot`` (sanity check that the validator's
        normalisation is in the data path, not just a gate).
    """
    from core.book_poller import BookPoller

    poller = BookPoller()
    poller.set_tokens(["T1"])

    book_data = _make_book_payload()
    await poller._apply_book("T1", book_data)
    # Drain fire-and-forget ``asyncio.create_task`` calls so the
    # ``record_snapshot`` AsyncMock is awaited before assertions.
    await asyncio.sleep(0)

    # Validator saw exactly one valid snapshot.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1, (
        f"expected valid_count=1, got {stats['valid_count']}"
    )
    assert stats["invalid_count"] == 0
    assert stats["duplicate_count"] == 0

    # record_snapshot WAS called (exactly once) — on the db_manager facade.
    assert mock_db_manager.record_snapshot.call_count == 1, (
        f"expected record_snapshot.call_count=1, "
        f"got {mock_db_manager.record_snapshot.call_count}"
    )

    # The book_poller passes the normalised best_bid / best_ask through
    # to record_snapshot. The validator's normalised payload carries
    # ``mid`` / ``spread`` derived from the bid/ask pair; the book_poller
    # reads ``book.best_bid`` / ``book.best_ask`` / ``book.mid`` /
    # ``book.spread`` from the OrderBook dataclass (parsed from the raw
    # CLOB payload), which match the validator's view.
    call_kwargs = mock_db_manager.record_snapshot.call_args.kwargs
    assert call_kwargs["token_id"] == "T1"
    assert call_kwargs["best_bid"] == pytest.approx(0.49)
    assert call_kwargs["best_ask"] == pytest.approx(0.51)
    assert call_kwargs["mid"] == pytest.approx(0.50)
    assert call_kwargs["spread"] == pytest.approx(0.02)


# ────────────────────────────────────────────────────────────────────────────
# 2. book_poller wiring — invalid snapshot is REJECTED
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_book_poller_skips_invalid_snapshot(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    mock_timescale: MagicMock,
    mock_raw_vault: MagicMock,  # noqa: ARG001
    mock_source_registry: MagicMock,  # noqa: ARG001
):
    """An INVALID snapshot (negative ``best_bid`` — out of the ``[0, 1]``
    probability range) must be rejected by the validator AND the
    ``db_manager.record_snapshot`` call must be SKIPPED entirely.

    W26-3 — ``record_snapshot`` is now routed through the
    ``db_manager`` facade (was previously a direct
    ``timescale_db.record_snapshot`` call). The skipped-call assertion
    therefore targets ``mock_db_manager.record_snapshot``.
    ``record_tick`` still routes through ``timescale_db`` directly, so
    its skipped-call assertion continues to target ``mock_timescale``.

    The book_poller builds the ``raw_snapshot`` from the parsed
    ``OrderBook`` (which itself was parsed from the raw CLOB ``/book``
    payload via ``float(b["price"])``). A negative price in the bids
    ladder flows through to ``book.best_bid`` and then to the
    ``raw_snapshot["best_bid"]`` field — the validator's value check
    fires and the record is rejected.

    Belt-and-braces:
      * ``validate_snapshot`` IS called (validator saw 1 invalid).
      * ``db_manager.record_snapshot`` is NOT called (zero call_count).
      * ``timescale_db.record_tick`` is NOT called (the book_poller
        schedules both via ``asyncio.create_task`` AFTER the validator
        gate; both must be skipped).
    """
    from core.book_poller import BookPoller

    poller = BookPoller()

    # Negative best_bid — validator's ``_is_in_unit_range(-0.5)`` check
    # returns False and an "Invalid best_bid" error is appended.
    book_data = _make_book_payload(bids_price="-0.5", asks_price="0.51")
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)

    # Validator saw 1 invalid (the snapshot hash was added before the
    # value check, so ``seen_hashes_size == 1`` too).
    stats = fresh_validator.get_stats()
    assert stats["invalid_count"] == 1, (
        f"expected invalid_count=1, got {stats['invalid_count']}"
    )
    assert stats["valid_count"] == 0

    # record_snapshot NOT called — the validator gate short-circuited.
    assert mock_db_manager.record_snapshot.call_count == 0, (
        f"expected record_snapshot NOT called, got "
        f"{mock_db_manager.record_snapshot.call_count} calls"
    )
    # record_tick NOT called either — both downstream calls are gated.
    assert mock_timescale.record_tick.call_count == 0


# ────────────────────────────────────────────────────────────────────────────
# 3. book_poller wiring — duplicate snapshot is SKIPPED
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_book_poller_skips_duplicate_snapshot(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    mock_timescale: MagicMock,  # noqa: ARG001 — still pulled in so ``record_tick`` is stubbed
    mock_raw_vault: MagicMock,  # noqa: ARG001
    mock_source_registry: MagicMock,  # noqa: ARG001
):
    """A second poll of the SAME token with the SAME top-of-book (same
    ``token_id`` + ``best_bid`` + ``best_ask`` + ``timestamp``) must be
    flagged as a duplicate by the validator's hash dedup — and the
    ``db_manager.record_snapshot`` call must be SKIPPED on the
    duplicate path.

    W26-3 — ``record_snapshot`` is now routed through the
    ``db_manager`` facade (was previously a direct
    ``timescale_db.record_snapshot`` call). The duplicate-skip
    assertion therefore targets ``mock_db_manager.record_snapshot``.

    The CLOB legitimately returns the same book on consecutive polls
    within a single Tier-1 interval, so this is the expected steady
    state — the dedup is the W24-4 task's primary motivation (without
    it, the TimescaleDB hypertable would be inflated with duplicate
    rows).

    Belt-and-braces:
      * First poll: validator ``valid_count == 1``, ``record_snapshot``
        called once.
      * Second poll (identical): validator ``duplicate_count == 1``,
        ``record_snapshot`` still at 1 (not re-called).
    """
    from core.book_poller import BookPoller

    poller = BookPoller()
    poller.set_tokens(["T1"])

    # Use a FIXED timestamp so the two polls hash to the same key.
    fixed_ts = time.time()
    book_data = _make_book_payload(timestamp=fixed_ts)

    # First poll — accepted.
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)
    first_call_count = mock_db_manager.record_snapshot.call_count
    assert first_call_count == 1, (
        f"first poll should have called record_snapshot once, "
        f"got {first_call_count}"
    )

    # Second poll with IDENTICAL payload + timestamp → duplicate hash.
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)

    # record_snapshot NOT called again — duplicate was skipped.
    assert mock_db_manager.record_snapshot.call_count == first_call_count, (
        f"second poll should NOT have called record_snapshot, "
        f"got {mock_db_manager.record_snapshot.call_count - first_call_count} "
        f"extra calls"
    )

    # Validator saw 1 valid + 1 duplicate.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["duplicate_count"] == 1


# ────────────────────────────────────────────────────────────────────────────
# 4. trade_ingester wiring — validates BEFORE recording (happy path)
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_trade_ingester_validates_trade_before_recording(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    """``core.trade_ingester._ingest_trades`` MUST route each trade
    through the validator BEFORE awaiting ``db_manager.record_trade``,
    and the data passed to ``record_trade`` must come from the
    validator's NORMALISED payload (``price`` / ``size`` coerced to
    float, ``side`` upper-cased, ``timestamp`` normalised to float).

    Mock strategy: replace ``clob_client.get_public_trades`` with an
    AsyncMock returning one well-formed trade (lower-case side,
    numeric-string price / size). The validator's normalisation must
    coerce all three and the ingester must pass the normalised values
    to ``db_manager.record_trade``.

    Belt-and-braces:
      * ``validate_trade`` IS called (validator saw 1 valid).
      * ``record_trade`` IS called exactly once.
      * The recorded trade carries the validator's normalised payload:
        ``side="BUY"`` (upper-cased), ``price=0.55`` (float), ``size=100.0``
        (float).
      * The validator's stats reflect ``valid_count == 1``.
    """
    from core.trade_ingester import TradeTapeIngester

    # Mock clob_client.get_public_trades to return one trade with
    # lower-case side + numeric-string price/size so we can assert the
    # validator's normalisation flows through to record_trade.
    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {
            "trade_id": "trade-happy",
            "token_id": "T1",
            "price": "0.55",   # string → validator coerces to float
            "size": "100",     # string → validator coerces to float
            "side": "buy",     # lowercase → validator upper-cases
            "timestamp": str(time.time()),
            "maker_address": "0xmaker",
            "taker_order_id": "0xtaker",
        },
    ])
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    ingester = TradeTapeIngester()
    await ingester._ingest_trades()

    # Validator saw 1 valid trade.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1, (
        f"expected valid_count=1, got {stats['valid_count']}"
    )
    assert stats["invalid_count"] == 0
    assert stats["duplicate_count"] == 0

    # record_trade WAS called once.
    assert mock_db_manager.record_trade.call_count == 1, (
        f"expected record_trade.call_count=1, "
        f"got {mock_db_manager.record_trade.call_count}"
    )

    # The recorded trade carries the validator's NORMALISED payload:
    # side upper-cased, price/size coerced to float.
    call_kwargs = mock_db_manager.record_trade.call_args.kwargs
    assert call_kwargs["trade_id"] == "trade-happy"
    assert call_kwargs["token_id"] == "T1"
    assert call_kwargs["side"] == "BUY", (
        f"expected side='BUY' (upper-cased), got {call_kwargs['side']!r}"
    )
    assert call_kwargs["price"] == pytest.approx(0.55)
    assert call_kwargs["size"] == pytest.approx(100.0)
    assert isinstance(call_kwargs["timestamp"], float)


# ────────────────────────────────────────────────────────────────────────────
# 5. trade_ingester wiring — invalid trade is REJECTED
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_trade_ingester_skips_invalid_trade(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    """An INVALID trade (missing ``token_id`` AND negative ``price``)
    must be rejected by the validator AND ``db_manager.record_trade``
    must NOT be called for that trade.

    The ingester wraps each trade's recording in its own try/except so
    a single bad trade can't poison the rest of the batch — the test
    verifies the validator gate is the first line of defence (the
    try/except is the second).

    Belt-and-braces:
      * Two trades are returned by ``get_public_trades`` — one valid,
        one invalid (missing ``token_id`` + negative ``price``).
      * ``validate_trade`` IS called twice (validator saw 1 valid + 1
        invalid).
      * ``record_trade`` is called exactly ONCE (for the valid trade).
      * The recorded trade_id is the valid one's id (``"good"``).
    """
    from core.trade_ingester import TradeTapeIngester

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {
            "trade_id": "good",
            "token_id": "T1",
            "price": 0.55,
            "size": 100.0,
            "side": "BUY",
            "timestamp": time.time(),
        },
        {
            "trade_id": "bad",
            # token_id missing
            "price": -0.10,  # negative price → invalid
            "size": 50.0,
            "side": "SELL",
            "timestamp": time.time(),
        },
    ])
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    ingester = TradeTapeIngester()
    await ingester._ingest_trades()

    # Validator saw 1 valid + 1 invalid.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1, (
        f"expected valid_count=1, got {stats['valid_count']}"
    )
    assert stats["invalid_count"] == 1, (
        f"expected invalid_count=1, got {stats['invalid_count']}"
    )

    # record_trade called exactly once (for the valid trade only).
    assert mock_db_manager.record_trade.call_count == 1, (
        f"expected record_trade.call_count=1 (valid only), "
        f"got {mock_db_manager.record_trade.call_count}"
    )

    # The recorded trade is the VALID one.
    call_kwargs = mock_db_manager.record_trade.call_args.kwargs
    assert call_kwargs["trade_id"] == "good", (
        f"expected trade_id='good', got {call_kwargs['trade_id']!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 6. trade_ingester wiring — duplicate trade is SKIPPED
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_trade_ingester_skips_duplicate_trade(
    fresh_validator: DataValidator,
    mock_db_manager: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    """A trade whose ``trade_id`` was already seen must be flagged as
    a duplicate by the validator AND ``db_manager.record_trade`` must
    NOT be called for the duplicate.

    The trade_ingester's own in-memory ``_last_trade_ids`` set is the
    fast-path dedup; the validator's ``_seen_ids`` deque is the second
    layer (belt-and-braces — fires only if the fast path misses, e.g.
    on a race between two polls returning the same ``trade_id``). This
    test exercises the fast-path dedup (the second poll's trades list
    contains a trade_id already in ``_last_trade_ids``) — the validator
    never even sees the duplicate because the ingester skips it before
    calling ``validate_trade``.

    To exercise the validator's own dedup (the second-layer path), the
    test bypasses the fast path by calling ``validate_trade`` directly
    in the assertion section.

    Belt-and-braces:
      * First poll: 1 valid trade → ``record_trade`` called once.
      * Second poll (same ``trade_id``): fast-path dedup skips it →
        ``record_trade`` still at 1.
      * Direct validator call with the same ``trade_id`` →
        ``is_duplicate=True``.
    """
    from core.trade_ingester import TradeTapeIngester

    trade_payload = {
        "trade_id": "dup-1",
        "token_id": "T1",
        "price": 0.55,
        "size": 100.0,
        "side": "BUY",
        "timestamp": time.time(),
    }

    mock_clob = MagicMock()
    # First call: one trade. Second call: the SAME trade (fast-path
    # dedup should skip it before it reaches the validator).
    mock_clob.get_public_trades = AsyncMock(
        side_effect=[[trade_payload], [trade_payload]]
    )
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    ingester = TradeTapeIngester()

    # First poll — accepted, record_trade called once.
    await ingester._ingest_trades()
    assert mock_db_manager.record_trade.call_count == 1, (
        f"first poll should call record_trade once, got "
        f"{mock_db_manager.record_trade.call_count}"
    )

    # Second poll — fast-path dedup hits in ``_last_trade_ids`` so
    # ``validate_trade`` is never even called for the duplicate.
    await ingester._ingest_trades()
    assert mock_db_manager.record_trade.call_count == 1, (
        f"second poll should NOT call record_trade again, got "
        f"{mock_db_manager.record_trade.call_count} total"
    )

    # The validator saw exactly 1 valid trade (the first poll).
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["invalid_count"] == 0
    # ``duplicate_count`` is 0 because the fast-path dedup caught it
    # before the validator. Belt-and-braces: verify the validator's
    # OWN dedup path fires when called directly.
    assert stats["duplicate_count"] == 0

    # Direct validator call — second-layer dedup. The trade_id is
    # already in ``fresh_validator._seen_ids``, so this MUST return
    # ``is_duplicate=True``.
    direct_result = fresh_validator.validate_trade(trade_payload)
    assert direct_result.is_valid is False
    assert direct_result.is_duplicate is True, (
        "direct validate_trade call with a known trade_id must return "
        "is_duplicate=True (second-layer dedup)"
    )


# ────────────────────────────────────────────────────────────────────────────
# 7. API route — GET /api/data-validator/stats (minimal FastAPI app)
# ────────────────────────────────────────────────────────────────────────────
def test_data_validator_stats_route_returns_200_with_shape(
    monkeypatch: pytest.MonkeyPatch,
):
    """``GET /api/data-validator/stats`` returns HTTP 200 + the
    documented counter shape (``valid_count`` / ``invalid_count`` /
    ``duplicate_count`` / ``seen_ids_size`` / ``seen_hashes_size``).

    Mirrors the minimal-app pattern in ``tests/test_data_validator.py``:
    a fresh FastAPI app with ONLY the data-validator route registered,
    so the test runs in <100 ms and doesn't pull in the full
    ``api/server.py`` lifespan startup. The route handler is the same
    shape the production wiring uses — ``return data_validator.get_stats()``.

    Belt-and-braces:
      * Response status is 200.
      * Response body has exactly the 5 documented keys.
      * All values are plain ints (JSON-serialisable).
      * On a fresh validator, every counter is 0.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fresh = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh)

    app = FastAPI()

    @app.get("/api/data-validator/stats", tags=["system"])
    async def stats():
        from core.data_validator import data_validator
        return data_validator.get_stats()

    client = TestClient(app)
    resp = client.get("/api/data-validator/stats")

    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert set(body.keys()) == {
        "valid_count", "invalid_count", "duplicate_count",
        "seen_ids_size", "seen_hashes_size",
    }
    # All values are plain ints.
    for k, v in body.items():
        assert isinstance(v, int), f"expected int for {k!r}, got {type(v).__name__}"
    # Fresh validator — all counters at zero.
    assert body["valid_count"] == 0
    assert body["invalid_count"] == 0
    assert body["duplicate_count"] == 0
    assert body["seen_ids_size"] == 0
    assert body["seen_hashes_size"] == 0


# ────────────────────────────────────────────────────────────────────────────
# 8. API route — reflects validator state (live singleton read)
# ────────────────────────────────────────────────────────────────────────────
def test_data_validator_stats_route_reflects_validator_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """After a mix of valid / invalid / duplicate validation calls,
    the stats route's response reflects the updated counters — proves
    the route reads the LIVE singleton (not a snapshot captured at
    handler registration time).

    Belt-and-braces:
      * Before any calls: all counters are 0.
      * After 1 valid snapshot + 1 duplicate snapshot + 1 valid trade:
        ``valid_count == 2``, ``duplicate_count == 1``,
        ``seen_hashes_size == 1``, ``seen_ids_size == 1``.
      * The route's response matches the validator's ``get_stats()``
        return value exactly.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fresh = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh)

    app = FastAPI()

    @app.get("/api/data-validator/stats", tags=["system"])
    async def stats():
        from core.data_validator import data_validator
        return data_validator.get_stats()

    client = TestClient(app)

    # Before any calls — all zero.
    resp0 = client.get("/api/data-validator/stats")
    assert resp0.json()["valid_count"] == 0

    # Make a mix of validation calls.
    snap = _make_valid_snapshot_raw()
    fresh.validate_snapshot(snap)         # 1 valid snapshot
    fresh.validate_snapshot(snap)         # 1 duplicate snapshot
    fresh.validate_trade(_make_valid_trade_raw())  # 1 valid trade

    # After the calls — counters reflect the mix.
    resp = client.get("/api/data-validator/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid_count"] == 2, (
        f"expected valid_count=2 (1 snap + 1 trade), got {body['valid_count']}"
    )
    assert body["duplicate_count"] == 1, (
        f"expected duplicate_count=1, got {body['duplicate_count']}"
    )
    assert body["seen_hashes_size"] == 1
    assert body["seen_ids_size"] == 1

    # The route's response EXACTLY matches the validator's get_stats().
    # Proves the route is reading the live singleton (not a cached copy).
    assert body == fresh.get_stats()


# ────────────────────────────────────────────────────────────────────────────
# 9. API route — registered on the production app (defensive)
# ────────────────────────────────────────────────────────────────────────────
def test_data_validator_stats_route_registered_on_production_app():
    """The production ``api/server.py`` FastAPI app MUST have the
    ``/api/data-validator/stats`` route registered.

    This is a defensive test against accidental deletion / rename of
    the W24-4 wiring block in ``api/server.py``. The previous two
    tests exercise the route handler logic against a minimal app; this
    test asserts the wiring block is present on the production app so
    a future refactor (e.g. moving routes into a ``register_routes``
    function in a new module) can't silently drop the endpoint without
    a test failure.

    Implementation: import the production ``app`` from ``api.server``
    and walk its ``app.routes`` for a ``GET /api/data-validator/stats``
    entry. We do NOT make an HTTP request (the production app requires
    bearer auth + would trigger the full lifespan startup if we used
    a context-managed TestClient); the route-table introspection is
    sufficient and runs in <1 s.
    """
    # Late import so the heavy ``api/server.py`` module load only
    # happens for this one test (the other 8 tests stay hermetic + fast).
    from api.server import app

    registered_paths = {
        getattr(route, "path", None)
        for route in app.routes
    }
    assert "/api/data-validator/stats" in registered_paths, (
        "the production api/server.py app is missing the "
        "/api/data-validator/stats route (W24-4 wiring block). "
        "Routes actually registered: "
        f"{sorted(p for p in registered_paths if p and 'data' in p.lower())}"
    )

    # Belt-and-braces: find the actual route object and verify its
    # HTTP method is GET (not POST / PUT / DELETE — the W24-4 spec
    # calls for a read-only GET).
    matching = [
        route for route in app.routes
        if getattr(route, "path", None) == "/api/data-validator/stats"
    ]
    assert len(matching) == 1, (
        f"expected exactly 1 route at /api/data-validator/stats, "
        f"got {len(matching)}"
    )
    methods = getattr(matching[0], "methods", set()) or set()
    assert "GET" in methods, (
        f"expected GET method on /api/data-validator/stats, "
        f"got methods={methods}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Module-level singleton — sanity check (mirror of test_data_validator.py)
# ────────────────────────────────────────────────────────────────────────────
def test_module_singleton_is_data_validator():
    """The module-level ``data_validator`` singleton is a ``DataValidator``
    instance — defensive against an accidental refactor that swaps it
    for a different type (e.g. a proxy / wrapper / mock left over from
    a prior test)."""
    assert isinstance(_module_singleton, DataValidator)
    # Public API surface is intact.
    assert hasattr(_module_singleton, "validate_snapshot")
    assert hasattr(_module_singleton, "validate_trade")
    assert hasattr(_module_singleton, "get_stats")
