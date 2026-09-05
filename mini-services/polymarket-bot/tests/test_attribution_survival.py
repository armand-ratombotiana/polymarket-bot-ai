"""tests/test_attribution_survival.py — W37-5 attribution survival tests.

Verifies that the unified ``decision_id`` correlation key survives every
non-trivial lifecycle path a trade can take — restarts, retries, partial
fills, cancellations, replacements, market resolutions, replay, and the
edge case where attribution is missing altogether (manual / external
trades that bypassed the ledger). When attribution is missing, the
system must surface it (not silently drop the trade) so an operator can
investigate the gap.

The eight contract surfaces (one test per scenario):

  1. ``test_attribution_survives_restart``         — DecisionLedger
     persists across restart. The 12-stage chain written before a
     simulated restart is fully recoverable after the ledger is
     re-instantiated against the same SQLite DB file.
  2. ``test_attribution_survives_retry``            — Same order
     retried. The decision_id is preserved across retry attempts; the
     chain shows the original SIGNAL + the retried ORDER + the eventual
     FILL, all sharing the same correlation key.
  3. ``test_attribution_survives_partial_fill``     — Order goes OPEN
     → PARTIALLY_FILLED → FILLED. Each transition's metadata (filled
     size, residual size) is captured on the chain via the ORDER stage;
     the decision_id never breaks.
  4. ``test_attribution_survives_cancellation``    — Order cancelled
     before fill. The chain carries a complete ORDER event with the
     cancellation reason embedded in the metadata so the dashboard
     can render "this trade was cancelled because X" without ambiguity.
  5. ``test_attribution_survives_replacement``     — Order A is
     replaced by Order B (a common pattern: price-improve a stale
     quote). Both orders carry the originating decision_id so the
     attribution roll-up credits the strategy, not the order
     placeholder.
  6. ``test_attribution_survives_resolution``      — Market resolves.
     OUTCOME + PNL events are appended to the existing chain; the
     final chain carries all 12 canonical stages from MARKET_SNAPSHOT
     to PNL.
  7. ``test_unattributed_trades_flagged``          — A trade that
     bypassed the decision ledger (manual / external / legacy entry)
     is flagged by the ``find_unattributed_trades`` helper rather than
     silently rolled into the strategy P&L.
  8. ``test_attribution_survives_replay``          — Replaying
     history (re-recording the same stage events, e.g. during a
     backtest or a recovery back-fill) preserves attribution: the
     dedup_registry allows re-records with different payloads through,
     and the original decision_id chain is intact afterward.

The tests construct a fresh ``DecisionLedger(tmp_path / "test.db")``
per test (the module-level singleton's DB_PATH is monkeypatched so the
no-arg constructor resolves to the test path) — mirrors the isolation
pattern in ``tests/test_decision_ledger.py``. The state-recovery
manager and closed-positions store are similarly scoped to ``tmp_path``
so the tests never touch the module-level singletons' on-disk state.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` so a sibling test file invoked directly
# (``python -m pytest tests/test_attribution_survival.py``) boots
# hermetic to ``/tmp`` rather than clobbering any real persisted state
# in the repo's ``data/`` directory. ``setdefault`` lets the conftest's
# redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_attribution_survival_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "ORDER_STATE_MACHINE_DB_PATH": str(_TMP_ROOT / "order_state_machine.db"),
    "RECOVERY_STATE_PATH": str(_TMP_ROOT / "recovery_state.json"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-attribution-survival",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``backtesting.*``, ``ml.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.closed_positions import ClosedPositionsStore  # noqa: E402
from core.decision_ledger import (  # noqa: E402
    CANONICAL_STAGE_ORDER,
    STAGE_FILL,
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
)
from core.dedup import dedup_registry  # noqa: E402
from core.order_state_machine import (  # noqa: E402
    OrderState,
    OrderStateMachine,
    create_order,
    transition,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module — mirrors the convention in every sibling test module.
pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Fresh ``DecisionLedger`` whose SQLite DB file lives under
    ``tmp_path`` so each test is hermetic. ``DB_PATH`` is monkeypatched
    so the no-arg ``DecisionLedger()`` constructor picks up the test
    path — the same global-lookup code path production uses."""
    db_path = tmp_path / "test_attribution_ledger.db"
    monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
    return DecisionLedger()


@pytest.fixture
def osm(tmp_path):
    """Fresh ``OrderStateMachine`` whose SQLite DB lives under
    ``tmp_path`` — used to drive the order lifecycle (CREATED →
    SUBMITTED → OPEN → PARTIALLY_FILLED → FILLED / CANCELLED / ...)
    and verify that the decision_id carries through every
    transition."""
    return OrderStateMachine(tmp_path / "test_osm.db")


@pytest.fixture
def closed_positions(tmp_path, monkeypatch):
    """Fresh ``ClosedPositionsStore`` whose SQLite DB lives under
    ``tmp_path`` so the unattributed-trades-flagged test can inject
    trades with NULL decision_ids and verify the helper surfaces
    them."""
    db_path = tmp_path / "test_closed_positions.db"
    monkeypatch.setattr("core.closed_positions.DB_PATH", db_path)
    return ClosedPositionsStore()


@pytest.fixture(autouse=True)
def _clear_dedup_registry_per_test():
    """The unified ``dedup_registry`` singleton persists across tests;
    the test_attribution_survives_replay test deliberately exercises the
    dedup path (re-record same stage) so we MUST start from a clean
    registry. Mirrors the autouse pattern in ``tests/conftest.py``."""
    dedup_registry.clear()
    yield
    dedup_registry.clear()


# ── Helper ───────────────────────────────────────────────────────────────────


async def find_unattributed_trades(
    closed_positions: ClosedPositionsStore,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return closed-position rows whose ``decision_id`` is NULL or
    empty.

    These are trades that bypassed the unified decision ledger —
    manual entries, external broker imports, or pre-ledger legacy
    data. Surfacing them is the first step in attributing their P&L
    (or marking them as ``unknown_strategy`` in the dashboard).

    The helper queries the ``closed_positions`` table directly via
    SQLite so it doesn't depend on the production ``get_closed_positions``
    method's filtering / ordering — the goal is a fast diagnostic for
    "what's missing attribution?" not a paginated API response.
    """
    import sqlite3

    db_path = closed_positions._db_path
    loop = asyncio.get_event_loop()

    def _fetch() -> list[dict[str, Any]]:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT position_id, token_id, strategy, pnl,
                           decision_id, timestamp
                    FROM closed_positions
                    WHERE decision_id IS NULL OR decision_id = ''
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception:
            return []

    return await loop.run_in_executor(None, _fetch)


async def _record_full_pre_trade_chain(
    ledger: DecisionLedger,
    decision_id: str,
    token_id: str,
    strategy: str,
) -> None:
    """Record the 5-stage pre-trade chain (MARKET_SNAPSHOT →
    INTELLIGENCE_SNAPSHOT → FEATURE_SNAPSHOT → PREDICTION → SIGNAL)
    that ``signal_trader._ml_signal`` would emit on a happy path.

    Used as a setup helper so each test can assert against the post-
    trade stages (RISK_APPROVED / ORDER / FILL / POSITION / OUTCOME /
    PNL) without re-recording the 5 pre-trade stages every time.
    """
    await ledger.record_market_snapshot(
        correlation_id=decision_id,
        token_id=token_id,
        snapshot={"best_bid": 0.50, "best_ask": 0.52, "mid": 0.51},
        strategy=strategy,
    )
    await ledger.record_intelligence_snapshot(
        correlation_id=decision_id,
        token_id=token_id,
        snapshot={"slug": "test-market", "volume_24h": 10_000},
        strategy=strategy,
    )
    # FEATURE_SNAPSHOT not yet exposed as a top-level helper — record
    # via the generic ``record()`` so the chain has the stage.
    await ledger.record(
        decision_id=decision_id,
        stage="FEATURE_SNAPSHOT",
        token_id=token_id,
        strategy=strategy,
        features={"f1": 0.1, "f2": -0.2},
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=token_id,
        strategy=strategy,
        p_yes=0.62,
        confidence=0.24,
        predicted_edge=0.08,
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=token_id,
        strategy=strategy,
        action="BUY",
        size=10.0,
        price=0.51,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Attribution survives restart
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_restart(ledger, monkeypatch, tmp_path):
    """The decision ledger persists its SQLite database to disk; after a
    simulated restart (re-instantiating ``DecisionLedger`` against the
    same DB file), the full 12-stage decision chain written before the
    restart must be recoverable via ``get_chain(decision_id)`` and
    ``get_full_chain(decision_id)``.

    This is the canonical "did the bot remember what it was doing
    after a reboot?" test — the God Mode §51 audit-trail contract.
    """
    db_path = ledger._db_path
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_RESTART"
    strategy = "ml_sig_v1"

    # Record the full happy-path chain (5 pre-trade + 3 post-trade).
    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
        kelly_fraction=0.12,
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-restart-1",
        side="BUY",
        price=0.51,
        size=10.0,
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-restart-1",
        fill_price=0.511,
        filled_size=10.0,
        pnl=0.0,
    )

    # Sanity: pre-restart chain has the 8 stages we wrote.
    pre_chain = await ledger.get_full_chain(decision_id)
    assert len(pre_chain) == 8, (
        f"pre-restart chain should have 8 stages, got {len(pre_chain)}"
    )

    # ── Simulate restart ────────────────────────────────────────────
    # Drop the in-memory ledger instance and re-instantiate against
    # the SAME on-disk DB file. The module-level ``DB_PATH`` is
    # already monkeypatched to point at this file (the ``ledger``
    # fixture does that), so a fresh ``DecisionLedger()`` picks it up
    # automatically — exactly the code path the FastAPI lifespan
    # startup uses in production.
    del ledger
    restarted_ledger = DecisionLedger()

    # Post-restart: the same decision_id resolves to the same chain.
    post_chain = await restarted_ledger.get_full_chain(decision_id)

    # All 8 stages survived the restart.
    assert len(post_chain) == 8, (
        f"post-restart chain should have 8 stages, got {len(post_chain)}"
    )
    # Stage set is preserved.
    pre_stages = set(pre_chain.keys())
    post_stages = set(post_chain.keys())
    assert pre_stages == post_stages, (
        f"stage set changed across restart: pre={pre_stages}, post={post_stages}"
    )
    # The decision_id on every stage event is the original.
    for stage_name, ev in post_chain.items():
        assert ev["decision_id"] == decision_id, (
            f"stage {stage_name} has decision_id={ev['decision_id']!r}, "
            f"expected {decision_id!r}"
        )
    # The token_id is preserved (the recovery lookup uses it).
    assert all(
        ev["token_id"] == token_id for ev in post_chain.values()
    ), "token_id lost across restart"
    # The strategy is preserved (the attribution roll-up uses it).
    assert all(
        ev["strategy"] == strategy for ev in post_chain.values()
    ), "strategy lost across restart"

    # get_latest_decision_id_for_token still resolves to the same id.
    latest_did = await restarted_ledger.get_latest_decision_id_for_token(
        token_id
    )
    assert latest_did == decision_id, (
        f"latest decision_id for token post-restart: {latest_did!r}, "
        f"expected {decision_id!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Attribution survives retry
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_retry(ledger, osm):
    """When an order is retried (initial submit failed / timed out /
    was rejected by the exchange and the strategy re-submits), the
    SAME ``decision_id`` is preserved across both attempts so the
    attribution roll-up credits the strategy once (not twice) and
    the audit trail shows the retry path.

    Concretely:
      - Initial ORDER event lands on the chain (attempt 1).
      - The retry's ORDER event also lands (attempt 2) — different
        ``order_id``, different payload, but the same ``decision_id``.
      - The eventual FILL is recorded against the retry's order_id,
        but the chain attribution still points to the original
        ``decision_id``.
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_RETRY"
    strategy = "ml_sig_v1"

    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
    )

    # ── Attempt 1: ORDER submitted, then rejected by exchange ───────
    order1 = create_order(
        strategy=strategy,
        token_id=token_id,
        side="BUY",
        price=0.51,
        size=10.0,
        decision_id=decision_id,
        order_id="ord-retry-1",
    )
    osm.save(order1)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id=order1.order_id,
        attempt=1,
        side="BUY",
        price=0.51,
        size=10.0,
    )
    # Exchange rejected attempt 1.
    rejected = transition(order1, OrderState.REJECTED)
    osm.save(rejected)

    # ── Attempt 2: ORDER submitted at improved price, then filled ──
    order2 = create_order(
        strategy=strategy,
        token_id=token_id,
        side="BUY",
        price=0.515,  # improved price
        size=10.0,
        decision_id=decision_id,  # SAME correlation key
        order_id="ord-retry-2",
    )
    osm.save(order2)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id=order2.order_id,
        attempt=2,
        side="BUY",
        price=0.515,
        size=10.0,
        retry_of=order1.order_id,
    )
    # Walk the happy-path transitions: CREATED → VALIDATED → SUBMITTED
    # → ACKNOWLEDGED → OPEN → FILLED.
    o2_validated = transition(order2, OrderState.VALIDATED)
    o2_submitted = transition(o2_validated, OrderState.SUBMITTED)
    o2_ack = transition(o2_submitted, OrderState.ACKNOWLEDGED)
    o2_open = transition(o2_ack, OrderState.OPEN)
    o2_filled = transition(o2_open, OrderState.FILLED)
    osm.save(o2_filled)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id=order2.order_id,
        fill_price=0.515,
        filled_size=10.0,
        pnl=0.0,
    )

    # ── Assertions ──────────────────────────────────────────────────
    # The chain has TWO ORDER events (attempt 1 + attempt 2) sharing
    # the same decision_id. ``get_chain`` returns rows in chronological
    # order so we can read both ORDER rows.
    chain = await ledger.get_chain(decision_id)
    order_events = [e for e in chain if e["stage"] == STAGE_ORDER]
    assert len(order_events) == 2, (
        f"expected 2 ORDER events (attempt 1 + retry), got {len(order_events)}"
    )
    # Both ORDER events carry the same decision_id.
    assert all(e["decision_id"] == decision_id for e in order_events)
    # Attempt numbers preserve the retry semantics.
    attempt1_data = order_events[0]["data"]
    attempt2_data = order_events[1]["data"]
    assert attempt1_data.get("attempt") == 1
    assert attempt2_data.get("attempt") == 2
    # Attempt 2 references attempt 1's order_id (the retry-of link).
    assert attempt2_data.get("retry_of") == "ord-retry-1"
    # Different order_ids on each attempt (the retry is a new order).
    assert attempt1_data["order_id"] != attempt2_data["order_id"]
    # The FILL event records the RETRY's order_id (not attempt 1's).
    fill_events = [e for e in chain if e["stage"] == STAGE_FILL]
    assert len(fill_events) == 1
    assert fill_events[0]["data"]["order_id"] == "ord-retry-2"

    # The OSM audit trail also shows both orders with the same
    # decision_id (used by reconciliation to group retries).
    order1_loaded = osm.load("ord-retry-1")
    order2_loaded = osm.load("ord-retry-2")
    assert order1_loaded is not None and order2_loaded is not None
    assert order1_loaded.decision_id == decision_id
    assert order2_loaded.decision_id == decision_id


# ═══════════════════════════════════════════════════════════════════════════
# 3. Attribution survives partial fill
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_partial_fill(ledger, osm):
    """An order that fills in two tranches (PARTIALLY_FILLED →
    PARTIALLY_FILLED → FILLED) must carry its ``decision_id`` through
    every transition so the position attribution roll-up credits the
    full position to the originating strategy.

    The chain records:
      - The initial ORDER event (size=10).
      - The first partial FILL (filled_size=3, residual=7).
      - The second FILL (filled_size=7, residual=0 — terminal).
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_PARTIAL"
    strategy = "value_v1"

    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
    )

    # Initial ORDER.
    order = create_order(
        strategy=strategy,
        token_id=token_id,
        side="BUY",
        price=0.50,
        size=10.0,
        decision_id=decision_id,
        order_id="ord-partial-1",
    )
    osm.save(order)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-partial-1",
        side="BUY",
        price=0.50,
        size=10.0,
    )

    # First partial fill (3 of 10). Walk the happy-path transitions
    # CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN →
    # PARTIALLY_FILLED → FILLED. Use ``osm.transition`` (which
    # persists every snapshot) so the audit-trail history carries
    # every state.
    o_validated = osm.transition(order, OrderState.VALIDATED)
    o_submitted = osm.transition(o_validated, OrderState.SUBMITTED)
    o_ack = osm.transition(o_submitted, OrderState.ACKNOWLEDGED)
    o_open = osm.transition(o_ack, OrderState.OPEN)
    pf1 = osm.transition(o_open, OrderState.PARTIALLY_FILLED, filled_size=3.0)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-partial-1",
        fill_price=0.495,
        filled_size=3.0,
        residual_size=7.0,
        fill_seq=1,
        pnl=0.0,
    )

    # Second fill (remaining 7).
    osm.transition(pf1, OrderState.FILLED, filled_size=10.0)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-partial-1",
        fill_price=0.50,
        filled_size=7.0,
        residual_size=0.0,
        fill_seq=2,
        pnl=0.0,
    )

    # ── Assertions ──────────────────────────────────────────────────
    chain = await ledger.get_chain(decision_id)
    # 5 pre-trade + 1 RISK + 1 ORDER + 2 FILLs = 9 stages.
    assert len(chain) == 9, (
        f"expected 9 chain events (5 pre + RISK + ORDER + 2 FILLs), got {len(chain)}"
    )

    # Both FILL events share the same decision_id.
    fill_events = [e for e in chain if e["stage"] == STAGE_FILL]
    assert len(fill_events) == 2
    assert all(e["decision_id"] == decision_id for e in fill_events)

    # The fills' cumulative size matches the order's intended size.
    cumulative_filled = sum(float(e["data"].get("filled_size", 0.0)) for e in fill_events)
    assert cumulative_filled == pytest.approx(10.0), (
        f"cumulative filled size {cumulative_filled} should equal order size 10.0"
    )

    # The OSM audit trail records every transition with the same
    # decision_id.
    history = osm.get_history("ord-partial-1")
    assert len(history) >= 4  # CREATED, OPEN, PARTIALLY_FILLED, FILLED
    assert all(h.decision_id == decision_id for h in history), (
        "OSM history lost the decision_id across transitions"
    )

    # The terminal state of the order is FILLED.
    latest = osm.load("ord-partial-1")
    assert latest is not None
    assert latest.state == OrderState.FILLED
    assert latest.filled_size == pytest.approx(10.0)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Attribution survives cancellation
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_cancellation(ledger, osm):
    """An order cancelled before fill must still have a complete
    decision chain — the ORDER event records the cancellation reason
    in its metadata, and the originating decision_id is preserved so
    an operator can answer "why did we cancel?" via the chain.

    A cancelled order is NOT a gap in the audit trail: the strategy
    decided to trade, the risk engine approved it, the order was
    placed, then cancelled. Each of those is an audit-worthy event.
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_CANCEL"
    strategy = "stat_arb_v1"

    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
    )

    order = create_order(
        strategy=strategy,
        token_id=token_id,
        side="SELL",
        price=0.55,
        size=20.0,
        decision_id=decision_id,
        order_id="ord-cancel-1",
    )
    osm.save(order)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-cancel-1",
        side="SELL",
        price=0.55,
        size=20.0,
    )

    # Cancel the order — record the cancellation as an ORDER stage
    # update with the cancellation reason (the chain's last write
    # wins for repeated stage names; the cancelled metadata
    # overwrites the open-state metadata).
    cancelled = osm.transition(order, OrderState.CANCELLED)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-cancel-1",
        side="SELL",
        price=0.55,
        size=20.0,
        status="CANCELLED",
        cancel_reason="spread_widened",
        cancel_ts=time.time(),
    )

    # ── Assertions ──────────────────────────────────────────────────
    chain = await ledger.get_chain(decision_id)

    # The chain has the 5 pre-trade stages + RISK_APPROVED + ORDER.
    # No FILL (the order was cancelled before fill).
    stages_present = {e["stage"] for e in chain}
    assert STAGE_PREDICTION in stages_present
    assert STAGE_SIGNAL in stages_present
    assert STAGE_RISK_APPROVED in stages_present
    assert STAGE_ORDER in stages_present
    assert STAGE_FILL not in stages_present, (
        "cancelled order must not have a FILL stage"
    )

    # The ORDER stage's latest event carries the cancellation metadata.
    full_chain = await ledger.get_full_chain(decision_id)
    order_event = full_chain[STAGE_ORDER]
    assert order_event["data"]["status"] == "CANCELLED"
    assert order_event["data"]["cancel_reason"] == "spread_widened"

    # The OSM audit trail's terminal state is CANCELLED.
    latest = osm.load("ord-cancel-1")
    assert latest is not None
    assert latest.state == OrderState.CANCELLED
    assert latest.decision_id == decision_id


# ═══════════════════════════════════════════════════════════════════════════
# 5. Attribution survives replacement
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_replacement(ledger, osm):
    """When an order is *replaced* (the strategy cancels order A and
    submits order B at a better price), both orders must be tracked
    against the same ``decision_id`` so the attribution roll-up
    credits the strategy — not the order placeholder.

    The replacement pattern is common in market-making: the quote
    drifts, the old order is cancelled, a new one is placed at the
    new mid. Without attribution survival, each replacement would
    look like a fresh strategy decision (inflating the "trades per
    strategy" count and confusing the attribution roll-up).
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_REPLACE"
    strategy = "market_maker_v1"

    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
    )

    # ── Order A: placed at 0.50, cancelled as the mid drifts ────────
    order_a = create_order(
        strategy=strategy,
        token_id=token_id,
        side="BUY",
        price=0.50,
        size=15.0,
        decision_id=decision_id,
        order_id="ord-replace-A",
    )
    osm.save(order_a)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-replace-A",
        attempt=1,
        side="BUY",
        price=0.50,
        size=15.0,
    )
    osm.transition(order_a, OrderState.CANCELLED)

    # ── Order B: placed at 0.48 (improved), filled ──────────────────
    order_b = create_order(
        strategy=strategy,
        token_id=token_id,
        side="BUY",
        price=0.48,
        size=15.0,
        decision_id=decision_id,  # SAME correlation key
        order_id="ord-replace-B",
    )
    osm.save(order_b)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-replace-B",
        attempt=2,
        side="BUY",
        price=0.48,
        size=15.0,
        replaces="ord-replace-A",
    )
    # Walk order B through the happy path: CREATED → VALIDATED →
    # SUBMITTED → ACKNOWLEDGED → OPEN → FILLED.
    ob_v = osm.transition(order_b, OrderState.VALIDATED)
    ob_s = osm.transition(ob_v, OrderState.SUBMITTED)
    ob_a = osm.transition(ob_s, OrderState.ACKNOWLEDGED)
    ob_o = osm.transition(ob_a, OrderState.OPEN)
    osm.transition(ob_o, OrderState.FILLED)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-replace-B",
        fill_price=0.48,
        filled_size=15.0,
        pnl=0.0,
    )

    # ── Assertions ──────────────────────────────────────────────────
    chain = await ledger.get_chain(decision_id)

    # Two ORDER events (A + B), one FILL event (B filled).
    order_events = [e for e in chain if e["stage"] == STAGE_ORDER]
    fill_events = [e for e in chain if e["stage"] == STAGE_FILL]
    assert len(order_events) == 2, (
        f"expected 2 ORDER events (A + B), got {len(order_events)}"
    )
    assert len(fill_events) == 1

    # Both ORDER events share the decision_id.
    assert all(e["decision_id"] == decision_id for e in order_events)

    # Order B explicitly records that it replaced Order A.
    order_b_event = next(
        e for e in order_events if e["data"]["order_id"] == "ord-replace-B"
    )
    assert order_b_event["data"]["replaces"] == "ord-replace-A"

    # The OSM audit trail has separate rows for both orders, both
    # carrying the same decision_id.
    a_loaded = osm.load("ord-replace-A")
    b_loaded = osm.load("ord-replace-B")
    assert a_loaded.decision_id == decision_id
    assert b_loaded.decision_id == decision_id
    assert a_loaded.state == OrderState.CANCELLED
    assert b_loaded.state == OrderState.FILLED


# ═══════════════════════════════════════════════════════════════════════════
# 6. Attribution survives resolution
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_resolution(ledger):
    """When a market resolves, the OUTCOME and PNL stages are appended
    to the existing decision chain so the final chain carries every
    canonical stage from MARKET_SNAPSHOT to PNL.

    The settlement pipeline (``core/settlement.py::_process_resolved_market``)
    calls ``record_outcome`` then ``record_pnl`` immediately after the
    YES/NO position is settled — these events close the audit loop
    ("the bot made this trade, here's why, here's what happened, here's
    the realised P&L").
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_RESOLVE"
    strategy = "value_v1"

    # Record the full pre-trade + trade chain.
    await _record_full_pre_trade_chain(ledger, decision_id, token_id, strategy)
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=token_id,
        strategy=strategy,
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-resolve-1",
        side="BUY",
        price=0.60,
        size=10.0,
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_FILL,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-resolve-1",
        fill_price=0.60,
        filled_size=10.0,
        pnl=0.0,
    )
    await ledger.record_position(
        correlation_id=decision_id,
        token_id=token_id,
        position={
            "yes_shares": 10.0,
            "avg_entry_price": 0.60,
            "total_invested": 6.0,
            "strategy": strategy,
            "paper": True,
        },
        strategy=strategy,
    )

    # ── Market resolves YES — the position pays out $1.00 / share ──
    await ledger.record_outcome(
        correlation_id=decision_id,
        token_id=token_id,
        outcome={
            "resolved_yes": True,
            "resolution_price": 1.0,
            "market_slug": "test-market-yes",
            "resolved_at": time.time(),
        },
        strategy=strategy,
    )
    # PNL: 10 shares × ($1.00 - $0.60) = $4.00 realised.
    await ledger.record_pnl(
        correlation_id=decision_id,
        token_id=token_id,
        pnl={
            "realized_pnl": 4.0,
            "payout": 10.0,
            "invested_cost": 6.0,
            "shares": 10.0,
            "exit_price": 1.0,
        },
        strategy=strategy,
    )

    # ── Assertions ──────────────────────────────────────────────────
    full_chain = await ledger.get_full_chain(decision_id)

    # All 12 canonical stages are present.
    expected_stages = {
        STAGE_MARKET_SNAPSHOT,
        STAGE_INTELLIGENCE_SNAPSHOT,
        "FEATURE_SNAPSHOT",
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
        STAGE_POSITION,
        STAGE_OUTCOME,
        STAGE_PNL,
    }
    assert expected_stages.issubset(full_chain.keys()), (
        f"missing stages: {expected_stages - set(full_chain.keys())}"
    )

    # The OUTCOME event records YES resolution.
    outcome_ev = full_chain[STAGE_OUTCOME]
    assert outcome_ev["data"]["resolved_yes"] is True
    assert outcome_ev["data"]["resolution_price"] == pytest.approx(1.0)

    # The PNL event's dedicated ``pnl`` column carries the realised P&L.
    pnl_ev = full_chain[STAGE_PNL]
    assert pnl_ev["pnl"] == pytest.approx(4.0), (
        f"PNL event's pnl column should be 4.0 (10 × ($1 - $0.60)), got {pnl_ev['pnl']}"
    )
    # The PNL payload carries the payout / invested cost / shares.
    assert pnl_ev["data"]["payout"] == pytest.approx(10.0)
    assert pnl_ev["data"]["invested_cost"] == pytest.approx(6.0)
    assert pnl_ev["data"]["shares"] == pytest.approx(10.0)

    # Every stage in the chain shares the originating decision_id —
    # the full 12-stage audit trail is intact end-to-end.
    for stage_name, ev in full_chain.items():
        assert ev["decision_id"] == decision_id, (
            f"stage {stage_name} lost the decision_id"
        )
        assert ev["token_id"] == token_id


# ═══════════════════════════════════════════════════════════════════════════
# 7. Unattributed trades flagged
# ═══════════════════════════════════════════════════════════════════════════


async def test_unattributed_trades_flagged(closed_positions):
    """Closed positions whose ``decision_id`` is NULL or empty must be
    surfaced by the ``find_unattributed_trades`` helper so an operator
    can investigate the gap (rather than silently rolling their P&L
    into the ``unknown_strategy`` attribution bucket).

    Sources of unattributed trades:
      - Manual / broker-imported entries (no strategy context).
      - Legacy trades that pre-date the unified ledger.
      - A bug in ``signal_trader._ml_signal`` that drops the
        ``decision_id`` before calling ``record_closed_position``.

    The helper queries the ``closed_positions`` table directly for
    rows where ``decision_id IS NULL OR decision_id = ''`` and
    returns them with enough context (position_id, token_id, strategy,
    pnl, timestamp) to drive a back-fill effort.
    """
    # ── Trade A: properly attributed (decision_id present) ──────────
    await closed_positions.record_closed_position(
        token_id="TOK_ATTR",
        strategy="ml_sig_v1",
        entry_price=0.50,
        exit_price=0.55,
        shares=10.0,
        pnl=0.50,
        holding_seconds=3600.0,
        decision_id="dec-attributed-A",
    )

    # ── Trade B: NULL decision_id (manual entry) ────────────────────
    await closed_positions.record_closed_position(
        token_id="TOK_UNATTR_NULL",
        strategy="manual",
        entry_price=0.40,
        exit_price=0.45,
        shares=5.0,
        pnl=0.25,
        holding_seconds=7200.0,
        # decision_id deliberately NOT supplied — lands as NULL.
    )

    # ── Trade C: empty-string decision_id (legacy / bug) ────────────
    await closed_positions.record_closed_position(
        token_id="TOK_UNATTR_EMPTY",
        strategy="legacy_v0",
        entry_price=0.30,
        exit_price=0.28,
        shares=20.0,
        pnl=-0.40,
        holding_seconds=600.0,
        decision_id="",  # explicit empty string
    )

    # ── Helper surfaces only the unattributed rows ─────────────────
    flagged = await find_unattributed_trades(closed_positions, limit=100)

    # Two of the three trades are unattributed (NULL + empty string).
    assert len(flagged) == 2, (
        f"expected 2 unattributed trades (NULL + empty), got {len(flagged)}"
    )

    # The flagged rows carry the right tokens (the unattributed ones).
    flagged_tokens = {r["token_id"] for r in flagged}
    assert flagged_tokens == {"TOK_UNATTR_NULL", "TOK_UNATTR_EMPTY"}
    # The properly-attributed trade is NOT in the flagged set.
    assert "TOK_ATTR" not in flagged_tokens

    # Each flagged row carries enough context to drive a back-fill.
    for row in flagged:
        assert row["position_id"]  # non-empty
        assert row["token_id"]  # non-empty
        assert row["strategy"]  # non-empty
        # decision_id is None (NULL) or "" (empty string).
        assert row["decision_id"] is None or row["decision_id"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# 8. Attribution survives replay
# ═══════════════════════════════════════════════════════════════════════════


async def test_attribution_survives_replay(ledger):
    """Replaying history (re-recording the same stage events, e.g.
    during a backtest, a recovery back-fill, or a migration from a
    legacy ledger) must preserve attribution.

    The dedup_registry's TTL-bucketed key blocks EXACT-payload
    duplicates within a 5-minute window (so a retry storm doesn't
    double-write the same stage event). However, a re-record with a
    DIFFERENT payload (the canonical "last write wins" semantic tested
    by ``test_get_full_chain_last_write_wins_for_repeated_stage`` in
    ``tests/test_decision_ledger.py``) is allowed through — and the
    original ``decision_id`` survives every replayed write.
    """
    decision_id = DecisionLedger.new_decision_id()
    token_id = "TOK_REPLAY"
    strategy = "ml_sig_v1"

    # ── Initial recording: PREDICTION + SIGNAL + ORDER ─────────────
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=token_id,
        strategy=strategy,
        p_yes=0.55,
        confidence=0.20,
        predicted_edge=0.04,
        source="live",
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=token_id,
        strategy=strategy,
        action="BUY",
        size=5.0,
        price=0.51,
        source="live",
    )
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-replay-1",
        side="BUY",
        price=0.51,
        size=5.0,
    )

    pre_replay_chain = await ledger.get_chain(decision_id)
    assert len(pre_replay_chain) == 3

    # ── Replay: re-record PREDICTION with enriched payload (e.g. ────
    # back-filling the SHAP values after a model-explanation run).
    # The payload is DIFFERENT from the initial record, so the dedup
    # gate allows it through; the chain's PREDICTION stage is updated
    # to the replayed version (last write wins).
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=token_id,
        strategy=strategy,
        p_yes=0.55,
        confidence=0.20,
        predicted_edge=0.04,
        source="replay",
        shap_top_features=[
            {"feature": "f1", "value": 0.12},
            {"feature": "f2", "value": -0.08},
        ],
        replay_ts=time.time(),
    )

    # ── Replay also re-records the ORDER (e.g. reconciliation ───────
    # discovers the order_id was wrong and re-records the corrected
    # value). Different payload → allowed through.
    await ledger.record(
        decision_id=decision_id,
        stage=STAGE_ORDER,
        token_id=token_id,
        strategy=strategy,
        order_id="ord-replay-1-CORRECTED",
        side="BUY",
        price=0.51,
        size=5.0,
        source="replay",
        replaces="ord-replay-1",
    )

    # ── Assertions ──────────────────────────────────────────────────
    full_chain = await ledger.get_full_chain(decision_id)

    # The chain still has exactly 3 stages (PREDICTION, SIGNAL, ORDER).
    # Re-records with different payloads do NOT create duplicate stage
    # entries in ``get_full_chain`` — last write wins, so the stage
    # set is unchanged.
    assert set(full_chain.keys()) == {STAGE_PREDICTION, STAGE_SIGNAL, STAGE_ORDER}

    # The PREDICTION event is the replayed version (carries
    # ``source="replay"`` + the SHAP payload).
    pred_ev = full_chain[STAGE_PREDICTION]
    assert pred_ev["data"]["source"] == "replay"
    assert "shap_top_features" in pred_ev["data"]
    assert pred_ev["data"]["replay_ts"] > 0

    # The SIGNAL event was NOT replayed — its payload is still the
    # original (``source="live"``).
    sig_ev = full_chain[STAGE_SIGNAL]
    assert sig_ev["data"]["source"] == "live"

    # The ORDER event is the replayed version (carries the corrected
    # order_id + ``source="replay"``).
    order_ev = full_chain[STAGE_ORDER]
    assert order_ev["data"]["order_id"] == "ord-replay-1-CORRECTED"
    assert order_ev["data"]["source"] == "replay"
    assert order_ev["data"]["replaces"] == "ord-replay-1"

    # The decision_id survived every replayed write — the audit
    # trail's correlation key is intact.
    for stage_name, ev in full_chain.items():
        assert ev["decision_id"] == decision_id, (
            f"stage {stage_name} lost decision_id across replay"
        )
        assert ev["token_id"] == token_id
        assert ev["strategy"] == strategy


# ── Sanity: the canonical stage order constant ──────────────────────────────


async def test_canonical_stage_order_constant_complete():
    """Sanity check that the ``CANONICAL_STAGE_ORDER`` constant the
    survival tests rely on still enumerates all 12 stages — guards
    against a future refactor that silently drops a stage from the
    tuple (which would make the survival tests pass vacuously)."""
    expected = {
        "MARKET_SNAPSHOT",
        "INTELLIGENCE_SNAPSHOT",
        "FEATURE_SNAPSHOT",
        "PREDICTION",
        "SIGNAL",
        "RISK_APPROVED",
        "RISK_REJECTED",
        "ORDER",
        "FILL",
        "POSITION",
        "OUTCOME",
        "PNL",
    }
    assert set(CANONICAL_STAGE_ORDER) == expected, (
        f"CANONICAL_STAGE_ORDER is incomplete: {set(CANONICAL_STAGE_ORDER)} "
        f"vs expected {expected}"
    )
    assert len(CANONICAL_STAGE_ORDER) == 12
