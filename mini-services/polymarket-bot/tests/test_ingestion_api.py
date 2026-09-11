"""tests/test_ingestion_api.py — W32-3 ingestion API route coverage.

End-to-end HTTP verification of the W32-3 ingestion control routes added
to ``api/server.py``. The W31-5 ``IngestionHealthPanel`` React component
already polls ``GET /api/ingestion/health`` / ``/quality`` / ``/dead-
letter`` / ``/coverage`` / ``/gaps`` — this module covers the W32-3
additions that surface the W31-4 ingestion infrastructure (raw vault,
backfill engine, dead-letter queue, checkpoint manager, pipeline control)
through a REST surface an operator can drive from a terminal.

Routes covered (all under the ``ingestion`` OpenAPI tag):

  Raw vault
    * GET  /api/ingestion/raw/recent                     recent raw records
    * GET  /api/ingestion/raw/{record_id}                 single raw record

  Backfill
    * POST /api/ingestion/backfill/markets               kick off metadata
    * POST /api/ingestion/backfill/prices/{token_id}     kick off prices
    * GET  /api/ingestion/backfill/status                run ledger + cps

  Dead-letter queue
    * GET    /api/ingestion/dead-letter                  queue depth + items
    * POST   /api/ingestion/dead-letter/retry            retry one / all
    * DELETE /api/ingestion/dead-letter/{record_id}      hard-delete one

  Checkpoints
    * GET  /api/ingestion/checkpoints                    list every source
    * POST /api/ingestion/checkpoints/{source}/reset     hard-reset one

  Pipeline control
    * POST /api/ingestion/pipeline/start                 flip running=True
    * POST /api/ingestion/pipeline/stop                  flip running=False
    * GET  /api/ingestion/pipeline/status                running + stats

Strategy mirrors ``tests/test_backtest_api.py`` and
``tests/test_openapi.py``: the production ``api.server.app`` is imported
ONCE per test (via the ``client`` fixture); the shared limiter is disabled
by ``conftest.py`` so the ``WRITE_LIMIT`` / ``READ_LIMIT`` decorators
don't 429 the second request in a class. Each test seeds the underlying
singleton (``raw_vault`` / ``dead_letter_queue`` / ``checkpoint_manager``
/ ``backfill_engine.store``) DIRECTLY via its module's public API so the
route's response can be asserted against a known state — no mocking of
the singleton itself, only direct seeding.

Auth enforcement is verified for every route via the ``_no_auth_*``
tests (the route must 401 when the ``Authorization`` header is missing).
Error-handling is verified via the ``_404`` / ``_422`` tests.

Tests are SYNC ``def test_...`` — ``TestClient`` bridges each request
into the async route handlers (mirrors ``tests/test_openapi.py``).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_api.py``) so a
# sibling test file invoked directly
# (``python -m pytest tests/test_ingestion_api.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_ingestion_api_tests")
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
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    # W31-1 raw vault SQLite db. Module-level singleton ``raw_vault`` is
    # constructed at import time and would otherwise try to mkdir
    # ``/app/data`` (read-only in the sandbox).
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    # W31-4 dead-letter queue SQLite db.
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    # W31-4 checkpoint manager SQLite db.
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*``, ``core.*``, ``api.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import
# mode inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — same fix as
# ``tests/test_ingestion_infra.py`` (see the long note in that file).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package — same defensive cache-clear as
# ``tests/test_ingestion_infra.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

# ── Shared fixtures ─────────────────────────────────────────────────────────
# Bearer token the conftest sets up (via ``API_TOKEN=test-token-conftest``).
_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_backtest_api.py``.

    The limiter is disabled in ``conftest.py`` so the ``WRITE_LIMIT`` /
    ``READ_LIMIT`` decorators on the W32-3 routes don't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry.

    Mirrors the ``auth_headers`` fixture in ``tests/test_openapi.py`` /
    ``tests/test_backtest_api.py`` so the ``enforce_api_auth`` middleware
    accepts the request.
    """
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_ingestion_singletons():
    """Reset the W31-1 / W31-4 ingestion singletons before every test.

    The API routes under ``/api/ingestion/*`` read from the module-level
    singletons (``raw_vault`` / ``dead_letter_queue`` /
    ``checkpoint_manager`` / ``backfill_engine.store``) — these persist
    across tests within a pytest session. Without a reset, a prior
    test's seeded record would leak into the next test's HTTP
    response and break the count / membership assertions.

    Belt-and-braces with the existing ``_reset_store_factory_defaults``
    conftest fixture (which resets ``store`` / ``risk_manager`` /
    ``paper_sim``). The reset is best-effort — a transient SQLite
    hiccup is swallowed so the test session can still proceed (the
    next test's seed will retry the write).
    """
    # W31-1 raw vault — clear the in-memory dedup deque + counters.
    # NOTE: this does NOT truncate the on-disk SQLite table (the vault
    # has no public truncate method by design — every record survives
    # for audit). The dedup deque clear is sufficient because the next
    # seed call uses a UNIQUE source_id (timestamp-suffixed) so the
    # in-memory dedup check won't reject it.
    try:
        from ingestion.raw_vault import raw_vault
        raw_vault.reset_stats()
    except Exception:  # pragma: no cover — defensive
        pass
    # W31-4 dead-letter queue — clear every record from the queue.
    # ``clear()`` with no args drops every row from the underlying
    # ``dead_letter`` SQLite table.
    try:
        from ingestion.dead_letter import dead_letter_queue
        dead_letter_queue.clear()
    except Exception:  # pragma: no cover — defensive
        pass
    # W31-4 checkpoint manager — clear every checkpoint row.
    try:
        from ingestion.checkpoint import checkpoint_manager
        checkpoint_manager.clear()
    except Exception:  # pragma: no cover — defensive
        pass

    yield  # ── test runs ──

    # No post-test teardown — the pre-test reset of the NEXT test
    # cleans up whatever the prior test seeded.


# ── Module-level helpers ────────────────────────────────────────────────────


def _seed_raw_vault_with_one_record() -> str:
    """Seed the W31-1 ``raw_vault`` singleton with one synthetic record.

    Returns the ``observation_id`` of the seeded record so a test can
    assert on the W32-3 ``GET /api/ingestion/raw/{record_id}`` response.

    Uses the module-level singleton (the same one the route reads) so
    the route's response reflects the seeded state without any mocking.
    The seed uses a UNIQUE ``source_id`` per call (timestamp-suffixed)
    so the vault's dedup UNIQUE constraint
    ``(source, source_id, payload_hash)`` doesn't reject the second
    insert after the autouse ``_reset_ingestion_singletons`` fixture
    has cleared the in-memory dedup deque (the on-disk SQLite table
    is NOT truncated by the reset — every record survives for audit).
    """
    from ingestion.raw_vault import raw_vault

    unique_id = f"test-source-id-{int(time.time() * 1000)}-{os.getpid()}"
    obs_id = raw_vault.record_observation(
        source="clob",
        source_id=unique_id,
        event_type="snapshot",
        raw_payload={"token_id": "test-token-1", "best_bid": 0.50, "best_ask": 0.51,
                     "_seed_id": unique_id},
        event_timestamp=time.time(),
        validation_status="valid",
        quality_score=1.0,
    )
    # ``record_observation`` returns ``None`` on dedup / storage error.
    # In tests we WANT a non-None id so the route can fetch it back —
    # fail fast here (rather than in the test's HTTP assertion) so the
    # failure message names the actual problem.
    assert obs_id, (
        "raw_vault.record_observation returned None — vault may be "
        "unwritable in the sandbox; check RAW_VAULT_DB_PATH"
    )
    return obs_id


def _seed_dlq_with_one_record() -> str:
    """Seed the W31-4 ``dead_letter_queue`` singleton with one record.

    Returns the ``record_id`` (UUID4 hex). Alert firing is left
    ENABLED — the conftest's ``alert_engine`` singleton is constructed
    against the conftest's redirected ``AUDIT_DB_PATH`` so the alert
    persists without polluting the sandbox's ``/app/data/alerts.db``.
    """
    from ingestion.dead_letter import dead_letter_queue

    record_id = dead_letter_queue.add(
        source="clob_rest",
        record_type="snapshot",
        payload={"token_id": "test-token-1", "best_bid": 0.50},
        reason="validation_failed",
        error="missing required field 'asks'",
        metadata={"test": "test_ingestion_api"},
    )
    assert record_id, (
        "dead_letter_queue.add returned empty string — DLQ db may be "
        "unwritable in the sandbox; check DLQ_DB_PATH"
    )
    return record_id


def _seed_checkpoint_for_source(source: str = "clob_rest") -> None:
    """Seed the W31-4 ``checkpoint_manager`` singleton with a checkpoint.

    Uses ``save`` with a single field (``last_processed``) so the test
    can assert the W32-3 ``GET /api/ingestion/checkpoints`` response
    includes the seeded source.
    """
    from ingestion.checkpoint import checkpoint_manager

    ok = checkpoint_manager.save(
        source=source,
        last_processed=time.time(),
        last_processed_type="timestamp",
    )
    assert ok, (
        "checkpoint_manager.save returned False — checkpoint db may be "
        "unwritable in the sandbox; check CHECKPOINT_DB_PATH"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Raw vault — GET /api/ingestion/raw/recent
# ═══════════════════════════════════════════════════════════════════════════


class TestRawRecentRoute:
    """``GET /api/ingestion/raw/recent`` — recent raw records from the vault."""

    def test_returns_200_with_empty_vault(self, client, auth_headers):
        """``GET /api/ingestion/raw/recent`` must return 200 with the
        zero-state (empty ``records`` list) when the vault hasn't
        received any records yet — no fabrication, no 500."""
        response = client.get("/api/ingestion/raw/recent", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ingestion/raw/recent must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "records" in data
        assert isinstance(data["records"], list)
        assert "count" in data
        assert data["count"] == len(data["records"])
        assert "vault_stats" in data
        assert "generated_at" in data

    def test_returns_seeded_record(self, client, auth_headers):
        """When the vault has been seeded with a record, the route
        surfaces it in the ``records`` list."""
        obs_id = _seed_raw_vault_with_one_record()
        response = client.get("/api/ingestion/raw/recent", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1, (
            f"expected ≥1 record after seeding; got count={data['count']}"
        )
        # The most-recent record should be the one we just seeded.
        ids = [r.get("observation_id") for r in data["records"]]
        assert obs_id in ids, (
            f"seeded observation_id {obs_id!r} not in returned ids {ids}"
        )

    def test_source_filter_narrows_results(self, client, auth_headers):
        """The ``source`` query param narrows the result set to records
        from that source only."""
        _seed_raw_vault_with_one_record()  # source="clob"
        response = client.get(
            "/api/ingestion/raw/recent?source=clob",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["source_filter"] == "clob"
        for rec in data["records"]:
            assert rec["source"] == "clob", (
                f"source=clob filter should narrow to clob records; "
                f"got source={rec['source']!r}"
            )

    def test_limit_param_caps_response_size(self, client, auth_headers):
        """The ``limit`` query param caps the number of records returned."""
        response = client.get(
            "/api/ingestion/raw/recent?limit=1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["records"]) <= 1, (
            f"limit=1 should cap response at 1 record; got "
            f"{len(data['records'])}"
        )

    def test_limit_over_max_rejected(self, client, auth_headers):
        """``limit=1001`` exceeds the ``le=1000`` ceiling — the route
        must 422 (FastAPI's standard validation error)."""
        response = client.get(
            "/api/ingestion/raw/recent?limit=1001",
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"limit=1001 must 422; got {response.status_code}"
        )

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/raw/recent`` without an Authorization
        header must 401 (fail-closed auth middleware)."""
        response = client.get("/api/ingestion/raw/recent")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 2. Raw vault — GET /api/ingestion/raw/{record_id}
# ═══════════════════════════════════════════════════════════════════════════


class TestRawRecordRoute:
    """``GET /api/ingestion/raw/{record_id}`` — single raw record by id."""

    def test_returns_200_for_existing_record(self, client, auth_headers):
        """The route returns the full record (with ``raw_payload``
        parsed back from JSON) for an existing ``observation_id``."""
        obs_id = _seed_raw_vault_with_one_record()
        response = client.get(
            f"/api/ingestion/raw/{obs_id}",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/ingestion/raw/{{id}} must return 200 for an "
            f"existing record; got {response.status_code}. "
            f"Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "record" in data
        rec = data["record"]
        assert rec["observation_id"] == obs_id
        assert rec["source"] == "clob"
        # ``raw_payload`` should be parsed back from JSON to a dict.
        assert isinstance(rec["raw_payload"], dict)
        assert "token_id" in rec["raw_payload"]

    def test_returns_404_for_unknown_record(self, client, auth_headers):
        """The route returns 404 when the ``observation_id`` isn't in
        the vault (deleted / never stored / dedup-rejected)."""
        response = client.get(
            "/api/ingestion/raw/does-not-exist-uuid",
            headers=auth_headers,
        )
        assert response.status_code == 404, (
            f"GET /api/ingestion/raw/{{id}} must 404 for an unknown id; "
            f"got {response.status_code}"
        )
        data = response.json()
        # FastAPI's HTTPException(404) returns ``{"detail": "..."}``.
        assert "detail" in data

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/raw/{record_id}`` without an
        Authorization header must 401."""
        response = client.get("/api/ingestion/raw/any-id")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 3. Backfill — POST /api/ingestion/backfill/markets
# ═══════════════════════════════════════════════════════════════════════════


class TestBackfillMarketsRoute:
    """``POST /api/ingestion/backfill/markets`` — kick off metadata backfill."""

    def test_returns_200_with_task_id(self, client, auth_headers):
        """The route kicks off the backfill as a background task and
        returns immediately with a ``task_id``."""
        response = client.post(
            "/api/ingestion/backfill/markets",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/backfill/markets must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["status"] == "started"
        assert "task_id" in data and data["task_id"]
        assert data["type"] == "metadata"
        assert data["resume"] is True  # default
        assert "started_at" in data

    def test_resume_param_echoed(self, client, auth_headers):
        """The ``resume`` query param is echoed in the response."""
        response = client.post(
            "/api/ingestion/backfill/markets?resume=false",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["resume"] is False

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/backfill/markets`` without auth must 401."""
        response = client.post("/api/ingestion/backfill/markets")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 4. Backfill — POST /api/ingestion/backfill/prices/{token_id}
# ═══════════════════════════════════════════════════════════════════════════


class TestBackfillPricesRoute:
    """``POST /api/ingestion/backfill/prices/{token_id}`` — kick off
    price history backfill for a single market."""

    def test_returns_200_with_task_id(self, client, auth_headers):
        """The route kicks off the backfill and returns immediately
        with a ``task_id``."""
        response = client.post(
            "/api/ingestion/backfill/prices/test-token-1?days=7",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/backfill/prices/{{token}} must return "
            f"200; got {response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["status"] == "started"
        assert "task_id" in data and data["task_id"]
        assert data["type"] == "prices"
        assert data["token_id"] == "test-token-1"
        assert data["days"] == 7
        assert "resolution" in data
        assert "started_at" in data

    def test_default_days_is_30(self, client, auth_headers):
        """The ``days`` query param defaults to 30 when omitted."""
        response = client.post(
            "/api/ingestion/backfill/prices/test-token-1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["days"] == 30

    def test_invalid_resolution_returns_422(self, client, auth_headers):
        """An unsupported ``resolution`` value must 422 with a
        friendly error message listing the valid resolutions."""
        response = client.post(
            "/api/ingestion/backfill/prices/test-token-1?resolution=2h",
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"unsupported resolution must 422; got {response.status_code}"
        )
        data = response.json()
        # FastAPI's HTTPException(422) returns ``{"detail": "..."}``.
        assert "detail" in data
        assert "2h" in data["detail"]

    def test_days_over_max_returns_422(self, client, auth_headers):
        """``days=366`` exceeds the ``le=365`` ceiling — the route
        must 422 (FastAPI validation)."""
        response = client.post(
            "/api/ingestion/backfill/prices/test-token-1?days=366",
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/backfill/prices/{token_id}`` without
        auth must 401."""
        response = client.post(
            "/api/ingestion/backfill/prices/test-token-1"
        )
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 5. Backfill — GET /api/ingestion/backfill/status
# ═══════════════════════════════════════════════════════════════════════════


class TestBackfillStatusRoute:
    """``GET /api/ingestion/backfill/status`` — run ledger + per-type
    checkpoint state + engine tunables."""

    def test_returns_200_with_runs_and_checkpoints(self, client, auth_headers):
        """The route returns the ``backfill_runs`` ledger (possibly
        empty), the per-type checkpoint state, and the engine tunables."""
        response = client.get(
            "/api/ingestion/backfill/status",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/ingestion/backfill/status must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "runs" in data
        assert isinstance(data["runs"], list)
        # Per-type checkpoint state — every BackfillType except ``all``.
        cps = data["checkpoints"]
        for bt in ("metadata", "prices", "trades", "outcomes", "snapshots"):
            assert bt in cps, (
                f"backfill status must include checkpoint for type {bt!r}; "
                f"got {sorted(cps.keys())}"
            )
        # Engine tunables — surfaced so an operator can see the
        # configured RPS / concurrency / page size alongside the run
        # history.
        es = data["engine_stats"]
        for field in ("target_rps", "current_interval", "concurrency",
                      "page_size", "max_pages"):
            assert field in es, (
                f"engine_stats must include {field!r}; got {sorted(es.keys())}"
            )
        assert "generated_at" in data

    def test_limit_param_caps_response_size(self, client, auth_headers):
        """The ``limit`` query param caps the number of runs returned."""
        response = client.get(
            "/api/ingestion/backfill/status?limit=5",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["runs"]) <= 5

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/backfill/status`` without auth must 401."""
        response = client.get("/api/ingestion/backfill/status")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 6. DLQ — GET /api/ingestion/dead-letter
# ═══════════════════════════════════════════════════════════════════════════


class TestDeadLetterGetRoute:
    """``GET /api/ingestion/dead-letter`` — queue depth + recent items."""

    def test_returns_200_with_empty_queue(self, client, auth_headers):
        """``GET /api/ingestion/dead-letter`` must return 200 with the
        zero-state (empty ``recent`` list, ``depth=0``) when the queue
        is empty."""
        response = client.get("/api/ingestion/dead-letter", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ingestion/dead-letter must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "depth" in data
        assert "recent" in data
        assert isinstance(data["recent"], list)
        assert "error_breakdown" in data
        assert "generated_at" in data
        # W32-3 — extended contract with pending / retried / abandoned /
        # by_source (sourced from the W31-4 DLQ's get_stats()).
        assert "pending" in data
        assert "retried" in data
        assert "abandoned" in data
        assert "by_source" in data

    def test_returns_seeded_record(self, client, auth_headers):
        """When the DLQ has been seeded, the route surfaces the record
        in the ``recent`` list."""
        record_id = _seed_dlq_with_one_record()
        response = client.get("/api/ingestion/dead-letter", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["depth"] >= 1, (
            f"depth must be ≥1 after seeding; got {data['depth']}"
        )
        ids = [r.get("id") for r in data["recent"]]
        assert record_id in ids, (
            f"seeded record_id {record_id!r} not in returned ids {ids}"
        )
        # Each recent item carries the W32-3 contract fields.
        seeded = next(r for r in data["recent"] if r["id"] == record_id)
        assert seeded["source"] == "clob_rest"
        assert seeded["reason"] == "validation_failed"
        assert seeded["retries"] == 0
        assert seeded["status"] == "pending"
        assert seeded["record_type"] == "snapshot"

    def test_limit_param_default_50(self, client, auth_headers):
        """The ``limit`` query param defaults to 50."""
        # Seed at least one record so the queue is non-empty.
        _seed_dlq_with_one_record()
        response = client.get("/api/ingestion/dead-letter", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["recent"]) <= 50, (
            f"recent must be capped at 50 (default); got {len(data['recent'])}"
        )

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/dead-letter`` without auth must 401."""
        response = client.get("/api/ingestion/dead-letter")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 7. DLQ — POST /api/ingestion/dead-letter/retry
# ═══════════════════════════════════════════════════════════════════════════


class TestDeadLetterRetryRoute:
    """``POST /api/ingestion/dead-letter/retry`` — retry one / all."""

    def test_drain_all_returns_200_with_zero_when_empty(self, client, auth_headers):
        """When the queue is empty, the drain-all path returns
        ``retried=0`` with a friendly message."""
        response = client.post(
            "/api/ingestion/dead-letter/retry",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/dead-letter/retry must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["retried"] == 0
        assert "no pending records to retry" in data["message"]
        assert "attempted_at" in data

    def test_drain_all_marks_seeded_record_retried(self, client, auth_headers):
        """When the queue has a pending record, the drain-all path
        marks it retried and returns ``retried >= 1``."""
        _seed_dlq_with_one_record()
        response = client.post(
            "/api/ingestion/dead-letter/retry",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["retried"] >= 1, (
            f"drain-all must retry ≥1 pending record; got retried={data['retried']}"
        )

    def test_single_record_retry_returns_200(self, client, auth_headers):
        """The ``record_id`` query param narrows the retry to a single
        record. The route returns ``success=True`` and ``retried=1``."""
        record_id = _seed_dlq_with_one_record()
        response = client.post(
            f"/api/ingestion/dead-letter/retry?record_id={record_id}",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"single-record retry must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["retried"] == 1
        assert data["record_id"] == record_id
        assert "marked retried" in data["message"]

    def test_single_record_retry_unknown_id_returns_success_false(self, client, auth_headers):
        """Retry of an unknown ``record_id`` returns ``success=False``
        with a friendly message — NOT a 404 (the route treats "record
        not found" as a no-op rather than an error, mirroring the DLQ's
        best-effort contract)."""
        response = client.post(
            "/api/ingestion/dead-letter/retry?record_id=does-not-exist",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"single-record retry of unknown id must return 200 (no-op), "
            f"not 404; got {response.status_code}"
        )
        data = response.json()
        assert data["success"] is False
        assert data["retried"] == 0
        assert "not found" in data["message"]

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/dead-letter/retry`` without auth must 401."""
        response = client.post("/api/ingestion/dead-letter/retry")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 8. DLQ — DELETE /api/ingestion/dead-letter/{record_id}
# ═══════════════════════════════════════════════════════════════════════════


class TestDeadLetterDeleteRoute:
    """``DELETE /api/ingestion/dead-letter/{record_id}`` — hard-delete one."""

    def test_returns_200_on_existing_record(self, client, auth_headers):
        """The route returns 200 with ``success=True`` for an existing
        record, and the record is removed from the queue."""
        record_id = _seed_dlq_with_one_record()
        response = client.delete(
            f"/api/ingestion/dead-letter/{record_id}",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"DELETE /api/ingestion/dead-letter/{{id}} must return 200; "
            f"got {response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["record_id"] == record_id
        assert "deleted_at" in data

        # Verify the record is gone from the queue.
        from ingestion.dead_letter import dead_letter_queue
        assert dead_letter_queue.get(record_id) is None, (
            f"record {record_id!r} should be deleted from the DLQ"
        )

    def test_returns_200_with_success_false_on_unknown_id(self, client, auth_headers):
        """DELETE of an unknown ``record_id`` returns 200 with
        ``success=False`` (DELETE idempotency — deleting a non-existent
        record is a no-op, not an error)."""
        response = client.delete(
            "/api/ingestion/dead-letter/does-not-exist",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"DELETE of unknown id must return 200 (no-op), not 404; "
            f"got {response.status_code}"
        )
        data = response.json()
        assert data["success"] is False
        assert data["record_id"] == "does-not-exist"

    def test_no_auth_returns_401(self, client):
        """``DELETE /api/ingestion/dead-letter/{record_id}`` without
        auth must 401."""
        response = client.delete("/api/ingestion/dead-letter/any-id")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 9. Checkpoints — GET /api/ingestion/checkpoints
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckpointsGetRoute:
    """``GET /api/ingestion/checkpoints`` — list every checkpoint."""

    def test_returns_200_with_empty_store(self, client, auth_headers):
        """``GET /api/ingestion/checkpoints`` must return 200 with the
        zero-state (empty ``checkpoints`` list, ``count=0``) when no
        checkpoint has been persisted yet."""
        response = client.get("/api/ingestion/checkpoints", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ingestion/checkpoints must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "checkpoints" in data
        assert isinstance(data["checkpoints"], list)
        assert "count" in data
        assert data["count"] == len(data["checkpoints"])
        assert "generated_at" in data

    def test_returns_seeded_checkpoint(self, client, auth_headers):
        """When the checkpoint manager has been seeded, the route
        surfaces it in the ``checkpoints`` list."""
        _seed_checkpoint_for_source("clob_rest")
        response = client.get("/api/ingestion/checkpoints", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1, (
            f"count must be ≥1 after seeding; got {data['count']}"
        )
        sources = [c.get("source") for c in data["checkpoints"]]
        assert "clob_rest" in sources, (
            f"seeded source 'clob_rest' not in returned sources {sources}"
        )
        # The seeded checkpoint should carry the W31-4 contract fields.
        cp = next(c for c in data["checkpoints"] if c["source"] == "clob_rest")
        assert "last_processed" in cp
        assert "last_processed_type" in cp
        assert "last_processed_at" in cp
        assert "offset" in cp
        assert "metadata" in cp

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/checkpoints`` without auth must 401."""
        response = client.get("/api/ingestion/checkpoints")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 10. Checkpoints — POST /api/ingestion/checkpoints/{source}/reset
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckpointResetRoute:
    """``POST /api/ingestion/checkpoints/{source}/reset`` — hard-reset one."""

    def test_returns_200_with_cleared_count_for_existing(self, client, auth_headers):
        """Resetting an existing checkpoint returns 200 with
        ``cleared=1`` and removes the row from the store."""
        _seed_checkpoint_for_source("gamma_api")
        response = client.post(
            "/api/ingestion/checkpoints/gamma_api/reset",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/checkpoints/{{source}}/reset must return "
            f"200; got {response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["source"] == "gamma_api"
        assert data["cleared"] >= 1, (
            f"cleared must be ≥1 for an existing checkpoint; got {data['cleared']}"
        )
        assert "reset_at" in data

        # Verify the checkpoint is gone from the store.
        from ingestion.checkpoint import checkpoint_manager
        assert checkpoint_manager.load("gamma_api") is None, (
            "checkpoint 'gamma_api' should be cleared from the store"
        )

    def test_returns_200_with_cleared_zero_for_unknown(self, client, auth_headers):
        """Resetting a non-existent checkpoint returns 200 with
        ``cleared=0`` (DELETE idempotency)."""
        response = client.post(
            "/api/ingestion/checkpoints/never-existed/reset",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"reset of unknown source must return 200 (no-op), not 404; "
            f"got {response.status_code}"
        )
        data = response.json()
        assert data["success"] is True
        assert data["cleared"] == 0
        assert data["source"] == "never-existed"

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/checkpoints/{source}/reset`` without
        auth must 401."""
        response = client.post(
            "/api/ingestion/checkpoints/any-source/reset"
        )
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 11. Pipeline — POST /api/ingestion/pipeline/start
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineStartRoute:
    """``POST /api/ingestion/pipeline/start`` — flip running=True."""

    def test_returns_200_with_running_true(self, client, auth_headers):
        """The route returns 200 with ``running=True`` and the
        ``started_at`` timestamp."""
        response = client.post(
            "/api/ingestion/pipeline/start",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/pipeline/start must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["status"] == "started"
        assert data["running"] is True
        assert "ws_started" in data
        assert "rest_started" in data
        assert "started_at" in data
        # The flag flip persists across requests within the same module
        # state, so a subsequent ``GET /api/ingestion/pipeline/status``
        # must observe ``running=True``.
        status_resp = client.get(
            "/api/ingestion/pipeline/status",
            headers=auth_headers,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["running"] is True

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/pipeline/start`` without auth must 401."""
        response = client.post("/api/ingestion/pipeline/start")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 12. Pipeline — POST /api/ingestion/pipeline/stop
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineStopRoute:
    """``POST /api/ingestion/pipeline/stop`` — flip running=False."""

    def test_returns_200_with_running_false(self, client, auth_headers):
        """The route returns 200 with ``running=False`` and the
        ``stopped_at`` timestamp."""
        # Start first so the stop has something to flip.
        client.post(
            "/api/ingestion/pipeline/start",
            headers=auth_headers,
        )
        response = client.post(
            "/api/ingestion/pipeline/stop",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/pipeline/stop must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["status"] == "stopped"
        assert data["running"] is False
        assert "ws_stopped" in data
        assert "rest_stopped" in data
        assert "stopped_at" in data
        # The flag flip persists — a subsequent ``GET .../status`` must
        # observe ``running=False``.
        status_resp = client.get(
            "/api/ingestion/pipeline/status",
            headers=auth_headers,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["running"] is False

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/pipeline/stop`` without auth must 401."""
        response = client.post("/api/ingestion/pipeline/stop")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 13. Pipeline — GET /api/ingestion/pipeline/status
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineStatusRoute:
    """``GET /api/ingestion/pipeline/status`` — running flag + per-source
    state + pipeline / raw-vault stats."""

    def test_returns_200_with_state_block(self, client, auth_headers):
        """The route returns 200 with the running flag, the per-source
        running state, the pipeline / raw-vault stats, and the
        last-started / last-stopped timestamps."""
        response = client.get(
            "/api/ingestion/pipeline/status",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/ingestion/pipeline/status must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        for field in (
            "running", "ws_running", "rest_running",
            "ws_reconnect_count", "ws_subscribed_tokens",
            "rest_tracked_tokens", "pipeline_stats", "raw_vault_stats",
            "last_started_at", "last_stopped_at", "generated_at",
        ):
            assert field in data, (
                f"pipeline status must include {field!r}; got "
                f"{sorted(data.keys())}"
            )

    def test_running_flag_reflects_start_stop(self, client, auth_headers):
        """After ``POST /start``, ``running=True``; after ``POST /stop``,
        ``running=False``."""
        client.post(
            "/api/ingestion/pipeline/start",
            headers=auth_headers,
        )
        started = client.get(
            "/api/ingestion/pipeline/status",
            headers=auth_headers,
        ).json()
        assert started["running"] is True

        client.post(
            "/api/ingestion/pipeline/stop",
            headers=auth_headers,
        )
        stopped = client.get(
            "/api/ingestion/pipeline/status",
            headers=auth_headers,
        ).json()
        assert stopped["running"] is False

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/pipeline/status`` without auth must 401."""
        response = client.get("/api/ingestion/pipeline/status")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 14. OpenAPI tag declaration
# ═══════════════════════════════════════════════════════════════════════════


class TestIngestionTagDeclared:
    """The ``ingestion`` tag must be declared in ``openapi_tags`` so
    Swagger UI groups the W31-5 + W32-3 ingestion routes under a
    heading with a description."""

    def test_ingestion_tag_present_with_description(self, client, auth_headers):
        """``openapi_tags`` includes ``ingestion`` with a non-empty
        ``description`` (added by W32-3 alongside the new routes)."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        assert "tags" in schema, "openapi.json must declare top-level 'tags'"
        names = {t.get("name"): t for t in schema["tags"]}
        assert "ingestion" in names, (
            f"openapi.json must declare 'ingestion' tag; got "
            f"{sorted(names.keys())}"
        )
        assert names["ingestion"].get("description"), (
            "ingestion tag must carry a non-empty description"
        )

    def test_all_w32_3_routes_under_ingestion_tag(self, client, auth_headers):
        """Every W32-3 route carries the ``ingestion`` tag in the
        OpenAPI schema so Swagger UI groups them correctly."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        w32_3_paths = [
            "/api/ingestion/raw/recent",
            "/api/ingestion/raw/{record_id}",
            "/api/ingestion/backfill/markets",
            "/api/ingestion/backfill/prices/{token_id}",
            "/api/ingestion/backfill/status",
            "/api/ingestion/dead-letter",
            "/api/ingestion/dead-letter/retry",
            "/api/ingestion/dead-letter/{record_id}",
            "/api/ingestion/checkpoints",
            "/api/ingestion/checkpoints/{source}/reset",
            "/api/ingestion/pipeline/start",
            "/api/ingestion/pipeline/stop",
            "/api/ingestion/pipeline/status",
        ]
        for path in w32_3_paths:
            assert path in paths, (
                f"OpenAPI schema must declare path {path!r}; got "
                f"{sorted(paths.keys())[:50]}..."
            )
            for method, op in paths[path].items():
                if method not in ("get", "post", "delete", "put", "patch"):
                    continue
                tags = op.get("tags", [])
                assert "ingestion" in tags, (
                    f"{method.upper()} {path} must carry tags=['ingestion']; "
                    f"got {tags}"
                )
