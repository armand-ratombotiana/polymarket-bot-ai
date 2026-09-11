"""
tests/test_strategy_base.py — Unit tests for strategies/base.py.

X11 — BaseStrategy unit tests.

Covers the five behaviours required by the task spec:

  (1) ``submit_order`` passes through the risk gate when risk approves —
      the order is forwarded to ``paper_sim.create_order`` and the
      resulting ``Order`` is returned unchanged (no strategy-layer
      synthesis in paper mode).
  (2) ``submit_order`` returns ``None`` when risk rejects — ``paper_sim``
      is NOT called and the rejection is short-circuited.
  (3) ``submit_order`` in paper mode delegates to ``paper_sim.create_order``
      with the correct ``OrderArgs`` / ``strategy`` / ``decision_id`` so
      the simulator can attribute the resulting fill to the originating
      strategy + decision chain.
  (4) ``cancel_order`` removes the order from ``store.open_orders`` —
      exercised end-to-end through the REAL ``paper_sim`` so the contract
      ("removes from open_orders") is verified, not just the delegation.
  (5) ``start`` / ``stop`` lifecycle works — ``start`` flips ``_running``
      to True and schedules the ``_run`` task; ``stop`` cancels the task
      and flips ``_running`` back to False (and is idempotent on re-call).

Approach
--------
``BaseStrategy`` is abstract (``_run`` is ``@abstractmethod``); a tiny
``_StubStrategy`` subclass provides a runnable ``_run`` whose body blocks
on an ``asyncio.Event`` so the task stays alive until ``stop()`` cancels
it. This lets test 5 exercise the real lifecycle (``asyncio.create_task``,
``task.cancel()``, ``await task`` swallowing ``CancelledError``).

The risk gate and the paper simulator are swapped out via
``monkeypatch.setattr`` on the ``strategies.base`` module's bindings of
``risk_manager`` and ``paper_sim`` — the same singleton bindings the
production code path uses inside ``submit_order`` / ``cancel_order``.
``unittest.mock.AsyncMock`` is used to capture call args and assert await
counts. For test 4 we keep the REAL ``paper_sim`` so the end-to-end
"removes from open_orders" contract is exercised (not just the
delegation); ``cancel_order`` doesn't consult the risk gate at all, so
no mock is required there.

Env-var redirection and the autouse singleton-reset live in
``tests/conftest.py`` (T15); this file mirrors the env-var redirect
block purely so it remains self-contained when imported outside the
pytest runner (an IDE that doesn't load conftest first). The redirect
uses ``setdefault`` so it never clobbers a path the conftest already
set.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/strategy_base_tests")
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
    # Force the canonical trading mode to paper + live disabled so the
    # paper-mode branch of ``submit_order`` (``self._paper is True``) is
    # the path exercised end-to-end without the live-trading gate
    # short-circuiting anything.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-strategy-base",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``strategies.*``) regardless of the
# cwd pytest was launched from. Mirrors the bootstrap pattern in
# ``tests/test_paper_simulator.py`` and ``tests/test_risk_manager.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import Order, OrderStatus, Side, store  # noqa: E402
from strategies import base as base_module  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` declares ``testpaths = tests`` and
# ``asyncio_mode`` is left at the pytest-asyncio default (``strict``); the
# module-level ``pytestmark`` idiom opts every async test in without
# editing ``pytest.ini`` / ``pyproject.toml`` (both forbidden by the X11
# task constraint: "Do NOT edit existing files").
pytestmark = pytest.mark.asyncio


# ── Stub strategy ────────────────────────────────────────────────────────────
class _StubStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for lifecycle tests.

    ``_run`` blocks on an ``asyncio.Event`` that is never set, so the
    task stays alive until ``stop()`` cancels it. This mirrors the
    real-world strategy contract (``_run`` is a long-running loop) and
    lets test 5 exercise the real ``start`` → ``stop`` cycle without an
    early return racing the assertions. The event is created in
    ``__init__`` (which runs inside the test's event loop under
    pytest-asyncio strict mode), so it is correctly bound to the loop
    the task will run on.
    """

    name: str = "stub"

    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    async def _run(self) -> None:
        # Blocks indefinitely until cancelled by ``stop()``.
        await self._gate.wait()


# ── Helpers ──────────────────────────────────────────────────────────────────
_TOKEN_ID = "0xstrategy_base_test_token_id_deadbeef"


def _order_args(
    *,
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 2.0,
    token_id: str = _TOKEN_ID,
) -> OrderArgs:
    """Build a minimal ``OrderArgs`` payload for ``submit_order``."""
    return OrderArgs(token_id=token_id, price=price, side=side, size=size)


# ── (1) submit_order passes through risk gate ───────────────────────────────
async def test_submit_order_passes_through_risk_gate_when_approved(monkeypatch):
    """When the risk gate returns ``(True, "OK")``, ``submit_order`` must
    forward the order to ``paper_sim.create_order`` and return the
    resulting ``Order`` unchanged.

    The risk gate is consulted BEFORE ``paper_sim`` so a rejecting gate
    never reaches the simulator; this test pins that the approved path
    actually delegates by (a) mocking ``paper_sim.create_order`` to
    return a sentinel ``Order`` and (b) asserting the returned value IS
    that sentinel (i.e. the strategy layer doesn't synthesise its own
    Order in paper mode — it returns exactly what the simulator hands
    back).

    Also pins the provisional ``Order`` shape that the strategy hands to
    the risk gate: the gate must see the same token_id / side / price /
    size as the originating ``OrderArgs``, attributed to this strategy,
    flagged ``paper=True`` (so live-only gates don't fire), and carrying
    the ``decision_id`` for ledger linkage.
    """
    strat = _StubStrategy()
    # Belt-and-braces: the conftest's ``TRADING_MODE=paper`` redirect
    # must have surfaced in ``settings.paper_trade`` at BaseStrategy
    # construction time. If this fails, the env-var redirect in
    # ``tests/conftest.py`` is no longer taking effect (a regression in
    # the test bootstrap that would silently flip the live-mode branch).
    assert strat._paper is True

    sentinel_order = Order(
        order_id="paper-sentinel",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-1",
    )

    # Mock the risk gate to approve unconditionally.
    mock_risk = SimpleNamespace(
        check_order=AsyncMock(return_value=(True, "OK")),
    )
    # Mock paper_sim.create_order to return the sentinel.
    mock_paper = SimpleNamespace(
        create_order=AsyncMock(return_value=sentinel_order),
    )
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-1")

    # (a) Risk gate was awaited exactly once with the provisional Order.
    mock_risk.check_order.assert_awaited_once()
    provisional = mock_risk.check_order.await_args.args[0]
    assert isinstance(provisional, Order)
    assert provisional.token_id == args.token_id
    assert provisional.side == args.side
    assert provisional.price == args.price
    assert provisional.size == args.size
    assert provisional.strategy == strat.name
    assert provisional.paper is True
    assert provisional.decision_id == "dec-1"

    # (b) Paper sim was awaited exactly once with the correct payload.
    # W18-1 — submit_order now passes a pre-minted ``order_id`` so the
    # OSM audit trail and the in-memory ``Order`` share one identity;
    # ``assert_awaited_once_with`` would require us to predict the uuid,
    # so we just check the call was made with the right args / strategy /
    # decision_id (the order_id kwarg is checked separately for shape).
    mock_paper.create_order.assert_awaited_once()
    call_args, call_kwargs = mock_paper.create_order.await_args
    assert call_args == (args,)
    assert call_kwargs.get("strategy") == strat.name
    assert call_kwargs.get("decision_id") == "dec-1"
    # The pre-minted order_id is a non-empty string with the paper- prefix
    # so the OSM audit trail and the in-memory Order share one identity.
    assert call_kwargs.get("order_id", "").startswith("paper-")

    # (c) The returned Order is the sentinel (no synthesis at the
    #     strategy layer in paper mode).
    assert result is sentinel_order


# ── (2) submit_order returns None when risk rejects ─────────────────────────
async def test_submit_order_returns_none_when_risk_rejects(monkeypatch):
    """When the risk gate returns ``(False, reason)``, ``submit_order``
    must short-circuit and return ``None`` — ``paper_sim.create_order``
    must NOT be invoked (otherwise a rejected order would still hit the
    book).

    The rejection path also records a ``RISK_REJECTED`` stage in the
    decision ledger (best-effort, try/except-swallowed); this test does
    not assert on that side-effect (it's not part of the X11 contract)
    but relies on the try/except being non-fatal so the ``return None``
    is reached cleanly.
    """
    strat = _StubStrategy()

    mock_risk = SimpleNamespace(
        check_order=AsyncMock(return_value=(False, "Mock rejection: cash reserve breach")),
    )
    # AsyncMock with no return_value configured — if it is ever awaited,
    # ``assert_not_awaited`` below will fail loudly.
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-rej")

    # (a) Returned None — the canonical "rejected" sentinel.
    assert result is None
    # (b) paper_sim.create_order was never awaited — the rejection
    #     short-circuited before any simulator interaction.
    mock_paper.create_order.assert_not_awaited()


# ── (3) submit_order creates paper order in paper mode ──────────────────────
async def test_submit_order_creates_paper_order_in_paper_mode(monkeypatch):
    """In paper mode (``self._paper is True``), an approved order must be
    routed to ``paper_sim.create_order`` with the originating
    ``OrderArgs`` and the ``strategy`` / ``decision_id`` propagated.

    This pins the paper-mode branch of ``submit_order`` end-to-end:
      - The ``strategy`` kwarg matches ``self.name`` (so the simulator
        can attribute the resulting fill to the originating strategy).
      - The ``decision_id`` kwarg propagates verbatim (so the
        decision-ledger ORDER stage can be linked to the originating
        PREDICTION → SIGNAL → RISK_APPROVED chain).
      - The returned ``Order`` mirrors the ``OrderArgs`` payload and is
        flagged ``paper=True`` (the simulator never produces a live
        Order in paper mode).
    """
    strat = _StubStrategy()
    assert strat._paper is True  # paper-mode branch is the path under test

    mock_risk = SimpleNamespace(
        check_order=AsyncMock(return_value=(True, "OK")),
    )

    # Build the Order the real PaperSimulator.create_order would have
    # produced: identity fields mirror ``args``, attributed to the
    # caller's strategy + decision_id, paper=True.
    # W18-1 — accepts the optional ``order_id`` kwarg that
    # ``BaseStrategy.submit_order`` now passes so the OSM audit trail and
    # the in-memory ``Order`` share one identity.
    def _create_order(args, strategy="", decision_id="", order_id=None):
        return Order(
            order_id=order_id or "paper-mock-1234",
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            strategy=strategy,
            paper=True,
            decision_id=decision_id,
        )

    mock_paper = SimpleNamespace(create_order=AsyncMock(side_effect=_create_order))
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args(side=Side.BUY, price=0.42, size=3.5)
    result = await strat.submit_order(args, decision_id="dec-paper-3")

    # (a) paper_sim.create_order was awaited exactly once with the
    #     originating OrderArgs (identity check — same instance) and
    #     the strategy / decision_id propagated verbatim. W18-1 also
    #     passes a pre-minted ``order_id`` (paper-{uuid}) — checked for
    #     shape rather than value because the uuid is non-deterministic.
    mock_paper.create_order.assert_awaited_once()
    call_args, call_kwargs = mock_paper.create_order.await_args
    assert call_args == (args,)
    assert call_kwargs.get("strategy") == strat.name
    assert call_kwargs.get("decision_id") == "dec-paper-3"
    assert call_kwargs.get("order_id", "").startswith("paper-")

    # (b) Returned Order is paper=True, carries the right identity
    #     fields, and is attributed to this strategy + decision_id.
    assert result is not None
    assert result.paper is True
    assert result.token_id == args.token_id
    assert result.side == args.side
    assert result.price == args.price
    assert result.size == args.size
    assert result.strategy == strat.name
    assert result.decision_id == "dec-paper-3"


# ── (4) cancel_order removes from open_orders ────────────────────────────────
async def test_cancel_order_removes_from_open_orders():
    """``cancel_order`` in paper mode must remove the target order from
    ``store.open_orders``.

    The X11 task says "mock risk_manager and paper_sim" as a general
    isolation strategy; we honour that for tests 1–3 (where we're
    testing ``submit_order``'s branching logic in isolation) but keep
    the REAL ``paper_sim`` here so the "removes from open_orders"
    contract is exercised end-to-end rather than merely asserted by a
    mock's call list. ``cancel_order`` is a pure delegation in paper
    mode (``paper_sim.cancel_order(order_id)``) and ``risk_manager`` is
    not consulted at all, so no mock is required for this test.

    The real ``paper_sim.cancel_order`` calls
    ``store.update_order(order_id, status=OrderStatus.CANCELLED)``,
    which (per ``DataStore.update_order``) finds the order in
    ``open_orders``, sets its status to ``CANCELLED``, evicts it from
    ``open_orders``, and appends it to ``order_history``. This test
    stages an order directly into ``open_orders`` and verifies all
    three side-effects fire.
    """
    strat = _StubStrategy()
    assert strat._paper is True  # paper-mode branch → paper_sim.cancel_order

    # Stage an open paper order directly in the store.
    order_id = "paper-cancel-target"
    staged = Order(
        order_id=order_id,
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
    )
    await store.add_order(staged)

    # Sanity: the order is in open_orders before the cancel.
    assert order_id in store.open_orders
    assert store.open_orders[order_id].status == OrderStatus.OPEN

    # Cancel via the strategy layer.
    ok = await strat.cancel_order(order_id)

    # (a) The strategy reports a successful cancel.
    assert ok is True
    # (b) The order has been evicted from store.open_orders — the
    #     "removes from open_orders" contract under test.
    assert order_id not in store.open_orders
    # (c) Belt-and-braces: the order was moved to order_history with
    #     the CANCELLED status (the canonical post-cancel state for
    #     ``DataStore.update_order`` on a CANCELLED transition).
    history_ids = {o.order_id for o in store.order_history}
    assert order_id in history_ids
    cancelled = next(o for o in store.order_history if o.order_id == order_id)
    assert cancelled.status == OrderStatus.CANCELLED
    assert cancelled.order_id == order_id


# ── (5) start/stop lifecycle works ───────────────────────────────────────────
async def test_start_stop_lifecycle_works():
    """``start()`` flips ``_running`` to True and schedules the ``_run``
    coroutine as a named ``asyncio.Task``; ``stop()`` cancels the task
    and flips ``_running`` back to False (and is idempotent on
    re-call — a second ``stop()`` must not raise even though ``_task``
    is already cancelled).

    The ``_run`` body blocks on an ``asyncio.Event`` so the task stays
    alive until ``stop()`` cancels it — exercising the real
    ``task.cancel()`` + ``await task`` + ``CancelledError`` swallow path
    that ``BaseStrategy.stop`` uses in production.
    """
    strat = _StubStrategy()

    # Baseline sanity.
    assert strat._running is False
    assert strat._task is None

    await strat.start()

    # (a) start() flipped _running.
    assert strat._running is True
    # (b) start() scheduled the _run task.
    assert strat._task is not None
    assert isinstance(strat._task, asyncio.Task)
    # (c) The task was given the canonical "strategy-<name>" name (so
    #     ``asyncio.all_tasks()`` introspection + watchdog diagnostics
    #     can identify it in production).
    assert strat._task.get_name() == f"strategy-{strat.name}"
    # (d) The task is alive (not done) — i.e. _run is genuinely blocked
    #     on the gate, not racing to completion before stop() can
    #     exercise the cancel path.
    assert not strat._task.done()
    assert not strat._task.cancelled()

    # Yield control so the task actually starts executing _run (the
    # ``asyncio.create_task`` call in start() only schedules it).
    await asyncio.sleep(0)
    assert not strat._task.done()

    await strat.stop()

    # (e) stop() flipped _running back to False.
    assert strat._running is False
    # (f) stop() cancelled the task and awaited it to completion (no
    #     pending cancel left). After ``await task`` swallows the
    #     CancelledError, the task is either ``cancelled()`` or
    #     ``done()`` depending on the precise timing of when the
    #     cancellation propagated; both are acceptable terminal states.
    assert strat._task.cancelled() or strat._task.done()

    # (g) Belt-and-braces: stop() is idempotent — calling it again with
    #     _task already cancelled must not raise (the try/except path
    #     in stop() swallows CancelledError; the ``if self._task`` guard
    #     re-enters the cancel branch but ``task.cancel()`` on an
    #     already-cancelled task is a no-op).
    await strat.stop()  # must not raise
    assert strat._running is False
    assert strat._task.cancelled() or strat._task.done()
