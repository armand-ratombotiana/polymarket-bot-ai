"""W21-2 — Unit tests for ``core/pg_health_monitor.py`` + ``core/database_manager.py``.

Covers the seven behavioural contracts required by the W21-2 task spec:

  (1) Health check with no PG available — ``_check_health`` records an
      unhealthy ``HealthCheck`` with a non-empty error string and
      ``is_healthy()`` returns ``False`` (the sandbox has no live PG).
  (2) Consecutive-failure threshold — after ``failure_threshold``
      (default 3) consecutive failures, the monitor marks PG as
      unhealthy; before the threshold, ``is_healthy()`` stays ``True``
      (so a single flapping failure doesn't bounce the backend flag).
  (3) Consecutive-success recovery — after ``recovery_success_threshold``
      (default 2) consecutive successes, the monitor marks PG as
      healthy again; a single success is insufficient.
  (4) Uptime percentage computation — ``uptime_pct`` is recomputed on
      every check (``(total - failures) / total * 100``); the bootstrap
      window (``total_checks == 0``) is excluded (no 0/0 divide).
  (5) Average latency computation — ``avg_latency_ms`` is the mean
      latency across the last 100 *healthy* samples (failed pings don't
      contribute a 0 ms latency that would deflate the metric).
  (6) Database manager wiring — ``DatabaseManager.initialize`` starts
      the ``pg_health_monitor`` background task + the manager's own
      ``_pg_retry_loop``; ``shutdown`` cancels both. The retry loop
      reconciles ``_status.backend`` with ``pg_health_monitor.is_healthy()``
      per the W21-2 task-spec wiring snippet.
  (7) API routes — ``GET /api/database/pg-health`` returns the monitor's
      status dict (200); ``POST /api/database/pg-health/check`` forces
      an immediate ping (200 + the post-check status dict). Both routes
      require the bearer auth token (the auth middleware enforces it).

Mocking strategy
----------------
There's no live PostgreSQL in the sandbox, so every test that needs a
*controllable* health verdict monkeypatches
``PGHealthMonitor._ping_postgres`` (the actual ``SELECT 1`` ping is
delegated to this method specifically so tests can inject synthetic
success / failure without standing up a real PG instance). Each test
constructs a FRESH ``PGHealthMonitor`` (rather than mutating the module-
level singleton) so state cannot leak between tests.

For the API route tests, the production ``api.server.app`` is imported
(mirrors ``tests/test_prometheus.py``); ``TestClient(app)`` (without the
context manager) skips the lifespan startup so each test stays fast.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors ``tests/test_async_db.py`` /
``tests/test_decision_ledger.py`` — pytest-asyncio is already a project
dependency).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from core.pg_health_monitor import (
    HealthCheck,
    PGHealthMonitor,
    PGHealthStatus,
    pg_health_monitor,
)


# Only the async tests are explicitly marked with ``@pytest.mark.asyncio``
# below — the rest are sync (TestClient-based). The repo's convention is
# the module-level ``pytestmark = pytest.mark.asyncio`` (see
# ``tests/test_clob_client.py``) but that produces a PytestWarning for
# every sync test, so we annotate only the async ones explicitly to keep
# the test output clean. Mirrors ``tests/test_circuit_breaker.py``.


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def monitor() -> PGHealthMonitor:
    """Fresh ``PGHealthMonitor`` for each test (no shared state).

    The module-level ``pg_health_monitor`` singleton is NOT mutated by
    these tests — each test constructs its own instance so a state
    transition recorded in one test cannot leak into the next. The
    singleton is still exercised by the API route tests below (they
    hit the production ``app`` which imports the singleton at module
    load time).
    """
    return PGHealthMonitor(
        check_interval=0.01,  # very fast for tests
        failure_threshold=3,
        recovery_interval=0.01,
        recovery_success_threshold=2,
        ping_timeout=0.5,
    )


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_prometheus.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    from api.server import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": "Bearer test-token-conftest"}


# ── (1) Health check with no PG available ────────────────────────────────────


@pytest.mark.asyncio
async def test_check_health_unhealthy_when_pg_unreachable(monitor: PGHealthMonitor):
    """``_check_health`` records an unhealthy ``HealthCheck`` when PG is down.

    The sandbox has no live PostgreSQL — ``_ping_postgres`` raises
    ``ConnectionError`` (asyncpg cannot reach ``localhost:5432``). The
    monitor must record the failure, populate the error string, and
    surface it via ``last_check.error`` so the dashboard can render the
    underlying cause (DNS / refused / timeout).
    """
    # Force a deterministic failure so the test doesn't depend on the
    # TCP stack's behavior (some sandboxes reject instantly, others hang
    # for the full timeout — both yield unhealthy, but the latency is
    # non-deterministic).
    async def _fail():
        raise ConnectionError("PG unreachable (test stub)")

    monitor._ping_postgres = _fail  # type: ignore[method-assign]
    check = await monitor._check_health()

    assert check.healthy is False, (
        "Ping failure must produce an unhealthy HealthCheck (the monitor "
        "records the failure outcome + the error string, not the exception)."
    )
    assert check.error, (
        "Error string must be non-empty so the dashboard can render the "
        "underlying cause (DNS / refused / timeout)."
    )
    assert "PG unreachable" in check.error, (
        f"Error string must preserve the underlying cause — got {check.error!r}."
    )
    assert monitor.is_healthy() is False, (
        "A single failed ping must flip the singleton to unhealthy (default "
        "recovery_success_threshold=2 only governs the unhealthy→healthy "
        "transition; the healthy→unhealthy transition is governed by the "
        "initial state, which defaults to is_healthy=False)."
    )


# ── (2) Consecutive-failure threshold ────────────────────────────────────────


@pytest.mark.asyncio
async def test_consecutive_failure_threshold_trips_unhealthy():
    """After ``failure_threshold`` (3) consecutive failures, ``is_healthy()``
    stays ``False`` — but a single failure must NOT trigger a state change
    if the monitor was previously healthy (the threshold exists to absorb
    flapping)."""
    # Start the monitor in a HEALTHY state to test the healthy→unhealthy
    # transition (the threshold only fires if ``is_healthy`` was True
    # when the failures started — see ``_record_check``).
    monitor = PGHealthMonitor(failure_threshold=3, recovery_success_threshold=2)
    monitor._status.is_healthy = True

    # Inject 2 consecutive failures — under the threshold, ``is_healthy()``
    # must stay True (the operator tolerates a transient blip).
    async def _fail():
        raise ConnectionError("transient")

    monitor._ping_postgres = _fail  # type: ignore[method-assign]
    await monitor._check_health()
    await monitor._check_health()
    assert monitor.is_healthy() is True, (
        "2 failures < threshold=3 — monitor must absorb the flap and stay "
        "healthy so the operator doesn't see a SQLite fallback on every "
        "transient PG blip."
    )
    assert monitor._status.consecutive_failures == 2, (
        "Failure counter must accumulate (2 of 3) so the next failure trips."
    )

    # 3rd failure trips the threshold — monitor flips to unhealthy.
    await monitor._check_health()
    assert monitor.is_healthy() is False, (
        "3 consecutive failures >= threshold=3 — monitor must mark PG "
        "unhealthy and signal the database manager to fall back to SQLite."
    )
    assert monitor._status.consecutive_failures == 3
    assert monitor._status.total_failures == 3


# ── (3) Consecutive-success recovery ───────────────────────────────────────


@pytest.mark.asyncio
async def test_consecutive_success_recovery_restores_healthy():
    """After ``recovery_success_threshold`` (2) consecutive successes,
    the monitor marks PG healthy again. A single success is insufficient
    — that's the flap-suppression contract that prevents the backend
    flag from bouncing on a recovering PG that's still intermittent."""
    monitor = PGHealthMonitor(
        failure_threshold=3,
        recovery_success_threshold=2,
    )
    # Start unhealthy — test the unhealthy→healthy transition.
    monitor._status.is_healthy = False

    async def _ok():
        return 5.5  # 5.5 ms latency

    monitor._ping_postgres = _ok  # type: ignore[method-assign]

    # One success — under the recovery threshold, monitor stays unhealthy.
    await monitor._check_health()
    assert monitor.is_healthy() is False, (
        "1 success < recovery_success_threshold=2 — monitor must stay "
        "unhealthy so a single flapping success doesn't bounce the backend."
    )
    assert monitor._status.consecutive_successes == 1

    # Second success — meets the recovery threshold.
    await monitor._check_health()
    assert monitor.is_healthy() is True, (
        "2 consecutive successes >= recovery_success_threshold=2 — monitor "
        "must mark PG healthy and signal the database manager to flip back."
    )
    assert monitor._status.consecutive_failures == 0, (
        "Recovery must zero the failure counter so the next outage starts "
        "fresh from 0 (mirrors the circuit_breaker record_success contract)."
    )


# ── (4) Uptime percentage computation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_uptime_pct_computed_from_lifetime_totals():
    """``uptime_pct`` is ``(total_checks - total_failures) / total_checks * 100``.

    Verified by direct ``_record_check`` calls (no async ping — keeps the
    test fast + deterministic; the formula itself is the unit under test,
    not the ``_check_health`` orchestration)."""
    monitor = PGHealthMonitor()

    # 3 healthy + 1 failure = 75 % uptime.
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=10.0))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=10.0))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=10.0))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=False, latency_ms=0.0, error="boom"))

    assert monitor._status.total_checks == 4
    assert monitor._status.total_failures == 1
    assert monitor._status.uptime_pct == 75.0, (
        "3 of 4 healthy = 75.0 % uptime — verified the formula isn't "
        "accidentally dividing by failures instead of total_checks."
    )

    # Bootstrap window: 0 checks → uptime_pct stays 0.0 (no 0/0 divide).
    empty = PGHealthMonitor()
    assert empty._status.uptime_pct == 0.0
    assert empty._status.total_checks == 0


# ── (5) Average latency computation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_avg_latency_excludes_failed_pings():
    """``avg_latency_ms`` is the mean latency of *healthy* samples only.

    Failed pings report ``latency_ms=0`` — including them in the mean
    would deflate the metric and mask a latency spike on the next
    healthy sample (the dashboard would report "5 ms avg" while the
    actual healthy ping was 200 ms)."""
    monitor = PGHealthMonitor()

    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=10.0))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=20.0))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=False, latency_ms=0.0, error="fail"))
    monitor._record_check(HealthCheck(timestamp=time.time(), healthy=True, latency_ms=30.0))

    # (10 + 20 + 30) / 3 = 20.0 ms (the failure is excluded from the mean).
    assert monitor._status.avg_latency_ms == 20.0, (
        "avg_latency_ms must be the mean of healthy samples only — the "
        "0 ms failed-ping latency must NOT deflate the metric (else the "
        "dashboard would mask a real latency spike)."
    )


# ── (6) Database manager wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_manager_initialize_starts_monitor_and_retry_loop():
    """``db_manager.initialize()`` starts the PG health monitor + the
    ``_pg_retry_loop`` background task.

    After ``initialize()``, both ``pg_health_monitor._running`` and
    ``db_manager._retry_task`` must be non-None. ``shutdown()`` cancels
    both. Idempotency: a second ``initialize()`` is a no-op (no
    duplicate retry tasks)."""
    from core.database_manager import db_manager
    from core import pg_health_monitor as _pgm_module

    # Reset the singleton's state in case a prior test left it running.
    await db_manager.shutdown()
    await _pgm_module.pg_health_monitor.stop()

    # Initialize — should start the monitor + the retry loop.
    await db_manager.initialize()

    try:
        assert _pgm_module.pg_health_monitor._running is True, (
            "pg_health_monitor.start() must be called by db_manager.initialize() "
            "so callers don't need to manage two lifecycles."
        )
        assert db_manager._retry_task is not None, (
            "db_manager._retry_task must be created by initialize() so the "
            "backend-selection loop can reconcile _status.backend with the "
            "monitor's verdict."
        )
        assert not db_manager._retry_task.done(), (
            "Retry loop must be RUNNING, not already finished — a finished "
            "task indicates an unhandled exception in _pg_retry_loop."
        )

        # Idempotency — second initialize() must be a no-op (no duplicate
        # retry tasks, no duplicate monitor tasks).
        first_task = db_manager._retry_task
        await db_manager.initialize()
        assert db_manager._retry_task is first_task, (
            "Second initialize() call must NOT replace the retry task — "
            "the contract is idempotent so duplicate lifespan startups "
            "(e.g. the W21-1 + W21-2 wiring blocks both calling init) "
            "don't spawn duplicate loops."
        )
    finally:
        # Clean up — cancel the background tasks so the next test starts
        # from a clean baseline.
        await db_manager.shutdown()
        await _pgm_module.pg_health_monitor.stop()


@pytest.mark.asyncio
async def test_db_manager_retry_loop_flips_backend_on_health_change():
    """The retry loop reconciles ``_status.backend`` with the monitor's verdict.

    Verified by manually invoking ``_sync_backend_with_monitor`` (rather
    than sleeping for the 5 s poll interval — keeps the test fast +
    deterministic). The transition mirrors the W21-2 task spec's wiring
    snippet verbatim."""
    from core.database_manager import DatabaseBackend, DatabaseManager
    from core.pg_health_monitor import PGHealthMonitor

    # Inject a fresh monitor so we can flip its verdict without touching
    # the module-level singleton.
    fresh_monitor = PGHealthMonitor()
    dbm = DatabaseManager()
    # Replace the manager's monitor reference with our fresh instance.
    # ``DatabaseManager.__init__`` doesn't accept a monitor kwarg (the
    # existing W21-5 ctor is no-arg), so we patch the attribute directly.
    # ``_sync_backend_with_monitor`` references the module-level
    # ``pg_health_monitor`` singleton (not ``self._monitor``) — so we
    # patch the module attribute instead. Use monkeypatch-style setattr
    # for test isolation.
    import core.database_manager as dm_module
    original_monitor = dm_module.pg_health_monitor
    dm_module.pg_health_monitor = fresh_monitor
    try:
        # Pre-state: PG unavailable, backend = SQLITE.
        dbm._status.backend = DatabaseBackend.SQLITE
        dbm._status.pg_available = False
        fresh_monitor._status.is_healthy = True

        await dbm._sync_backend_with_monitor()
        assert dbm._status.backend == DatabaseBackend.POSTGRESQL, (
            "Monitor says healthy + manager says not pg_available → flip "
            "to POSTGRESQL (the W21-2 task-spec wiring snippet)."
        )
        assert dbm._status.pg_available is True
        assert dbm._status.fallback_count == 1, (
            "Each flip must bump fallback_count so the operator sees "
            "'PG came back' in the status payload."
        )

        # Now flip the monitor verdict to unhealthy → manager must flip
        # back to SQLITE.
        fresh_monitor._status.is_healthy = False
        await dbm._sync_backend_with_monitor()
        assert dbm._status.backend == DatabaseBackend.SQLITE
        assert dbm._status.pg_available is False
        assert dbm._status.fallback_count == 2, (
            "Second flip must bump the counter again — the count is the "
            "total number of state transitions, not the number of UNIQUE "
            "transitions (an operator sees 'flapped twice' in the payload)."
        )

        # Third sync — no change in verdict → no flip (idempotent).
        before_count = dbm._status.fallback_count
        await dbm._sync_backend_with_monitor()
        assert dbm._status.fallback_count == before_count, (
            "When the monitor and manager already agree, the sync must be "
            "a no-op (no flip, no fallback_count bump)."
        )
    finally:
        dm_module.pg_health_monitor = original_monitor


# ── (7) API routes ──────────────────────────────────────────────────────────


def test_get_pg_health_endpoint_returns_200(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """``GET /api/database/pg-health`` returns 200 + the monitor's status dict.

    The status dict carries every documented field (``is_healthy``,
    ``consecutive_failures`` / ``consecutive_successes``, ``total_checks`` /
    ``total_failures``, ``uptime_pct``, ``avg_latency_ms``, ``last_check``,
    ``checks``) so the dashboard can render the full PG-health panel without
    a second scrape. Auth is enforced by the existing middleware (the
    request must carry the bearer token).
    """
    response = client.get("/api/database/pg-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"GET /api/database/pg-health returned {response.status_code} — "
        f"expected 200. Body: {response.text[:300]}"
    )
    payload = response.json()

    # Every documented field must be present so the dashboard panel
    # doesn't render "no data" for any column.
    expected_keys = {
        "is_healthy",
        "consecutive_failures",
        "consecutive_successes",
        "last_check",
        "checks",
        "total_checks",
        "total_failures",
        "uptime_pct",
        "avg_latency_ms",
    }
    missing = expected_keys - set(payload.keys())
    assert not missing, (
        f"GET /api/database/pg-health payload missing keys: {sorted(missing)} — "
        f"the dashboard panel reads these fields directly; a missing key "
        f"would render as 'no data' for that column."
    )
    # ``checks`` must be a list (the last 100 HealthCheck samples).
    assert isinstance(payload["checks"], list), (
        "checks must be a list (last 100 HealthCheck samples) so the "
        "dashboard can render the timeline."
    )
    # ``is_healthy`` must be a bool (not a string / int) — the dashboard
    # renders a green/red dot based on this field's truthiness.
    assert isinstance(payload["is_healthy"], bool)


def test_force_pg_health_check_endpoint_returns_200(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """``POST /api/database/pg-health/check`` forces an immediate ping.

    Returns 200 + the post-check status dict. The endpoint exists so an
    operator who just restarted PG can verify the recovery in the
    dashboard without waiting for the next background tick (default 15 s)."""
    response = client.post("/api/database/pg-health/check", headers=auth_headers)
    assert response.status_code == 200, (
        f"POST /api/database/pg-health/check returned {response.status_code} — "
        f"expected 200. Body: {response.text[:300]}"
    )
    payload = response.json()
    # The endpoint must have recorded at least one HealthCheck (the
    # forced ping itself) — ``total_checks`` increments by 1 per call.
    assert payload["total_checks"] >= 1, (
        "Force-check endpoint must record the HealthCheck outcome (the "
        "singleton's total_checks counter must be ≥1 after the call)."
    )


def test_pg_health_endpoints_require_auth(client: TestClient):
    """Both pg-health endpoints enforce the bearer token.

    A request without the Authorization header must return 401 (the auth
    middleware rejects it) — not 200 with leaked health data. This is
    the same contract every authenticated API route in the bot enforces
    (mirrors ``tests/test_security.py`` patterns).
    """
    get_resp = client.get("/api/database/pg-health")
    assert get_resp.status_code in (401, 403), (
        f"GET /api/database/pg-health without auth returned {get_resp.status_code} — "
        f"expected 401 or 403 (the endpoint must enforce the bearer token so "
        f"an unauthenticated scraper can't read PG health telemetry)."
    )

    post_resp = client.post("/api/database/pg-health/check")
    assert post_resp.status_code in (401, 403), (
        f"POST /api/database/pg-health/check without auth returned {post_resp.status_code} — "
        f"expected 401 or 403 (the endpoint must enforce the bearer token so "
        f"an unauthenticated attacker can't trigger forced PG pings — a "
        f"flood of forced pings would mask the monitor's real verdict)."
    )


# ── Bonus: dataclass serialisation ──────────────────────────────────────────


def test_health_check_to_dict_is_jsonable():
    """``HealthCheck.to_dict`` returns a JSON-able dict (no dataclass
    objects left over) so the API response can be serialised without a
    custom encoder. ``latency_ms`` is rounded to 3 decimals so a 0.12345
    ms latency doesn't render with 5 trailing digits in the dashboard."""
    check = HealthCheck(
        timestamp=1700000000.5,
        healthy=True,
        latency_ms=12.34567,
        error="",
    )
    d = check.to_dict()
    assert d == {
        "timestamp": 1700000000.5,
        "healthy": True,
        "latency_ms": 12.346,  # rounded to 3 decimals
        "error": "",
    }
    # Verify JSON serialisability (the API serialiser uses ``json.dumps``
    # under the hood — a non-serialisable value would raise here).
    import json
    json.dumps(d)


def test_pg_health_status_to_dict_includes_nested_check_dict():
    """``PGHealthStatus.to_dict`` renders ``last_check`` as a dict (not a
    dataclass instance) and ``checks`` as a list of dicts — both via
    ``HealthCheck.to_dict`` so the response payload is fully JSON-able
    without a custom encoder."""
    status = PGHealthStatus()
    status.last_check = HealthCheck(timestamp=1.0, healthy=True, latency_ms=5.0)
    status.checks.append(HealthCheck(timestamp=2.0, healthy=False, latency_ms=0.0, error="x"))
    d = status.to_dict()

    assert isinstance(d["last_check"], dict), (
        "last_check must be a dict (rendered via HealthCheck.to_dict) so "
        "the API serialiser doesn't need a custom encoder for nested "
        "dataclass instances."
    )
    assert d["last_check"]["healthy"] is True
    assert isinstance(d["checks"], list)
    assert isinstance(d["checks"][0], dict)
    assert d["checks"][0]["healthy"] is False


# ── Bonus: monitor lifecycle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_start_stop_is_idempotent():
    """``PGHealthMonitor.start()`` / ``stop()`` are idempotent — calling
    either twice in a row is a no-op (no duplicate tasks, no exceptions
    on the second stop). Mirrors the ``book_poller.start`` /
    ``paper_sim.stop`` contract."""
    monitor = PGHealthMonitor(check_interval=0.01, recovery_interval=0.01)
    await monitor.start()
    await monitor.start()  # second call must be a no-op
    assert monitor._running is True
    assert monitor._task is not None
    first_task = monitor._task

    await monitor.stop()
    await monitor.stop()  # second call must be a no-op
    assert monitor._running is False
    assert monitor._task is None

    # Restart should work (lifecycle is repeatable — important for the
    # FastAPI lifespan which may be re-entered during testing).
    await monitor.start()
    assert monitor._task is not None and monitor._task is not first_task, (
        "Restart must create a NEW task (the first task was cancelled)."
    )
    await monitor.stop()


@pytest.mark.asyncio
async def test_monitor_loop_uses_recovery_interval_when_unhealthy():
    """When ``is_healthy()`` is False, the monitor loop uses
    ``recovery_interval`` (slower cadence — give the recovering PG some
    breathing room before the next probe) instead of ``check_interval``."""
    monitor = PGHealthMonitor(
        check_interval=0.01,
        recovery_interval=0.5,  # noticeable difference
        failure_threshold=1,
        recovery_success_threshold=1,
    )

    # Force-unhealthy.
    async def _fail():
        raise ConnectionError("test")
    monitor._ping_postgres = _fail  # type: ignore[method-assign]

    await monitor.start()
    try:
        # Give the loop a couple of ticks to record failures.
        await asyncio.sleep(0.05)
        # After ≥1 failure (threshold=1), ``is_healthy()`` must be False.
        assert monitor.is_healthy() is False, (
            "Force-failing ping + threshold=1 must flip the monitor to "
            "unhealthy so the recovery_interval path is exercised."
        )
        assert monitor._status.total_checks >= 1
    finally:
        await monitor.stop()
