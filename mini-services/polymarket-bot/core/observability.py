"""
core/observability.py — SQLite-backed System Observability.

Single, generic metric store for the entire trading pipeline. Every subsystem
records structured health metrics with the same ``record_metric(category, name,
value, **metadata)`` call so the dashboard can render a unified health report
across six canonical categories:

  ┌──────────────┬──────────────────────────────────────────────────────┐
  │ category     │ example metric names                                │
  ├──────────────┼──────────────────────────────────────────────────────┤
  │ data_source  │ updates, latency, staleness  (per source_id)         │
  │ bot          │ cycles, errors                                       │
  │ strategy     │ evaluations, signals, rejects                         │
  │ execution    │ submissions, fills, rejections, slippage             │
  │ ml           │ inference_latency, prediction_distribution, drift    │
  │ system       │ cpu_percent, memory_percent, memory_used_mb           │
  └──────────────┴──────────────────────────────────────────────────────┘

Schema (SQLite, separate db at ``OBSERVABILITY_DB_PATH`` defaulting to
``/app/data/observability.db`` so the audit-trail & decision-ledger DBs are
not perturbed — same convention as ``core/audit_logger.py`` and
``core/decision_ledger.py``):

  metrics  (id, timestamp, category, name, value, metadata_json)

Indexes:
  (category, name, timestamp DESC)   — latest-per-metric lookup
  (name, timestamp DESC)              — ``get_metric_history(name)`` fast-path
  (category)                          — per-category aggregate queries

All writes are fire-and-forget from the caller's perspective: persistence
errors are logged at ``error`` level and swallowed so an observability hiccup
can never break the trading pipeline (mirrors the ``decision_ledger``
contract).

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose:

  GET /api/observability            structured health report (latest value
                                    per (category, name), bucketed by canonical
                                    category; unknown categories go to "other")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("OBSERVABILITY_DB_PATH", "/app/data/observability.db"))

# ── Canonical metric categories ────────────────────────────────────────────
# Single source of truth across the pipeline — every emitter references these
# so the health-report buckets are stable in the dashboard.
CAT_DATA_SOURCE = "data_source"
CAT_BOT = "bot"
CAT_STRATEGY = "strategy"
CAT_EXECUTION = "execution"
CAT_ML = "ml"
CAT_SYSTEM = "system"

CATEGORIES: tuple[str, ...] = (
    CAT_DATA_SOURCE,
    CAT_BOT,
    CAT_STRATEGY,
    CAT_EXECUTION,
    CAT_ML,
    CAT_SYSTEM,
)

# ── Recommended metric names (documentation, not enforcement) ──────────────
# Centralised here so call sites don't drift on naming; the recorder itself
# accepts ANY (category, name) pair so ad-hoc metrics still work — they just
# land in the "other" bucket in the health report.
METRIC_NAMES: dict[str, tuple[str, ...]] = {
    CAT_DATA_SOURCE: ("updates", "latency", "staleness"),
    CAT_BOT: ("cycles", "errors"),
    CAT_STRATEGY: ("evaluations", "signals", "rejects"),
    CAT_EXECUTION: ("submissions", "fills", "rejections", "slippage"),
    CAT_ML: ("inference_latency", "prediction_distribution", "drift"),
    CAT_SYSTEM: ("cpu_percent", "memory_percent", "memory_used_mb"),
}


class Observability:
    """
    Asynchronous, SQLite-backed system health/metrics recorder.

    Reads return plain ``list[dict]`` rows (most recent first where applicable).
    All public methods swallow their own persistence errors (logged at
    ``error`` level) so an observability hiccup can never break the trading
    pipeline.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the metrics table + indexes if absent. Safe on every boot."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                # WAL = better read concurrency for dashboards that poll
                # /api/observability while writes stream in from the pipeline.
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass  # WAL not supported on some backends (e.g. :memory:)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value REAL NOT NULL,
                        metadata_json TEXT
                    )
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metrics_cat_name_time "
                    "ON metrics(category, name, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metrics_name_time "
                    "ON metrics(name, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metrics_cat "
                    "ON metrics(category)"
                )
                conn.commit()
        except Exception as e:
            log.error("[observability] Init failed (%s): %s", self._db_path, e)

    # ── Writes ────────────────────────────────────────────────────────────

    async def record_metric(
        self,
        category: str,
        name: str,
        value: float | int | bool,
        **metadata: Any,
    ) -> None:
        """
        Persist a single metric sample.

        ``value`` is coerced to ``float`` (bool → 0.0/1.0). ``metadata`` is
        JSON-serialised with ``default=str`` (handles Decimals / enums /
        dataclasses / numpy scalars) and stored in ``metadata_json``. Empty
        ``category`` or ``name`` is skipped silently — bad call-site inputs
        never propagate as schema noise.
        """
        if not category or not name:
            return
        ts = time.time()
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            log.debug(
                "[observability] record_metric: coercing value failed "
                "cat=%s name=%s value=%r (%s) — defaulting to 0.0",
                category, name, value, e,
            )
            v = 0.0
        payload = json.dumps(metadata, default=str) if metadata else None

        def _insert() -> None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO metrics
                        (timestamp, category, name, value, metadata_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (ts, str(category), str(name), v, payload),
                    )
                    conn.commit()
            except Exception as e:
                log.error(
                    "[observability] record_metric failed cat=%s name=%s: %s",
                    category, name, e,
                )

        await asyncio.to_thread(_insert)

    async def record_system_snapshot(self) -> None:
        """
        Convenience emitter: record CPU + memory from ``psutil`` if available.

        Safe to call from a background loop (e.g. every 10 s). No-op (with a
        debug log) if ``psutil`` isn't installed — keeps the module optional.
        """
        try:
            import psutil  # local import — module must load even without psutil
        except ImportError:
            log.debug("[observability] psutil not installed — skipping system snapshot")
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            await self.record_metric(CAT_SYSTEM, "cpu_percent", cpu)
            await self.record_metric(CAT_SYSTEM, "memory_percent", mem.percent)
            await self.record_metric(
                CAT_SYSTEM, "memory_used_mb", round(mem.used / (1024.0 * 1024.0), 2)
            )
        except Exception as e:
            log.debug("[observability] system snapshot failed: %s", e)

    # ── Reads ──────────────────────────────────────────────────────────────

    async def get_metric_history(
        self, name: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Return the most recent N samples for ``name`` (newest first).

        Each row: ``{timestamp, category, name, value, metadata}``. The
        ``metadata`` key is the decoded ``metadata_json`` column (or ``None``
        if no metadata was recorded / decode failed).
        """
        if not name:
            return []
        cap = max(1, min(int(limit), 1000))

        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT timestamp, category, name, value, metadata_json
                        FROM metrics
                        WHERE name = ?
                        ORDER BY timestamp DESC, id DESC
                        LIMIT ?
                        """,
                        (name, cap),
                    )
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error(
                    "[observability] get_metric_history failed name=%s: %s",
                    name, e,
                )
                return []
            for r in rows:
                r["metadata"] = _safe_json(r.pop("metadata_json", None))
            return rows

        return await asyncio.to_thread(_fetch)

    async def get_health_report(self) -> dict[str, Any]:
        """
        Aggregate latest-per-(category, name) into a structured health report.

        Buckets each metric under its canonical category (``CAT_*``); metrics
        recorded under an unknown category land in an ``other`` bucket so they
        are never silently dropped. Each metric entry carries its value,
        timestamp, age (seconds since sample), and decoded metadata.
        """
        def _fetch() -> dict[str, Any]:
            now = time.time()
            rows: list[dict[str, Any]] = []
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    # ROW_NUMBER() window requires SQLite ≥ 3.25 (2018).
                    # The base image ships SQLite ≥ 3.31 so this is safe.
                    cursor.execute("""
                        SELECT m.timestamp, m.category, m.name, m.value,
                               m.metadata_json
                        FROM (
                            SELECT *,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY category, name
                                       ORDER BY timestamp DESC, id DESC
                                   ) AS rn
                            FROM metrics
                        ) m
                        WHERE m.rn = 1
                        ORDER BY m.category, m.name
                    """)
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error("[observability] get_health_report failed: %s", e)
                rows = []

            report: dict[str, Any] = {
                "generated_at": now,
                "category_count": len(CATEGORIES),
                "metric_count": len(rows),
                "oldest_sample_age_seconds": None,
                "newest_sample_age_seconds": None,
                "categories": {cat: {} for cat in CATEGORIES},
            }
            if not rows:
                return report

            timestamps: list[float] = []
            for r in rows:
                cat = r.get("category", "") or ""
                name = r.get("name", "") or ""
                ts = float(r.get("timestamp") or now)
                timestamps.append(ts)
                entry = {
                    "value": r.get("value"),
                    "timestamp": ts,
                    "age_seconds": round(now - ts, 3),
                    "metadata": _safe_json(r.get("metadata_json")),
                }
                if cat in report["categories"]:
                    report["categories"][cat][name] = entry
                else:
                    report["categories"].setdefault("other", {})[name] = entry

            report["oldest_sample_age_seconds"] = round(now - min(timestamps), 3)
            report["newest_sample_age_seconds"] = round(now - max(timestamps), 3)
            return report

        return await asyncio.to_thread(_fetch)


# Module-level singleton (mirrors the ``audit_logger`` / ``decision_ledger``
# convention so importers can grab the instance at module import time).
observability = Observability()

# Module-level aliases — ergonomic for fire-and-forget call sites:
#     from core.observability import record_metric
#     asyncio.create_task(record_metric("bot", "cycle", 1, scan_id=scan_id))
# `record_metric` is the bound method of the singleton, so callers don't
# need to reference `observability` explicitly.
record_metric = observability.record_metric
get_health_report = observability.get_health_report
get_metric_history = observability.get_metric_history


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append observability inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/observability
          Structured system health report — latest value per (category,
          name), bucketed under the six canonical categories
          (data_source / bot / strategy / execution / ml / system) plus an
          ``other`` bucket for ad-hoc metrics. Includes overall metric
          count and oldest/newest sample ages.
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/observability", tags=["observability"])
    async def _observability_overview():
        """Return the structured system health report (latest value per metric)."""
        return await observability.get_health_report()

    @app.get("/api/observability/history/{name}", tags=["observability"])
    async def _observability_history(
        name: str,
        limit: int = Query(100, ge=1, le=1000, description="Max samples to return"),
    ):
        """Return the most recent N samples for metric ``name`` (newest first)."""
        rows = await observability.get_metric_history(name, limit=limit)
        return {"name": name, "count": len(rows), "samples": rows}


def _safe_json(raw: str | None) -> Any:
    """Best-effort JSON decode for the ``metadata_json`` column."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


__all__ = [
    "DB_PATH",
    "Observability",
    "observability",
    "record_metric",
    "get_health_report",
    "get_metric_history",
    "register_routes",
    "CAT_DATA_SOURCE",
    "CAT_BOT",
    "CAT_STRATEGY",
    "CAT_EXECUTION",
    "CAT_ML",
    "CAT_SYSTEM",
    "CATEGORIES",
    "METRIC_NAMES",
]
