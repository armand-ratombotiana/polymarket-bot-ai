"""
Unit tests for the W22-7 missing-observability-metrics expansion.

W22-7 — adds the 9 God-Mode-§54 spec metrics that were previously
declared-but-never-collected:

  * ``data_source.latency``        — CLOB REST round-trip ms
  * ``data_source.reconnects``     — cumulative WebSocket reconnect count
  * ``bot.errors``                 — bot-level errors in the last 30 s
  * ``bot.actions``                — bot-level actions in the last 30 s
  * ``strategy.evaluations``       — per-strategy evaluation counter
  * ``strategy.signals``           — per-strategy signal counter
  * ``strategy.rejects``            — per-strategy reject counter
  * ``execution.latency``          — median ms of recent executions
  * ``system.db_connections``      — DB backend live (1/0)
  * ``system.queue_health``        — pending job count

(The spec lists "9 missing metrics"; the strategy bucket counts as one
metric in the spec sketch because evaluations / signals / rejects are
emitted by a single ``_collect_strategy_metrics`` fan-out over each
active strategy.)

Test strategy
~~~~~~~~~~~~~

Each collector is invoked directly (mirrors the
``test_observability_collector.py`` pattern of bypassing the 30 s
background loop). ``record_metric`` is monkeypatched on the collector
module to capture every call, then asserted on. The fake forwards to
the real backend so a follow-up ``get_health_report()`` call sees the
rows too — that's how the dashboard round-trip test (Step 3 of the
spec) is verified.

Error-handling tests
~~~~~~~~~~~~~~~~~~~~

For each collector that depends on an external subsystem import, a
test verifies the collector never raises when the import fails (the
broad ``except Exception`` + ``debug`` log contract). This is verified
by monkeypatching ``sys.modules`` to poison the import path and
asserting the collector returns ``None`` silently.

Async mode
~~~~~~~~~~

pytest-asyncio 1.3.0; ``pytest.ini`` is in strict mode so every
``async def test_*`` is decorated via ``pytestmark = pytest.mark.asyncio``
(same convention as every sibling test module).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)
import pytest_asyncio  # noqa: E402

import core.observability_collector as observability_collector  # noqa: E402
from core.observability import (  # noqa: E402
    CAT_BOT,
    CAT_DATA_SOURCE,
    CAT_EXECUTION,
    CAT_STRATEGY,
    CAT_SYSTEM,
    METRIC_NAMES,
    get_health_report,
    observability,
)
from core.data_store import store  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` cannot be edited per the W8 task
# constraint (carried forward); same convention as every sibling test
# module.
pytestmark = pytest.mark.asyncio


# ── Autouse: reset collector state ──────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _reset_collector_state():
    """Stop any leftover collector task and clear the module global.

    Mirrors the autouse fixture in ``test_observability_collector.py``:
    clears the module-level ``_collector_task`` global before AND after
    every test so idempotency assertions start from a clean baseline.
    Belt-and-braces with conftest's autouse ``_reset_store_factory_defaults``
    which resets ``store`` / ``risk_manager`` / ``paper_sim`` singletons.
    """
    observability_collector._collector_task = None

    yield  # ── test runs ──

    if observability_collector._collector_task is not None:
        try:
            await observability_collector.stop_collector()
        except Exception:
            pass
        observability_collector._collector_task = None


# ── Test-local helper: capture record_metric calls ───────────────────────────
def _make_capturing_record_metric(monkeypatch):
    """Monkeypatch the collector's ``record_metric`` binding to a capturing fake.

    Returns the captured list. The fake forwards to the real backend so
    a follow-up ``get_health_report()`` call sees the rows too (mirrors
    the pattern in ``test_observability_collector.py``).
    """
    captured: list[tuple[str, str, float, dict]] = []
    original_record_metric = observability_collector.record_metric

    async def capturing_record_metric(category, name, value, **metadata):
        captured.append((category, name, value, metadata))
        await original_record_metric(category, name, value, **metadata)

    monkeypatch.setattr(
        observability_collector, "record_metric", capturing_record_metric
    )
    return captured


# ── (1) data_source.latency ──────────────────────────────────────────────────
async def test_data_source_latency_collected_on_success(monkeypatch):
    """``_collect_data_source_latency`` emits ``data_source.latency`` (ms).

    On a successful ``clob_client.health_check()`` call, the collector
    must emit a single ``record_metric`` call with:
      * category = ``data_source``
      * name     = ``latency``
      * value    = the latency returned by ``health_check`` (float, ms)
      * metadata = ``{"source": "clob_rest"}``

    ``clob_client.health_check`` is monkeypatched to a no-op coroutine
    returning a fixed latency (the test doesn't make a real network
    call — that would be flaky in CI).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    async def fake_health_check():
        return 123.456

    # Patch the clob_client module-level singleton's health_check method.
    from core.clob_client import clob_client
    monkeypatch.setattr(clob_client, "health_check", fake_health_check)

    await observability_collector._collect_data_source_latency()

    latency_calls = [
        c for c in captured if c[0] == CAT_DATA_SOURCE and c[1] == "latency"
    ]
    assert len(latency_calls) == 1, (
        f"expected exactly 1 data_source.latency record_metric call, got "
        f"{len(latency_calls)}; full captured: {captured}"
    )
    cat, name, value, metadata = latency_calls[0]
    assert value == pytest.approx(123.456), (
        f"recorded latency value must match health_check return (123.456); "
        f"got {value}"
    )
    assert metadata.get("source") == "clob_rest"
    assert "error" not in metadata, (
        "successful probe must NOT set the error flag"
    )


async def test_data_source_latency_emits_sentinel_on_failure(monkeypatch):
    """``_collect_data_source_latency`` emits ``latency=-1`` + ``error=True``
    when ``health_check`` raises.

    The probe failure must surface as a sentinel ``-1`` value with
    ``error=True`` metadata so the dashboard can distinguish "we
    measured 250ms" from "we couldn't measure". A bare ``record_metric``
    skip on failure would leave the metric absent, which is harder to
    alert on than a sentinel.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    async def failing_health_check():
        raise RuntimeError("connection refused")

    from core.clob_client import clob_client
    monkeypatch.setattr(clob_client, "health_check", failing_health_check)

    await observability_collector._collect_data_source_latency()

    latency_calls = [
        c for c in captured if c[0] == CAT_DATA_SOURCE and c[1] == "latency"
    ]
    assert len(latency_calls) == 1, (
        f"expected exactly 1 data_source.latency record_metric call (the "
        f"sentinel) on probe failure; got {len(latency_calls)}"
    )
    cat, name, value, metadata = latency_calls[0]
    assert value == pytest.approx(-1.0), (
        f"failed probe must emit latency=-1 sentinel; got {value}"
    )
    assert metadata.get("error") is True, (
        "failed probe must set error=True metadata flag"
    )
    assert "reason" in metadata, (
        "failed probe must include the exception message in metadata.reason"
    )


# ── (2) data_source.reconnects ────────────────────────────────────────────────
async def test_data_source_reconnects_reads_ws_client_counter(monkeypatch):
    """``_collect_data_source_reconnects`` reads ``ws_client._reconnect_count``
    and emits it as ``data_source.reconnects``.

    The counter is cumulative and monotonic; the test sets a known
    value via monkeypatch and verifies the metric round-trips it
    unchanged.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.ws_client import ws_client
    monkeypatch.setattr(ws_client, "_reconnect_count", 7, raising=False)

    await observability_collector._collect_data_source_reconnects()

    reconnect_calls = [
        c for c in captured if c[0] == CAT_DATA_SOURCE and c[1] == "reconnects"
    ]
    assert len(reconnect_calls) == 1, (
        f"expected exactly 1 data_source.reconnects call; got "
        f"{len(reconnect_calls)}; full captured: {captured}"
    )
    cat, name, value, metadata = reconnect_calls[0]
    assert value == pytest.approx(7.0), (
        f"recorded reconnects must match ws_client._reconnect_count (7); "
        f"got {value}"
    )
    assert metadata.get("source") == "websocket"


async def test_data_source_reconnects_handles_missing_ws_client(monkeypatch):
    """``_collect_data_source_reconnects`` must NOT raise if the
    ``ws_client`` import fails (e.g. the websockets package isn't
    installed in the test env).

    The collector's broad ``except Exception`` + ``debug`` log contract
    means a missing dependency silently skips the metric — the rest of
    the cycle continues. This test poisons the import path so
    ``from core.ws_client import ws_client`` raises ImportError inside
    the collector and asserts the collector returns ``None`` silently.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    # Poison the import path so ``from core.ws_client import ws_client``
    # raises ImportError inside the collector.
    import builtins

    real_import = builtins.__import__

    def poisoned_import(name, *args, **kwargs):
        if name == "core.ws_client":
            raise ImportError("simulated missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poisoned_import)

    # Must not raise.
    await observability_collector._collect_data_source_reconnects()

    # No metric was recorded because the import failed before the
    # record_metric call.
    reconnect_calls = [
        c for c in captured if c[0] == CAT_DATA_SOURCE and c[1] == "reconnects"
    ]
    assert reconnect_calls == [], (
        "no reconnects metric should be recorded when the import fails"
    )


# ── (3) bot.errors ───────────────────────────────────────────────────────────
async def test_bot_errors_reads_windowed_count(monkeypatch):
    """``_collect_bot_errors`` reads ``store.get_error_count_since`` and
    emits the count as ``bot.errors``.

    The test records 3 errors via ``store.record_error()``, then
    invokes the collector and asserts the metric value is 3. A 4th
    error outside the window (older than 30 s) must NOT be counted.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    # 3 recent errors (within the 30 s window).
    for _ in range(3):
        await store.record_error()
    # 1 stale error (older than the 30 s window) — must NOT be counted.
    stale_ts = time.time() - 60.0
    store._errors.append(stale_ts)

    await observability_collector._collect_bot_errors()

    error_calls = [
        c for c in captured if c[0] == CAT_BOT and c[1] == "errors"
    ]
    assert len(error_calls) == 1, (
        f"expected exactly 1 bot.errors call; got {len(error_calls)}"
    )
    cat, name, value, metadata = error_calls[0]
    assert value == pytest.approx(3.0), (
        f"recorded bot.errors must match the in-window count (3); got {value}"
    )
    assert metadata.get("window_seconds") == 30


async def test_bot_errors_handles_missing_method(monkeypatch):
    """``_collect_bot_errors`` must NOT raise if ``store`` lacks
    ``get_error_count_since`` (the ``hasattr`` guard falls through to
    ``count=0`` rather than ``AttributeError``).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    # ``get_error_count_since`` is a method on the DataStore class. Hide
    # it from the singleton via a per-instance shadowing delete so the
    # ``hasattr`` check returns False. The next collector call should
    # fall through to ``count=0``.
    cls = type(store)
    original = cls.get_error_count_since

    # Replace with a descriptor that returns False for hasattr (a
    # property that raises AttributeError on access — the standard
    # Python idiom for "this attribute doesn't exist").
    def _raise_attr_error(self):
        raise AttributeError("simulated missing method")

    monkeypatch.setattr(cls, "get_error_count_since",
                        property(_raise_attr_error))

    await observability_collector._collect_bot_errors()

    error_calls = [
        c for c in captured if c[0] == CAT_BOT and c[1] == "errors"
    ]
    assert len(error_calls) == 1, (
        f"expected exactly 1 bot.errors call (with count=0); got "
        f"{len(error_calls)}"
    )
    assert error_calls[0][2] == pytest.approx(0.0), (
        f"missing method must fall through to count=0; got {error_calls[0][2]}"
    )


# ── (4) bot.actions ──────────────────────────────────────────────────────────
async def test_bot_actions_reads_windowed_count(monkeypatch):
    """``_collect_bot_actions`` reads ``store.get_action_count_since`` and
    emits the count as ``bot.actions``.

    Mirrors ``test_bot_errors_reads_windowed_count``: records 5 actions
    in the window + 2 stale actions outside, asserts the metric value
    is 5.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    for _ in range(5):
        await store.record_action()
    # 2 stale actions outside the 30 s window.
    stale_ts = time.time() - 120.0
    store._actions.extend([stale_ts, stale_ts])

    await observability_collector._collect_bot_actions()

    action_calls = [
        c for c in captured if c[0] == CAT_BOT and c[1] == "actions"
    ]
    assert len(action_calls) == 1, (
        f"expected exactly 1 bot.actions call; got {len(action_calls)}"
    )
    cat, name, value, metadata = action_calls[0]
    assert value == pytest.approx(5.0), (
        f"recorded bot.actions must match the in-window count (5); got {value}"
    )
    assert metadata.get("window_seconds") == 30


# ── (5/6/7) strategy.evaluations / signals / rejects ─────────────────────────
async def test_strategy_metrics_per_active_strategy(monkeypatch):
    """``_collect_strategy_metrics`` fans out over each active strategy in
    ``strategy_registry.get_active_instances()`` and emits three metrics
    per strategy: evaluations / signals / rejects.

    The test registers a stub strategy with bumped counters, then
    asserts all three metrics appear with the stub's strategy name in
    the metadata.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from strategies.base import BaseStrategy

    class _StubStrategy(BaseStrategy):
        name = "test_stub_strategy"

        def __init__(self):
            super().__init__()
            # Bump the counters to non-zero so the test is meaningful.
            self._stats["signals"] = 4
            self._stats["trades"] = 2
            self._stats["errors"] = 1
            self._stats["evaluations"] = 11
            self._stats["rejects"] = 3

        async def _run(self):
            pass

    stub = _StubStrategy()
    # Monkeypatch the registry so ``get_active_instances`` returns our
    # stub keyed by its strategy_id.
    from strategies.registry import strategy_registry
    monkeypatch.setattr(
        strategy_registry, "get_active_instances",
        lambda: {"test_stub_strategy": stub},
    )

    await observability_collector._collect_strategy_metrics()

    strategy_calls = [c for c in captured if c[0] == CAT_STRATEGY]
    names = {c[1] for c in strategy_calls}
    assert {"evaluations", "signals", "rejects"}.issubset(names), (
        f"expected evaluations/signals/rejects under CAT_STRATEGY; got {names}"
    )

    eval_calls = [c for c in strategy_calls if c[1] == "evaluations"]
    sig_calls = [c for c in strategy_calls if c[1] == "signals"]
    rej_calls = [c for c in strategy_calls if c[1] == "rejects"]
    assert len(eval_calls) == 1 and eval_calls[0][2] == pytest.approx(11.0)
    assert len(sig_calls) == 1 and sig_calls[0][2] == pytest.approx(4.0)
    assert len(rej_calls) == 1 and rej_calls[0][2] == pytest.approx(3.0)

    # All three carry the strategy name in metadata.
    for c in strategy_calls:
        assert c[3].get("strategy") == "test_stub_strategy"
        assert c[3].get("strategy_id") == "test_stub_strategy"


async def test_strategy_metrics_no_active_strategies_emits_nothing(monkeypatch):
    """When no strategies are active, ``_collect_strategy_metrics`` emits
    nothing (no per-strategy metrics, no zero-valued placeholder rows).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from strategies.registry import strategy_registry
    monkeypatch.setattr(
        strategy_registry, "get_active_instances", lambda: {}
    )

    await observability_collector._collect_strategy_metrics()

    strategy_calls = [c for c in captured if c[0] == CAT_STRATEGY]
    assert strategy_calls == [], (
        f"no strategy metrics should be emitted when no strategies are "
        f"active; got {strategy_calls}"
    )


async def test_strategy_metrics_handles_diagnostics_exception(monkeypatch):
    """``_collect_strategy_metrics`` must NOT raise if a strategy's
    ``diagnostics()`` raises — the offending strategy is skipped (logged
    at debug) and the loop continues with the next one.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from strategies.base import BaseStrategy

    class _RaisingStrategy(BaseStrategy):
        name = "raising_strategy"

        async def _run(self):
            pass

        def diagnostics(self):
            raise RuntimeError("diagnostics blew up")

    class _HealthyStrategy(BaseStrategy):
        name = "healthy_strategy"

        async def _run(self):
            pass

    raising = _RaisingStrategy()
    healthy = _HealthyStrategy()
    healthy._stats["signals"] = 5
    healthy._stats["evaluations"] = 10
    healthy._stats["rejects"] = 2

    from strategies.registry import strategy_registry
    monkeypatch.setattr(
        strategy_registry, "get_active_instances",
        lambda: {"raising": raising, "healthy": healthy},
    )

    # Must not raise.
    await observability_collector._collect_strategy_metrics()

    # The healthy strategy's metrics were still recorded.
    healthy_calls = [
        c for c in captured if c[3].get("strategy_id") == "healthy"
    ]
    assert len(healthy_calls) == 3, (
        f"healthy strategy must still emit its 3 metrics after the raising "
        f"strategy was skipped; got {len(healthy_calls)}: {healthy_calls}"
    )


# ── (8) execution.latency ────────────────────────────────────────────────────
async def test_execution_latency_reads_median_ms(monkeypatch):
    """``_collect_execution_latency`` reads
    ``latency_tracker.get_stats(0.5)`` and emits ``execution.latency`` as
    the median ms of recent total-latency samples.

    The existing ``core.latency_tracker`` module (W22-6) returns a
    dict shaped like ``{"n_records": N, "decision_latency": {...},
    "execution_latency": {...}, "total_latency": {...}}``. When the
    window is empty it returns ``{"n_records": 0}`` (no ``total_latency``
    key) — the collector must fall through to ``median_ms=0.0`` rather
    than ``KeyError``.

    The test monkeypatches the singleton's ``get_stats`` to return a
    fixed payload so the assertion is deterministic (no real
    record_signal → record_order → record_fill triple required).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.latency_tracker import latency_tracker
    monkeypatch.setattr(
        latency_tracker, "get_stats",
        lambda hours=0.5: {
            "n_records": 5,
            "decision_latency": {"median_ms": 12.0},
            "execution_latency": {"median_ms": 30.0},
            "total_latency": {
                "median_ms": 42.0,
                "p95_ms": 80.0,
                "p99_ms": 95.0,
                "min_ms": 10.0,
                "max_ms": 100.0,
            },
        },
    )

    await observability_collector._collect_execution_latency()

    latency_calls = [
        c for c in captured if c[0] == CAT_EXECUTION and c[1] == "latency"
    ]
    assert len(latency_calls) == 1, (
        f"expected exactly 1 execution.latency call; got {len(latency_calls)}"
    )
    cat, name, value, metadata = latency_calls[0]
    assert value == pytest.approx(42.0, rel=1e-3), (
        f"recorded execution.latency must match total_latency.median_ms "
        f"(42.0); got {value}"
    )
    assert metadata.get("window_hours") == 0.5
    assert metadata.get("sample_count") == 5
    assert metadata.get("p95_ms") == pytest.approx(80.0)
    assert metadata.get("p99_ms") == pytest.approx(95.0)


async def test_execution_latency_zero_when_no_samples(monkeypatch):
    """``_collect_execution_latency`` emits ``0.0`` when the tracker has no
    samples (a fresh boot before any execution has been recorded).

    The existing ``core.latency_tracker.get_stats`` returns
    ``{"n_records": 0}`` when the window is empty — no ``total_latency``
    key. The collector's ``stats.get("total_latency", {})`` fallback
    must absorb the missing key and emit ``median_ms=0.0`` (rather than
    ``KeyError``).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.latency_tracker import latency_tracker
    monkeypatch.setattr(
        latency_tracker, "get_stats",
        lambda hours=0.5: {"n_records": 0},
    )

    await observability_collector._collect_execution_latency()

    latency_calls = [
        c for c in captured if c[0] == CAT_EXECUTION and c[1] == "latency"
    ]
    assert len(latency_calls) == 1
    assert latency_calls[0][2] == pytest.approx(0.0), (
        "empty tracker must emit 0.0 (no None / no skip)"
    )
    assert latency_calls[0][3].get("sample_count") == 0


async def test_execution_latency_handles_get_stats_exception(monkeypatch):
    """``_collect_execution_latency`` must NOT raise if
    ``latency_tracker.get_stats`` raises (broad ``except Exception``
    contract).

    The collector's broad ``except Exception`` + ``debug`` log means a
    failed ``get_stats`` (e.g. the SQLite DB is locked / corrupted)
    silently skips the metric rather than crashing the collector loop.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.latency_tracker import latency_tracker

    def raising_get_stats(hours=0.5):
        raise RuntimeError("simulated DB corruption")

    monkeypatch.setattr(latency_tracker, "get_stats", raising_get_stats)

    # Must not raise.
    await observability_collector._collect_execution_latency()

    latency_calls = [
        c for c in captured if c[0] == CAT_EXECUTION and c[1] == "latency"
    ]
    assert latency_calls == [], (
        f"no execution.latency metric should be recorded when get_stats "
        f"raises; got {latency_calls}"
    )


# ── (9) system.db_connections ─────────────────────────────────────────────────
async def test_db_connections_emits_liveness_flag(monkeypatch):
    """``_collect_db_connections`` emits ``1.0`` when the DB backend is
    live (``is_postgres or is_sqlite``), else ``0.0``.

    The test sets the backend to ``SQLITE`` → ``is_sqlite=True`` so the
    metric value is ``1.0`` and the metadata records ``backend="sqlite"``
    + ``is_postgres=False``.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.database_manager import db_manager, DatabaseBackend
    # Force sqlite backend.
    original_backend = db_manager._status.backend
    db_manager._status.backend = DatabaseBackend.SQLITE
    try:
        await observability_collector._collect_db_connections()
    finally:
        db_manager._status.backend = original_backend

    db_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "db_connections"
    ]
    assert len(db_calls) == 1, (
        f"expected exactly 1 system.db_connections call; got {len(db_calls)}"
    )
    cat, name, value, metadata = db_calls[0]
    assert value == pytest.approx(1.0), (
        f"sqlite backend must emit 1.0 (live); got {value}"
    )
    assert metadata.get("backend") == "sqlite"
    assert metadata.get("is_postgres") is False


async def test_db_connections_emits_zero_when_backend_none(monkeypatch):
    """When the manager has explicitly committed to ``NONE`` (the rare
    pre-initialize state where neither ``is_postgres`` nor ``is_sqlite``
    is True), the metric must emit ``0.0``.

    Per the W21-9 semantics: ``is_sqlite`` returns True for both
    ``NONE`` and ``SQLITE`` states (so the PG path is never attempted
    until ``initialize()`` flips the flag). To exercise the ``0.0``
    branch the test sets ``is_postgres=False`` AND ``is_sqlite=False``
    explicitly via monkeypatch (rather than relying on the enum value).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.database_manager import db_manager
    # Monkeypatch the properties (read-only at runtime) so the
    # collector's ``is_postgres or is_sqlite`` check evaluates to False.
    monkeypatch.setattr(type(db_manager), "is_postgres",
                        property(lambda self: False))
    monkeypatch.setattr(type(db_manager), "is_sqlite",
                        property(lambda self: False))
    monkeypatch.setattr(type(db_manager), "backend_name",
                        property(lambda self: "none"))

    await observability_collector._collect_db_connections()

    db_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "db_connections"
    ]
    assert len(db_calls) == 1
    assert db_calls[0][2] == pytest.approx(0.0), (
        f"NONE backend must emit 0.0 (not live); got {db_calls[0][2]}"
    )
    assert db_calls[0][3].get("backend") == "none"


# ── (10) system.queue_health ─────────────────────────────────────────────────
async def test_queue_health_reads_pending_count(monkeypatch):
    """``_collect_queue_health`` reads ``job_queue.get_stats()["by_status"]
    ["pending"]`` and emits it as ``system.queue_health``.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.job_queue import job_queue
    # The JobQueue's get_stats reads directly from its SQLite db; rather
    # than inserting rows, monkeypatch the method to return a fixed
    # payload (the collector only reads the dict shape).
    monkeypatch.setattr(
        job_queue, "get_stats",
        lambda: {
            "total_jobs": 42,
            "by_status": {"pending": 3, "running": 1, "completed": 38},
            "workers_active": 2,
            "handlers_registered": ["retrain", "backtest", "export"],
        },
    )

    await observability_collector._collect_queue_health()

    queue_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "queue_health"
    ]
    assert len(queue_calls) == 1, (
        f"expected exactly 1 system.queue_health call; got "
        f"{len(queue_calls)}"
    )
    cat, name, value, metadata = queue_calls[0]
    assert value == pytest.approx(3.0), (
        f"queue_health must match the pending count (3); got {value}"
    )
    assert metadata.get("type") == "pending_jobs"
    assert metadata.get("total_jobs") == 42
    assert metadata.get("workers_active") == 2


async def test_queue_health_handles_missing_get_stats(monkeypatch):
    """``_collect_queue_health`` must NOT raise if ``job_queue`` lacks
    ``get_stats`` (the ``hasattr`` guard falls through to ``{}`` →
    pending=0).
    """
    captured = _make_capturing_record_metric(monkeypatch)

    from core.job_queue import job_queue

    # ``get_stats`` is an instance method on JobQueue. Monkeypatch a
    # property that raises AttributeError so ``hasattr`` returns False.
    cls = type(job_queue)

    def _raise_attr_error(self):
        raise AttributeError("simulated missing method")

    monkeypatch.setattr(cls, "get_stats", property(_raise_attr_error))

    await observability_collector._collect_queue_health()

    queue_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "queue_health"
    ]
    assert len(queue_calls) == 1, (
        f"expected exactly 1 system.queue_health call (count=0); got "
        f"{len(queue_calls)}"
    )
    assert queue_calls[0][2] == pytest.approx(0.0), (
        f"missing get_stats must fall through to pending=0; got "
        f"{queue_calls[0][2]}"
    )


# ── Step 2: _collect_cycle emits every new metric ───────────────────────────
async def test_collect_cycle_emits_all_new_metrics(monkeypatch):
    """A single ``_collect_cycle()`` pass must record at least one metric
    in each of the 9 new (category, name) pairs.

    The cycle's design contract: each ``_collect_*`` call is
    independently fault-tolerant. The test captures every
    ``record_metric`` call during a single ``_collect_cycle`` pass and
    asserts the 9 new pairs all appear.
    """
    captured = _make_capturing_record_metric(monkeypatch)

    # Patch external dependencies so the cycle doesn't make real network
    # calls (clob_client.health_check) or rely on optional subsystems.
    async def fake_health_check():
        return 88.0

    from core.clob_client import clob_client
    monkeypatch.setattr(clob_client, "health_check", fake_health_check)

    from core.ws_client import ws_client
    monkeypatch.setattr(ws_client, "_reconnect_count", 0, raising=False)

    from core.latency_tracker import latency_tracker
    monkeypatch.setattr(
        latency_tracker, "get_stats",
        lambda hours=0.5: {"n_records": 0},
    )

    # No strategies active → strategy bucket gets zero metrics. Add a
    # stub strategy so the strategy bucket also emits.
    from strategies.base import BaseStrategy

    class _Stub(BaseStrategy):
        name = "stub_for_cycle_test"

        async def _run(self):
            pass

    from strategies.registry import strategy_registry
    monkeypatch.setattr(
        strategy_registry, "get_active_instances",
        lambda: {"stub_for_cycle_test": _Stub()},
    )

    await observability_collector._collect_cycle()

    names_touched = {(cat, name) for cat, name, _, _ in captured}

    expected = {
        (CAT_DATA_SOURCE, "latency"),
        (CAT_DATA_SOURCE, "reconnects"),
        (CAT_BOT, "errors"),
        (CAT_BOT, "actions"),
        (CAT_STRATEGY, "evaluations"),
        (CAT_STRATEGY, "signals"),
        (CAT_STRATEGY, "rejects"),
        (CAT_EXECUTION, "latency"),
        (CAT_SYSTEM, "db_connections"),
        (CAT_SYSTEM, "queue_health"),
    }
    missing = expected - names_touched
    assert not missing, (
        f"_collect_cycle must record all 9 new (category, name) pairs + "
        f"the strategy triple; missing: {sorted(missing)}; touched: "
        f"{sorted(names_touched)}"
    )


# ── Step 3: /api/observability endpoint returns the new metrics ─────────────
async def test_health_report_includes_all_new_metrics_after_cycle(monkeypatch):
    """The dashboard's ``GET /api/observability`` endpoint reads
    ``observability.get_health_report()`` which returns the latest value
    per (category, name) pair. After a single ``_collect_cycle()`` pass,
    the report's ``categories`` dict must include every new metric.

    This is the Step 3 spec test — "Ensure the /api/observability
    endpoint returns these new metrics". The endpoint delegates to
    ``get_health_report``; the test invokes the latter directly to
    avoid the FastAPI TestClient overhead (the endpoint is a thin
    wrapper around ``get_health_report`` — covered separately by
    ``tests/test_observability.py``).
    """
    # Use the real record_metric (don't capture) so the rows persist
    # for get_health_report to read back.
    async def fake_health_check():
        return 99.0

    from core.clob_client import clob_client
    monkeypatch.setattr(clob_client, "health_check", fake_health_check)

    from core.ws_client import ws_client
    monkeypatch.setattr(ws_client, "_reconnect_count", 5, raising=False)

    from core.latency_tracker import latency_tracker
    monkeypatch.setattr(
        latency_tracker, "get_stats",
        lambda hours=0.5: {"n_records": 0},
    )

    from strategies.base import BaseStrategy

    class _Stub(BaseStrategy):
        name = "stub_for_health_report"

        async def _run(self):
            pass

    from strategies.registry import strategy_registry
    monkeypatch.setattr(
        strategy_registry, "get_active_instances",
        lambda: {"stub_for_health_report": _Stub()},
    )

    # Drop the cache key so the next read doesn't return a stale report.
    try:
        from core.cache import observability_cache
        observability_cache.clear()
    except Exception:
        pass

    await observability_collector._collect_cycle()

    # Clear again — the collector wrote rows; the cached report from a
    # prior test (if any) must not shadow the fresh data.
    try:
        from core.cache import observability_cache
        observability_cache.clear()
    except Exception:
        pass

    report = await get_health_report()

    # Each new metric must appear in the report's categories dict.
    expected_in_report = [
        (CAT_DATA_SOURCE, "latency"),
        (CAT_DATA_SOURCE, "reconnects"),
        (CAT_BOT, "errors"),
        (CAT_BOT, "actions"),
        (CAT_STRATEGY, "evaluations"),
        (CAT_STRATEGY, "signals"),
        (CAT_STRATEGY, "rejects"),
        (CAT_EXECUTION, "latency"),
        (CAT_SYSTEM, "db_connections"),
        (CAT_SYSTEM, "queue_health"),
    ]
    missing = []
    for cat, name in expected_in_report:
        bucket = report["categories"].get(cat, {})
        if name not in bucket:
            missing.append((cat, name))
    assert not missing, (
        f"get_health_report must include every new (category, name) pair "
        f"after a _collect_cycle pass; missing: {missing}; report "
        f"categories: {list(report['categories'].keys())}"
    )


# ── METRIC_NAMES catalog includes every new metric ───────────────────────────
def test_metric_names_catalog_includes_every_new_metric():
    """``core.observability.METRIC_NAMES`` is the single source of truth
    for the canonical metric surface; the 9 new metrics must all appear
    in it so a future task grepping for the canonical names finds them
    in one place.
    """
    expected = {
        (CAT_DATA_SOURCE, "latency"),
        (CAT_DATA_SOURCE, "reconnects"),
        (CAT_BOT, "errors"),
        (CAT_BOT, "actions"),
        (CAT_STRATEGY, "evaluations"),
        (CAT_STRATEGY, "signals"),
        (CAT_STRATEGY, "rejects"),
        (CAT_EXECUTION, "latency"),
        (CAT_SYSTEM, "db_connections"),
        (CAT_SYSTEM, "queue_health"),
    }
    actual = {
        (cat, name)
        for cat, names in METRIC_NAMES.items()
        for name in names
    }
    missing = expected - actual
    assert not missing, (
        f"METRIC_NAMES catalog must include every new metric; missing: "
        f"{sorted(missing)}"
    )


# ── DataStore.record_error / record_action unit tests ────────────────────────
async def test_data_store_record_error_and_count_since():
    """``store.record_error`` appends a timestamp; ``get_error_count_since``
    returns the count of entries with ``timestamp >= since_ts``.
    """
    store._errors.clear()  # belt-and-braces with the autouse fixture
    assert store.get_error_count_since(time.time() - 30) == 0
    await store.record_error()
    await store.record_error()
    assert store.get_error_count_since(time.time() - 30) == 2
    # Stale entry — must NOT be counted.
    store._errors.append(time.time() - 120)
    assert store.get_error_count_since(time.time() - 30) == 2, (
        "stale entry must NOT be counted in the 30s window"
    )


async def test_data_store_record_action_and_count_since():
    store._actions.clear()
    assert store.get_action_count_since(time.time() - 30) == 0
    await store.record_action()
    assert store.get_action_count_since(time.time() - 30) == 1


async def test_data_store_error_cap_drops_oldest():
    """When the cap is reached, the oldest 10% of entries are dropped."""
    store._errors.clear()
    original_cap = store._ERROR_CAP
    store._ERROR_CAP = 100  # temporarily lower the cap for the test
    try:
        for _ in range(105):
            await store.record_error()
        # After the overflow, ~10% (10 entries) should be dropped → ~95 left.
        assert len(store._errors) <= 100, (
            f"cap must be enforced; got {len(store._errors)}"
        )
        assert len(store._errors) == 95, (
            f"drop-oldest-10% policy must leave 95 entries after 105 "
            f"appends at cap=100; got {len(store._errors)}"
        )
    finally:
        store._ERROR_CAP = original_cap


# ── ClobClient.health_check unit test ────────────────────────────────────────
async def test_clob_client_health_check_returns_latency_ms(monkeypatch):
    """``clob_client.health_check()`` returns the round-trip latency in
    milliseconds of an unauthenticated ``GET /markets`` call.

    The test fakes the underlying ``httpx.AsyncClient`` so no real
    network call is made.
    """
    from core.clob_client import ClobClient

    class _FakeResponse:
        def raise_for_status(self):
            pass

    class _FakeClient:
        async def get(self, path):
            return _FakeResponse()

    client = ClobClient()

    async def fake_ensure_http():
        return _FakeClient()

    monkeypatch.setattr(client, "_ensure_http", fake_ensure_http)

    # Stub out the clock so the latency math is deterministic.
    import core.clob_client as clob_module
    counter = {"n": 0}

    def fake_time():
        counter["n"] += 1
        # First call returns 0.0, second returns 0.1 → latency = 100 ms.
        return 0.0 if counter["n"] == 1 else 0.1

    monkeypatch.setattr(clob_module.time, "time", fake_time)

    latency = await client.health_check()
    assert latency == pytest.approx(100.0, rel=1e-3), (
        f"health_check must return the round-trip latency in ms; got {latency}"
    )


async def test_clob_client_health_check_propagates_http_error(monkeypatch):
    """``clob_client.health_check()`` raises on HTTP error so the
    observability collector's ``except Exception`` branch can record
    the sentinel ``-1`` value.

    The test fakes an HTTP 503 response and asserts ``raise_for_status``
    propagates a real exception (not swallowed).
    """
    from core.clob_client import ClobClient

    class _FakeErrorResponse:
        def raise_for_status(self):
            raise RuntimeError("HTTP 503")

    class _FakeClient:
        async def get(self, path):
            return _FakeErrorResponse()

    client = ClobClient()

    async def fake_ensure_http():
        return _FakeClient()

    monkeypatch.setattr(client, "_ensure_http", fake_ensure_http)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await client.health_check()
