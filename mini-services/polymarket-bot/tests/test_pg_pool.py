"""W21-8 — Unit tests for ``core/pg_pool.py``.

Covers the seven behaviour contracts required by the task spec:

  (1) ``initialize()`` fails gracefully (returns ``False``, no exception)
      when ``asyncpg.create_pool`` cannot reach a backend — the pool
      must NOT crash the caller (mirrors the lazy-init pattern in
      ``core.timescale_db`` / ``core.db_pool`` so a missing PG backend
      doesn't break FastAPI startup).
  (2) ``initialize()`` succeeds and records ``total_connections`` when
      ``asyncpg.create_pool`` returns a pool — and resets the
      ``_consecutive_failures`` counter + clears the circuit-open flag
      so a recovery after a prior outage starts from a clean baseline.
  (3) ``execute()`` retries up to 3 attempts on transient failures and
      raises the last error after exhausting retries — and increments
      the ``failed_queries`` counter + records ``last_error`` /
      ``last_error_time``.
  (4) The circuit breaker trips after ``_max_failures`` (5) consecutive
      ``execute()`` failures — ``_circuit_open`` becomes ``True`` and
      the next ``execute()`` raises ``ConnectionError("Circuit breaker
      is open — PG unavailable")`` WITHOUT calling into the pool.
  (5) The circuit breaker recovers after ``_recovery_timeout`` (30 s)
      — once the elapsed-since-tripped window passes, the next
      ``execute()`` call clears the open flag, resets the failure
      counter, and tries the pool again (verified by manipulating
      ``_circuit_opened_at`` directly rather than sleeping 30 s).
  (6) ``health_check()`` returns ``False`` when the pool is not
      initialized, when the circuit is open, or when the underlying
      ``SELECT 1`` raises; returns ``True`` on success.
  (7) ``get_stats()`` returns the full snapshot — connection counts,
      query counters, rolling avg query time, last error, circuit
      breaker state, threshold, and recovery timeout.

Mocking strategy
-----------------
There is no live PostgreSQL in the sandbox, so every test patches
``asyncpg.create_pool`` (the symbol the pool imports lazily inside
``initialize()``). A custom ``FakeAsyncpgPool`` simulates asyncpg's
``Pool.acquire()`` async-context-manager contract — its ``acquire()``
returns a ``_FakeAcquireContext`` whose ``__aenter__`` returns a
``_FakeConnection`` whose ``fetch`` / ``fetchval`` behaviour the test
configures via ``on_fetch`` / ``on_fetchval`` callbacks. This exercises
the same code path production uses (``async with self._pool.acquire()
as conn: await conn.fetch(...)``) without ever opening a real socket.

A module-level autouse fixture resets the module-level singleton
``pg_pool`` before each test so state cannot leak between tests. Each
test constructs its own ``PGConnectionPool()`` instance (rather than
mutating the singleton) so the singleton stays pristine even if a
future sibling test module exercises it.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors
``tests/test_async_db.py`` / ``tests/test_decision_ledger.py`` — the
repo's ``pytest.ini`` cannot be edited per the additive-files
constraint).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pg_pool import PGConnectionPool, PoolStats, pg_pool as _singleton_pg_pool

# Apply ``@pytest.mark.asyncio`` explicitly to each ``async def test_...``
# below — NOT a module-level ``pytestmark`` declaration. This file mixes
# sync and async tests; a module-level ``pytestmark = pytest.mark.asyncio``
# would emit a PytestWarning for every sync test ("test is marked with
# '@pytest.mark.asyncio' but it is not an async function"). Annotating only
# the async ones keeps the test output clean — mirrors the pattern in
# ``tests/test_circuit_breaker.py``.


# ── Fixture: reset the module-level singleton before each test ──────────────
@pytest.fixture(autouse=True)
def _reset_singleton_pg_pool():
    """Reset the module-level ``pg_pool`` singleton BEFORE each test.

    ``api/server.py`` imports ``pg_pool`` from ``core.pg_pool`` (via the
    ``GET /api/database/pool-stats`` route's local import). Without
    this reset, a test that exercised the singleton's ``initialize()``
    (or any of its ``execute()`` / ``health_check()`` paths) would
    leave the singleton in a non-baseline state — pool attached, stats
    counters non-zero, circuit possibly open — and the next test that
    inspected the singleton via ``get_stats()`` would see stale state.

    The reset is identical to constructing a fresh ``PGConnectionPool``
    against the default DATABASE_URL but DOES NOT replace the singleton
    object (other modules' ``from core.pg_pool import pg_pool`` binding
    keeps working). Instead, it pokes the singleton's private fields
    back to the post-ctor baseline so every test starts from a clean
    pool.

    Each individual test constructs its OWN ``PGConnectionPool()``
    instance via the ``pool`` fixture below — the singleton reset is
    purely a defensive guard against cross-test pollution.
    """
    _singleton_pg_pool._pool = None
    _singleton_pg_pool._stats = PoolStats()
    _singleton_pg_pool._consecutive_failures = 0
    _singleton_pg_pool._circuit_open = False
    _singleton_pg_pool._circuit_opened_at = 0
    yield
    # No post-test teardown: the pre-test reset above is what fixes any
    # cross-test pollution (mirrors the conftest autouse pattern).


# ── Fixture: a fresh PGConnectionPool (NOT the singleton) ──────────────────
@pytest.fixture
def pool() -> PGConnectionPool:
    """Return a fresh ``PGConnectionPool`` instance per test.

    Uses an explicit, invalid-by-default DATABASE_URL so a test that
    forgets to patch ``asyncpg.create_pool`` fails fast with a
    connection error (rather than silently trying to reach a real
    PostgreSQL and timing out for 5 s per attempt).
    """
    p = PGConnectionPool(
        database_url="postgresql://test:test@127.0.0.1:1/none",  # unreachable
        min_size=2,
        max_size=10,
    )
    # Make tests fast: shrink the recovery timeout to 0.05 s so the
    # circuit-recovery test can sleep without slowing the suite.
    p._recovery_timeout = 0.05
    return p


# ── Helpers: simulate asyncpg.Pool / asyncpg.Connection ─────────────────────


class _FakeConnection:
    """Simulates ``asyncpg.Connection``'s ``fetch`` / ``fetchval`` calls.

    The test configures ``on_fetch`` / ``on_fetchval`` callbacks to
    either return a value (success) or raise (simulates a transient
    backend failure). Defaults to returning ``[]`` / ``1`` so a test
    that doesn't care about the result still gets a clean success.
    """

    def __init__(
        self,
        on_fetch: Optional[Callable[[str, tuple], Any]] = None,
        on_fetchval: Optional[Callable[[str, tuple], Any]] = None,
    ) -> None:
        self._on_fetch = on_fetch
        self._on_fetchval = on_fetchval
        self.last_query: Optional[str] = None
        self.last_params: Optional[tuple] = None

    async def fetch(self, query: str, *params) -> list:
        self.last_query = query
        self.last_params = params
        if self._on_fetch is not None:
            return self._on_fetch(query, params)
        return []

    async def fetchval(self, query: str, *params) -> Any:
        self.last_query = query
        self.last_params = params
        if self._on_fetchval is not None:
            return self._on_fetchval(query, params)
        return 1


class _FakeAcquireContext:
    """Async-context-manager returned by ``FakeAsyncpgPool.acquire()``.

    Matches asyncpg's ``Pool.acquire()`` contract: ``async with
    pool.acquire() as conn: ...``.
    """

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeAsyncpgPool:
    """Simulates ``asyncpg.Pool`` for the ``execute()`` / ``health_check()`` paths.

    The test sets ``on_acquire`` to either return a ``_FakeConnection``
    (success) or raise an exception (simulates a transient backend
    failure mid-acquire). The ``close`` coroutine is recorded so the
    shutdown test can assert the pool was actually closed.
    """

    def __init__(
        self,
        conn: Optional[_FakeConnection] = None,
        on_acquire: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._conn = conn or _FakeConnection()
        self._on_acquire = on_acquire
        self.closed = False
        self.size = 2

    def acquire(self) -> _FakeAcquireContext:
        if self._on_acquire is not None:
            # ``on_acquire`` may raise to simulate a transient acquire
            # failure. The raise happens INSIDE acquire() — mirroring
            # asyncpg's behaviour when the pool is exhausted.
            result = self._on_acquire()
            # ``on_acquire`` may also return a different connection for
            # per-call flexibility.
            if isinstance(result, _FakeConnection):
                return _FakeAcquireContext(result)
        return _FakeAcquireContext(self._conn)

    def get_size(self) -> int:
        return self.size

    async def close(self) -> None:
        self.closed = True


# ── (1) initialize() fails gracefully when PG unavailable ──────────────────


@pytest.mark.asyncio
async def test_initialize_returns_false_when_create_pool_raises(pool):
    """``initialize()`` returns ``False`` and does NOT raise when
    ``asyncpg.create_pool`` cannot reach the backend.

    Production contract: the pool must NOT crash FastAPI startup when
    PostgreSQL is unavailable (mirrors the lazy-init pattern in
    ``core.timescale_db`` — a missing PG must surface as a degraded
    service, not a 500 on /health).
    """
    with patch("asyncpg.create_pool", new=AsyncMock(side_effect=OSError("Connection refused"))):
        result = await pool.initialize()
    assert result is False
    assert pool._pool is None
    # First failed init increments the failure counter (so a sustained
    # init outage trips the circuit breaker after _max_failures).
    assert pool._consecutive_failures == 1
    # But the circuit isn't tripped yet (need 5 consecutive failures).
    assert pool._circuit_open is False


@pytest.mark.asyncio
async def test_initialize_failure_does_not_raise_via_wait_for_timeout(pool):
    """``initialize()`` returns ``False`` when ``asyncpg.create_pool``
    hangs past the 5 s ``wait_for`` timeout.

    Patches ``create_pool`` to a coroutine that sleeps longer than the
    5 s ``asyncio.wait_for`` window so ``asyncio.TimeoutError`` is
    raised inside ``initialize()`` and caught — returned as ``False``
    rather than propagating to the caller.
    """
    async def _slow_create_pool(*_args, **_kwargs):
        await asyncio.sleep(10)  # exceeds the 5.0 s wait_for window

    with patch("asyncpg.create_pool", new=_slow_create_pool):
        # Speed up: shrink the wait_for timeout via patching
        # ``asyncio.wait_for`` isn't trivial (it's used elsewhere too);
        # instead just verify the function doesn't raise on OSError.
        result = await pool.initialize()
    assert result is False


# ── (2) initialize() succeeds and records total_connections ────────────────


@pytest.mark.asyncio
async def test_initialize_succeeds_and_records_connections(pool):
    """``initialize()`` returns ``True`` and records
    ``total_connections`` = ``min_size`` on success.

    Also verifies that the failure counter + circuit-open flag are
    cleared on a successful init (so a recovery after a prior outage
    starts from a clean baseline).
    """
    fake_pool = FakeAsyncpgPool()

    # Prime the pool's failure state — a successful init must clear it.
    pool._consecutive_failures = 4
    pool._circuit_open = True

    with patch("asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)):
        result = await pool.initialize()

    assert result is True
    assert pool._pool is fake_pool
    assert pool._stats.total_connections == pool._min_size
    assert pool._consecutive_failures == 0
    assert pool._circuit_open is False


# ── (3) execute() retries up to 3 attempts on transient failures ────────────


@pytest.mark.asyncio
async def test_execute_succeeds_on_first_attempt(pool):
    """A successful ``execute()`` returns the fetch result and
    increments ``total_queries`` without touching ``failed_queries``."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: [{"x": 1}, {"x": 2}])
    fake_pool = FakeAsyncpgPool(conn=fake_conn)
    pool._pool = fake_pool  # skip initialize()

    result = await pool.execute("SELECT $1::int AS x", 1)

    assert result == [{"x": 1}, {"x": 2}]
    assert pool._stats.total_queries == 1
    assert pool._stats.failed_queries == 0
    assert pool._consecutive_failures == 0
    # avg_query_time_ms is set on the first successful query (non-zero).
    assert pool._stats.avg_query_time_ms > 0


@pytest.mark.asyncio
async def test_execute_retries_3_attempts_and_raises_after_exhausting(pool):
    """When every retry fails, ``execute()`` raises the LAST error
    after 3 attempts and increments ``failed_queries``.

    Verifies the retry count by counting how many times the mocked
    ``fetch`` raises — must be exactly 3 (the loop bound). Backoff
    delays between retries are patched to 0 to keep the test fast.
    """
    call_count = {"n": 0}

    def _always_fail(_q, _p):
        call_count["n"] += 1
        raise OSError("connection reset")

    fake_conn = _FakeConnection(on_fetch=_always_fail)
    fake_pool = FakeAsyncpgPool(conn=fake_conn)
    pool._pool = fake_pool

    # Patch asyncio.sleep so the test doesn't wait 100 ms + 500 ms.
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with pytest.raises(OSError, match="connection reset"):
            await pool.execute("SELECT 1")

    assert call_count["n"] == 3  # 3 attempts total (the loop bound)
    assert pool._stats.failed_queries == 1
    assert pool._stats.total_queries == 0
    assert pool._stats.last_error == "connection reset"
    assert pool._stats.last_error_time > 0
    # _consecutive_failures increments by 1 per failed execute() call
    # (NOT per retry — the retry loop is one logical query).
    assert pool._consecutive_failures == 1


@pytest.mark.asyncio
async def test_execute_retries_then_succeeds_on_second_attempt(pool):
    """A transient failure on attempt 1 followed by success on attempt 2
    returns the result — verifies the retry loop's early-return path.

    Also verifies ``failed_queries`` is NOT incremented (the overall
    operation succeeded, only some retries failed).
    """
    call_count = {"n": 0}

    def _fail_then_succeed(_q, _p):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("transient blip")
        return [{"ok": True}]

    fake_conn = _FakeConnection(on_fetch=_fail_then_succeed)
    fake_pool = FakeAsyncpgPool(conn=fake_conn)
    pool._pool = fake_pool

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        result = await pool.execute("SELECT 1")

    assert result == [{"ok": True}]
    assert call_count["n"] == 2  # failed once, succeeded on retry
    assert pool._stats.total_queries == 1
    assert pool._stats.failed_queries == 0
    # Success resets the consecutive-failures counter (so a sustained
    # outage's count doesn't leak into the next isolated blip).
    assert pool._consecutive_failures == 0


@pytest.mark.asyncio
async def test_execute_lazy_initializes_pool_when_uninitialized(pool):
    """When ``execute()`` is called on an uninitialized pool, it
    auto-initializes via ``initialize()`` — production contract: the
    pool is lazy so callers can ``execute()`` without first awaiting
    ``initialize()``.

    The lazy init must succeed; if it fails, ``execute()`` raises
    ``ConnectionError("PG pool not available")`` (no infinite retry
    on the init path).
    """
    fake_pool = FakeAsyncpgPool(conn=_FakeConnection(on_fetch=lambda q, p: []))

    with patch("asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)):
        result = await pool.execute("SELECT 1")

    assert result == []
    assert pool._pool is fake_pool  # was set by initialize()
    assert pool._stats.total_queries == 1


@pytest.mark.asyncio
async def test_execute_raises_connection_error_when_init_fails(pool):
    """When the lazy-init path fails, ``execute()`` raises
    ``ConnectionError`` rather than retrying the init infinitely."""
    with patch("asyncpg.create_pool", new=AsyncMock(side_effect=OSError("refused"))):
        with pytest.raises(ConnectionError, match="PG pool not available"):
            await pool.execute("SELECT 1")
    assert pool._stats.total_queries == 0


# ── (4) Circuit breaker trips after max consecutive failures ────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_max_failures(pool):
    """After ``_max_failures`` (5) consecutive ``execute()`` failures,
    the circuit breaker trips — ``_circuit_open`` becomes ``True`` and
    ``_circuit_opened_at`` is set to ``time.time()``."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: (_ for _ in ()).throw(OSError("nope")))
    pool._pool = FakeAsyncpgPool(conn=fake_conn)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        # Each execute() call does 3 retries (all failing) and
        # increments _consecutive_failures by 1.
        for i in range(pool._max_failures):
            with pytest.raises(OSError):
                await pool.execute("SELECT 1")
            # Circuit should trip exactly when failures hit the threshold.
            if i < pool._max_failures - 1:
                assert pool._circuit_open is False, (
                    f"circuit should NOT be open after {i + 1} failures"
                )

    assert pool._consecutive_failures == pool._max_failures
    assert pool._circuit_open is True
    assert pool._circuit_opened_at > 0


@pytest.mark.asyncio
async def test_execute_raises_connection_error_when_circuit_open(pool):
    """When the circuit is OPEN, ``execute()`` raises
    ``ConnectionError("Circuit breaker is open")`` WITHOUT calling into
    the underlying pool — fail-fast to prevent cascading failures."""
    pool._circuit_open = True
    pool._circuit_opened_at = time.time()  # just-tripped

    # The pool should NOT be acquired at all — set _pool to a fake whose
    # acquire would explode if called, to prove the open-circuit short-
    # circuit happened first.
    sentinel = MagicMock()
    sentinel.acquire.side_effect = AssertionError("pool.acquire must NOT be called when circuit is open")
    pool._pool = sentinel

    with pytest.raises(ConnectionError, match="Circuit breaker is open"):
        await pool.execute("SELECT 1")

    sentinel.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_failure_count_contributes_to_circuit_trip(pool):
    """Failed ``initialize()`` calls also count toward the circuit-
    breaker threshold — a sustained PG outage during init (not just
    during execute) trips the breaker.

    This is why ``initialize()`` increments ``_consecutive_failures``
    on failure (mirrors the per-execute path).
    """
    with patch("asyncpg.create_pool", new=AsyncMock(side_effect=OSError("refused"))):
        for i in range(pool._max_failures):
            result = await pool.initialize()
            assert result is False
            if i < pool._max_failures - 1:
                assert pool._circuit_open is False

    assert pool._consecutive_failures == pool._max_failures
    assert pool._circuit_open is True


# ── (5) Circuit breaker recovery after timeout ──────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_after_timeout(pool):
    """After ``_recovery_timeout`` elapses since the breaker tripped,
    the next ``execute()`` call clears the open flag and retries the
    pool — production contract: the breaker self-heals rather than
    requiring an external reset."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: [{"recovered": True}])
    fake_pool = FakeAsyncpgPool(conn=fake_conn)
    pool._pool = fake_pool

    # Trip the breaker.
    pool._circuit_open = True
    pool._circuit_opened_at = time.time() - pool._recovery_timeout - 0.01

    # The next execute() should: detect open, see the timeout has
    # elapsed, clear the open flag, retry the pool, succeed.
    result = await pool.execute("SELECT 1")

    assert result == [{"recovered": True}]
    assert pool._circuit_open is False
    assert pool._consecutive_failures == 0  # reset on recovery entry
    assert pool._stats.total_queries == 1


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_recover_before_timeout(pool):
    """When ``_circuit_opened_at`` is too recent (within the recovery
    window), ``execute()`` still raises ``ConnectionError`` — verifies
    the recovery timeout actually gates recovery (no false recovery)."""
    pool._circuit_open = True
    pool._circuit_opened_at = time.time()  # just tripped, 0 s elapsed

    with pytest.raises(ConnectionError, match="Circuit breaker is open"):
        await pool.execute("SELECT 1")


@pytest.mark.asyncio
async def test_circuit_breaker_recovery_re_trips_on_continued_failure(pool):
    """When the breaker recovers after the timeout but the very next
    ``execute()`` still fails, the failure counter increments again
    (so a sustained outage re-trips the breaker after another
    ``_max_failures`` failures, NOT immediately)."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: (_ for _ in ()).throw(OSError("still down")))
    pool._pool = FakeAsyncpgPool(conn=fake_conn)

    # Trip + recover.
    pool._circuit_open = True
    pool._circuit_opened_at = time.time() - pool._recovery_timeout - 0.01

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with pytest.raises(OSError):
            await pool.execute("SELECT 1")

    # Recovery cleared the open flag at entry; the failed execute
    # incremented the counter (but did NOT re-trip — needs 5 in a row).
    assert pool._circuit_open is False
    assert pool._consecutive_failures == 1
    assert pool._stats.failed_queries == 1


# ── (6) health_check() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_returns_false_when_pool_uninitialized(pool):
    """``health_check()`` returns ``False`` when no pool exists."""
    assert await pool.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_false_when_circuit_open(pool):
    """``health_check()`` short-circuits to ``False`` when the circuit
    is OPEN (no point probing — the breaker has already decided the
    backend is unhealthy)."""
    pool._circuit_open = True
    pool._pool = FakeAsyncpgPool(conn=_FakeConnection(on_fetchval=lambda q, p: 1))
    assert await pool.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_true_on_select_1(pool):
    """``health_check()`` returns ``True`` when the underlying
    ``SELECT 1`` returns ``1`` — the canonical PG liveness probe."""
    fake_conn = _FakeConnection(on_fetchval=lambda q, p: 1)
    pool._pool = FakeAsyncpgPool(conn=fake_conn)
    assert await pool.health_check() is True
    # The probe query is exactly ``SELECT 1``.
    assert fake_conn.last_query == "SELECT 1"


@pytest.mark.asyncio
async def test_health_check_returns_false_when_fetchval_raises(pool):
    """``health_check()`` returns ``False`` (and does NOT raise) when
    the underlying ``SELECT 1`` raises — simulates a backend that
    accepted the connection but errors mid-query (e.g. PG restarted
    between acquire and fetchval)."""
    fake_conn = _FakeConnection(on_fetchval=lambda q, p: (_ for _ in ()).throw(OSError("connection lost")))
    pool._pool = FakeAsyncpgPool(conn=fake_conn)
    assert await pool.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_false_on_non_unit_result(pool):
    """``health_check()`` returns ``False`` when ``SELECT 1`` returns a
    value other than ``1`` — defensive: a misbehaving proxy / mock that
    returns a different value should NOT be reported as healthy."""
    fake_conn = _FakeConnection(on_fetchval=lambda q, p: 0)
    pool._pool = FakeAsyncpgPool(conn=fake_conn)
    assert await pool.health_check() is False


# ── (7) get_stats() / PoolStats ─────────────────────────────────────────────


def test_get_stats_returns_zero_state_on_fresh_pool(pool):
    """A fresh ``PGConnectionPool()`` returns the documented zero-state
    snapshot via ``get_stats()`` — every counter at 0, circuit closed,
    threshold + recovery timeout populated from config."""
    stats = pool.get_stats()
    assert stats["total_connections"] == 0
    assert stats["active_connections"] == 0
    assert stats["idle_connections"] == 0
    assert stats["total_queries"] == 0
    assert stats["failed_queries"] == 0
    assert stats["avg_query_time_ms"] == 0.0
    assert stats["last_error"] == ""
    assert stats["last_error_time"] == 0
    assert stats["circuit_open"] is False
    assert stats["consecutive_failures"] == 0
    assert stats["circuit_threshold"] == pool._max_failures
    assert stats["recovery_timeout"] == pool._recovery_timeout


def test_pool_stats_dataclass_to_dict_has_all_fields():
    """``PoolStats.to_dict()`` returns every documented field — pinned
    so a future refactor that dropped a field would surface as a test
    failure rather than a silent dashboard regression."""
    s = PoolStats(
        total_connections=4,
        active_connections=1,
        idle_connections=3,
        total_queries=100,
        failed_queries=2,
        avg_query_time_ms=12.5,
        last_error="boom",
        last_error_time=1700000000.0,
    )
    d = s.to_dict()
    assert d == {
        "total_connections": 4,
        "active_connections": 1,
        "idle_connections": 3,
        "total_queries": 100,
        "failed_queries": 2,
        "avg_query_time_ms": 12.5,
        "last_error": "boom",
        "last_error_time": 1700000000.0,
    }


def test_update_avg_query_time_ema(pool):
    """``_update_avg_query_time`` uses an exponential moving average
    (α = 0.1) — the first sample seeds the average, subsequent samples
    converge toward the new value rather than jumping to it."""
    pool._stats.avg_query_time_ms = 0.0
    pool._update_avg_query_time(100.0)
    assert pool._stats.avg_query_time_ms == 100.0  # first sample seeds

    pool._update_avg_query_time(200.0)
    # EMA: 0.9 * 100 + 0.1 * 200 = 110
    assert pool._stats.avg_query_time_ms == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_get_stats_reflects_state_after_successful_query(pool):
    """After a successful query, ``get_stats()`` reflects the updated
    counters — total_queries=1, total_connections=min_size, avg_query_time_ms>0."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: [])
    fake_pool = FakeAsyncpgPool(conn=fake_conn)
    pool._pool = fake_pool
    pool._stats.total_connections = pool._min_size  # as initialize() would

    await pool.execute("SELECT 1")

    stats = pool.get_stats()
    assert stats["total_queries"] == 1
    assert stats["failed_queries"] == 0
    assert stats["total_connections"] == pool._min_size
    assert stats["active_connections"] == fake_pool.size  # from get_size()
    assert stats["avg_query_time_ms"] > 0


@pytest.mark.asyncio
async def test_get_stats_reflects_failure_after_failed_query(pool):
    """After a failed query (3 retries exhausted), ``get_stats()``
    reflects ``failed_queries=1``, ``last_error`` populated, and
    ``last_error_time`` > 0."""
    fake_conn = _FakeConnection(on_fetch=lambda q, p: (_ for _ in ()).throw(RuntimeError("boom")))
    pool._pool = FakeAsyncpgPool(conn=fake_conn)

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError):
            await pool.execute("SELECT 1")

    stats = pool.get_stats()
    assert stats["total_queries"] == 0
    assert stats["failed_queries"] == 1
    assert stats["last_error"] == "boom"
    assert stats["last_error_time"] > 0
    assert stats["consecutive_failures"] == 1


# ── close() / graceful shutdown ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_underlying_pool_and_clears_reference(pool):
    """``close()`` calls ``self._pool.close()`` and clears the
    ``_pool`` reference — graceful shutdown contract.

    A second ``close()`` call is a no-op (idempotent — production
    contract: FastAPI shutdown handler may call close() defensively
    without checking if a prior shutdown already closed it)."""
    fake_pool = FakeAsyncpgPool(conn=_FakeConnection())
    pool._pool = fake_pool

    await pool.close()
    assert fake_pool.closed is True
    assert pool._pool is None

    # Second close is a no-op (no error, no AttributeError on None).
    await pool.close()
    assert pool._pool is None


@pytest.mark.asyncio
async def test_close_is_safe_when_pool_never_initialized(pool):
    """``close()`` on a never-initialized pool is a no-op — defensive
    against a FastAPI shutdown handler that always calls close()."""
    assert pool._pool is None
    await pool.close()  # must NOT raise
    assert pool._pool is None


# ── Singleton sanity ────────────────────────────────────────────────────────


def test_module_singleton_is_pg_connection_pool():
    """The module-level ``pg_pool`` is a ``PGConnectionPool`` instance.

    Pinned so a future refactor that turned the singleton into a
    factory function would surface as a test failure rather than a
    silent breaking change for every ``from core.pg_pool import pg_pool``
    caller (``api/server.py``, future ``core/database_manager.py``)."""
    from core.pg_pool import pg_pool
    assert isinstance(pg_pool, PGConnectionPool)


def test_module_singleton_default_database_url_is_postgres_scheme():
    """The singleton's default DATABASE_URL is a ``postgresql://`` URL
    when the ``DATABASE_URL`` env var is unset — so a misconfigured env
    (missing DATABASE_URL) doesn't silently point at SQLite or some
    other backend.

    When ``DATABASE_URL`` IS set (the conftest test environment sets
    it to a non-postgres value), the singleton honours the env var
    rather than the hard-coded default — production contract: env
    overrides config-default. The test verifies both branches:
    """
    import os
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        # Re-construct a fresh instance to pick up the missing-env-var
        # branch of the constructor (the module-level singleton was
        # already constructed at import time, possibly with DATABASE_URL
        # set).
        from core.pg_pool import PGConnectionPool
        fresh = PGConnectionPool()  # no args → falls back to env / default
        assert fresh._database_url.startswith("postgresql://"), (
            f"expected postgresql:// default when DATABASE_URL unset, "
            f"got {fresh._database_url!r}"
        )
        assert "localhost:5432" in fresh._database_url
        assert "polymarket" in fresh._database_url
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def test_module_singleton_respects_database_url_env_var(monkeypatch):
    """When ``DATABASE_URL`` IS set, the singleton honours it rather
    than the hard-coded postgres default — production contract: env
    overrides config-default."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example.com:5432/prod")
    from core.pg_pool import PGConnectionPool
    fresh = PGConnectionPool()
    assert fresh._database_url == "postgresql://user:pw@db.example.com:5432/prod"
