"""tests/test_raw_vault_api.py — W34-4 raw vault replay API coverage.

End-to-end HTTP verification of the four raw-vault / replay / checkpoint
routes surfaced under the ``ingestion`` OpenAPI tag in ``api/server.py``:

  Raw vault
    * GET  /api/ingestion/raw/recent              recent raw records
    * GET  /api/ingestion/raw/{record_id}         single raw record by id

  Replay
    * POST /api/ingestion/replay                  re-feed raw vault records
                                                   through the pipeline

  Checkpoints
    * GET  /api/ingestion/checkpoints             list every source's last
                                                   processed position

The W34-4 task spec is "Raw vault replay API" — three of the four routes
(``raw/recent``, ``raw/{record_id}``, ``checkpoints``) were already
shipped by the W32-3 ingestion-API task; this module provides dedicated
regression coverage for them AND covers the NEW ``POST
/api/ingestion/replay`` route that W34-4 introduces (the W32-3
``test_ingestion_api.py`` module pre-dates the replay route, so the
replay coverage is fresh ground here).

Strategy mirrors ``tests/test_ingestion_api.py``: the production
``api.server.app`` is imported ONCE per test (via the ``client``
fixture); the shared limiter is disabled by ``conftest.py`` so the
``WRITE_LIMIT`` / ``READ_LIMIT`` decorators don't 429 the second
request in a class. Each test seeds the underlying singleton
(``raw_vault`` / ``checkpoint_manager``) DIRECTLY via its module's
public API so the route's response can be asserted against a known
state — no mocking of the singleton itself, only direct seeding.

Auth enforcement is verified for every route via the ``_no_auth_*``
tests (the route must 401 when the ``Authorization`` header is missing).
Error-handling is verified via the ``_404`` / ``_422`` tests.

Tests are SYNC ``def test_...`` — ``TestClient`` bridges each request
into the async route handlers (mirrors ``tests/test_openapi.py`` /
``tests/test_ingestion_api.py``).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_ingestion_api.py``) so a
# sibling test file invoked directly
# (``python -m pytest tests/test_raw_vault_api.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_w34_4_raw_vault_api_tests")
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
    # W31-4 dead-letter queue SQLite db (the replay path may push records
    # there if validation fails — belt-and-braces: redirect so a stray
    # DLQ write doesn't pollute the sandbox's /app/data).
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    # W31-4 checkpoint manager SQLite db.
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    # NOTE: ``API_TOKEN`` is intentionally NOT set here — the project-
    # level ``tests/conftest.py`` runs BEFORE this module and sets
    # ``API_TOKEN=test-token-conftest`` via its own ``setdefault``. Our
    # ``_VALID_TOKEN`` constant below mirrors that, so the
    # ``enforce_api_auth`` middleware accepts the bearer token our
    # ``auth_headers`` fixture sends. (Setting a different token here
    # would have no effect — conftest's setdefault already won the
    # race, and our request would 401.)
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
# ``tests/test_ingestion_infra.py`` / ``tests/test_ingestion_api.py``.
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
    mirrors the pattern in ``tests/test_ingestion_api.py`` /
    ``tests/test_backtest_api.py``.

    The limiter is disabled in ``conftest.py`` so the ``WRITE_LIMIT`` /
    ``READ_LIMIT`` decorators on the W34-4 routes don't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry.

    Mirrors the ``auth_headers`` fixture in ``tests/test_openapi.py`` /
    ``tests/test_ingestion_api.py`` so the ``enforce_api_auth`` middleware
    accepts the request.
    """
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_ingestion_singletons():
    """Reset the W31-1 / W31-4 ingestion singletons before every test.

    The API routes under ``/api/ingestion/*`` read from the module-level
    singletons (``raw_vault`` / ``checkpoint_manager``) — these persist
    across tests within a pytest session. Without a reset, a prior
    test's seeded record would leak into the next test's HTTP
    response and break the count / membership assertions.

    Belt-and-braces with the existing ``_reset_store_factory_defaults``
    conftest fixture (which resets ``store`` / ``risk_manager`` /
    ``paper_sim``). The reset is best-effort — a transient SQLite
    hiccup is swallowed so the test session can still proceed (the
    next test's seed will retry the write).
    """
    # W31-1 raw vault — clear the in-memory dedup deque + counters AND
    # truncate the on-disk SQLite table. The W34-4 ``to_timestamp`` /
    # ``from_timestamp`` filter tests assert on exact ``scanned`` counts;
    # without a truncate, records seeded by a prior pytest run (whose
    # on-disk SQLite file persists in ``/tmp``) leak into the count and
    # break the filter assertions non-deterministically. The
    # ``truncate()`` method is the W34-4 test-only helper for this
    # exact purpose — production code never calls it (the vault's
    # contract is "every record survives for audit").
    try:
        from ingestion.raw_vault import raw_vault
        raw_vault.truncate()
    except Exception:  # pragma: no cover — defensive
        try:
            from ingestion.raw_vault import raw_vault
            raw_vault.reset_stats()
        except Exception:
            pass
    # W31-4 dead-letter queue — clear every record from the queue (the
    # replay path may push invalid records to the DLQ; clearing here so
    # the next test's DLQ state is predictable).
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


def _seed_raw_vault_record(
    source: str = "clob",
    event_type: str = "snapshot",
    payload: dict | None = None,
    event_ts: float | None = None,
) -> str:
    """Seed the W31-1 ``raw_vault`` singleton with one synthetic record.

    Returns the ``observation_id`` of the seeded record so a test can
    assert on the W34-4 ``GET /api/ingestion/raw/{record_id}`` response.

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

    unique_id = f"test-source-id-{int(time.time() * 1000)}-{os.getpid()}-{_seed_raw_vault_record._counter}"
    _seed_raw_vault_record._counter += 1
    obs_id = raw_vault.record_observation(
        source=source,
        source_id=unique_id,
        event_type=event_type,
        raw_payload=payload if payload is not None else {
            "token_id": "test-token-1", "best_bid": 0.50, "best_ask": 0.51,
            "_seed_id": unique_id,
        },
        event_timestamp=event_ts if event_ts is not None else time.time(),
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


_seed_raw_vault_record._counter = 0  # type: ignore[attr-defined]


def _seed_checkpoint_for_source(source: str = "clob_rest") -> None:
    """Seed the W31-4 ``checkpoint_manager`` singleton with a checkpoint.

    Uses ``save`` with a single field (``last_processed``) so the test
    can assert the W34-4 ``GET /api/ingestion/checkpoints`` response
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
        obs_id = _seed_raw_vault_record()
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
        _seed_raw_vault_record(source="clob")  # source="clob"
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
        obs_id = _seed_raw_vault_record()
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
# 3. Replay — POST /api/ingestion/replay
# ═══════════════════════════════════════════════════════════════════════════


class TestReplayRoute:
    """``POST /api/ingestion/replay`` — re-feed raw vault records through
    the ingestion pipeline."""

    def test_returns_200_with_zero_state_for_empty_vault(self, client, auth_headers):
        """When no records match the source filter, the replay returns
        200 with ``scanned=0`` and every counter at zero — no
        fabrication, no 500.

        Uses a UNIQUE source name (``w34_4_empty``) so prior-test
        records (which persist in the on-disk SQLite table — the
        vault has no public truncate method by design) don't leak
        into this test's ``scanned`` count.
        """
        response = client.post(
            "/api/ingestion/replay?source=w34_4_empty",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/ingestion/replay must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        for field in (
            "source", "from_timestamp", "to_timestamp", "limit",
            "scanned", "reprocessed", "duplicates", "invalid", "stale",
            "errors", "error_samples", "vault_stats", "replayed_at",
        ):
            assert field in data, (
                f"replay summary must include {field!r}; got "
                f"{sorted(data.keys())}"
            )
        assert data["source"] == "w34_4_empty"
        assert data["scanned"] == 0
        assert data["reprocessed"] == 0
        assert data["duplicates"] == 0
        assert data["errors"] == 0
        assert isinstance(data["error_samples"], list)
        assert isinstance(data["vault_stats"], dict)

    def test_missing_source_param_returns_422(self, client, auth_headers):
        """``source`` is a required query param — omitting it must 422
        (FastAPI's standard validation error)."""
        response = client.post(
            "/api/ingestion/replay",
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"missing source param must 422; got {response.status_code}"
        )

    def test_replay_seeded_record_classifies_into_summary(self, client, auth_headers):
        """When the vault has a seeded record, the replay reads it
        back and runs ``pipeline.process`` against it. The seeded
        record's outcome is tallied into one of the summary buckets
        (``reprocessed`` / ``duplicates`` / ``invalid`` / ``stale``).

        The seeded record uses a UNIQUE ``source_id`` per call so the
        vault's UNIQUE constraint doesn't reject it. The pipeline's
        dedup deque, however, has been cleared by the autouse reset
        fixture so the first replay should re-feed the record into
        the pipeline cleanly. The result is at least one record
        scanned + bucketed.
        """
        obs_id = _seed_raw_vault_record(source="clob")
        response = client.post(
            "/api/ingestion/replay?source=clob",
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"replay after seeding must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["source"] == "clob"
        assert data["scanned"] >= 1, (
            f"replay should scan ≥1 record after seeding; got "
            f"scanned={data['scanned']}"
        )
        # The total of every bucket must equal ``scanned`` (every record
        # is classified into exactly one bucket OR the errors bucket).
        classified = (
            data["reprocessed"] + data["duplicates"]
            + data["invalid"] + data["stale"] + data["errors"]
        )
        assert classified == data["scanned"], (
            f"every scanned record must be classified (reprocessed + "
            f"duplicates + invalid + stale + errors == scanned); got "
            f"classified={classified}, scanned={data['scanned']}"
        )
        # Seeded record's observation_id is in the vault's recent set
        # — sanity-check by reading it back via the raw-recent route.
        recent = client.get(
            "/api/ingestion/raw/recent?source=clob",
            headers=auth_headers,
        ).json()
        ids = [r.get("observation_id") for r in recent["records"]]
        assert obs_id in ids, (
            f"seeded obs_id {obs_id!r} should be in vault; got {ids}"
        )

    def test_from_timestamp_filter_excludes_old_records(self, client, auth_headers):
        """``from_timestamp`` excludes records older than the cutoff.

        Uses a UNIQUE source name (``w34_4_from_ts``) so prior-test
        records don't leak into the count — only the seeded OLD
        record exists for this source, and it must be EXCLUDED by
        the from_timestamp filter (cutoff = 1m ago; record = 1h ago).
        """
        old_ts = time.time() - 3600  # 1 hour ago
        _seed_raw_vault_record(source="w34_4_from_ts", event_ts=old_ts)
        from_ts = time.time() - 60  # 1 minute ago cutoff
        response = client.post(
            f"/api/ingestion/replay?source=w34_4_from_ts&from_timestamp={from_ts}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["from_timestamp"] == from_ts
        # The old record (1h ago) should be EXCLUDED by the
        # from_timestamp filter (cutoff = 1m ago). scanned == 0
        # because the only record for this source is too old.
        assert data["scanned"] == 0, (
            f"from_timestamp filter should exclude the 1h-old seeded "
            f"record; got scanned={data['scanned']}"
        )

    def test_to_timestamp_filter_excludes_new_records(self, client, auth_headers):
        """``to_timestamp`` excludes records newer than the cutoff.

        Uses a UNIQUE source name (``w34_4_to_ts``) so prior-test
        records don't leak into the count — only the seeded NEW
        record exists for this source, and it must be EXCLUDED by
        the to_timestamp filter (cutoff = 1h ago; record = now).
        """
        _seed_raw_vault_record(source="w34_4_to_ts", event_ts=time.time())
        to_ts = time.time() - 3600  # 1 hour ago cutoff
        response = client.post(
            f"/api/ingestion/replay?source=w34_4_to_ts&to_timestamp={to_ts}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["to_timestamp"] == to_ts
        assert data["scanned"] == 0, (
            f"to_timestamp filter should exclude the just-seeded "
            f"record; got scanned={data['scanned']}"
        )

    def test_limit_param_caps_replay_size(self, client, auth_headers):
        """The ``limit`` query param caps the number of records replayed."""
        _seed_raw_vault_record(source="clob")
        response = client.post(
            "/api/ingestion/replay?source=clob&limit=1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["scanned"] <= 1, (
            f"limit=1 should cap replay at 1 record; got "
            f"scanned={data['scanned']}"
        )

    def test_limit_over_max_rejected(self, client, auth_headers):
        """``limit=10001`` exceeds the ``le=10_000`` ceiling — the route
        must 422."""
        response = client.post(
            "/api/ingestion/replay?source=clob&limit=10001",
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"limit=10001 must 422; got {response.status_code}"
        )

    def test_no_auth_returns_401(self, client):
        """``POST /api/ingestion/replay`` without auth must 401."""
        response = client.post("/api/ingestion/replay?source=clob")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 4. Checkpoints — GET /api/ingestion/checkpoints
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckpointsRoute:
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
# 5. OpenAPI tag declaration — verifies the W34-4 replay route carries the
#    ``ingestion`` tag (so Swagger UI groups it with the rest).
# ═══════════════════════════════════════════════════════════════════════════


class TestRawVaultApiTagDeclared:
    """Every W34-4 route carries the ``ingestion`` tag in the OpenAPI
    schema so Swagger UI groups them correctly."""

    def test_replay_route_carries_ingestion_tag(self, client, auth_headers):
        """``POST /api/ingestion/replay`` must be declared in the
        OpenAPI schema under the ``ingestion`` tag."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        assert "/api/ingestion/replay" in paths, (
            f"OpenAPI schema must declare /api/ingestion/replay; got "
            f"{sorted(paths.keys())[:50]}..."
        )
        post_op = paths["/api/ingestion/replay"].get("post")
        assert post_op is not None, (
            "POST /api/ingestion/replay must have a POST operation"
        )
        tags = post_op.get("tags", [])
        assert "ingestion" in tags, (
            f"POST /api/ingestion/replay must carry tags=['ingestion']; "
            f"got {tags}"
        )

    def test_all_w34_4_routes_declared(self, client, auth_headers):
        """Every W34-4 route must be declared in the OpenAPI schema
        (catches a regression where a route is accidentally deleted)."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        w34_4_paths = [
            "/api/ingestion/raw/recent",
            "/api/ingestion/raw/{record_id}",
            "/api/ingestion/replay",
            "/api/ingestion/checkpoints",
        ]
        for path in w34_4_paths:
            assert path in paths, (
                f"OpenAPI schema must declare path {path!r}; got "
                f"{sorted(paths.keys())[:50]}..."
            )
