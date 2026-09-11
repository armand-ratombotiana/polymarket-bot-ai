"""Unit + integration tests for the W17-8 async job queue.

W17-8 — ``core/job_queue.py`` + the five ``/api/jobs`` routes
registered additively in ``api/server.py``.

Covers (per the W17-8 task spec's Step 5 list):

  1. ``JobQueue.enqueue`` + ``get_job`` — round-trip a job and verify
     every persisted column (id / type / status / payload / timestamps).
  2. ``get_pending_jobs`` — ordering (oldest-first by ``created_at``)
     + the status filter.
  3. Job execution with a test handler — register a custom handler,
     enqueue a job, start one worker, poll ``get_job`` until the
     status flips to ``completed``, verify the handler's return
     value was persisted as ``result``.
  4. Job failure path — handler raises; verify status ``failed`` +
     the exception message is in ``error``.
  5. ``cancel_job`` — happy path (pending → cancelled) + the
     non-cancellable path (a job in ``running`` returns False).
  6. ``get_stats`` — total / by_status / handlers_registered /
     workers_active counts against a seeded queue.
  7. ``register_default_handlers`` + ``_handle_export`` — the three
     built-in handler types are registered; ``_handle_export`` returns
     a manifest dict against the live ``DataStore`` (zero trades →
     ``row_count=0``, but the handler must not raise).
  8. API routes — the five ``/api/jobs*`` endpoints via
     ``fastapi.testclient.TestClient`` against the production
     ``api.server.app``.

Hermeticity
~~~~~~~~~~~
Unit tests construct their own ``JobQueue(tmp_path / "jq.db")`` per
test so they're hermetic to each other AND to the module-level
``job_queue`` singleton used by the API routes. The API-route tests
use the singleton (because that's what the routes call) — a per-test
``_wipe_job_queue_db`` autouse fixture DELETEs every row from the
singleton's ``jobs`` table BEFORE each test so the API tests don't
see each other's enqueued jobs.

The ``conftest.py`` env-var redirect already pins ``JOB_QUEUE_DB``
to ``/tmp/pmbot_conftest_isolation/job_queue.db`` BEFORE the
singleton is constructed at first import — so neither the unit nor
the API tests ever touch ``/app/data``.

All tests are SYNC ``def`` (not ``async def``) — ``TestClient`` runs
the ASGI app in a separate thread with its own event loop, and the
worker-threads themselves are plain OS threads (no asyncio). Sync
tests avoid the asyncio-mode plumbing pitfalls the rest of the suite
already navigates around (see ``tests/test_live_safety_gate_api.py``
header comment for the same idiom).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.job_queue import (
    Job,
    JobQueue,
    JobStatus,
    _handle_export,
    job_queue as _job_queue_singleton,
    job_to_dict,
    register_default_handlers,
)


# ── Auth token (matches ``conftest.py``'s ``API_TOKEN`` redirect) ────────────
VALID_TOKEN = "test-token-conftest"


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — JobQueue contract (hermetic tmp_path DB per test)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fresh_queue(tmp_path: Path) -> JobQueue:
    """Per-test ``JobQueue`` against a tmp_path SQLite file.

    The fixture returns a freshly-constructed ``JobQueue`` whose
    ``_init_db()`` has already created the ``jobs`` table. Each test
    gets its own DB file under pytest's ``tmp_path`` so unit-test
    state NEVER leaks across tests or into the module-level singleton.
    """
    return JobQueue(db_path=tmp_path / "jq.db", max_workers=1)


class TestEnqueueGetJob:
    """``enqueue`` writes a row; ``get_job`` round-trips every column."""

    def test_enqueue_returns_pending_job_with_id(self, fresh_queue: JobQueue):
        """``enqueue`` returns a ``Job`` with status PENDING + a 12-char id."""
        job = fresh_queue.enqueue("custom", {"foo": "bar"})
        assert job.job_id  # 12-char uuid prefix
        assert len(job.job_id) == 12
        assert job.job_type == "custom"
        assert job.status is JobStatus.PENDING
        assert job.payload == {"foo": "bar"}
        assert job.result is None
        assert job.error is None
        assert job.progress == 0.0
        assert job.created_at > 0
        assert job.started_at is None
        assert job.completed_at is None
        assert job.worker_id is None

    def test_get_job_round_trips_every_column(self, fresh_queue: JobQueue):
        """The persisted row matches the in-memory ``Job`` for every column.

        ``get_job`` is the canonical read path; if it drops / mangles
        any column the API routes (``GET /api/jobs/{job_id}``) would
        surface bad data to the dashboard. This test verifies the
        round-trip for every column the schema persists.
        """
        job = fresh_queue.enqueue("backtest", {"strategy_id": "ml_quant", "days": 30})
        fetched = fresh_queue.get_job(job.job_id)

        assert fetched is not None
        assert fetched.job_id == job.job_id
        assert fetched.job_type == "backtest"
        assert fetched.status is JobStatus.PENDING
        assert fetched.payload == {"strategy_id": "ml_quant", "days": 30}
        assert fetched.result is None  # not yet executed
        assert fetched.error is None
        assert fetched.progress == 0.0
        assert fetched.created_at == pytest.approx(job.created_at, rel=1e-9)
        assert fetched.started_at is None
        assert fetched.completed_at is None
        assert fetched.worker_id is None

    def test_get_job_unknown_id_returns_none(self, fresh_queue: JobQueue):
        """A miss returns ``None`` (the API route maps this to 404)."""
        assert fresh_queue.get_job("nonexistent-id") is None

    def test_enqueue_default_payload_is_empty_dict(self, fresh_queue: JobQueue):
        """``payload=None`` (the default) is stored as ``{}``, not ``None``.

        SQLite + json.dumps(None) would persist the string ``"null"``;
        ``json.loads("null")`` returns Python ``None``, which would
        break callers that do ``payload["key"]``. The default to ``{}``
        guards against that.
        """
        job = fresh_queue.enqueue("custom")
        assert job.payload == {}
        fetched = fresh_queue.get_job(job.job_id)
        assert fetched is not None
        assert fetched.payload == {}


class TestGetPendingJobs:
    """``get_pending_jobs`` returns pending rows oldest-first."""

    def test_empty_queue_returns_empty_list(self, fresh_queue: JobQueue):
        """A fresh queue has no pending jobs."""
        assert fresh_queue.get_pending_jobs() == []

    def test_pending_jobs_returned_oldest_first(self, fresh_queue: JobQueue):
        """``ORDER BY created_at ASC`` is the FIFO contract.

        Workers pick up the oldest pending job first; if this ordering
        ever breaks, a long-stuck job could starve indefinitely behind
        newer arrivals.
        """
        # Enqueue three jobs with explicit created_at drift so the
        # ordering assertion is robust (sqlite's microsecond resolution
        # is fine in practice but we make the gap explicit here).
        j1 = fresh_queue.enqueue("custom", {"n": 1})
        time.sleep(0.01)
        j2 = fresh_queue.enqueue("custom", {"n": 2})
        time.sleep(0.01)
        j3 = fresh_queue.enqueue("custom", {"n": 3})

        pending = fresh_queue.get_pending_jobs(limit=10)
        assert [j.job_id for j in pending] == [j1.job_id, j2.job_id, j3.job_id]

    def test_limit_caps_result_count(self, fresh_queue: JobQueue):
        """``limit`` is honoured — only N pending jobs are returned."""
        for _ in range(5):
            fresh_queue.enqueue("custom", {"x": 1})
        pending = fresh_queue.get_pending_jobs(limit=2)
        assert len(pending) == 2

    def test_non_pending_jobs_excluded(self, fresh_queue: JobQueue):
        """``RUNNING`` / ``COMPLETED`` / ``FAILED`` / ``CANCELLED`` rows
        are NOT returned by ``get_pending_jobs`` — the worker loop
        relies on this to avoid re-claiming finished work."""
        job = fresh_queue.enqueue("custom")
        # Flip status to RUNNING via the internal API (mirrors what
        # the worker loop does when it claims a job).
        fresh_queue._update_job(job.job_id, JobStatus.RUNNING, worker_id="worker-0")
        assert fresh_queue.get_pending_jobs() == []


class TestJobExecution:
    """End-to-end worker execution: pending → running → completed/failed."""

    def test_completed_job_persists_handler_result(self, fresh_queue: JobQueue):
        """A handler's return value lands in ``result`` + status → COMPLETED."""

        def echo_handler(payload: dict) -> dict:
            return {"echo": payload, "processed_at": time.time()}

        fresh_queue.register_handler("echo", echo_handler)
        job = fresh_queue.enqueue("echo", {"hello": "world"})

        fresh_queue.start_workers(num_workers=1)
        try:
            fetched = _poll_until_status(fresh_queue, job.job_id, JobStatus.COMPLETED, timeout=5.0)
        finally:
            fresh_queue.stop_workers()

        assert fetched is not None
        assert fetched.status is JobStatus.COMPLETED
        assert fetched.result is not None
        assert fetched.result["echo"] == {"hello": "world"}
        assert fetched.result["processed_at"] > 0
        assert fetched.error is None
        assert fetched.progress == 1.0
        assert fetched.started_at is not None
        assert fetched.completed_at is not None
        assert fetched.completed_at >= fetched.started_at
        assert fetched.worker_id == "worker-0"

    def test_failed_job_persists_exception_message(self, fresh_queue: JobQueue):
        """A handler that raises → status FAILED + ``error`` carries the message.

        The exception type itself is NOT persisted (only ``str(e)``) so
        a downstream consumer doesn't need to import the exception
        class to render the failure reason.
        """

        def boom_handler(payload: dict) -> dict:
            raise ValueError("simulated handler failure")

        fresh_queue.register_handler("boom", boom_handler)
        job = fresh_queue.enqueue("boom", {"trigger": True})

        fresh_queue.start_workers(num_workers=1)
        try:
            fetched = _poll_until_status(fresh_queue, job.job_id, JobStatus.FAILED, timeout=5.0)
        finally:
            fresh_queue.stop_workers()

        assert fetched is not None
        assert fetched.status is JobStatus.FAILED
        assert fetched.result is None
        assert "simulated handler failure" in (fetched.error or "")
        assert fetched.completed_at is not None
        assert fetched.worker_id == "worker-0"

    def test_unknown_job_type_fails_with_descriptive_error(self, fresh_queue: JobQueue):
        """A job whose ``type`` has no registered handler fails with a
        ``"No handler for job type: <type>"`` error (so the operator
        can fix the handler registration without re-submitting)."""
        job = fresh_queue.enqueue("no_such_type", {})
        fresh_queue.start_workers(num_workers=1)
        try:
            fetched = _poll_until_status(fresh_queue, job.job_id, JobStatus.FAILED, timeout=5.0)
        finally:
            fresh_queue.stop_workers()

        assert fetched is not None
        assert fetched.status is JobStatus.FAILED
        assert "No handler" in (fetched.error or "")
        assert "no_such_type" in (fetched.error or "")


class TestCancelJob:
    """``cancel_job`` only cancels PENDING jobs; returns False otherwise."""

    def test_cancel_pending_job_returns_true(self, fresh_queue: JobQueue):
        """Happy path: pending → cancelled; ``cancel_job`` returns True."""
        job = fresh_queue.enqueue("custom", {"x": 1})
        assert fresh_queue.cancel_job(job.job_id) is True
        fetched = fresh_queue.get_job(job.job_id)
        assert fetched is not None
        assert fetched.status is JobStatus.CANCELLED
        assert fetched.completed_at is not None

    def test_cancel_non_pending_job_returns_false(self, fresh_queue: JobQueue):
        """A RUNNING job is NOT cancellable — ``cancel_job`` returns False.

        The worker loop's atomic UPDATE has already moved the row out
        of ``pending``; ``cancel_job``'s ``WHERE status = 'pending'``
        clause matches 0 rows, so ``rowcount > 0`` is False. This is
        the contract the API route's ``POST /api/jobs/{id}/cancel``
        handler relies on to surface 409 to the caller.
        """
        job = fresh_queue.enqueue("custom")
        fresh_queue._update_job(job.job_id, JobStatus.RUNNING, worker_id="worker-0")
        assert fresh_queue.cancel_job(job.job_id) is False
        fetched = fresh_queue.get_job(job.job_id)
        assert fetched is not None
        assert fetched.status is JobStatus.RUNNING  # unchanged

    def test_cancel_unknown_id_returns_false(self, fresh_queue: JobQueue):
        """A miss returns False (no row to cancel)."""
        assert fresh_queue.cancel_job("nonexistent-id") is False


class TestGetStats:
    """``get_stats`` aggregates total + per-status counts."""

    def test_empty_queue_stats(self, fresh_queue: JobQueue):
        """A fresh queue has zero total jobs + empty ``by_status`` dict."""
        stats = fresh_queue.get_stats()
        assert stats["total_jobs"] == 0
        assert stats["by_status"] == {}
        assert stats["workers_active"] == 0  # no workers started yet
        assert stats["handlers_registered"] == []

    def test_seeded_stats(self, fresh_queue: JobQueue):
        """Stats correctly count 3 pending + 1 completed + 1 cancelled."""
        fresh_queue.register_handler("noop", lambda p: {"ok": True})
        for _ in range(3):
            fresh_queue.enqueue("noop")
        completed = fresh_queue.enqueue("noop")
        fresh_queue._update_job(completed.job_id, JobStatus.COMPLETED, result={"ok": True})
        cancelled = fresh_queue.enqueue("noop")
        fresh_queue.cancel_job(cancelled.job_id)

        stats = fresh_queue.get_stats()
        assert stats["total_jobs"] == 5
        assert stats["by_status"]["pending"] == 3
        assert stats["by_status"]["completed"] == 1
        assert stats["by_status"]["cancelled"] == 1
        assert stats["handlers_registered"] == ["noop"]

    def test_workers_active_after_start_workers(self, fresh_queue: JobQueue):
        """``workers_active`` reflects the number of running worker threads."""
        fresh_queue.start_workers(num_workers=2)
        try:
            stats = fresh_queue.get_stats()
            assert stats["workers_active"] == 2
        finally:
            fresh_queue.stop_workers()
        # After stop_workers, the count drops back to 0.
        stats = fresh_queue.get_stats()
        assert stats["workers_active"] == 0


class TestRegisterDefaultHandlers:
    """``register_default_handlers`` wires retrain / backtest / export."""

    def test_three_default_handlers_registered(self, fresh_queue: JobQueue):
        """``register_default_handlers`` adds retrain / backtest / export."""
        register_default_handlers(fresh_queue)
        handlers = fresh_queue._handlers.keys()
        assert "retrain" in handlers
        assert "backtest" in handlers
        assert "export" in handlers

    def test_register_default_handlers_is_idempotent(self, fresh_queue: JobQueue):
        """Calling ``register_default_handlers`` twice doesn't duplicate entries."""
        register_default_handlers(fresh_queue)
        register_default_handlers(fresh_queue)
        handlers = fresh_queue._handlers
        assert len(handlers) == 3  # still 3, not 6
        assert set(handlers.keys()) == {"retrain", "backtest", "export"}


class TestExportHandler:
    """``_handle_export`` returns a manifest against the live ``DataStore``."""

    def test_export_default_kind_is_trades(self):
        """Default ``kind`` is ``trades``; manifest carries row_count."""
        result = _handle_export({})
        assert result["kind"] == "trades"
        assert "row_count" in result
        assert "limit" in result
        assert "generated_at" in result
        assert result["row_count"] >= 0  # store.trades is empty in tests → 0
        assert result["limit"] == 1000

    def test_export_with_explicit_kind_and_limit(self):
        """``kind=positions`` + ``limit=50`` propagate to the manifest."""
        result = _handle_export({"kind": "positions", "limit": 50})
        assert result["kind"] == "positions"
        assert result["limit"] == 50
        assert result["row_count"] >= 0

    def test_export_unknown_kind_returns_zero_count(self):
        """An unknown ``kind`` doesn't raise — manifest carries ``row_count=0``."""
        result = _handle_export({"kind": "unknown_kind", "limit": 10})
        assert result["kind"] == "unknown_kind"
        assert result["row_count"] == 0


class TestJobToDict:
    """``job_to_dict`` unwraps the ``JobStatus`` enum for JSON-serialisability."""

    def test_status_value_unwrapped_to_string(self):
        """``status`` is the ``.value`` string, not the ``Enum`` instance."""
        job = Job(
            job_id="abc123",
            job_type="custom",
            status=JobStatus.COMPLETED,
            payload={"k": "v"},
            result={"ok": True},
            progress=1.0,
            created_at=1234.5,
            started_at=1235.0,
            completed_at=1236.0,
            worker_id="worker-0",
        )
        d = job_to_dict(job)
        assert d["job_id"] == "abc123"
        assert d["job_type"] == "custom"
        assert d["status"] == "completed"  # string, not Enum
        assert d["payload"] == {"k": "v"}
        assert d["result"] == {"ok": True}
        assert d["error"] is None
        assert d["progress"] == 1.0
        assert d["created_at"] == 1234.5
        assert d["started_at"] == 1235.0
        assert d["completed_at"] == 1236.0
        assert d["worker_id"] == "worker-0"

    def test_dict_is_json_serialisable(self):
        """The output dict can be passed to ``json.dumps`` without raising."""
        import json

        job = Job(
            job_id="abc123",
            job_type="custom",
            status=JobStatus.PENDING,
            payload={"x": [1, 2, 3]},
        )
        # Must not raise (Enum values would raise TypeError under json.dumps).
        s = json.dumps(job_to_dict(job))
        assert isinstance(s, str)


# ═══════════════════════════════════════════════════════════════════════════
# API-route integration tests — TestClient against the production app
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _wipe_job_queue_db():
    """DELETE every row from the module-level singleton's ``jobs`` table BEFORE each test.

    The five ``/api/jobs*`` routes in ``api/server.py`` all call the
    module-level ``job_queue`` singleton (imported as
    ``_job_queue_singleton`` in server.py). Tests that hit those
    routes share the singleton's SQLite file across the whole session;
    without a wipe, an enqueued job from one test would leak into the
    next test's ``GET /api/jobs`` list and break count assertions.

    The wipe is a single ``DELETE FROM jobs`` — sqlite3 auto-commits
    inside the ``with sqlite3.connect(...):`` block. The singleton's
    ``_db_path`` is whatever ``JOB_QUEUE_DB`` was set to at import
    time (``/tmp/pmbot_conftest_isolation/job_queue.db`` under the
    conftest env-var redirect).
    """
    with sqlite3.connect(_job_queue_singleton._db_path) as conn:
        conn.execute("DELETE FROM jobs")
    # Also ensure no handlers from a prior test are still registered
    # (the lifespan startup that registers default handlers never
    # fires under TestClient(app) — the lifespan is skipped when the
    # context-manager form is NOT used — so handlers are empty here).
    # We DO register the three default handlers explicitly so the
    # API tests can enqueue retrain / backtest / export jobs if needed.
    register_default_handlers(_job_queue_singleton)
    yield


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it
    Starlette re-raises in the test process. Mirrors the pattern in
    ``tests/test_openapi.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding + starts the job-queue
    workers. ``TestClient(app)`` (NOT ``with TestClient(app)``) skips
    the lifespan so each test stays fast AND no workers run — that
    means enqueued jobs stay in ``pending`` until a test explicitly
    starts workers, which the unit tests do but the API tests don't
    need to (they verify the enqueue / list / cancel / stats surface,
    not the worker dispatch).
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry.

    Matches the ``API_TOKEN=test-token-conftest`` env-var redirect
    the conftest applies before any project module is imported.
    """
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


class TestApiEnqueueJob:
    """``POST /api/jobs`` — enqueue + return the new job."""

    def test_enqueue_returns_pending_job(self, client: TestClient, auth_headers):
        """POST returns 200 (the project's standard envelope) with a pending Job."""
        response = client.post(
            "/api/jobs",
            json={"type": "export", "payload": {"kind": "trades"}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "job" in body
        job = body["job"]
        assert job["job_type"] == "export"
        assert job["status"] == "pending"
        assert job["payload"] == {"kind": "trades"}
        assert len(job["job_id"]) == 12
        assert job["created_at"] > 0

    def test_enqueue_default_payload_is_empty_dict(self, client: TestClient, auth_headers):
        """Omitting ``payload`` defaults to ``{}`` (Pydantic Field default_factory)."""
        response = client.post(
            "/api/jobs",
            json={"type": "custom"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["job"]["payload"] == {}

    def test_enqueue_rejects_missing_type(self, client: TestClient, auth_headers):
        """``type`` is required (Pydantic Field ``...``); missing → 422."""
        response = client.post(
            "/api/jobs",
            json={"payload": {}},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_enqueue_rejects_empty_type(self, client: TestClient, auth_headers):
        """Empty-string ``type`` violates ``min_length=1`` → 422."""
        response = client.post(
            "/api/jobs",
            json={"type": "", "payload": {}},
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestApiListJobs:
    """``GET /api/jobs`` — list recent + filter by status."""

    def test_empty_list_returns_200_with_count_zero(self, client: TestClient, auth_headers):
        """A wiped queue returns an empty jobs list."""
        response = client.get("/api/jobs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["jobs"] == []
        assert body["count"] == 0

    def test_list_returns_enqueued_jobs(self, client: TestClient, auth_headers):
        """After enqueueing N jobs, ``GET /api/jobs`` returns them newest-first."""
        ids = []
        for i in range(3):
            r = client.post(
                "/api/jobs",
                json={"type": "custom", "payload": {"i": i}},
                headers=auth_headers,
            )
            assert r.status_code == 200
            ids.append(r.json()["job"]["job_id"])

        response = client.get("/api/jobs", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 3
        returned_ids = [j["job_id"] for j in body["jobs"]]
        # newest-first → reversed insertion order
        assert returned_ids == list(reversed(ids))

    def test_status_filter_returns_only_matching_jobs(self, client: TestClient, auth_headers):
        """``?status=pending`` returns only pending jobs (the wipe leaves all pending)."""
        client.post("/api/jobs", json={"type": "custom", "payload": {}}, headers=auth_headers)
        client.post("/api/jobs", json={"type": "custom", "payload": {}}, headers=auth_headers)

        response = client.get("/api/jobs?status=pending", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert all(j["status"] == "pending" for j in body["jobs"])

    def test_status_filter_excludes_non_matching_jobs(self, client: TestClient, auth_headers):
        """``?status=completed`` returns 0 jobs when only pending exist."""
        client.post("/api/jobs", json={"type": "custom"}, headers=auth_headers)

        response = client.get("/api/jobs?status=completed", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 0
        assert body["jobs"] == []


class TestApiGetJobStats:
    """``GET /api/jobs/stats`` — aggregate queue stats."""

    def test_stats_empty_queue(self, client: TestClient, auth_headers):
        """A wiped queue reports ``total_jobs=0`` + empty by_status."""
        response = client.get("/api/jobs/stats", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total_jobs"] == 0
        assert body["by_status"] == {}
        assert "workers_active" in body
        assert "handlers_registered" in body

    def test_stats_after_enqueue(self, client: TestClient, auth_headers):
        """After enqueueing, ``total_jobs`` reflects the new count."""
        for _ in range(3):
            client.post("/api/jobs", json={"type": "custom"}, headers=auth_headers)
        response = client.get("/api/jobs/stats", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total_jobs"] == 3
        assert body["by_status"].get("pending") == 3

    def test_stats_route_does_not_clash_with_job_id_path(self, client: TestClient, auth_headers):
        """``GET /api/jobs/stats`` is NOT interpreted as ``GET /api/jobs/{job_id="stats"}``.

        Route registration order matters: ``/api/jobs/stats`` is
        registered BEFORE ``/api/jobs/{job_id}`` in server.py so
        FastAPI's path matcher hits the literal-stats route first.
        If the order ever flips, this test would 404 (because no job
        with id ``"stats"`` exists).
        """
        response = client.get("/api/jobs/stats", headers=auth_headers)
        assert response.status_code == 200
        assert "total_jobs" in response.json()


class TestApiGetJob:
    """``GET /api/jobs/{job_id}`` — single-job fetch."""

    def test_get_job_returns_enqueued_job(self, client: TestClient, auth_headers):
        """Round-trip: POST to enqueue, GET to fetch."""
        r = client.post(
            "/api/jobs",
            json={"type": "export", "payload": {"kind": "positions"}},
            headers=auth_headers,
        )
        job_id = r.json()["job"]["job_id"]

        response = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["job"]["job_id"] == job_id
        assert body["job"]["job_type"] == "export"
        assert body["job"]["payload"] == {"kind": "positions"}

    def test_get_job_unknown_id_returns_404(self, client: TestClient, auth_headers):
        """A miss returns 404 (not 200 with a null body)."""
        response = client.get("/api/jobs/nonexistent-id-12345", headers=auth_headers)
        assert response.status_code == 404


class TestApiCancelJob:
    """``POST /api/jobs/{job_id}/cancel`` — pending-only cancellation."""

    def test_cancel_pending_job_returns_200(self, client: TestClient, auth_headers):
        """A freshly-enqueued (pending) job cancels cleanly → 200."""
        r = client.post(
            "/api/jobs",
            json={"type": "custom", "payload": {}},
            headers=auth_headers,
        )
        job_id = r.json()["job"]["job_id"]

        response = client.post(f"/api/jobs/{job_id}/cancel", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["cancelled"] is True
        assert body["job"]["status"] == "cancelled"
        assert body["job"]["completed_at"] is not None

    def test_cancel_unknown_id_returns_404(self, client: TestClient, auth_headers):
        """A miss returns 404 (not 200 with cancelled=False)."""
        response = client.post("/api/jobs/nonexistent-id/cancel", headers=auth_headers)
        assert response.status_code == 404

    def test_cancel_non_pending_job_returns_409(self, client: TestClient, auth_headers):
        """A CANCELLED job cannot be cancelled again → 409.

        Uses the singleton directly to flip the status to CANCELLED
        (mimicking a prior cancel call) without going through the API,
        then asserts the API surfaces 409 on the second cancel.
        """
        r = client.post(
            "/api/jobs",
            json={"type": "custom", "payload": {}},
            headers=auth_headers,
        )
        job_id = r.json()["job"]["job_id"]
        # First cancel succeeds.
        first = client.post(f"/api/jobs/{job_id}/cancel", headers=auth_headers)
        assert first.status_code == 200
        # Second cancel on the now-cancelled job must 409.
        second = client.post(f"/api/jobs/{job_id}/cancel", headers=auth_headers)
        assert second.status_code == 409


class TestApiAuthEnforced:
    """Auth contract: the ``/api/jobs`` surface requires a bearer token."""

    def test_jobs_routes_require_auth(self, client: TestClient):
        """No auth header → 401 (the global ``enforce_api_auth`` middleware).

        Verifies the W17-8 routes are NOT in ``PUBLIC_PATHS`` — only
        ``/api/health`` + ``/api/version`` are public. Every other
        route, including the five new ``/api/jobs*`` paths, must 401
        on a missing / invalid token.
        """
        # No Authorization header at all.
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/jobs/stats").status_code == 401
        # Invalid token.
        bad_headers = {"Authorization": "Bearer wrong-token"}
        assert client.get("/api/jobs", headers=bad_headers).status_code == 401
        assert client.post(
            "/api/jobs",
            json={"type": "custom"},
            headers=bad_headers,
        ).status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _poll_until_status(
    queue: JobQueue,
    job_id: str,
    target: JobStatus,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> Job | None:
    """Poll ``queue.get_job`` until the status flips to ``target`` or timeout.

    Returns the final ``Job`` (or ``None`` if the timeout elapses
    before the target status is reached — the caller's subsequent
    assertions will surface that as a test failure).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = queue.get_job(job_id)
        if job is not None and job.status is target:
            return job
        time.sleep(interval)
    return queue.get_job(job_id)


# Late import so the API-route tests pick up the SAME app instance
# the rest of the suite uses (the autouse ``_wipe_job_queue_db``
# fixture above relies on the singleton's ``_db_path`` being the
# conftest-redirected one — importing here ensures the env-var
# redirect is in place before the singleton is constructed).
from api.server import app  # noqa: E402
