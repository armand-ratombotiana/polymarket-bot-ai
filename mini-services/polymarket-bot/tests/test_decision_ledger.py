"""
Unit tests for ``core/decision_ledger.py``.

S9 — Decision Ledger unit tests.

Covers the six public-surface guarantees of the unified decision ledger:

  1. ``DecisionLedger.new_decision_id()`` returns unique ids.
  2. ``record()`` persists stage events with the correct ``stage`` /
     ``data`` payload (and ``token_id`` / ``strategy`` / ``pnl`` columns).
  3. ``get_chain(decision_id)`` returns events in ascending-timestamp order.
  4. ``get_chain_by_token(token_id)`` returns the event chain scoped to a
     single token (newest-first).
  5. ``record_rejection()`` persists a rejection row carrying
     ``predicted_edge`` and ``reason`` (and also emits a
     ``RISK_REJECTED`` stage event on the main chain).
  6. ``get_rejections()`` reads ONLY from the ``decision_rejections`` table
     — i.e. it never returns regular ``PREDICTION`` / ``SIGNAL`` / ``ORDER``
     / ``FILL`` stage events even when they coexist in ``decision_events``.

The decision-ledger module reads its DB path from a module-level
``DB_PATH`` constant at construction time. Each test monkeypatches
``core.decision_ledger.DB_PATH`` to a fresh ``tmp_path``-scoped SQLite file
and then constructs a ``DecisionLedger()`` (no constructor arg) — this
exercises the same code path production uses (``DB_PATH`` resolved inside
``__init__``) while keeping tests hermetic.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (pytest-asyncio is already a project
dependency — see ``core/audit_logger.py`` for the same async+sqlite
pattern this ledger mirrors).
"""
from __future__ import annotations

import asyncio

import pytest

from core.decision_ledger import (
    REASON_LOW_CONFIDENCE,
    REASON_NEUTRAL_ZONE,
    STAGE_FILL,
    STAGE_ORDER,
    STAGE_PREDICTION,
    STAGE_RISK_APPROVED,
    STAGE_RISK_REJECTED,
    STAGE_SIGNAL,
    DecisionLedger,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the S9 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed ledger per test ───────────────────────────
@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """
    Return a ``DecisionLedger`` whose SQLite file lives under ``tmp_path``.

    ``DB_PATH`` is monkeypatched so the no-arg ``DecisionLedger()``
    constructor picks up the test path — the same global-lookup code path
    production uses (``DECISION_LEDGER_DB_PATH`` env var override →
    ``DB_PATH`` module global → ``__init__``). This avoids touching the
    singleton ``decision_ledger`` constructed at module-import time
    (which is left in its production /app/data state).
    """
    db_path = tmp_path / "test_decision_ledger.db"
    monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
    return DecisionLedger()


# ── 1. new_decision_id returns unique IDs ───────────────────────────────────
async def test_new_decision_id_returns_unique_ids():
    """``new_decision_id()`` must produce globally-unique, sortable ids."""
    ids = [DecisionLedger.new_decision_id() for _ in range(1000)]

    # (a) All 1000 ids are distinct — uuid4 collision probability is ~0,
    #     but this is the contract we're verifying.
    assert len(set(ids)) == 1000

    # (b) Canonical shape: "dec-" prefix + 32 hex chars (uuid4 .hex).
    for did in ids:
        assert did.startswith("dec-")
        assert len(did) == 4 + 32  # len("dec-") + len(uuid4.hex)
        # hex body is lowercase hex
        hex_body = did[len("dec-"):]
        assert all(c in "0123456789abcdef" for c in hex_body)


# ── 2. record() stores events with correct stage / data ────────────────────
async def test_record_stores_events_with_correct_stage_and_data(ledger):
    """``record()`` must persist the stage, token, strategy, and ``**data``
    payload exactly as the caller supplied them."""
    did = DecisionLedger.new_decision_id()

    await ledger.record(
        did,
        STAGE_PREDICTION,
        token_id="TOK_A",
        strategy="ml_sig_v1",
        p_yes=0.62,
        confidence=0.24,
        predicted_edge=0.08,
    )

    chain = await ledger.get_chain(did)

    # Exactly one event was written.
    assert len(chain) == 1
    ev = chain[0]

    # Identity columns persisted verbatim.
    assert ev["decision_id"] == did
    assert ev["stage"] == STAGE_PREDICTION
    assert ev["token_id"] == "TOK_A"
    assert ev["strategy"] == "ml_sig_v1"

    # ``pnl`` defaults to 0.0 when not supplied.
    assert ev["pnl"] == 0.0

    # ``**data`` was JSON-serialised into ``data_json`` and decoded back
    # into a ``data`` dict on read (mirrors the production convenience
    # field surfaced by ``get_chain``).
    assert isinstance(ev["data"], dict)
    assert ev["data"]["p_yes"] == pytest.approx(0.62)
    assert ev["data"]["confidence"] == pytest.approx(0.24)
    assert ev["data"]["predicted_edge"] == pytest.approx(0.08)


# ── 3. get_chain() returns events in timestamp order ───────────────────────
async def test_get_chain_returns_events_in_timestamp_order(ledger):
    """``get_chain`` must return stage events in ascending-timestamp order
    (the chronological order in which they were recorded)."""
    did = DecisionLedger.new_decision_id()
    stages = [
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ]

    # Record each stage with a tiny sleep so each event lands at a strictly
    # greater ``time.time()`` value (SQLite stores REAL with ~µs precision;
    # 5 ms is a comfortable margin even on a heavily-loaded CI box).
    for stage in stages:
        await ledger.record(did, stage, token_id="TOK_A", strategy="s")
        await asyncio.sleep(0.005)

    chain = await ledger.get_chain(did)

    # Five events recorded → five events returned.
    assert len(chain) == 5

    # Stage order matches insertion order (chronological).
    assert [e["stage"] for e in chain] == stages

    # Timestamps are monotonically non-decreasing.
    timestamps = [e["timestamp"] for e in chain]
    assert timestamps == sorted(timestamps)

    # All events share the same decision_id (sanity check).
    assert all(e["decision_id"] == did for e in chain)


# ── 4. get_chain_by_token() returns chains for a token ────────────────────
async def test_get_chain_by_token_returns_chains_for_token(ledger):
    """``get_chain_by_token`` must return only the events for the requested
    token, most-recent-first."""
    did_a = DecisionLedger.new_decision_id()
    did_b = DecisionLedger.new_decision_id()

    # Three events on TOK_A (two different decision ids) + one on TOK_B.
    await ledger.record(did_a, STAGE_PREDICTION, token_id="TOK_A", strategy="s")
    await asyncio.sleep(0.005)
    await ledger.record(did_b, STAGE_PREDICTION, token_id="TOK_B", strategy="s")
    await asyncio.sleep(0.005)
    await ledger.record(did_a, STAGE_SIGNAL, token_id="TOK_A", strategy="s")

    chain_a = await ledger.get_chain_by_token("TOK_A")
    chain_b = await ledger.get_chain_by_token("TOK_B")

    # Token-filter is correct.
    assert len(chain_a) == 2
    assert len(chain_b) == 1
    assert all(e["token_id"] == "TOK_A" for e in chain_a)
    assert all(e["token_id"] == "TOK_B" for e in chain_b)

    # Newest-first ordering (DESC by timestamp).
    assert chain_a[0]["stage"] == STAGE_SIGNAL
    assert chain_a[1]["stage"] == STAGE_PREDICTION
    assert chain_a[0]["timestamp"] >= chain_a[1]["timestamp"]

    # Unknown token → empty list (the API's 404 path depends on this).
    assert await ledger.get_chain_by_token("UNKNOWN_TOKEN") == []


# ── 5. record_rejection() stores rejection with predicted_edge + reason ───
async def test_record_rejection_stores_predicted_edge_and_reason(ledger):
    """``record_rejection`` must persist ``predicted_edge``, ``reason``,
    ``confidence``, ``market_mid`` etc. — both on the ``RISK_REJECTED``
    chain event AND in the ``decision_rejections`` fast-listing table."""
    did = DecisionLedger.new_decision_id()

    await ledger.record_rejection(
        token_id="TOK_REJ",
        strategy="ml_sig_v1",
        predicted_edge=0.05,
        confidence=0.12,
        reason=REASON_LOW_CONFIDENCE,
        market_mid=0.55,
        decision_id=did,
    )

    # (a) The originating decision chain now contains exactly one stage
    #     event: RISK_REJECTED, carrying predicted_edge + reason in its
    #     ``data`` payload.
    chain = await ledger.get_chain(did)
    assert len(chain) == 1

    rej_event = chain[0]
    assert rej_event["stage"] == STAGE_RISK_REJECTED
    assert rej_event["token_id"] == "TOK_REJ"
    assert rej_event["strategy"] == "ml_sig_v1"

    # ``data`` payload mirrors the kwargs passed to record_rejection().
    assert rej_event["data"]["predicted_edge"] == pytest.approx(0.05)
    assert rej_event["data"]["confidence"] == pytest.approx(0.12)
    assert rej_event["data"]["reason"] == REASON_LOW_CONFIDENCE
    assert rej_event["data"]["market_mid"] == pytest.approx(0.55)

    # (b) The decision_rejections table also carries a row (this is the
    #     fast-filtered listing the dashboard reads from). We assert it
    #     via get_rejections() since record_rejection's contract is that
    #     BOTH stores are written.
    rejs = await ledger.get_rejections()
    matching = [r for r in rejs if r["decision_id"] == did]
    assert len(matching) == 1

    rej_row = matching[0]
    assert rej_row["token_id"] == "TOK_REJ"
    assert rej_row["strategy"] == "ml_sig_v1"
    assert rej_row["predicted_edge"] == pytest.approx(0.05)
    assert rej_row["confidence"] == pytest.approx(0.12)
    assert rej_row["reason"] == REASON_LOW_CONFIDENCE
    assert rej_row["market_mid"] == pytest.approx(0.55)


# ── 6. get_rejections() returns only REJECTED stage events ─────────────────
async def test_get_rejections_returns_only_rejected_stage_events(ledger):
    """``get_rejections()`` reads from the ``decision_rejections`` table
    only — it must NEVER return regular PREDICTION/SIGNAL/ORDER/FILL stage
    events, even when many such events coexist in ``decision_events``."""
    did_happy = DecisionLedger.new_decision_id()
    did_rej = DecisionLedger.new_decision_id()

    # Write a full happy-path stage chain (5 events on decision_events).
    for stage in [
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ]:
        await ledger.record(
            did_happy, stage, token_id="TOK_HAPPY", strategy="s", pnl=1.5
        )
        await asyncio.sleep(0.005)

    # Write one rejection. record_rejection emits BOTH a RISK_REJECTED
    # stage event on decision_events AND a row in decision_rejections.
    await ledger.record_rejection(
        token_id="TOK_REJ",
        strategy="ml_sig_v1",
        predicted_edge=0.03,
        confidence=0.08,
        reason=REASON_NEUTRAL_ZONE,
        market_mid=0.51,
        decision_id=did_rej,
    )

    # Sanity: decision_events now has 6 rows (5 happy-path + 1 RISK_REJECTED).
    happy_chain = await ledger.get_chain(did_happy)
    rej_chain = await ledger.get_chain(did_rej)
    assert len(happy_chain) == 5
    assert len(rej_chain) == 1
    assert rej_chain[0]["stage"] == STAGE_RISK_REJECTED

    # get_rejections() must return exactly ONE row — the rejection row from
    # decision_rejections. The 5 happy-path stage events must NOT appear
    # here (they live in decision_events, a separate table).
    rejs = await ledger.get_rejections()
    assert len(rejs) == 1

    rej = rejs[0]
    # All rejection-schema columns are populated.
    assert rej["decision_id"] == did_rej
    assert rej["token_id"] == "TOK_REJ"
    assert rej["strategy"] == "ml_sig_v1"
    assert rej["predicted_edge"] == pytest.approx(0.03)
    assert rej["confidence"] == pytest.approx(0.08)
    assert rej["reason"] == REASON_NEUTRAL_ZONE
    assert rej["market_mid"] == pytest.approx(0.51)
    # The happy-path decision_id must NOT surface in the rejection listing.
    assert all(r["decision_id"] != did_happy for r in rejs)
