"""tests/test_ingestion_wiring.py — W32-2 wiring tests for the unified
ingestion pipeline + health monitor integration into the FastAPI
server lifespan, the ``/api/status`` endpoint, and the
``core.observability_collector`` cycle.

Verifies the four contract surfaces the W32-2 task spec requires:

  (1) **Startup wiring** — the lifespan startup section awaits
      ``ingestion_pipeline.start()`` AND
      ``ingestion_health_monitor.start()`` (each in its own
      ``try/except`` block so a transient init failure never blocks
      boot, mirroring the ``live_fill_monitor`` /
      ``trade_tape_ingester`` pattern).

  (2) **Shutdown wiring** — the lifespan shutdown section awaits
      ``ingestion_pipeline.stop()`` AND
      ``ingestion_health_monitor.stop()`` so the lifecycle flag is
      flipped to ``False`` before the process exits (the
      ``/api/status`` snapshot stays honest post-shutdown).

  (3) **``/api/status`` ingestion block** — the existing
      ``GET /api/status`` endpoint includes an ``ingestion`` key
      carrying ``pipeline_running`` / ``sources_active`` /
      ``events_ingested`` / ``health`` subkeys sourced from the
      pipeline + health monitor singletons.

  (4) **Observability collector wiring** —
      ``core.observability_collector._collect_cycle`` invokes the
      new ``_collect_ingestion_metrics`` helper, which emits the
      three ``data_source`` metrics (``events_per_second`` /
      ``latency_ms`` / ``failed_records``) sourced from the
      pipeline singleton.

  (5) **Pipeline + health monitor lifecycle contracts** —
      ``Pipeline.start`` / ``stop`` flip ``is_running`` idempotently;
      ``Pipeline.events_per_second`` / ``avg_latency_ms`` /
      ``failed_count`` / ``active_sources`` / ``total_events``
      reflect the live ``process()`` counters;
      ``IngestionHealthMonitor.start`` / ``stop`` flip ``is_running``
      idempotently; ``IngestionHealthMonitor.get_summary`` aggregates
      per-source metrics into a cross-source summary dict.

Mock strategy
~~~~~~~~~~~~~

  * Source-inspection (``inspect.getsource(lifespan)``) for the
    lifespan startup / shutdown wiring tests — mirrors the
    ``tests/test_state_recovery_wiring.py`` pattern (spinning up
    the production lifespan is too heavy for a unit test; it would
    initialise TimescaleDB / paper_sim / market seeding taking >10 s
    and requiring external services). Substring assertions survive
    code reformatting.

  * ``fastapi.testclient.TestClient`` for the ``/api/status`` test
    — ``TestClient(app)`` (NOT ``with TestClient(app)``) skips the
    lifespan so each test stays sub-second. Mirrors the pattern in
    ``tests/test_api_resilience_wiring.py``.

  * Direct invocation of ``_collect_ingestion_metrics()`` for the
    observability test, with ``record_metric`` patched to a
    recorder so we can assert on the (category, name, value,
    metadata) tuples. Mirrors the pattern in
    ``tests/test_missing_metrics.py``.

  * Fresh ``Pipeline(vault=...)`` / ``IngestionHealthMonitor()``
    instances for the lifecycle tests so the module-level
    singletons (which live across the whole test session) are NOT
    mutated — mirrors the isolation strategy in
    ``tests/test_ingestion_infra.py``.

Async tests are collected via the per-test ``@pytest.mark.asyncio``
decorator (NOT the module-level ``pytestmark``) so the sync
``TestClient`` + source-inspection tests don't trip pytest-asyncio's
"marked but not async" warning. Mirrors the convention in
``tests/test_state_recovery_wiring.py``.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Mirrors the bootstrap pattern in ``tests/test_ingestion_infra.py`` and
# ``tests/test_state_recovery_wiring.py``. Belt-and-braces with the same
# redirect in ``tests/conftest.py`` (which pytest loads before this file).
_TMP_ROOT = Path("/tmp/ingestion_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "bot_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-w32-2",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``api.server`` / ``ingestion.*`` / ``core.*``). Mirrors the bootstrap
# pattern in every existing ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import
# mode inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — see the note below.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
_TESTS_DIR = str(Path(__file__).resolve().parent)
# Drop the ``tests/`` directory from sys.path entirely so the sibling
# W31-7 ``tests/ingestion/`` package can't shadow our top-level
# ``polymarket-bot/ingestion/`` package. Mirrors the pattern in
# ``tests/test_ws_ingestion.py``.
sys.path = [p for p in sys.path if p != _TESTS_DIR]
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# ── Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package ──────────────────────────────────────────
# A sibling wave (W31-7) created ``tests/ingestion/`` with an ``__init__.py``,
# turning it into a Python package also named ``ingestion``. Pytest's default
# ``prepend`` import mode inserts ``tests/`` at ``sys.path[0]`` during test
# collection, which means Python finds ``tests/ingestion/`` BEFORE our
# top-level ``polymarket-bot/ingestion/`` package. Clear any cached
# ``ingestion`` / ``ingestion.*`` module that points at the ``tests/ingestion/``
# directory so the next import resolves against the freshly-prepended
# ``_PROJECT_ROOT`` and finds our top-level package.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _cached = sys.modules.get(_mod_name)
    if _cached is None:
        continue
    _cached_file = getattr(_cached, "__file__", "") or ""
    if "tests/ingestion" in _cached_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

# Per-test asyncio marker (NOT module-level ``pytestmark``) so the SYNC
# ``TestClient`` + source-inspection tests below don't trip pytest-asyncio's
# "marked but not async" warning. Mirrors the same convention in
# ``tests/test_state_recovery_wiring.py``.
ASYNC = pytest.mark.asyncio

# ``conftest.py`` sets ``API_TOKEN`` via ``os.environ.setdefault`` BEFORE
# any project module is imported. The redirect block above sets a
# file-local ``API_TOKEN`` (``test-token-w32-2``) ONLY if conftest hasn't
# already set one — but conftest IS imported before this file (pytest
# orders ``tests/conftest.py`` before any sibling test module), so the
# value below reflects whatever the conftest-redirected env won with.
VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-w32-2")


# ── (1) Startup wiring ──────────────────────────────────────────────────────


def test_lifespan_startup_calls_ingestion_pipeline_start() -> None:
    """The lifespan startup section must ``await
    ingestion_pipeline.start()`` inside a try/except so the pipeline's
    lifecycle flag is flipped to ``True`` on every boot.

    Source-inspected (rather than executed) because the production
    lifespan is too heavy to spin up in a unit test — it would
    initialise TimescaleDB / paper_sim / market seeding / the watchdog
    subsystem, taking >10 s and requiring external services. Asserting
    against ``inspect.getsource(lifespan)`` keeps the test sub-second
    and survives code reformatting (the assertion matches substrings,
    not exact whitespace).
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    assert "from ingestion.pipeline import ingestion_pipeline" in src, (
        "lifespan startup must import ``ingestion_pipeline`` (the W32-2 "
        "operator-facing alias for the ``pipeline`` singleton) — the "
        "ingestion pipeline's lifecycle is gated by the lifespan, not by "
        "module-import time."
    )
    assert "await ingestion_pipeline.start()" in src, (
        "lifespan startup must ``await ingestion_pipeline.start()`` — "
        "W32-2 contract (1): the pipeline's ``is_running`` flag is the "
        "primary signal the ``/api/status`` endpoint surfaces for "
        "'ingestion layer alive'."
    )
    # Defensive try/except wrap so a transient init failure never blocks boot.
    assert "except Exception" in src, (
        "lifespan startup must wrap ``ingestion_pipeline.start()`` in "
        "try/except — W32-2 fail-soft contract (mirrors the "
        "``live_fill_monitor.start()`` / ``trade_tape_ingester.start()`` "
        "pattern in W18-2 / W20-7)."
    )
    assert "Ingestion pipeline started" in src, (
        "lifespan startup must log ``Ingestion pipeline started`` on "
        "success — the operator-facing log line is the primary signal "
        "the pipeline lifecycle hook ran."
    )


def test_lifespan_startup_calls_ingestion_health_monitor_start() -> None:
    """The lifespan startup section must ``await
    ingestion_health_monitor.start()`` inside a try/except so the
    health monitor's lifecycle flag is flipped to ``True`` on every
    boot.

    Belt-and-braces with the pipeline test above — both ingestion
    subsystems (pipeline + health monitor) must be wired into the
    same lifespan startup block so a single ``start()`` failure on
    either doesn't leave the other in a half-initialised state.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    assert "from ingestion.health import ingestion_health_monitor" in src, (
        "lifespan startup must import ``ingestion_health_monitor`` — "
        "W32-2 contract (1): the monitor's lifecycle flag is the "
        "primary signal the ``/api/status`` endpoint's "
        "``ingestion.health.is_running`` field surfaces."
    )
    assert "await ingestion_health_monitor.start()" in src, (
        "lifespan startup must ``await "
        "ingestion_health_monitor.start()`` — W32-2 contract (1)."
    )
    assert "Ingestion health monitor started" in src, (
        "lifespan startup must log ``Ingestion health monitor "
        "started`` on success — the operator-facing log line is the "
        "primary signal the monitor lifecycle hook ran."
    )


# ── (2) Shutdown wiring ─────────────────────────────────────────────────────


def test_lifespan_shutdown_calls_ingestion_pipeline_stop() -> None:
    """The lifespan shutdown section must ``await
    ingestion_pipeline.stop()`` so the pipeline's ``is_running`` flag
    is flipped to ``False`` before the process exits.

    Without this, the post-shutdown ``GET /api/status`` snapshot
    would falsely report ``pipeline_running: True`` even after the
    bot has fully torn down — confusing an operator who's looking
    at a stale dashboard.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    assert "await ingestion_pipeline.stop()" in src, (
        "lifespan shutdown must ``await ingestion_pipeline.stop()`` — "
        "W32-2 contract (2): the pipeline's lifecycle flag must be "
        "flipped to ``False`` before the process exits."
    )
    assert "await ingestion_health_monitor.stop()" in src, (
        "lifespan shutdown must also ``await "
        "ingestion_health_monitor.stop()`` — W32-2 contract (2): the "
        "monitor's lifecycle flag must be flipped alongside the "
        "pipeline's so the post-shutdown snapshot stays consistent."
    )
    assert "Ingestion pipeline stopped" in src, (
        "lifespan shutdown must log ``Ingestion pipeline stopped`` so "
        "the operator-facing log shows the lifecycle hook ran end-to-"
        "end (mirrors the startup-side ``Ingestion pipeline started`` "
        "log line)."
    )


def test_lifespan_shutdown_wraps_pipeline_stop_in_try_except() -> None:
    """The shutdown ``ingestion_pipeline.stop()`` call must be wrapped
    in ``try/except`` so a teardown failure (e.g. a transient SQLite
    hiccup) never blocks the rest of the lifespan shutdown.

    Mirrors the defensive pattern used by the sibling
    ``trade_tape_ingester.stop()`` / ``live_fill_monitor.stop()``
    blocks.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)
    # Locate the shutdown block (after ``yield``).
    yield_idx = src.find("yield")
    assert yield_idx != -1, "lifespan must contain a yield (separating startup from shutdown)"
    shutdown_src = src[yield_idx:]

    assert "ingestion_pipeline.stop()" in shutdown_src
    assert "Ingestion pipeline stop failed" in shutdown_src, (
        "lifespan shutdown must log a distinct error message when the "
        "pipeline ``stop()`` call fails — operators need a single "
        "log line to grep for 'shutdown failed' cases."
    )
    assert "except Exception" in shutdown_src


# ── (3) /api/status ingestion block ────────────────────────────────────────


@pytest.fixture
def client() -> Any:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_api_resilience_wiring.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_api_status_includes_ingestion_block(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """``GET /api/status`` must include an ``ingestion`` key carrying the
    four W32-2 contract subkeys (``pipeline_running`` /
    ``sources_active`` / ``events_ingested`` / ``health``).

    The block is sourced from the live
    ``ingestion_pipeline`` + ``ingestion_health_monitor`` singletons,
    so before any ``process()`` call has been made the values are
    well-defined zero-state (``pipeline_running=False`` /
    ``sources_active=0`` / ``events_ingested=0`` / ``health={...}``)
    rather than missing or ``None``. The endpoint is rate-limited
    under ``READ_LIMIT`` — TestClient rides through the limiter.
    """
    response = client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 from GET /api/status; got {response.status_code} "
        f"(body: {response.text[:500]})"
    )
    body = response.json()
    assert "ingestion" in body, (
        "GET /api/status response must include an ``ingestion`` key — "
        "W32-2 contract (3): the operator dashboard's primary ingestion-"
        "layer signal must be reachable without drilling into "
        "``/api/ingestion/health``."
    )
    ingestion = body["ingestion"]
    assert isinstance(ingestion, dict)
    # The four contract subkeys must be present.
    for key in ("pipeline_running", "sources_active", "events_ingested", "health"):
        assert key in ingestion, (
            f"GET /api/status response's ``ingestion`` block must "
            f"include ``{key}`` — W32-2 contract (3)."
        )
    # The pipeline_running flag must be a boolean (NOT null / missing).
    assert isinstance(ingestion["pipeline_running"], bool)
    # sources_active + events_ingested must be ints (NOT null).
    assert isinstance(ingestion["sources_active"], int)
    assert isinstance(ingestion["events_ingested"], int)
    # health must be a dict (the cross-source summary shape).
    assert isinstance(ingestion["health"], dict)


def test_api_status_health_block_carries_summary_keys(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """The ``ingestion.health`` subkey must carry the
    ``IngestionHealthMonitor.get_summary()`` shape — at minimum the
    cross-source aggregate fields (``sources`` / ``available_sources``
    / ``events_received`` / ``events_failed`` / ``error_rate`` /
    ``throughput_eps`` / ``avg_latency_ms`` / ``dlq_depth`` /
    ``last_event_at`` / ``is_running``).

    The zero-state (no events yet) is acceptable for every numeric
    field (0.0 / 0 / False) — the test only asserts the SHAPE is
    present so a future metric addition doesn't silently break the
    contract.
    """
    response = client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    health = body["ingestion"]["health"]
    expected_keys = {
        "sources",
        "available_sources",
        "events_received",
        "events_failed",
        "error_rate",
        "throughput_eps",
        "avg_latency_ms",
        "dlq_depth",
        "last_event_at",
        "is_running",
        "alerts",
    }
    missing = expected_keys - set(health.keys())
    assert not missing, (
        f"GET /api/status ingestion.health is missing keys: {sorted(missing)} "
        "— W32-2 contract (3) requires the cross-source summary shape from "
        "``IngestionHealthMonitor.get_summary()``."
    )


# ── (4) Observability collector wiring ──────────────────────────────────────


@ASYNC
async def test_collect_cycle_invokes_ingestion_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """``core.observability_collector._collect_cycle`` must call the new
    ``_collect_ingestion_metrics`` helper so the pipeline's three
    ``data_source`` metrics land in the observability store at the
    same 30 s cadence as every other subsystem.

    The test patches ``_collect_ingestion_metrics`` with a recorder
    (rather than calling ``_collect_cycle`` end-to-end against the
    real SQLite store) so the assertion is scoped to the W32-2
    contract surface — "the cycle invokes the ingestion collector"
    — without paying the cost of running the other 12 collectors.
    """
    import core.observability_collector as observability_collector

    call_count = {"n": 0}

    async def _record_ingestion() -> None:
        call_count["n"] += 1

    monkeypatch.setattr(
        observability_collector,
        "_collect_ingestion_metrics",
        _record_ingestion,
    )
    # Patch every other ``_collect_*`` to a no-op so the cycle runs
    # in microseconds and doesn't hit the SQLite store / psutil.
    for _name in (
        "_collect_data_source_metrics",
        "_collect_execution_metrics",
        "_collect_ml_metrics",
        "_collect_system_metrics",
        "_collect_data_source_latency",
        "_collect_data_source_reconnects",
        "_collect_bot_errors",
        "_collect_bot_actions",
        "_collect_strategy_metrics",
        "_collect_execution_latency",
        "_collect_db_connections",
        "_collect_queue_health",
    ):
        async def _noop() -> None:
            return None
        monkeypatch.setattr(observability_collector, _name, _noop)
    # Patch the final ``record_metric`` heartbeat to a no-op so the
    # test doesn't write to the SQLite observability store.
    async def _noop_record(*args: Any, **kwargs: Any) -> None:
        return None
    monkeypatch.setattr(observability_collector, "record_metric", _noop_record)

    await observability_collector._collect_cycle()

    assert call_count["n"] == 1, (
        "_collect_cycle must invoke ``_collect_ingestion_metrics`` "
        "exactly once per cycle — W32-2 contract (4)."
    )


@ASYNC
async def test_collect_ingestion_metrics_emits_three_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_collect_ingestion_metrics`` must emit the three ``data_source``
    metrics named in the W32-2 spec: ``events_per_second``,
    ``latency_ms``, and ``failed_records``.

    The test patches ``record_metric`` with a recorder so we can
    assert on the (category, name, value, metadata) tuples without
    touching the SQLite observability store. The pipeline singleton
    is reset to a fresh state (``reset_stats()`` + ``stop()``) before
    the test so prior tests' counters don't leak in.
    """
    import core.observability_collector as observability_collector
    from ingestion.pipeline import ingestion_pipeline

    # Reset the pipeline counters so the test starts from a known
    # zero state (the singleton is shared across the test session).
    ingestion_pipeline.reset_stats()
    # Stop + restart so the lifecycle flag is set (the collector
    # reads ``is_running`` / ``active_sources`` as metadata).
    await ingestion_pipeline.stop()
    await ingestion_pipeline.start()

    captured: list[tuple[str, str, float, dict[str, Any]]] = []

    async def _capturing_record(
        category: str, name: str, value: float, **metadata: Any
    ) -> None:
        captured.append((category, name, value, dict(metadata)))

    monkeypatch.setattr(
        observability_collector, "record_metric", _capturing_record
    )

    await observability_collector._collect_ingestion_metrics()

    # The three metrics must be emitted.
    names = [t[1] for t in captured]
    assert "events_per_second" in names, (
        "_collect_ingestion_metrics must emit ``data_source.events_per_second`` "
        "— W32-2 contract (4)."
    )
    assert "latency_ms" in names, (
        "_collect_ingestion_metrics must emit ``data_source.latency_ms`` — "
        "W32-2 contract (4)."
    )
    assert "failed_records" in names, (
        "_collect_ingestion_metrics must emit ``data_source.failed_records`` — "
        "W32-2 contract (4)."
    )
    # Every metric must land in the ``data_source`` category.
    for category, name, value, metadata in captured:
        assert category == "data_source", (
            f"ingestion metric {name!r} must be in the ``data_source`` "
            f"category (got {category!r}) — W32-2 contract (4) lands the "
            "metrics alongside the existing book_poller / ws_client "
            "data_source metrics."
        )
        # Each metric must carry the pipeline's ``pipeline_running`` +
        # ``active_sources`` metadata so the dashboard can render
        # "pipeline alive + N sources connected" without a separate
        # lookup.
        assert "pipeline_running" in metadata, (
            f"ingestion metric {name!r} must carry ``pipeline_running`` "
            "metadata — W32-2 contract (4)."
        )
        assert "active_sources" in metadata, (
            f"ingestion metric {name!r} must carry ``active_sources`` "
            "metadata — W32-2 contract (4)."
        )

    # Tear down so the singleton state doesn't leak into a sibling test.
    await ingestion_pipeline.stop()
    ingestion_pipeline.reset_stats()


# ── (5) Pipeline + health monitor lifecycle contracts ──────────────────────


@ASYNC
async def test_pipeline_start_stop_flips_is_running_idempotently() -> None:
    """``Pipeline.start()`` / ``stop()`` must flip ``is_running`` and
    be idempotent — calling ``start()`` twice (or ``stop()`` twice)
    is a no-op rather than a state corruption.

    Uses a fresh ``Pipeline()`` instance (NOT the module-level
    singleton) so the singleton's state isn't mutated by the test —
    mirrors the isolation pattern in ``tests/test_ingestion_infra.py``.
    """
    from ingestion.pipeline import Pipeline

    p = Pipeline(vault=None, router=lambda rec: None)
    # Fresh instance is NOT running.
    assert p.is_running is False

    await p.start()
    assert p.is_running is True

    # Idempotent: a second ``start()`` is a no-op.
    await p.start()
    assert p.is_running is True

    await p.stop()
    assert p.is_running is False

    # Idempotent: a second ``stop()`` is a no-op.
    await p.stop()
    assert p.is_running is False


@ASYNC
async def test_pipeline_properties_track_process_counters() -> None:
    """``Pipeline.total_events`` / ``active_sources`` /
    ``events_per_second`` / ``avg_latency_ms`` / ``failed_count``
    must reflect the live ``process()`` counters.

    The test processes a few synthetic records through a fresh
    ``Pipeline`` instance (with a no-op router + a no-op vault) and
    asserts the properties advance after each call. The
    ``events_per_second`` window is 60 s so the test stays sub-second
    (the timestamps land in the rolling window immediately).
    """
    from ingestion.pipeline import Pipeline, PipelineRecord

    # Use a no-op vault via a tiny stub — the default ``raw_vault``
    # singleton touches SQLite, which we don't want in a unit test.
    class _NoopVault:
        def record_observation(self, **kwargs: Any) -> str | None:
            return "obs-test"

    # Use a custom validator that returns "valid" for every record so
    # the test doesn't depend on the W24-4 ``data_validator`` rules
    # (which require ``token_id`` / ``best_bid`` / ``best_ask`` fields
    # and would mark our synthetic snapshot ``invalid``).
    def _always_valid(rec: PipelineRecord) -> tuple[str, str, dict[str, Any], float]:
        return ("valid", "", {}, 1.0)

    # Disable the W33-3 contract validator + dead-letter queue so the
    # synthetic snapshot (which lacks ``token_id`` / ``best_bid`` /
    # ``best_ask``) isn't reclassified as ``invalid`` after the
    # custom validator returns ``valid``. Passing ``False`` is the
    # documented escape hatch (see ``Pipeline.__init__`` docstring).
    p = Pipeline(
        vault=_NoopVault(),
        validator=_always_valid,
        router=lambda rec: None,
        contract_validator=False,
        dead_letter_queue=False,
    )

    assert p.total_events == 0
    assert p.active_sources == 0
    assert p.failed_count == 0
    assert p.events_per_second == 0.0
    assert p.avg_latency_ms == 0.0

    # Process one valid record from source "clob_rest".
    p.process(
        source="clob_rest",
        source_id="snap-1",
        event_type="snapshot",
        raw_payload={"bids": [["0.50", "10"]], "asks": [["0.52", "5"]]},
    )
    assert p.total_events == 1
    assert p.active_sources == 1
    assert p.failed_count == 0  # valid record → no failures.
    assert p.events_per_second > 0.0  # one sample in the 60 s window.
    assert p.avg_latency_ms >= 0.0  # at least one latency sample.

    # Process a record from a second source — active_sources advances.
    p.process(
        source="gamma_rest",
        source_id="market-1",
        event_type="market_info",
        raw_payload={"id": "market-1"},
    )
    assert p.total_events == 2
    assert p.active_sources == 2

    # Process a record from the FIRST source again — active_sources
    # does NOT advance (set semantics).
    p.process(
        source="clob_rest",
        source_id="snap-2",
        event_type="snapshot",
        raw_payload={"bids": [["0.51", "10"]], "asks": [["0.53", "5"]]},
    )
    assert p.total_events == 3
    assert p.active_sources == 2  # unchanged — set semantics.


@ASYNC
async def test_health_monitor_start_stop_flips_is_running_idempotently() -> None:
    """``IngestionHealthMonitor.start()`` / ``stop()`` must flip
    ``is_running`` and be idempotent — mirrors the pipeline test.
    """
    from ingestion.health import IngestionHealthMonitor

    m = IngestionHealthMonitor()
    assert m.is_running is False

    await m.start()
    assert m.is_running is True

    # Idempotent.
    await m.start()
    assert m.is_running is True

    await m.stop()
    assert m.is_running is False

    # Idempotent.
    await m.stop()
    assert m.is_running is False


def test_health_monitor_get_summary_zero_state() -> None:
    """``IngestionHealthMonitor.get_summary()`` must return the
    well-defined zero-state shape when no events have been recorded
    (rather than ``{}`` or ``None``) so the ``/api/status`` endpoint
    can render the ingestion block even on a fresh boot.
    """
    from ingestion.health import IngestionHealthMonitor

    m = IngestionHealthMonitor()
    summary = m.get_summary()
    expected_keys = {
        "sources",
        "available_sources",
        "events_received",
        "events_failed",
        "error_rate",
        "throughput_eps",
        "avg_latency_ms",
        "dlq_depth",
        "last_event_at",
        "is_running",
        "alerts",
    }
    assert set(summary.keys()) == expected_keys
    assert summary["sources"] == 0
    assert summary["available_sources"] == 0
    assert summary["events_received"] == 0
    assert summary["events_failed"] == 0
    assert summary["error_rate"] == 0.0
    assert summary["throughput_eps"] == 0.0
    assert summary["avg_latency_ms"] == 0.0
    assert summary["dlq_depth"] == 0
    assert summary["last_event_at"] == 0.0
    assert summary["is_running"] is False
    assert summary["alerts"] == 0


def test_health_monitor_get_summary_aggregates_multiple_sources() -> None:
    """``get_summary()`` must aggregate per-source metrics across
    every source — totals for events_received / events_failed / dlq_depth,
    a weighted error_rate, summed throughput_eps, and a max
    last_event_at (so a single busy source keeps the freshness signal
    alive even when every other source is silent).
    """
    from ingestion.health import IngestionHealthMonitor

    m = IngestionHealthMonitor()
    # Two sources, 10 events each, 1 failure on the first source.
    for _ in range(9):
        m.record_event("clob_rest")
    m.record_event("clob_rest", success=False, error="simulated 5xx")
    for _ in range(10):
        m.record_event("gamma_rest")
    # Mark one source unavailable.
    m.mark_unavailable("gamma_rest")

    summary = m.get_summary()
    assert summary["sources"] == 2
    assert summary["available_sources"] == 1  # gamma_rest marked unavailable.
    assert summary["events_received"] == 20
    assert summary["events_failed"] == 1
    assert summary["error_rate"] == 1 / 20
    assert summary["last_event_at"] > 0.0


# ── (6) Cross-cutting: ingestion_pipeline alias is the same object as pipeline ─


def test_ingestion_pipeline_alias_references_same_singleton() -> None:
    """``ingestion_pipeline`` must be an alias for ``pipeline`` (NOT
    a second ``Pipeline()`` construction) so the lifecycle calls in
    ``api/server.py``'s lifespan mutate the same singleton the
    ``/api/status`` endpoint + observability collector read from.
    """
    from ingestion.pipeline import ingestion_pipeline, pipeline

    assert ingestion_pipeline is pipeline, (
        "``ingestion_pipeline`` must be the SAME object as ``pipeline`` — "
        "the operator-facing alias is a name-only convenience; constructing "
        "a second ``Pipeline()`` would split the lifecycle state from the "
        "read state."
    )


def test_ingestion_pipeline_exported_from_pipeline_module() -> None:
    """``ingestion_pipeline`` must be importable from the top-level
    ``ingestion.pipeline`` module — the lifespan block does
    ``from ingestion.pipeline import ingestion_pipeline`` so the
    symbol must be in the module's namespace (and ideally in
    ``__all__``).
    """
    import sys

    # Resolve the module via ``sys.modules`` (NOT ``import
    # ingestion.pipeline as pipeline_mod``) because the latter can be
    # shadowed by the package-level ``pipeline`` attribute re-export
    # (``from ingestion.pipeline import pipeline`` in
    # ``ingestion/__init__.py``). ``sys.modules["ingestion.pipeline"]``
    # always resolves to the actual module object regardless of the
    # package-level attribute shadow.
    assert "ingestion.pipeline" in sys.modules, (
        "``ingestion.pipeline`` module must be importable — "
        "W32-2 contract (1): the lifespan block imports "
        "``ingestion_pipeline`` from this module name."
    )
    pipeline_mod = sys.modules["ingestion.pipeline"]
    assert hasattr(pipeline_mod, "ingestion_pipeline"), (
        "``ingestion.pipeline`` must export ``ingestion_pipeline`` — "
        "W32-2 contract (1): the lifespan import resolves against this "
        "module name."
    )
    assert "ingestion_pipeline" in getattr(pipeline_mod, "__all__", []), (
        "``ingestion_pipeline`` must be listed in ``ingestion.pipeline.__all__`` "
        "so it's surfaced as a public symbol of the module."
    )
