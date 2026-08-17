"""
M3 — Persistence Integrity (P0-DAT-02/03) tests.

Coverage:
- Write paths fail loudly and are counted in telemetry (no silent swallow).
- get_stats reflects the ACTIVE backend (cold-standby sqlite when Timescale unreachable).
- fetch_training_samples never fabricates labels: only stored outcome_resolved.
- record_feature_vector round-trip + settlement outcome backfill (KD-27).
- Reconciliation job: clean report artifact; drift detection when rows are lost.
- /api/database/records is backend-aware and auth-protected.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest


def _run(*coros):
    """Python 3.14: asyncio.run no longer installs a current loop and
    asyncio.gather is now a plain function that resolves the loop at call time,
    so the loop must be installed BEFORE gather is invoked."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(asyncio.gather(*coros))
        return results[0] if len(results) == 1 else results
    finally:
        loop.close()


def test_write_paths_count_success_and_failure(tmp_path: Path):
    from core.timescale_db import TimescaleDBEngine

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "write.db")
    ok = _run(
        engine.record_snapshot(token_id="tok-a", slug="mkt-a", best_bid=0.51, best_ask=0.53, mid=0.52, spread=0.02)
    )
    assert ok is True
    ok2 = _run(
        engine.record_tick(token_id="tok-a", best_bid_size=10.0, best_ask_size=8.0, ofi=0.1, micro_price=0.52)
    )
    assert ok2 is True

    stats = engine.get_stats()
    assert stats["snapshots_recorded"] >= 1
    assert stats["ticks_recorded"] >= 1
    assert stats["snapshots_recorded"] == stats["inserts_ok"]["market_snapshots"]
    assert stats["inserts_failed"]["market_snapshots"] == 0
    assert stats["last_error"] is None


def test_write_failure_is_fail_loud_not_silent(tmp_path: Path):
    from core.timescale_db import TimescaleDBEngine

    block_parent = tmp_path / "blocker.txt"
    block_parent.write_text("x")

    engine = TimescaleDBEngine(sqlite_path=block_parent / "market_intelligence.db")  # parent is a FILE -> connect fails
    engine._is_postgres = False
    engine._pool = None

    ok = _run(
        engine.record_snapshot(token_id="tok-x", slug="mkt-x", best_bid=0.5, best_ask=0.52, mid=0.51, spread=0.02)
    )
    assert ok is False
    stats = engine.get_stats()
    assert stats["inserts_failed"]["market_snapshots"] >= 1
    assert stats["last_error"] is not None

    ok_tick = _run(
        engine.record_tick(token_id="tok-x", best_bid_size=1.0, best_ask_size=1.0, ofi=0.0, micro_price=0.51)
    )
    assert ok_tick is False
    assert engine.get_stats()["inserts_failed"]["orderbook_ticks"] >= 1


def test_fetch_training_samples_uses_only_stored_outcomes(tmp_path: Path):
    from core.timescale_db import TimescaleDBEngine

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "labels.db")
    _run(
        engine.record_feature_vector("tok-f1", [0.1] * 32, p_pred=0.6, confidence=0.8),
        engine.record_feature_vector("tok-f1", [0.2] * 32, p_pred=0.4, confidence=0.7, outcome_resolved=1),
        engine.record_feature_vector("tok-f1", [0.3] * 32, p_pred=0.3, confidence=0.6, outcome_resolved=0),
    )

    X, y = engine.fetch_training_samples(min_samples=1)
    assert X is not None and y is not None
    assert len(y) == 2
    # Labels must equal the STORED outcomes exactly — never a random draw.
    assert sorted(int(v) for v in y) == [0, 1]


def test_fetch_training_samples_returns_none_without_outcomes(tmp_path: Path):
    from core.timescale_db import TimescaleDBEngine

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "nolabels.db")
    _run(engine.record_feature_vector("tok-f2", [0.5] * 32, p_pred=0.5, confidence=0.5))
    X, y = engine.fetch_training_samples(min_samples=1)
    assert X is None and y is None


def test_settlement_outcome_backfill_marks_only_null_rows(tmp_path: Path):
    from core.timescale_db import TimescaleDBEngine

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "backfill.db")
    _run(
        engine.record_feature_vector("tok-r", [0.1] * 32, p_pred=0.9, confidence=0.9),
        engine.record_feature_vector("tok-r", [0.2] * 32, p_pred=0.9, confidence=0.9),
        engine.record_feature_vector("tok-r", [0.3] * 32, p_pred=0.9, confidence=0.9, outcome_resolved=0),
    )

    updated = engine.mark_resolved_outcomes("tok-r", resolved_yes=True)
    assert updated == 2
    X, y = engine.fetch_training_samples(min_samples=1)
    assert len(y) == 3
    # DESC timestamp order: newest first, so the pre-resolved row is LAST.
    assert sorted(int(v) for v in y) == [0, 1, 1]  # previously-resolved row untouched


def test_reconciliation_clean_report_and_artifact(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RECON_REPORT_DIR", str(tmp_path / "reports"))
    from core.timescale_db import TimescaleDBEngine
    from core.reconciliation import run_reconciliation

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "recon.db")
    _run(
        engine.record_snapshot(token_id="tok-rec", slug="mkt-rec", best_bid=0.5, best_ask=0.5, mid=0.5, spread=0.01),
        engine.record_tick(token_id="tok-rec", best_bid_size=1.0, best_ask_size=1.0, ofi=0.0, micro_price=0.5),
        engine.record_news("headline", "source", "category", 0.0, ["tok-rec"]),
        engine.record_feature_vector("tok-rec", [0.1] * 32, p_pred=0.5, confidence=0.5, outcome_resolved=1),
    )

    report = run_reconciliation(engine=engine)
    assert report["is_clean"] is True
    assert report["tables"]["market_snapshots"]["drift"] == 0
    assert report["tables"]["orderbook_ticks"]["drift"] == 0
    assert report["tables"]["fundamental_news"]["drift"] == 0
    assert report["tables"]["ml_feature_store"]["drift"] == 0
    artifacts = list((tmp_path / "reports").glob("reconciliation_*.json"))
    assert len(artifacts) >= 1


def test_reconciliation_detects_lost_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RECON_REPORT_DIR", str(tmp_path / "reports"))
    from core.timescale_db import TimescaleDBEngine
    from core.reconciliation import run_reconciliation

    engine = TimescaleDBEngine(sqlite_path=tmp_path / "recon2.db")
    _run(
        engine.record_snapshot(token_id="tok-lost", slug="mkt-lost", best_bid=0.5, best_ask=0.5, mid=0.5, spread=0.01),
        engine.record_snapshot(token_id="tok-lost", slug="mkt-lost", best_bid=0.5, best_ask=0.5, mid=0.5, spread=0.01),
    )

    with sqlite3.connect(engine._sqlite_path) as conn:
        conn.execute("DELETE FROM market_snapshots WHERE id = (SELECT MAX(id) FROM market_snapshots)")

    report = run_reconciliation(engine=engine)
    assert report["is_clean"] is False
    snap = report["tables"]["market_snapshots"]
    assert snap["drift"] > 0
    assert any("drift" in b.lower() or "lost" in b.lower() for b in report["breaches"])


def test_database_records_endpoint_backend_aware_and_authed():
    from fastapi.testclient import TestClient

    from core.timescale_db import timescale_db
    from api.server import app

    _run(
        timescale_db.record_snapshot(token_id="tok-api", slug="mkt-api", best_bid=0.5, best_ask=0.5, mid=0.5, spread=0.01),
    )

    client = TestClient(app)
    unauth = client.get("/api/database/records", params={"table": "market_snapshots"})
    assert unauth.status_code in (401, 403)

    auth = client.get(
        "/api/database/records",
        params={"table": "market_snapshots", "limit": 5},
        headers={"Authorization": "Bearer test-token-123"},
    )
    assert auth.status_code == 200
    body = auth.json()
    assert body["is_success"] is True
    assert any(r["token_id"] == "tok-api" for r in body["records"])
    assert body["backend"] in ("sqlite", "postgres")


def test_database_records_invalid_table_rejected():
    from fastapi.testclient import TestClient

    from api.server import app

    client = TestClient(app)
    resp = client.get(
        "/api/database/records",
        params={"table": "evil_table; DROP TABLE market_snapshots"},
        headers={"Authorization": "Bearer test-token-123"},
    )
    assert resp.status_code == 400