"""
tests/test_pre_submission_gate.py — Unit + integration tests for the
W24-3 pre-submission risk gate.

W24-3 — God Mode §pre-submission-gate. Every order MUST pass through
ALL 14 risk checks before submission. This test module exercises each
check independently (so a regression on one check fails one test, not
all of them) AND the wiring into ``BaseStrategy.submit_order`` (so a
gate rejection actually short-circuits the order path) AND the API
route (so ``POST /api/risk/pre-submission-check`` returns the full
result shape).

Test layout
-----------
- ``_gate`` fixture: fresh ``PreSubmissionGate`` singleton (the module
  global is reset before every test so a prior test's threshold tweak
  doesn't leak).
- ``_reset_idempotency`` autouse fixture: clears the
  ``idempotency_manager`` cache before every test so a prior test's
  recorded keys don't trigger false duplicates.
- Per-check tests (one per check #1..#14): each test constructs an
  order_request / market_data / account_state that passes every OTHER
  check but fails the one under test, then asserts the gate rejects
  with the right ``rejection_category``.
- ``test_all_checks_pass_with_valid_order``: the happy path — every
  check passes → ``approved=True``.
- ``test_permissive_when_context_absent``: when ``account_state`` /
  ``market_data`` are ``None``, the account-state and market-data
  checks are recorded as PASSED with the explicit "skipped" message
  (so backward-compatible callers that don't pass context still flow
  through).
- ``test_submit_order_short_circuits_on_gate_rejection``: end-to-end
  wiring — when the gate rejects, ``submit_order`` returns ``None``
  and ``paper_sim.create_order`` is NEVER called.
- ``test_submit_order_proceeds_on_gate_approval``: end-to-end wiring —
  when the gate approves, ``submit_order`` proceeds to
  ``risk_manager.check_order`` (the second gate, defense in depth).
- ``test_api_route_returns_full_result``: drives the real production
  ``app`` (``api.server.app``) via ``TestClient`` and asserts the
  ``POST /api/risk/pre-submission-check`` route returns 200 with the
  full ``PreSubmissionResult`` shape.
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
_TMP_ROOT = Path("/tmp/pre_submission_gate_tests")
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
    "API_TOKEN": "test-token-pre-submission-gate",
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
    CircuitState,
    clob_breaker,
)
from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import Order, Side, store  # noqa: E402
from core.idempotency import idempotency_manager  # noqa: E402
from core.pre_submission_gate import (  # noqa: E402
    PreSubmissionGate,
    pre_submission_gate,
)
from strategies import base as base_module  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_strategy_base.py``.
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


@pytest.fixture
def gate() -> PreSubmissionGate:
    """Return the module-global gate singleton (tests use the same
    instance ``submit_order`` uses so the wiring test is realistic)."""
    # Reset thresholds to defaults (a prior test may have called
    # ``configure``). Re-construct the singleton's thresholds by hand
    # so the singleton identity is preserved.
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    return pre_submission_gate


def _valid_order_request(**overrides) -> dict:
    """Build an order_request that passes EVERY check by default.

    Tests override the field they want to break (e.g.
    ``_valid_order_request(edge=0.01)`` to trip the min_edge check).
    """
    base = {
        "token_id": "0xtest_token_id",
        "side": "BUY",
        "size": 2.0,
        "price": 0.50,
        "strategy": "test_strategy",
        "edge": 0.05,
        "confidence": 0.65,
        "order_id": "test-ord-1",
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


# ── Happy path ──────────────────────────────────────────────────────────────

def test_all_checks_pass_with_valid_order(gate: PreSubmissionGate):
    """Every check passes when the order / market / account are all
    within their thresholds — ``approved=True``, no rejection reason."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert result.approved is True
    assert result.rejection_reason == ""
    assert result.rejection_category == ""
    # All 14 checks ran.
    assert len(result.checks) == 14
    # Every check passed.
    failed = [c for c in result.checks if not c.passed]
    assert failed == [], (
        f"Expected all 14 checks to pass; failures: "
        f"{[(c.check_name, c.message) for c in failed]}"
    )
    # Sanity: each check_name is one of the canonical 14.
    expected_names = {
        "kill_switch", "balance", "max_exposure", "max_single_position",
        "max_open_orders", "daily_loss_limit", "max_drawdown",
        "data_freshness", "max_spread", "min_liquidity",
        "min_edge", "min_confidence", "idempotency", "circuit_breaker",
    }
    actual_names = {c.check_name for c in result.checks}
    assert actual_names == expected_names, (
        f"Expected the 14 canonical check names; got {actual_names}"
    )


def test_permissive_when_context_absent(gate: PreSubmissionGate):
    """When ``account_state`` and ``market_data`` are ``None``, the
    account-state and market-data checks are recorded as PASSED with
    the explicit "skipped — no input data" message. The kill-switch /
    idempotency / circuit-breaker checks STILL run (they don't depend
    on caller-supplied context)."""
    result = gate.check(
        order_request={
            "token_id": "0xt",
            "side": "BUY",
            "size": 1.0,
            "price": 0.5,
            "strategy": "test",
            "order_id": "ord-1",
            # NOTE: no "edge" / "confidence" → those checks skipped too.
        },
        market_data=None,
        account_state=None,
    )
    assert result.approved is True, (
        f"Gate should approve when context is absent (permissive); "
        f"got rejection: {result.rejection_reason}"
    )
    # Kill switch, idempotency, circuit breaker all RAN (not skipped).
    by_name = {c.check_name: c for c in result.checks}
    assert by_name["kill_switch"].message == "OK"
    assert by_name["idempotency"].message == "OK"
    assert by_name["circuit_breaker"].message == "OK"
    # Account-state and market-data checks were SKIPPED.
    for name in (
        "balance", "max_exposure", "max_single_position",
        "max_open_orders", "daily_loss_limit", "max_drawdown",
        "data_freshness", "max_spread", "min_liquidity",
        "min_edge", "min_confidence",
    ):
        assert by_name[name].passed is True
        assert "skipped" in by_name[name].message.lower(), (
            f"Check {name!r} should be skipped (no input data); "
            f"got message={by_name[name].message!r}"
        )


# ── Per-check rejection tests ──────────────────────────────────────────────

def test_kill_switch_blocks(gate: PreSubmissionGate, monkeypatch):
    """Check #1 — kill switch active → rejected with category=kill_switch."""
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "kill_switch"
    assert "Kill switch is active" in result.rejection_reason


def test_insufficient_balance_blocks(gate: PreSubmissionGate):
    """Check #2 — balance < cost → rejected with category=balance."""
    result = gate.check(
        order_request=_valid_order_request(size=2.0, price=0.50),  # cost=1.0
        market_data=_valid_market_data(),
        account_state=_valid_account_state(balance=0.50),  # < 1.0 cost
    )
    assert result.approved is False
    assert result.rejection_category == "balance"
    assert "Balance" in result.rejection_reason


def test_max_exposure_blocks(gate: PreSubmissionGate):
    """Check #3 — total exposure (existing + new) > cap → rejected."""
    result = gate.check(
        order_request=_valid_order_request(size=2.0, price=0.50),  # cost=1.0
        market_data=_valid_market_data(),
        account_state=_valid_account_state(
            total_exposure=25.0,        # already at cap
            max_total_exposure=25.0,    # +1.0 cost → 26.0 > 25.0
        ),
    )
    assert result.approved is False
    assert result.rejection_category == "max_exposure"


def test_max_single_position_blocks(gate: PreSubmissionGate):
    """Check #4 — single-position cost > per-order cap → rejected."""
    result = gate.check(
        order_request=_valid_order_request(size=10.0, price=0.50),  # cost=5.0
        market_data=_valid_market_data(),
        account_state=_valid_account_state(
            max_single_position=3.0,   # 5.0 > 3.0
            balance=1000.0,             # don't trip balance check
            max_total_exposure=1000.0, # don't trip exposure check
        ),
    )
    assert result.approved is False
    assert result.rejection_category == "max_single_position"


def test_max_open_orders_blocks(gate: PreSubmissionGate):
    """Check #5 — open orders >= max → rejected."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(
            open_orders=8, max_open_orders=8,   # 8 >= 8 → fail
        ),
    )
    assert result.approved is False
    assert result.rejection_category == "max_open_orders"


def test_daily_loss_limit_blocks(gate: PreSubmissionGate):
    """Check #6 — daily P&L below the loss limit → rejected."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(
            daily_pnl=-3.0,             # below -2.0 limit
            daily_loss_limit=-2.0,
        ),
    )
    assert result.approved is False
    assert result.rejection_category == "daily_loss_limit"


def test_max_drawdown_blocks(gate: PreSubmissionGate):
    """Check #7 — current drawdown > max → rejected."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(
            drawdown=0.20,               # > 0.15 max
            max_drawdown_limit=0.15,
        ),
    )
    assert result.approved is False
    assert result.rejection_category == "max_drawdown"


def test_stale_data_blocks(gate: PreSubmissionGate):
    """Check #8 — market_data.last_update older than staleness window →
    rejected with category=data_freshness."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(
            last_update=time.time() - 120,   # 120s old, > 60s window
        ),
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "data_freshness"


def test_max_spread_blocks(gate: PreSubmissionGate):
    """Check #9 — bid-ask spread > max → rejected."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(spread=0.15),   # > 0.10 max
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "max_spread"


def test_min_liquidity_blocks(gate: PreSubmissionGate):
    """Check #10 — book liquidity < min → rejected."""
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(liquidity=10.0),   # < 50.0 min
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "min_liquidity"


def test_min_edge_blocks(gate: PreSubmissionGate):
    """Check #11 — signal edge < min → rejected."""
    result = gate.check(
        order_request=_valid_order_request(edge=0.01),   # < 0.03 min
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "min_edge"


def test_min_confidence_blocks(gate: PreSubmissionGate):
    """Check #12 — signal confidence < min → rejected."""
    result = gate.check(
        order_request=_valid_order_request(confidence=0.40),   # < 0.55 min
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "min_confidence"


def test_duplicate_blocks(gate: PreSubmissionGate):
    """Check #13 — same (strategy, token_id, side, size, price) 5-tuple
    recorded twice within the TTL window → second call rejected with
    category=idempotency."""
    # First call — records the key, approves.
    r1 = gate.check(
        order_request=_valid_order_request(order_id="ord-1"),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert r1.approved is True

    # Second call — same 5-tuple, different order_id. The idempotency
    # check should detect the duplicate and reject.
    r2 = gate.check(
        order_request=_valid_order_request(order_id="ord-2"),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert r2.approved is False
    assert r2.rejection_category == "idempotency"
    assert "Duplicate" in r2.rejection_reason
    # The rejection message references the existing (first) order_id.
    assert "ord-1" in r2.rejection_reason


def test_circuit_breaker_blocks(gate: PreSubmissionGate):
    """Check #14 — CLOB circuit breaker OPEN → rejected with
    category=circuit_breaker."""
    # Force the CLOB breaker into OPEN state by directly mutating its
    # internal state (the public API only transitions through
    # ``record_failure`` calls which require N consecutive failures).
    clob_breaker._state = CircuitState.OPEN
    clob_breaker._last_failure_time = time.time()
    result = gate.check(
        order_request=_valid_order_request(),
        market_data=_valid_market_data(),
        account_state=_valid_account_state(),
    )
    assert result.approved is False
    assert result.rejection_category == "circuit_breaker"
    assert "CLOB circuit breaker" in result.rejection_reason


# ── Idempotency manager unit tests ──────────────────────────────────────────

def test_idempotency_manager_generate_key_is_deterministic():
    """The idempotency key is deterministic over the 5-tuple — same
    inputs produce the same key; any input perturbation produces a
    different key."""
    k1 = idempotency_manager.generate_key(
        strategy="strat", token_id="tok", side="BUY", size=2.0, price=0.5,
    )
    k2 = idempotency_manager.generate_key(
        strategy="strat", token_id="tok", side="BUY", size=2.0, price=0.5,
    )
    assert k1 == k2
    # Any perturbation produces a different key.
    k3 = idempotency_manager.generate_key(
        strategy="strat", token_id="tok", side="SELL", size=2.0, price=0.5,
    )
    assert k3 != k1


def test_idempotency_manager_check_and_record_first_call_is_unique():
    """The first call with a fresh key returns ``(False, None)`` — not a
    duplicate — and records the key so a subsequent call IS a duplicate."""
    idempotency_manager.reset()
    key = idempotency_manager.generate_key(
        strategy="s", token_id="t", side="BUY", size=1.0, price=0.5,
    )
    is_dup, existing = idempotency_manager.check_and_record(key, "ord-1")
    assert is_dup is False
    assert existing is None
    # Second call with same key → duplicate.
    is_dup, existing = idempotency_manager.check_and_record(key, "ord-2")
    assert is_dup is True
    assert existing == "ord-1"


def test_idempotency_manager_expired_entry_is_unique(monkeypatch):
    """When a recorded entry is older than the TTL window, a subsequent
    call returns ``(False, None)`` — the entry is treated as expired,
    removed, and the new entry recorded in its place."""
    # Use a fresh manager with a tiny TTL so we can simulate expiry
    # without a real sleep.
    from core.idempotency import IdempotencyManager
    mgr = IdempotencyManager(ttl_seconds=0.05)
    key = mgr.generate_key(
        strategy="s", token_id="t", side="BUY", size=1.0, price=0.5,
    )
    is_dup, _ = mgr.check_and_record(key, "ord-1")
    assert is_dup is False
    # Sleep past the TTL.
    time.sleep(0.10)
    is_dup, existing = mgr.check_and_record(key, "ord-2")
    assert is_dup is False
    assert existing is None


# ── submit_order wiring tests ───────────────────────────────────────────────

class _StubStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for wiring tests."""

    name: str = "stub_gate_test"

    def __init__(self) -> None:
        super().__init__()

    async def _run(self) -> None:
        await asyncio.Event().wait()


_TOKEN_ID = "0xpre_submission_gate_wiring_test"


def _order_args(
    *, side: Side = Side.BUY, price: float = 0.50, size: float = 2.0,
    token_id: str = _TOKEN_ID,
) -> OrderArgs:
    return OrderArgs(token_id=token_id, price=price, side=side, size=size)


async def test_submit_order_short_circuits_on_gate_rejection(monkeypatch):
    """When the pre-submission gate rejects, ``submit_order`` returns
    ``None`` AND ``risk_manager.check_order`` is NEVER called AND
    ``paper_sim.create_order`` is NEVER called."""
    strat = _StubStrategy()
    assert strat._paper is True  # paper-mode branch

    # Force the gate to reject by activating the kill switch.
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )

    mock_risk = SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-gate-rej")

    # Gate rejected → submit_order returned None.
    assert result is None
    # risk_manager.check_order was NEVER called (gate short-circuited).
    mock_risk.check_order.assert_not_awaited()
    # paper_sim.create_order was NEVER called.
    mock_paper.create_order.assert_not_awaited()


async def test_submit_order_proceeds_on_gate_approval(monkeypatch):
    """When the pre-submission gate approves, ``submit_order`` proceeds
    to ``risk_manager.check_order`` (defense in depth) and then to
    ``paper_sim.create_order`` when risk approves too."""
    strat = _StubStrategy()

    # Gate approves (kill switch off, no account_state → permissive).
    # Belt-and-braces: ensure kill switch is off.
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: False
    )

    sentinel_order = Order(
        order_id="paper-sentinel-gate-approve",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-gate-ok",
    )
    mock_risk = SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))
    mock_paper = SimpleNamespace(
        create_order=AsyncMock(return_value=sentinel_order),
    )
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-gate-ok")

    # Risk gate was called (gate approved → defense in depth proceeded).
    mock_risk.check_order.assert_awaited_once()
    # Paper sim was called (risk approved → paper path proceeded).
    mock_paper.create_order.assert_awaited_once()
    # Returned the sentinel order (paper path).
    assert result is sentinel_order


async def test_submit_order_passes_gate_context_to_gate(monkeypatch):
    """When the caller passes ``gate_context`` with edge/confidence/
    market_data/account_state, the gate receives them and enforces
    the corresponding checks. A failing account_state check (e.g.
    insufficient balance) short-circuits the order."""
    strat = _StubStrategy()
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: False
    )
    mock_risk = SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args(size=10.0, price=0.50)  # cost = 5.0
    # Pass gate_context with insufficient balance → balance check fails.
    result = await strat.submit_order(
        args,
        decision_id="dec-gate-ctx",
        gate_context={
            "edge": 0.05,
            "confidence": 0.65,
            "market_data": _valid_market_data(),
            "account_state": _valid_account_state(balance=1.0),  # < 5.0 cost
        },
    )
    # Gate rejected → submit_order returned None.
    assert result is None
    # Risk gate NEVER called (gate short-circuited).
    mock_risk.check_order.assert_not_awaited()
    mock_paper.create_order.assert_not_awaited()


# ── API route test ───────────────────────────────────────────────────────────

def test_api_route_returns_full_result(monkeypatch):
    """``POST /api/risk/pre-submission-check`` returns 200 with the full
    ``PreSubmissionResult`` shape (``approved``, ``checks[]``,
    ``rejection_reason``, ``rejection_category``, ``timestamp``).

    Drives the real production ``app`` (``api.server.app``) via
    ``TestClient`` so the request traverses the CORS / auth /
    request-logging middlewares AND the route handler."""
    # Late import so the env-var redirects above are in effect when
    # ``api.server`` is imported (it reads env vars at module-import
    # time).
    from api.server import app
    from fastapi.testclient import TestClient

    # Belt-and-braces: ensure the kill switch is off for the duration
    # of this test so the kill_switch check passes.
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: False
    )
    # Reset the gate's thresholds (in case a prior test configured them).
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    # Clear the idempotency cache so the request is not flagged as a dup.
    idempotency_manager.reset()

    # Use the conftest-set API token.
    token = os.environ.get("API_TOKEN", "test-token-conftest")

    # ── (a) Approving request ──────────────────────────────────────────
    payload = {
        "order_request": _valid_order_request(order_id="api-ord-1"),
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
    assert len(body["checks"]) == 14
    assert "timestamp" in body

    # ── (b) Rejecting request — kill switch on ─────────────────────────
    monkeypatch.setattr(
        "core.safety.kill_switch_file_exists", lambda: True
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/pre-submission-check",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, (
        f"POST /api/risk/pre-submission-check must return 200 even for "
        f"a rejecting payload (the route reports the rejection in the "
        f"body, not the HTTP status); got {response.status_code}. "
        f"Body: {response.text!r}"
    )
    body = response.json()
    assert body["approved"] is False
    assert body["rejection_category"] == "kill_switch"
    assert "Kill switch is active" in body["rejection_reason"]
    # The kill_switch check entry should be present and failed.
    ks_check = next(
        (c for c in body["checks"] if c["check_name"] == "kill_switch"),
        None,
    )
    assert ks_check is not None
    assert ks_check["passed"] is False


def test_api_route_auth_required():
    """``POST /api/risk/pre-submission-check`` is NOT in ``PUBLIC_PATHS`` —
    a request without a bearer token returns 401."""
    from api.server import app
    from fastapi.testclient import TestClient

    # Empty / missing Authorization header → 401 (the route is not in
    # PUBLIC_PATHS so ``enforce_api_auth`` middleware blocks it).
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/pre-submission-check",
            json={"order_request": _valid_order_request()},
        )
    assert response.status_code in (401, 403), (
        f"POST /api/risk/pre-submission-check without a bearer token "
        f"must return 401 or 403 (auth required); got "
        f"{response.status_code}. Body: {response.text!r}"
    )
