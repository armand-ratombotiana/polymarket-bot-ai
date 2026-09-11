"""tests/test_dedup_wiring.py — W25-7 end-to-end wiring tests for the
unified deduplication registry.

W25-7 — wire the unified ``core.dedup.dedup_registry`` into ALL event-
processing paths and verify end-to-end that duplicate events are silently
dropped BEFORE they mutate downstream state (SQLite rows / Trade objects).
The wiring itself was implemented by W24-6; this file exercises the wiring
through the public surfaces a duplicate would actually pollute (DB tables +
``store.trades``) so a regression that detaches the dedup gate from any of
the 4 paths is caught immediately.

Wiring verified
---------------

  (1) ``core/decision_ledger.py::record`` — a duplicate ``(decision_id,
      stage, payload)`` call within the 300 s TTL window is dropped BEFORE
      the ``INSERT INTO decision_events`` runs (verified by reading the
      chain back via ``get_chain`` and asserting only one row survived).
      Unique decisions with a different stage OR a different payload pass
      through (the ``payload_hash`` component of the dedup key allows
      legitimate "last write wins" updates to the same stage).

  (2) ``core/alerting.py::fire_alert`` — a duplicate ``alert_id`` within
      the 300 s TTL window is dropped BEFORE the ``INSERT OR REPLACE INTO
      alerts`` runs (verified via ``get_stats()["total_alerts"]`` and the
      ``fire_alert`` return value flipping ``True`` → ``False`` on the
      second call). Unique alerts with a different ``alert_id`` pass
      through.

  (3) ``paper/simulator.py::_execute_fill`` — a duplicate ``order_id``
      within the 3600 s TTL window is dropped BEFORE the ``Trade`` is
      constructed and appended to ``store.trades``. Unique orders pass
      through.

  (4) ``core/live_fill_monitor.py::_process_trade`` — a duplicate
      ``trade_id`` within the 3600 s TTL window is dropped BEFORE the
      ``Trade`` is constructed and appended to ``store.trades``. Unique
      trade_ids pass through.

API surface
-----------

  (5) ``GET /api/dedup/stats`` — returns the registry's per-type stats
      dict (matches ``dedup_registry.get_stats()``).

  (6) ``GET /api/dedup/stats?entity_type=X`` — filters to one type,
      returning a single flat dict (NOT nested under a top-level key).

  (7) ``GET /api/dedup/stats?entity_type=unknown`` — returns a zeroed
      ``DedupStats`` stub so the dashboard panel's shape is stable for
      pre-listed entity types that haven't fired yet.

Relationship to ``tests/test_dedup.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``tests/test_dedup.py`` (W24-6) covers the registry's API surface in
isolation (``check_and_add`` semantics, TTL bucketing, ``get_stats`` /
``clear`` shape, thread-safety, memory bound) plus two basic wire-up
sanity tests (#15 decision_ledger, #16 fire_alert) that only assert the
``dedup_registry`` singleton's counters reflect a blocked duplicate.

This file goes ONE LAYER DEEPER: it asserts the DOWNSTREAM state (DB
rows / Trade objects) is NOT polluted by a duplicate. A regression that
imports ``dedup_registry`` but forgets to ``return`` after the
``check_and_add`` call would still pass test_dedup.py's wire-up sanity
tests (the registry would still record the duplicate) but would fail
these tests (the duplicate would land in the DB / ``store.trades``).

Test isolation
~~~~~~~~~~~~~~

Each test that needs a SQLite-scoped ledger / engine constructs a fresh
instance against ``tmp_path`` (via the conftest ``isolated_decision_ledger``
fixture for the ledger, and ``AlertEngine(db_path=tmp_path / ...)`` for
alerts). The conftest autouse ``_reset_store_factory_defaults`` fixture
clears ``dedup_registry`` + ``store`` + ``risk_manager`` + ``paper_sim``
between every test so no test's dedup keys / Trades leak into the next.

Sync vs async
~~~~~~~~~~~~~

Tests (1)–(4) are ``async def`` (the wiring entry points are async —
``record`` / ``_execute_fill`` / ``_process_trade`` — and ``fire_alert``
schedules an ``asyncio.create_task`` for the W23-3 WS broadcast that
needs a running event loop to land cleanly without leaking a coroutine).
Each is opted into asyncio via the per-test ``@pytest.mark.asyncio``
decorator (mirrors the convention in ``tests/test_dedup.py`` — the
repo's ``pytest.ini`` leaves ``asyncio_mode`` at the pytest-asyncio
default ``strict``).

Tests (5)–(7) are SYNC ``def`` — ``TestClient`` bridges each request
into the ASGI app via its own ``anyio`` portal (mirrors
``tests/test_openapi.py`` / ``tests/test_advanced_router.py``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads ``os.environ`` at module-import time. ────────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/dedup_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*`` / ``paper.*`` / ``risk.*`` / ``ml.*``) regardless of the cwd
# pytest was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.alerting import (  # noqa: E402
    Alert,
    AlertEngine,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
)
from core.data_store import Order, OrderStatus, Side, store  # noqa: E402
from core.decision_ledger import (  # noqa: E402
    STAGE_PREDICTION,
    STAGE_SIGNAL,
    DecisionLedger,
)
from core.dedup import dedup_registry  # noqa: E402
from core.live_fill_monitor import LiveFillMonitor  # noqa: E402
from paper.simulator import PaperSimulator  # noqa: E402


# ── Shared test fixtures ────────────────────────────────────────────────────

_TOKEN_ID = "0xdeadbeefcafe000000000000000000000000000000000000000000000000beef"


def _order(
    order_id: str = "paper-test-order",
    side: Side = Side.BUY,
    price: float = 0.55,
    size: float = 5.0,
    token_id: str = _TOKEN_ID,
    strategy: str = "signal_trader",
    decision_id: str = "",
    paper: bool = True,
) -> Order:
    """Build a minimal ``Order`` for the paper-simulator fill path.

    Mirrors the ``_order`` helper in ``tests/test_paper_simulator.py`` so
    the shape matches what ``strategies/base.submit_order`` produces in
    production (a paper BUY with size_remaining == size, status OPEN).
    """
    return Order(
        order_id=order_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        paper=paper,
        strategy=strategy,
        decision_id=decision_id,
    )


def _trade(
    trade_id: str = "trade-1",
    order_id: str = "order-1",
    side: str = "BUY",
    price: float = 0.55,
    size: float = 10.0,
    token_id: str = _TOKEN_ID,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal CLOB-shaped trade dict for ``_process_trade``.

    Mirrors the ``_trade`` helper in ``tests/test_live_fill_monitor.py``
    so the dict shape matches what ``clob_client.get_trades()`` returns in
    production.
    """
    base: dict[str, Any] = {
        "id": trade_id,
        "taker_order_id": order_id,
        "asset_id": token_id,
        "side": side,
        "price": str(price),
        "size": str(size),
        "status": "MATCHED",
    }
    if extra:
        base.update(extra)
    return base


def _alert(alert_id: str = "w25-7-test-alert", name: str = "w25_7_test") -> Alert:
    """Build a minimal ``Alert`` for the ``fire_alert`` path."""
    return Alert(
        alert_id=alert_id,
        timestamp=1234567890.0,
        category="risk",
        name=name,
        severity=SEVERITY_CRITICAL,
        message="W25-7 wiring test alert",
    )


# ═══════════════════════════════════════════════════════════════════════════
# (1) Decision ledger — duplicate (decision_id, stage, payload) is skipped
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_duplicate_decision_record_skipped_at_db_level(
    isolated_decision_ledger: DecisionLedger,
):
    """Calling ``record()`` twice with the EXACT same args must result in
    exactly ONE row in ``decision_events`` — the second call is silently
    dropped by the ``dedup_registry.check_and_add("decision", ...)`` gate
    in ``core/decision_ledger.py::record``.

    The dedup key is ``f"{decision_id}:{stage}:{payload_hash}"`` so a
    truly identical second call (same ``decision_id`` + same ``stage`` +
    same ``**data`` kwargs → same payload hash → same dedup key) is
    blocked. The 300 s TTL window is plenty for a unit test (we don't
    have to sleep past it).
    """
    did = "dec-w25-7-dup-test"
    # First call — must succeed and persist one row.
    await isolated_decision_ledger.record(
        did, STAGE_PREDICTION, token_id="TOK_DUP", strategy="signal_trader",
    )
    # Second call — same args; must be dedup'd (no second row).
    await isolated_decision_ledger.record(
        did, STAGE_PREDICTION, token_id="TOK_DUP", strategy="signal_trader",
    )

    chain = await isolated_decision_ledger.get_chain(did)
    assert len(chain) == 1, (
        f"duplicate record() must not insert a second row; got {len(chain)}"
    )
    # The single surviving row carries the right identity.
    assert chain[0]["decision_id"] == did
    assert chain[0]["stage"] == STAGE_PREDICTION
    assert chain[0]["token_id"] == "TOK_DUP"

    # The dedup registry's per-type counters reflect the blocked duplicate.
    stats = dedup_registry.get_stats(entity_type="decision")
    assert stats["total_seen"] == 2
    assert stats["unique_passed"] == 1
    assert stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# (1b) Decision ledger — unique decisions with DIFFERENT stages pass through
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unique_decision_records_with_different_stages_pass_through(
    isolated_decision_ledger: DecisionLedger,
):
    """A PREDICTION and a SIGNAL on the same ``decision_id`` are TWO
    distinct events (different ``stage`` → different dedup key) and must
    BOTH land in ``decision_events``.

    This is the canonical "12-stage decision chain" pattern: a single
    ``decision_id`` accumulates one row per stage as the prediction flows
    through risk → order → fill. The dedup gate MUST NOT collapse these
    into one row.
    """
    did = "dec-w25-7-multi-stage"
    await isolated_decision_ledger.record(
        did, STAGE_PREDICTION, token_id="TOK_MULTI", strategy="signal_trader",
        p_yes=0.62, confidence=0.71,
    )
    await isolated_decision_ledger.record(
        did, STAGE_SIGNAL, token_id="TOK_MULTI", strategy="signal_trader",
        edge=0.08,
    )

    chain = await isolated_decision_ledger.get_chain(did)
    assert len(chain) == 2, (
        f"two distinct stages must persist two rows; got {len(chain)}"
    )
    stages = {row["stage"] for row in chain}
    assert stages == {STAGE_PREDICTION, STAGE_SIGNAL}

    # Both events passed the dedup gate.
    stats = dedup_registry.get_stats(entity_type="decision")
    assert stats["unique_passed"] == 2
    assert stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (1c) Decision ledger — same stage + DIFFERENT payload passes through
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unique_decision_records_with_different_payloads_pass_through(
    isolated_decision_ledger: DecisionLedger,
):
    """A second ``record()`` call for the SAME ``(decision_id, stage)``
    but with a DIFFERENT ``**data`` payload must PASS through the dedup
    gate. The dedup key includes a SHA-256 of the JSON-serialised payload
    so a legitimate "last write wins" update (e.g. a re-prediction with
    a refreshed model version) is allowed — only byte-for-byte identical
    re-records are dedup'd.
    """
    did = "dec-w25-7-payload-update"
    # First PREDICTION with p_yes=0.62.
    await isolated_decision_ledger.record(
        did, STAGE_PREDICTION, token_id="TOK_UPD", strategy="signal_trader",
        p_yes=0.62, confidence=0.71,
    )
    # Second PREDICTION — same stage, DIFFERENT p_yes → different payload
    # hash → different dedup key → must pass through.
    await isolated_decision_ledger.record(
        did, STAGE_PREDICTION, token_id="TOK_UPD", strategy="signal_trader",
        p_yes=0.58, confidence=0.65,
    )

    chain = await isolated_decision_ledger.get_chain(did)
    assert len(chain) == 2, (
        "two distinct payloads for the same stage must persist two rows; "
        f"got {len(chain)}"
    )

    stats = dedup_registry.get_stats(entity_type="decision")
    assert stats["unique_passed"] == 2
    assert stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (2) Alerting — duplicate alert_id is skipped at DB level
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_duplicate_alert_fire_skipped_at_db_level(tmp_path: Path):
    """Calling ``fire_alert(alert)`` twice with the SAME ``alert_id``
    must result in exactly ONE row in the ``alerts`` SQLite table. The
    second call returns ``False`` (duplicate) and never reaches the
    ``_store`` INSERT.

    This mirrors the operator-facing contract: a single ``alert_id`` (the
    dedup key) yields exactly one alert card on the dashboard, even if
    the risk gate that fired it is re-evaluated multiple times within
    the 300 s TTL window (e.g. before the operator acknowledges).
    """
    engine = AlertEngine(db_path=tmp_path / "alerts_dup_test.db")
    alert = _alert(alert_id="w25-7-dup-alert")

    # First fire — returns True (unique), persists the row.
    ok1 = engine.fire_alert(alert)
    assert ok1 is True
    # Yield once so the fire-and-forget WS broadcast coroutine lands
    # (avoids leaking the create_task'd coroutine on test teardown).
    import asyncio as _asyncio
    await _asyncio.sleep(0)

    # Second fire — same alert_id; must be dedup'd.
    ok2 = engine.fire_alert(alert)
    assert ok2 is False, (
        "fire_alert must return False for a duplicate alert_id within "
        "the TTL window"
    )
    await _asyncio.sleep(0)

    # Only one row survived in the SQLite store.
    stats = engine.get_stats()
    assert stats["total_alerts"] == 1, (
        f"duplicate fire_alert must not insert a second row; got {stats['total_alerts']}"
    )
    assert stats["unacknowledged"] == 1

    # The dedup registry counters reflect the blocked duplicate.
    dedup_stats = dedup_registry.get_stats(entity_type="alert")
    assert dedup_stats["total_seen"] == 2
    assert dedup_stats["unique_passed"] == 1
    assert dedup_stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# (2b) Alerting — unique alerts with different alert_ids pass through
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unique_alerts_with_different_ids_pass_through(tmp_path: Path):
    """Two alerts with DIFFERENT ``alert_id``s must BOTH land in the
    ``alerts`` SQLite table. The dedup key is the ``alert_id`` itself
    (no payload hash component — alerting semantics are "one card per
    distinct id") so different ids → different keys → both pass.
    """
    engine = AlertEngine(db_path=tmp_path / "alerts_unique_test.db")
    alert_a = _alert(alert_id="w25-7-unique-a", name="alert_a")
    alert_b = _alert(alert_id="w25-7-unique-b", name="alert_b")

    ok_a = engine.fire_alert(alert_a)
    ok_b = engine.fire_alert(alert_b)
    assert ok_a is True
    assert ok_b is True

    import asyncio as _asyncio
    await _asyncio.sleep(0)
    await _asyncio.sleep(0)

    stats = engine.get_stats()
    assert stats["total_alerts"] == 2

    dedup_stats = dedup_registry.get_stats(entity_type="alert")
    assert dedup_stats["unique_passed"] == 2
    assert dedup_stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (3) Paper simulator — duplicate order_id is skipped at store level
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_duplicate_paper_fill_skipped_at_store_level(
    isolated_paper_sim: PaperSimulator,
):
    """Calling ``_execute_fill(order, fill_price)`` twice for the SAME
    order must result in exactly ONE ``Trade`` appended to
    ``store.trades``. The dedup key is ``f"paper:{order.order_id}"`` so
    a re-entry (e.g. the periodic ``_try_fill_orders`` loop racing a
    manual cancel that fires after the order's size_remaining was
    already zeroed) is silently dropped before the second Trade is
    constructed.
    """
    order = _order(order_id="paper-w25-7-dup", size=5.0)
    # Add the order to ``store.open_orders`` so ``store.update_order``
    # (called inside ``_execute_fill``) finds it + transitions it to
    # FILLED. Without this, the dedup gate still fires (it's the first
    # thing in ``_execute_fill``) but the order-state transition would
    # be silently skipped — we want the FULL fill path to run on the
    # first call so the second call has the realistic "order already
    # FILLED" state to dedup against.
    store.open_orders[order.order_id] = order

    # First call — fills the order, appends a Trade.
    await isolated_paper_sim._execute_fill(order, fill_price=0.55)
    # Second call — same order_id; must be dedup'd.
    await isolated_paper_sim._execute_fill(order, fill_price=0.55)

    assert len(store.trades) == 1, (
        f"duplicate _execute_fill must not append a second Trade; "
        f"got {len(store.trades)}"
    )
    trade = store.trades[0]
    assert trade.token_id == _TOKEN_ID
    assert trade.size == pytest.approx(5.0)
    assert trade.price == pytest.approx(0.55)
    assert trade.paper is True

    # The dedup registry counters reflect the blocked duplicate.
    stats = dedup_registry.get_stats(entity_type="fill")
    assert stats["total_seen"] == 2
    assert stats["unique_passed"] == 1
    assert stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# (3b) Paper simulator — unique orders pass through
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unique_paper_fills_with_different_orders_pass_through(
    isolated_paper_sim: PaperSimulator,
):
    """Two orders with DIFFERENT ``order_id``s must BOTH produce a
    ``Trade`` in ``store.trades``. The dedup key includes the
    ``order_id`` so distinct orders → distinct keys → both pass.
    """
    order_a = _order(order_id="paper-w25-7-unique-a", size=3.0)
    order_b = _order(order_id="paper-w25-7-unique-b", size=4.0)
    store.open_orders[order_a.order_id] = order_a
    store.open_orders[order_b.order_id] = order_b

    await isolated_paper_sim._execute_fill(order_a, fill_price=0.50)
    await isolated_paper_sim._execute_fill(order_b, fill_price=0.52)

    assert len(store.trades) == 2, (
        f"two distinct orders must produce two Trades; got {len(store.trades)}"
    )
    sizes = sorted(t.size for t in store.trades)
    assert sizes == [pytest.approx(3.0), pytest.approx(4.0)]

    stats = dedup_registry.get_stats(entity_type="fill")
    assert stats["unique_passed"] == 2
    assert stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (4) Live fill monitor — duplicate trade_id is skipped at store level
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_duplicate_live_fill_skipped_at_store_level():
    """Calling ``_process_trade(trade)`` twice with the SAME ``trade_id``
    must result in exactly ONE ``Trade`` appended to ``store.trades``.

    The live-fill monitor has TWO dedup layers:
      (a) local ``_last_trade_ids`` set — first-level, blocks the
          second call BEFORE the registry is consulted.
      (b) ``dedup_registry.check_and_add("fill", f"live:{trade_id}",
          ttl_seconds=3600)`` — second-level, surfaced via
          ``GET /api/dedup/stats`` for cross-path observability.

    To verify the W25-7 REGISTRY wiring (not just the local set), this
    test clears ``_last_trade_ids`` between the two calls so the
    registry is the ONLY gate. Without this clear, the local set would
    block the duplicate and the registry would never see the second
    call — passing the ``store.trades`` assertion but NOT verifying
    the registry is wired.

    ``_process_trade`` is called directly (bypassing the paper-mode
    short-circuit in ``_check_for_new_fills``) so the test doesn't need
    to override ``settings.paper_trade``.
    """
    monitor = LiveFillMonitor(poll_interval=0.01)
    trade = _trade(trade_id="live-w25-7-dup", order_id="order-live-dup")

    # First call — records the fill, registers the trade_id in BOTH the
    # local set and the global registry.
    await monitor._process_trade(trade)
    # Clear the LOCAL dedup set so the second call reaches the registry
    # (otherwise the local set would short-circuit before the registry
    # is consulted, and we wouldn't be testing the W25-7 wiring).
    monitor._last_trade_ids.clear()
    # Second call — same trade_id; the local set is empty so the call
    # proceeds to the registry, which blocks it as a duplicate.
    await monitor._process_trade(trade)

    assert len(store.trades) == 1, (
        f"duplicate _process_trade must not append a second Trade; "
        f"got {len(store.trades)}"
    )
    trade_record = store.trades[0]
    assert trade_record.trade_id == "live-w25-7-dup"
    # The live-fill monitor marks trades as paper=False (it only runs in
    # live mode — paper fills are handled by the paper simulator).
    assert trade_record.paper is False

    # The dedup registry counters reflect the blocked duplicate — BOTH
    # calls reached the registry (because we cleared the local set
    # between them), and the registry blocked the second.
    stats = dedup_registry.get_stats(entity_type="fill")
    assert stats["total_seen"] == 2
    assert stats["unique_passed"] == 1
    assert stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# (4b) Live fill monitor — unique trade_ids pass through
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unique_live_fills_with_different_trade_ids_pass_through():
    """Two trades with DIFFERENT ``trade_id``s must BOTH produce a
    ``Trade`` in ``store.trades``.
    """
    monitor = LiveFillMonitor(poll_interval=0.01)
    trade_a = _trade(trade_id="live-w25-7-unique-a", order_id="order-live-a")
    trade_b = _trade(trade_id="live-w25-7-unique-b", order_id="order-live-b")

    await monitor._process_trade(trade_a)
    await monitor._process_trade(trade_b)

    assert len(store.trades) == 2, (
        f"two distinct trade_ids must produce two Trades; got {len(store.trades)}"
    )
    ids = sorted(t.trade_id for t in store.trades)
    assert ids == ["live-w25-7-unique-a", "live-w25-7-unique-b"]

    stats = dedup_registry.get_stats(entity_type="fill")
    assert stats["unique_passed"] == 2
    assert stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (5) API route — GET /api/dedup/stats returns per-type counters
# ═══════════════════════════════════════════════════════════════════════════
def test_api_dedup_stats_returns_per_type_counters():
    """``GET /api/dedup/stats`` must return the same dict
    ``dedup_registry.get_stats()`` returns — a mapping from entity_type
    to its ``DedupStats`` asdict shape (entity_type / total_seen /
    duplicates_blocked / unique_passed / duplicate_rate).

    Verifies the route is wired to the singleton (a regression that
    constructs a fresh registry inside the handler would return ``{}``
    for an empty registry). The conftest autouse fixture clears the
    singleton before every test, so this test seeds two entity_types
    (order + alert) and asserts both are present in the response.
    """
    # Defensive: disable rate limiting (conftest already does this, but
    # we belt-and-braces it here in case this test runs in isolation
    # before conftest's disable lands).
    try:
        from api.rate_limit import limiter
        limiter.enabled = False
    except ImportError:
        pass

    # Seed two entity_types so the response isn't empty.
    assert dedup_registry.check_and_add("order", "w25-7-api-test-order") is True
    assert dedup_registry.check_and_add("order", "w25-7-api-test-order") is False
    assert dedup_registry.check_and_add("alert", "w25-7-api-test-alert") is True

    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    # conftest sets ``API_TOKEN=test-token-conftest`` via
    # ``os.environ.setdefault`` BEFORE any project module is imported, so
    # the bearer token below matches what ``enforce_api_auth`` accepts.
    response = client.get(
        "/api/dedup/stats",
        headers={"Authorization": "Bearer test-token-conftest"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    # The response is a dict keyed by entity_type (no envelope).
    assert isinstance(body, dict)
    assert "order" in body
    assert "alert" in body

    order_stats = body["order"]
    assert order_stats["entity_type"] == "order"
    assert order_stats["total_seen"] == 2
    assert order_stats["unique_passed"] == 1
    assert order_stats["duplicates_blocked"] == 1
    assert order_stats["duplicate_rate"] == pytest.approx(0.5)

    alert_stats = body["alert"]
    assert alert_stats["entity_type"] == "alert"
    assert alert_stats["total_seen"] == 1
    assert alert_stats["unique_passed"] == 1
    assert alert_stats["duplicates_blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# (6) API route — GET /api/dedup/stats?entity_type=X filters to one type
# ═══════════════════════════════════════════════════════════════════════════
def test_api_dedup_stats_with_entity_type_filter_returns_one_type():
    """``GET /api/dedup/stats?entity_type=alert`` returns the stats dict
    for ONE type — a flat ``DedupStats`` shape (NOT nested under a
    top-level key). This is the contract the dashboard's "Dedup" panel
    relies on when it polls a single type's counter for a sparkline.
    """
    try:
        from api.rate_limit import limiter
        limiter.enabled = False
    except ImportError:
        pass

    # Seed one entry under "alert" so the response isn't a zeroed stub.
    assert dedup_registry.check_and_add("alert", "w25-7-api-filter-test") is True

    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/dedup/stats?entity_type=alert",
        headers={"Authorization": "Bearer test-token-conftest"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    # Single-type shape — flat dict, NOT nested under "alert".
    assert isinstance(body, dict)
    assert "entity_type" in body
    assert body["entity_type"] == "alert"
    assert body["total_seen"] == 1
    assert body["unique_passed"] == 1
    assert body["duplicates_blocked"] == 0
    assert body["duplicate_rate"] == pytest.approx(0.0)
    # Belt-and-braces: no top-level "alert" key (would indicate the
    # handler ignored the ``entity_type`` query param).
    assert "alert" not in body


# ═══════════════════════════════════════════════════════════════════════════
# (7) API route — unknown entity_type returns a zeroed stub
# ═══════════════════════════════════════════════════════════════════════════
def test_api_dedup_stats_with_unknown_entity_type_returns_zeroed_stub():
    """``GET /api/dedup/stats?entity_type=audit`` (an entity_type that
    has never been recorded) returns a zeroed ``DedupStats`` stub so the
    API shape is stable for callers that pre-list the entity types they
    care about (e.g. the dashboard panel always renders the same 6
    rows: order / fill / decision / alert / audit / snapshot).
    """
    try:
        from api.rate_limit import limiter
        limiter.enabled = False
    except ImportError:
        pass

    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/dedup/stats?entity_type=audit",
        headers={"Authorization": "Bearer test-token-conftest"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_type"] == "audit"
    assert body["total_seen"] == 0
    assert body["unique_passed"] == 0
    assert body["duplicates_blocked"] == 0
    assert body["duplicate_rate"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# (8) API route — unauthenticated request returns 401 (auth enforced)
# ═══════════════════════════════════════════════════════════════════════════
def test_api_dedup_stats_unauthenticated_returns_401():
    """``GET /api/dedup/stats`` with NO ``Authorization`` header must
    return 401 — the dedup counters leak operational state (duplicate
    rate / total orders seen) that an unauthenticated observer could
    use to fingerprint the bot's traffic patterns. The route is NOT in
    ``PUBLIC_PATHS`` (only ``/api/health`` / ``/api/version`` / docs
    / metrics are).
    """
    try:
        from api.rate_limit import limiter
        limiter.enabled = False
    except ImportError:
        pass

    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/dedup/stats")  # no Authorization header

    assert response.status_code == 401, (
        f"unauthenticated dedup/stats must return 401; got {response.status_code}"
    )
