"""
core/observability_collector.py — Background Auto-Collector for System Health Metrics.

A single long-running asyncio task that periodically (every 30 s) pulls
operational stats from every active subsystem and persists them through
``core.observability.record_metric()`` so the unified health dashboard at
``GET /api/observability`` always has fresh data without each subsystem
having to instrument itself.

Sources read every cycle:

  ┌──────────────┬───────────────────────────────────────────────────────────────┐
  │ category     │ source → metrics emitted                                       │
  ├──────────────┼───────────────────────────────────────────────────────────────┤
  │ data_source  │ core.book_poller.book_poller.stats                            │
  │              │   → updates (success_count), errors (error_count),            │
  │              │     staleness (max seconds since book.updated_at),            │
  │              │     tracked_tokens (tier1+tier2)                              │
  ├──────────────┼───────────────────────────────────────────────────────────────┤
  │ execution    │ core.data_store.store                                        │
  │              │   → submissions (open_orders count), fills (trades count),    │
  │              │     rejections (CANCELLED in order_history),                 │
  │              │     slippage (avg pnl/recent trades proxy),                   │
  │              │     positions, paper_balance, daily_pnl                       │
  ├──────────────┼───────────────────────────────────────────────────────────────┤
  │ ml           │ ml.model.ml_model + ml.drift_detector.drift_detector           │
  │              │   → prediction_distribution (max adaptive weight),            │
  │              │     drift (PSI score), brier_score, ece, roc_auc,            │
  │              │     is_fitted, n_updates, seconds_since_last_trained          │
  ├──────────────┼───────────────────────────────────────────────────────────────┤
  │ system       │ psutil                                                       │
  │              │   → cpu_percent, memory_percent, memory_used_mb              │
  ├──────────────┼───────────────────────────────────────────────────────────────┤
  │ bot          │ collector heartbeat                                          │
  │              │   → cycles (1 per cycle — "collector alive" signal)          │
  └──────────────┴───────────────────────────────────────────────────────────────┘

Lifecycle:

  - ``await start_collector()`` schedules the background loop as a named
    asyncio task (``observability-collector``) and returns immediately. The
    first collection pass runs before the first 30 s sleep so the dashboard
    isn't empty on boot. Idempotent — calling it twice is a no-op.

  - ``await stop_collector()`` cancels the task cleanly. Safe to call even
    if the collector was never started.

  - ``register_routes(app)`` is the FastAPI wiring hook. Unlike sibling
    modules' ``register_routes`` functions, it adds NO HTTP routes —
    instead it wraps the app's existing ``lifespan`` context manager so
    ``start_collector()`` is awaited after the app's own startup completes
    (guaranteeing book_poller / store / ml_model are initialised before
    the first collection pass) and ``stop_collector()`` is awaited on
    shutdown (before subsystem teardown). This is necessary because
    FastAPI's ``on_event("startup")`` handlers do NOT fire when the app
    is constructed with ``lifespan=...`` (verified on FastAPI 0.128).

Every collection call is fully self-healing: any subsystem that fails to
import or raises during attribute access is logged at ``debug`` level and
skipped — the rest of the cycle still runs. An observability hiccup can
never break the trading pipeline (mirrors the ``core.observability``
contract).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from core.observability import (
    CAT_BOT,
    CAT_DATA_SOURCE,
    CAT_EXECUTION,
    CAT_ML,
    CAT_SYSTEM,
    record_metric,
)

log = logging.getLogger(__name__)

# 30-second cadence per the T7 spec — balances dashboard freshness against
# SQLite write amplification (each cycle emits ~18 metrics = 18 INSERTs).
COLLECTION_INTERVAL_SECONDS: float = 30.0

# How many recent trades to average for the slippage proxy. 50 ≈ one
# minute of fills at peak activity — small enough to be responsive,
# large enough to dampen single-trade noise.
_SLIPPAGE_WINDOW: int = 50

# Module-level handle to the running collector task. ``start_collector``
# populates this; ``stop_collector`` cancels and clears it. Idempotent
# on both sides.
_collector_task: asyncio.Task[None] | None = None

# Guard against double-wrapping the lifespan if ``register_routes`` is
# called more than once (defensive — should not happen in practice).
_lifespan_wrapped: bool = False


# ── Per-subsystem collectors ─────────────────────────────────────────────────
# Each is a standalone async function so a single failure in one source
# never prevents the others from being collected. Local imports keep the
# module load-time surface minimal (no transitive import of sklearn /
# httpx / psutil at module import — only when the collector actually runs).


async def _collect_data_source_metrics() -> None:
    """
    Emit ``data_source`` metrics from ``core.book_poller.book_poller.stats``.

    Maps the poller's cumulative counters to the canonical metric names
    declared in ``core.observability.METRIC_NAMES[CAT_DATA_SOURCE]``:
    ``updates`` (success_count), ``staleness`` (worst-case seconds since
    any tracked book was refreshed), plus an ``errors`` and
    ``tracked_tokens`` extension that lands in the same ``data_source``
    bucket (the recorder accepts any name; non-canonical names are
    surfaced alongside the canonical ones in the health report).
    """
    try:
        from core.book_poller import book_poller  # local import — defer httpx load
        from core.data_store import store

        stats = book_poller.stats
        success_count = float(stats.get("success_count", 0))
        error_count = float(stats.get("error_count", 0))
        total_tracked = float(stats.get("total_tracked", 0))

        await record_metric(
            CAT_DATA_SOURCE, "updates", success_count, source="clob_rest"
        )
        await record_metric(
            CAT_DATA_SOURCE, "errors", error_count, source="clob_rest"
        )
        await record_metric(
            CAT_DATA_SOURCE, "tracked_tokens", total_tracked,
            tier1=int(stats.get("tier1_tokens", 0)),
            tier2=int(stats.get("tier2_tokens", 0)),
        )

        # Staleness: seconds since each tracked book was last refreshed.
        # Emit the WORST case (max across all tracked books) — that's the
        # health signal the dashboard cares about ("is any market going
        # blind?"). An empty book map emits 0.0 (no markets → no staleness).
        now = time.time()
        book_staleness_values: list[float] = []
        # Iterate over a snapshot of the books dict under the store's lock
        # so we don't race with concurrent ``update_order_book`` writes.
        async with store._lock:
            for book in store.order_books.values():
                if book.updated_at:
                    book_staleness_values.append(max(0.0, now - book.updated_at))
        max_staleness = max(book_staleness_values) if book_staleness_values else 0.0
        await record_metric(
            CAT_DATA_SOURCE, "staleness", round(max_staleness, 3),
            book_count=len(book_staleness_values),
        )
    except Exception as e:
        log.debug("[observability_collector] data_source collection failed: %s", e)


async def _collect_execution_metrics() -> None:
    """
    Emit ``execution`` metrics from ``core.data_store.store``.

    Counts derive from ``store.open_orders`` (live orders), ``store.trades``
    (filled orders), and ``store.order_history`` (terminal orders). The
    slippage proxy is the mean per-trade PnL over the most recent
    ``_SLIPPAGE_WINDOW`` fills — a positive value means recent trades are
    net profitable (a coarse but useful "is execution leaking?" signal).
    Precise per-fill slippage is tracked separately by
    ``core.execution_quality.record_execution``.
    """
    try:
        from core.data_store import OrderStatus, store

        # Snapshot the relevant collections under the store's lock so the
        # counts are internally consistent (a trader could submit / fill
        # between reads otherwise). The lock is held only for the duration
        # of the snapshot, not while we persist to SQLite.
        async with store._lock:
            open_orders_count = len(store.open_orders)
            trades_count = len(store.trades)
            positions_count = len(store.positions)
            order_history = list(store.order_history)
            recent_trades = store.trades[-_SLIPPAGE_WINDOW:]
            paper_balance = float(store.paper_balance)
            daily_pnl = float(store.daily_pnl)
            peak_equity = float(store.peak_equity)
            kill_switch = bool(store.kill_switch_active)

        cancelled_count = sum(
            1 for o in order_history if o.status == OrderStatus.CANCELLED
        )
        filled_in_history = sum(
            1 for o in order_history if o.status == OrderStatus.FILLED
        )

        await record_metric(
            CAT_EXECUTION, "submissions", float(open_orders_count),
            filled_in_history=filled_in_history,
        )
        await record_metric(CAT_EXECUTION, "fills", float(trades_count))
        await record_metric(CAT_EXECUTION, "rejections", float(cancelled_count))
        await record_metric(
            CAT_EXECUTION, "positions", float(positions_count),
        )
        await record_metric(
            CAT_EXECUTION, "paper_balance", round(paper_balance, 4),
        )
        await record_metric(
            CAT_EXECUTION, "daily_pnl", round(daily_pnl, 4),
            peak_equity=round(peak_equity, 4),
            kill_switch=kill_switch,
        )

        # Slippage proxy: mean per-trade PnL over the recent window. This
        # is a realised-edge proxy (positive = recent fills net profitable).
        # True slippage (signal_price vs fill_price) is tracked by
        # ``core.execution_quality`` and surfaced at GET /api/execution-quality.
        if recent_trades:
            avg_pnl = sum(float(t.pnl) for t in recent_trades) / len(recent_trades)
            await record_metric(
                CAT_EXECUTION, "slippage", round(avg_pnl, 6),
                window=len(recent_trades),
                proxy="mean_per_trade_pnl",
            )
    except Exception as e:
        log.debug("[observability_collector] execution collection failed: %s", e)


async def _collect_ml_metrics() -> None:
    """
    Emit ``ml`` metrics from ``ml.model.ml_model`` + ``ml.drift_detector``.

    The canonical ML metric names are ``inference_latency``,
    ``prediction_distribution``, and ``drift``. ``inference_latency`` is not
    instrumented by ``MarketMLModel.predict`` (the predict path doesn't
    record per-call timing), so it's emitted as ``0.0`` with metadata
    flagging it as uninstrumented — keeps the canonical bucket populated
    for the dashboard schema without fabricating a misleading number.
    The remaining metrics (brier_score, ece, roc_auc, is_fitted,
    n_updates, seconds_since_last_trained) are useful model-health
    extensions that land in the same ``ml`` bucket.
    """
    try:
        from ml.model import ml_model  # local import — defers sklearn load
    except Exception as e:
        log.debug("[observability_collector] ml_model import failed: %s", e)
        return

    try:
        # ── Canonical ML metrics ──────────────────────────────────────────
        # inference_latency: not tracked by MarketMLModel.predict; emit 0.0
        # with metadata so the canonical bucket is populated but the value
        # is interpretable. (Future hardening: wrap predict() with a timer.)
        await record_metric(
            CAT_ML, "inference_latency", 0.0,
            instrumented=False,
            note="MarketMLModel.predict does not record per-call latency",
        )

        # prediction_distribution: emit the max adaptive weight as a scalar
        # concentration metric (1.0 = single-model monopoly, 0.25 = perfectly
        # uniform 4-model blend). The full weights dict travels in metadata
        # so the dashboard can render the per-model breakdown.
        weights = ml_model.adaptive_weights or {}
        max_weight = max(weights.values()) if weights else 0.0
        await record_metric(
            CAT_ML, "prediction_distribution", round(float(max_weight), 4),
            weights=weights,
            lgbm_available=bool(getattr(ml_model, "lgbm_available", False)),
        )

        # drift: PSI score from the drift detector (0.0 = no drift detected).
        # Wrapped in its own try/except so a drift_detector init failure
        # doesn't drop the rest of the ML metrics.
        try:
            from ml.drift_detector import drift_detector
            psi = float(getattr(drift_detector, "last_psi", 0.0) or 0.0)
            status = str(getattr(drift_detector, "drift_status", "UNKNOWN"))
            await record_metric(
                CAT_ML, "drift", round(psi, 6),
                status=status,
                rolling_brier=getattr(drift_detector, "rolling_brier", None),
                ewma_brier=getattr(drift_detector, "ewma_brier", None),
            )
        except Exception as e:
            log.debug("[observability_collector] drift_detector read failed: %s", e)

        # ── Extension ML metrics (same bucket) ────────────────────────────
        await record_metric(
            CAT_ML, "brier_score", float(getattr(ml_model, "brier_score", 0.0))
        )
        await record_metric(
            CAT_ML, "ece", float(getattr(ml_model, "ece", 0.0))
        )
        await record_metric(
            CAT_ML, "roc_auc", float(getattr(ml_model, "roc_auc", 0.0))
        )
        await record_metric(
            CAT_ML, "is_fitted", 1.0 if getattr(ml_model, "is_fitted", False) else 0.0
        )
        await record_metric(
            CAT_ML, "n_updates", float(getattr(ml_model, "_n_updates", 0))
        )

        last_trained = float(getattr(ml_model, "_last_trained", 0.0) or 0.0)
        seconds_since_trained = max(0.0, time.time() - last_trained) if last_trained else 0.0
        await record_metric(
            CAT_ML, "seconds_since_last_trained", round(seconds_since_trained, 3),
            last_trained=last_trained,
            training_source=getattr(ml_model, "training_source", "unknown"),
            n_real_samples=getattr(ml_model, "n_real_samples", 0),
            n_synthetic_samples=getattr(ml_model, "n_synthetic_samples", 0),
        )
    except Exception as e:
        log.debug("[observability_collector] ml collection failed: %s", e)


async def _collect_system_metrics() -> None:
    """
    Emit ``system`` metrics from ``psutil``.

    Mirrors ``core.observability.Observability.record_system_snapshot`` but
    is invoked by the collector loop directly so the system snapshot is
    guaranteed to land at the same cadence as the other subsystem metrics
    (rather than relying on each subsystem to call the convenience emitter).
    ``psutil.cpu_percent(interval=None)`` returns CPU since the last call —
    the first call returns 0.0 (or unreliable), so the very first cycle
    after boot may report 0.0; subsequent cycles are accurate.
    """
    try:
        import psutil  # local import — module must load even without psutil
    except ImportError:
        log.debug(
            "[observability_collector] psutil not installed — skipping system metrics"
        )
        return
    try:
        cpu = float(psutil.cpu_percent(interval=None))
        mem = psutil.virtual_memory()
        await record_metric(CAT_SYSTEM, "cpu_percent", cpu)
        await record_metric(CAT_SYSTEM, "memory_percent", float(mem.percent))
        await record_metric(
            CAT_SYSTEM, "memory_used_mb", round(float(mem.used) / (1024.0 * 1024.0), 2)
        )
    except Exception as e:
        log.debug("[observability_collector] system collection failed: %s", e)


# ── Collection cycle / loop ──────────────────────────────────────────────────


async def _collect_cycle() -> None:
    """
    Single collection pass — gather metrics from all four subsystems plus
    a bot-level heartbeat.

    Each ``_collect_*`` call is independently fault-tolerant (catches its
    own exceptions and logs at ``debug``), so a failure in one subsystem
    never prevents the others from being recorded. The bot ``cycles``
    heartbeat at the end is the collector's own liveness signal — if the
    dashboard sees ``bot/cycles`` age growing, the collector itself is
    stuck (not just one subsystem).
    """
    await _collect_data_source_metrics()
    await _collect_execution_metrics()
    await _collect_ml_metrics()
    await _collect_system_metrics()
    # Collector heartbeat — 1.0 per cycle. The dashboard can sum this
    # over a window to compute "collector cycles in last N minutes".
    await record_metric(
        CAT_BOT, "cycles", 1.0,
        interval_seconds=COLLECTION_INTERVAL_SECONDS,
        collector="observability_collector",
    )


async def _collector_loop() -> None:
    """
    Background loop: collect metrics every ``COLLECTION_INTERVAL_SECONDS``.

    The first pass runs IMMEDIATELY (no initial sleep) so the dashboard
    has data on boot instead of waiting up to 30 s. Each pass is wrapped
    in a top-level try/except so a thrown exception never kills the loop
    — the next cycle runs on schedule regardless.
    """
    log.info(
        "[observability_collector] Auto-collector started (interval=%ss)",
        COLLECTION_INTERVAL_SECONDS,
    )
    while True:
        try:
            await _collect_cycle()
        except asyncio.CancelledError:
            # Cooperative cancellation — propagate so stop_collector's
            # ``await task`` completes cleanly.
            log.info("[observability_collector] Collector loop cancelled — exiting")
            raise
        except Exception as e:
            # Defensive: _collect_cycle already swallows per-subsystem
            # errors, so this catch is for any unforeseen cross-cutting
            # failure (e.g. asyncio.to_thread unavailable). Never let
            # the loop die — log and continue.
            log.error("[observability_collector] Collection cycle crashed: %s", e)
        await asyncio.sleep(COLLECTION_INTERVAL_SECONDS)


# ── Public lifecycle API ─────────────────────────────────────────────────────


async def start_collector() -> None:
    """
    Start the background observability auto-collector (idempotent).

    Schedules ``_collector_loop`` as a named asyncio task
    (``observability-collector``) and returns immediately. The first
    collection pass runs before the first 30 s sleep. Calling this when
    a collector is already running (and not done) is a no-op — the
    existing task is returned unchanged.

    Typical wiring::

        from core.observability_collector import start_collector
        await start_collector()  # from inside an async lifespan / startup hook

    Or, for FastAPI apps, prefer ``register_routes(app)`` which wraps
    the app's lifespan to call this automatically.
    """
    global _collector_task
    if _collector_task is not None and not _collector_task.done():
        log.debug("[observability_collector] start_collector called but task already running — no-op")
        return
    _collector_task = asyncio.create_task(
        _collector_loop(), name="observability-collector"
    )


async def stop_collector() -> None:
    """
    Stop the background collector (best-effort, idempotent).

    Cancels the running task and awaits its completion. Safe to call
    when no collector is running — the no-op path is silent.
    """
    global _collector_task
    if _collector_task is None:
        return
    task = _collector_task
    _collector_task = None
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.debug("[observability_collector] stop_collector: task cleanup raised: %s", e)


# ── FastAPI wiring hook ──────────────────────────────────────────────────────


def register_routes(app: Any) -> None:
    """
    NO HTTP ROUTES ADDED — instead, ensures the observability collector
    background task starts when the FastAPI app's lifespan runs.

    Per the T7 spec, this hook exists so ``api/server.py`` can call
    ``register_routes(app)`` alongside the other ``core.*.register_routes``
    invocations without special-casing the collector. Unlike the sibling
    modules' ``register_routes`` functions (which append ``@app.get(...)``
    endpoints), this one adds zero routes — it only wraps the app's
    existing ``lifespan`` context manager so:

      1. ``start_collector()`` is awaited AFTER the app's own startup
         completes (so ``book_poller`` / ``store`` / ``ml_model`` are
         all initialised before the first collection pass).
      2. ``stop_collector()`` is awaited BEFORE the app's own shutdown
         logic runs (so the collector stops cleanly while the
         subsystems it reads from are still alive).

    The wrapping is additive — the app's original lifespan body is
    invoked unchanged; only ``start_collector`` / ``stop_collector`` are
    sandwiched around it. This is necessary because FastAPI's
    ``on_event("startup")`` handlers do NOT fire when the app is
    constructed with ``lifespan=...`` (verified on FastAPI 0.128), so
    the only robust way to hook startup is to wrap the lifespan itself.
    """
    global _lifespan_wrapped

    if _lifespan_wrapped:
        log.debug(
            "[observability_collector] register_routes called twice — lifespan "
            "already wrapped, no-op"
        )
        return

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_collector(app: Any):
        # Run the app's original startup (book_poller.start(), store init,
        # ml_model load, strategy registry, etc.) FIRST so the collector's
        # first cycle has real subsystem state to read.
        async with original_lifespan(app):
            # Startup complete — start the collector. Idempotent; safe even
            # if the caller already started it manually before lifespan.
            try:
                await start_collector()
            except Exception as e:
                log.error(
                    "[observability_collector] start_collector failed during "
                    "lifespan startup: %s — collector will NOT run", e
                )
            try:
                yield
            finally:
                # App is shutting down — stop the collector BEFORE the
                # original lifespan's teardown runs (so the collector isn't
                # reading from subsystems that are mid-teardown).
                try:
                    await stop_collector()
                except Exception as e:
                    log.debug(
                        "[observability_collector] stop_collector raised during "
                        "lifespan shutdown: %s", e
                    )

    app.router.lifespan_context = _lifespan_with_collector
    _lifespan_wrapped = True
    log.info(
        "[observability_collector] Lifespan wrapped — collector will start "
        "after app startup and stop before app shutdown (no HTTP routes added)"
    )


__all__ = [
    "COLLECTION_INTERVAL_SECONDS",
    "start_collector",
    "stop_collector",
    "register_routes",
]
