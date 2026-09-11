"""
Unit + integration tests for ``core/data_quality.py``.

W20-6 — Data quality monitoring pipeline.

Covers the six behaviours required by the task spec:

  (1) ``run_all_checks()`` against an EMPTY DB (table created, zero rows):
      the freshness check ``fail``s (MAX(timestamp) is NULL → age is huge),
      the tracked-markets completeness check ``warning``s (0 < 10 markets),
      and the remaining checks pass. Overall status = ``critical``
      (because of the freshness fail).
  (2) Freshness check: fresh data (< 60 s old) → ``pass``; stale data
      (> 60 s old) → ``fail``.
  (3) Completeness check: rows with NULL / zero ``mid`` → ``warning``;
      all rows valid → ``pass``.
  (4) Validity check: negative ``best_bid`` / ``best_ask`` → ``fail``;
      prices > 1.0 → ``warning``.
  (5) Overall status determination: every check passes → ``healthy``;
      ≥1 warning, 0 fails → ``degraded``; ≥1 fail → ``critical``.
  (6) API route: ``GET /api/data-quality`` returns 200 + the report
      envelope (overall_status / summary / checks / timestamp).

DB isolation strategy
---------------------
The data-quality monitor reads from ``MARKET_DB_PATH`` (env-overridable,
redirected to ``/tmp/pmbot_conftest_isolation/market_intelligence.db`` by
the repo's ``tests/conftest.py``). Each test passes an explicit ``db_path``
to ``DataQualityMonitor(db_path=...)`` so the unit tests are fully hermetic
(no shared state across tests, no reliance on the env-var redirect). The
module-level singleton ``data_quality_monitor`` is monkeypatched in the
API-route test so the route handler hits the test-scoped DB.

Each test creates the canonical ``market_snapshots`` schema (mirroring
``core/market_db.py`` lines 50–62) so the SQL queries execute against a
real table even when no rows have been inserted yet.

All tests are SYNC ``def`` (the entire ``core/data_quality.py`` module is
sync — no ``asyncio.to_thread`` wraps the checks). The API-route test uses
``fastapi.testclient.TestClient``, which runs the async handler inside its
own sync portal — no ``pytestmark = pytest.mark.asyncio`` is needed.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Defensive env-var redirect (mirrors the established pattern in
# tests/test_retention.py / tests/test_alerting.py). ``setdefault`` lets
# conftest (which loads first) win when present; this block is purely a
# defensive net so the file stays hermetic in a hypothetical conftest-less
# invocation. ``MARKET_DB_PATH`` is the env var ``DataQualityMonitor``
# reads in its constructor.
_TMP_ROOT = Path("/tmp/data_quality_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    # Force paper mode + live disabled so any co-collected stateful test
    # doesn't trip a shadow / live-trading gate at import time.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-data-quality",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.data_quality``) regardless of the cwd pytest was launched from.
# Mirrors the bootstrap pattern in tests/test_features.py /
# tests/test_observability.py / tests/test_decision_ledger.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_quality import (  # noqa: E402
    DataQualityMonitor,
    DataQualityReport,
    QualityCheck,
    data_quality_monitor,
)


# ── Schema helpers ──────────────────────────────────────────────────────────

# The canonical ``market_snapshots`` schema (mirrors
# ``core/market_db.py`` lines 50–62 so the SQL queries in
# ``DataQualityMonitor`` execute against a real table even when no rows
# have been inserted yet).
_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_id TEXT NOT NULL,
    slug TEXT,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread REAL,
    volume_24h REAL,
    liquidity REAL
)
"""


def _create_market_snapshots_table(db_path: Path) -> None:
    """Create the ``market_snapshots`` table in ``db_path`` (idempotent).

    Mirrors the canonical schema in ``core/market_db.py`` so the SQL
    queries in ``DataQualityMonitor`` execute against a real table.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SNAPSHOT_SCHEMA)
        conn.commit()


def _insert_snapshot(
    db_path: Path,
    *,
    token_id: str = "token_001",
    timestamp: float | None = None,
    best_bid: float | None = 0.45,
    best_ask: float | None = 0.55,
    mid: float | None = 0.50,
    spread: float | None = 0.10,
    slug: str = "test-market",
    volume_24h: float = 1000.0,
    liquidity: float = 500.0,
) -> None:
    """Insert one ``market_snapshots`` row with sensible defaults.

    Every field is keyword-only so a test can override just the field(s)
    it wants to vary (e.g. ``mid=None`` for the null-mid completeness test,
    ``best_bid=-0.1`` for the negative-prices validity test).
    """
    ts = timestamp if timestamp is not None else time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots
                (timestamp, token_id, slug, best_bid, best_ask,
                 mid, spread, volume_24h, liquidity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                token_id,
                slug,
                best_bid,
                best_ask,
                mid,
                spread,
                volume_24h,
                liquidity,
            ),
        )
        conn.commit()


# ── Fixture: fresh tmp-path DB + DataQualityMonitor per test ────────────────
@pytest.fixture
def monitor(tmp_path):
    """Return a ``DataQualityMonitor`` whose DB lives under ``tmp_path``.

    The fixture pre-creates the ``market_snapshots`` table so the SQL
    queries in ``_check_freshness`` / ``_check_completeness`` /
    ``_check_validity`` execute against a real (empty) table — matches
    the production state immediately after ``core.market_db.MarketIntelligenceDB
    ._init_db()`` runs.
    """
    db_path = tmp_path / "data_quality.db"
    _create_market_snapshots_table(db_path)
    return DataQualityMonitor(db_path=str(db_path))


# ── 1. run_all_checks against an empty DB ──────────────────────────────────
def test_run_all_checks_with_empty_db(monitor):
    """``run_all_checks()`` against an empty ``market_snapshots`` table
    (table exists, zero rows) must:

      * return a ``DataQualityReport`` with the documented shape
        (overall_status / checks / summary / timestamp);
      * emit at least 4 checks (freshness age + tracked-markets count +
        null-mid + negative-prices + prices-over-1 = 5 checks);
      * mark the freshness check ``fail`` (``MAX(timestamp)`` is NULL →
        age is the full epoch-seconds since 1970 ≈ 1.7 billion seconds,
        well over the 60 s threshold);
      * mark the tracked-markets-count check ``warning`` (0 < 10);
      * mark the remaining completeness / validity checks ``pass``
        (no rows → no NULL mids / no negative prices / no over-1 prices);
      * derive ``overall_status = "critical"`` because of the freshness
        fail.
    """
    report = monitor.run_all_checks()

    # Shape: DataQualityReport dataclass.
    assert isinstance(report, DataQualityReport)
    assert isinstance(report.checks, list)
    assert isinstance(report.summary, dict)
    assert isinstance(report.timestamp, float)
    assert report.timestamp > 0
    assert time.time() - report.timestamp < 5.0

    # At least the 5 canonical checks are present.
    assert len(report.checks) >= 5
    check_names = {c.name for c in report.checks}
    expected_names = {
        "market_data_freshness",
        "tracked_markets_count",
        "null_mid_prices",
        "negative_prices",
        "prices_over_1",
    }
    assert expected_names.issubset(check_names), (
        f"missing checks: {expected_names - check_names}"
    )

    # Freshness check: empty table → MAX(timestamp) is NULL → age is
    # (now - 0) ≈ 1.7 billion seconds → fail.
    freshness = next(c for c in report.checks if c.name == "market_data_freshness")
    assert freshness.category == "freshness"
    assert freshness.status == "fail"
    assert "old" in freshness.value
    assert freshness.threshold == "< 60s"

    # Tracked-markets count: 0 distinct tokens in last 5 min → warning.
    tracked = next(c for c in report.checks if c.name == "tracked_markets_count")
    assert tracked.category == "completeness"
    assert tracked.status == "warning"
    assert tracked.value == 0
    assert tracked.threshold == ">= 10"

    # Completeness (null mid): 0 rows → 0 nulls → pass.
    null_mid = next(c for c in report.checks if c.name == "null_mid_prices")
    assert null_mid.status == "pass"
    assert null_mid.value == 0

    # Validity (negative prices): 0 rows → pass.
    neg = next(c for c in report.checks if c.name == "negative_prices")
    assert neg.status == "pass"
    assert neg.value == 0

    # Validity (prices > 1): 0 rows → pass.
    over = next(c for c in report.checks if c.name == "prices_over_1")
    assert over.status == "pass"
    assert over.value == 0

    # Summary counts.
    assert report.summary["total_checks"] == len(report.checks)
    assert report.summary["failed"] >= 1  # freshness fail
    assert report.summary["warnings"] >= 1  # tracked-markets warning

    # Overall: critical because of the freshness fail.
    assert report.overall_status == "critical"


# ── 2. freshness check ─────────────────────────────────────────────────────
def test_freshness_check_pass_with_fresh_data(monitor, tmp_path):
    """``market_data_freshness`` must be ``pass`` when the latest
    ``market_snapshots`` row is < 60 s old."""
    # Insert a fresh snapshot (timestamp = now).
    _insert_snapshot(tmp_path / "data_quality.db", timestamp=time.time())

    report = monitor.run_all_checks()
    freshness = next(c for c in report.checks if c.name == "market_data_freshness")
    assert freshness.status == "pass"
    # Value is "<age>s old" with age < 60.
    assert freshness.value.endswith("s old")


def test_freshness_check_fail_with_stale_data(monitor, tmp_path):
    """``market_data_freshness`` must be ``fail`` when the latest
    ``market_snapshots`` row is > 60 s old."""
    # Insert a snapshot 5 minutes ago (well over the 60 s threshold).
    _insert_snapshot(
        tmp_path / "data_quality.db",
        timestamp=time.time() - 300,  # 5 min ago
    )

    report = monitor.run_all_checks()
    freshness = next(c for c in report.checks if c.name == "market_data_freshness")
    assert freshness.status == "fail"


def test_freshness_check_tracked_markets_count_threshold(monitor, tmp_path):
    """``tracked_markets_count`` must be ``pass`` when ≥ 10 distinct
    ``token_id`` values have a snapshot in the last 5 minutes."""
    # Insert 10 distinct-token snapshots (all fresh).
    db_path = tmp_path / "data_quality.db"
    now = time.time()
    for i in range(10):
        _insert_snapshot(
            db_path,
            token_id=f"token_{i:03d}",
            timestamp=now,
        )

    report = monitor.run_all_checks()
    tracked = next(c for c in report.checks if c.name == "tracked_markets_count")
    assert tracked.status == "pass"
    assert tracked.value == 10


# ── 3. completeness check (null_mid_prices) ────────────────────────────────
def test_completeness_check_pass_when_all_mids_present(monitor, tmp_path):
    """``null_mid_prices`` must be ``pass`` when every snapshot has a
    non-null, non-zero ``mid``."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", mid=0.45)
    _insert_snapshot(db_path, token_id="t2", mid=0.55)

    report = monitor.run_all_checks()
    null_mid = next(c for c in report.checks if c.name == "null_mid_prices")
    assert null_mid.status == "pass"
    assert null_mid.value == 0


def test_completeness_check_warning_with_null_mid(monitor, tmp_path):
    """``null_mid_prices`` must be ``warning`` when ≥ 1 snapshot has a
    NULL ``mid`` (inserted via an explicit ``None`` Python value, which
    sqlite3 maps to SQL NULL)."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", mid=0.45)  # valid
    _insert_snapshot(db_path, token_id="t2", mid=None)  # NULL mid

    report = monitor.run_all_checks()
    null_mid = next(c for c in report.checks if c.name == "null_mid_prices")
    assert null_mid.status == "warning"
    assert null_mid.value == 1


def test_completeness_check_warning_with_zero_mid(monitor, tmp_path):
    """``null_mid_prices`` must be ``warning`` when ≥ 1 snapshot has a
    zero ``mid`` (the SQL predicate is ``mid IS NULL OR mid = 0``)."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", mid=0.45)  # valid
    _insert_snapshot(db_path, token_id="t2", mid=0.0)    # zero mid

    report = monitor.run_all_checks()
    null_mid = next(c for c in report.checks if c.name == "null_mid_prices")
    assert null_mid.status == "warning"
    assert null_mid.value == 1


# ── 4. validity check (negative_prices + prices_over_1) ────────────────────
def test_validity_check_negative_prices_fail(monitor, tmp_path):
    """``negative_prices`` must be ``fail`` when ≥ 1 snapshot has a
    negative ``best_bid`` or ``best_ask``."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", best_bid=0.45, best_ask=0.55)  # valid
    _insert_snapshot(db_path, token_id="t2", best_bid=-0.1, best_ask=0.55)  # neg bid

    report = monitor.run_all_checks()
    neg = next(c for c in report.checks if c.name == "negative_prices")
    assert neg.status == "fail"
    assert neg.value == 1


def test_validity_check_negative_ask_fail(monitor, tmp_path):
    """``negative_prices`` must catch a negative ``best_ask`` too (the
    SQL predicate is ``best_bid < 0 OR best_ask < 0``)."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", best_bid=0.45, best_ask=-0.05)

    report = monitor.run_all_checks()
    neg = next(c for c in report.checks if c.name == "negative_prices")
    assert neg.status == "fail"
    assert neg.value == 1


def test_validity_check_prices_over_1_warning(monitor, tmp_path):
    """``prices_over_1`` must be ``warning`` when ≥ 1 snapshot has a
    ``best_bid`` or ``best_ask`` > 1.0 (out-of-probability range for
    a single-token probability market)."""
    db_path = tmp_path / "data_quality.db"
    _insert_snapshot(db_path, token_id="t1", best_bid=0.45, best_ask=0.55)  # valid
    _insert_snapshot(db_path, token_id="t2", best_bid=1.05, best_ask=1.10)   # over 1

    report = monitor.run_all_checks()
    over = next(c for c in report.checks if c.name == "prices_over_1")
    assert over.status == "warning"
    assert over.value == 1


# ── 5. overall status determination (healthy / degraded / critical) ────────
def test_overall_status_healthy_when_all_pass(tmp_path):
    """``overall_status`` must be ``healthy`` when every check passes.

    Setup: 10 distinct tokens with fresh snapshots, all mids present,
    all prices in [0, 1]. Every check passes:
      * freshness → pass (age < 60 s)
      * tracked_markets_count → pass (10 ≥ 10)
      * null_mid_prices → pass (0)
      * negative_prices → pass (0)
      * prices_over_1 → pass (0)
    """
    db_path = tmp_path / "healthy.db"
    _create_market_snapshots_table(db_path)
    now = time.time()
    for i in range(10):
        _insert_snapshot(
            db_path,
            token_id=f"t{i:03d}",
            timestamp=now,
            best_bid=0.40,
            best_ask=0.60,
            mid=0.50,
        )

    monitor = DataQualityMonitor(db_path=str(db_path))
    report = monitor.run_all_checks()

    # Sanity: every check is pass.
    statuses = {c.status for c in report.checks}
    assert statuses == {"pass"}, (
        f"expected every check to pass; got statuses={statuses}, "
        f"checks={[(c.name, c.status) for c in report.checks]}"
    )
    assert report.overall_status == "healthy"
    assert report.summary["failed"] == 0
    assert report.summary["warnings"] == 0
    assert report.summary["passed"] == len(report.checks)


def test_overall_status_degraded_with_warning_only(tmp_path):
    """``overall_status`` must be ``degraded`` when ≥ 1 check is
    ``warning`` and zero checks are ``fail``.

    Setup: 1 fresh snapshot (so freshness passes), only 1 distinct token
    (so tracked_markets_count warns — 1 < 10), valid mid, valid prices.
    The only non-pass check is the warning, so overall = degraded.
    """
    db_path = tmp_path / "degraded.db"
    _create_market_snapshots_table(db_path)
    _insert_snapshot(
        db_path,
        token_id="t1",
        timestamp=time.time(),
        best_bid=0.40,
        best_ask=0.60,
        mid=0.50,
    )

    monitor = DataQualityMonitor(db_path=str(db_path))
    report = monitor.run_all_checks()

    # The tracked-markets-count check is the sole warning.
    statuses_by_name = {c.name: c.status for c in report.checks}
    assert statuses_by_name["market_data_freshness"] == "pass"
    assert statuses_by_name["tracked_markets_count"] == "warning"
    assert statuses_by_name["null_mid_prices"] == "pass"
    assert statuses_by_name["negative_prices"] == "pass"
    assert statuses_by_name["prices_over_1"] == "pass"

    # Zero fails, ≥1 warning → degraded.
    assert report.summary["failed"] == 0
    assert report.summary["warnings"] >= 1
    assert report.overall_status == "degraded"


def test_overall_status_critical_with_fail(tmp_path):
    """``overall_status`` must be ``critical`` when ≥ 1 check is ``fail``,
    even if other checks are ``warning`` (fail outranks warning)."""
    db_path = tmp_path / "critical.db"
    _create_market_snapshots_table(db_path)
    # A stale snapshot (5 min ago) → freshness fail.
    # Only 1 distinct token → tracked_markets_count warning.
    # Negative best_bid → negative_prices fail.
    _insert_snapshot(
        db_path,
        token_id="t1",
        timestamp=time.time() - 300,
        best_bid=-0.10,
        best_ask=0.55,
        mid=0.50,
    )

    monitor = DataQualityMonitor(db_path=str(db_path))
    report = monitor.run_all_checks()

    statuses_by_name = {c.name: c.status for c in report.checks}
    assert statuses_by_name["market_data_freshness"] == "fail"
    assert statuses_by_name["negative_prices"] == "fail"
    assert statuses_by_name["tracked_markets_count"] == "warning"

    assert report.summary["failed"] >= 2
    assert report.summary["warnings"] >= 1
    assert report.overall_status == "critical"


# ── 6. API route via TestClient ─────────────────────────────────────────────
def _build_client_with_isolated_monitor(tmp_path, monkeypatch):
    """Build a TestClient against a minimal FastAPI app with ONLY the
    data-quality route registered, and a fresh ``data_quality_monitor``
    singleton pointing at ``tmp_path / api_data_quality.db``.

    The module-level singleton ``data_quality_monitor`` in
    ``core.data_quality`` is monkeypatched so the route handler (which
    imports the singleton via ``from core.data_quality import
    data_quality_monitor``) hits the isolated DB. Teardown auto-reverts
    the monkeypatch.

    Why register ONLY the data-quality route (instead of importing the
    full ``api/server.py`` app): the full server pulls in dozens of
    module-level singletons (data_store, paper_sim, risk_manager,
    book_poller, …) whose construction has side effects (file I/O,
    background tasks). A minimal app with just the data-quality route is
    sufficient to exercise the contract under test and runs in <100 ms.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db_path = tmp_path / "api_data_quality.db"
    _create_market_snapshots_table(db_path)
    fresh = DataQualityMonitor(db_path=str(db_path))
    monkeypatch.setattr("core.data_quality.data_quality_monitor", fresh)

    # The route handler imports ``data_quality_monitor`` from
    # ``core.data_quality`` at request time (``from core.data_quality
    # import data_quality_monitor`` is inside the handler body), so it
    # resolves the monkeypatched attribute on every request.
    app = FastAPI()

    @app.get("/api/data-quality", tags=["system"])
    async def get_data_quality():
        from core.data_quality import data_quality_monitor as _monitor

        report = _monitor.run_all_checks()
        return {
            "overall_status": report.overall_status,
            "summary": report.summary,
            "checks": [c.__dict__ for c in report.checks],
            "timestamp": report.timestamp,
        }

    return TestClient(app), fresh


def test_api_get_data_quality_empty_db_critical(tmp_path, monkeypatch):
    """``GET /api/data-quality`` on a fresh (empty) DB returns 200 +
    ``overall_status="critical"`` (freshness fail) + the documented
    envelope (summary / checks / timestamp)."""
    client, _ = _build_client_with_isolated_monitor(tmp_path, monkeypatch)

    response = client.get("/api/data-quality")
    assert response.status_code == 200, response.text
    body = response.json()

    # Envelope shape.
    assert set(body.keys()) == {
        "overall_status",
        "summary",
        "checks",
        "timestamp",
    }
    assert body["overall_status"] == "critical"  # freshness fail on empty DB

    # Summary shape.
    summary = body["summary"]
    assert summary["total_checks"] == len(body["checks"])
    assert summary["failed"] >= 1  # freshness fail
    assert summary["warnings"] >= 1  # tracked-markets warning
    assert summary["passed"] >= 0

    # Each check serialises to the dataclass dict shape.
    for check in body["checks"]:
        assert set(check.keys()) == {
            "name",
            "category",
            "status",
            "value",
            "threshold",
            "message",
            "timestamp",
        }
        assert check["status"] in {"pass", "warning", "fail"}

    # Timestamp is a recent epoch second.
    assert isinstance(body["timestamp"], float)
    assert body["timestamp"] > 0
    assert time.time() - body["timestamp"] < 5.0


def test_api_get_data_quality_healthy_after_inserts(tmp_path, monkeypatch):
    """``GET /api/data-quality`` returns ``overall_status="healthy"``
    after 10 distinct-token fresh snapshots are inserted (every check
    passes)."""
    client, monitor = _build_client_with_isolated_monitor(tmp_path, monkeypatch)
    db_path = Path(monitor._db_path)
    now = time.time()
    for i in range(10):
        _insert_snapshot(
            db_path,
            token_id=f"t{i:03d}",
            timestamp=now,
            best_bid=0.40,
            best_ask=0.60,
            mid=0.50,
        )

    response = client.get("/api/data-quality")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall_status"] == "healthy"
    assert body["summary"]["failed"] == 0
    assert body["summary"]["warnings"] == 0
    assert body["summary"]["passed"] == len(body["checks"])
