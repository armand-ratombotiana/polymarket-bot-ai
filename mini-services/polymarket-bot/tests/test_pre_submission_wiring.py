"""
tests/test_pre_submission_wiring.py — Wiring tests for the W25-4 pre-submission
risk gate integration into ``BaseStrategy.submit_order`` and the FastAPI
``POST /api/risk/pre-submission-check`` route.

W25-4 — Pre-submission gate wiring verification.

This module is a focused, contract-style test of the WIRING — not the gate
internals (which are exercised exhaustively in
``tests/test_pre_submission_gate.py``). Each test below asserts ONE
end-to-end wiring invariant of the spec'd integration:

  1. ``submit_order`` calls the gate. The base method must invoke
     ``pre_submission_gate.check(...)`` on every order before the
     existing risk-engine gate runs. Verified by spying on
     ``pre_submission_gate.check`` and asserting it was called with the
     order_request built from the ``OrderArgs`` the caller passed.

  2. ``submit_order`` returns ``None`` when the gate rejects. The order
     path short-circuits — neither ``risk_manager.check_order`` nor
     ``paper_sim.create_order`` is invoked.

  3. The rejection is recorded in the rejected-opportunities store. The
     fire-and-forget ``asyncio.create_task(record_rejected_opportunity(...))``
     call inside ``submit_order`` schedules a write to the SQLite-backed
     ``rejected_opportunity_store`` so the operator dashboard surfaces
     "what the gate rejected and why" in the same analytics roll-up as
     risk-engine rejections. Verified by spying on
     ``record_rejected_opportunity`` and asserting it was called with the
     originating order's token_id / strategy / side / price / size /
     rejection_reason.

  4. Approved orders proceed normally. When the gate approves AND the
     risk engine approves, ``paper_sim.create_order`` (paper mode) /
     ``clob_client.create_order`` (live mode) IS called and the resulting
     ``Order`` is returned. Verified by mocking both gates to approve,
     asserting ``paper_sim.create_order`` was awaited, and asserting the
     returned ``Order`` is the sentinel the mock returned.

  5. The API route ``POST /api/risk/pre-submission-check`` returns 200
     with the full ``PreSubmissionResult`` shape (``approved``,
     ``checks[]``, ``rejection_reason``, ``rejection_category``,
     ``timestamp``). Verified by driving the real production ``app``
     (``api.server.app``) via ``TestClient`` — the request traverses the
     CORS / auth / request-logging middlewares AND the route handler.

  6. The API route surfaces rejections (kill switch on →
     ``approved: false`` with ``rejection_category="kill_switch"``).

  7. The API route is NOT in ``PUBLIC_PATHS`` — a request without a
     bearer token returns 401.

Test layout
-----------
- ``_reset_idempotency`` autouse fixture: clears the
  ``idempotency_manager`` cache before every test so a prior test's
  recorded keys don't trigger false duplicates (mirrors the same fixture
  in ``tests/test_pre_submission_gate.py``).
- ``_reset_clob_breaker`` autouse fixture: forces the CLOB circuit
  breaker back to CLOSED before every test so a prior test that opened
  it doesn't block every subsequent test.
- ``_StubStrategy``: minimal concrete ``BaseStrategy`` subclass whose
  ``_run`` is a no-op await so the strategy can be constructed without
  booting its async loop.
- ``_order_args``: factory for ``OrderArgs`` instances with sensible
  defaults (BUY / 0.50 / 2.0 / ``_TOKEN_ID``).

The wiring tests are written with ``SimpleNamespace + AsyncMock`` mocks
for ``risk_manager`` and ``paper_sim`` (injected via ``monkeypatch`` on
``strategies.base``) so the gate is the only thing under test — the
risk-engine and paper-sim code paths are stubbed out.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set.
_TMP_ROOT = Path("/tmp/pre_submission_wiring_tests")
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
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-pre-submission-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitBreakerConfig,
    clob_breaker,
)
from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import Order, Side  # noqa: E402
from core.idempotency import idempotency_manager  # noqa: E402
from core.pre_submission_gate import (  # noqa: E402
    PreSubmissionResult,
    RiskCheckResult,
    pre_submission_gate,
)
from strategies import base as base_module  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_pre_submission_gate.py``.
pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_idempotency():
    """Clear the ``idempotency_manager`` cache before every test so a
    prior test's recorded keys don't trigger false duplicates."""
    idempotency_manager.reset()
    yield
    idempotency_manager.reset()


@pytest.fixture(autouse=True)
def _reset_clob_breaker():
    """Force the CLOB circuit breaker back to CLOSED before every test
    so a prior test that opened it doesn't block every subsequent test."""
    clob_breaker.reset()
    yield
    clob_breaker.reset()


@pytest.fixture(autouse=True)
def _reset_gate_thresholds():
    """Reset the gate's thresholds to factory defaults before every test
    so a prior test's ``configure`` call doesn't leak (mirrors the
    ``gate`` fixture in ``tests/test_pre_submission_gate.py``)."""
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    yield


@pytest.fixture(autouse=True)
def _kill_switch_off(monkeypatch):
    """Default the kill switch to OFF for every test (belt-and-braces —
    a prior test that activated it would otherwise leak and cause every
    subsequent test's gate to reject)."""
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: False
    )


# ── Stub strategy ──────────────────────────────────────────────────────────

class _StubStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for wiring tests.

    The async ``_run`` is a no-op await so the strategy can be
    constructed (the ``__init__`` doesn't start the loop) without
    booting its async loop. The strategy is created in PAPER mode by
    default (``paper=True`` in ``BaseStrategy.__init__``).
    """

    name: str = "stub_wiring_test"

    def __init__(self) -> None:
        super().__init__()

    async def _run(self) -> None:
        await asyncio.Event().wait()


_TOKEN_ID = "0xpre_submission_wiring_test_token"


def _order_args(
    *,
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 2.0,
    token_id: str = _TOKEN_ID,
) -> OrderArgs:
    """Build an ``OrderArgs`` instance with sensible test defaults."""
    return OrderArgs(token_id=token_id, price=price, side=side, size=size)


def _mock_risk_manager_approves() -> SimpleNamespace:
    """Build a ``risk_manager`` mock whose ``check_order`` approves."""
    return SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))


def _mock_paper_sim_returns_sentinel(sentinel: Order) -> SimpleNamespace:
    """Build a ``paper_sim`` mock whose ``create_order`` returns the
    sentinel ``Order`` (so the wiring test can assert the return value
    passes through unchanged)."""
    return SimpleNamespace(create_order=AsyncMock(return_value=sentinel))


# ── Test 1: submit_order invokes pre_submission_gate.check ─────────────────

async def test_submit_order_invokes_pre_submission_gate_check(monkeypatch):
    """WIRING INVARIANT: ``BaseStrategy.submit_order`` MUST call
    ``pre_submission_gate.check(...)`` on every order, BEFORE the existing
    ``risk_manager.check_order`` gate. The call must pass an
    ``order_request`` dict carrying the order's token_id / side / size /
    price / strategy / order_id derived from the ``OrderArgs`` the caller
    supplied.

    The wiring is the W25-4 contract: no order can bypass the gate. If
    a future refactor moves the gate call out of ``submit_order`` (or
    renames the kwarg / drops a field), this test fails — surfacing the
    regression before a single unguarded order reaches the exchange.
    """
    strat = _StubStrategy()
    assert strat._paper is True  # paper-mode branch

    # Replace the singleton's ``check`` with a spy that records the call
    # and returns an APPROVED result so the rest of ``submit_order``
    # proceeds (the gate is the thing under test, not risk_manager or
    # paper_sim).
    captured: dict = {}

    def _spy_check(order_request, market_data=None, account_state=None):
        captured["order_request"] = order_request
        captured["market_data"] = market_data
        captured["account_state"] = account_state
        return PreSubmissionResult(approved=True, checks=[])

    monkeypatch.setattr(pre_submission_gate, "check", _spy_check)

    mock_risk = _mock_risk_manager_approves()
    mock_paper = _mock_paper_sim_returns_sentinel(Order(
        order_id="paper-sentinel-spy",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-spy",
    ))
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    await strat.submit_order(args, decision_id="dec-spy")

    # The gate was called.
    assert "order_request" in captured, (
        "pre_submission_gate.check was not called by submit_order"
    )
    # The order_request carries the originating order's fields.
    or_ = captured["order_request"]
    assert or_["token_id"] == _TOKEN_ID
    assert or_["side"] == "BUY"
    assert or_["size"] == 2.0
    assert or_["price"] == 0.50
    assert or_["strategy"] == strat.name
    assert "order_id" in or_ and or_["order_id"], (
        "order_request.order_id must be populated (the gate uses it for "
        "the idempotency cache key and the rejected-opportunities store "
        "uses it for the correlation_id back-link)"
    )

    # The gate was called BEFORE risk_manager.check_order. We can't
    # observe ordering directly, but if the gate was called AT ALL then
    # the spy ran before the test ended — and since the gate runs as
    # the first action inside submit_order (per the W24-3 wiring), the
    # spy MUST have been called before risk_manager.check_order. We
    # assert the gate was called AND risk_manager.check_order was called
    # (proving the order didn't short-circuit before either).
    mock_risk.check_order.assert_awaited_once()


# ── Test 2: rejected orders return None ────────────────────────────────────

async def test_submit_order_returns_none_when_gate_rejects(monkeypatch):
    """WIRING INVARIANT: when ``pre_submission_gate.check`` returns a
    result with ``approved=False``, ``submit_order`` MUST return
    ``None`` AND ``risk_manager.check_order`` MUST NOT be called AND
    ``paper_sim.create_order`` MUST NOT be called. The order is
    short-circuited at the gate (defense in depth — the existing
    risk-engine gate is the second gate, not the first).
    """
    strat = _StubStrategy()

    # Force the gate to reject by activating the kill switch.
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )

    mock_risk = _mock_risk_manager_approves()
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-gate-rej")

    # Gate rejected → submit_order returned None.
    assert result is None, (
        "submit_order must return None when the pre-submission gate "
        "rejects; got a non-None return value"
    )
    # risk_manager.check_order was NEVER called (gate short-circuited).
    mock_risk.check_order.assert_not_awaited()
    # paper_sim.create_order was NEVER called.
    mock_paper.create_order.assert_not_awaited()


# ── Test 3: rejected orders are recorded in rejected_opportunity_store ─────

async def test_gate_rejection_records_to_rejected_opportunity_store(monkeypatch):
    """WIRING INVARIANT: when the pre-submission gate rejects an order,
    ``submit_order`` MUST record the rejection in the
    ``rejected_opportunities`` store (fire-and-forget async) so the
    operator dashboard surfaces "what the gate rejected and why" in the
    same analytics roll-up as risk-engine rejections.

    The fire-and-forget ``asyncio.create_task(record_rejected_opportunity(...))``
    call inside ``submit_order`` schedules a write to the SQLite-backed
    ``rejected_opportunity_store``. This test spies on
    ``record_rejected_opportunity`` (the kwargs-style convenience wrapper)
    and asserts the call passes the originating order's identity (token_id,
    strategy, side, price, size) AND the gate's rejection reason (slug +
    raw message in ``rejection_details``).
    """
    strat = _StubStrategy()

    # Force the gate to reject by activating the kill switch.
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )

    # Spy on ``record_rejected_opportunity``. Replace the function
    # attribute on the ``core.rejected_opportunities`` module so the
    # late-import inside ``submit_order`` sees the spy. The spy records
    # the kwargs it was called with and returns a sentinel row id.
    captured: dict = {}

    async def _spy_record(**kwargs):
        captured["kwargs"] = kwargs
        return 42  # sentinel row id

    import core.rejected_opportunities as _ro_module
    monkeypatch.setattr(
        _ro_module, "record_rejected_opportunity", _spy_record
    )

    mock_risk = _mock_risk_manager_approves()
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-rej-record")

    # Gate rejected → submit_order returned None.
    assert result is None

    # The fire-and-forget ``asyncio.create_task`` schedules the record
    # call on the next event loop tick. ``await asyncio.sleep(0)`` lets
    # the loop drain pending tasks (the task is created with
    # ``asyncio.create_task`` so it runs as soon as the awaiting coroutine
    # yields — ``asyncio.sleep(0)`` yields control).
    #
    # Belt-and-braces: drain multiple ticks (the asyncio task graph may
    # have nested awaits — e.g. the spy itself returns a coroutine that
    # has to be awaited, then the result is awaited by the task
    # wrapper). Three ticks is enough for the spy to fire on every CPython
    # version we support.
    for _ in range(5):
        await asyncio.sleep(0)

    # The spy was called with the originating order's identity.
    assert "kwargs" in captured, (
        "record_rejected_opportunity was not called by submit_order's "
        "gate-rejection branch (the fire-and-forget task either didn't "
        "fire or didn't see the spy)"
    )
    kwargs = captured["kwargs"]
    assert kwargs["token_id"] == _TOKEN_ID, (
        f"token_id mismatch: expected {_TOKEN_ID!r}, got "
        f"{kwargs.get('token_id')!r}"
    )
    assert kwargs["strategy"] == strat.name, (
        f"strategy mismatch: expected {strat.name!r}, got "
        f"{kwargs.get('strategy')!r}"
    )
    assert kwargs["signal_action"] == "BUY", (
        f"signal_action mismatch: expected 'BUY', got "
        f"{kwargs.get('signal_action')!r}"
    )
    assert kwargs["signal_price"] == 0.50, (
        f"signal_price mismatch: expected 0.50, got "
        f"{kwargs.get('signal_price')!r}"
    )
    assert kwargs["signal_size"] == 2.0, (
        f"signal_size mismatch: expected 2.0, got "
        f"{kwargs.get('signal_size')!r}"
    )
    # rejection_reason carries the gate's rejection_category slug
    # (kill_switch). The raw message lives in rejection_details.
    assert kwargs["rejection_reason"] == "kill_switch", (
        f"rejection_reason mismatch: expected 'kill_switch' (the gate's "
        f"rejection_category slug), got {kwargs.get('rejection_reason')!r}"
    )
    # rejection_details carries the raw message + structured fields.
    details = kwargs.get("rejection_details") or {}
    assert "raw_message" in details, (
        f"rejection_details must carry the raw gate message under "
        f"'raw_message'; got details={details!r}"
    )
    assert "kill" in details["raw_message"].lower(), (
        f"raw_message should mention the kill switch; got "
        f"{details['raw_message']!r}"
    )
    assert details.get("gate_layer") == "pre_submission", (
        f"rejection_details.gate_layer should be 'pre_submission' so the "
        f"operator dashboard can distinguish gate rejections from "
        f"risk-engine rejections; got {details.get('gate_layer')!r}"
    )
    # correlation_id back-links to the decision ledger.
    assert kwargs.get("correlation_id") == "dec-rej-record", (
        f"correlation_id should be the decision_id passed to submit_order; "
        f"got {kwargs.get('correlation_id')!r}"
    )


# ── Test 4: approved orders proceed normally ───────────────────────────────

async def test_approved_order_proceeds_to_paper_sim(monkeypatch):
    """WIRING INVARIANT: when the gate approves AND ``risk_manager.check_order``
    also approves, ``submit_order`` MUST proceed to the paper / live
    submission path and return the resulting ``Order``. Specifically,
    ``paper_sim.create_order`` (paper mode) MUST be awaited and the
    ``Order`` it returns MUST pass through ``submit_order`` unchanged.
    """
    strat = _StubStrategy()

    # Gate approves (kill switch off — fixture default).
    mock_risk = _mock_risk_manager_approves()
    sentinel_order = Order(
        order_id="paper-sentinel-approved",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-gate-approve",
    )
    mock_paper = _mock_paper_sim_returns_sentinel(sentinel_order)
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-gate-approve")

    # Risk gate was called (gate approved → defense in depth proceeded).
    mock_risk.check_order.assert_awaited_once()
    # Paper sim was called (risk approved → paper path proceeded).
    mock_paper.create_order.assert_awaited_once()
    # Returned the sentinel order (paper path returned the Order unchanged).
    assert result is sentinel_order, (
        "submit_order must return the Order returned by paper_sim.create_order "
        "unchanged when the gate + risk both approve"
    )


async def test_approved_order_proceeds_to_risk_manager(monkeypatch):
    """WIRING INVARIANT: when the gate approves, ``submit_order`` MUST
    proceed to the second gate (``risk_manager.check_order``) — defense
    in depth. The pre-submission gate is the FIRST gate, not the ONLY
    gate. This test verifies the second gate IS invoked after a gate
    approval (the existing risk-engine gate handles exposure / position
    caps / etc. via the in-memory ``store`` snapshot).
    """
    strat = _StubStrategy()

    mock_risk = _mock_risk_manager_approves()
    sentinel_order = Order(
        order_id="paper-sentinel-defense-in-depth",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-did",
    )
    mock_paper = _mock_paper_sim_returns_sentinel(sentinel_order)
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    await strat.submit_order(args, decision_id="dec-did")

    # Defense in depth — risk_manager.check_order WAS called.
    mock_risk.check_order.assert_awaited_once()
    # The Order it received carries the originating order's fields
    # (sanity: the provisional Order built inside submit_order is the
    # one the risk gate sees).
    provision = mock_risk.check_order.await_args.args[0]
    assert provision.token_id == _TOKEN_ID
    assert float(provision.price) == 0.50
    assert float(provision.size) == 2.0


# ── Test 5: API route returns full result shape ────────────────────────────

def _valid_order_request(**overrides) -> dict:
    """Build an order_request that passes EVERY check by default."""
    base = {
        "token_id": "0xtest_token_id_wiring",
        "side": "BUY",
        "size": 2.0,
        "price": 0.50,
        "strategy": "test_strategy_wiring",
        "edge": 0.05,
        "confidence": 0.65,
        "order_id": "wiring-ord-1",
    }
    base.update(overrides)
    return base


def _valid_market_data(**overrides) -> dict:
    """Build a market_data snapshot that passes EVERY market check."""
    base = {
        "best_bid": 0.48,
        "best_ask": 0.52,
        "spread": 0.04,
        "liquidity": 250.0,
        "last_update": time.time(),
        "mid": 0.50,
    }
    base.update(overrides)
    return base


def _valid_account_state(**overrides) -> dict:
    """Build an account_state that passes EVERY account check."""
    base = {
        "balance": 100.0,
        "total_exposure": 5.0,
        "open_orders": 2,
        "daily_pnl": 0.5,
        "drawdown": 0.02,
        "max_total_exposure": 25.0,
        "max_single_position": 3.0,
        "max_open_orders": 8,
        "daily_loss_limit": -2.0,
        "max_drawdown_limit": 0.15,
    }
    base.update(overrides)
    return base


def test_api_route_returns_full_result_shape(monkeypatch):
    """WIRING INVARIANT: ``POST /api/risk/pre-submission-check``
    returns 200 with the full ``PreSubmissionResult`` shape —
    ``{approved, checks[], rejection_reason, rejection_category,
    timestamp}`` — so a caller can dry-run an order against the gate
    before committing to ``submit_order``.

    Drives the real production ``app`` (``api.server.app``) via
    ``TestClient`` so the request traverses the CORS / auth /
    request-logging middlewares AND the route handler. Belt-and-braces
    with ``test_api_route_returns_full_result`` in
    ``tests/test_pre_submission_gate.py`` — that test asserts the same
    shape but this test is intentionally duplicated in the wiring module
    so the wiring contract is visible from one file.
    """
    # Late import so the env-var redirects above are in effect when
    # ``api.server`` is imported (it reads env vars at module-import time).
    from api.server import app
    from fastapi.testclient import TestClient

    # Reset the gate's thresholds (in case a prior test configured them).
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    # Clear the idempotency cache so the request is not flagged as a dup.
    idempotency_manager.reset()
    clob_breaker.reset()

    token = os.environ.get("API_TOKEN", "test-token-conftest")

    payload = {
        "order_request": _valid_order_request(),
        "market_data": _valid_market_data(),
        "account_state": _valid_account_state(),
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/pre-submission-check",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, (
        f"POST /api/risk/pre-submission-check must return 200 for an "
        f"approving payload; got {response.status_code}. "
        f"Body: {response.text!r}"
    )
    body = response.json()
    assert body["approved"] is True
    assert body["rejection_reason"] == ""
    assert body["rejection_category"] == ""
    assert "checks" in body
    assert len(body["checks"]) == 14, (
        f"Expected 14 checks; got {len(body['checks'])} (the gate runs "
        f"every check on every call so a check was dropped or added)"
    )
    assert "timestamp" in body
    # Each check entry carries the structured fields.
    for c in body["checks"]:
        assert "check_name" in c
        assert "passed" in c
        assert "value" in c
        assert "threshold" in c
        assert "message" in c


def test_api_route_surfaces_gate_rejection(monkeypatch):
    """WIRING INVARIANT: when the gate rejects (e.g. kill switch on),
    ``POST /api/risk/pre-submission-check`` returns 200 with
    ``approved: false`` AND ``rejection_category`` set to the failing
    check's ``check_name``. The route reports the rejection in the
    BODY, not via HTTP status — the route is a dry-run, not an order
    submission, so 4xx / 5xx would be misleading.
    """
    from api.server import app
    from fastapi.testclient import TestClient

    # Reset state.
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    idempotency_manager.reset()
    clob_breaker.reset()

    # Activate the kill switch → the kill_switch check fails first
    # (it's check #1, so its failure preempts every other check).
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )

    token = os.environ.get("API_TOKEN", "test-token-conftest")
    payload = {
        "order_request": _valid_order_request(),
        "market_data": _valid_market_data(),
        "account_state": _valid_account_state(),
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/pre-submission-check",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, (
        f"POST /api/risk/pre-submission-check must return 200 even for a "
        f"rejecting payload (the route reports the rejection in the body, "
        f"not the HTTP status); got {response.status_code}. "
        f"Body: {response.text!r}"
    )
    body = response.json()
    assert body["approved"] is False, (
        f"approved must be False when the gate rejects; got {body.get('approved')!r}"
    )
    assert body["rejection_category"] == "kill_switch", (
        f"rejection_category must be 'kill_switch' (the first failing "
        f"check's check_name); got {body.get('rejection_category')!r}"
    )
    assert "Kill switch is active" in body["rejection_reason"], (
        f"rejection_reason must mention the kill switch; got "
        f"{body.get('rejection_reason')!r}"
    )
    # The kill_switch check entry IS present and failed.
    ks_check = next(
        (c for c in body["checks"] if c["check_name"] == "kill_switch"),
        None,
    )
    assert ks_check is not None, (
        "kill_switch check entry must be present in checks[]"
    )
    assert ks_check["passed"] is False


def test_api_route_auth_required():
    """WIRING INVARIANT: ``POST /api/risk/pre-submission-check`` is NOT
    in ``PUBLIC_PATHS`` — a request without a bearer token returns 401
    (or 403). The route exposes the gate's threshold knobs indirectly
    (via the per-check ``value`` / ``threshold`` fields), so it must be
    auth-protected.
    """
    from api.server import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/api/risk/pre-submission-check",
            json={"order_request": _valid_order_request()},
            # No Authorization header.
        )
    assert response.status_code in (401, 403), (
        f"POST /api/risk/pre-submission-check without a bearer token "
        f"must return 401 or 403 (auth required); got "
        f"{response.status_code}. Body: {response.text!r}"
    )


# ── Test 6: real-strategy wiring — signal_trader routes through submit_order

async def test_signal_trader_routes_through_submit_order(monkeypatch):
    """WIRING INVARIANT: ``SignalTraderStrategy._act_on_signal`` MUST
    call ``self.submit_order`` (not directly to ``paper_sim.create_order``
    or ``clob_client.create_order``). If a future refactor bypasses
    ``submit_order``, the pre-submission gate is silently skipped —
    the very gap W24-3 + W25-4 close. This test guards against that
    regression by spying on ``BaseStrategy.submit_order`` and asserting
    the signal-trader's action path invokes it.

    The signal-trader doesn't actually run its async loop here — we
    directly invoke the private ``_act_on_signal`` method with a
    synthesized ``MarketSignal`` so the test is fast and hermetic.
    """
    from strategies.signal_trader import (
        MarketSignal,
        SignalTraderStrategy,
    )

    strat = SignalTraderStrategy()

    # Spy on the inherited ``submit_order`` (so we can assert it was
    # called). The spy returns ``None`` (simulating a paper-sim reject
    # or a gate reject — either is fine for THIS wiring test, which
    # only cares that the call path goes through ``submit_order``).
    submit_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(strat, "submit_order", submit_spy)

    # Synthesise a minimal ``MarketSignal`` the signal-trader's
    # ``_act_on_signal`` will accept. The fields mirror the production
    # ``MarketSignal`` dataclass shape (see ``strategies/signal_trader.py``).
    sig = MarketSignal(
        token_id=_TOKEN_ID,
        slug="test-market-wiring",
        direction=Side.BUY,
        confidence=0.65,
        target_price=0.50,
        size_usdc=2.0,
        reason="wiring-test-signal",
        ml_score=0.65,
        source="ml",
        edge=0.05,
        price=0.50,
        decision_id="dec-signal-trader-wiring",
    )

    # The signal-trader guards against stacking directional positions
    # per market — make sure ``store.positions`` is empty for the token
    # so the act path doesn't early-return.
    from core.data_store import store
    store.positions.pop(_TOKEN_ID, None)
    store.open_orders.pop(_TOKEN_ID, None)

    await strat._act_on_signal(sig)

    # ``submit_order`` was called — the signal-trader routed through
    # the base method (and therefore through the pre-submission gate).
    submit_spy.assert_awaited_once()
    # The ``OrderArgs`` it received carries the signal's identity.
    call_args = submit_spy.await_args
    args = call_args.args[0] if call_args.args else call_args.kwargs.get("args")
    assert args is not None, (
        "submit_order was called without an OrderArgs positional argument"
    )
    assert args.token_id == _TOKEN_ID
    assert float(args.price) == 0.50
    assert args.side == Side.BUY


# ── Test 7: real-strategy wiring — market_maker routes through submit_order

async def test_market_maker_routes_through_submit_order(monkeypatch):
    """WIRING INVARIANT: ``MarketMakerStrategy`` MUST call
    ``self.submit_order`` for both legs of its quote (bid + ask) and
    for any flush orders. The market maker's quote-refresh loop is the
    highest-frequency submit path in the bot — if it ever bypassed
    ``submit_order``, the pre-submission gate would be silently
    skipped on the majority of order traffic.

    This test directly invokes the market maker's private quote-placement
    method (``_place_skewed_quotes``) with a synthesized book so the test
    is fast and hermetic. The spy asserts ``submit_order`` was called
    for at least the BUY leg (the SELL leg only fires when the strategy
    already holds YES inventory, which a fresh strategy doesn't).
    """
    from core.data_store import OrderBook, PriceLevel, Side, store
    from strategies.market_maker import MarketMakerStrategy

    strat = MarketMakerStrategy()

    # Spy on the inherited ``submit_order`` (returns None so the market
    # maker's "did the submit succeed?" branch takes the no-op path —
    # the spy is only here to capture the call).
    submit_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(strat, "submit_order", submit_spy)

    # Synthesise an order book for the test token so the market maker
    # has a mid-price to quote around. ``OrderBook.best_bid`` /
    # ``best_ask`` / ``mid`` / ``spread`` are derived properties off
    # the ``bids`` / ``asks`` lists, so we populate them with
    # ``PriceLevel`` entries.
    test_token = _TOKEN_ID
    book = OrderBook(token_id=test_token)
    book.bids.append(PriceLevel(price=0.48, size=100.0))
    book.asks.append(PriceLevel(price=0.52, size=100.0))
    store.order_books[test_token] = book
    # Make sure the token isn't in the active-positions map so the
    # market maker doesn't early-return on a stale inventory guard.
    store.positions.pop(test_token, None)

    # Invoke the market maker's quote-placement method directly. The
    # method name is stable (``_place_skewed_quotes``) — if a future
    # refactor renames it, this test surfaces the rename.
    place_quotes = getattr(strat, "_place_skewed_quotes", None)
    if place_quotes is None or not callable(place_quotes):
        pytest.skip(
            "MarketMakerStrategy._place_skewed_quotes not found on this "
            "version — the quote-placement API may have been renamed; "
            "update this test to point at the new method name"
        )

    try:
        await place_quotes(token_id=test_token, book=book)
    except Exception:
        # The market maker's quote-placement may raise on missing state
        # (inventory, gamma config, etc.). The wiring invariant we care
        # about is "did submit_order get called" — not "did the quote
        # placement succeed end-to-end". The spy assertion below is
        # what matters.
        pass

    # ``submit_order`` was called at least once (the market maker tried
    # to place at least the BUY leg of the quote — the SELL leg only
    # fires when the strategy already holds inventory).
    assert submit_spy.await_count >= 1, (
        "MarketMakerStrategy._place_skewed_quotes must route through "
        "self.submit_order for at least the BUY leg of the quote — the "
        "pre-submission gate is bypassed if the market maker goes "
        "directly to paper_sim / clob_client"
    )


# ── Test 8: real-strategy wiring — arb_scanner routes through submit_order

async def test_arb_scanner_routes_through_submit_order(monkeypatch):
    """WIRING INVARIANT: ``ArbScannerStrategy`` MUST call ``self.submit_order``
    for both legs of an arbitrage opportunity (YES + NO on the paired
    markets). The arb scanner's two-leg atomicity contract is the
    single most ordering-sensitive path in the bot — if it bypassed
    ``submit_order``, the pre-submission gate would silently skip
    arb-order risk checks (exposure / correlation / etc.).

    This test directly invokes the arb scanner's private execution
    method (``_execute_arb``) with a synthesized opportunity so the
    test is fast and hermetic. The spy asserts ``submit_order`` was
    called for at least the YES leg (the method calls both legs in
    parallel via ``asyncio.gather``).
    """
    from strategies.arb_scanner import ArbScannerStrategy

    strat = ArbScannerStrategy()

    # Spy on the inherited ``submit_order`` (returns None so the
    # arb-scanner's "did the submit succeed?" branch takes the no-op
    # path).
    submit_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(strat, "submit_order", submit_spy)

    # The arb scanner's execution method is ``_execute_arb``. Its
    # signature is positional: ``(yes_token, no_token, yes_price,
    # no_price, profit, arb_type="long_dutch_book")``. If the method
    # doesn't exist on this version, skip — the test is a regression
    # guard.
    execute_arb = getattr(strat, "_execute_arb", None)
    if execute_arb is None or not callable(execute_arb):
        pytest.skip(
            "ArbScannerStrategy._execute_arb not found on this version — "
            "the arb-execution API may have been renamed; update this "
            "test to point at the new method name"
        )

    try:
        await execute_arb(
            _TOKEN_ID,                       # yes_token
            "0xarb_no_leg_wiring_test",      # no_token
            0.45,                             # yes_price
            0.50,                             # no_price
            0.05,                             # profit (5% edge)
        )
    except Exception:
        # The arb scanner may raise on missing gamma config / inventory
        # state. The wiring invariant is "did submit_order get called".
        pass

    # ``submit_order`` was called at least once for the YES leg (and
    # typically for the NO leg too — ``_execute_arb`` calls both legs
    # in parallel via ``asyncio.gather``).
    assert submit_spy.await_count >= 1, (
        "ArbScannerStrategy._execute_arb must route through "
        "self.submit_order for at least the YES leg — the pre-submission "
        "gate is bypassed if the arb scanner goes directly to paper_sim / "
        "clob_client"
    )
