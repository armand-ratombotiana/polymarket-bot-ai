"""
W9-5 — Unit tests for ``core/audit_logger.py``.

Covers the durable SQLite audit-trail public surface:

  1. ``log_event`` persists an immutable row carrying ``category``,
     ``event_type``, ``details``, ``token_id``, ``slug``, ``pnl``,
     ``strategy``, and a non-empty ``idempotency_key``.
  2. ``log_event`` with an explicit ``idempotency_key`` is idempotent —
     re-logging with the SAME key does NOT insert a duplicate row
     (``INSERT OR IGNORE`` contract).
  3. ``log_event`` without an explicit ``idempotency_key`` auto-mints a
     deterministic-shape key (``<category>_<event_type>_<ts>_<random_hex>``)
     that is unique across two consecutive calls.
  4. ``get_recent_events(limit=N)`` returns rows in descending-timestamp
     order (most-recent-first), capped at ``N``.
  5. ``get_recent_events(category=...)`` filters by category — rows from
     OTHER categories never appear in the result.
  6. ``get_recent_events(category="all")`` is treated as no filter — rows
     from every category are eligible.
  7. ``get_recent_events`` on an empty DB returns an empty list (not None).

Isolation
----------
``AuditLogger.__init__`` reads ``DB_PATH`` at construction time. Each test
monkeypatches ``core.audit_logger.DB_PATH`` to a fresh ``tmp_path``-scoped
SQLite file and then constructs a fresh ``AuditLogger()`` — the same
global-lookup code path production uses (``AUDIT_DB_PATH`` env override →
``DB_PATH`` module global → ``__init__``). The module-level singleton
``audit_logger`` (constructed at import time against the conftest-redirected
``AUDIT_DB_PATH``) is left untouched.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling ``tests/test_*.py``).
"""
from __future__ import annotations

import asyncio

import pytest

from core.audit_logger import AuditLogger

pytestmark = pytest.mark.asyncio


@pytest.fixture
def logger(monkeypatch, tmp_path):
    """Fresh ``AuditLogger`` whose SQLite file lives under ``tmp_path``.

    Mirrors the ``ledger`` fixture pattern in ``tests/test_decision_ledger.py``
    — ``DB_PATH`` is monkeypatched so the no-arg ``AuditLogger()`` ctor
    picks up the test path (the same global-lookup path production uses).
    The conftest-redirected global singleton is left untouched.
    """
    db_path = tmp_path / "test_audit_logger.db"
    monkeypatch.setattr("core.audit_logger.DB_PATH", db_path)
    return AuditLogger()


# ── 1. log_event persists a row carrying every supplied field ────────────────
async def test_log_event_persists_all_supplied_fields(logger):
    """``log_event`` must persist every public field exactly as supplied."""
    await logger.log_event(
        category="order",
        event_type="FILL",
        details="Order filled at 0.62 for 100 shares",
        token_id="TOK_A",
        slug="market-a",
        pnl=1.50,
        strategy="ml_sig_v1",
        idempotency_key="test-key-1",
    )

    events = await logger.get_recent_events(limit=10)
    assert len(events) == 1
    ev = events[0]

    assert ev["category"] == "order"
    assert ev["event_type"] == "FILL"
    assert ev["details"] == "Order filled at 0.62 for 100 shares"
    assert ev["token_id"] == "TOK_A"
    assert ev["slug"] == "market-a"
    assert ev["pnl"] == pytest.approx(1.50)
    assert ev["strategy"] == "ml_sig_v1"
    assert ev["idempotency_key"] == "test-key-1"
    assert ev["timestamp"] > 0.0


# ── 2. log_event with explicit idempotency_key is idempotent ─────────────────
async def test_log_event_with_explicit_key_is_idempotent(logger):
    """Re-logging with the SAME ``idempotency_key`` must NOT insert a
    duplicate — the SQLite ``INSERT OR IGNORE`` contract."""
    key = "idem-abc-123"
    for _ in range(3):
        await logger.log_event(
            category="risk",
            event_type="REJECTED",
            details="dup-test",
            idempotency_key=key,
        )

    events = await logger.get_recent_events(limit=100)
    assert len(events) == 1, f"expected 1 row, got {len(events)}"
    assert events[0]["idempotency_key"] == key


# ── 3. log_event without explicit idempotency_key auto-mints unique keys ────
async def test_log_event_auto_mints_unique_keys(logger):
    """When no ``idempotency_key`` is supplied, each call must auto-mint a
    unique key (so two identical log_event calls produce two distinct rows)."""
    for _ in range(5):
        await logger.log_event(
            category="signal",
            event_type="PREDICTION",
            details="auto-key-test",
        )

    events = await logger.get_recent_events(limit=100)
    assert len(events) == 5

    keys = [e["idempotency_key"] for e in events]
    # All five auto-minted keys are distinct — none collided.
    assert len(set(keys)) == 5
    # Each auto-minted key follows the ``<category>_<event_type>_<ts>_<hex>``
    # convention — prefixed with the category / event_type literal.
    for k in keys:
        assert k.startswith("signal_PREDICTION_"), (
            f"auto-minted key {k!r} does not follow the documented convention"
        )


# ── 4. get_recent_events returns rows newest-first ───────────────────────────
async def test_get_recent_events_returns_newest_first(logger):
    """``get_recent_events`` must return rows in descending-timestamp order
    (most-recent-first), capped at the supplied ``limit``."""
    for i in range(5):
        await logger.log_event(
            category="order",
            event_type="CREATED",
            details=f"order-{i}",
            idempotency_key=f"order-{i}",
        )
        await asyncio.sleep(0.01)  # ensure strictly-increasing timestamps

    out = await logger.get_recent_events(limit=3)
    assert len(out) == 3
    # Newest-first: details string carries the loop index, so reverse order.
    assert [e["details"] for e in out] == ["order-4", "order-3", "order-2"]
    # Timestamps are strictly descending.
    ts = [e["timestamp"] for e in out]
    assert ts == sorted(ts, reverse=True)


# ── 5. get_recent_events filters by category ─────────────────────────────────
async def test_get_recent_events_filters_by_category(logger):
    """``get_recent_events(category="risk")`` must return ONLY risk rows —
    rows in OTHER categories (order / signal / ml) must NEVER appear in the
    result, even when they coexist in the same audit_events table."""
    await logger.log_event(
        category="order", event_type="CREATED", details="o",
        idempotency_key="k1",
    )
    await logger.log_event(
        category="risk", event_type="REJECTED", details="r1",
        idempotency_key="k2",
    )
    await logger.log_event(
        category="risk", event_type="STOP_LOSS", details="r2",
        idempotency_key="k3",
    )
    await logger.log_event(
        category="ml", event_type="PREDICTION", details="m",
        idempotency_key="k4",
    )

    risk = await logger.get_recent_events(limit=100, category="risk")
    assert len(risk) == 2
    assert all(r["category"] == "risk" for r in risk)

    # The other two categories are excluded.
    details_set = {r["details"] for r in risk}
    assert details_set == {"r1", "r2"}


# ── 6. get_recent_events treats category="all" as no filter ──────────────────
async def test_get_recent_events_category_all_returns_everything(logger):
    """``category="all"`` (and ``category=None``) must NOT filter — every
    row regardless of its category is eligible."""
    for cat, et in [("order", "A"), ("risk", "B"), ("ml", "C"), ("signal", "D")]:
        await logger.log_event(
            category=cat, event_type=et, details=f"d-{cat}",
            idempotency_key=f"k-{cat}",
        )

    all_rows = await logger.get_recent_events(limit=100, category="all")
    assert len(all_rows) == 4
    cats_seen = {r["category"] for r in all_rows}
    assert cats_seen == {"order", "risk", "ml", "signal"}

    # ``None`` must behave identically (no filter).
    none_rows = await logger.get_recent_events(limit=100, category=None)
    assert len(none_rows) == 4


# ── 7. get_recent_events on an empty DB returns an empty list ────────────────
async def test_get_recent_events_on_empty_db_returns_empty_list(logger):
    """A fresh logger with zero logged events must return ``[]`` — never
    ``None``, never raises."""
    out = await logger.get_recent_events(limit=10)
    assert out == []
    assert out is not None

    # Filtered by category must also return [] on an empty DB.
    out_filtered = await logger.get_recent_events(limit=10, category="risk")
    assert out_filtered == []


# ── 8. pnl defaults to 0.0 when not supplied ────────────────────────────────
async def test_log_event_pnl_defaults_to_zero(logger):
    """``pnl`` defaults to ``0.0`` when the caller omits the kwarg — the
    ``pnl REAL DEFAULT 0.0`` column contract."""
    await logger.log_event(
        category="signal", event_type="CREATED", details="no-pnl-supplied",
        idempotency_key="no-pnl-1",
    )
    events = await logger.get_recent_events(limit=10)
    assert len(events) == 1
    assert events[0]["pnl"] == 0.0


# ── 9. None token_id / slug / strategy are persisted as NULL ──────────────────
async def test_log_event_none_optional_fields_persist_as_null(logger):
    """``token_id``, ``slug``, ``strategy`` are all optional. When the caller
    omits them, the persisted row must carry NULL — NOT an empty string."""
    await logger.log_event(
        category="system", event_type="BOOT", details="system boot",
        idempotency_key="boot-1",
    )
    events = await logger.get_recent_events(limit=10)
    assert len(events) == 1
    ev = events[0]
    # All three optional fields were omitted → persisted as NULL (Python None).
    assert ev["token_id"] is None
    assert ev["slug"] is None
    assert ev["strategy"] is None


# ── 10. limit parameter caps the returned row count ──────────────────────────
async def test_get_recent_events_limit_caps_returned_rows(logger):
    """``limit=N`` must cap the returned list at N rows — even when more
    rows exist in the DB."""
    for i in range(10):
        await logger.log_event(
            category="order", event_type="CREATED", details=f"o-{i}",
            idempotency_key=f"k-{i}",
        )

    out = await logger.get_recent_events(limit=3)
    assert len(out) == 3

    # limit=0 returns an empty list (not a SELECT *, which would ignore the
    # cap — the parameter is bound directly into LIMIT ?).
    out_zero = await logger.get_recent_events(limit=0)
    assert out_zero == []
