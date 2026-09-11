"""Async job queue for long-running tasks.

Processes jobs in background workers:
- ML retrain (takes 10-60s)
- Backtest runs (takes 5-30s)
- Data exports (takes 2-10s)
- Bulk operations

Jobs are persisted to SQLite so they survive restarts.

W17-8 — Additive: a new ``core/job_queue.py`` module introducing a
SQLite-backed job queue with background worker threads. Long-running
operations (ML retrains, backtests, data exports, bulk operations)
that today block the FastAPI event loop for 5-60s can now be enqueued
as jobs and polled for completion via the new ``/api/jobs`` routes
registered additively in ``api/server.py``.

Design
~~~~~~

* ``JobQueue`` owns one ``sqlite3`` connection per call (no
  module-level connection is held open). SQLite's WAL-mode journaling
  is NOT enabled here (the per-call ``with sqlite3.connect(...):``
  context auto-commits and releases the connection); if two worker
  threads race to claim the same pending job, the atomic
  ``UPDATE ... WHERE job_id = ? AND status = 'pending'`` statement
  settles the race — only the first ``rowcount > 0`` caller proceeds.
* Handlers are registered per ``job_type`` via ``register_handler``.
  The handler signature is ``Callable[[dict], Any]`` — payload in,
  JSON-able result out. Handlers that raise are caught in the worker
  loop and the job's status is set to ``failed`` with the exception
  message recorded in the ``error`` column.
* Workers run in ``daemon=True`` threads so they don't block process
  shutdown; ``stop_workers`` sets ``_running = False`` and joins each
  worker with a 5s timeout — if a worker is mid-handler when
  shutdown is requested, it is allowed to finish naturally (the next
  loop iteration's ``while self._running`` check exits cleanly).
* The module-level singleton ``job_queue`` is constructed at first
  import. Production code (``api/server.py`` lifespan) calls
  ``register_default_handlers(job_queue)`` + ``start_workers()``.
  Tests construct their own ``JobQueue(tmp_path / "test.db")``
  instances so they're hermetic to each other AND to the production
  singleton.
"""
import sqlite3
import json
import time
import logging
import asyncio
import threading
import os
from pathlib import Path
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import uuid

logger = logging.getLogger(__name__)

JOB_QUEUE_DB = Path(os.environ.get("JOB_QUEUE_DB", "/app/data/job_queue.db"))


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    job_type: str  # "retrain", "backtest", "export", "custom"
    status: JobStatus
    payload: dict
    result: Optional[dict] = None
    error: Optional[str] = None
    progress: float = 0.0  # 0.0 to 1.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    worker_id: Optional[str] = None


def job_to_dict(job: Job) -> dict:
    """Serialize a ``Job`` to a JSON-able ``dict``.

    The ``status`` ``Enum`` is unwrapped to its ``.value`` (string)
    so the result is JSON-serialisable (FastAPI's ``jsonable_encoder``
    handles ``Enum`` natively but tests / callers that use plain
    ``json.dumps`` on the response do not).
    """
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
        "payload": job.payload,
        "result": job.result,
        "error": job.error,
        "progress": job.progress,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "worker_id": job.worker_id,
    }


class JobQueue:
    """SQLite-backed job queue with async worker support."""

    def __init__(self, db_path: Path = JOB_QUEUE_DB, max_workers: int = 2):
        self._db_path = db_path
        self._max_workers = max_workers
        self._workers: list[threading.Thread] = []
        self._running = False
        self._handlers: dict[str, Callable] = {}
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    progress REAL DEFAULT 0.0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    worker_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
            """)

    def register_handler(self, job_type: str, handler: Callable):
        """Register a handler for a job type."""
        self._handlers[job_type] = handler
        logger.info(f"Registered handler for job type: {job_type}")

    def enqueue(self, job_type: str, payload: dict = None) -> Job:
        """Add a job to the queue."""
        job_id = str(uuid.uuid4())[:12]
        job = Job(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            payload=payload or {},
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO jobs (job_id, job_type, status, payload, progress, created_at)
                VALUES (?, ?, ?, ?, 0.0, ?)
            """, (job.job_id, job.job_type, job.status.value, json.dumps(job.payload), job.created_at))

        logger.info(f"Enqueued job {job_id}: {job_type}")
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return Job(
                job_id=row["job_id"],
                job_type=row["job_type"],
                status=JobStatus(row["status"]),
                payload=json.loads(row["payload"]),
                result=json.loads(row["result"]) if row["result"] else None,
                error=row["error"],
                progress=row["progress"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                worker_id=row["worker_id"],
            )

    def get_pending_jobs(self, limit: int = 10) -> list[Job]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (JobStatus.PENDING.value, limit)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def get_recent_jobs(self, limit: int = 50, status: str = None) -> list[Job]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            status=JobStatus(row["status"]),
            payload=json.loads(row["payload"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            progress=row["progress"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            worker_id=row["worker_id"],
        )

    def _update_job(self, job_id: str, status: JobStatus, result: dict = None,
                    error: str = None, progress: float = None, worker_id: str = None):
        with sqlite3.connect(self._db_path) as conn:
            if status == JobStatus.RUNNING:
                conn.execute(
                    "UPDATE jobs SET status = ?, started_at = ?, worker_id = ? WHERE job_id = ?",
                    (status.value, time.time(), worker_id, job_id)
                )
            elif status in (JobStatus.COMPLETED, JobStatus.FAILED):
                conn.execute(
                    "UPDATE jobs SET status = ?, result = ?, error = ?, progress = ?, completed_at = ? WHERE job_id = ?",
                    (status.value, json.dumps(result) if result else None, error,
                     1.0 if status == JobStatus.COMPLETED else progress, time.time(), job_id)
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, progress = ? WHERE job_id = ?",
                    (status.value, progress, job_id)
                )

    def cancel_job(self, job_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, completed_at = ? WHERE job_id = ? AND status = ?",
                (JobStatus.CANCELLED.value, time.time(), job_id, JobStatus.PENDING.value)
            )
            return cursor.rowcount > 0

    def start_workers(self, num_workers: int = None):
        """Start background worker threads."""
        num_workers = num_workers or self._max_workers
        self._running = True

        for i in range(num_workers):
            worker = threading.Thread(target=self._worker_loop, args=(f"worker-{i}",), daemon=True)
            worker.start()
            self._workers.append(worker)
            logger.info(f"Started job worker: worker-{i}")

    def stop_workers(self):
        """Stop all worker threads."""
        self._running = False
        for worker in self._workers:
            worker.join(timeout=5)
        self._workers.clear()
        logger.info("Stopped all job workers")

    def _worker_loop(self, worker_id: str):
        """Worker thread loop — picks up pending jobs and executes them."""
        logger.info(f"Worker {worker_id} started")

        while self._running:
            try:
                # Get next pending job
                jobs = self.get_pending_jobs(1)
                if not jobs:
                    time.sleep(1)  # No jobs, wait
                    continue

                job = jobs[0]

                # Atomically claim the job (set to RUNNING)
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, started_at = ?, worker_id = ? WHERE job_id = ? AND status = ?",
                        (JobStatus.RUNNING.value, time.time(), worker_id, job.job_id, JobStatus.PENDING.value)
                    )
                    if cursor.rowcount == 0:
                        continue  # Another worker grabbed it

                logger.info(f"Worker {worker_id} processing job {job.job_id} ({job.job_type})")

                # Execute handler
                handler = self._handlers.get(job.job_type)
                if handler:
                    try:
                        result = handler(job.payload)
                        self._update_job(job.job_id, JobStatus.COMPLETED, result=result)
                        logger.info(f"Job {job.job_id} completed")
                    except Exception as e:
                        self._update_job(job.job_id, JobStatus.FAILED, error=str(e))
                        logger.error(f"Job {job.job_id} failed: {e}")
                else:
                    self._update_job(job.job_id, JobStatus.FAILED, error=f"No handler for job type: {job.job_type}")
                    logger.error(f"No handler for job type: {job.job_type}")

            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                time.sleep(5)

    def get_stats(self) -> dict:
        with sqlite3.connect(self._db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
            ).fetchall()
            return {
                "total_jobs": total,
                "by_status": {row[0]: row[1] for row in by_status},
                "workers_active": len(self._workers),
                "handlers_registered": list(self._handlers.keys()),
            }


# ── Default handlers ─────────────────────────────────────────────────────────
# Three out-of-the-box handlers covering the three long-running task
# families called out in the W17-8 spec: ML retrain, backtest, export.
# Each handler body uses LAZY imports (``from ml.model import ml_model``
# inside the function, not at module top) so importing ``core.job_queue``
# does NOT trigger imports of the heavy ML / backtesting modules — a
# test that constructs ``JobQueue`` for unit testing should not pay the
# sklearn / numpy import cost just to enqueue a no-op job.


def _handle_retrain(payload: dict) -> dict:
    """Retrain the ML ensemble (heavy: 10-60s).

    Calls ``ml_model.fit_initial()`` (re-fits RF + GB + SGD + LightGBM
    ensemble on the latest labelled feature store rows) and then
    ``ml_model.save()`` to persist the pickle. Mirrors the
    ``POST /api/ml/retrain`` route's body (minus the cache invalidation
    + WS broadcast — those are HTTP-layer concerns, not job-queue
    concerns).
    """
    from ml.model import ml_model
    from ml.model_registry import model_registry

    ml_model.fit_initial()
    ml_model.save()
    return {
        "status": "retrained",
        "brier_score": float(ml_model.brier_score),
        "roc_auc": float(ml_model.roc_auc),
        "log_loss": float(ml_model.log_loss_score),
        "ece": float(ml_model.ece),
        "model_version": model_registry.active_version,
    }


def _handle_backtest(payload: dict) -> dict:
    """Run a backtest simulation (5-30s).

    ``payload`` keys (all optional — defaults mirror
    ``api.server.BacktestRequest``):

      - ``strategy_id``: str (default ``"ml_random_forest_quant"``)
      - ``initial_capital``: float (default 10000.0)
      - ``days``: int (default 30)
      - ``fee_bps``: float (default 0.0)
      - ``slippage_bps``: float (default 5.0)
    """
    from backtesting.engine import backtest_engine

    result = backtest_engine.run_backtest(
        strategy_id=payload.get("strategy_id", "ml_random_forest_quant"),
        initial_capital=float(payload.get("initial_capital", 10000.0)),
        days=int(payload.get("days", 30)),
        fee_bps=float(payload.get("fee_bps", 0.0)),
        slippage_bps=float(payload.get("slippage_bps", 5.0)),
    )
    return result.to_dict()


def _handle_export(payload: dict) -> dict:
    """Generate a data export manifest (2-10s).

    ``payload`` keys:

      - ``kind``: ``"trades"`` (default) / ``"positions"`` / ``"orders"``
        — which in-memory store to summarise.
      - ``limit``: int — max rows to include in the manifest count
        (default 1000).

    The current implementation returns a manifest dict (``kind`` /
    ``row_count`` / ``limit`` / ``generated_at``). The manifest can be
    extended in a follow-up to write the artifact to disk and return
    a path; the job-queue surface is agnostic to the handler's return
    shape as long as it's JSON-able.
    """
    kind = payload.get("kind", "trades")
    limit = int(payload.get("limit", 1000))

    count = 0
    try:
        from core.data_store import store

        if kind == "trades":
            count = min(len(store.trades), limit)
        elif kind == "positions":
            count = min(len(store.positions), limit)
        elif kind == "orders":
            count = min(len(store.open_orders), limit)
    except Exception as e:  # pragma: no cover — defensive: store import must never fail the job
        logger.warning("Export handler could not read core.data_store.store: %s", e)

    return {
        "kind": kind,
        "row_count": count,
        "limit": limit,
        "generated_at": time.time(),
    }


def register_default_handlers(queue: JobQueue) -> None:
    """Register the three default job handlers (``retrain``, ``backtest``, ``export``).

    Idempotent: re-registering the same handler for a job type
    overwrites the prior entry in ``_handlers`` (dict semantics).
    Safe to call multiple times — the last registration wins.
    """
    queue.register_handler("retrain", _handle_retrain)
    queue.register_handler("backtest", _handle_backtest)
    queue.register_handler("export", _handle_export)


# Singleton — constructed at first import. ``JOB_QUEUE_DB`` env var
# (redirected to a writable tmp path by ``tests/conftest.py``) controls
# the on-disk path so tests / sandbox environments never try to mkdir
# a read-only ``/app/data``.
job_queue = JobQueue()
