"""
W13-2 — Unit tests for ``core/circuit_breaker.py``.

Covers the full state-machine surface plus the decorator / registry helpers:

  Closed-state behaviour
    1. ``can_execute`` returns True while CLOSED (the steady state).
    2. ``record_success`` keeps the breaker CLOSED and zeroes failure_count.

  Failure-accumulation / OPEN transition
    3. ``record_failure`` accumulates failures; after ``failure_threshold``
       consecutive failures the breaker transitions CLOSED -> OPEN.
    4. ``can_execute`` returns False while OPEN — failing fast.

  HALF_OPEN + recovery transition
    5. After ``recovery_timeout`` elapses, ``state`` flips OPEN -> HALF_OPEN
       (verified by manipulating ``_last_failure_time`` directly rather than
       sleeping — keeps the test fast and deterministic).
    6. ``can_execute`` returns True for the first ``half_open_max_calls``
       calls in HALF_OPEN, then False.
    7. After ``success_threshold`` consecutive successes in HALF_OPEN the
       breaker transitions HALF_OPEN -> CLOSED.
    8. A single failure in HALF_OPEN re-opens the breaker (HALF_OPEN -> OPEN).

  Decorator (sync + async)
    9. The ``@breaker`` decorator wraps a sync function: success records
       ``record_success``, exception records ``record_failure`` + re-raises.
   10. The ``@breaker`` decorator wraps an ``async def`` function: the
       wrapper is itself a coroutine function and awaits the wrapped call
       before recording the outcome (so a coroutine that raises is
       recorded as a failure, NOT a success).
   11. The decorator raises ``CircuitBreakerOpenError`` immediately when
       the breaker is OPEN — the wrapped function is never invoked.

  Thread safety
   12. Concurrent ``can_execute`` + ``record_failure`` from multiple threads
       does not crash / corrupt state (basic race-free behaviour — no
       exceptions, breaker ends up OPEN after ``failure_threshold``
       failures).

  reset / status
   13. ``reset()`` force-transitions any state back to CLOSED and zeroes
       the failure / success counters.
   14. ``status()`` returns a dict with every documented field
       (``name`` / ``state`` / ``failure_count`` / ``success_count`` /
       ``failure_threshold`` / ``recovery_timeout`` /
       ``last_failure_time`` / ``last_state_change``).

  Registry helpers
   15. ``get_breaker("clob_api")`` returns the module-level clob_breaker.
   16. ``get_breaker("does_not_exist")`` returns None.
   17. ``get_all_breaker_status()`` returns a list with one entry per
       registered breaker (3 entries — clob / gamma / ws).

The module-level breakers (``clob_breaker`` / ``gamma_breaker`` /
``websocket_breaker``) are NEVER mutated by these tests — each test
constructs a fresh ``CircuitBreaker()`` instance so that state cannot
leak into other test modules that exercise the integrated API clients
(``tests/test_clob_client.py``, ``tests/test_gamma_client.py``) and
rely on the singleton breakers staying CLOSED.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration — mirrors every sibling test module
(``tests/test_gamma_client.py``, ``tests/test_decision_ledger.py`` etc.)
since the repo's ``pytest.ini`` cannot be edited per the additive-files
constraint.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
    clob_breaker,
    gamma_breaker,
    get_all_breaker_status,
    get_breaker,
    websocket_breaker,
)

# Only the two async tests are explicitly marked with ``@pytest.mark.asyncio``
# below — the rest are sync. The repo's convention is the module-level
# ``pytestmark = pytest.mark.asyncio`` (see ``tests/test_clob_client.py``)
# but that produces a PytestWarning for every sync test, so we annotate
# only the async ones explicitly to keep the test output clean.


# ── 1. CLOSED allows execution ──────────────────────────────────────────────


def test_closed_state_allows_execution():
    """``can_execute`` returns True while CLOSED (the steady state)."""
    breaker = CircuitBreaker("test_closed", CircuitBreakerConfig(failure_threshold=5))
    assert breaker.state == CircuitState.CLOSED
    assert breaker.can_execute() is True


# ── 2. record_success keeps CLOSED + zeroes failures ────────────────────────


def test_record_success_keeps_closed_and_zeroes_failures():
    """A success while CLOSED is a no-op for state but resets failure_count
    (so the next sustained failure run must restart from 0)."""
    breaker = CircuitBreaker("test_succ_closed", CircuitBreakerConfig(failure_threshold=3))
    # Prime the failure counter with a couple of failures (but not enough
    # to trip the breaker).
    breaker.record_failure(ValueError("boom"))
    breaker.record_failure(ValueError("boom"))
    assert breaker._failure_count == 2

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker._failure_count == 0


# ── 3. failure accumulation trips the breaker ───────────────────────────────


def test_failure_accumulation_trips_breaker():
    """After ``failure_threshold`` consecutive failures the breaker
    transitions CLOSED -> OPEN."""
    breaker = CircuitBreaker("test_acc_fail", CircuitBreakerConfig(failure_threshold=3))
    breaker.record_failure(ValueError("e1"))
    breaker.record_failure(ValueError("e2"))
    assert breaker.state == CircuitState.CLOSED, "two failures < threshold=3"
    breaker.record_failure(ValueError("e3"))
    assert breaker.state == CircuitState.OPEN, (
        "third failure must trip the breaker CLOSED -> OPEN"
    )


# ── 4. OPEN blocks execution ────────────────────────────────────────────────


def test_open_state_blocks_execution():
    """``can_execute`` returns False while OPEN — fail fast, no upstream call."""
    breaker = CircuitBreaker("test_open_block", CircuitBreakerConfig(failure_threshold=2))
    breaker.record_failure(ValueError("e1"))
    breaker.record_failure(ValueError("e2"))
    assert breaker.state == CircuitState.OPEN
    assert breaker.can_execute() is False


# ── 5. recovery_timeout elapses -> OPEN flips to HALF_OPEN ──────────────────


def test_open_transitions_to_half_open_after_recovery_timeout():
    """After ``recovery_timeout`` elapses, accessing ``state`` flips the
    breaker OPEN -> HALF_OPEN (one-shot — the transition happens at most
    once per OPEN window)."""
    breaker = CircuitBreaker(
        "test_recovery",
        CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0),
    )
    breaker.record_failure(ValueError("trip"))
    assert breaker.state == CircuitState.OPEN

    # Simulate that the recovery timeout has elapsed by rewinding the
    # last_failure_time past the threshold. (Sleeping 30s would also work
    # but makes the test unnecessarily slow.)
    breaker._last_failure_time = time.time() - 31.0

    state = breaker.state  # property accessor triggers the OPEN -> HALF_OPEN transition
    assert state == CircuitState.HALF_OPEN


# ── 6. HALF_OPEN allows limited calls ───────────────────────────────────────


def test_half_open_allows_limited_calls():
    """In HALF_OPEN, ``can_execute`` returns True for the first
    ``half_open_max_calls`` calls and False thereafter (test requests
    are rate-limited so a recovering service isn't flooded)."""
    breaker = CircuitBreaker(
        "test_half_open_cap",
        CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=1.0,
            half_open_max_calls=3,
            success_threshold=2,
        ),
    )
    breaker.record_failure(ValueError("trip"))
    assert breaker.state == CircuitState.OPEN

    # Force HALF_OPEN
    breaker._last_failure_time = time.time() - 2.0
    assert breaker.state == CircuitState.HALF_OPEN

    # First three calls are allowed.
    assert breaker.can_execute() is True
    assert breaker.can_execute() is True
    assert breaker.can_execute() is True
    # Fourth call is rejected (cap reached).
    assert breaker.can_execute() is False


# ── 7. HALF_OPEN -> CLOSED after success_threshold successes ────────────────


def test_half_open_closes_after_success_threshold():
    """After ``success_threshold`` consecutive successes in HALF_OPEN,
    the breaker transitions HALF_OPEN -> CLOSED (full recovery)."""
    breaker = CircuitBreaker(
        "test_half_to_closed",
        CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=1.0,
            half_open_max_calls=5,
            success_threshold=2,
        ),
    )
    breaker.record_failure(ValueError("trip"))
    breaker._last_failure_time = time.time() - 2.0
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.HALF_OPEN, "one success < threshold=2"
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED, (
        "second success must close the breaker HALF_OPEN -> CLOSED"
    )
    assert breaker._failure_count == 0
    assert breaker._success_count == 0


# ── 8. HALF_OPEN failure re-opens the breaker ───────────────────────────────


def test_half_open_failure_reopens_breaker():
    """A single failure in HALF_OPEN transitions the breaker back to OPEN
    (the test request failed — service has not recovered)."""
    breaker = CircuitBreaker(
        "test_half_fail_reopen",
        CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=1.0,
            half_open_max_calls=5,
            success_threshold=2,
        ),
    )
    breaker.record_failure(ValueError("trip"))
    breaker._last_failure_time = time.time() - 2.0
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure(ValueError("still broken"))
    assert breaker.state == CircuitState.OPEN, (
        "failure in HALF_OPEN must re-open the breaker"
    )


# ── 9. Decorator wraps sync functions ────────────────────────────────────────


def test_decorator_wraps_sync_function_success():
    """``@breaker`` on a sync function: success records ``record_success``;
    the breaker stays CLOSED."""
    breaker = CircuitBreaker("test_deco_sync_ok", CircuitBreakerConfig())

    @breaker
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5
    assert breaker.state == CircuitState.CLOSED
    assert breaker._failure_count == 0


def test_decorator_wraps_sync_function_failure():
    """``@breaker`` on a sync function: an exception records
    ``record_failure`` AND re-raises the original exception."""
    breaker = CircuitBreaker(
        "test_deco_sync_fail",
        CircuitBreakerConfig(failure_threshold=2),
    )

    @breaker
    def boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        boom()
    assert breaker._failure_count == 1, "first failure must be recorded"
    assert breaker.state == CircuitState.CLOSED, "threshold=2 not yet reached"

    with pytest.raises(RuntimeError, match="kaboom"):
        boom()
    assert breaker.state == CircuitState.OPEN, (
        "second failure must trip the breaker"
    )


# ── 10. Decorator wraps async functions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_decorator_wraps_async_function_success():
    """``@breaker`` on an ``async def``: the wrapper is itself a coroutine
    function — it awaits the wrapped call BEFORE recording the outcome."""
    breaker = CircuitBreaker("test_deco_async_ok", CircuitBreakerConfig())

    @breaker
    async def fetch_value() -> int:
        return 42

    result = await fetch_value()
    assert result == 42
    assert breaker.state == CircuitState.CLOSED
    assert breaker._failure_count == 0
    assert breaker._success_count == 0  # CLOSED branch resets failure_count only


@pytest.mark.asyncio
async def test_decorator_wraps_async_function_failure():
    """``@breaker`` on an ``async def``: an exception raised from the
    awaited coroutine is recorded as a failure AND re-raised.

    Crucially: a naive sync decorator would return the un-awaited coroutine
    object (truthy, no exception raised yet) and call ``record_success``
    BEFORE the coroutine actually ran — masking every failure from the
    breaker. This test pins the async-aware behaviour.
    """
    breaker = CircuitBreaker(
        "test_deco_async_fail",
        CircuitBreakerConfig(failure_threshold=2),
    )

    @breaker
    async def boom_async() -> None:
        raise RuntimeError("async kaboom")

    with pytest.raises(RuntimeError, match="async kaboom"):
        await boom_async()
    assert breaker._failure_count == 1, (
        "async exception must be recorded as a failure"
    )

    with pytest.raises(RuntimeError, match="async kaboom"):
        await boom_async()
    assert breaker.state == CircuitState.OPEN


# ── 11. Decorator raises CircuitBreakerOpenError when OPEN ───────────────────


def test_decorator_raises_when_open():
    """When the breaker is OPEN, calling the decorated function raises
    ``CircuitBreakerOpenError`` immediately — the wrapped function is
    never invoked (verified via a sentinel side-effect)."""
    breaker = CircuitBreaker(
        "test_deco_open",
        CircuitBreakerConfig(failure_threshold=1),
    )

    sentinel: list[str] = []

    @breaker
    def side_effecting_call() -> str:
        sentinel.append("called")
        return "ok"

    # Trip the breaker.
    breaker.record_failure(ValueError("trip"))
    assert breaker.state == CircuitState.OPEN

    with pytest.raises(CircuitBreakerOpenError, match="OPEN"):
        side_effecting_call()
    assert sentinel == [], (
        "the wrapped function must NOT execute when the breaker is OPEN"
    )


# ── 12. Thread safety — concurrent access does not crash ────────────────────


def test_concurrent_access_does_not_crash():
    """``can_execute`` + ``record_failure`` are guarded by a
    ``threading.Lock`` — concurrent calls from multiple threads do not
    crash, do not corrupt internal counters, and converge on the OPEN
    state once enough failures have been recorded.

    This is a smoke test for the lock discipline — it does NOT attempt to
    verify linearizability (which would require a stricter test harness
    than pytest's default thread scheduling)."""
    breaker = CircuitBreaker(
        "test_threaded",
        CircuitBreakerConfig(failure_threshold=100, recovery_timeout=1.0),
    )

    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(200):
                if breaker.can_execute():
                    try:
                        # Simulate occasional success / failure.
                        breaker.record_failure(ValueError("worker fail"))
                    except Exception as e:  # noqa: BLE001 — captured for assertion
                        errors.append(e)
        except Exception as e:  # noqa: BLE001 — captured for assertion
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"no exceptions expected from concurrent access: {errors}"
    # After 8 threads * 200 failures = 1600 recorded failures against a
    # threshold of 100, the breaker MUST be OPEN (it trips at failure #100).
    assert breaker.state == CircuitState.OPEN


# ── 13. reset() force-closes ────────────────────────────────────────────────


def test_reset_force_closes_breaker():
    """``reset()`` transitions the breaker from any state to CLOSED and
    zeroes the failure / success counters — used by operators after they
    have manually verified the upstream service is healthy."""
    breaker = CircuitBreaker("test_reset", CircuitBreakerConfig(failure_threshold=1))
    breaker.record_failure(ValueError("trip"))
    breaker._last_failure_time = time.time() - 100.0
    breaker.state  # trigger OPEN -> HALF_OPEN transition
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
    assert breaker._failure_count == 0
    assert breaker._success_count == 0
    assert breaker._half_open_calls == 0


def test_reset_on_open_breaker():
    """``reset()`` works from OPEN state too (not just HALF_OPEN)."""
    breaker = CircuitBreaker("test_reset_open", CircuitBreakerConfig(failure_threshold=1))
    breaker.record_failure(ValueError("trip"))
    assert breaker.state == CircuitState.OPEN

    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
    assert breaker._failure_count == 0


# ── 14. status() returns the documented fields ──────────────────────────────


def test_status_returns_documented_fields():
    """``status()`` returns a dict carrying every documented field with
    the expected types / values for a freshly-constructed breaker."""
    breaker = CircuitBreaker(
        "test_status",
        CircuitBreakerConfig(failure_threshold=5, recovery_timeout=30.0),
    )
    status = breaker.status()
    assert status["name"] == "test_status"
    assert status["state"] == "closed"
    assert status["failure_count"] == 0
    assert status["success_count"] == 0
    assert status["failure_threshold"] == 5
    assert status["recovery_timeout"] == 30.0
    assert status["last_failure_time"] is None
    assert isinstance(status["last_state_change"], float)


def test_status_reflects_failure_accumulation():
    """After a failure, ``status()`` reports the updated failure_count and
    a populated ``last_failure_time``."""
    breaker = CircuitBreaker("test_status_fail", CircuitBreakerConfig(failure_threshold=5))
    breaker.record_failure(ValueError("e"))
    status = breaker.status()
    assert status["failure_count"] == 1
    assert status["last_failure_time"] is not None
    assert isinstance(status["last_failure_time"], float)


def test_status_reflects_open_state():
    """After tripping the breaker, ``status()`` reports state='open'."""
    breaker = CircuitBreaker("test_status_open", CircuitBreakerConfig(failure_threshold=2))
    breaker.record_failure(ValueError("e1"))
    breaker.record_failure(ValueError("e2"))
    status = breaker.status()
    assert status["state"] == "open"
    assert status["failure_count"] == 2


# ── 15. Registry: get_breaker returns named singletons ──────────────────────


def test_get_breaker_returns_named_singletons():
    """``get_breaker("clob_api")`` / ``("gamma_api")`` / ``("polymarket_ws")``
    return the module-level breakers of the same name."""
    assert get_breaker("clob_api") is clob_breaker
    assert get_breaker("gamma_api") is gamma_breaker
    assert get_breaker("polymarket_ws") is websocket_breaker


# ── 16. Registry: unknown name returns None ────────────────────────────────


def test_get_breaker_unknown_returns_none():
    """``get_breaker("does_not_exist")`` returns ``None`` (no exception,
    no silent default — the registry lookup is a dict.get)."""
    assert get_breaker("does_not_exist") is None


# ── 17. Registry: get_all_breaker_status lists every breaker ────────────────


def test_get_all_breaker_status_returns_three_entries():
    """``get_all_breaker_status()`` returns one entry per registered
    breaker (clob_api / gamma_api / polymarket_ws) — each carrying the
    full status dict shape."""
    statuses = get_all_breaker_status()
    assert isinstance(statuses, list)
    assert len(statuses) == 3, f"expected 3 breakers, got {len(statuses)}"
    names = {s["name"] for s in statuses}
    assert names == {"clob_api", "gamma_api", "polymarket_ws"}
    # Each entry must carry the documented status fields.
    for s in statuses:
        assert "state" in s and s["state"] == "closed", (
            f"breaker '{s['name']}' must start CLOSED (tests must not mutate "
            f"the module-level singletons)"
        )
        assert "failure_count" in s
        assert "success_count" in s
        assert "failure_threshold" in s
        assert "recovery_timeout" in s


# ── Bonus: CircuitBreakerOpenError is a real Exception subclass ────────────


def test_circuit_breaker_open_error_is_exception_subclass():
    """``CircuitBreakerOpenError`` must be a subclass of ``Exception`` so
    callers can ``except Exception`` catch it generically, or catch it
    specifically with ``except CircuitBreakerOpenError``."""
    assert issubclass(CircuitBreakerOpenError, Exception)
    err = CircuitBreakerOpenError("test")
    assert isinstance(err, Exception)


# ── Bonus: config defaults ──────────────────────────────────────────────────


def test_default_config_values():
    """The default ``CircuitBreakerConfig`` dataclass exposes the
    documented defaults (failure_threshold=5, recovery_timeout=30,
    half_open_max_calls=3, success_threshold=2, timeout=10)."""
    cfg = CircuitBreakerConfig()
    assert cfg.failure_threshold == 5
    assert cfg.recovery_timeout == 30.0
    assert cfg.half_open_max_calls == 3
    assert cfg.success_threshold == 2
    assert cfg.timeout == 10.0


# ── Bonus: module singletons use the right configs ──────────────────────────


def test_module_singletons_use_documented_configs():
    """The module-level breakers (``clob_breaker`` / ``gamma_breaker`` /
    ``websocket_breaker``) are pre-configured per the W13-2 spec:

      * clob_breaker:        failure_threshold=5, recovery_timeout=30
      * gamma_breaker:       failure_threshold=3, recovery_timeout=60
      * websocket_breaker:   failure_threshold=5, recovery_timeout=15
    """
    assert clob_breaker.config.failure_threshold == 5
    assert clob_breaker.config.recovery_timeout == 30
    assert gamma_breaker.config.failure_threshold == 3
    assert gamma_breaker.config.recovery_timeout == 60
    assert websocket_breaker.config.failure_threshold == 5
    assert websocket_breaker.config.recovery_timeout == 15
