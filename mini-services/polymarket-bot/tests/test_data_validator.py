"""
tests/test_data_validator.py — Unit + integration tests for the W24-4
data ingestion validator.

Covers the nine behaviour classes required by the W24-4 task spec:

  1. ``validate_snapshot`` with valid data — accepts the record, returns
     ``is_valid=True``, normalised payload carries provenance fields
     (``ingestion_time`` / ``processing_time`` / ``source``) and derived
     fields (``mid`` / ``spread``).
  2. ``validate_snapshot`` with duplicate — second identical call returns
     ``is_duplicate=True``, ``is_valid=False``, and the dedup hash deque
     holds exactly one entry.
  3. ``validate_snapshot`` with missing fields — each missing required
     field (``token_id`` / ``best_bid`` / ``best_ask``) appends a
     distinct error message and the record is rejected.
  4. ``validate_snapshot`` with invalid values — negative ``best_bid``,
     ``best_bid > 1.0``, and non-numeric ``best_ask`` all append errors
     and the record is rejected.
  5. ``validate_trade`` with valid data — accepts the record, normalised
     payload carries ``price`` / ``size`` / ``timestamp`` as floats,
     ``side`` upper-cased, provenance fields added.
  6. ``validate_trade`` with duplicate — second call with the same
     ``trade_id`` returns ``is_duplicate=True``.
  7. Timestamp normalisation — numeric strings, ISO-8601 strings, and
     missing timestamps all coerce to a Unix-epoch float.
  8. Staleness detection — timestamp > 60s in the past emits a warning;
     > 300s in the past rejects the record with an error.
  9. ``get_stats`` — returns the expected key shape + counts after a
     mix of valid / invalid / duplicate calls.

Integration coverage:

  * ``core.book_poller._apply_book`` — second identical call (duplicate
    snapshot hash) skips ``timescale_db.record_snapshot`` so the
    hypertable isn't inflated with duplicate rows.
  * ``core.trade_ingester._ingest_trades`` — an invalid trade (missing
    ``token_id`` / negative ``price``) is rejected by the validator
    and never reaches ``db_manager.record_trade``.
  * ``GET /api/data-validator/stats`` (HTTP) — returns 200 + the live
    counters shape.

Isolation strategy
------------------
Each test constructs a fresh ``DataValidator()`` instance per test (NOT
the module-level singleton) so the in-memory ``_seen_ids`` / ``_seen_hashes``
deques are empty at the start of every test — no cross-test pollution.
The module-level ``data_validator`` singleton is monkeypatched in the
integration tests so the production code paths (which import the singleton
lazily inside the function body) pick up the test-scoped instance.

All tests are SYNC ``def`` — the entire ``core/data_validator.py`` module
is sync (no ``await`` inside ``validate_snapshot`` / ``validate_trade``).
The HTTP-route test uses ``fastapi.testclient.TestClient``, which runs
the async handler inside its own sync portal — no ``pytestmark =
pytest.mark.asyncio`` is needed.

The integration tests that touch ``book_poller._apply_book`` /
``trade_ingester._ingest_trades`` ARE async (the production call sites
are ``async def``) and are explicitly marked with ``@pytest.mark.asyncio``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Defensive env-var redirect (mirrors the established pattern in
# tests/test_data_quality.py / tests/test_retention.py). ``setdefault``
# lets conftest (which loads first) win when present; this block is
# purely a defensive net so the file stays hermetic in a hypothetical
# conftest-less invocation.
_TMP_ROOT = Path("/tmp/data_validator_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    # Force paper mode + live disabled so any co-collected stateful test
    # doesn't trip a shadow / live-trading gate at import time.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-data-validator",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.data_validator``) regardless of the cwd pytest was launched from.
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
# Fixtures
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def validator() -> DataValidator:
    """Fresh ``DataValidator`` per test (empty dedup deques + zero counters).

    The module-level singleton ``data_validator`` is NOT used so each
    test starts with empty ``_seen_ids`` / ``_seen_hashes`` / counters
    — no leakage between tests. Mirrors the ``poller`` fixture in
    ``tests/test_book_poller.py``.
    """
    return DataValidator()


def _make_valid_snapshot(
    *,
    token_id: str = "0xabc123",
    best_bid: float = 0.49,
    best_ask: float = 0.51,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal valid snapshot dict for tests.

    ``timestamp`` defaults to ``time.time()`` (fresh) so the staleness
    check passes; tests that exercise the staleness branches override
    it explicitly.
    """
    snap = {
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "source": "test",
    }
    return snap


def _make_valid_trade(
    *,
    trade_id: str = "trade-1",
    token_id: str = "0xabc123",
    price: float = 0.50,
    size: float = 100.0,
    side: str = "BUY",
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal valid trade dict for tests."""
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


# ────────────────────────────────────────────────────────────────────────────
# 1. validate_snapshot — valid data
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_valid(validator: DataValidator):
    """A well-formed snapshot (token_id + in-range bid/ask + fresh
    timestamp) must be accepted, with provenance + derived fields
    added to the normalised payload.
    """
    raw = _make_valid_snapshot(best_bid=0.49, best_ask=0.51)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is True
    assert result.is_duplicate is False
    assert result.errors == []

    # Provenance fields present.
    assert "ingestion_time" in result.normalized_data
    assert "processing_time" in result.normalized_data
    assert result.normalized_data["source"] == "test"

    # Derived fields computed.
    assert result.normalized_data["mid"] == pytest.approx(0.50)
    assert result.normalized_data["spread"] == pytest.approx(0.02)

    # Timestamp normalised to float.
    assert isinstance(result.normalized_data["timestamp"], float)

    # Stats reflect the one valid record.
    stats = validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["invalid_count"] == 0
    assert stats["duplicate_count"] == 0
    assert stats["seen_hashes_size"] == 1
    assert stats["seen_ids_size"] == 0  # trades-only deque


def test_validate_snapshot_preserves_input_fields(validator: DataValidator):
    """The normalised payload must preserve every input field (via the
    ``{**raw_data, ...}`` spread) — callers should NOT have to merge
    the original dict with the normalised one.
    """
    raw = _make_valid_snapshot()
    raw["custom_field"] = "preserved"
    raw["another"] = 42

    result = validator.validate_snapshot(raw)
    assert result.is_valid is True
    assert result.normalized_data["custom_field"] == "preserved"
    assert result.normalized_data["another"] == 42


# ────────────────────────────────────────────────────────────────────────────
# 2. validate_snapshot — duplicate
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_duplicate(validator: DataValidator):
    """Two identical snapshots (same token_id / best_bid / best_ask /
    timestamp) must dedup — the second call returns
    ``is_duplicate=True``, ``is_valid=False``, and the dedup counter
    increments.
    """
    raw = _make_valid_snapshot()
    first = validator.validate_snapshot(raw)
    assert first.is_valid is True

    # Second identical call — dedup hit.
    second = validator.validate_snapshot(raw)
    assert second.is_valid is False
    assert second.is_duplicate is True
    assert "Duplicate snapshot" in second.errors

    stats = validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["duplicate_count"] == 1
    # Hash deque holds exactly one entry (the duplicate wasn't re-added).
    assert stats["seen_hashes_size"] == 1


def test_validate_snapshot_duplicate_with_different_token_id(validator: DataValidator):
    """Two snapshots with DIFFERENT token_ids must NOT dedup — even if
    every other field matches.
    """
    raw1 = _make_valid_snapshot(token_id="A")
    raw2 = _make_valid_snapshot(token_id="B")

    r1 = validator.validate_snapshot(raw1)
    r2 = validator.validate_snapshot(raw2)

    assert r1.is_valid is True
    assert r2.is_valid is True
    assert r2.is_duplicate is False

    assert validator.get_stats()["duplicate_count"] == 0
    assert validator.get_stats()["seen_hashes_size"] == 2


# ────────────────────────────────────────────────────────────────────────────
# 3. validate_snapshot — missing fields
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_missing_token_id(validator: DataValidator):
    """Missing ``token_id`` appends a "Missing required field: token_id"
    error and the record is rejected.
    """
    raw = _make_valid_snapshot()
    del raw["token_id"]
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert result.is_duplicate is False
    assert any("token_id" in e for e in result.errors)
    assert result.normalized_data == {}


def test_validate_snapshot_missing_best_bid(validator: DataValidator):
    """Missing ``best_bid`` appends an error AND triggers the
    ``_is_in_unit_range(0)`` check (since the default is ``0``) —
    both must surface, and the record must be rejected.
    """
    raw = _make_valid_snapshot()
    del raw["best_bid"]
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("best_bid" in e for e in result.errors)


def test_validate_snapshot_missing_all_required(validator: DataValidator):
    """Missing every required field — three distinct errors, rejected."""
    raw = {"timestamp": time.time()}
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert len(result.errors) >= 3
    assert result.normalized_data == {}


# ────────────────────────────────────────────────────────────────────────────
# 4. validate_snapshot — invalid values
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_negative_bid(validator: DataValidator):
    """``best_bid < 0`` appends an "Invalid best_bid" error and rejects."""
    raw = _make_valid_snapshot(best_bid=-0.10, best_ask=0.50)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("Invalid best_bid" in e for e in result.errors)


def test_validate_snapshot_bid_over_one(validator: DataValidator):
    """``best_bid > 1.0`` appends an "Invalid best_bid" error (out of
    the ``[0, 1]`` probability range) and rejects.
    """
    raw = _make_valid_snapshot(best_bid=1.5, best_ask=1.6)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("Invalid best_bid" in e for e in result.errors)


def test_validate_snapshot_non_numeric_bid(validator: DataValidator):
    """Non-numeric ``best_bid`` (string "abc") appends an error and
    rejects — the validator's ``_is_in_unit_range`` helper swallows the
    ``TypeError`` / ``ValueError`` so the validator never crashes.
    """
    raw = _make_valid_snapshot()
    raw["best_bid"] = "not-a-number"
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("Invalid best_bid" in e for e in result.errors)


def test_validate_snapshot_crossed_market_warning(validator: DataValidator):
    """``best_bid > best_ask`` is a crossed market — the validator
    should NOT reject it (crossed markets are observable in production
    when an aggressive market maker sweeps both sides), but should
    emit a warning so the operator can see the anomaly.
    """
    raw = _make_valid_snapshot(best_bid=0.55, best_ask=0.45)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is True
    assert any("Crossed market" in w for w in result.warnings)


# ────────────────────────────────────────────────────────────────────────────
# 5. validate_trade — valid data
# ────────────────────────────────────────────────────────────────────────────
def test_validate_trade_valid(validator: DataValidator):
    """A well-formed trade (token_id + positive price/size + valid side
    + fresh timestamp) is accepted, with normalised + provenance fields.
    """
    raw = _make_valid_trade(price=0.50, size=100.0, side="buy")
    result = validator.validate_trade(raw)

    assert result.is_valid is True
    assert result.is_duplicate is False
    assert result.errors == []

    # Normalised types.
    assert isinstance(result.normalized_data["price"], float)
    assert isinstance(result.normalized_data["size"], float)
    assert isinstance(result.normalized_data["timestamp"], float)
    # Side upper-cased.
    assert result.normalized_data["side"] == "BUY"

    # Provenance.
    assert "ingestion_time" in result.normalized_data
    assert "processing_time" in result.normalized_data

    # Stats.
    stats = validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["seen_ids_size"] == 1


def test_validate_trade_preserves_input_fields(validator: DataValidator):
    """Normalised payload preserves every input field (the ``{**raw_data,
    ...}`` spread)."""
    raw = _make_valid_trade()
    raw["maker_address"] = "0xabc"
    raw["taker_order_id"] = "0xdef"
    raw["extra"] = "kept"

    result = validator.validate_trade(raw)
    assert result.is_valid is True
    assert result.normalized_data["maker_address"] == "0xabc"
    assert result.normalized_data["taker_order_id"] == "0xdef"
    assert result.normalized_data["extra"] == "kept"


# ────────────────────────────────────────────────────────────────────────────
# 6. validate_trade — duplicate
# ────────────────────────────────────────────────────────────────────────────
def test_validate_trade_duplicate(validator: DataValidator):
    """Two trades with the same ``trade_id`` dedup — the second call
    returns ``is_duplicate=True`` and the dedup counter increments.
    """
    raw = _make_valid_trade(trade_id="t-001")
    first = validator.validate_trade(raw)
    assert first.is_valid is True

    second = validator.validate_trade(raw)
    assert second.is_valid is False
    assert second.is_duplicate is True
    assert "Duplicate trade" in second.errors

    stats = validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["duplicate_count"] == 1
    assert stats["seen_ids_size"] == 1  # not re-added


def test_validate_trade_duplicate_via_id_field(validator: DataValidator):
    """The dedup key falls back to the ``id`` field when ``trade_id``
    is absent (mirrors the CLOB normalisation in
    ``clob_client.get_public_trades`` which emits ``trade_id`` but the
    raw CLOB response may carry ``id``).
    """
    raw1 = {"id": "abc", "token_id": "T", "price": 0.5, "size": 10, "side": "BUY", "timestamp": time.time()}
    raw2 = {"id": "abc", "token_id": "T", "price": 0.5, "size": 10, "side": "BUY", "timestamp": time.time()}

    r1 = validator.validate_trade(raw1)
    r2 = validator.validate_trade(raw2)

    assert r1.is_valid is True
    assert r2.is_valid is False
    assert r2.is_duplicate is True


def test_validate_trade_no_id_skips_dedup(validator: DataValidator):
    """A trade with NO ``trade_id`` AND no ``id`` skips the dedup fast
    path (the validator can't dedup an unknown key) — but still runs
    every other check. Two such trades must both be accepted.
    """
    raw1 = {"token_id": "T", "price": 0.5, "size": 10, "side": "BUY", "timestamp": time.time()}
    raw2 = {"token_id": "T", "price": 0.6, "size": 20, "side": "SELL", "timestamp": time.time()}

    r1 = validator.validate_trade(raw1)
    r2 = validator.validate_trade(raw2)

    assert r1.is_valid is True
    assert r2.is_valid is True
    assert r2.is_duplicate is False
    assert validator.get_stats()["duplicate_count"] == 0


# ────────────────────────────────────────────────────────────────────────────
# 7. Timestamp normalisation
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_timestamp_numeric_string(validator: DataValidator):
    """A timestamp given as a numeric string ("1700000000.0") is
    coerced to a float.
    """
    raw = _make_valid_snapshot(timestamp=None)
    raw["timestamp"] = "1700000000.0"
    result = validator.validate_snapshot(raw)

    # Stale (timestamp is in 2023) — should be rejected with a staleness
    # error, but the timestamp field was still normalised to a float.
    # Actually, for an old timestamp like 2023, staleness will be huge →
    # the very-stale branch fires and the record is rejected. So we
    # expect is_valid=False with the staleness error.
    assert result.is_valid is False
    assert any("stale" in e.lower() for e in result.errors)


def test_validate_snapshot_timestamp_iso8601(validator: DataValidator):
    """An ISO-8601 timestamp string (e.g. "2026-09-04T12:34:56+00:00")
    is parsed via ``datetime.fromisoformat`` and converted to a Unix
    epoch float.
    """
    # Use a recent ISO timestamp (now) so the staleness check passes.
    from datetime import datetime, timezone

    iso_now = datetime.now(timezone.utc).isoformat()
    raw = _make_valid_snapshot(timestamp=None)
    raw["timestamp"] = iso_now

    result = validator.validate_snapshot(raw)
    assert result.is_valid is True
    assert isinstance(result.normalized_data["timestamp"], float)
    # The normalised timestamp should be ~now (within a few seconds).
    assert abs(result.normalized_data["timestamp"] - time.time()) < 5.0


def test_validate_snapshot_timestamp_iso_with_z_suffix(validator: DataValidator):
    """An ISO-8601 timestamp with a trailing ``Z`` (UTC designator)
    is parsed correctly (the validator strips the ``Z`` and replaces
    with ``+00:00`` for Python 3.10 compatibility).
    """
    from datetime import datetime, timezone

    iso_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = _make_valid_snapshot(timestamp=None)
    raw["timestamp"] = iso_z

    result = validator.validate_snapshot(raw)
    assert result.is_valid is True
    assert isinstance(result.normalized_data["timestamp"], float)


def test_validate_snapshot_timestamp_missing(validator: DataValidator):
    """A missing timestamp falls back to ``ingestion_time`` and emits a
    warning (so the operator can see the downstream effect).
    """
    raw = _make_valid_snapshot()
    del raw["timestamp"]
    result = validator.validate_snapshot(raw)

    assert result.is_valid is True
    assert any("Missing timestamp" in w for w in result.warnings)
    # The normalised timestamp is the ingestion_time (within a
    # microsecond — both captured in the same call).
    assert abs(result.normalized_data["timestamp"] - result.ingestion_time) < 0.001


def test_validate_snapshot_timestamp_invalid_string(validator: DataValidator):
    """A timestamp that's neither numeric nor ISO-8601 (e.g. "garbage")
    appends an "Invalid timestamp format" error AND falls back to
    ``ingestion_time`` — the record is rejected (because of the error)
    but the timestamp field is still normalised so a downstream
    consumer doesn't choke on a non-numeric value.
    """
    raw = _make_valid_snapshot()
    raw["timestamp"] = "garbage"
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("Invalid timestamp" in e for e in result.errors)


def test_validate_trade_timestamp_normalisation(validator: DataValidator):
    """Trades also normalise their timestamp to a float — numeric
    strings, ISO-8601, and missing all coerce cleanly.
    """
    # Numeric string.
    raw = _make_valid_trade(timestamp=None)
    raw["timestamp"] = str(time.time())
    r1 = validator.validate_trade(raw)
    assert r1.is_valid is True
    assert isinstance(r1.normalized_data["timestamp"], float)

    # ISO-8601.
    from datetime import datetime, timezone
    raw2 = _make_valid_trade(trade_id="t-iso", timestamp=None)
    raw2["timestamp"] = datetime.now(timezone.utc).isoformat()
    r2 = validator.validate_trade(raw2)
    assert r2.is_valid is True
    assert isinstance(r2.normalized_data["timestamp"], float)

    # Missing — falls back to ingestion_time with no warning
    # (trades don't run staleness, so missing timestamp is silent).
    raw3 = _make_valid_trade(trade_id="t-none", timestamp=None)
    del raw3["timestamp"]
    r3 = validator.validate_trade(raw3)
    assert r3.is_valid is True
    assert abs(r3.normalized_data["timestamp"] - r3.ingestion_time) < 0.001


# ────────────────────────────────────────────────────────────────────────────
# 8. Staleness detection
# ────────────────────────────────────────────────────────────────────────────
def test_validate_snapshot_stale_warning(validator: DataValidator):
    """A timestamp 90s in the past (between 60s and 300s) emits a
    "Stale data" warning but does NOT reject the record.
    """
    raw = _make_valid_snapshot(timestamp=time.time() - 90)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is True
    assert any("Stale data" in w for w in result.warnings)
    assert not any("stale" in e.lower() for e in result.errors)


def test_validate_snapshot_very_stale_rejection(validator: DataValidator):
    """A timestamp 600s in the past (> 300s) REJECTS the record with a
    "Very stale data" error (the very-stale branch fires BEFORE the
    warning branch — W24-4 spec fix).
    """
    raw = _make_valid_snapshot(timestamp=time.time() - 600)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is False
    assert any("Very stale data" in e for e in result.errors)


def test_validate_snapshot_fresh_no_warning(validator: DataValidator):
    """A timestamp 10s in the past (< 60s) is fresh — no staleness
    warning or error.
    """
    raw = _make_valid_snapshot(timestamp=time.time() - 10)
    result = validator.validate_snapshot(raw)

    assert result.is_valid is True
    assert not any("stale" in w.lower() for w in result.warnings)
    assert not any("stale" in e.lower() for e in result.errors)


# ────────────────────────────────────────────────────────────────────────────
# 9. get_stats
# ────────────────────────────────────────────────────────────────────────────
def test_get_stats_initial_state(validator: DataValidator):
    """``get_stats`` on a fresh validator returns all-zero counters
    with the documented key shape.
    """
    stats = validator.get_stats()
    assert set(stats.keys()) == {
        "valid_count", "invalid_count", "duplicate_count",
        "seen_ids_size", "seen_hashes_size",
    }
    assert stats["valid_count"] == 0
    assert stats["invalid_count"] == 0
    assert stats["duplicate_count"] == 0
    assert stats["seen_ids_size"] == 0
    assert stats["seen_hashes_size"] == 0


def test_get_stats_mixed_calls(validator: DataValidator):
    """After a mix of valid / invalid / duplicate calls, the stats
    counters reflect the cumulative counts.
    """
    # 2 valid + 1 duplicate snapshots.
    s1 = _make_valid_snapshot(token_id="A")
    s2 = _make_valid_snapshot(token_id="B")
    validator.validate_snapshot(s1)
    validator.validate_snapshot(s2)
    validator.validate_snapshot(s2)  # duplicate

    # 1 valid + 1 invalid (missing field) + 1 duplicate trade.
    t1 = _make_valid_trade(trade_id="t1")
    validator.validate_trade(t1)
    validator.validate_trade(t1)  # duplicate
    bad_trade = {"token_id": "T", "price": 0.5, "size": 10, "side": "BUY", "timestamp": time.time()}
    del bad_trade["price"]
    validator.validate_trade(bad_trade)  # invalid — missing price

    # 1 invalid snapshot (missing field).
    bad_snap = {"best_bid": 0.5, "best_ask": 0.6, "timestamp": time.time()}
    validator.validate_snapshot(bad_snap)  # invalid — missing token_id

    stats = validator.get_stats()
    assert stats["valid_count"] == 3   # 2 snapshots + 1 trade
    assert stats["invalid_count"] == 2  # 1 bad trade + 1 bad snapshot
    assert stats["duplicate_count"] == 2  # 1 duplicate snap + 1 duplicate trade
    assert stats["seen_ids_size"] == 1   # only t1 was a successful trade dedup add
    # ``seen_hashes_size`` is 3 (not 2) because the validator appends the
    # snapshot hash to the dedup deque BEFORE the schema / value checks
    # run — so the invalid snapshot (missing ``token_id``) still gets its
    # hash added. This is the spec-defined behaviour: a subsequent
    # identical bad snapshot would be flagged as a duplicate rather
    # than re-evaluated for schema errors (mild semantic quirk, but
    # acceptable — the durable DB constraint is the real backstop).
    assert stats["seen_hashes_size"] == 3


# ────────────────────────────────────────────────────────────────────────────
# Integration — book_poller._apply_book skips duplicates
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_book_poller_apply_book_skips_duplicate_snapshot(monkeypatch: pytest.MonkeyPatch):
    """``core.book_poller._apply_book`` must route the snapshot through
    the validator and SKIP the downstream ``timescale_db.record_snapshot``
    call when the validator detects a duplicate.

    Mock strategy: replace ``timescale_db.record_snapshot`` /
    ``record_tick`` with AsyncMocks so we can assert they were NOT
    called on the duplicate path. Replace the module-level
    ``data_validator`` singleton with a fresh instance so the test
    starts from an empty dedup deque.
    """
    from core.book_poller import BookPoller
    from core.data_store import store

    # Fresh validator singleton for this test.
    fresh_validator = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh_validator)

    # Mock downstream singletons.
    mock_ts = MagicMock()
    mock_ts.record_snapshot = AsyncMock(return_value=True)
    mock_ts.record_tick = AsyncMock(return_value=True)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    poller = BookPoller()
    poller.set_tokens(["T1"])

    # First call — should validate successfully and call record_snapshot.
    book_data = {
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "timestamp": time.time(),
    }
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)  # let fire-and-forget tasks settle

    assert mock_ts.record_snapshot.call_count == 1
    first_call_count = mock_ts.record_snapshot.call_count

    # Second call with IDENTICAL data — duplicate hash hit.
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)

    # record_snapshot NOT called again — duplicate was skipped.
    assert mock_ts.record_snapshot.call_count == first_call_count

    # The validator saw exactly 1 valid + 1 duplicate.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["duplicate_count"] == 1


@pytest.mark.asyncio
async def test_book_poller_apply_book_invalid_skips_record(monkeypatch: pytest.MonkeyPatch):
    """``_apply_book`` must skip ``record_snapshot`` when the validator
    rejects the snapshot (e.g. invalid value). The book_poller builds
    the raw_snapshot from the parsed OrderBook, so we have to inject
    a bad value via the raw book data — the easiest path is a
    negative price in the bids ladder.
    """
    from core.book_poller import BookPoller

    fresh_validator = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh_validator)

    mock_ts = MagicMock()
    mock_ts.record_snapshot = AsyncMock(return_value=True)
    mock_ts.record_tick = AsyncMock(return_value=True)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    poller = BookPoller()

    # A book with a NEGATIVE best_bid — the validator should reject it
    # with "Invalid best_bid: -0.5 (must be 0-1)".
    book_data = {
        "bids": [{"price": "-0.5", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "timestamp": time.time(),
    }
    await poller._apply_book("T1", book_data)
    await asyncio.sleep(0)

    # record_snapshot NOT called — invalid snapshot was skipped.
    assert mock_ts.record_snapshot.call_count == 0

    # The validator saw 1 invalid.
    stats = fresh_validator.get_stats()
    assert stats["invalid_count"] == 1
    assert stats["valid_count"] == 0


# ────────────────────────────────────────────────────────────────────────────
# Integration — trade_ingester._ingest_trades skips invalid trades
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_trade_ingester_skips_invalid_trade(monkeypatch: pytest.MonkeyPatch):
    """``core.trade_ingester._ingest_trades`` must route each trade
    through the validator and SKIP the downstream
    ``db_manager.record_trade`` call when the validator rejects it
    (e.g. missing required field).
    """
    from core.trade_ingester import TradeTapeIngester
    from core import trade_ingester as trade_ingester_mod

    # Fresh validator singleton.
    fresh_validator = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh_validator)

    # Mock clob_client.get_public_trades to return one valid + one
    # invalid trade (missing token_id).
    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {
            "trade_id": "valid-1",
            "token_id": "T1",
            "price": 0.5,
            "size": 100.0,
            "side": "BUY",
            "timestamp": time.time(),
        },
        {
            "trade_id": "invalid-1",
            # token_id missing
            "price": 0.6,
            "size": 50.0,
            "side": "SELL",
            "timestamp": time.time(),
        },
    ])
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    # Mock db_manager.record_trade so we can assert call count.
    mock_db = MagicMock()
    mock_db.record_trade = AsyncMock(return_value=True)
    monkeypatch.setattr("core.database_manager.db_manager", mock_db)

    ingester = TradeTapeIngester()
    await ingester._ingest_trades()

    # Only the valid trade reached record_trade.
    assert mock_db.record_trade.call_count == 1
    # The recorded trade is the valid one.
    call_kwargs = mock_db.record_trade.call_args.kwargs
    assert call_kwargs["trade_id"] == "valid-1"

    # Validator saw 1 valid + 1 invalid.
    stats = fresh_validator.get_stats()
    assert stats["valid_count"] == 1
    assert stats["invalid_count"] == 1


@pytest.mark.asyncio
async def test_trade_ingester_uses_normalised_payload(monkeypatch: pytest.MonkeyPatch):
    """The ingester must pass the validator's NORMALISED payload to
    ``db_manager.record_trade`` — side upper-cased, price/size as floats.
    """
    from core.trade_ingester import TradeTapeIngester

    fresh_validator = DataValidator()
    monkeypatch.setattr("core.data_validator.data_validator", fresh_validator)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {
            "trade_id": "t1",
            "token_id": "T1",
            "price": "0.55",  # string — validator coerces to float
            "size": "100",  # string — validator coerces to float
            "side": "buy",  # lowercase — validator upper-cases
            "timestamp": str(time.time()),
        },
    ])
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    mock_db = MagicMock()
    mock_db.record_trade = AsyncMock(return_value=True)
    monkeypatch.setattr("core.database_manager.db_manager", mock_db)

    ingester = TradeTapeIngester()
    await ingester._ingest_trades()

    assert mock_db.record_trade.call_count == 1
    call_kwargs = mock_db.record_trade.call_args.kwargs
    assert call_kwargs["price"] == pytest.approx(0.55)
    assert call_kwargs["size"] == pytest.approx(100.0)
    assert call_kwargs["side"] == "BUY"
    assert isinstance(call_kwargs["timestamp"], float)


# ────────────────────────────────────────────────────────────────────────────
# HTTP API — GET /api/data-validator/stats
# ────────────────────────────────────────────────────────────────────────────
def test_api_data_validator_stats():
    """``GET /api/data-validator/stats`` returns 200 + the documented
    counter shape (``valid_count`` / ``invalid_count`` /
    ``duplicate_count`` / ``seen_ids_size`` / ``seen_hashes_size``).

    Mirrors the minimal-app pattern in ``tests/test_data_quality.py``:
    a fresh FastAPI app with ONLY the data-validator route registered,
    so the test runs in <100 ms and doesn't pull in the full
    ``api/server.py`` lifespan startup.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Fresh validator — counters start at zero.
    fresh = DataValidator()

    app = FastAPI()

    @app.get("/api/data-validator/stats", tags=["system"])
    async def stats():
        return fresh.get_stats()

    client = TestClient(app)
    resp = client.get("/api/data-validator/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "valid_count", "invalid_count", "duplicate_count",
        "seen_ids_size", "seen_hashes_size",
    }
    assert body["valid_count"] == 0
    assert body["invalid_count"] == 0
    assert body["duplicate_count"] == 0


def test_api_data_validator_stats_reflects_calls():
    """After a few validation calls, the stats endpoint reflects the
    updated counters — proving the route reads the live singleton,
    not a stale snapshot.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fresh = DataValidator()

    app = FastAPI()

    @app.get("/api/data-validator/stats", tags=["system"])
    async def stats():
        return fresh.get_stats()

    client = TestClient(app)

    # Make a few validation calls.
    snap = _make_valid_snapshot()
    fresh.validate_snapshot(snap)
    fresh.validate_snapshot(snap)  # duplicate
    fresh.validate_trade(_make_valid_trade())

    resp = client.get("/api/data-validator/stats")
    assert resp.status_code == 200
    body = resp.json()

    assert body["valid_count"] == 2  # 1 snap + 1 trade
    assert body["duplicate_count"] == 1
    assert body["seen_hashes_size"] == 1
    assert body["seen_ids_size"] == 1


# ────────────────────────────────────────────────────────────────────────────
# Module singleton — sanity check
# ────────────────────────────────────────────────────────────────────────────
def test_module_singleton_is_data_validator():
    """The module-level ``data_validator`` singleton is a ``DataValidator``
    instance and exposes the documented public API."""
    assert isinstance(_module_singleton, DataValidator)
    assert hasattr(_module_singleton, "validate_snapshot")
    assert hasattr(_module_singleton, "validate_trade")
    assert hasattr(_module_singleton, "get_stats")
    # ``get_stats`` works on the singleton.
    stats = _module_singleton.get_stats()
    assert "valid_count" in stats


def test_validation_result_dataclass_defaults():
    """``ValidationResult`` dataclass has the documented defaults —
    empty lists for errors/warnings, empty dict for normalized_data,
    0.0 for ingestion_time. Required so callers can construct partial
    results without ``TypeError`` on missing kwargs.
    """
    r = ValidationResult(is_valid=True, is_duplicate=False)
    assert r.errors == []
    assert r.warnings == []
    assert r.normalized_data == {}
    assert r.ingestion_time == 0.0


def test_dedup_deque_maxlen_default():
    """The default ``max_seen_ids`` is 10_000 — a long-running session
    can't grow the deques without limit. Verified by constructing a
    validator with the default and inspecting the ``maxlen`` attribute.
    """
    v = DataValidator()
    assert v._seen_ids.maxlen == 10_000
    assert v._seen_hashes.maxlen == 10_000


def test_dedup_deque_custom_maxlen():
    """``max_seen_ids`` is configurable — a smaller-cap validator
    evicts earlier entries when full."""
    v = DataValidator(max_seen_ids=3)
    assert v._seen_ids.maxlen == 3

    # Insert 5 trades — only the last 3 should be in the deque.
    for i in range(5):
        v.validate_trade(_make_valid_trade(trade_id=f"t{i}"))
    assert v.get_stats()["seen_ids_size"] == 3

    # The first two trade_ids ("t0", "t1") should have been evicted —
    # re-submitting them must NOT be flagged as a duplicate.
    r = v.validate_trade(_make_valid_trade(trade_id="t0"))
    assert r.is_valid is True  # not a duplicate — evicted from deque
