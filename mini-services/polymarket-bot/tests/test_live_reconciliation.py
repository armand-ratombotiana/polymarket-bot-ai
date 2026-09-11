"""tests/test_live_reconciliation.py — Live reconciler unit tests (W18-4, P0-C04).

Covers the six behaviour contracts required by the W18-4 task spec:

  (1) ``reconcile()`` returns ``is_clean=True`` when local state
      matches exchange state exactly (matched order-id sets, matched
      position sizes).
  (2) ``reconcile()`` detects stale-local orders (local order ids not
      present in the exchange's open-order set) → ``is_clean=False``
      + ``stale_local`` populated.
  (3) ``reconcile()`` detects orphaned-exchange orders (exchange order
      ids not present locally) → ``is_clean=False`` +
      ``orphaned_exchange`` populated.
  (4) ``reconcile()`` detects position-size mismatches (local
      ``Position.yes_shares`` diverges from exchange ``size`` beyond
      ``POSITION_TOLERANCE``) → ``is_clean=False`` +
      ``position_mismatches`` populated.
  (5) ``reconcile()`` short-circuits to a clean result in paper mode
      (``settings.paper_trade=True``) without ever calling the CLOB
      client — the CLOB REST endpoints are L2-auth'd against wallet
      creds that don't exist in paper mode.
  (6) The two API routes (``GET /api/reconciliation/live`` and
      ``POST /api/reconciliation/run``) surface the live reconciler's
      state correctly, including the ``no_reconciliation_yet`` sentinel
      before the first pass completes.

Test isolation strategy
~~~~~~~~~~~~~~~~~~~~~~~~
Each test patches ``clob_client.get_open_orders`` /
``clob_client.get_positions`` via ``monkeypatch.setattr`` on the
module-level singleton (``core.clob_client.clob_client``) so the test
drives the diff logic without hitting the network. The local
``DataStore`` singleton is reset by the autouse
``_reset_store_factory_defaults`` fixture in ``tests/conftest.py``
before every test (orders / positions / trades / equity cleared,
kill switch off, PnL zeroed).

For the API route tests, we build a minimal FastAPI app via the
``_build_client`` factory (mirrors the pattern in
``tests/test_live_safety_gate_api.py``) that registers ONLY the two
W18-4 endpoints so the test focuses on the contract under test —
no auth middleware, no other endpoints, no lifespan startup.

The module-level ``live_reconciler`` singleton is reset to a fresh
``LiveReconciler`` instance per-test via the ``fresh_reconciler``
fixture so the ``get_last_result()`` cache doesn't leak across
tests (the singleton is otherwise process-global — set once at
module import time and shared with the production lifespan handler).

Async / sync discipline
~~~~~~~~~~~~~~~~~~~~~~~~
``async def`` tests opt into pytest-asyncio individually via
``@pytest.mark.asyncio`` (the project's pytest.ini leaves
asyncio_mode=strict — module-level ``pytestmark`` would
additionally warn when applied to the sync API-route tests, so we
mark per-test instead). Sync tests use ``fastapi.testclient.TestClient``
directly (its sync portal runs the ASGI app synchronously, no
asyncio mark needed).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.data_store import Order, OrderStatus, Position, Side, store
from core.live_reconciliation import (
    LiveReconciler,
    POSITION_TOLERANCE,
    ReconciliationResult,
    live_reconciler,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_reconciler():
    """Return a fresh ``LiveReconciler`` (no shared state with the
    module-level ``live_reconciler`` singleton).

    The singleton is constructed at module-import time and shared
    with the production lifespan handler; without this reset, a test
    that set ``_last_result`` would leak into the next. We construct a
    fresh instance scoped to this fixture so the singleton's state is
    untouched and the test's view of ``get_last_result()`` is
    deterministic.
    """
    return LiveReconciler(interval=0.01)


@pytest.fixture
def live_mode(monkeypatch):
    """Patch ``settings.paper_trade=False`` so ``reconcile()`` doesn't
    short-circuit at the paper-mode guard.

    ``paper_trade`` is a plain pydantic bool field on ``Settings`` —
    ``monkeypatch.setattr`` on the singleton mutates it in place (the
    field has no validator that runs on assignment). Reverted
    automatically on test teardown by monkeypatch.
    """
    monkeypatch.setattr("config.settings.paper_trade", False)
    monkeypatch.setattr("config.settings.trading_mode", "live")


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_local_order(order_id: str, token_id: str = "tok-A") -> Order:
    """Build a local ``Order`` dataclass instance with the given id."""
    return Order(
        order_id=order_id,
        token_id=token_id,
        side=Side.BUY,
        price=0.50,
        size=10.0,
        status=OrderStatus.OPEN,
    )


def _make_exchange_order(order_id: str, token_id: str = "tok-A") -> dict:
    """Build a fake CLOB open-order dict (mirrors the field shape the
    real ``clob_client.get_open_orders()`` returns)."""
    return {"id": order_id, "order_id": order_id, "token_id": token_id, "side": "BUY"}


def _make_exchange_position(token_id: str, size: float) -> dict:
    """Build a fake CLOB position dict (mirrors the field shape the
    real ``clob_client.get_positions()`` returns)."""
    return {"asset_id": token_id, "size": str(size)}


async def _add_local_position(token_id: str, yes_shares: float) -> None:
    """Insert a position into the global ``store`` singleton (used by
    ``reconcile()`` via ``store.positions``)."""
    store.positions[token_id] = Position(
        token_id=token_id,
        yes_shares=yes_shares,
        no_shares=0.0,
    )


def _patch_clob(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_orders: list[dict] | None = None,
    positions: list[dict] | None = None,
) -> None:
    """Patch the module-level ``clob_client`` singleton's
    ``get_open_orders`` / ``get_positions`` async methods with
    ``AsyncMock`` returning the supplied lists. ``None`` leaves the
    method unpatched (so a test can opt out of one of the two
    patches)."""
    from core.clob_client import clob_client

    if open_orders is not None:
        monkeypatch.setattr(
            clob_client,
            "get_open_orders",
            AsyncMock(return_value=open_orders),
        )
    if positions is not None:
        monkeypatch.setattr(
            clob_client,
            "get_positions",
            AsyncMock(return_value=positions),
        )


# ── 1. Matching state → is_clean=True ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_matching_state_is_clean(live_mode, fresh_reconciler, monkeypatch):
    """When the local open-order ids and the exchange's open-order ids
    are identical AND the local position sizes match the exchange's
    sizes, ``reconcile()`` returns ``is_clean=True`` with empty
    discrepancy lists and the correct ``matched`` count.

    This is the happy-path baseline — every other test in this file
    perturbs exactly one of (orders / positions) and asserts the
    discrepancy list that surfaces. If this test fails, the
    reconciliation logic itself is broken and every downstream test's
    isolation assertion becomes unreliable.
    """
    # Two matching open orders on both sides.
    await store.add_order(_make_local_order("ord-1", "tok-A"))
    await store.add_order(_make_local_order("ord-2", "tok-B"))
    await _add_local_position("tok-A", yes_shares=100.0)
    _patch_clob(
        monkeypatch,
        open_orders=[_make_exchange_order("ord-1"), _make_exchange_order("ord-2")],
        positions=[_make_exchange_position("tok-A", 100.0)],
    )

    result = await fresh_reconciler.reconcile()

    assert isinstance(result, ReconciliationResult)
    assert result.is_clean is True, (
        f"matching state should produce is_clean=True; got {result!r}"
    )
    assert result.local_orders == 2
    assert result.exchange_orders == 2
    assert result.matched == 2
    assert result.stale_local == []
    assert result.orphaned_exchange == []
    assert result.position_mismatches == []
    assert result.timestamp > 0


# ── 2. Stale local orders ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_detects_stale_local_orders(live_mode, fresh_reconciler, monkeypatch):
    """When a local order id is NOT present in the exchange's open-order
    set, ``reconcile()`` returns ``is_clean=False`` with that id in
    ``stale_local`` (and the matching count is reduced by the
    number of stale ids).

    Models the real-world failure mode: the bot placed an order that
    the CLOB either never received (network blip between sign + POST)
    or that has already been filled / cancelled / expired without the
    bot's update hook firing. Without live reconciliation, the bot
    would continue to believe the order is OPEN and would never
    reconcile its in-memory state with the exchange's reality.
    """
    # Three local orders; only one is still open on the exchange.
    await store.add_order(_make_local_order("ord-1", "tok-A"))
    await store.add_order(_make_local_order("ord-stale-1", "tok-B"))
    await store.add_order(_make_local_order("ord-stale-2", "tok-C"))
    _patch_clob(
        monkeypatch,
        open_orders=[_make_exchange_order("ord-1")],
        positions=[],
    )

    result = await fresh_reconciler.reconcile()

    assert result.is_clean is False, (
        f"stale-local orders should flip is_clean to False; got {result!r}"
    )
    assert sorted(result.stale_local) == ["ord-stale-1", "ord-stale-2"], (
        f"stale_local should list the two local-only order ids; got {result.stale_local!r}"
    )
    assert result.orphaned_exchange == []
    assert result.local_orders == 3
    assert result.exchange_orders == 1
    assert result.matched == 1


# ── 3. Orphaned exchange orders ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_detects_orphaned_exchange_orders(live_mode, fresh_reconciler, monkeypatch):
    """When an exchange open-order id is NOT present locally,
    ``reconcile()`` returns ``is_clean=False`` with that id in
    ``orphaned_exchange``.

    Models the failure mode: an order placed out-of-band via the
    wallet's other UI (Polymarket.com web app, a different bot
    instance sharing the wallet). Without live reconciliation, the
    bot would never know about the orphaned order and couldn't
    account for the wallet's actual exposure — a real risk if the
    orphaned order fills and the bot's position-tracking doesn't
    reflect it.
    """
    # One local order; the exchange shows that one PLUS an orphan.
    await store.add_order(_make_local_order("ord-1", "tok-A"))
    _patch_clob(
        monkeypatch,
        open_orders=[
            _make_exchange_order("ord-1"),
            _make_exchange_order("ord-orphan-1"),
        ],
        positions=[],
    )

    result = await fresh_reconciler.reconcile()

    assert result.is_clean is False
    assert result.orphaned_exchange == ["ord-orphan-1"], (
        f"orphaned_exchange should list the exchange-only order id; "
        f"got {result.orphaned_exchange!r}"
    )
    assert result.stale_local == []
    assert result.local_orders == 1
    assert result.exchange_orders == 2
    assert result.matched == 1


# ── 4. Position mismatches ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_detects_position_mismatches(live_mode, fresh_reconciler, monkeypatch):
    """When the local ``Position.yes_shares`` diverges from the
    exchange's reported ``size`` by more than ``POSITION_TOLERANCE``,
    ``reconcile()`` returns ``is_clean=False`` with the divergence
    detailed in ``position_mismatches``.

    Models the failure mode: a fill that landed on the exchange but
    whose ``record_fill`` hook never fired locally (e.g. the WS feed
    dropped the fill event). Without live reconciliation, the bot
    would continue to believe its old position size and would
    mis-count exposure / PnL.
    """
    # Matching order sets so the only discrepancy is in positions.
    await store.add_order(_make_local_order("ord-1", "tok-A"))
    _patch_clob(
        monkeypatch,
        open_orders=[_make_exchange_order("ord-1")],
        positions=[
            _make_exchange_position("tok-A", 100.0),  # matches local
            _make_exchange_position("tok-B", 50.0),   # local missing
            _make_exchange_position("tok-C", 25.0),   # local has 30 (mismatch)
        ],
    )
    await _add_local_position("tok-A", yes_shares=100.0)  # matches
    await _add_local_position("tok-C", yes_shares=30.0)  # diverges by 5

    result = await fresh_reconciler.reconcile()

    assert result.is_clean is False, (
        f"position mismatch should flip is_clean to False; got {result!r}"
    )
    # Order-id diff is clean; only positions diverge.
    assert result.stale_local == []
    assert result.orphaned_exchange == []
    # Two mismatches: tok-B (local missing entirely → diff = 50) and
    # tok-C (local 30 vs exchange 25 → diff = -5). The order of the
    # list is non-deterministic (set iteration), so collect into a
    # dict keyed by token_id before asserting.
    mismatches_by_token = {m["token_id"]: m for m in result.position_mismatches}
    assert set(mismatches_by_token.keys()) == {"tok-B", "tok-C"}, (
        f"expected mismatches on tok-B and tok-C; got {mismatches_by_token.keys()!r}"
    )

    m_b = mismatches_by_token["tok-B"]
    assert m_b["local_size"] == 0.0
    assert m_b["exchange_size"] == 50.0
    assert m_b["diff"] == 50.0

    m_c = mismatches_by_token["tok-C"]
    assert m_c["local_size"] == 30.0
    assert m_c["exchange_size"] == 25.0
    assert m_c["diff"] == -5.0


@pytest.mark.asyncio
async def test_reconcile_position_within_tolerance_is_clean(
    live_mode, fresh_reconciler, monkeypatch
):
    """Position deltas ≤ ``POSITION_TOLERANCE`` (0.001 shares) are
    treated as equal — no mismatch is recorded.

    Polymarket's on-chain amounts are 6-decimal, so a 0.0005-share
    delta is sub-micro-cent and never reflects a real state
    disagreement (it's just float-rounding between the bot's
    ``record_fill`` arithmetic and the exchange's settled size).
    """
    await store.add_order(_make_local_order("ord-1", "tok-A"))
    _patch_clob(
        monkeypatch,
        open_orders=[_make_exchange_order("ord-1")],
        positions=[_make_exchange_position("tok-A", 100.0005)],
    )
    await _add_local_position("tok-A", yes_shares=100.0)

    result = await fresh_reconciler.reconcile()

    assert result.is_clean is True, (
        f"position delta within POSITION_TOLERANCE ({POSITION_TOLERANCE}) "
        f"should NOT register as a mismatch; got {result!r}"
    )
    assert result.position_mismatches == []


# ── 5. Paper-mode short-circuit ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_paper_mode_short_circuits(fresh_reconciler, monkeypatch):
    """In paper mode (``settings.paper_trade=True``), ``reconcile()``
    returns a clean ``ReconciliationResult`` WITHOUT calling the CLOB
    client — the L2-auth'd REST endpoints would 401 against the
    stubbed paper-mode creds, so the loop would log a noisy failure
    every minute for no signal.

    The test patches ``clob_client.get_open_orders`` /
    ``get_positions`` with mocks that, if called, would raise —
    proving the short-circuit doesn't reach them.
    """
    # Default test config has paper_trade=True (set by tests/conftest.py).
    # Belt-and-braces: assert it explicitly so a future conftest change
    # that flips the default can't silently mask this test's intent.
    from config import settings

    assert settings.paper_trade is True, (
        "tests/conftest.py must default paper_trade=True — if this fails, "
        "the W18-4 paper-mode short-circuit test is no longer proving what "
        "it claims to prove."
    )

    # If the short-circuit fails, these mocks raise — proving the
    # CLOB client was NOT supposed to be called.
    from core.clob_client import clob_client

    async def _explode(*args, **kwargs):
        raise AssertionError(
            "clob_client must NOT be called in paper mode — reconcile() "
            "should short-circuit before reaching get_open_orders / "
            "get_positions."
        )

    monkeypatch.setattr(clob_client, "get_open_orders", _explode)
    monkeypatch.setattr(clob_client, "get_positions", _explode)

    # The store should also NOT be consulted for orders / positions —
    # the paper-mode guard fires before any local-state read.
    result = await fresh_reconciler.reconcile()

    assert isinstance(result, ReconciliationResult)
    assert result.is_clean is True
    assert result.local_orders == 0
    assert result.exchange_orders == 0
    assert result.matched == 0
    assert result.stale_local == []
    assert result.orphaned_exchange == []
    assert result.position_mismatches == []


# ── 6. API routes ──────────────────────────────────────────────────────────


def _build_client() -> TestClient:
    """Build a ``TestClient`` against a minimal FastAPI app with only
    the two W18-4 endpoints registered.

    Mirrors the pattern in ``tests/test_live_safety_gate_api.py`` —
    registering only the routes under test keeps the test focused on
    the W18-4 contract (no auth middleware, no other endpoints).
    """
    app = FastAPI()

    from core.live_reconciliation import live_reconciler as _singleton

    @app.get("/api/reconciliation/live", tags=["system"])
    async def live_reconciliation_status():
        result = _singleton.get_last_result()
        if result is None:
            return {"status": "no_reconciliation_yet"}
        return {
            "timestamp": result.timestamp,
            "is_clean": result.is_clean,
            "local_orders": result.local_orders,
            "exchange_orders": result.exchange_orders,
            "matched": result.matched,
            "stale_local": result.stale_local,
            "orphaned_exchange": result.orphaned_exchange,
            "position_mismatches": result.position_mismatches,
            "fill_mismatches": result.fill_mismatches,
        }

    @app.post("/api/reconciliation/run", tags=["system"])
    async def run_reconciliation_now():
        result = await _singleton.reconcile()
        return {"is_clean": result.is_clean, "details": result.__dict__}

    return TestClient(app)


def test_get_live_reconciliation_no_result_yet():
    """Before the first reconciliation pass completes,
    ``GET /api/reconciliation/live`` returns
    ``{"status": "no_reconciliation_yet"}`` rather than a 4xx / 5xx.

    Surfaces the ``get_last_result() is None`` branch — the singleton
    is constructed at module-import time with ``_last_result=None``;
    without an explicit reset, a prior test that set ``_last_result``
    would mask this branch.
    """
    # Reset the singleton's last-result cache so the test sees the
    # pre-first-pass state. The autouse conftest reset doesn't touch
    # the live_reconciler singleton (it's a W18-4 addition), so we
    # reset it explicitly here.
    live_reconciler._last_result = None

    client = _build_client()
    response = client.get("/api/reconciliation/live")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "no_reconciliation_yet"}


def test_get_live_reconciliation_with_result():
    """After at least one pass has run, ``GET /api/reconciliation/live``
    returns the full ``ReconciliationResult`` shape (timestamp,
    is_clean, counts, discrepancy lists).

    The test injects a synthetic ``ReconciliationResult`` directly
    into the singleton's ``_last_result`` cache (bypassing
    ``reconcile()``) so the route handler's serialization path is
    exercised independently of the reconciliation logic.
    """
    # Inject a synthetic dirty result.
    now = time.time()
    live_reconciler._last_result = ReconciliationResult(
        timestamp=now,
        local_orders=3,
        exchange_orders=2,
        matched=1,
        stale_local=["ord-stale-1"],
        orphaned_exchange=["ord-orphan-1"],
        position_mismatches=[{
            "token_id": "tok-X",
            "local_size": 10.0,
            "exchange_size": 15.0,
            "diff": 5.0,
        }],
        is_clean=False,
    )

    client = _build_client()
    response = client.get("/api/reconciliation/live")

    assert response.status_code == 200
    body = response.json()
    assert body["timestamp"] == now
    assert body["is_clean"] is False
    assert body["local_orders"] == 3
    assert body["exchange_orders"] == 2
    assert body["matched"] == 1
    assert body["stale_local"] == ["ord-stale-1"]
    assert body["orphaned_exchange"] == ["ord-orphan-1"]
    assert body["position_mismatches"] == [{
        "token_id": "tok-X",
        "local_size": 10.0,
        "exchange_size": 15.0,
        "diff": 5.0,
    }]
    # fill_mismatches defaults to [] in the dataclass — the route
    # surfaces it so a future fill-count check (W18-5 / similar) can
    # populate it without a route change.
    assert body["fill_mismatches"] == []


def test_post_run_reconciliation_in_paper_mode():
    """``POST /api/reconciliation/run`` returns a well-formed body in
    paper mode — ``reconcile()`` short-circuits to a clean result.

    The route is sync-TestClient-callable (FastAPI's TestClient runs
    the ASGI app in a sync portal so async handlers are awaited
    synchronously). The test relies on the autouse conftest reset
    having ``settings.paper_trade=True`` (the default sandbox state).
    """
    # Force the singleton into paper mode (belt-and-braces — conftest
    # already defaults paper_trade=True; this makes the assertion's
    # intent explicit).
    live_reconciler._last_result = None

    client = _build_client()
    response = client.post("/api/reconciliation/run")

    assert response.status_code == 200
    body = response.json()
    assert body["is_clean"] is True
    details = body["details"]
    assert details["local_orders"] == 0
    assert details["exchange_orders"] == 0
    assert details["matched"] == 0
    assert details["is_clean"] is True
    assert details["stale_local"] == []
    assert details["orphaned_exchange"] == []
    assert details["position_mismatches"] == []


def test_post_run_reconciliation_in_live_mode_with_mismatch(live_mode):
    """In live mode with a real discrepancy, ``POST
    /api/reconciliation/run`` returns ``is_clean=False`` with the
    discrepancy details populated in the ``details`` envelope.

    The test patches ``clob_client.get_open_orders`` /
    ``get_positions`` with deterministic mismatched state so the
    route exercises the same diff path the background loop does.
    """
    from core.clob_client import clob_client

    # One local order; the exchange shows a different one (stale +
    # orphaned).
    store.open_orders.clear()
    store.positions.clear()
    # Synchronous set: ``store.open_orders`` is a plain dict (the
    # async ``add_order`` acquires the lock; direct dict mutation is
    # fine in tests because the test thread is the only producer).
    store.open_orders["ord-local-1"] = _make_local_order("ord-local-1")

    async def _fake_get_open_orders():
        return [_make_exchange_order("ord-exchange-1")]

    async def _fake_get_positions():
        return []

    # ``monkeypatch`` isn't in scope here (the fixture patches the
    # settings attr but doesn't pass the monkeypatch instance). Use
    # direct setattr — the route's import of ``clob_client`` resolves
    # to the same singleton, so the patch is picked up. Restore in
    # the finally block so the patch doesn't leak.
    orig_orders = clob_client.get_open_orders
    orig_positions = clob_client.get_positions
    clob_client.get_open_orders = _fake_get_open_orders
    clob_client.get_positions = _fake_get_positions

    try:
        client = _build_client()
        response = client.post("/api/reconciliation/run")
    finally:
        clob_client.get_open_orders = orig_orders
        clob_client.get_positions = orig_positions

    assert response.status_code == 200
    body = response.json()
    assert body["is_clean"] is False
    details = body["details"]
    assert details["local_orders"] == 1
    assert details["exchange_orders"] == 1
    assert details["matched"] == 0
    assert details["stale_local"] == ["ord-local-1"]
    assert details["orphaned_exchange"] == ["ord-exchange-1"]
    assert details["is_clean"] is False


# ── 7. Lifecycle: start() / stop() are idempotent ──────────────────────────


@pytest.mark.asyncio
async def test_start_stop_idempotent(fresh_reconciler):
    """``start()`` and ``stop()`` are idempotent — calling either twice
    does not spawn a second background task or raise.

    Guards against a real-world failure mode: a FastAPI lifespan
    handler that fires twice under reload (uvicorn --reload) would
    otherwise spawn two background tasks racing against each other
    for the same ``_last_result`` cache.
    """
    # Set a tiny interval so the loop fires immediately and we can
    # observe ``_last_result`` being populated before stop().
    fresh_reconciler.interval = 0.01

    await fresh_reconciler.start()
    await fresh_reconciler.start()  # idempotent: no second task
    assert fresh_reconciler._running is True
    assert fresh_reconciler._task is not None

    # Give the loop one tick to populate _last_result (paper mode is
    # the default sandbox state — the first iteration returns a clean
    # result immediately).
    await asyncio.sleep(0.05)

    await fresh_reconciler.stop()
    await fresh_reconciler.stop()  # idempotent: no error

    assert fresh_reconciler._running is False
    assert fresh_reconciler._task is None
    # The loop should have produced at least one result before stop.
    last = fresh_reconciler.get_last_result()
    assert last is not None, (
        "reconcile loop should have populated _last_result before stop()"
    )
    assert last.is_clean is True  # paper mode → clean


# ── 8. Failure isolation: clob_client raises → per-section guard ────────────


@pytest.mark.asyncio
async def test_reconcile_clob_client_failure_isolated(live_mode, fresh_reconciler, monkeypatch):
    """If ``clob_client.get_open_orders()`` raises (network outage /
    circuit breaker OPEN), ``reconcile()`` does NOT propagate the
    exception — the per-section ``except Exception`` catches it,
    logs a warning, sets ``exchange_orders=[]``, and continues to
    the return statement.

    Models the real-world failure mode: a transient CLOB 5xx /
    timeout would otherwise kill the loop and the operator would
    lose visibility into the bot's local-vs-exchange drift until the
    process is restarted.

    Belt-and-braces with the outer-except test below: this test
    exercises the per-section guard (the inner ``try`` around
    ``clob_client.get_open_orders``), the outer test exercises the
    outer ``try`` (catches failures in the local-store read or the
    diff logic).
    """
    from core.clob_client import clob_client

    async def _boom():
        raise RuntimeError("simulated CLOB outage")

    # Patch BOTH async methods — the per-section guard around
    # ``clob_client.get_open_orders()`` catches the order-fetch
    # failure; the per-section guard around ``clob_client.get_positions()``
    # catches the position-fetch failure. Neither propagates.
    monkeypatch.setattr(clob_client, "get_open_orders", _boom)
    monkeypatch.setattr(clob_client, "get_positions", _boom)

    result = await fresh_reconciler.reconcile()

    # The order-fetch failure is caught by the per-section
    # ``except Exception`` block (logs a warning, sets
    # exchange_orders=[]); the outer try/except doesn't fire.
    # So we have a local_orders count (0 here because the test didn't
    # seed any) and an empty exchange_orders, and the diff treats
    # everything as orphaned-local-only IF the local set is non-empty.
    # With an empty local set AND empty exchange set, is_clean=True.
    # We assert the result is well-formed (no exception) and the
    # failure was logged (verified by the absence of an unhandled
    # exception bubbling out of ``reconcile``).
    assert isinstance(result, ReconciliationResult)
    # Empty local + empty exchange = no discrepancies to flag.
    assert result.is_clean is True
    assert result.local_orders == 0
    assert result.exchange_orders == 0


@pytest.mark.asyncio
async def test_reconcile_outer_failure_returns_dirty(fresh_reconciler, monkeypatch):
    """If an unexpected exception fires inside the main reconcile
    body (outside the per-section guards around clob_client calls),
    the outer ``try/except Exception`` catches it and returns
    ``is_clean=False`` so the operator sees the failure on the
    dashboard rather than the loop crashing.

    The test forces ``store.get_open_orders()`` (an awaitable on
    ``DataStore``) to raise — the per-section guards don't cover it,
    so the outer ``except`` is the only line of defence.
    """
    # Switch OFF paper mode so reconcile() reaches the inner body
    # (paper mode short-circuits before the failing call).
    monkeypatch.setattr("config.settings.paper_trade", False)

    # Patch ``store.get_open_orders`` to raise. We patch the bound
    # method on the global singleton so the route's
    # ``from core.data_store import store`` resolves to the patched
    # instance.
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated local-store outage")

    monkeypatch.setattr(store, "get_open_orders", _boom)

    result = await fresh_reconciler.reconcile()

    assert isinstance(result, ReconciliationResult)
    assert result.is_clean is False, (
        "outer-except path should return is_clean=False so the operator "
        "sees the failure on the dashboard rather than a stale clean "
        f"result. Got: {result!r}"
    )
    assert result.local_orders == 0
    assert result.exchange_orders == 0
