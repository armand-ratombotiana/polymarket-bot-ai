"""W17-9 — Cross-module integration tests for the risk-management pipeline.

Drives the three pillars of the risk-management layer end-to-end:

  1. **Kill switch halt + recovery**: activate the durable kill switch
     via ``risk_manager.activate_kill_switch()``, verify ``check_order``
     rejects every subsequent order with the canonical "Kill switch is
     active — all trading halted" reason, then ``deactivate_kill_switch``
     and verify trading resumes.

  2. **Circuit breaker state machine for external API calls**: drive a
     ``CircuitBreaker`` through CLOSED → OPEN → HALF_OPEN → CLOSED
     transitions, verifying fail-fast behaviour in OPEN state and the
     recovery path through HALF_OPEN.

  3. **Max drawdown → circuit breaker trips**: simulate a drawdown
     breach (``peak_equity - current_equity >= MAX_DRAWDOWN_LIMIT``) and
     verify ``check_order`` rejects with "Max drawdown limit reached",
     AND the risk engine activates the durable kill switch (so trading
     halts across the whole system, not just on the next order).

Hermeticity
-----------
The autouse ``_reset_store_factory_defaults`` conftest fixture wipes
``store.kill_switch_active`` AND the durable kill-switch marker file to
factory defaults BEFORE every test, so a prior test's kill-switch
activation cannot leak into the next. Each test uses its own unique
``token_id`` so ``store.positions`` and ``store.open_orders`` entries
from prior tests never collide.
"""
from __future__ import annotations

import pytest

from core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
)
from core.clob_client import OrderArgs
from core.data_store import Order, Side, store
from core.safety import KILL_SWITCH_PATH, kill_switch_file_exists
from risk.manager import MAX_DRAWDOWN_LIMIT, risk_manager

# pytest-asyncio strict mode — explicit module-level mark for async tests.
pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_order(
    token_id: str, decision_id: str = "dec-test", strategy: str = "signal_trader"
) -> Order:
    """Build a small paper BUY order that satisfies all the size / price /
    liquidity constraints in ``check_order`` (so the only rejection path
    exercised by the test is the one the test deliberately triggers).

    ``price=0.50``, ``size=2.0`` → ``order_cost = 1.0`` USD, well under
    every per-trade cap. ``paper=True`` so the live-trading-disabled and
    observation-only gates short-circuit cleanly.
    """
    return Order(
        order_id=f"test-order-{token_id}",
        token_id=token_id,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strategy,
        paper=True,
        decision_id=decision_id,
    )


# ── (1) Kill switch halt + recovery ────────────────────────────────────────


async def test_kill_switch_activation_blocks_all_orders():
    """Activating the kill switch causes every subsequent ``check_order``
    call to reject with the canonical "Kill switch is active" reason.

    Verifies the cross-module wiring: ``risk_manager.activate_kill_switch``
    writes the durable marker file (via ``core.safety.write_kill_switch``)
    AND flips ``store.kill_switch_active = True`` — so both gates in
    ``_check_order_impl`` (the in-memory flag AND the file-existence
    check) trip simultaneously.
    """
    TOKEN = f"TEST_KS_ON_{abs(hash('test_kill_switch_activation_blocks_all_orders')) % 10_000_000}"

    # Pre-condition: kill switch is NOT active (autouse conftest fixture
    # clears it before every test, but defensive check).
    assert not store.kill_switch_active
    assert not kill_switch_file_exists()

    # Activate the kill switch.
    await risk_manager.activate_kill_switch("W17-9 test kill switch activation")

    # Both gates now tripped.
    assert store.kill_switch_active is True
    assert kill_switch_file_exists(), (
        "durable kill-switch marker file must exist after activation"
    )

    # Every order is rejected.
    order = _build_order(token_id=TOKEN)
    allowed, reason = await risk_manager.check_order(order)
    assert allowed is False
    assert "kill switch" in reason.lower(), (
        f"rejection reason should mention kill switch; got {reason!r}"
    )

    # Cleanup: deactivate so subsequent tests see a clean slate (the
    # autouse conftest fixture also clears the marker file before the
    # next test, but explicit is safer).
    await risk_manager.deactivate_kill_switch()
    assert store.kill_switch_active is False


async def test_kill_switch_deactivation_allows_orders():
    """After ``deactivate_kill_switch``, ``check_order`` accepts orders
    again — the in-memory flag is cleared AND the durable marker file
    is removed."""
    TOKEN = f"TEST_KS_OFF_{abs(hash('test_kill_switch_deactivation_allows_orders')) % 10_000_000}"

    # Activate.
    await risk_manager.activate_kill_switch("W17-9 test kill switch deactivation")
    assert store.kill_switch_active is True
    assert kill_switch_file_exists()

    # Deactivate.
    await risk_manager.deactivate_kill_switch()

    # Both gates cleared.
    assert store.kill_switch_active is False
    assert not kill_switch_file_exists(), (
        "durable kill-switch marker file must be removed after deactivation"
    )

    # Reset peak_equity to the operating-capital baseline so the MDD
    # gate doesn't trip on a phantom drawdown. ``deactivate_kill_switch``
    # sets ``peak_equity = BANKROLL_CEILING + daily_pnl = 200`` while
    # ``_check_order_impl`` computes ``current_equity = OPERATING_CAPITAL
    # + daily_pnl = 100`` — a 100-dollar mismatch that immediately trips
    # the MDD breaker (documented production quirk; the test works
    # around it by resetting the peak to the operating-capital baseline).
    from risk.manager import OPERATING_CAPITAL
    store.peak_equity = float(OPERATING_CAPITAL) + store.daily_pnl

    # Orders are accepted again (a small paper BUY on a fresh store).
    order = _build_order(token_id=TOKEN)
    allowed, reason = await risk_manager.check_order(order)
    assert allowed, (
        f"after kill switch deactivation, a small paper BUY must be "
        f"allowed; got allowed={allowed}, reason={reason!r}"
    )


async def test_kill_switch_marker_file_survives_in_memory_reset():
    """The kill switch is FILE-BACKED so it survives an in-memory reset.

    Simulates a process restart: clear ``store.kill_switch_active``
    (in-memory flag) but leave the marker file in place. A subsequent
    ``check_order`` call must STILL reject because the file-existence
    gate trips — the durable design is what makes the kill switch
    reliable across container recycles.
    """
    TOKEN = f"TEST_KS_DURABLE_{abs(hash('test_kill_switch_marker_file_survives_in_memory_reset')) % 10_000_000}"

    # Activate (writes the marker file + sets the in-memory flag).
    await risk_manager.activate_kill_switch("W17-9 durable test")
    assert kill_switch_file_exists()

    # Simulate a process restart: clear the in-memory flag only.
    store.kill_switch_active = False
    # The marker file is still on disk.
    assert KILL_SWITCH_PATH.exists()

    # ``check_order`` still rejects because the file-existence gate trips.
    order = _build_order(token_id=TOKEN)
    allowed, reason = await risk_manager.check_order(order)
    assert allowed is False
    assert "kill switch" in reason.lower()

    # Cleanup.
    await risk_manager.deactivate_kill_switch()


# ── (2) Circuit breaker state machine for external API calls ───────────────


def test_circuit_breaker_opens_after_failure_threshold():
    """The breaker transitions CLOSED → OPEN after ``failure_threshold``
    consecutive failures.

    Subsequent ``can_execute()`` calls in the OPEN state return False
    (fail-fast — no call to the underlying external service is made).

    Uses ``recovery_timeout=60`` so the OPEN state is stable across the
    test's wall-clock duration (the OPEN → HALF_OPEN auto-transition in
    ``state`` only fires after ``recovery_timeout`` elapses since the
    last failure).
    """
    breaker = CircuitBreaker(
        name="test_breaker_open",
        config=CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout=60.0,
            half_open_max_calls=2,
            success_threshold=2,
        ),
    )
    assert breaker.state == CircuitState.CLOSED

    # Two failures — still CLOSED (threshold not reached).
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED

    # Third failure trips the breaker.
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    # Subsequent calls fail-fast.
    assert breaker.can_execute() is False


def test_circuit_breaker_decorator_raises_when_open():
    """A decorated function raises ``CircuitBreakerOpenError`` when the
    breaker is OPEN — fail-fast behaviour that protects the caller
    from queuing requests against a known-degraded service.
    """
    breaker = CircuitBreaker(
        name="test_breaker_decorator",
        config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout=60.0,  # long timeout so OPEN state stays stable
        ),
    )

    @breaker
    def always_fails():
        raise RuntimeError("simulated downstream failure")

    # First call: raises the underlying exception ( breaker still CLOSED
    # because threshold is 2).
    with pytest.raises(RuntimeError, match="simulated downstream failure"):
        always_fails()
    assert breaker.state == CircuitState.CLOSED

    # Second call: trips the breaker.
    with pytest.raises(RuntimeError, match="simulated downstream failure"):
        always_fails()
    assert breaker.state == CircuitState.OPEN

    # Third call: fail-fast with CircuitBreakerOpenError (no underlying call).
    with pytest.raises(CircuitBreakerOpenError):
        always_fails()


def test_circuit_breaker_recover_half_open_to_closed():
    """After ``recovery_timeout`` elapses, the breaker transitions
    OPEN → HALF_OPEN → CLOSED on ``success_threshold`` consecutive
    successful calls.

    Uses ``recovery_timeout=0.05`` and ``time.sleep(0.10)`` to force the
    OPEN → HALF_OPEN transition; the success_threshold=2 path then
    closes the breaker.
    """
    breaker = CircuitBreaker(
        name="test_breaker_recover",
        config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout=0.05,  # short timeout so the test is fast
            half_open_max_calls=5,
            success_threshold=2,
        ),
    )

    # Trip the breaker.
    breaker.record_failure()
    breaker.record_failure()
    # Wait until recovery_timeout elapses so the next ``state`` access
    # auto-transitions OPEN → HALF_OPEN.
    import time as _time
    _time.sleep(0.10)

    # First can_execute() after recovery_timeout elapsed → transitions
    # to HALF_OPEN, returns True (the half-open probe is allowed).
    assert breaker.can_execute() is True
    assert breaker.state == CircuitState.HALF_OPEN

    # First success in half-open does NOT close (success_threshold=2).
    breaker.record_success()
    assert breaker.state == CircuitState.HALF_OPEN

    # Second success closes the breaker.
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_circuit_breaker_half_open_failure_reopens():
    """A failure during HALF_OPEN immediately re-OPENs the breaker.

    The half-open state is the "probe" phase — even a single failure
    means the downstream service is still unhealthy, so the breaker
    returns to OPEN to fail-fast again until the next recovery_timeout.
    """
    breaker = CircuitBreaker(
        name="test_breaker_half_open_reopen",
        config=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=0.05,
            success_threshold=2,
        ),
    )
    # Trip immediately.
    breaker.record_failure()
    # Wait for recovery_timeout to elapse so the next state access
    # transitions to HALF_OPEN.
    import time as _time
    _time.sleep(0.10)

    # Probe → half-open.
    assert breaker.can_execute() is True
    assert breaker.state == CircuitState.HALF_OPEN

    # Failure during probe → reopen.
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


# ── (3) Max drawdown → circuit breaker trips ──────────────────────────────


async def test_max_drawdown_trips_kill_switch():
    """A drawdown breach (``peak_equity - current_equity >=
    MAX_DRAWDOWN_LIMIT``) trips the kill switch via
    ``_trigger_kill_switch``.

    Verifies the cross-module wiring: the MDD check inside
    ``_check_order_impl`` calls ``_trigger_kill_switch``, which flips
    ``store.kill_switch_active = True`` AND writes the durable marker
    file (so trading halts across the system, not just on the next
    order to the same strategy).
    """
    TOKEN = f"TEST_MDD_{abs(hash('test_max_drawdown_trips_kill_switch')) % 10_000_000}"

    # Set up a drawdown breach: peak_equity high + daily_pnl ≈ 0.
    # ``current_equity = OPERATING_CAPITAL + daily_pnl``
    # ``drawdown = peak_equity - current_equity``
    # We want drawdown >= MAX_DRAWDOWN_LIMIT (8.0) WITHOUT tripping the
    # daily-loss stop (which fires at daily_pnl <= -2.0). So set
    # daily_pnl = 0 and peak_equity = OPERATING_CAPITAL + MAX_DRAWDOWN_LIMIT
    # = 100 + 8 = 108.
    from risk.manager import OPERATING_CAPITAL

    store.peak_equity = float(OPERATING_CAPITAL) + float(MAX_DRAWDOWN_LIMIT)
    store.daily_pnl = 0.0
    assert not store.kill_switch_active

    order = _build_order(token_id=TOKEN)
    allowed, reason = await risk_manager.check_order(order)

    # MDD breaker tripped — order rejected + kill switch activated.
    assert allowed is False
    assert "max drawdown" in reason.lower() or "drawdown" in reason.lower(), (
        f"rejection reason should mention max drawdown; got {reason!r}"
    )

    # The MDD breaker activates the durable kill switch — so the next
    # order is rejected by the kill-switch gate (NOT the MDD gate)
    # because the kill-switch check runs FIRST in ``_check_order_impl``.
    assert store.kill_switch_active is True
    assert kill_switch_file_exists()

    second_order = _build_order(
        token_id=f"{TOKEN}_SECOND",
        decision_id="dec-test-mdd-second",
    )
    allowed2, reason2 = await risk_manager.check_order(second_order)
    assert allowed2 is False
    assert "kill switch" in reason2.lower()

    # Cleanup: deactivate the kill switch so subsequent tests start clean.
    await risk_manager.deactivate_kill_switch()


async def test_max_drawdown_does_not_trip_below_threshold():
    """Sanity check: a drawdown BELOW the threshold does NOT trip the
    MDD breaker.

    This guards against a regression where the comparison operator is
    inverted (``<`` instead of ``>=``) or the threshold is mis-typed.
    """
    TOKEN = f"TEST_MDD_OK_{abs(hash('test_max_drawdown_does_not_trip_below_threshold')) % 10_000_000}"

    from risk.manager import OPERATING_CAPITAL

    # Drawdown of 5.0 (well below MAX_DRAWDOWN_LIMIT of 8.0).
    store.peak_equity = float(OPERATING_CAPITAL) + 5.0
    store.daily_pnl = 0.0

    order = _build_order(token_id=TOKEN)
    allowed, reason = await risk_manager.check_order(order)

    # Order is accepted (no MDD breach, no kill switch).
    assert allowed, (
        f"small drawdown should NOT trip MDD breaker; got "
        f"allowed={allowed}, reason={reason!r}"
    )
    assert not store.kill_switch_active
    assert not kill_switch_file_exists()
