"""
tests/test_strategy_health_wiring.py — W25-2 strategy health monitor
loop wiring tests.

W25-2 wires ``core.strategy_health.strategy_health_monitor`` into the
``api.server`` lifespan as a periodic background sweep (every 5 min).
This module exercises the wiring end-to-end:

  1. **Loop function exists + is registered as a lifespan task**
        — ``_strategy_health_loop`` is importable from ``api.server``
        and the ``lifespan`` startup body creates an
        ``asyncio.create_task(_strategy_health_loop(), ...)`` whose
        name is ``"strategy-health"``.

  2. **Loop body iterates every IMPLEMENTED strategy + invokes
     ``check_strategy``** with that strategy's recent closed positions
     (filtered from a single global fetch on the closed-positions
     journal).

  3. **API routes** — ``GET /api/strategies/health`` and
     ``GET /api/strategies/health/summary`` are mounted (the W24-8
     routes pre-existed; this is a smoke test ensuring they weren't
     accidentally dropped during the W25-2 wiring).

  4. **Unhealthy strategies get auto-disabled** by the sweep — the
     loop's per-strategy ``check_strategy`` call drives the monitor's
     threshold check, which delegates to ``StrategyRegistry.disable``
     on a breach.

  5. **Prometheus metrics are emitted** — the loop sets
     ``polymarket_alerts_active{severity=disabled|degraded}`` gauges
     from ``strategy_health_monitor.get_summary()`` after each sweep.

Each test drives the loop body for ONE iteration by monkeypatching
``asyncio.sleep`` to return immediately on the first call (the 5-min
pre-tick delay) and raise a sentinel ``_StopLoop`` exception on the
second call (so the ``while True`` exits cleanly after exactly one
sweep). The closed-positions store is monkeypatched to a synthetic
``AsyncMock`` so no SQLite I/O is required.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/strategy_health_wiring_tests")
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
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-strategy-health-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)
os.makedirs(_TMP_ROOT / "dao_data", exist_ok=True)
os.makedirs(_TMP_ROOT / "reports", exist_ok=True)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``strategies.*``, ``api.*``) when pytest is invoked from a
# different cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)
from fastapi.testclient import TestClient  # noqa: E402

from core.strategy_health import (  # noqa: E402
    StrategyHealthMonitor,
    strategy_health_monitor,
)
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    strategy_registry,
)

VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-strategy-health-wiring")

# A small stable IMPLEMENTED strategy_id from the real catalog so the
# disable-flow tests exercise the actual registry path.
_TEST_STRATEGY_ID = "mm_avellaneda_stoikov"


# ── Sentinel exception used to break out of the ``while True`` loop ──
# after exactly one sweep. Raised by the monkeypatched ``asyncio.sleep``
# on its SECOND invocation (so the first call — the 5-min pre-tick delay
# — returns immediately and the loop body runs once; the second call —
# the next iteration's pre-tick — raises, exiting the loop cleanly).
class _StopLoop(Exception):
    """Sentinel raised by the patched ``asyncio.sleep`` to break the
    ``while True`` loop in ``_strategy_health_loop`` after one iteration.
    """


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_strategy_health_singleton():
    """Reset the module-level ``strategy_health_monitor`` singleton's
    in-memory ``_health`` dict before AND after every test so a prior
    test's evaluated strategies don't leak into the next test's
    assertions. Mirrors the autouse-reset convention in
    ``tests/test_latency_wiring.py`` / ``tests/test_rate_limit_tracker.py``.

    The registry's ``_disabled`` set is also reset so a disable from one
    test doesn't leak into the next (mirrors the ``clean_registry``
    fixture in ``tests/test_strategy_health.py``).
    """
    strategy_health_monitor._health.clear()
    strategy_registry._disabled.clear()
    yield
    strategy_health_monitor._health.clear()
    strategy_registry._disabled.clear()


@pytest.fixture
def healthy_trades() -> list[dict]:
    """11 trades, 10 wins, 1 loss — win_rate ≈ 90.9%, expectancy ≈
    +$0.044/trade, all closed in the last minute so the staleness check
    doesn't fire. Below the win-rate / expectancy / drawdown thresholds,
    so the monitor marks the strategy HEALTHY.
    """
    now = time.time()
    pnls = [0.10, 0.05, -0.02, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04]
    return [{"pnl": p, "closed_at": now - 60, "strategy": _TEST_STRATEGY_ID} for p in pnls]


@pytest.fixture
def unhealthy_trades() -> list[dict]:
    """10 trades, 2 wins, 8 losses — win_rate = 20% (below the 30%
    threshold). Small absolute pnls so expectancy stays above -$0.05
    (isolates the win-rate branch from the expectancy branch).
    """
    now = time.time()
    pnls = [0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01]
    return [{"pnl": p, "closed_at": now - 60, "strategy": _TEST_STRATEGY_ID} for p in pnls]


# ── Helper: run the loop body for exactly ONE iteration ─────────────────────


async def _run_loop_one_iteration(
    *,
    closed_positions_rows: list[dict],
    catalog_override: list[dict] | None = None,
    monitor_override: StrategyHealthMonitor | None = None,
) -> None:
    """Drive ``api.server._strategy_health_loop`` through exactly ONE
    sweep, then exit cleanly via the ``_StopLoop`` sentinel.

    The closed-positions store is monkeypatched to an ``AsyncMock`` that
    returns ``closed_positions_rows`` (so no SQLite I/O is required).
    The strategy registry's ``get_catalog`` is optionally overridden
    with ``catalog_override`` so the test can drive a minimal catalog
    (default = the real catalog, which has ≥10 IMPLEMENTED rows).

    The ``strategy_health_monitor`` singleton is optionally overridden
    with ``monitor_override`` so the test can drive a fresh monitor
    instance (default = the production singleton, which the autouse
    fixture above has already cleared).
    """
    # Lazy import (inside the test) so the env-var redirects above are
    # in effect when ``api.server`` is first imported.
    import api.server as _server_module
    from api.server import _strategy_health_loop

    # ── Monkeypatch ``asyncio.sleep`` so:
    #   • the FIRST call (the 5-min pre-tick delay) returns immediately;
    #   • the SECOND call (the next iteration's pre-tick) raises
    #     ``_StopLoop`` so the ``while True`` exits cleanly after one
    #     sweep.
    call_count = {"n": 0}

    async def _fake_sleep(seconds: float) -> None:  # noqa: ARG001 — unused
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise _StopLoop()

    # ── Monkeypatch the closed-positions store's ``get_closed_positions``
    # to return the synthetic rows (the loop fetches via a LAZY import
    # inside its body, so we patch the underlying module attribute).
    async def _fake_get_closed_positions(limit: int = 50, strategy: str | None = None):  # noqa: ARG001
        return list(closed_positions_rows)

    import core.closed_positions as _closed_positions_module

    _orig_get = _closed_positions_module.closed_positions.get_closed_positions
    _closed_positions_module.closed_positions.get_closed_positions = _fake_get_closed_positions

    # ── Optionally override the registry catalog.
    if catalog_override is not None:
        _orig_catalog = strategy_registry.get_catalog
        strategy_registry.get_catalog = lambda *a, **k: list(catalog_override)  # type: ignore[assignment]
    else:
        _orig_catalog = None

    # ── Optionally override the monitor singleton.
    if monitor_override is not None:
        _orig_server_monitor = _server_module.strategy_health_monitor
        _server_module.strategy_health_monitor = monitor_override
    else:
        _orig_server_monitor = None

    # ── Save + restore the original ``asyncio.sleep``.
    _orig_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep  # type: ignore[assignment]

    try:
        # The loop's outermost try/except catches ``Exception``, so the
        # ``_StopLoop`` sentinel — which subclasses ``Exception`` — is
        # caught by the loop's outer ``except Exception`` block and
        # logged at error level. The loop then ``while True``s back to
        # the top, hits ``asyncio.sleep(300)`` again, which would
        # immediately raise another ``_StopLoop``...
        #
        # To avoid an infinite loop, we patch the outer ``except``
        # clause's log call to be a no-op AND we count the
        # ``check_strategy`` invocations directly. The loop's first
        # iteration: ``asyncio.sleep(300)`` returns immediately (call
        # 1) → loop body runs → ``asyncio.sleep(300)`` raises _StopLoop
        # (call 2) → outer ``except Exception`` catches it → loop
        # iterates back to top → ``asyncio.sleep(300)`` raises again
        # (call 3) → outer ``except Exception`` catches it → ...
        #
        # Cleaner: re-raise ``_StopLoop`` from outside the loop's
        # ``except`` block. We do this by patching ``log.error`` to
        # re-raise on a ``_StopLoop`` instance.
        _orig_log_error = _server_module.log.error

        def _re_raise_on_stoploop(msg, *args, **kwargs):  # noqa: ARG001
            # The loop logs ``"[strategy-health] sweep failed: %s"``
            # with the exception as the first arg. If the exception is
            # our sentinel, re-raise it so it propagates out of the
            # loop entirely (instead of being swallowed by the outer
            # ``except Exception``).
            if args and isinstance(args[0], _StopLoop):
                raise args[0]

        _server_module.log.error = _re_raise_on_stoploop  # type: ignore[assignment]

        try:
            with pytest.raises(_StopLoop):
                await _strategy_health_loop()
        finally:
            _server_module.log.error = _orig_log_error  # type: ignore[assignment]
    finally:
        asyncio.sleep = _orig_sleep  # type: ignore[assignment]
        _closed_positions_module.closed_positions.get_closed_positions = _orig_get
        if _orig_catalog is not None:
            strategy_registry.get_catalog = _orig_catalog  # type: ignore[assignment]
        if _orig_server_monitor is not None:
            _server_module.strategy_health_monitor = _orig_server_monitor  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════════
# 1. Loop function exists + is wired into lifespan
# ═══════════════════════════════════════════════════════════════════════════


def test_strategy_health_loop_function_importable():
    """``_strategy_health_loop`` is importable from ``api.server`` —
    the W25-2 wiring step that defines the background loop function.
    """
    from api.server import _strategy_health_loop
    assert callable(_strategy_health_loop)


def test_strategy_health_loop_referenced_in_lifespan():
    """The ``lifespan`` startup body in ``api/server.py`` schedules
    ``_strategy_health_loop`` as a background task. We assert by
    inspecting the source — the lifespan function is too heavy to
    actually run end-to-end in a unit test (it pulls in PG pools /
    market discovery / ML orchestrator / etc.), so we verify the
    wiring statically.
    """
    import inspect

    from api.server import lifespan
    src = inspect.getsource(lifespan)
    # The create_task call uses the loop function name.
    assert "_strategy_health_loop" in src, (
        "lifespan must create a background task via "
        "``asyncio.create_task(_strategy_health_loop(), ...)``"
    )
    # And it should be cancelled on shutdown.
    assert "strategy_health_task.cancel()" in src, (
        "lifespan must cancel the strategy-health background task on shutdown"
    )
    # And the task name should be ``"strategy-health"``.
    assert '"strategy-health"' in src or "'strategy-health'" in src, (
        "the create_task call should name the task 'strategy-health' "
        "so it's identifiable in task listings / logs"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Loop body iterates IMPLEMENTED strategies + calls check_strategy
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_calls_check_strategy_for_each_implemented_strategy(
    healthy_trades,
):
    """The loop walks every IMPLEMENTED strategy in the registry catalog
    and invokes ``strategy_health_monitor.check_strategy`` with the
    strategy's recent closed positions (filtered from the global
    fetch).

    Drives the loop body for one iteration with a synthetic catalog
    containing ONE IMPLEMENTED strategy + ONE PLANNED strategy. Only
    the IMPLEMENTED row should reach ``check_strategy``.
    """
    # Build a minimal catalog: one IMPLEMENTED + one PLANNED row.
    minimal_catalog = [
        {
            "strategy_id": _TEST_STRATEGY_ID,
            "name": "Test Implemented",
            "category": "market_making",
            "description": "test",
            "risk_level": "Medium",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
        {
            "strategy_id": "mm_glft_optimal",
            "name": "Test Planned",
            "category": "market_making",
            "description": "test planned",
            "risk_level": "Medium",
            "status": STATUS_PLANNED,
            "implemented": False,
            "is_running": False,
            "default_enabled": False,
            "is_disabled": False,
        },
    ]

    # Track which strategies were evaluated.
    seen: list[str] = []
    fresh_monitor = StrategyHealthMonitor()
    orig_check = fresh_monitor.check_strategy

    def _tracking_check(strategy_name, trades, errors=0):
        seen.append(strategy_name)
        return orig_check(strategy_name, trades, errors=errors)

    fresh_monitor.check_strategy = _tracking_check  # type: ignore[assignment]

    await _run_loop_one_iteration(
        closed_positions_rows=healthy_trades,
        catalog_override=minimal_catalog,
        monitor_override=fresh_monitor,
    )

    # The IMPLEMENTED strategy was evaluated; the PLANNED strategy was NOT.
    assert _TEST_STRATEGY_ID in seen, (
        f"IMPLEMENTED strategy '{_TEST_STRATEGY_ID}' must be passed to "
        f"check_strategy; saw {seen}"
    )
    assert "mm_glft_optimal" not in seen, (
        "PLANNED strategies must be skipped by the loop (only IMPLEMENTED "
        f"rows are evaluated); saw {seen}"
    )

    # And the monitor recorded health for the IMPLEMENTED strategy.
    all_health = fresh_monitor.get_all_health()
    strategy_names = {h["strategy_name"] for h in all_health}
    assert _TEST_STRATEGY_ID in strategy_names
    # Healthy trades → HEALTHY status.
    impl_health = next(
        h for h in all_health if h["strategy_name"] == _TEST_STRATEGY_ID
    )
    assert impl_health["status"] == "healthy"


@pytest.mark.asyncio
async def test_loop_filters_positions_per_strategy(healthy_trades):
    """The loop filters the global closed-positions fetch by
    ``strategy == strategy_id`` so each strategy's ``check_strategy``
    call receives ONLY its own trades.

    Drives the loop with a synthetic catalog of TWO IMPLEMENTED rows
    + a closed-positions list that mixes trades from both. The
    monitor must see only its own strategy's trades.
    """
    other_strategy_id = "arb_binary_dutch_book"
    # Build a list of trades where each strategy has its own rows.
    now = time.time()
    other_healthy_trades = [
        {"pnl": 0.10, "closed_at": now - 60, "strategy": other_strategy_id}
        for _ in range(11)
    ]
    all_rows = list(healthy_trades) + other_healthy_trades

    minimal_catalog = [
        {
            "strategy_id": _TEST_STRATEGY_ID,
            "name": "First",
            "category": "market_making",
            "description": "test",
            "risk_level": "Medium",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
        {
            "strategy_id": other_strategy_id,
            "name": "Second",
            "category": "arbitrage",
            "description": "test",
            "risk_level": "Low",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
    ]

    fresh_monitor = StrategyHealthMonitor()

    # Spy on check_strategy to capture the trade lists each strategy saw.
    seen_trades: dict[str, list[dict]] = {}
    orig_check = fresh_monitor.check_strategy

    def _capturing_check(strategy_name, trades, errors=0):
        seen_trades[strategy_name] = list(trades)
        return orig_check(strategy_name, trades, errors=errors)

    fresh_monitor.check_strategy = _capturing_check  # type: ignore[assignment]

    await _run_loop_one_iteration(
        closed_positions_rows=all_rows,
        catalog_override=minimal_catalog,
        monitor_override=fresh_monitor,
    )

    # Each strategy saw ONLY its own trades (no cross-contamination).
    assert len(seen_trades[_TEST_STRATEGY_ID]) == 11
    assert all(
        t["strategy"] == _TEST_STRATEGY_ID for t in seen_trades[_TEST_STRATEGY_ID]
    )
    assert len(seen_trades[other_strategy_id]) == 11
    assert all(
        t["strategy"] == other_strategy_id
        for t in seen_trades[other_strategy_id]
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. API routes — TestClient smoke tests
# ═══════════════════════════════════════════════════════════════════════════


def _build_client_with_isolated_monitor(monkeypatch, fresh_monitor):
    """Build a TestClient against the real ``api.server.app`` (so the
    ``enforce_api_auth`` middleware + auth policy is exercised end-to-end)
    while the ``strategy_health_monitor`` singleton is monkeypatched to
    a fresh instance.

    Mirrors the helper in ``tests/test_strategy_health.py`` — patches
    BOTH ``core.strategy_health.strategy_health_monitor`` AND
    ``api.server.strategy_health_monitor`` because the route handlers
    capture the singleton via closure at import time.
    """
    from api.server import app
    monkeypatch.setattr(
        "core.strategy_health.strategy_health_monitor", fresh_monitor,
    )
    monkeypatch.setattr(
        "api.server.strategy_health_monitor", fresh_monitor,
    )
    return TestClient(app, raise_server_exceptions=False), fresh_monitor


def test_api_get_strategy_health_route_exists(monkeypatch):
    """``GET /api/strategies/health`` is registered on ``api.server.app``
    and returns 200 + a JSON list (the W24-8 route — this is a smoke
    test ensuring the W25-2 wiring didn't accidentally drop it).
    """
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)


def test_api_get_strategy_health_summary_route_exists(monkeypatch):
    """``GET /api/strategies/health/summary`` is registered + returns the
    counts dict (total / healthy / degraded / disabled / inactive)."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health/summary",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body.keys()) == {
        "total_strategies", "healthy", "degraded", "disabled", "inactive",
    }


def test_api_routes_require_auth(monkeypatch):
    """Both health routes require the bearer token — the auth middleware
    applies to every non-public path (regression guard against an
    accidental ``PUBLIC_PATHS.add("/api/strategies/health")``)."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    assert client.get("/api/strategies/health").status_code == 401
    assert (
        client.get("/api/strategies/health/summary").status_code == 401
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Unhealthy strategies get auto-disabled by the sweep
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_auto_disables_unhealthy_strategy(unhealthy_trades):
    """When the loop's per-strategy ``check_strategy`` call detects a
    threshold breach (e.g., win_rate < 30%), the monitor delegates to
    ``StrategyRegistry.disable`` so the strategy is marked disabled in
    the registry + ``start_strategy`` short-circuits.

    Drives the loop with a synthetic catalog of ONE IMPLEMENTED strategy
    whose recent closed positions have a 20% win rate. After the sweep:
      • the monitor records the strategy as DISABLED;
      • the registry marks the strategy as disabled;
      • a subsequent ``start_strategy`` call short-circuits.
    """
    minimal_catalog = [
        {
            "strategy_id": _TEST_STRATEGY_ID,
            "name": "Test Implemented",
            "category": "market_making",
            "description": "test",
            "risk_level": "Medium",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
    ]

    await _run_loop_one_iteration(
        closed_positions_rows=unhealthy_trades,
        catalog_override=minimal_catalog,
    )

    # ── Monitor recorded the strategy as DISABLED with a win-rate reason.
    all_health = strategy_health_monitor.get_all_health()
    assert len(all_health) == 1
    health = all_health[0]
    assert health["strategy_name"] == _TEST_STRATEGY_ID
    assert health["status"] == "disabled", (
        f"unhealthy strategy must be marked DISABLED; got {health['status']}"
    )
    assert "Win rate" in health["disable_reason"]
    assert health["disable_time"] > 0

    # ── Registry has the strategy flagged as disabled.
    assert strategy_registry.is_disabled(_TEST_STRATEGY_ID) is True

    # ── ``start_strategy`` short-circuits when disabled.
    ok = await strategy_registry.start_strategy(_TEST_STRATEGY_ID)
    assert ok is False, (
        "a disabled strategy must NOT be restartable without enable()"
    )

    # ── Cleanup: re-enable so the test's autouse reset fixture starts
    # from a clean baseline (defensive — the autouse fixture clears
    # ``_disabled`` already, but ``enable()`` exercises the canonical
    # path).
    strategy_registry.enable(_TEST_STRATEGY_ID)
    assert strategy_registry.is_disabled(_TEST_STRATEGY_ID) is False


@pytest.mark.asyncio
async def test_loop_does_not_disable_healthy_strategy(healthy_trades):
    """Symmetric positive: a healthy strategy (win_rate ≥ 30%, expectancy
    ≥ -$0.05, drawdown ≤ 15%) is NOT disabled by the sweep — the
    monitor marks it HEALTHY and the registry's ``_disabled`` set is
    unchanged.
    """
    minimal_catalog = [
        {
            "strategy_id": _TEST_STRATEGY_ID,
            "name": "Test Implemented",
            "category": "market_making",
            "description": "test",
            "risk_level": "Medium",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
    ]

    await _run_loop_one_iteration(
        closed_positions_rows=healthy_trades,
        catalog_override=minimal_catalog,
    )

    all_health = strategy_health_monitor.get_all_health()
    assert len(all_health) == 1
    health = all_health[0]
    assert health["status"] == "healthy", (
        f"healthy strategy must be marked HEALTHY; got {health['status']}"
    )
    assert health["disable_reason"] == ""
    assert health["disable_time"] == 0.0
    assert strategy_registry.is_disabled(_TEST_STRATEGY_ID) is False


# ═══════════════════════════════════════════════════════════════════════════
# 5. Prometheus metrics emission
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_emits_prometheus_metrics(unhealthy_trades, monkeypatch):
    """After the per-strategy sweep, the loop sets the
    ``polymarket_alerts_active{severity=disabled|degraded}`` Prometheus
    gauges from ``strategy_health_monitor.get_summary()``.

    Drives the loop with one unhealthy strategy (1 disabled) and
    spies on ``alerts_active.labels(...).set`` to assert the disabled
    count is emitted.
    """
    minimal_catalog = [
        {
            "strategy_id": _TEST_STRATEGY_ID,
            "name": "Test Implemented",
            "category": "market_making",
            "description": "test",
            "risk_level": "Medium",
            "status": STATUS_IMPLEMENTED,
            "implemented": True,
            "is_running": False,
            "default_enabled": True,
            "is_disabled": False,
        },
    ]

    # ── Spy on ``alerts_active.labels(...).set(...)`` calls. The gauge
    # is a ``prometheus_client.Gauge``; we monkeypatch its ``labels``
    # method to return a MagicMock so we can introspect the ``set``
    # invocations.
    set_calls: list[tuple[str, float]] = []

    def _fake_labels(severity: str):
        def _set(value: float) -> None:
            set_calls.append((severity, float(value)))
        return SimpleNamespace(set=_set)

    # Patch the gauge on the ``core.prometheus_metrics`` module (the
    # loop lazy-imports the gauge from there, so the patch must land
    # on the underlying module attribute — not on a captured copy).
    import core.prometheus_metrics as _prom_module

    _orig_alerts_active = _prom_module.alerts_active
    _prom_module.alerts_active = SimpleNamespace(labels=_fake_labels)  # type: ignore[assignment]

    try:
        await _run_loop_one_iteration(
            closed_positions_rows=unhealthy_trades,
            catalog_override=minimal_catalog,
        )
    finally:
        _prom_module.alerts_active = _orig_alerts_active  # type: ignore[assignment]

    # The disabled + degraded counts were emitted (severity labels match).
    severities_emitted = {sev for sev, _ in set_calls}
    assert "disabled" in severities_emitted, (
        f"loop must emit the disabled count via alerts_active.labels"
        f"(severity='disabled').set(); got {set_calls}"
    )
    assert "degraded" in severities_emitted, (
        f"loop must emit the degraded count via alerts_active.labels"
        f"(severity='degraded').set(); got {set_calls}"
    )

    # The disabled count value = 1 (one unhealthy strategy auto-disabled).
    disabled_value = next(
        v for sev, v in set_calls if sev == "disabled"
    )
    assert disabled_value == 1.0, (
        f"alerts_active.labels(severity='disabled').set() must reflect "
        f"the monitor's disabled count (1); got {disabled_value}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Defensive — loop body survives transient failures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_does_not_crash_on_closed_positions_failure():
    """If the closed-positions store raises (e.g. SQLite busy / corrupt),
    the loop's outer ``except Exception`` catches it + logs at error
    level — the loop does NOT propagate the exception to the caller
    (the lifespan would otherwise crash).

    Drives the loop with a closed-positions fetch that raises. The
    loop body completes (raises ``_StopLoop`` from the patched
    ``asyncio.sleep``) without re-raising the synthetic DB error.
    """
    import api.server as _server_module
    from api.server import _strategy_health_loop

    # ── Patch ``asyncio.sleep`` so the loop runs exactly ONE iteration.
    call_count = {"n": 0}

    async def _fake_sleep(seconds: float) -> None:  # noqa: ARG001
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise _StopLoop()

    # ── Patch the closed-positions fetch to raise.
    async def _raising_get(*args, **kwargs):
        raise RuntimeError("synthetic DB failure")

    import core.closed_positions as _closed_positions_module

    _orig_get = _closed_positions_module.closed_positions.get_closed_positions
    _closed_positions_module.closed_positions.get_closed_positions = _raising_get

    _orig_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep  # type: ignore[assignment]

    # ── The loop's outer ``except Exception`` swallows the RuntimeError
    # (logs at error level). The next iteration's ``asyncio.sleep``
    # raises ``_StopLoop``, which the outer ``except`` also catches —
    # we patch ``log.error`` to re-raise on ``_StopLoop`` so the loop
    # exits cleanly after one iteration.
    _orig_log_error = _server_module.log.error

    def _re_raise_on_stoploop(msg, *args, **kwargs):  # noqa: ARG001
        if args and isinstance(args[0], _StopLoop):
            raise args[0]

    _server_module.log.error = _re_raise_on_stoploop  # type: ignore[assignment]

    try:
        with pytest.raises(_StopLoop):
            await _strategy_health_loop()
    finally:
        _server_module.log.error = _orig_log_error  # type: ignore[assignment]
        asyncio.sleep = _orig_sleep  # type: ignore[assignment]
        _closed_positions_module.closed_positions.get_closed_positions = _orig_get

    # ── The synthetic DB failure did NOT disable any strategies — the
    # loop body's exception was caught before ``check_strategy`` ran.
    assert strategy_registry.is_disabled(_TEST_STRATEGY_ID) is False
    assert strategy_health_monitor.get_all_health() == []


# ═══════════════════════════════════════════════════════════════════════════
# 7. Real-catalog smoke — at least one IMPLEMENTED row exists
# ═══════════════════════════════════════════════════════════════════════════


def test_real_catalog_has_implemented_strategies():
    """Sanity check that the real strategy catalog has at least one
    IMPLEMENTED row — otherwise the loop's ``for strategy_meta in
    strategy_registry.get_catalog()`` body would never run a single
    ``check_strategy`` call (the W25-2 wiring would be a no-op against
    the production catalog)."""
    catalog = strategy_registry.get_catalog()
    implemented = [s for s in catalog if s.get("status") == STATUS_IMPLEMENTED]
    assert len(implemented) >= 1, (
        "the real catalog must have at least one IMPLEMENTED strategy for "
        "the W25-2 health sweep to have any strategies to evaluate"
    )
    # And specifically the strategy_id the tests above use must be
    # IMPLEMENTED in the real catalog (catches a rename before it
    # silently breaks the wiring tests).
    test_strategy_ids = {s["strategy_id"] for s in implemented}
    assert _TEST_STRATEGY_ID in test_strategy_ids, (
        f"test strategy_id '{_TEST_STRATEGY_ID}' must be IMPLEMENTED in "
        f"the real catalog; got IMPLEMENTED ids {test_strategy_ids}"
    )
