"""
tests/test_osm_integration.py — W18-1 OSM integration tests.

Closes the P0-C-01 finding from the W17-2 Bot Execution Engine Assessment:
the Order State Machine (``core/order_state_machine.py``) existed with the
full canonical lifecycle

    CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN →
        PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED / EXPIRED

but was NEVER invoked from the production trade path. W18-1 wires the OSM
into ``BaseStrategy.submit_order``, ``paper_sim.create_order`` /
``_execute_fill`` / ``cancel_order``, and adds the
``GET /api/orders/{order_id}/state`` HTTP surface.

These integration tests exercise the end-to-end trade path with the REAL
production code (no mocking of OSM internals) and verify that:

  1. ``BaseStrategy.submit_order`` (paper mode) creates an OSM entry and
     walks it through CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED →
     OPEN in the canonical order.
  2. The paper-sim fill loop's ``_execute_fill`` records the FILLED
     transition (and stamps ``filled_size`` / ``fill_price`` /
     ``pnl`` on the snapshot's metadata).
  3. ``paper_sim.cancel_order`` records the CANCELLED transition (the V13
     hook was previously broken: it called the module-level
     ``transition(order_id, ...)`` with the wrong signature — taking an
     ``order_id`` str instead of an ``Order`` instance — and the bogus
     kwargs ``reason=...`` was never accepted; the call silently raised
     and was swallowed by ``except: pass``).
  4. The OSM state matches the actual ``store.open_orders`` state
     (OPEN in both; FILLED in both after a fill; CANCELLED in both after
     a cancel).
  5. OSM failure (simulated via a monkeypatched ``osm.save`` that raises
     ``sqlite3.OperationalError``) does NOT break trading — the
     ``submit_order`` / ``create_order`` / ``_execute_fill`` / ``cancel_order``
     call sites all wrap their OSM writes in ``try/except`` so a
     persistence hiccup is logged and swallowed (mirrors the fail-soft
     contract of every other audit singleton in the codebase).
  6. ``osm.get_order`` returns ``None`` for an unknown ``order_id`` (so
     the HTTP endpoint correctly returns 404).
  7. ``osm.transition`` raises ``InvalidTransition`` for an illegal hop
     (e.g. FILLED → OPEN — a stale ref to a terminal order cannot
     resurrect it). The ``BaseStrategy.submit_order`` paper path
     swallows the ``InvalidTransition`` so a mid-sequence failure
     (e.g. a pre-existing terminal state) doesn't break the order
     creation.
  8. The ``osm`` module-level alias (``from core.order_state_machine
     import osm``) resolves to the same singleton as
     ``order_state_machine``.

Test isolation: each test gets a fresh ``OrderStateMachine`` instance
whose SQLite file lives under ``tmp_path`` (mirrors the ``machine``
fixture in ``tests/test_order_state_machine.py``). For tests that
exercise the production ``submit_order`` / ``create_order`` path, the
module-level ``order_state_machine`` singleton (constructed at import
time against the conftest-redirected
``/tmp/pmbot_conftest_isolation/order_state_machine.db`` path) is
monkeypatched to swap in the test-scoped instance — so every internal
``from core.order_state_machine import osm; osm.transition(...)``
call resolves to the test's own DB.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling
``tests/test_*.py``).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# ── Redirect ORDER_STATE_MACHINE_DB_PATH to /tmp BEFORE importing the
# module. The singleton ``order_state_machine`` is constructed at import
# time and reads its DB path from this env var (falling back to
# ``/app/data/order_state_machine.db`` — unwritable in the sandbox).
# ``setdefault`` lets an outer runner / sibling test file override if it
# needs to (mirrors ``tests/test_observability.py`` lines 59-67 and
# ``tests/test_order_state_machine.py`` lines 51-63).
_TMP_ROOT = Path("/tmp/osm_integration_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault(
    "ORDER_STATE_MACHINE_DB_PATH", str(_TMP_ROOT / "order_state_machine.db")
)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``strategies.*``) regardless of the
# cwd pytest was launched from. Mirrors the bootstrap pattern in every
# sibling ``tests/test_*.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import (  # noqa: E402
    OrderBook,
    PriceLevel,
    Side,
    store,
)
from core.order_state_machine import (  # noqa: E402
    InvalidTransition,
    Order,
    OrderState,
    OrderStateMachine,
    order_state_machine,
    osm,
)
from paper.simulator import PaperSimulator, paper_sim  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module (mirrors every sibling ``tests/test_*.py``).
pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────────────────
_TOKEN_ID = "0xosm_integration_test_token_0000000000000000000000000000001"


def _book(ask_price=0.50, ask_size=100.0, bid_price=0.49, bid_size=100.0) -> OrderBook:
    """Two-sided book with enough depth that a 5-share order pays only
    the flat 1-tick crossing penalty (no size impact, deterministic 0/1
    tick queue penalty)."""
    asks = [PriceLevel(price=ask_price, size=ask_size)] if ask_price is not None else []
    bids = [PriceLevel(price=bid_price, size=bid_size)] if bid_price is not None else []
    return OrderBook(token_id=_TOKEN_ID, bids=bids, asks=asks)


class _ConcreteStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for testing ``submit_order``.

    ``BaseStrategy._run`` is abstract — this subclass provides a no-op
    implementation so the class can be instantiated. Mirrors the pattern
    used by every sibling strategy (``SignalTraderStrategy``,
    ``MarketMakerStrategy``, ``ArbScannerStrategy``,
    ``QuantStrategyInstance``).
    """

    name = "osm_test_strategy"

    async def _run(self) -> None:  # pragma: no cover — not exercised here
        pass


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def test_osm(tmp_path):
    """Fresh ``OrderStateMachine`` whose SQLite file lives under ``tmp_path``.

    The module-level singleton ``order_state_machine`` (built at import
    time against the conftest-redirected ``/tmp/...`` path) is left
    untouched; this fixture returns a fresh instance scoped to the
    test's own ``tmp_path``. Mirrors the ``machine`` fixture in
    ``tests/test_order_state_machine.py``.
    """
    return OrderStateMachine(tmp_path / "osm_integration.db")


@pytest.fixture
def patched_osm(tmp_path, monkeypatch):
    """Replace the module-level ``osm`` / ``order_state_machine`` singletons
    with a test-scoped ``OrderStateMachine`` whose SQLite file lives
    under ``tmp_path``.

    Production code paths (``BaseStrategy.submit_order``,
    ``paper_sim.create_order``, ``_execute_fill``, ``cancel_order``)
    import the singleton lazily inside the function body:

        from core.order_state_machine import OrderState, osm
        osm.transition(order.order_id, OrderState.FILLED)

    Monkeypatching both ``core.order_state_machine.osm`` and
    ``core.order_state_machine.order_state_machine`` (and re-binding the
    ``osm`` symbol) ensures every ``from core.order_state_machine import
    osm`` call resolves to the test's own DB. The production import-time
    singleton is left intact for tests that don't use this fixture.
    """
    fresh = OrderStateMachine(tmp_path / "osm_patched.db")
    monkeypatch.setattr("core.order_state_machine.osm", fresh)
    monkeypatch.setattr("core.order_state_machine.order_state_machine", fresh)
    return fresh


@pytest.fixture
def fresh_store():
    """Reset the in-memory ``DataStore`` singleton between tests so
    positions, orders, P&L, and books from prior tests do not bleed
    into this one.

    The autouse ``_reset_store_factory_defaults`` fixture in
    ``tests/conftest.py`` already runs before every test, but it
    doesn't reset ``paper_sim._virtual_balance_usdc`` in lockstep with
    the ``store.paper_balance`` reset (the W11-6 contract). This fixture
    is additive — it just clears the orders / books / trades containers
    one more time so the test starts from a known-empty state.
    """
    store.open_orders.clear()
    store.order_history.clear()
    store.trades.clear()
    store.order_books.clear()
    store.event_log.clear()
    store.daily_pnl = 0.0
    store.paper_balance = 100.0
    paper_sim._virtual_balance_usdc = 100.0
    return store


@pytest.fixture
def strategy():
    """Fresh ``_ConcreteStrategy`` instance in paper mode."""
    return _ConcreteStrategy()


# ── (1) BaseStrategy.submit_order creates an OSM entry ──────────────────────
async def test_submit_order_creates_osm_entry_through_OPEN(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """``BaseStrategy.submit_order`` (paper mode) must create an OSM entry
    and walk it through CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED →
    OPEN in the canonical order before returning the Order to the caller.

    Verifies:
      - The OSM entry exists after submit_order returns.
      - The OSM state is OPEN (the final pre-fill hop).
      - The OSM order_id matches the production Order's order_id.
      - The OSM history is the canonical 5-snapshot chain.
      - The production Order is in store.open_orders with status OPEN.
    """
    # Mock the risk gate to always approve (the W18-1 task scope is the
    # OSM wiring, not the 22-gate risk engine; the integration test for
    # the risk pipeline is ``tests/integration/test_risk_pipeline.py``).
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.50, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-osm-test-1")

    # Production Order is returned with the pre-minted osm order_id.
    assert order is not None
    assert order.paper is True
    assert order.token_id == _TOKEN_ID

    # OSM entry exists and is OPEN.
    osm_order = patched_osm.get_order(order.order_id)
    assert osm_order is not None, "OSM entry must exist after submit_order"
    assert osm_order.state == OrderState.OPEN
    assert osm_order.order_id == order.order_id
    assert osm_order.token_id == _TOKEN_ID
    assert osm_order.side == "BUY"
    assert osm_order.price == pytest.approx(0.50)
    assert osm_order.size == pytest.approx(5.0)
    assert osm_order.decision_id == "dec-osm-test-1"

    # Canonical lifecycle chain (5 hops: CREATED → VALIDATED → SUBMITTED
    # → ACKNOWLEDGED → OPEN) is persisted in the audit trail.
    history = patched_osm.get_history(order.order_id)
    states = [h.state for h in history]
    assert states == [
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
    ], f"expected canonical lifecycle, got {states}"

    # Production Order is in store.open_orders with status OPEN.
    open_orders = await store.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].order_id == order.order_id
    assert open_orders[0].status.value == "OPEN"


# ── (2) _execute_fill records the FILLED transition ─────────────────────────
async def test_fill_transitions_osm_to_FILLED(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """``paper_sim._execute_fill`` must transition the OSM entry to FILLED
    and stamp ``filled_size`` / ``fill_price`` / ``pnl`` on the snapshot's
    metadata so the audit trail captures the executed quantity alongside
    the state.
    """
    # Bypass the risk gate.
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    # Submit a paper order against a BUY-crossing book.
    await store.update_order_book(_book(ask_price=0.50, ask_size=100.0))
    args = OrderArgs(token_id=_TOKEN_ID, price=0.55, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-osm-test-2")

    assert order is not None
    # OSM is OPEN at this point.
    pre = patched_osm.get_order(order.order_id)
    assert pre is not None
    assert pre.state == OrderState.OPEN
    assert pre.filled_size == pytest.approx(0.0)

    # Drive the paper fill loop manually (paper_sim._try_fill_orders is the
    # production method called by the 1s fill loop). The BUY order at
    # 0.55 crosses the 0.50 ask → fills at 0.50 + slippage.
    await paper_sim._try_fill_orders()

    # OSM is now FILLED with filled_size = 5.0 and metadata stamped.
    post = patched_osm.get_order(order.order_id)
    assert post is not None
    assert post.state == OrderState.FILLED
    assert post.filled_size == pytest.approx(5.0)
    # Fill metadata is stamped on the snapshot.
    assert "fill_price" in post.metadata
    assert "pnl" in post.metadata
    # Slippage model: 1 tick crossing + 0 size impact (5 shares <
    # 100-share top depth) + deterministic 0/1 tick queue position
    # (SHA-256 of order_id, byte & 1). For a BUY at best_ask=0.50, the
    # slipped fill price is either 0.51 (queue=0) or 0.52 (queue=1).
    fill_price = post.metadata["fill_price"]
    assert fill_price in (0.51, 0.52), (
        f"expected fill_price in {{0.51, 0.52}} (1-2 ticks slippage), "
        f"got {fill_price}"
    )


# ── (3) cancel_order records the CANCELLED transition ──────────────────────
async def test_cancel_transitions_osm_to_CANCELLED(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """``BaseStrategy.cancel_order`` (paper path delegates to
    ``paper_sim.cancel_order``) must transition the OSM entry to CANCELLED.

    This also exercises the W18-1 fix for the V13 hook: the V13 code
    called the module-level ``transition(order_id, OrderState.CANCELLED,
    reason="manual cancel")`` — the wrong signature (the module-level
    ``transition`` takes an ``Order`` instance, not an ``order_id`` str;
    the kwargs ``reason=`` was never accepted). The bogus call silently
    raised and was swallowed by the bare ``except: pass``. The W18-1 fix
    uses the ``osm.transition(order_id, state)`` convenience helper
    which loads + transitions + persists in one call.
    """
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.40, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-osm-test-3")
    assert order is not None
    assert patched_osm.get_order(order.order_id).state == OrderState.OPEN

    # Cancel via the BaseStrategy path (delegates to paper_sim.cancel_order
    # in paper mode). Should land CANCELLED in the OSM audit trail.
    ok = await strategy.cancel_order(order.order_id)
    assert ok is True

    post = patched_osm.get_order(order.order_id)
    assert post is not None
    assert post.state == OrderState.CANCELLED

    # History now has the CANCELLED snapshot at the tail.
    history = patched_osm.get_history(order.order_id)
    assert history[-1].state == OrderState.CANCELLED
    assert history[0].state == OrderState.CREATED


# ── (4) OSM state matches the actual order state ────────────────────────────
async def test_osm_state_matches_store_state_through_lifecycle(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """The OSM state and the ``store.open_orders[i].status`` field must
    agree at every point in the lifecycle (OPEN, FILLED, CANCELLED).
    """
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    await store.update_order_book(_book(ask_price=0.50, ask_size=100.0))

    # ── OPEN ──
    args = OrderArgs(token_id=_TOKEN_ID, price=0.55, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-osm-test-4a")
    assert order is not None
    open_orders = {o.order_id: o for o in await store.get_open_orders()}
    assert order.order_id in open_orders
    assert open_orders[order.order_id].status.value == "OPEN"
    assert patched_osm.get_order(order.order_id).state == OrderState.OPEN

    # ── FILLED ──
    await paper_sim._try_fill_orders()
    open_orders = {o.order_id: o for o in await store.get_open_orders()}
    # The filled order should no longer be in the open_orders set (the
    # fill loop calls ``store.update_order(order_id, status=FILLED)`` which
    # removes it from open_orders). The OSM should agree (FILLED is
    # terminal → not in OPEN list).
    assert order.order_id not in open_orders
    assert patched_osm.get_order(order.order_id).state == OrderState.FILLED

    # ── CANCELLED on a separate OPEN order ──
    args2 = OrderArgs(token_id=_TOKEN_ID, price=0.30, side=Side.BUY, size=5.0)
    order2 = await strategy.submit_order(args2, decision_id="dec-osm-test-4b")
    assert order2 is not None
    # price 0.30 is below best_ask 0.50 → won't fill → stays OPEN until cancel.
    await paper_sim._try_fill_orders()
    assert patched_osm.get_order(order2.order_id).state == OrderState.OPEN

    ok = await strategy.cancel_order(order2.order_id)
    assert ok is True
    assert patched_osm.get_order(order2.order_id).state == OrderState.CANCELLED


# ── (5) OSM failure does NOT break trading ──────────────────────────────────
async def test_osm_save_failure_does_not_break_submit_order(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """A persistence failure inside ``osm.save`` must NOT break the trade
    path — the ``submit_order`` / ``create_order`` / ``_execute_fill`` /
    ``cancel_order`` call sites all wrap their OSM writes in
    ``try/except`` so a persistence hiccup is logged and swallowed
    (mirrors the fail-soft contract of every other audit singleton).

    The production Order is still returned; only the OSM audit trail is
    degraded. This is the load-bearing isolation property that lets the
    OSM be wired into production without risking trading uptime.
    """
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    # Force ``osm.save`` to raise on every call. ``osm.transition`` calls
    # ``save`` internally (via the W18-1 convenience helper) so this also
    # breaks transitions — but the ``try/except`` wrappers in
    # ``BaseStrategy.submit_order`` and ``paper_sim.create_order`` swallow
    # the failure.
    def _broken_save(_order):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(patched_osm, "save", _broken_save)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.55, side=Side.BUY, size=5.0)
    # submit_order must NOT raise even though osm.save is broken.
    order = await strategy.submit_order(args, decision_id="dec-osm-test-5")
    assert order is not None, (
        "submit_order must still return the production Order even when "
        "the OSM persistence layer is broken"
    )
    assert order.token_id == _TOKEN_ID
    # Production Order is in store.open_orders (the OSM is best-effort).
    open_orders = {o.order_id: o for o in await store.get_open_orders()}
    assert order.order_id in open_orders


# ── (5b) OSM create_order failure does NOT break trading ────────────────────
async def test_osm_create_order_failure_does_not_break_submit_order(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """If ``osm.create_order`` itself raises (e.g. import-time error,
    constructor failure), the ``submit_order`` path must still proceed
    with the actual paper / live submission so trading is never blocked
    by the audit-trail layer.
    """
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    def _broken_create_order(**kwargs):
        raise RuntimeError("simulated OSM create_order failure")

    monkeypatch.setattr(patched_osm, "create_order", _broken_create_order)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.55, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-osm-test-5b")
    assert order is not None
    # Production Order is in store.open_orders (the OSM is best-effort).
    open_orders = {o.order_id: o for o in await store.get_open_orders()}
    assert order.order_id in open_orders


# ── (6) osm.get_order returns None for unknown id ───────────────────────────
async def test_osm_get_order_returns_None_for_unknown_id(patched_osm):
    """``osm.get_order(unknown_id)`` must return ``None`` so the HTTP
    endpoint correctly raises 404.
    """
    assert patched_osm.get_order("ord-does-not-exist") is None
    assert patched_osm.get_order("") is None


# ── (7) osm.transition raises InvalidTransition for illegal hop ─────────────
async def test_osm_transition_raises_InvalidTransition_for_illegal_hop(patched_osm):
    """``osm.transition`` must raise ``InvalidTransition`` for an illegal
    state transition (e.g. FILLED → OPEN — a stale ref to a terminal
    order cannot resurrect it). The fail-closed contract is encoded
    structurally in ``ALLOWED_TRANSITIONS``: terminal states map to an
    empty frozenset, so any attempted transition out of them raises.
    """
    order = patched_osm.create_order(
        strategy="test",
        token_id=_TOKEN_ID,
        side="BUY",
        price=0.50,
        size=5.0,
        order_id="ord-illegal-hop-test",
    )
    # Drive to FILLED via the canonical chain.
    for state in (
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
        OrderState.FILLED,
    ):
        patched_osm.transition(order.order_id, state)

    # FILLED → OPEN is illegal (FILLED is terminal).
    with pytest.raises(InvalidTransition):
        patched_osm.transition(order.order_id, OrderState.OPEN)

    # FILLED → CANCELLED is also illegal (terminal state, no exit).
    with pytest.raises(InvalidTransition):
        patched_osm.transition(order.order_id, OrderState.CANCELLED)


# ── (7b) osm.transition returns None for unknown order_id ────────────────────
async def test_osm_transition_returns_None_for_unknown_order_id(patched_osm):
    """``osm.transition(unknown_id, state)`` must return ``None`` (not
    raise) when the order_id has no prior snapshot — e.g. a legacy order
    that pre-dates the OSM wiring, or an unknown id. This lets the
    ``paper_sim.cancel_order`` path swallow the no-op silently rather
    than crashing on every cancel of a pre-W18-1 order.
    """
    result = patched_osm.transition("ord-never-existed", OrderState.CANCELLED)
    assert result is None


# ── (8) osm alias resolves to the same singleton ────────────────────────────
async def test_osm_alias_is_same_singleton_as_order_state_machine():
    """``from core.order_state_machine import osm`` must resolve to the
    same instance as ``order_state_machine`` (the alias is purely
    ergonomic — same SQLite DB, same state). Belt-and-braces against a
    future refactor that accidentally creates two singletons.
    """
    from core.order_state_machine import osm as _osm_alias
    from core.order_state_machine import order_state_machine as _singleton

    assert _osm_alias is _singleton, (
        "osm alias must be the same instance as order_state_machine"
    )


# ── (9) OSM helper methods accept both Order instance and order_id str ──────
async def test_osm_transition_accepts_order_instance_or_id_str(patched_osm):
    """The ``osm.transition`` convenience helper accepts either an
    ``Order`` instance (skipping the ``load`` round-trip) or an
    ``order_id`` string (loading the latest snapshot from the audit
    trail). Both forms must produce the same persisted post-state.
    """
    order = patched_osm.create_order(
        strategy="test",
        token_id=_TOKEN_ID,
        side="BUY",
        price=0.50,
        size=5.0,
        order_id="ord-transition-form-test",
    )
    # Form 1: pass the Order instance.
    updated = patched_osm.transition(order, OrderState.VALIDATED)
    assert updated is not None
    assert updated.state == OrderState.VALIDATED
    assert updated.order_id == order.order_id

    # Form 2: pass the order_id string (the latest snapshot is loaded).
    updated2 = patched_osm.transition(order.order_id, OrderState.SUBMITTED)
    assert updated2 is not None
    assert updated2.state == OrderState.SUBMITTED

    # Both forms hit the same audit trail.
    history = patched_osm.get_history(order.order_id)
    states = [h.state for h in history]
    assert states == [OrderState.CREATED, OrderState.VALIDATED, OrderState.SUBMITTED]


# ── (10) Live-mode submit_order records SUBMITTED → ACKNOWLEDGED → OPEN ─────
async def test_live_submit_order_records_OPEN_when_clob_succeeds(
    fresh_store, patched_osm, monkeypatch
):
    """``BaseStrategy.submit_order`` in live mode must record the
    SUBMITTED → ACKNOWLEDGED → OPEN hops in the OSM audit trail after
    ``clob_client.create_order`` returns successfully. The exchange-
    assigned order_id is stamped into the OSM entry's
    ``metadata.exchange_order_id`` so the OSM tracking id and the
    exchange id can be cross-referenced from the audit trail.
    """
    # Live-mode strategy.
    strategy = _ConcreteStrategy()
    strategy._paper = False

    # Bypass the risk gate (the live-mode risk gates — observation-only,
    # live-mode authorisation, etc. — are exercised in
    # ``tests/integration/test_risk_pipeline.py``; this test isolates the
    # OSM wiring).
    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    # Mock clob_client.create_order to return a fake success response.
    async def _fake_clob_create(_args):
        return {"orderID": "EXCHANGE-ORD-12345"}

    monkeypatch.setattr("core.clob_client.clob_client.create_order", _fake_clob_create)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.50, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-live-test")

    assert order is not None
    assert order.paper is False
    assert order.order_id == "EXCHANGE-ORD-12345"
    # Production Order is in store.open_orders.
    open_orders = {o.order_id: o for o in await store.get_open_orders()}
    assert "EXCHANGE-ORD-12345" in open_orders

    # OSM entry exists and is OPEN. The order_id is the W18-1 pre-minted
    # tracking id (``ord-{uuid}``), NOT the exchange-assigned id — they
    # are linked via the ``exchange_order_id`` metadata field.
    # Walk the audit trail and find the OPEN snapshot whose metadata
    # carries the exchange id.
    all_orders = []
    # Query by exchange_order_id in metadata — we need to enumerate via
    # the most-recent snapshot's order_id. Since we pre-minted the
    # tracking id, we know the order_id prefix is ``ord-``. Walk every
    # OSM entry to find the one whose metadata has the exchange id.
    # Simpler: rely on the fact that the live submit_order path records
    # SUBMITTED → ACKNOWLEDGED → OPEN on the same OSM tracking id, and
    # the SUBMITTED snapshot's metadata carries exchange_order_id.
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(patched_osm._db_path)
    conn.row_factory = _sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id FROM order_transitions WHERE "
        "metadata_json LIKE '%EXCHANGE-ORD-12345%' "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, (
        "exchange_order_id must be stamped into the OSM audit trail "
        "metadata so the OSM tracking id and the exchange id can be "
        "cross-referenced"
    )
    osm_tracking_id = row["order_id"]
    osm_order = patched_osm.get_order(osm_tracking_id)
    assert osm_order is not None
    assert osm_order.state == OrderState.OPEN
    assert osm_order.metadata.get("exchange_order_id") == "EXCHANGE-ORD-12345"

    # History is the canonical 5-snapshot chain.
    history = patched_osm.get_history(osm_tracking_id)
    states = [h.state for h in history]
    assert states == [
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.OPEN,
    ]


# ── (11) Live-mode submit_order records REJECTED when clob returns None ─────
async def test_live_submit_order_records_REJECTED_when_clob_fails(
    fresh_store, patched_osm, monkeypatch
):
    """When ``clob_client.create_order`` returns ``None`` (signing
    failure, HTTP error, exception inside the client), the
    ``BaseStrategy.submit_order`` live path must record the SUBMITTED →
    REJECTED hops in the OSM audit trail and return ``None``.
    """
    strategy = _ConcreteStrategy()
    strategy._paper = False

    async def _approve(_order):
        return True, "OK"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

    async def _failing_clob_create(_args):
        return None

    monkeypatch.setattr("core.clob_client.clob_client.create_order", _failing_clob_create)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.50, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-rejected-test")

    assert order is None, "submit_order must return None when clob_client fails"

    # OSM entry was created (CREATED + VALIDATED + SUBMITTED + REJECTED).
    # Find the OSM tracking id by walking the audit trail for REJECTED state.
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(patched_osm._db_path)
    conn.row_factory = _sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id FROM order_transitions WHERE state = 'REJECTED' "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "REJECTED state must be persisted in the OSM"
    osm_tracking_id = row["order_id"]
    osm_order = patched_osm.get_order(osm_tracking_id)
    assert osm_order is not None
    assert osm_order.state == OrderState.REJECTED
    # The rejection reason is stamped in metadata.
    assert osm_order.metadata.get("reason") == "clob_client returned None"


# ── (12) Risk-rejected order records VALIDATED → REJECTED ───────────────────
async def test_risk_rejected_order_records_REJECTED_in_osm(
    fresh_store, patched_osm, strategy, monkeypatch
):
    """When the risk gate rejects an order, ``BaseStrategy.submit_order``
    must record the CREATED → VALIDATED → REJECTED hops in the OSM audit
    trail (with the rejection reason stamped in metadata) so a rejected
    order is visible in the audit even though it never reached the
    exchange / paper sim.
    """
    async def _reject(_order):
        return False, "kill switch active (simulated)"

    monkeypatch.setattr("risk.manager.risk_manager.check_order", _reject)

    args = OrderArgs(token_id=_TOKEN_ID, price=0.50, side=Side.BUY, size=5.0)
    order = await strategy.submit_order(args, decision_id="dec-risk-rejected")
    assert order is None

    # The OSM entry must exist and be REJECTED.
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(patched_osm._db_path)
    conn.row_factory = _sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id FROM order_transitions WHERE state = 'REJECTED' "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None
    osm_order = patched_osm.get_order(row["order_id"])
    assert osm_order is not None
    assert osm_order.state == OrderState.REJECTED
    # Rejection reason is stamped in metadata.
    assert osm_order.metadata.get("reason") == "kill switch active (simulated)"
    # The history shows CREATED → VALIDATED → REJECTED.
    history = patched_osm.get_history(row["order_id"])
    states = [h.state for h in history]
    assert states == [OrderState.CREATED, OrderState.VALIDATED, OrderState.REJECTED]
