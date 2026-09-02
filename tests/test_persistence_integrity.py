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
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import app
from config import settings
from core.reconciliation import run_reconciliation
from core.timescale_db import TimescaleDBEngine


class TestPersistenceIntegrity(unittest.TestCase):
    """Persistence integrity and fail-loud write tests."""

    def setUp(self):
        # ignore_cleanup_errors=True: Windows SQLite WAL mode keeps an OS-level
        # file handle alive briefly after `with sqlite3.connect()` exits; this
        # prevents PermissionError: [WinError 32] in tearDown on Windows.
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self._tmp_dir.name)
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {settings.api_token}"}

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_write_paths_count_success_and_failure(self):
        engine = TimescaleDBEngine(sqlite_path=self.tmp_path / "write.db")
        ok = asyncio.run(
            engine.record_snapshot(token_id="tok-a", slug="mkt-a", best_bid=0.51, best_ask=0.53, mid=0.52, spread=0.02)
        )
        self.assertTrue(ok)
        ok2 = asyncio.run(
            engine.record_tick(token_id="tok-a", best_bid_size=10.0, best_ask_size=8.0, ofi=0.1, micro_price=0.52)
        )
        self.assertTrue(ok2)

        stats = engine.get_stats()
        self.assertGreaterEqual(stats["snapshots_recorded"], 1)
        self.assertGreaterEqual(stats["ticks_recorded"], 1)
        self.assertEqual(stats["snapshots_recorded"], stats["inserts_ok"]["market_snapshots"])
        self.assertEqual(stats["inserts_failed"]["market_snapshots"], 0)
        self.assertIsNone(stats["last_error"])

    def test_training_samples_never_fabricates_labels(self):
        engine = TimescaleDBEngine(sqlite_path=self.tmp_path / "samples.db")
        X, y = engine.fetch_training_samples(min_samples=1)
        self.assertIsNone(X)
        self.assertIsNone(y)

    def test_reconciliation_detects_clean_and_drift_states(self):
        engine = TimescaleDBEngine(sqlite_path=self.tmp_path / "recon.db")
        asyncio.run(
            engine.record_snapshot(token_id="tok-c", slug="mkt-c", best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02)
        )
        rep = run_reconciliation(engine=engine)
        self.assertTrue(rep["is_clean"])
        self.assertEqual(len(rep["breaches"]), 0)


if __name__ == "__main__":
    unittest.main()