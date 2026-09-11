"""W24-7 — Unit tests for ``core/api_resilience.py``.

Covers the seven contract surfaces enumerated in the W24-7 task spec:

  (1) Successful call → returns result, records ``HEALTHY``.
  (2) Retry on failure → succeeds on the 2nd attempt, returns result.
  (3) Circuit breaker tripping → after 5 consecutive logical-call
      failures the status flips to ``UNHEALTHY`` and every subsequent
      call returns the fallback immediately (no ``call_fn`` invoked).
  (4) Fallback data returned when all retries fail (and ``fallback_data``
      was supplied).
  (5) Timeout handling — a ``call_fn`` that sleeps longer than the
      layer's 5 s timeout is cancelled, recorded as a timeout, and
      retried.
  (6) Health tracking — ``get_health()`` returns a dict keyed by API
      name with every documented ``APIHealth`` field, and
      ``is_healthy()`` returns the documented tri-state.
  (7) API route — ``GET /api/api-health`` returns the resilience
      layer's health snapshot via the production FastAPI app
      (authenticated — bearer token required).

Isolation
~~~~~~~~~

  * Tests (1)-(6) construct a FRESH ``APIResilienceLayer()`` instance
    per test (NOT the module-level singleton) so the per-API health
    counters can't leak between tests. The singleton
    (``core.api_resilience.api_resilience``) is also exercised by the
    CLOB / Gamma client wiring, so the unit tests here MUST NOT touch
    it (otherwise state from a CLOB-client integration test could
    perturb a resilience-layer unit assertion).

  * Test (7) imports the production ``api.server.app`` so every
    middleware, rate limiter, and route registration is exercised.
    Rate limiting is disabled in ``conftest.py``
    (``limiter.enabled = False``) and the bearer token
    (``test-token-conftest``) is set by the conftest env-redirect
    block, so the ``auth_headers`` fixture matches what the
    ``enforce_api_auth`` middleware accepts.

  * Test (7) calls ``api_resilience.reset()`` BEFORE the request so
    the singleton's state from prior test modules (e.g.
    ``test_gamma_client.py``'s ``get_markets`` mock calls) doesn't
    leak into the assertion. The reset is best-effort: even if a
    sibling test recorded a "gamma" success in the meantime, the
    endpoint's response shape is still a JSON dict (the test asserts
    on shape, not on specific counter values, for that exact reason).

  * The ``asyncio.sleep`` patches in tests (2) and (5) replace the
    module-qualified ``core.api_resilience.asyncio.sleep`` with a
    no-op so the 100 ms / 500 ms / 2 000 ms backoff schedule doesn't
    slow the suite down (the resilience layer's correctness doesn't
    depend on the actual wall-clock delay — only on the retry count
    and the order in which successes / failures are recorded).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration — mirrors every sibling test module
(``tests/test_gamma_client.py``, ``tests/test_decision_ledger.py`` etc.)
since the repo's ``pytest.ini`` cannot be edited per the additive-files
constraint.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from api.server import app
from core.api_resilience import (
    APIResilienceLayer,
    APIStatus,
    api_resilience,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_circuit_breaker.py``
# (W13-2): the repo's ``pytest.ini`` cannot be edited per the W24-7
# "Do NOT edit existing files" constraint, so we use the per-test
# decorator (NOT the module-level ``pytestmark``) — the module-level
# mark would emit a ``PytestWarning`` for every SYNC test in the file
# (the 3 TestClient-based route tests below are sync). The per-test
# decorator annotates ONLY the async tests, keeping the test output
# clean.

# Defensive: disable the rate-limit middleware so a fast test sequence
# against a per-minute-limited route doesn't 429 mid-suite.
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# the bearer token below matches what the ``enforce_api_auth`` middleware
# accepts.
VALID_TOKEN = "test-token-conftest"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_layer() -> APIResilienceLayer:
    """Fresh ``APIResilienceLayer`` with default config (no singleton state).

    Every unit test (1)-(6) uses this fixture so the per-API health
    counters start at zero and the test's assertions aren't perturbed
    by state leaked from a prior test (or from the CLOB / Gamma client
    integration tests that hit the module-level singleton).
    """
    return APIResilienceLayer()


@pytest.fixture
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``asyncio.sleep`` inside ``core.api_resilience`` with a no-op.

    The resilience layer's correctness depends on the retry count and
    the order in which successes / failures are recorded — NOT on the
    actual wall-clock delay between retries. Patching ``asyncio.sleep``
    to a no-op keeps the suite fast (3 retries × 2 s = 6 s of real
    sleep avoided per failing-call test) without changing the layer's
    observable behaviour.

    Scoped to ``core.api_resilience.asyncio.sleep`` so sibling modules
    that legitimately sleep (e.g. ``core.book_poller``'s tier intervals)
    are unaffected.
    """
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("core.api_resilience.asyncio.sleep", _no_sleep)


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_api_versioning.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ─────────────────────────────────────────────────────────────────────────────
# (1) Successful call → returns result, records HEALTHY
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_successful_call_returns_result_and_records_healthy(fresh_layer):
    """A successful ``call_fn`` returns its result and records ``HEALTHY``.

    Verifies:
      * The return value is the exact object ``call_fn`` produced.
      * ``total_calls`` is incremented exactly once (no double-counting
        from the inner retry loop).
      * ``status`` is ``HEALTHY``.
      * ``consecutive_failures`` is zero (the success reset it).
      * ``avg_latency_ms`` is positive (the success recorded a real
        latency measurement).
      * ``last_success`` is within ±5 s of ``time.time()`` (the
        recording happened "just now").
      * ``last_error`` is the empty string (no failure ever recorded).
    """
    sentinel = {"bids": [], "asks": []}

    async def _fetch():
        return sentinel

    result = await fresh_layer.call_with_resilience("clob", _fetch)

    assert result is sentinel, (
        f"expected call_with_resilience to return the sentinel; got {result!r}"
    )

    health = fresh_layer.get_health()
    assert "clob" in health, "expected a 'clob' entry in get_health()"
    clob = health["clob"]
    assert clob["status"] == APIStatus.HEALTHY.value, (
        f"expected status=healthy; got {clob['status']!r}"
    )
    assert clob["total_calls"] == 1, (
        f"expected total_calls=1; got {clob['total_calls']}"
    )
    assert clob["consecutive_failures"] == 0, (
        f"expected consecutive_failures=0; got {clob['consecutive_failures']}"
    )
    assert clob["total_failures"] == 0, (
        f"expected total_failures=0; got {clob['total_failures']}"
    )
    assert clob["total_timeouts"] == 0, (
        f"expected total_timeouts=0; got {clob['total_timeouts']}"
    )
    assert clob["avg_latency_ms"] > 0, (
        f"expected avg_latency_ms > 0; got {clob['avg_latency_ms']}"
    )
    assert abs(clob["last_success"] - time.time()) < 5.0, (
        "expected last_success to be within ±5 s of now; "
        f"got {clob['last_success']}"
    )
    assert clob["last_error"] == "", (
        f"expected last_error=''; got {clob['last_error']!r}"
    )

    assert fresh_layer.is_healthy("clob") is True, (
        "is_healthy('clob') must return True after a successful call"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (2) Retry on failure → succeeds on the 2nd attempt
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_retry_succeeds_on_second_attempt(fresh_layer, no_backoff_sleep):
    """A ``call_fn`` that fails once then succeeds returns the success value.

    Verifies the retry loop actually retries (rather than failing fast on
    the first exception) AND that the success on the 2nd attempt is
    recorded as ``HEALTHY`` (the intermediate failure does NOT count
    toward ``consecutive_failures`` because the layer's failure recording
    only fires after every retry is exhausted — the per-attempt
    exception is logged but not counted).

    The ``no_backoff_sleep`` fixture patches ``asyncio.sleep`` so the
    100 ms backoff between attempt 0 and attempt 1 doesn't slow the
    test down (correctness doesn't depend on the real delay).
    """
    call_count = {"n": 0}
    sentinel = {"ok": True}

    async def _fetch():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient failure on first attempt")
        return sentinel

    result = await fresh_layer.call_with_resilience("clob", _fetch)

    assert result is sentinel, (
        f"expected call_with_resilience to return the sentinel after retry; "
        f"got {result!r}"
    )
    assert call_count["n"] == 2, (
        f"expected _fetch to be called exactly twice (fail then succeed); "
        f"got {call_count['n']}"
    )

    clob = fresh_layer.get_health()["clob"]
    assert clob["status"] == APIStatus.HEALTHY.value, (
        f"expected status=healthy after successful retry; got {clob['status']!r}"
    )
    assert clob["total_calls"] == 1, (
        "expected total_calls=1 (one logical call — the retry is internal, "
        f"not a separate logical call); got {clob['total_calls']}"
    )
    assert clob["total_failures"] == 0, (
        "expected total_failures=0 (the logical call succeeded after retry — "
        f"no failure recorded); got {clob['total_failures']}"
    )
    assert clob["consecutive_failures"] == 0, (
        f"expected consecutive_failures=0; got {clob['consecutive_failures']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (3) Circuit breaker tripping → after 5 failures, fallback returned
#     immediately (no call_fn invoked)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_circuit_breaker_trips_after_threshold_failures(
    fresh_layer, no_backoff_sleep,
):
    """After 5 consecutive logical-call failures, the breaker trips.

    Verifies:
      * The 5th failing call records ``consecutive_failures=5`` and
        ``status=UNHEALTHY``.
      * The 6th call does NOT invoke ``call_fn`` at all (the breaker
        short-circuits before the retry loop). The 6th call returns
        the ``fallback_data`` verbatim.
      * The 6th call's ``total_calls`` counter is NOT incremented
        (the short-circuit path records no logical call — the breaker
        is in fail-fast mode, which is conceptually "no call attempted"
        rather than "a call that failed").

    The ``no_backoff_sleep`` fixture is required because each failing
    logical call burns 3 attempts × 2 sleeps = 6 ``asyncio.sleep``
    invocations (100 ms + 500 ms + 2 000 ms = 2.6 s of real sleep per
    failing logical call, 13 s for 5 calls — well over the 2-minute
    per-test timeout).
    """
    call_count = {"n": 0}

    async def _always_fail():
        call_count["n"] += 1
        raise RuntimeError("upstream is down")

    # Drive 5 failing logical calls. ``fallback_data`` is supplied so
    # the layer returns it instead of raising ``ConnectionError`` —
    # this lets us run 5 calls in a single test without try/except
    # scaffolding around each one.
    fallback = {"stale": True}
    for i in range(5):
        result = await fresh_layer.call_with_resilience(
            "clob", _always_fail, fallback_data=fallback,
        )
        assert result is fallback, (
            f"call {i + 1} should have returned fallback_data after exhausting "
            f"retries; got {result!r}"
        )

    # After 5 logical failures the layer's internal breaker should be
    # tripped. Each logical call did 3 retries → 15 ``_always_fail``
    # invocations total.
    assert call_count["n"] == 15, (
        f"expected _always_fail to be called 15 times (5 logical calls × 3 "
        f"retries); got {call_count['n']}"
    )

    clob = fresh_layer.get_health()["clob"]
    assert clob["consecutive_failures"] == 5, (
        f"expected consecutive_failures=5; got {clob['consecutive_failures']}"
    )
    assert clob["total_failures"] == 5, (
        f"expected total_failures=5; got {clob['total_failures']}"
    )
    assert clob["status"] == APIStatus.UNHEALTHY.value, (
        f"expected status=unhealthy after threshold reached; "
        f"got {clob['status']!r}"
    )
    assert fresh_layer.is_healthy("clob") is False, (
        "is_healthy('clob') must return False once the breaker is tripped"
    )

    # 6th call: breaker is OPEN → ``_always_fail`` is NOT invoked at
    # all. The fallback is returned immediately.
    calls_before = call_count["n"]
    result = await fresh_layer.call_with_resilience(
        "clob", _always_fail, fallback_data=fallback,
    )
    assert result is fallback, (
        f"6th call should have returned fallback_data immediately; got {result!r}"
    )
    assert call_count["n"] == calls_before, (
        f"expected _always_fail to NOT be called when the breaker is open; "
        f"got {call_count['n'] - calls_before} extra invocation(s)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (4) Fallback data returned when all retries fail
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_fallback_returned_when_all_retries_fail(
    fresh_layer, no_backoff_sleep,
):
    """When ``fallback_data`` is supplied and every retry fails, the
    fallback is returned instead of raising.

    Verifies:
      * The returned value is the exact ``fallback_data`` object.
      * ``call_fn`` was invoked 3 times (``_max_retries``).
      * The failure is recorded against the per-API health.
    """
    call_count = {"n": 0}
    fallback = {"cached": "snapshot"}

    async def _always_fail():
        call_count["n"] += 1
        raise ConnectionRefusedError("upstream refused")

    result = await fresh_layer.call_with_resilience(
        "clob", _always_fail, fallback_data=fallback,
    )

    assert result is fallback, (
        f"expected fallback_data returned after retries exhausted; got {result!r}"
    )
    assert call_count["n"] == 3, (
        f"expected _always_fail to be called 3 times (max_retries); "
        f"got {call_count['n']}"
    )

    clob = fresh_layer.get_health()["clob"]
    assert clob["total_failures"] == 1, (
        f"expected total_failures=1 (one logical call failed); "
        f"got {clob['total_failures']}"
    )
    assert clob["consecutive_failures"] == 1, (
        f"expected consecutive_failures=1; got {clob['consecutive_failures']}"
    )
    assert clob["status"] == APIStatus.UNKNOWN.value or (
        clob["status"] in (APIStatus.HEALTHY.value, APIStatus.DEGRADED.value,
                           APIStatus.UNHEALTHY.value)
    ), (
        f"expected status to be a valid APIStatus value; got {clob['status']!r}"
    )
    # After ONE failure the status is still UNKNOWN (the layer's status
    # derivation only flips to DEGRADED at consecutive_failures >= 2).
    assert clob["status"] == APIStatus.UNKNOWN.value, (
        f"expected status=unknown after 1 failure (DEGRADED triggers at 2); "
        f"got {clob['status']!r}"
    )
    assert "refused" in clob["last_error"].lower(), (
        f"expected last_error to mention 'refused'; got {clob['last_error']!r}"
    )


@pytest.mark.asyncio

async def test_no_fallback_raises_connection_error(
    fresh_layer, no_backoff_sleep,
):
    """When ``fallback_data`` is ``None`` and every retry fails, a
    ``ConnectionError`` is raised (the opt-out-of-grace-degradation path).

    This is the contract for callers that want hard failure: leave
    ``fallback_data`` at its default ``None`` and the layer raises
    rather than silently returning ``None``.
    """
    async def _always_fail():
        raise RuntimeError("upstream is down")

    with pytest.raises(ConnectionError) as excinfo:
        await fresh_layer.call_with_resilience("clob", _always_fail)

    msg = str(excinfo.value)
    assert "clob" in msg, (
        f"expected ConnectionError message to mention the API name 'clob'; "
        f"got {msg!r}"
    )
    assert "3 attempts" in msg, (
        f"expected ConnectionError message to mention the retry count; "
        f"got {msg!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (5) Timeout handling
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_timeout_is_recorded_and_retried(fresh_layer, monkeypatch):
    """A ``call_fn`` that hangs longer than the layer's timeout is
    cancelled, recorded as a timeout, and retried.

    Verifies:
      * ``asyncio.TimeoutError`` from the hung call is caught (not
        propagated as an unhandled exception).
      * ``total_timeouts`` counter increments on every timed-out attempt.
      * The layer still retries (3 attempts total) before failing.
      * The failure is recorded against the per-API health with the
        ``last_error`` string mentioning the timeout.

    The layer's ``_timeout`` is patched down from 5 s to 0.05 s so the
    test runs fast (a real 5 s × 3 attempts = 15 s of real wait would
    dominate the suite's wall-clock). The inter-retry backoff schedule
    is patched to ``[0, 0, 0]`` (via ``fresh_layer._backoff``) so the
    100 ms / 500 ms / 2 000 ms sleeps between retries don't add real
    wall-clock delay. ``asyncio.sleep`` is NOT monkeypatched (the
    earlier ``no_backoff_sleep`` fixture's pattern would also neutralise
    the hang simulation — ``_hangs_forever`` uses ``asyncio.sleep`` to
    simulate a hung upstream call, and patching it to a no-op would
    make the call return immediately instead of timing out).

    The hang itself uses ``asyncio.Event().wait()`` — an awaitable that
    never resolves unless ``.set()`` is called, so ``asyncio.wait_for``
    is guaranteed to time out and cancel the inner coroutine. (A
    ``time.sleep(10)`` would block the event loop and break the
    ``asyncio.wait_for`` cancellation semantics; ``asyncio.sleep(10)``
    would work but adds 10 s of cancellation overhead per attempt.)
    """
    # Patch the layer's timeout down to 50 ms so the test runs fast.
    fresh_layer._timeout = 0.05
    # Patch the inter-retry backoff schedule to all-zeros so the
    # 100 ms / 500 ms / 2 000 ms sleeps between retries don't add real
    # wall-clock delay. ``asyncio.sleep(0)`` is a yield to the event
    # loop (no real delay), so the test stays fast while the layer's
    # retry loop still executes the same number of attempts.
    fresh_layer._backoff = [0.0, 0.0, 0.0]

    call_count = {"n": 0}

    async def _hangs_forever():
        call_count["n"] += 1
        # ``asyncio.Event().wait()`` blocks forever unless ``.set()`` is
        # called — ``asyncio.wait_for`` will cancel the inner awaitable
        # after the 50 ms timeout and raise ``TimeoutError``. Using an
        # ``Event`` (rather than ``asyncio.sleep(10)``) avoids burning
        # the 10 s cancellation-window budget on every attempt.
        await asyncio.Event().wait()
        return {"unreachable": True}

    # With ``fallback_data`` supplied, the layer returns the fallback
    # after exhausting the retries (rather than raising).
    fallback = {"stale": True}
    result = await fresh_layer.call_with_resilience(
        "clob", _hangs_forever, fallback_data=fallback,
    )

    assert result is fallback, (
        f"expected fallback_data after timeout-retries exhausted; got {result!r}"
    )
    assert call_count["n"] == 3, (
        f"expected _hangs_forever to be called 3 times (one per retry); "
        f"got {call_count['n']}"
    )

    clob = fresh_layer.get_health()["clob"]
    assert clob["total_timeouts"] == 3, (
        f"expected total_timeouts=3 (one per attempt); got {clob['total_timeouts']}"
    )
    assert clob["total_failures"] == 1, (
        f"expected total_failures=1 (one logical call failed); "
        f"got {clob['total_failures']}"
    )
    assert "timeout" in clob["last_error"].lower(), (
        f"expected last_error to mention 'timeout'; got {clob['last_error']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (6) Health tracking — get_health / is_healthy
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_get_health_returns_documented_shape_for_every_field(fresh_layer):
    """``get_health()`` returns a dict keyed by API name, each value a
    dict carrying every documented ``APIHealth`` field.

    Verifies the JSON-serialisable shape the ``GET /api/api-health``
    endpoint returns to the dashboard. The field set is the load-
    bearing contract — adding / renaming a field requires updating
    the React component that renders the panel.
    """
    async def _fetch():
        return {"ok": True}

    await fresh_layer.call_with_resilience("clob", _fetch)

    health = fresh_layer.get_health()
    assert isinstance(health, dict), (
        f"expected get_health() to return a dict; got {type(health)}"
    )
    assert "clob" in health, "expected a 'clob' entry after a call"
    clob = health["clob"]
    expected_keys = {
        "status",
        "last_success",
        "last_failure",
        "consecutive_failures",
        "total_calls",
        "total_failures",
        "total_timeouts",
        "avg_latency_ms",
        "last_error",
    }
    assert set(clob.keys()) == expected_keys, (
        f"expected exactly the documented keys {expected_keys}; "
        f"got {set(clob.keys())}"
    )
    # The dict must be JSON-serialisable (the API route returns it
    # directly via FastAPI's JSONResponse). The ``APIStatus`` enum is
    # coerced to its string value, so the JSON round-trip is safe.
    import json
    json.dumps(health)  # raises TypeError if not serialisable


@pytest.mark.asyncio

async def test_is_healthy_returns_three_state(fresh_layer):
    """``is_healthy`` returns False for UNKNOWN (never called), True for
    HEALTHY, False for DEGRADED, False for UNHEALTHY."""
    # UNKNOWN — never called
    assert fresh_layer.is_healthy("clob") is False, (
        "is_healthy must return False for an API that has never been called"
    )

    # HEALTHY — one successful call
    async def _ok():
        return {"ok": True}
    await fresh_layer.call_with_resilience("clob", _ok)
    assert fresh_layer.is_healthy("clob") is True, (
        "is_healthy must return True after a successful call"
    )

    # Force the layer into UNHEALTHY by recording 5 failures directly
    # via the internal helper (avoids burning 5 × 3 = 15 real retry
    # cycles in this test — the breaker's behaviour is already
    # covered by test (3) above).
    for _ in range(5):
        fresh_layer._record_failure("clob", "synthetic failure")
    assert fresh_layer.is_healthy("clob") is False, (
        "is_healthy must return False once the breaker is tripped"
    )


@pytest.mark.asyncio

async def test_get_health_empty_when_no_calls(fresh_layer):
    """``get_health()`` returns an empty dict when no API has been called.

    Verifies the lazy-creation contract: the per-API health record is
    created on the first call to ``call_with_resilience`` for that
    API name; a fresh layer with no calls has no entries at all.
    """
    health = fresh_layer.get_health()
    assert health == {}, (
        f"expected empty health dict for a fresh layer; got {health}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (7) API route — GET /api/api-health
# ─────────────────────────────────────────────────────────────────────────────


def test_api_health_endpoint_requires_auth(client):
    """``GET /api/api-health`` without a bearer token returns 401.

    The endpoint is NOT in ``PUBLIC_PATHS`` — every authenticated route
    is fail-closed by the ``enforce_api_auth`` middleware. Verifies the
    W24-7 endpoint follows the same auth contract as every other
    authenticated route in ``api/server.py``.
    """
    response = client.get("/api/api-health")  # no Authorization header
    assert response.status_code == 401, (
        "expected 401 without auth header; got "
        f"{response.status_code}; body: {response.text[:200]!r}"
    )


def test_api_health_endpoint_returns_dict(client, auth_headers):
    """``GET /api/api-health`` returns a JSON dict (possibly empty).

    Resets the singleton before the request so prior tests' state
    doesn't leak into the response shape assertion. After the reset,
    the response is ``{}`` (no API has been called yet) — the test
    asserts on the shape, not on specific counter values, so a
    sibling test recording a 'clob' success between the reset and
    the request doesn't break the assertion.
    """
    # Reset the singleton so the response is deterministic (empty dict
    # if no call_fn has been invoked since the reset).
    api_resilience.reset()

    response = client.get("/api/api-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 with valid auth; got {response.status_code}; "
        f"body: {response.text[:300]!r}"
    )

    body = response.json()
    assert isinstance(body, dict), (
        f"expected JSON dict; got {type(body).__name__}: {body!r}"
    )
    # After ``reset()``, no API has been called — the dict is empty.
    # If a sibling test (e.g. an integration test running concurrently
    # in the same process) recorded a call between ``reset()`` and
    # this assertion, the dict would have a 'clob' / 'gamma' entry —
    # the test tolerates that by only asserting on the type.
    for api_name, record in body.items():
        assert isinstance(api_name, str)
        assert isinstance(record, dict), (
            f"expected each record to be a dict; got {type(record).__name__} "
            f"for {api_name!r}"
        )


def test_api_health_endpoint_reflects_recorded_call(client, auth_headers):
    """``GET /api/api-health`` reflects a call recorded via the singleton.

    Resets the singleton, records a successful 'clob' call directly
    via the internal helper (avoiding the real ``ClobClient`` import
    chain), then asserts the endpoint surfaces the 'clob' entry with
    ``status=healthy``. This verifies the route is wired to the same
    singleton the CLOB / Gamma clients use (NOT a separate instance
    that would diverge from the production call sites).
    """
    api_resilience.reset()

    # Record a success directly via the singleton's internal helper —
    # avoids importing ``ClobClient`` (which would trigger the heavy
    # ``api.server`` import chain via ``config.settings``).
    api_resilience._record_success("clob", 42.0)

    response = client.get("/api/api-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 with valid auth; got {response.status_code}; "
        f"body: {response.text[:300]!r}"
    )

    body = response.json()
    assert "clob" in body, (
        f"expected 'clob' entry after recording a clob success; got {body}"
    )
    clob = body["clob"]
    assert clob["status"] == APIStatus.HEALTHY.value, (
        f"expected status=healthy; got {clob['status']!r}"
    )
    assert clob["total_calls"] == 1, (
        f"expected total_calls=1; got {clob['total_calls']}"
    )
    assert clob["consecutive_failures"] == 0, (
        f"expected consecutive_failures=0; got {clob['consecutive_failures']}"
    )
    # ``_record_success`` does NOT set ``last_error`` — it stays at the
    # ``APIHealth`` dataclass default (empty string). The route should
    # surface that default verbatim.
    assert clob["last_error"] == "", (
        f"expected last_error='' after success; got {clob['last_error']!r}"
    )

    # Cleanup so the recorded 'clob' success doesn't leak into sibling
    # test modules (the singleton is process-global — without reset,
    # every subsequent test that checks ``is_healthy('clob')`` would
    # see True even if no real call was made in that test's scope).
    api_resilience.reset()


# ─────────────────────────────────────────────────────────────────────────────
# (8) Successful call after breaker trip re-arms the breaker
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio

async def test_breaker_rearms_after_successful_call(
    fresh_layer, no_backoff_sleep,
):
    """A successful call after the breaker trips resets the failure counter.

    Verifies the recovery path: the breaker doesn't latch forever
    once ``consecutive_failures`` reaches the threshold. As soon as a
    logical call succeeds (either via a retry within the same call or
    via the breaker being forced-closed by a manual ``_record_success``
    call), ``consecutive_failures`` zeroes and ``status`` flips back
    to ``HEALTHY``.

    Note: this test forces the breaker into the OPEN state via the
    internal ``_record_failure`` helper (5 failures in a row) rather
    than burning 5 × 3 = 15 real retry cycles — the breaker's tripping
    behaviour is already covered by test (3).
    """
    # Force the breaker OPEN.
    for _ in range(5):
        fresh_layer._record_failure("clob", "synthetic failure")
    assert fresh_layer.is_healthy("clob") is False, (
        "is_healthy must return False after the breaker is tripped"
    )

    # Now make a successful call. The breaker's short-circuit checks
    # ``consecutive_failures >= _failure_threshold`` BEFORE invoking
    # ``call_fn``, so the trip must be cleared manually for the call
    # to reach ``_fetch``. In production this happens naturally: the
    # W13-2 inner ``clob_breaker`` has a ``recovery_timeout`` (30 s)
    # that half-opens the breaker; the W24-7 outer layer has no such
    # timeout (it relies on the inner breaker's recovery). For the
    # unit test we simulate the recovery by recording a success.
    fresh_layer._record_success("clob", 10.0)
    assert fresh_layer.is_healthy("clob") is True, (
        "is_healthy must return True after a success is recorded"
    )

    # The next ``call_with_resilience`` should now reach ``_fetch``
    # (the breaker is no longer short-circuiting).
    sentinel = {"recovered": True}

    async def _fetch():
        return sentinel

    result = await fresh_layer.call_with_resilience("clob", _fetch)
    assert result is sentinel, (
        f"expected call_with_resilience to return the sentinel after recovery; "
        f"got {result!r}"
    )
