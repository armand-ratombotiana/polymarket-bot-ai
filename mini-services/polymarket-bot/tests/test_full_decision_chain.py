"""
tests/test_full_decision_chain.py — W19-3 tests for the complete 12-stage
unified decision ledger chain.

The God Mode §51 assessment found the decision ledger implemented only 6 of
12 stages:

    ✅ PREDICTION, SIGNAL, RISK_APPROVED/REJECTED, ORDER, FILL
    ❌ MARKET_SNAPSHOT, INTELLIGENCE_SNAPSHOT, FEATURE_SNAPSHOT, POSITION,
       OUTCOME, P&L

W19-3 closes that gap. These tests verify:

  (1) Each of the 6 new ``record_*`` helpers persists a row with the correct
      ``stage`` / ``token_id`` / payload — one test per helper.
  (2) ``get_full_chain(correlation_id)`` reconstructs the chain as a
      ``{stage_name: stage_event}`` dict and is empty / ``{}`` when the
      correlation_id has no events.
  (3) A complete 12-stage chain (all 12 stages recorded under one
      ``decision_id``) round-trips through ``get_full_chain`` — the dict's
      key set equals the full ``CANONICAL_STAGE_ORDER`` minus the
      RISK_REJECTED branch (a happy-path chain has RISK_APPROVED, not
      RISK_REJECTED).
  (4) ``get_latest_decision_id_for_token`` returns the most recent
      ``decision_id`` for a token (optionally filtered by stage), used by
      ``core/settlement`` to find the originating chain for a settled
      position.
  (5) The HTTP route ``GET /api/decision/{correlation_id}/full-chain``
      returns 200 with the full stage dict on hit, 404 on miss.

Hermeticity
-----------
Mirrors ``tests/test_decision_ledger.py``: each test monkeypatches
``core.decision_ledger.DB_PATH`` to a fresh ``tmp_path``-scoped SQLite file
so the no-arg ``DecisionLedger()`` constructor (the same code path
production uses) picks up the test path. The module-level singleton
``decision_ledger`` is left in its conftest-redirected state.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import decision_ledger as decision_ledger_module
from core.decision_ledger import (
    CANONICAL_STAGE_ORDER,
    STAGE_FILL,
    STAGE_FEATURE_SNAPSHOT,
    STAGE_INTELLIGENCE_SNAPSHOT,
    STAGE_MARKET_SNAPSHOT,
    STAGE_ORDER,
    STAGE_OUTCOME,
    STAGE_PNL,
    STAGE_POSITION,
    STAGE_PREDICTION,
    STAGE_RISK_APPROVED,
    STAGE_SIGNAL,
    DecisionLedger,
    register_routes,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module — mirrors the convention in ``tests/test_decision_ledger.py``.
#
# Note: the API-route tests below (``TestFullChainRoute``) are synchronous
# (they use ``fastapi.testclient.TestClient``, which is itself sync). They
# do NOT carry the ``@pytest.mark.asyncio`` decorator, so a module-level
# ``pytestmark = pytest.mark.asyncio`` would emit a PytestWarning on every
# sync test ("marked with asyncio but not async"). Instead each async
# test carries its own decorator. Mirrors the convention in
# ``tests/test_immutable_audit.py``.
def _asyncio_mark():
    """Helper to apply ``@pytest.mark.asyncio`` (kept here as a single
    declaration point so the async-test convention is grep-able)."""
    return pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed ledger per test ───────────────────────────
@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Return a ``DecisionLedger`` whose SQLite file lives under ``tmp_path``.

    ``DB_PATH`` is monkeypatched so the no-arg ``DecisionLedger()``
    constructor picks up the test path — the same global-lookup code path
    production uses (``DECISION_LEDGER_DB_PATH`` env var override →
    ``DB_PATH`` module global → ``__init__``).
    """
    db_path = tmp_path / "test_full_decision_chain.db"
    monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
    return DecisionLedger()


# ── (1) Per-helper record tests ─────────────────────────────────────────────


@_asyncio_mark()
async def test_record_market_snapshot_persists_stage_and_payload(ledger):
    """``record_market_snapshot`` writes a MARKET_SNAPSHOT row whose
    ``data`` payload preserves the caller-supplied snapshot fields
    verbatim."""
    did = DecisionLedger.new_decision_id()
    snapshot = {
        "mid": 0.5,
        "spread": 0.02,
        "best_bid": 0.49,
        "best_ask": 0.51,
        "bid_depth_top3": [{"price": 0.49, "size": 500.0}],
        "ask_depth_top3": [{"price": 0.51, "size": 500.0}],
    }

    await ledger.record_market_snapshot(
        correlation_id=did,
        token_id="TOK_MKT",
        strategy="signal_trader",
        snapshot=snapshot,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_MARKET_SNAPSHOT
    assert ev["decision_id"] == did
    assert ev["token_id"] == "TOK_MKT"
    assert ev["strategy"] == "signal_trader"
    # Default pnl column is 0.0 for snapshot-style stages.
    assert ev["pnl"] == 0.0
    # Payload is preserved verbatim (after reserved-key strip).
    assert ev["data"]["mid"] == pytest.approx(0.5)
    assert ev["data"]["spread"] == pytest.approx(0.02)
    assert ev["data"]["best_bid"] == pytest.approx(0.49)
    assert ev["data"]["best_ask"] == pytest.approx(0.51)
    assert ev["data"]["bid_depth_top3"] == snapshot["bid_depth_top3"]


@_asyncio_mark()
async def test_record_intelligence_snapshot_persists_stage_and_payload(ledger):
    """``record_intelligence_snapshot`` writes an INTELLIGENCE_SNAPSHOT row."""
    did = DecisionLedger.new_decision_id()
    snapshot = {
        "slug": "test-market",
        "volume24hr": 1234.56,
        "liquidity": 9999.0,
        "active": True,
        "closed": False,
        "end_date": "2026-12-31",
    }

    await ledger.record_intelligence_snapshot(
        correlation_id=did,
        token_id="TOK_INT",
        strategy="signal_trader",
        snapshot=snapshot,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_INTELLIGENCE_SNAPSHOT
    assert ev["token_id"] == "TOK_INT"
    assert ev["data"]["slug"] == "test-market"
    assert ev["data"]["volume24hr"] == pytest.approx(1234.56)
    assert ev["data"]["liquidity"] == pytest.approx(9999.0)
    assert ev["data"]["active"] is True
    assert ev["data"]["closed"] is False
    assert ev["data"]["end_date"] == "2026-12-31"


@_asyncio_mark()
async def test_record_feature_snapshot_persists_stage_and_payload(ledger):
    """``record_feature_snapshot`` writes a FEATURE_SNAPSHOT row carrying the
    feature vector + feature-store metadata."""
    did = DecisionLedger.new_decision_id()
    features = {
        "features": [0.1, 0.2, 0.3, 0.4, 0.5],
        "n_features": 5,
        "feature_set_version": "v1.2.0",
    }

    await ledger.record_feature_snapshot(
        correlation_id=did,
        token_id="TOK_FEAT",
        strategy="signal_trader",
        features=features,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_FEATURE_SNAPSHOT
    assert ev["data"]["features"] == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert ev["data"]["n_features"] == 5
    assert ev["data"]["feature_set_version"] == "v1.2.0"


@_asyncio_mark()
async def test_record_position_promotes_pnl_to_dedicated_column(ledger):
    """``record_position`` writes a POSITION row whose ``pnl`` column is
    populated from the position dict's ``pnl`` key (rather than landing
    silently inside ``data_json``)."""
    did = DecisionLedger.new_decision_id()
    position = {
        "yes_shares": 10.0,
        "avg_entry_price": 0.51,
        "total_invested": 5.1,
        "opened_at": 1700000000.0,
        "paper": True,
        # The closing-SELL realised P&L on this position — promoted to the
        # dedicated ``pnl`` column by ``record_position``.
        "pnl": 1.50,
    }

    await ledger.record_position(
        correlation_id=did,
        token_id="TOK_POS",
        strategy="signal_trader",
        position=position,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_POSITION
    # The ``pnl`` column is populated (promoted from the payload's ``pnl`` key)
    # so the dashboard's ``SELECT SUM(pnl) WHERE stage='POSITION'`` query works
    # out-of-the-box.
    assert ev["pnl"] == pytest.approx(1.50)
    # The remaining payload lands in ``data`` — minus the promoted ``pnl`` key.
    assert "pnl" not in ev["data"]
    assert ev["data"]["yes_shares"] == pytest.approx(10.0)
    assert ev["data"]["avg_entry_price"] == pytest.approx(0.51)
    assert ev["data"]["total_invested"] == pytest.approx(5.1)
    assert ev["data"]["paper"] is True


@_asyncio_mark()
async def test_record_outcome_persists_market_resolution(ledger):
    """``record_outcome`` writes an OUTCOME row capturing the market
    resolution outcome (resolved_yes / resolution_price / slug / etc.)."""
    did = DecisionLedger.new_decision_id()
    outcome = {
        "resolved_yes": True,
        "resolution_price": 1.0,
        "slug": "test-resolved-market",
        "shares_at_settlement": 10.0,
        "settled_at": 1700000000.0,
        "settlement_strategy": "settlement",
    }

    await ledger.record_outcome(
        correlation_id=did,
        token_id="TOK_OUT",
        strategy="settlement",
        outcome=outcome,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_OUTCOME
    assert ev["pnl"] == 0.0  # OUTCOME events carry no P&L (PNL stage does)
    assert ev["data"]["resolved_yes"] is True
    assert ev["data"]["resolution_price"] == pytest.approx(1.0)
    assert ev["data"]["slug"] == "test-resolved-market"
    assert ev["data"]["shares_at_settlement"] == pytest.approx(10.0)
    assert ev["data"]["settlement_strategy"] == "settlement"


@_asyncio_mark()
async def test_record_pnl_promotes_realized_pnl_to_dedicated_column(ledger):
    """``record_pnl`` writes a PNL row whose ``pnl`` column is populated
    from the dict's ``realized_pnl`` key (preferred) or ``pnl`` /
    ``pnl_amount`` fallbacks. The remaining payload lands in ``data``."""
    did = DecisionLedger.new_decision_id()
    pnl_dict = {
        "realized_pnl": 4.90,
        "payout": 10.0,
        "invested_cost": 5.1,
        "shares": 10.0,
        "exit_price": 1.0,
        "entry_price": 0.51,
        "resolution": "WINNER",
    }

    await ledger.record_pnl(
        correlation_id=did,
        token_id="TOK_PNL",
        strategy="settlement",
        pnl=pnl_dict,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    assert ev["stage"] == STAGE_PNL
    # ``realized_pnl`` is promoted to the dedicated ``pnl`` column.
    assert ev["pnl"] == pytest.approx(4.90)
    # The promoted key is dropped from the data payload (no duplication).
    assert "realized_pnl" not in ev["data"]
    assert "pnl" not in ev["data"]
    assert ev["data"]["payout"] == pytest.approx(10.0)
    assert ev["data"]["invested_cost"] == pytest.approx(5.1)
    assert ev["data"]["shares"] == pytest.approx(10.0)
    assert ev["data"]["resolution"] == "WINNER"


@_asyncio_mark()
async def test_record_pnl_falls_back_to_pnl_key_when_realized_pnl_absent(ledger):
    """When the dict lacks ``realized_pnl`` but carries a plain ``pnl``
    key, the helper falls back to the ``pnl`` key for the dedicated column."""
    did = DecisionLedger.new_decision_id()
    await ledger.record_pnl(
        correlation_id=did,
        token_id="TOK_PNL2",
        strategy="settlement",
        pnl={"pnl": 2.5, "payout": 7.5, "invested_cost": 5.0},
    )
    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    assert chain[0]["pnl"] == pytest.approx(2.5)
    assert "pnl" not in chain[0]["data"]


@_asyncio_mark()
async def test_record_helpers_noop_on_empty_correlation_id(ledger):
    """A falsy ``correlation_id`` (``""``) is a no-op for every helper —
    mirrors the existing ``record()`` guard against missing decision_ids
    (legacy / manual orders that don't participate in the unified ledger)."""
    # Each helper takes a different positional payload-arg name
    # (snapshot / features / position / outcome / pnl), so we exercise
    # them individually rather than via a uniform positional loop.
    await ledger.record_market_snapshot(
        correlation_id="", token_id="TOK_EMPTY", snapshot={"mid": 0.5}
    )
    await ledger.record_intelligence_snapshot(
        correlation_id="", token_id="TOK_EMPTY", snapshot={"slug": "x"}
    )
    await ledger.record_feature_snapshot(
        correlation_id="", token_id="TOK_EMPTY", features={"features": [0.1]}
    )
    await ledger.record_position(
        correlation_id="", token_id="TOK_EMPTY", position={"yes_shares": 1.0}
    )
    await ledger.record_outcome(
        correlation_id="", token_id="TOK_EMPTY", outcome={"resolved_yes": True}
    )
    await ledger.record_pnl(
        correlation_id="", token_id="TOK_EMPTY", pnl={"realized_pnl": 1.0}
    )
    # No events were persisted for any token.
    chain = await ledger.get_chain("TOK_EMPTY")  # not a real decision_id
    assert chain == []


@_asyncio_mark()
async def test_record_helpers_strip_reserved_keys_from_payload(ledger):
    """When a snapshot dict happens to carry a reserved-key name
    (``token_id`` / ``strategy`` / ``pnl`` / ``decision_id`` / ``stage``),
    the helper silently drops it from the ``**`` expansion so the snapshot
    doesn't raise ``TypeError: got multiple values for keyword argument``
    on ``record()``. The caller's explicit positional ``token_id`` wins."""
    did = DecisionLedger.new_decision_id()
    snapshot = {
        "mid": 0.5,
        # Reserved-key collisions — should be silently dropped.
        "token_id": "should_be_dropped",
        "strategy": "should_be_dropped",
        "stage": "should_be_dropped",
        "decision_id": "should_be_dropped",
        "pnl": 99.99,
        # Non-reserved key — preserved verbatim.
        "spread": 0.02,
    }

    await ledger.record_market_snapshot(
        correlation_id=did,
        token_id="TOK_COLLISION",
        strategy="signal_trader",
        snapshot=snapshot,
    )

    chain = await ledger.get_chain(did)
    assert len(chain) == 1
    ev = chain[0]
    # Caller's positional args win.
    assert ev["token_id"] == "TOK_COLLISION"
    assert ev["strategy"] == "signal_trader"
    assert ev["stage"] == STAGE_MARKET_SNAPSHOT
    assert ev["decision_id"] == did
    # The default ``pnl`` column for MARKET_SNAPSHOT is 0.0 (snapshot-style
    # stage), not 99.99 — the snapshot's own ``pnl`` key was stripped.
    assert ev["pnl"] == 0.0
    # Non-reserved keys are preserved.
    assert ev["data"]["mid"] == pytest.approx(0.5)
    assert ev["data"]["spread"] == pytest.approx(0.02)
    # Reserved keys were stripped from the payload.
    assert "token_id" not in ev["data"]
    assert "strategy" not in ev["data"]
    assert "stage" not in ev["data"]
    assert "decision_id" not in ev["data"]
    assert "pnl" not in ev["data"]


# ── (2) get_full_chain ──────────────────────────────────────────────────────


@_asyncio_mark()
async def test_get_full_chain_returns_empty_dict_for_unknown_correlation(ledger):
    """``get_full_chain`` returns ``{}`` for an unknown correlation_id
    (no rows fetched, no error raised)."""
    chain = await ledger.get_full_chain("dec-nonexistent-id")
    assert chain == {}


@_asyncio_mark()
async def test_get_full_chain_returns_empty_dict_for_empty_input(ledger):
    """``get_full_chain("")`` is a no-op returning ``{}``."""
    chain = await ledger.get_full_chain("")
    assert chain == {}


@_asyncio_mark()
async def test_get_full_chain_keys_stages_by_stage_name(ledger):
    """``get_full_chain`` returns a ``{stage_name: stage_event}`` dict
    for every stage recorded against ``correlation_id``."""
    did = DecisionLedger.new_decision_id()

    # Record three of the new snapshot-style stages.
    await ledger.record_market_snapshot(did, "TOK_GC", snapshot={"mid": 0.5})
    await ledger.record_intelligence_snapshot(did, "TOK_GC", snapshot={"slug": "x"})
    await ledger.record_feature_snapshot(did, "TOK_GC", features={"features": [1.0]})

    chain = await ledger.get_full_chain(did)
    assert isinstance(chain, dict)
    assert set(chain.keys()) == {
        STAGE_MARKET_SNAPSHOT,
        STAGE_INTELLIGENCE_SNAPSHOT,
        STAGE_FEATURE_SNAPSHOT,
    }
    # Each value carries the full event row.
    for stage_name, ev in chain.items():
        assert ev["stage"] == stage_name
        assert ev["decision_id"] == did
        assert ev["token_id"] == "TOK_GC"
        assert "data" in ev
        assert "timestamp" in ev


@_asyncio_mark()
async def test_get_full_chain_last_write_wins_for_repeated_stage(ledger):
    """When the same stage name is recorded multiple times against a
    correlation_id, the LAST event wins (later timestamp) — matching
    the dashboard's "show me the latest snapshot per stage" expectation."""
    did = DecisionLedger.new_decision_id()

    await ledger.record_market_snapshot(did, "TOK_DUP", snapshot={"mid": 0.5, "tag": "first"})
    await asyncio.sleep(0.005)
    await ledger.record_market_snapshot(did, "TOK_DUP", snapshot={"mid": 0.6, "tag": "second"})

    chain = await ledger.get_full_chain(did)
    # Only one MARKET_SNAPSHOT key in the dict.
    assert STAGE_MARKET_SNAPSHOT in chain
    ev = chain[STAGE_MARKET_SNAPSHOT]
    # The second (later) event wins.
    assert ev["data"]["tag"] == "second"
    assert ev["data"]["mid"] == pytest.approx(0.6)


# ── (3) Complete 12-stage chain reconstruction ──────────────────────────────


@_asyncio_mark()
async def test_full_12_stage_chain_reconstructs_via_get_full_chain(ledger):
    """Record every stage in ``CANONICAL_STAGE_ORDER`` (minus the mutually
    exclusive RISK_REJECTED — the happy path takes RISK_APPROVED instead)
    against a single ``decision_id`` and verify ``get_full_chain`` returns
    one entry per stage.

    This is the headline W19-3 acceptance test — it proves the ledger can
    finally answer "Why did the bot make this trade?" for the full chain
    from market data to realized P&L (God Mode §51 closure).
    """
    did = DecisionLedger.new_decision_id()
    token_id = "TOK_FULL_CHAIN"
    strategy = "signal_trader"

    # 1. Pre-prediction snapshots.
    await ledger.record_market_snapshot(
        did, token_id, strategy=strategy,
        snapshot={"mid": 0.5, "spread": 0.02, "best_bid": 0.49, "best_ask": 0.51},
    )
    await asyncio.sleep(0.001)
    await ledger.record_intelligence_snapshot(
        did, token_id, strategy=strategy,
        snapshot={"slug": "test-market", "volume24hr": 1000.0, "active": True},
    )
    await asyncio.sleep(0.001)
    await ledger.record_feature_snapshot(
        did, token_id, strategy=strategy,
        features={"features": [0.1, 0.2, 0.3], "n_features": 3, "feature_set_version": "v1"},
    )
    await asyncio.sleep(0.001)

    # 2. Original 6 stages (PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL).
    await ledger.record(
        decision_id=did, stage=STAGE_PREDICTION, token_id=token_id,
        strategy=strategy, pnl=0.0,
        p_yes=0.65, confidence=0.7, predicted_edge=0.15,
    )
    await asyncio.sleep(0.001)
    await ledger.record(
        decision_id=did, stage=STAGE_SIGNAL, token_id=token_id,
        strategy=strategy, pnl=0.0,
        direction="BUY", target_price=0.511, size_usdc=1.50,
    )
    await asyncio.sleep(0.001)
    await ledger.record(
        decision_id=did, stage=STAGE_RISK_APPROVED, token_id=token_id,
        strategy=strategy, pnl=0.0,
        side="BUY", price=0.511, size=2.93,
    )
    await asyncio.sleep(0.001)
    await ledger.record(
        decision_id=did, stage=STAGE_ORDER, token_id=token_id,
        strategy=strategy, pnl=0.0,
        order_id="paper-test-order", side="BUY", price=0.511, size=2.93,
    )
    await asyncio.sleep(0.001)
    await ledger.record(
        decision_id=did, stage=STAGE_FILL, token_id=token_id,
        strategy=strategy, pnl=0.0,
        fill_price=0.52, fill_size=2.93, side="BUY", order_id="paper-test-order",
    )
    await asyncio.sleep(0.001)

    # 3. Post-fill POSITION stage.
    await ledger.record_position(
        did, token_id, strategy=strategy,
        position={
            "yes_shares": 2.93,
            "avg_entry_price": 0.52,
            "total_invested": 1.5236,
            "opened_at": 1700000000.0,
            "paper": True,
            "pnl": 0.0,  # Opening BUY — no P&L yet.
        },
    )
    await asyncio.sleep(0.001)

    # 4. Market resolution: OUTCOME + PNL stages.
    await ledger.record_outcome(
        did, token_id, strategy="settlement",
        outcome={
            "resolved_yes": True,
            "resolution_price": 1.0,
            "slug": "test-market",
            "shares_at_settlement": 2.93,
            "settled_at": 1700000100.0,
            "settlement_strategy": "settlement",
        },
    )
    await asyncio.sleep(0.001)
    await ledger.record_pnl(
        did, token_id, strategy="settlement",
        pnl={
            "realized_pnl": 1.4064,  # 2.93 * 1.0 - 1.5236
            "payout": 2.93,
            "invested_cost": 1.5236,
            "shares": 2.93,
            "exit_price": 1.0,
            "entry_price": 0.52,
            "resolution": "WINNER",
        },
    )

    # ── Verify the full chain ──────────────────────────────────────────
    chain = await ledger.get_full_chain(did)
    expected_stages = {
        STAGE_MARKET_SNAPSHOT,
        STAGE_INTELLIGENCE_SNAPSHOT,
        STAGE_FEATURE_SNAPSHOT,
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,  # happy-path branch (not RISK_REJECTED)
        STAGE_ORDER,
        STAGE_FILL,
        STAGE_POSITION,
        STAGE_OUTCOME,
        STAGE_PNL,
    }
    assert set(chain.keys()) == expected_stages, (
        f"missing stages in chain: {expected_stages - set(chain.keys())}; "
        f"extra stages: {set(chain.keys()) - expected_stages}"
    )
    # All 11 happy-path stages (excludes the mutually exclusive RISK_REJECTED).
    assert len(chain) == 11

    # Every event row carries the same correlation_id + token_id.
    for ev in chain.values():
        assert ev["decision_id"] == did
        assert ev["token_id"] == token_id

    # The PNL event's dedicated ``pnl`` column is populated.
    assert chain[STAGE_PNL]["pnl"] == pytest.approx(1.4064)
    # The POSITION event's pnl column is 0.0 (opening BUY).
    assert chain[STAGE_POSITION]["pnl"] == pytest.approx(0.0)
    # The OUTCOME event captures the market resolution.
    assert chain[STAGE_OUTCOME]["data"]["resolved_yes"] is True
    # The MARKET_SNAPSHOT preserves the original book state.
    assert chain[STAGE_MARKET_SNAPSHOT]["data"]["mid"] == pytest.approx(0.5)
    # The FEATURE_SNAPSHOT preserves the feature vector.
    assert chain[STAGE_FEATURE_SNAPSHOT]["data"]["features"] == [0.1, 0.2, 0.3]
    # The INTELLIGENCE_SNAPSHOT preserves market metadata.
    assert chain[STAGE_INTELLIGENCE_SNAPSHOT]["data"]["slug"] == "test-market"

    # Cross-check: the chain is fully reconstructable via the canonical
    # stage ordering tuple. Every stage in CANONICAL_STAGE_ORDER except
    # RISK_REJECTED is present.
    canonical_present = {
        s for s in CANONICAL_STAGE_ORDER if s in chain
    }
    assert canonical_present == expected_stages


@_asyncio_mark()
async def test_rejected_chain_terminates_with_risk_rejected_not_order(ledger):
    """A rejected decision chain has PREDICTION + RISK_REJECTED but
    no SIGNAL / ORDER / FILL / POSITION / OUTCOME / PNL. ``get_full_chain``
    returns only the stages actually recorded (no placeholder entries
    for absent stages)."""
    did = DecisionLedger.new_decision_id()
    token_id = "TOK_REJ_CHAIN"

    # Pre-prediction snapshots fire for every evaluated market.
    await ledger.record_market_snapshot(did, token_id, snapshot={"mid": 0.5})
    await ledger.record_intelligence_snapshot(did, token_id, snapshot={"slug": "x"})
    await ledger.record_feature_snapshot(did, token_id, features={"features": [0.1]})

    # PREDICTION fires for every evaluated market.
    await ledger.record(
        decision_id=did, stage=STAGE_PREDICTION, token_id=token_id,
        strategy="signal_trader", p_yes=0.5, confidence=0.4,
    )

    # RISK_REJECTED fires on the rejection path (via record_rejection or
    # direct record()).
    await ledger.record_rejection(
        token_id=token_id,
        strategy="signal_trader",
        predicted_edge=0.0,
        confidence=0.4,
        reason="low_confidence",
        market_mid=0.5,
        decision_id=did,
    )

    chain = await ledger.get_full_chain(did)
    # Rejected chain has exactly: MARKET_SNAPSHOT, INTELLIGENCE_SNAPSHOT,
    # FEATURE_SNAPSHOT, PREDICTION, RISK_REJECTED. The downstream ORDER /
    # FILL / POSITION / OUTCOME / PNL stages are absent (the order was
    # never submitted).
    assert set(chain.keys()) == {
        STAGE_MARKET_SNAPSHOT,
        STAGE_INTELLIGENCE_SNAPSHOT,
        STAGE_FEATURE_SNAPSHOT,
        STAGE_PREDICTION,
        "RISK_REJECTED",
    }
    # No SIGNAL / ORDER / FILL / POSITION / OUTCOME / PNL — these stages
    # are absent because the decision was rejected before the order was
    # submitted. ``get_full_chain`` correctly omits them rather than
    # emitting placeholder rows.
    for absent_stage in (
        STAGE_SIGNAL, STAGE_ORDER, STAGE_FILL,
        STAGE_POSITION, STAGE_OUTCOME, STAGE_PNL,
    ):
        assert absent_stage not in chain


# ── (4) get_latest_decision_id_for_token ───────────────────────────────────


@_asyncio_mark()
async def test_get_latest_decision_id_for_token_returns_none_for_unknown(ledger):
    """An unknown token returns ``None`` (no error raised)."""
    result = await ledger.get_latest_decision_id_for_token("UNKNOWN_TOKEN")
    assert result is None


@_asyncio_mark()
async def test_get_latest_decision_id_for_token_returns_none_for_empty_input(ledger):
    """Empty / falsy ``token_id`` returns ``None`` immediately."""
    assert await ledger.get_latest_decision_id_for_token("") is None


@_asyncio_mark()
async def test_get_latest_decision_id_for_token_returns_most_recent(ledger):
    """The most recent ``decision_id`` for a token is returned when no
    stage filter is supplied."""
    token = "TOK_LOOKUP"
    did_old = DecisionLedger.new_decision_id()
    did_new = DecisionLedger.new_decision_id()

    await ledger.record(did_old, STAGE_PREDICTION, token_id=token, strategy="s")
    await asyncio.sleep(0.005)
    await ledger.record(did_new, STAGE_PREDICTION, token_id=token, strategy="s")

    result = await ledger.get_latest_decision_id_for_token(token)
    assert result == did_new


@_asyncio_mark()
async def test_get_latest_decision_id_for_token_filters_by_stage(ledger):
    """When ``stage`` is supplied, only events of that stage are
    considered. The settlement pipeline uses ``stage=STAGE_FILL`` to
    find the decision chain that actually resulted in a filled position
    (ignoring earlier PREDICTION-only chains that never traded)."""
    token = "TOK_STAGE_FILTER"
    did_pred_only = DecisionLedger.new_decision_id()
    did_filled = DecisionLedger.new_decision_id()

    # did_pred_only: only PREDICTION (no FILL — rejected signal).
    await ledger.record(did_pred_only, STAGE_PREDICTION, token_id=token, strategy="s")
    await asyncio.sleep(0.005)
    await ledger.record_rejection(
        token_id=token, strategy="s", predicted_edge=0.0,
        confidence=0.4, reason="low_confidence", market_mid=0.5,
        decision_id=did_pred_only,
    )
    await asyncio.sleep(0.005)

    # did_filled: full PREDICTION → FILL chain.
    await ledger.record(did_filled, STAGE_PREDICTION, token_id=token, strategy="s")
    await asyncio.sleep(0.005)
    await ledger.record(did_filled, STAGE_FILL, token_id=token, strategy="s")

    # Unfiltered lookup → most recent event is FILL on did_filled.
    result_unfiltered = await ledger.get_latest_decision_id_for_token(token)
    assert result_unfiltered == did_filled

    # Stage=FILL filter → only FILL events considered, returns did_filled
    # (the only decision_id with a FILL stage for this token).
    result_fill = await ledger.get_latest_decision_id_for_token(
        token, stage=STAGE_FILL
    )
    assert result_fill == did_filled

    # Stage=ORDER filter → no ORDER stage recorded for this token (we
    # skipped ORDER in this test fixture), returns ``None``.
    result_order = await ledger.get_latest_decision_id_for_token(
        token, stage=STAGE_ORDER
    )
    assert result_order is None


# ── (5) API route: GET /api/decision/{correlation_id}/full-chain ────────────


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Fresh ``FastAPI`` app with only the decision-ledger routes registered,
    backed by a ``tmp_path`` SQLite file.

    Monkeypatches ``core.decision_ledger.DB_PATH`` AND the module-level
    ``decision_ledger`` singleton's ``_db_path`` attribute so the
    module-global singleton used by the route handlers (closure over
    ``decision_ledger``) writes to the test path. Mirrors the
    ``isolated_trail`` fixture pattern in ``tests/test_immutable_audit.py``.
    """
    db_path = tmp_path / "test_decision_full_chain_api.db"
    monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
    # Replace the module-level singleton with a fresh instance backed by
    # the test path — the route handlers in ``register_routes`` reference
    # the module global ``decision_ledger`` at call time (closure over the
    # module namespace), so the swap is picked up without re-registration.
    fresh = DecisionLedger()
    monkeypatch.setattr(decision_ledger_module, "decision_ledger", fresh)
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestFullChainRoute:
    """HTTP-level coverage of ``GET /api/decision/{correlation_id}/full-chain``."""

    def test_returns_404_for_unknown_correlation_id(self, client):
        """An unknown ``correlation_id`` returns 404 with a clear detail
        message."""
        resp = client.get("/api/decision/dec-nonexistent-id/full-chain")
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body
        assert "dec-nonexistent-id" in body["detail"]

    def test_returns_200_with_full_chain_dict(self, client):
        """A correlation_id with multiple recorded stages returns 200
        with a ``{stage_name: stage_event}`` dict under the ``stages`` key."""
        # Use the module-global singleton that the route handlers close
        # over (it's been monkeypatched to the fresh ``tmp_path``-backed
        # instance by the ``client`` fixture).
        import asyncio as _asyncio

        did = DecisionLedger.new_decision_id()
        token = "TOK_API"
        loop = _asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                decision_ledger_module.decision_ledger.record_market_snapshot(
                    did, token, snapshot={"mid": 0.5}
                )
            )
            loop.run_until_complete(
                decision_ledger_module.decision_ledger.record_pnl(
                    did, token, pnl={"realized_pnl": 1.5, "payout": 5.0}
                )
            )
        finally:
            loop.close()

        resp = client.get(f"/api/decision/{did}/full-chain")
        assert resp.status_code == 200
        body = resp.json()
        assert body["correlation_id"] == did
        assert body["count"] == 2
        assert set(body["stages"].keys()) == {
            STAGE_MARKET_SNAPSHOT, STAGE_PNL
        }
        # The PNL row's dedicated ``pnl`` column is populated.
        assert body["stages"][STAGE_PNL]["pnl"] == pytest.approx(1.5)

    def test_returns_full_12_stage_chain_when_all_stages_recorded(self, client):
        """When every stage in ``CANONICAL_STAGE_ORDER`` (minus the
        mutually-exclusive RISK_REJECTED) is recorded against a single
        ``correlation_id``, the route returns all 11 stages."""
        import asyncio as _asyncio

        did = DecisionLedger.new_decision_id()
        token = "TOK_API_FULL"
        loop = _asyncio.new_event_loop()
        try:
            async def _record_all():
                lg = decision_ledger_module.decision_ledger
                await lg.record_market_snapshot(did, token, snapshot={"mid": 0.5})
                await _asyncio.sleep(0.001)
                await lg.record_intelligence_snapshot(did, token, snapshot={"slug": "x"})
                await _asyncio.sleep(0.001)
                await lg.record_feature_snapshot(did, token, features={"features": [0.1]})
                await _asyncio.sleep(0.001)
                await lg.record(did, STAGE_PREDICTION, token_id=token, strategy="s", p_yes=0.65)
                await _asyncio.sleep(0.001)
                await lg.record(did, STAGE_SIGNAL, token_id=token, strategy="s", direction="BUY")
                await _asyncio.sleep(0.001)
                await lg.record(did, STAGE_RISK_APPROVED, token_id=token, strategy="s", side="BUY")
                await _asyncio.sleep(0.001)
                await lg.record(did, STAGE_ORDER, token_id=token, strategy="s", order_id="o1")
                await _asyncio.sleep(0.001)
                await lg.record(did, STAGE_FILL, token_id=token, strategy="s", pnl=0.0)
                await _asyncio.sleep(0.001)
                await lg.record_position(did, token, position={"yes_shares": 1.0, "pnl": 0.0})
                await _asyncio.sleep(0.001)
                await lg.record_outcome(did, token, outcome={"resolved_yes": True})
                await _asyncio.sleep(0.001)
                await lg.record_pnl(did, token, pnl={"realized_pnl": 1.0, "payout": 2.0})

            loop.run_until_complete(_record_all())
        finally:
            loop.close()

        resp = client.get(f"/api/decision/{did}/full-chain")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 11
        expected = {
            STAGE_MARKET_SNAPSHOT, STAGE_INTELLIGENCE_SNAPSHOT,
            STAGE_FEATURE_SNAPSHOT, STAGE_PREDICTION, STAGE_SIGNAL,
            STAGE_RISK_APPROVED, STAGE_ORDER, STAGE_FILL,
            STAGE_POSITION, STAGE_OUTCOME, STAGE_PNL,
        }
        assert set(body["stages"].keys()) == expected


# ── (6) Sanity: existing decision_ledger tests still pass ──────────────────


@_asyncio_mark()
async def test_existing_record_method_still_works_alongside_new_helpers(ledger):
    """The new ``record_*`` helpers wrap ``record()`` — the original
    ``record()`` API is unchanged and works alongside the new helpers
    for the same ``decision_id`` (no regression in the pre-W19-3 surface)."""
    did = DecisionLedger.new_decision_id()

    # Mix old and new API calls against the same decision_id.
    await ledger.record_market_snapshot(did, "TOK_MIX", snapshot={"mid": 0.5})
    await ledger.record(did, STAGE_PREDICTION, token_id="TOK_MIX", strategy="s", p_yes=0.65)
    await ledger.record(did, STAGE_SIGNAL, token_id="TOK_MIX", strategy="s", direction="BUY")
    await ledger.record_position(did, "TOK_MIX", position={"yes_shares": 1.0})

    chain = await ledger.get_chain(did)
    # 4 events recorded in chronological order.
    assert len(chain) == 4
    stages = [e["stage"] for e in chain]
    assert stages == [
        STAGE_MARKET_SNAPSHOT,
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_POSITION,
    ]

    # ``get_full_chain`` reconstructs the same 4 stages as a dict.
    full = await ledger.get_full_chain(did)
    assert set(full.keys()) == {
        STAGE_MARKET_SNAPSHOT, STAGE_PREDICTION, STAGE_SIGNAL, STAGE_POSITION
    }
