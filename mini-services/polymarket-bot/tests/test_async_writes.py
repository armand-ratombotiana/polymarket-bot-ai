"""
Unit tests for the W23-6 async write paths.

W23-6 — ``core/db_pool.py`` (already async) + ``core/async_repositories.py``
(new write methods + 3 new repositories) + ``core/write_through_cache.py``.

Covers:

  1. ``AsyncDecisionRepository.record_event`` — INSERTs a row into
     ``decision_events``; persisted row has ``decision_id ==
     correlation_id`` + ``data_json`` carries the ``model_version``.
  2. ``AsyncDecisionRepository.record_rejection`` — INSERTs a row
     into ``decision_rejections``; persisted row has the
     ``correlation_id`` in the ``decision_id`` column + the
     ``risk_data`` well-known keys hoisted to dedicated columns.
  3. ``AsyncObservabilityRepository.record_metric`` — INSERTs a
     metric sample; persisted row has the coerced-float ``value`` +
     JSON ``metadata_json``.
  4. ``AsyncObservabilityRepository.record_metrics_batch`` — INSERTs
     N rows in a single ``executemany``; rows that fail coercion are
     still persisted (default 0.0); empty ``category`` / ``name``
     entries are skipped.
  5. ``AsyncExecutionQualityRepository.record_execution`` — INSERTs a
     per-fill execution-quality row; persisted row maps
     ``intended_price`` → ``signal_price`` + ``fill_price`` →
     ``actual_fill``.
  6. ``AsyncExecutionQualityRepository.get_stats(hours=24.0)`` —
     windowed stats filter by ``timestamp > now - hours * 3600``;
     default ``hours=None`` returns full-history stats (W16-7
     backward-compat).
  7. ``AsyncClosedPositionsRepository.record_close`` — INSERTs a
     closed-position row; spec parameter names (``side`` / ``size`` /
     ``realized_pnl`` / ``exit_reason``) map to the sync schema's
     column names (``direction`` / ``shares`` / ``pnl`` /
     ``metadata_json``).
  8. ``AsyncClosedPositionsRepository.get_recent`` + ``get_stats`` —
     read back the persisted rows + aggregate P&L / win-rate /
     profit-factor.
  9. ``AsyncAlertRepository.record_alert`` — INSERTs an alert row;
     ``acknowledge`` + ``acknowledge_all`` UPDATE the
     ``acknowledged`` column.
 10. ``AsyncAlertRepository.get_recent(unacknowledged_only=True)`` —
     filters to ``acknowledged = 0``.
 11. ``AsyncFeatureStoreRepository.register_feature`` — upserts a
     feature definition.
 12. ``AsyncFeatureStoreRepository.record_values`` — INSERTs one row
     per numeric feature; non-numeric values are skipped.
 13. ``AsyncFeatureStoreRepository.record_importance`` — INSERTs one
     row per feature, sorted by descending importance with a rank.
 14. ``AsyncFeatureStoreRepository.get_top_features`` — returns the
     top-N most important features for a model version.
 15. ``WriteThroughCache`` — write + read round-trip; DB writer is
     called; DB writer failure doesn't break the cache;
     ``read_or_fetch`` populates cache on miss; ``invalidate`` /
     ``clear`` / ``size``.

Each test creates its own temp DB under ``tmp_path`` so tests are
hermetic to each other. A module-level autouse fixture clears the
module-level singleton ``db_pool``'s connection cache BEFORE each
test (mirrors ``tests/test_async_db.py`` — the singleton is shared
with ``core.async_repositories`` which imports it).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from core.async_repositories import (
    AsyncAlertRepository,
    AsyncClosedPositionsRepository,
    AsyncDecisionRepository,
    AsyncExecutionQualityRepository,
    AsyncFeatureStoreRepository,
    AsyncObservabilityRepository,
)
from core.db_pool import db_pool as _singleton_db_pool
from core.write_through_cache import WriteThroughCache, write_through_cache

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the W16-7 / test_async_db.py pattern.
pytestmark = pytest.mark.asyncio


# ── Fixture: reset the module-level singleton before each test ──────────────
@pytest.fixture(autouse=True)
def _reset_singleton_db_pool():
    """Drop the singleton ``db_pool``'s connection cache BEFORE each test.

    Mirrors the autouse fixture in ``tests/test_async_db.py`` — the
    singleton is shared with ``core.async_repositories`` (which imports
    it as ``from core.db_pool import db_pool``), so without this reset
    a prior test's connection to a now-deleted tmp_path would leak into
    the next test and raise ``aiosqlite.OperationalError`` on the next
    ``get_connection`` call.
    """
    try:
        asyncio.run(_singleton_db_pool.close_all())
    except RuntimeError:
        # ``asyncio.run`` can't be called from inside a running loop —
        # fall through and let the per-test AsyncDBPool usage open fresh
        # connections (the singleton's stale entries are best-effort
        # closed by ``close_all`` above when called from a throwaway
        # loop; if that fails the test's own loop will close them).
        pass
    # Also clear the write-through cache singleton so cache state from
    # a prior test doesn't leak into the next.
    write_through_cache.clear()
    yield


# ── Helper: read back persisted rows via sync sqlite3 ───────────────────────
# Each test writes through the async pool + verifies the write landed in the
# SQLite file by reading it back via the sync ``sqlite3`` module (NOT via the
# async pool) so the assertion is independent of the write path.


def _sync_fetch_all(db_path: str, query: str, params: tuple = ()) -> list[dict]:
    """Read rows via sync sqlite3 — independent of the async write path."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def _sync_fetch_one(db_path: str, query: str, params: tuple = ()) -> dict | None:
    rows = _sync_fetch_all(db_path, query, params)
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# 1-2. AsyncDecisionRepository write paths
# ─────────────────────────────────────────────────────────────────────────────


async def test_decision_repo_record_event_persists_row(tmp_path):
    """``record_event`` must INSERT a row into ``decision_events`` with
    ``decision_id == correlation_id`` + ``model_version`` stashed in the
    ``data_json`` blob (mirrors the sync recorder's V14 auto-stamp)."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_event(
        correlation_id="dec-abc",
        token_id="TOK_A",
        stage="PREDICTION",
        data={"p_yes": 0.62, "mid_price": 0.55},
        model_version="v1.155.0",
    )
    rows = _sync_fetch_all(db, "SELECT * FROM decision_events")
    assert len(rows) == 1
    r = rows[0]
    assert r["decision_id"] == "dec-abc"
    assert r["stage"] == "PREDICTION"
    assert r["token_id"] == "TOK_A"
    payload = json.loads(r["data_json"])
    assert payload["model_version"] == "v1.155.0"
    assert payload["p_yes"] == 0.62
    assert payload["mid_price"] == 0.55


async def test_decision_repo_record_event_skips_empty_correlation_id(tmp_path):
    """An empty ``correlation_id`` must be skipped silently (mirrors
    the sync recorder's guard against legacy / manual orders that
    didn't participate in the unified ledger)."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_event(
        correlation_id="",
        token_id="TOK_X",
        stage="PREDICTION",
        data={"p_yes": 0.5},
    )
    rows = _sync_fetch_all(db, "SELECT * FROM decision_events")
    assert rows == [], "Empty correlation_id must NOT be persisted"


async def test_decision_repo_record_event_promotes_strategy_and_pnl(tmp_path):
    """``strategy`` and ``pnl`` keys in the ``data`` dict are hoisted
    to dedicated top-level columns (so the indexed ``strategy`` /
    ``pnl`` lookups work without a JSON scan)."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_event(
        correlation_id="dec-strat",
        token_id="TOK_S",
        stage="FILL",
        data={"strategy": "ml_sig_v1", "pnl": 1.25, "fill_price": 0.55},
    )
    r = _sync_fetch_one(db, "SELECT * FROM decision_events")
    assert r["strategy"] == "ml_sig_v1"
    assert r["pnl"] == 1.25
    # The hoisted keys must NOT also be in the JSON payload (otherwise
    # a downstream reader that re-merges would see duplicates).
    payload = json.loads(r["data_json"])
    assert "strategy" not in payload
    assert "pnl" not in payload
    assert payload["fill_price"] == 0.55


async def test_decision_repo_record_rejection_persists_row(tmp_path):
    """``record_rejection`` must INSERT a row into ``decision_rejections``
    with ``decision_id == correlation_id`` + the well-known ``risk_data``
    keys hoisted to dedicated columns (``predicted_edge`` /
    ``confidence`` / ``market_mid`` / ``strategy``)."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_rejection(
        correlation_id="dec-rej",
        token_id="TOK_R",
        stage="RISK_REJECTED",
        reason="low_confidence",
        risk_data={
            "strategy": "mm_avellaneda",
            "predicted_edge": 0.012,
            "confidence": 0.31,
            "market_mid": 0.48,
        },
    )
    r = _sync_fetch_one(db, "SELECT * FROM decision_rejections")
    assert r is not None
    assert r["decision_id"] == "dec-rej"
    assert r["token_id"] == "TOK_R"
    assert r["reason"] == "low_confidence"
    assert r["strategy"] == "mm_avellaneda"
    assert abs(r["predicted_edge"] - 0.012) < 1e-9
    assert abs(r["confidence"] - 0.31) < 1e-9
    assert abs(r["market_mid"] - 0.48) < 1e-9


async def test_decision_repo_record_rejection_handles_missing_risk_data(tmp_path):
    """``risk_data`` is optional — a missing dict leaves the
    attribution-dimension columns NULL (not 0.0) so downstream
    queries can distinguish "no value" from "value is zero"."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_rejection(
        correlation_id="dec-norisk",
        token_id="TOK_N",
        stage="RISK_REJECTED",
        reason="manual_halt",
        risk_data=None,
    )
    r = _sync_fetch_one(db, "SELECT * FROM decision_rejections")
    assert r is not None
    assert r["reason"] == "manual_halt"
    assert r["strategy"] is None
    assert r["predicted_edge"] is None
    assert r["confidence"] is None
    assert r["market_mid"] is None


async def test_decision_repo_record_event_then_read_via_async_pool(tmp_path):
    """A row written via ``record_event`` must be observable via the
    existing read methods (``get_recent`` / ``get_by_token`` / ``count_by_stage``)."""
    db = str(tmp_path / "decision.db")
    repo = AsyncDecisionRepository(db)
    await repo.record_event(
        correlation_id="dec-read",
        token_id="TOK_READ",
        stage="PREDICTION",
        data={"p_yes": 0.7},
    )
    await repo.record_event(
        correlation_id="dec-read",
        token_id="TOK_READ",
        stage="FILL",
        data={"pnl": 0.5},
    )
    recent = await repo.get_recent(limit=10)
    assert len(recent) == 2
    by_token = await repo.get_by_token("TOK_READ", limit=10)
    assert len(by_token) == 2
    assert await repo.count_by_stage("PREDICTION") == 1
    assert await repo.count_by_stage("FILL") == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3-4. AsyncObservabilityRepository write paths
# ─────────────────────────────────────────────────────────────────────────────


async def test_observability_repo_record_metric_persists_row(tmp_path):
    """``record_metric`` must INSERT a row with the coerced-float value
    + JSON ``metadata_json``."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    await repo.record_metric(
        category="system",
        name="cpu_percent",
        value=42.0,
        metadata={"host": "bot-1", "region": "us-east-1"},
    )
    r = _sync_fetch_one(db, "SELECT * FROM metrics")
    assert r is not None
    assert r["category"] == "system"
    assert r["name"] == "cpu_percent"
    assert r["value"] == 42.0
    meta = json.loads(r["metadata_json"])
    assert meta["host"] == "bot-1"
    assert meta["region"] == "us-east-1"


async def test_observability_repo_record_metric_coerces_bool_to_float(tmp_path):
    """``bool`` value → ``float`` (True → 1.0, False → 0.0)."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    await repo.record_metric("system", "alive", True)
    await repo.record_metric("system", "halted", False)
    rows = _sync_fetch_all(db, "SELECT * FROM metrics ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["value"] == 1.0
    assert rows[1]["value"] == 0.0


async def test_observability_repo_record_metric_skips_empty_keys(tmp_path):
    """An empty ``category`` or ``name`` must be skipped silently."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    await repo.record_metric("", "name", 1.0)
    await repo.record_metric("cat", "", 2.0)
    rows = _sync_fetch_all(db, "SELECT * FROM metrics")
    assert rows == []


async def test_observability_repo_record_metrics_batch_inserts_rows(tmp_path):
    """``record_metrics_batch`` must INSERT every valid entry in a single
    ``executemany`` transaction. Entries with empty ``category`` / ``name``
    are skipped (count reflects valid rows only)."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    n = await repo.record_metrics_batch(
        [
            {"category": "system", "name": "cpu", "value": 30.0},
            {"category": "system", "name": "mem", "value": 60.0, "metadata": {"pid": 1}},
            {"category": "", "name": "skip_me", "value": 99.0},  # skipped
            {"category": "ml", "name": "", "value": 99.0},  # skipped
            {"category": "ml", "name": "inference", "value": "not-a-number"},  # 0.0
        ]
    )
    assert n == 3, "Only 3 valid entries should be inserted"
    rows = _sync_fetch_all(db, "SELECT * FROM metrics ORDER BY id")
    assert len(rows) == 3
    assert [r["name"] for r in rows] == ["cpu", "mem", "inference"]
    # The non-coercible value defaults to 0.0.
    assert rows[-1]["value"] == 0.0


async def test_observability_repo_record_metrics_batch_empty_list(tmp_path):
    """An empty list must return 0 and INSERT nothing."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    n = await repo.record_metrics_batch([])
    assert n == 0
    rows = _sync_fetch_all(db, "SELECT * FROM metrics")
    assert rows == []


async def test_observability_repo_record_metric_then_read(tmp_path):
    """A metric written via ``record_metric`` must be observable via the
    existing read methods (``get_latest_metrics`` / ``get_metric_history``)."""
    db = str(tmp_path / "obs.db")
    repo = AsyncObservabilityRepository(db)
    await repo.record_metric("system", "cpu_percent", 10.0)
    await repo.record_metric("system", "cpu_percent", 20.0)
    await repo.record_metric("system", "cpu_percent", 30.0)
    latest = await repo.get_latest_metrics()
    assert len(latest) == 1
    assert latest[0]["value"] == 30.0  # latest sample wins
    history = await repo.get_metric_history("cpu_percent", limit=2)
    assert len(history) == 2
    assert history[0]["value"] == 30.0
    assert history[1]["value"] == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# 5-6. AsyncExecutionQualityRepository write paths
# ─────────────────────────────────────────────────────────────────────────────


async def test_execution_quality_repo_record_execution_persists_row(tmp_path):
    """``record_execution`` must INSERT a row mapping ``intended_price`` →
    ``signal_price`` and ``fill_price`` → ``actual_fill``."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    await repo.record_execution(
        token_id="TOK_E",
        side="BUY",
        intended_price=0.50,
        fill_price=0.52,
        slippage_bps=40.0,
        latency_ms=12.5,
        order_id="o-1",
    )
    r = _sync_fetch_one(db, "SELECT * FROM execution_quality")
    assert r is not None
    assert r["token_id"] == "TOK_E"
    assert r["side"] == "BUY"
    assert r["order_id"] == "o-1"
    assert r["signal_price"] == 0.50  # intended_price
    assert r["actual_fill"] == 0.52  # fill_price
    assert r["slippage_bps"] == 40.0
    assert r["latency_ms"] == 12.5


async def test_execution_quality_repo_record_execution_null_slippage(tmp_path):
    """``slippage_bps=None`` must persist as NULL (not 0.0) so the
    ``get_stats`` AVG(slippage_bps) over non-NULL rows excludes the
    NULL row — mirrors the sync recorder's semantics for fills where
    slippage couldn't be computed."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    await repo.record_execution(
        token_id="TOK_N",
        side="SELL",
        intended_price=0.30,
        fill_price=0.30,
        slippage_bps=None,
        latency_ms=8.0,
    )
    r = _sync_fetch_one(db, "SELECT * FROM execution_quality")
    assert r is not None
    assert r["slippage_bps"] is None


async def test_execution_quality_repo_record_execution_default_order_id(tmp_path):
    """``order_id=None`` must persist as an empty string (the sync
    schema marks ``order_id`` as NOT NULL — the async path passes an
    empty string to satisfy the constraint)."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    await repo.record_execution(
        token_id="TOK_D",
        side="BUY",
        intended_price=0.40,
        fill_price=0.41,
        slippage_bps=25.0,
        latency_ms=10.0,
        order_id=None,
    )
    r = _sync_fetch_one(db, "SELECT * FROM execution_quality")
    assert r is not None
    assert r["order_id"] == ""


async def test_execution_quality_repo_get_stats_default_full_history(tmp_path):
    """``get_stats()`` (no ``hours``) must return full-history stats —
    preserves the W16-7 backward-compat contract."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    # Insert 3 rows directly via the async repo (timestamps = now-ish).
    await repo.record_execution("T1", "BUY", 0.5, 0.51, 20.0, 10.0)
    await repo.record_execution("T2", "SELL", 0.5, 0.49, -20.0, 12.0)
    await repo.record_execution("T3", "BUY", 0.5, 0.5, None, 8.0)  # NULL slippage
    stats = await repo.get_stats()
    # AVG(20.0, -20.0) over the 2 non-NULL rows = 0.0
    assert stats["avg_slippage_bps"] == 0.0
    assert stats["total_fills"] == 3


async def test_execution_quality_repo_get_stats_windowed(tmp_path):
    """``get_stats(hours=1.0)`` must exclude rows older than 1 hour.
    We seed a row with a stale timestamp via sync sqlite3, then verify
    the windowed stats exclude it but the full-history stats include it."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    # Seed a stale row (timestamp = 1 hour + 5 min ago) via sync sqlite3.
    stale_ts = time.time() - 3900.0  # 65 min ago
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO execution_quality "
            "(timestamp, order_id, token_id, side, signal_price, actual_fill, "
            " slippage_bps, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (stale_ts, "o-stale", "T_STALE", "BUY", 0.5, 0.51, 100.0, 10.0),
        )
        conn.commit()
    # Insert a fresh row via the async repo (timestamp = now).
    await repo.record_execution("T_FRESH", "BUY", 0.5, 0.51, 50.0, 10.0)

    # Windowed: only the fresh row counts.
    windowed = await repo.get_stats(hours=1.0)
    assert windowed["total_fills"] == 1
    assert windowed["avg_slippage_bps"] == 50.0

    # Full history: both rows.
    full = await repo.get_stats()
    assert full["total_fills"] == 2
    # AVG(100.0, 50.0) = 75.0
    assert abs(full["avg_slippage_bps"] - 75.0) < 1e-9


async def test_execution_quality_repo_get_stats_empty_table(tmp_path):
    """``get_stats`` against an empty table returns zeros (not NULL /
    not an exception) — preserves the W16-7 contract."""
    db = str(tmp_path / "eq.db")
    repo = AsyncExecutionQualityRepository(db)
    stats = await repo.get_stats()
    assert stats == {"avg_slippage_bps": 0.0, "total_fills": 0}
    # Windowed stats against an empty table also return zeros.
    windowed = await repo.get_stats(hours=24.0)
    assert windowed == {"avg_slippage_bps": 0.0, "total_fills": 0}


# ─────────────────────────────────────────────────────────────────────────────
# 7-8. AsyncClosedPositionsRepository
# ─────────────────────────────────────────────────────────────────────────────


async def test_closed_positions_repo_record_close_persists_row(tmp_path):
    """``record_close`` must INSERT a row mapping the spec's parameter
    names to the sync schema's column names (``side`` → ``direction``,
    ``size`` → ``shares``, ``realized_pnl`` → ``pnl``). ``exit_reason``
    must land in ``metadata_json`` (no dedicated column)."""
    db = str(tmp_path / "cp.db")
    repo = AsyncClosedPositionsRepository(db)
    pid = await repo.record_close(
        token_id="TOK_CP",
        side="BUY",
        entry_price=0.40,
        exit_price=0.55,
        size=100.0,
        realized_pnl=15.0,
        exit_reason="take_profit",
        strategy="ml_sig_v1",
        decision_id="dec-cp",
        confidence=0.72,
    )
    assert pid.startswith("pos-")
    r = _sync_fetch_one(db, "SELECT * FROM closed_positions")
    assert r is not None
    assert r["position_id"] == pid
    assert r["token_id"] == "TOK_CP"
    assert r["direction"] == "BUY"  # side → direction
    assert r["shares"] == 100.0  # size → shares
    assert r["pnl"] == 15.0  # realized_pnl → pnl
    assert r["entry_price"] == 0.40
    assert r["exit_price"] == 0.55
    assert r["strategy"] == "ml_sig_v1"
    assert r["decision_id"] == "dec-cp"
    assert abs(r["confidence"] - 0.72) < 1e-9
    payload = json.loads(r["metadata_json"])
    assert payload["exit_reason"] == "take_profit"


async def test_closed_positions_repo_record_close_idempotent_position_id(tmp_path):
    """An explicit ``position_id`` is the UNIQUE key — a second
    ``record_close`` with the same ``position_id`` is IGNORED (the
    first row wins). Mirrors the sync recorder's ``INSERT OR IGNORE``
    idempotency contract."""
    db = str(tmp_path / "cp.db")
    repo = AsyncClosedPositionsRepository(db)
    pid = "pos-fixed-001"
    await repo.record_close(
        token_id="TOK_1", side="BUY", entry_price=0.4, exit_price=0.5,
        size=10.0, realized_pnl=1.0, exit_reason="tp",
        position_id=pid,
    )
    await repo.record_close(
        token_id="TOK_2", side="SELL", entry_price=0.6, exit_price=0.5,
        size=20.0, realized_pnl=-2.0, exit_reason="sl",
        position_id=pid,  # SAME id → ignored
    )
    rows = _sync_fetch_all(db, "SELECT * FROM closed_positions")
    assert len(rows) == 1
    assert rows[0]["token_id"] == "TOK_1"  # first write wins


async def test_closed_positions_repo_get_recent(tmp_path):
    """``get_recent`` must return the most-recent N rows (newest first)."""
    db = str(tmp_path / "cp.db")
    repo = AsyncClosedPositionsRepository(db)
    base_ts = time.time()
    # Insert 3 rows with explicit timestamps (newest last).
    for i, (side, pnl) in enumerate([("BUY", 1.0), ("SELL", -0.5), ("BUY", 2.0)]):
        await repo.record_close(
            token_id=f"TOK_{i}",
            side=side,
            entry_price=0.4,
            exit_price=0.5,
            size=10.0,
            realized_pnl=pnl,
            exit_reason="tp",
            timestamp=base_ts + i,
        )
    rows = await repo.get_recent(limit=2)
    assert len(rows) == 2
    # Newest first → timestamps must be non-increasing.
    assert rows[0]["timestamp"] >= rows[1]["timestamp"]
    assert rows[0]["timestamp"] == base_ts + 2


async def test_closed_positions_repo_get_stats(tmp_path):
    """``get_stats`` must return aggregate P&L stats: count, total_pnl,
    win_rate, gross_profit, gross_loss, profit_factor."""
    db = str(tmp_path / "cp.db")
    repo = AsyncClosedPositionsRepository(db)
    # 2 wins + 1 loss + 1 breakeven
    await repo.record_close("T1", "BUY", 0.4, 0.5, 10.0, 1.0, "tp")
    await repo.record_close("T2", "BUY", 0.4, 0.6, 10.0, 2.0, "tp")
    await repo.record_close("T3", "BUY", 0.5, 0.4, 10.0, -1.5, "sl")
    await repo.record_close("T4", "BUY", 0.4, 0.4, 10.0, 0.0, "manual")
    stats = await repo.get_stats()
    assert stats["count"] == 4
    assert abs(stats["total_pnl"] - 1.5) < 1e-9  # 1 + 2 - 1.5 + 0
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["breakeven"] == 1
    assert abs(stats["gross_profit"] - 3.0) < 1e-9
    assert abs(stats["gross_loss"] - 1.5) < 1e-9
    assert abs(stats["profit_factor"] - 2.0) < 1e-9
    assert abs(stats["win_rate"] - 0.5) < 1e-9


async def test_closed_positions_repo_get_stats_empty_table(tmp_path):
    """``get_stats`` against an empty table returns zeros + None
    profit_factor (no losses → profit_factor is None)."""
    db = str(tmp_path / "cp.db")
    repo = AsyncClosedPositionsRepository(db)
    stats = await repo.get_stats()
    assert stats["count"] == 0
    assert stats["total_pnl"] == 0.0
    assert stats["profit_factor"] is None
    assert stats["win_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 9-10. AsyncAlertRepository
# ─────────────────────────────────────────────────────────────────────────────


async def test_alert_repo_record_alert_persists_row(tmp_path):
    """``record_alert`` must INSERT a row with the supplied fields + the
    default ``acknowledged = 0``."""
    db = str(tmp_path / "alerts.db")
    repo = AsyncAlertRepository(db)
    aid = await repo.record_alert(
        alert_id="alert-1",
        category="risk",
        name="max_drawdown_exceeded",
        severity="critical",
        message="Daily loss limit exceeded: -$2.50",
        value=-2.50,
        threshold=-2.0,
    )
    assert aid == "alert-1"
    r = _sync_fetch_one(db, "SELECT * FROM alerts")
    assert r is not None
    assert r["alert_id"] == "alert-1"
    assert r["category"] == "risk"
    assert r["name"] == "max_drawdown_exceeded"
    assert r["severity"] == "critical"
    assert r["message"] == "Daily loss limit exceeded: -$2.50"
    assert r["value"] == -2.50
    assert r["threshold"] == -2.0
    assert r["acknowledged"] == 0


async def test_alert_repo_record_alert_replaces_existing(tmp_path):
    """``INSERT OR REPLACE`` — re-firing the same ``alert_id`` updates
    the row in place (the acknowledged flag is reset to 0)."""
    db = str(tmp_path / "alerts.db")
    repo = AsyncAlertRepository(db)
    await repo.record_alert(
        "a-1", "risk", "rule_a", "warning", "first", value=1.0,
    )
    # Acknowledge the first write.
    await repo.acknowledge("a-1")
    # Re-fire — must replace the row.
    await repo.record_alert(
        "a-1", "risk", "rule_a", "critical", "second", value=2.0,
    )
    rows = _sync_fetch_all(db, "SELECT * FROM alerts")
    assert len(rows) == 1
    r = rows[0]
    assert r["message"] == "second"
    assert r["value"] == 2.0
    assert r["severity"] == "critical"
    assert r["acknowledged"] == 0  # reset on INSERT OR REPLACE


async def test_alert_repo_acknowledge_updates_row(tmp_path):
    """``acknowledge`` must set ``acknowledged = 1`` on the named row
    and return True; acknowledging a non-existent id returns False."""
    db = str(tmp_path / "alerts.db")
    repo = AsyncAlertRepository(db)
    await repo.record_alert("a-1", "risk", "rule", "warning", "msg")
    assert await repo.acknowledge("a-1") is True
    r = _sync_fetch_one(db, "SELECT acknowledged FROM alerts WHERE alert_id = ?", ("a-1",))
    assert r["acknowledged"] == 1
    # Acknowledging a non-existent id returns False.
    assert await repo.acknowledge("does-not-exist") is False


async def test_alert_repo_acknowledge_all_updates_rows(tmp_path):
    """``acknowledge_all`` must mark every unacknowledged row
    acknowledged and return the count of rows updated."""
    db = str(tmp_path / "alerts.db")
    repo = AsyncAlertRepository(db)
    await repo.record_alert("a-1", "risk", "r1", "warning", "m1")
    await repo.record_alert("a-2", "ml", "r2", "critical", "m2")
    await repo.record_alert("a-3", "system", "r3", "info", "m3")
    # Acknowledge one first, then acknowledge_all should update the other 2.
    await repo.acknowledge("a-1")
    n = await repo.acknowledge_all()
    assert n == 2
    rows = _sync_fetch_all(db, "SELECT acknowledged FROM alerts ORDER BY alert_id")
    assert all(r["acknowledged"] == 1 for r in rows)
    # Second acknowledge_all is a no-op (returns 0).
    assert await repo.acknowledge_all() == 0


async def test_alert_repo_get_recent_filters_unacknowledged(tmp_path):
    """``get_recent(unacknowledged_only=True)`` must filter to
    ``acknowledged = 0`` rows."""
    db = str(tmp_path / "alerts.db")
    repo = AsyncAlertRepository(db)
    await repo.record_alert("a-1", "risk", "r1", "warning", "m1")
    await repo.record_alert("a-2", "ml", "r2", "critical", "m2")
    await repo.record_alert("a-3", "system", "r3", "info", "m3")
    await repo.acknowledge("a-2")
    all_alerts = await repo.get_recent(limit=10)
    assert len(all_alerts) == 3
    unacked = await repo.get_recent(limit=10, unacknowledged_only=True)
    assert len(unacked) == 2
    assert {r["alert_id"] for r in unacked} == {"a-1", "a-3"}


# ─────────────────────────────────────────────────────────────────────────────
# 11-14. AsyncFeatureStoreRepository
# ─────────────────────────────────────────────────────────────────────────────


async def test_feature_store_repo_register_feature_upserts(tmp_path):
    """``register_feature`` must INSERT OR REPLACE the feature definition."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    await repo.register_feature("mid_price", "numeric", "Market mid price")
    r = _sync_fetch_one(db, "SELECT * FROM feature_definitions WHERE name = ?", ("mid_price",))
    assert r is not None
    assert r["type"] == "numeric"
    assert r["description"] == "Market mid price"
    # Upsert — re-register with a new description.
    await repo.register_feature("mid_price", "numeric", "Updated description")
    rows = _sync_fetch_all(db, "SELECT * FROM feature_definitions WHERE name = ?", ("mid_price",))
    assert len(rows) == 1
    assert rows[0]["description"] == "Updated description"


async def test_feature_store_repo_record_values_inserts_numeric_only(tmp_path):
    """``record_values`` must INSERT one row per numeric feature value;
    non-numeric values are silently skipped."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    n = await repo.record_values(
        token_id="TOK_FV",
        features={
            "mid_price": 0.55,
            "spread": 0.02,
            "slug": "some-market",  # non-numeric → skipped
            "is_active": True,  # bool → 1.0
            "confidence": 0.7,
        },
        prediction_id="pred-1",
    )
    assert n == 4, "Only the 4 numeric features should be inserted"
    rows = _sync_fetch_all(db, "SELECT * FROM feature_values ORDER BY feature_name")
    assert {r["feature_name"] for r in rows} == {
        "mid_price", "spread", "is_active", "confidence"
    }
    assert all(r["token_id"] == "TOK_FV" for r in rows)
    assert all(r["prediction_id"] == "pred-1" for r in rows)
    # Bool → 1.0 (not True).
    active_row = next(r for r in rows if r["feature_name"] == "is_active")
    assert active_row["value"] == 1.0


async def test_feature_store_repo_record_values_empty_dict(tmp_path):
    """An empty features dict must return 0 and INSERT nothing."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    n = await repo.record_values("TOK", {})
    assert n == 0
    rows = _sync_fetch_all(db, "SELECT * FROM feature_values")
    assert rows == []


async def test_feature_store_repo_record_importance_ranks_descending(tmp_path):
    """``record_importance`` must sort the dict by descending importance
    and assign ranks 1..N (1 = most important)."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    n = await repo.record_importance(
        model_version="v1.155.0",
        importance_dict={
            "mid_price": 0.18,
            "spread_norm": 0.12,
            "volume": 0.05,
            "momentum": 0.30,  # most important
        },
    )
    assert n == 4
    rows = _sync_fetch_all(
        db,
        "SELECT * FROM feature_importance WHERE model_version = ? ORDER BY rank",
        ("v1.155.0",),
    )
    assert len(rows) == 4
    # Sorted by descending importance → momentum first.
    assert rows[0]["feature_name"] == "momentum"
    assert rows[0]["rank"] == 1
    assert rows[0]["importance"] == 0.30
    assert rows[1]["feature_name"] == "mid_price"
    assert rows[1]["rank"] == 2
    assert rows[3]["feature_name"] == "volume"
    assert rows[3]["rank"] == 4


async def test_feature_store_repo_get_top_features(tmp_path):
    """``get_top_features`` must return the top-N most important features
    for a model version, ordered by rank ASC (most important first)."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    await repo.record_importance(
        "v1.0",
        {"a": 0.4, "b": 0.3, "c": 0.2, "d": 0.1},
    )
    top = await repo.get_top_features("v1.0", top_n=2)
    assert len(top) == 2
    assert top[0]["feature_name"] == "a"
    assert top[0]["rank"] == 1
    assert top[1]["feature_name"] == "b"
    assert top[1]["rank"] == 2


async def test_feature_store_repo_get_top_features_unknown_version(tmp_path):
    """``get_top_features`` against an unknown model version returns an
    empty list (no exception, no None)."""
    db = str(tmp_path / "fs.db")
    repo = AsyncFeatureStoreRepository(db)
    top = await repo.get_top_features("nonexistent-version", top_n=10)
    assert top == []


# ─────────────────────────────────────────────────────────────────────────────
# 15. WriteThroughCache
# ─────────────────────────────────────────────────────────────────────────────


async def test_write_through_cache_write_and_read_round_trip():
    """``write`` + ``read`` must round-trip the value."""
    cache = WriteThroughCache()
    await cache.write("k1", {"value": 42})
    assert cache.read("k1") == {"value": 42}


async def test_write_through_cache_read_missing_key_returns_none():
    """``read`` on a missing key returns None."""
    cache = WriteThroughCache()
    assert cache.read("does-not-exist") is None


async def test_write_through_cache_invokes_db_writer():
    """``write(db_writer=...)`` must invoke the DB writer with the value."""
    cache = WriteThroughCache()
    called_with = []

    async def _writer(value):
        called_with.append(value)

    await cache.write("k1", "v1", db_writer=_writer)
    assert called_with == ["v1"]
    # Cache must also be populated.
    assert cache.read("k1") == "v1"


async def test_write_through_cache_db_writer_failure_does_not_break_cache():
    """If the DB writer raises, the cache must still be updated (so the
    caller's next read is consistent with what they just wrote). The
    exception must be swallowed (logged at ERROR) so the caller's write
    path doesn't crash."""
    cache = WriteThroughCache()

    async def _boom(value):
        raise RuntimeError("DB write failed")

    # Must NOT raise.
    await cache.write("k1", "v1", db_writer=_boom)
    # Cache must still reflect the value.
    assert cache.read("k1") == "v1"


async def test_write_through_cache_read_or_fetch_hit():
    """``read_or_fetch`` on a cache hit returns the cached value
    without invoking the fetcher."""
    cache = WriteThroughCache()
    await cache.write("k1", "cached-value")
    fetcher_calls = []

    async def _fetcher(key):
        fetcher_calls.append(key)
        return "fetched-value"

    result = await cache.read_or_fetch("k1", db_fetcher=_fetcher)
    assert result == "cached-value"
    assert fetcher_calls == [], "Fetcher must NOT be called on a cache hit"


async def test_write_through_cache_read_or_fetch_miss_populates_cache():
    """``read_or_fetch`` on a cache miss fetches from DB + populates the
    cache (so the next read is a cache hit)."""
    cache = WriteThroughCache()

    async def _fetcher(key):
        return f"fetched:{key}"

    result = await cache.read_or_fetch("k2", db_fetcher=_fetcher)
    assert result == "fetched:k2"
    # Second call must be a cache hit (fetcher NOT invoked).
    fetcher_calls = []

    async def _fetcher_count(key):
        fetcher_calls.append(key)
        return "should-not-be-called"

    result2 = await cache.read_or_fetch("k2", db_fetcher=_fetcher_count)
    assert result2 == "fetched:k2"
    assert fetcher_calls == []


async def test_write_through_cache_read_or_fetch_no_fetcher_returns_none():
    """``read_or_fetch`` on a cache miss with no ``db_fetcher`` returns
    None (no exception)."""
    cache = WriteThroughCache()
    result = await cache.read_or_fetch("missing-key")
    assert result is None


async def test_write_through_cache_read_or_fetch_fetcher_returns_none():
    """If the fetcher returns None, the cache must NOT be populated
    (so a subsequent ``read_or_fetch`` retries the fetch — None is
    treated as "no result", not as a cached value)."""
    cache = WriteThroughCache()
    fetcher_calls = []

    async def _fetcher(key):
        fetcher_calls.append(key)
        return None

    result = await cache.read_or_fetch("k3", db_fetcher=_fetcher)
    assert result is None
    assert len(fetcher_calls) == 1
    # Second call must retry the fetcher (cache not populated).
    result2 = await cache.read_or_fetch("k3", db_fetcher=_fetcher)
    assert result2 is None
    assert len(fetcher_calls) == 2


async def test_write_through_cache_invalidate_removes_key():
    """``invalidate`` removes the key from the cache."""
    cache = WriteThroughCache()
    await cache.write("k1", "v1")
    cache.invalidate("k1")
    assert cache.read("k1") is None


async def test_write_through_cache_invalidate_missing_key_is_noop():
    """``invalidate`` on a missing key is a no-op (no exception)."""
    cache = WriteThroughCache()
    cache.invalidate("never-existed")  # must NOT raise
    assert cache.size() == 0


async def test_write_through_cache_clear_drops_everything():
    """``clear`` drops every cached entry."""
    cache = WriteThroughCache()
    await cache.write("k1", "v1")
    await cache.write("k2", "v2")
    assert cache.size() == 2
    cache.clear()
    assert cache.size() == 0
    assert cache.read("k1") is None


async def test_write_through_cache_size_returns_count():
    """``size`` returns the number of cached entries."""
    cache = WriteThroughCache()
    assert cache.size() == 0
    await cache.write("k1", "v1")
    assert cache.size() == 1
    await cache.write("k2", "v2")
    assert cache.size() == 2
    cache.invalidate("k1")
    assert cache.size() == 1


async def test_write_through_cache_singleton_is_writable():
    """The module-level ``write_through_cache`` singleton is a working
    instance (writes + reads round-trip). The autouse fixture clears
    it between tests, so this test exercises a fresh empty cache."""
    await write_through_cache.write("singleton-key", "singleton-value")
    assert write_through_cache.read("singleton-key") == "singleton-value"
    assert write_through_cache.size() == 1


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: write through cache → async repo → SQLite persistence
# ─────────────────────────────────────────────────────────────────────────────


async def test_write_through_cache_to_async_repo_persists_to_sqlite(tmp_path):
    """End-to-end: a cache write that delegates to an async repo's
    write method must persist the value to SQLite. Verifies the
    write-through contract — a cache write should be observable via
    a fresh read from the SQLite file (NOT via the cache)."""
    db = str(tmp_path / "e2e.db")
    repo = AsyncDecisionRepository(db)
    cache = WriteThroughCache()

    payload = {
        "correlation_id": "dec-e2e",
        "token_id": "TOK_E2E",
        "stage": "PREDICTION",
        "data": {"p_yes": 0.65},
        "model_version": "v1.0",
    }

    async def _db_writer(value):
        await repo.record_event(
            correlation_id=value["correlation_id"],
            token_id=value["token_id"],
            stage=value["stage"],
            data=value["data"],
            model_version=value["model_version"],
        )

    await cache.write("dec-e2e", payload, db_writer=_db_writer)

    # The cache must reflect the value.
    assert cache.read("dec-e2e") == payload

    # The SQLite file must also reflect the value (independent read
    # via sync sqlite3 — NOT via the cache).
    r = _sync_fetch_one(db, "SELECT * FROM decision_events")
    assert r is not None
    assert r["decision_id"] == "dec-e2e"
    assert r["stage"] == "PREDICTION"
    assert r["token_id"] == "TOK_E2E"
    assert json.loads(r["data_json"])["model_version"] == "v1.0"


async def test_singleton_db_pool_reset_between_tests(tmp_path):
    """Smoke test: the autouse fixture resets the singleton ``db_pool``
    between tests. A second test that opens a connection to a fresh
    tmp_path must NOT inherit a stale connection from the prior test."""
    db1 = str(tmp_path / "first.db")
    repo1 = AsyncDecisionRepository(db1)
    await repo1.record_event("dec-1", "TOK", "PREDICTION", {"x": 1})
    # Snapshot the singleton's pool dict.
    pool_size_after_first = len(_singleton_db_pool._pools)
    assert pool_size_after_first >= 1
    # The autouse fixture will clear the singleton BEFORE the next test
    # runs; we can't assert across tests here, but we CAN assert that
    # within a single test, a second DB path yields a separate connection.
    db2 = str(tmp_path / "second.db")
    repo2 = AsyncDecisionRepository(db2)
    await repo2.record_event("dec-2", "TOK", "PREDICTION", {"x": 2})
    assert len(_singleton_db_pool._pools) >= 2
    # Both DBs must have their own row.
    assert len(_sync_fetch_all(db1, "SELECT * FROM decision_events")) == 1
    assert len(_sync_fetch_all(db2, "SELECT * FROM decision_events")) == 1
