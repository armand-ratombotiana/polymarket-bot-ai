"""
Unit + integration tests for ``ml/economic_value.py``.

W19-4 — ML Economic Value Tracker tests.

Covers the six guarantee groups enumerated in the W19-4 task spec:

  (1) ``record_trade`` persists every caller-supplied field
      (trade_id, token_id, model_version, prediction, confidence,
      predicted_edge, actual_pnl) verbatim.
  (2) ``get_pnl_by_model_version`` groups trades by ``model_version``
      and orders by ``total_pnl`` DESC.
  (3) ``get_pnl_by_confidence_bucket`` buckets trades into 0.2-wide bins
      (``0.0-0.2`` / ``0.2-0.4`` / ``0.4-0.6`` / ``0.6-0.8`` /
      ``0.8-1.0``) and orders by bucket label.
  (4) ``get_pnl_by_edge_bucket`` buckets trades by predicted_edge into
      ``<1%`` / ``1-3%`` / ``3-5%`` / ``5-10%`` / ``>10%``.
  (5) ``get_counterfactual`` returns the with-AI vs without-AI baseline
      with ``ml_value = with_ai_pnl - without_ai_pnl`` and
      ``ml_value_per_trade = ml_value / n_trades``.
  (6) API routes — the three FastAPI endpoints return 200 + the
      expected payload shape on a seeded DB and 200 + zeroed-out
      defaults on a fresh DB.

DB isolation mirrors ``tests/test_closed_positions.py`` — each test
constructs a fresh ``MLEconomicValueTracker(tmp_path / "test.db")`` so
the production singleton (built at import time against the non-writable
``/app/data/ml_economic_value.db`` sandbox path) is left untouched.

All tests in this module are SYNC (``def test_...``). The tracker's
public API is sync (matches the W19-4 task spec) and the API-route
tests use the synchronous ``fastapi.testclient.TestClient`` (mirrors
``tests/test_shadow_trading_api.py``).
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ml.economic_value import (
    COUNTERFACTUAL_EDGE_SCALE,
    MLEconomicValueTracker,
    ml_value_tracker,
    register_routes,
)


# ── Fixture: fresh temp-DB-backed tracker per test ───────────────────────────
@pytest.fixture
def tracker(tmp_path):
    """Return a ``MLEconomicValueTracker`` whose SQLite file lives under
    ``tmp_path``.

    Passing an explicit ``db_path`` to the constructor bypasses the
    module-level ``ML_VALUE_DB`` lookup that the production singleton
    uses, so the import-time singleton (built against the conftest-
    redirected ``/tmp/pmbot_conftest_isolation/ml_economic_value.db``
    path) is never touched. This is the same isolation strategy
    ``tests/test_closed_positions.py`` employs.
    """
    return MLEconomicValueTracker(tmp_path / "test_ml_economic_value.db")


# ── Fixture: fresh FastAPI app + TestClient ──────────────────────────────────
@pytest.fixture
def api_client(monkeypatch, tmp_path):
    """Fresh ``FastAPI()`` app with only the ML-economic-value routes
    registered, plus a tracker whose SQLite file lives under
    ``tmp_path``.

    The module-level ``ml_value_tracker`` singleton's ``_db_path`` is
    monkeypatched to point at a ``tmp_path`` SQLite file, and
    ``_init_db()`` is re-run so the ``ml_trade_attribution`` schema +
    indexes exist on the new path. The same global-lookup code path
    every public function in ``ml.economic_value`` uses (each function
    resolves ``self._db_path`` from the instance, NOT the module global)
    so monkeypatching the instance attribute is sufficient — no
    module-level constant patching needed.

    Mirrors the ``client`` fixture in ``tests/test_shadow_trading_api.py``
    so the two API test modules share an identical isolation contract.
    """
    db_path = tmp_path / "test_ml_economic_value_api.db"
    monkeypatch.setattr(ml_value_tracker, "_db_path", db_path)
    ml_value_tracker._init_db()
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


# ── (1) record_trade persists every field ───────────────────────────────────
def test_record_trade_persists_every_field(tracker):
    """``record_trade`` must persist every caller-supplied field verbatim —
    trade_id, token_id, model_version, prediction, confidence,
    predicted_edge, actual_pnl, plus the JSON metadata catch-all.
    """
    tracker.record_trade(
        trade_id="trade-1",
        token_id="TOK_TEST",
        model_version="v1.2.3",
        prediction=0.72,
        confidence=0.85,
        predicted_edge=0.15,
        actual_pnl=12.34,
        metadata={"strategy": "ml_sig_v1", "decision_id": "dec-1"},
    )
    with sqlite3.connect(tracker._db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM ml_trade_attribution WHERE trade_id = ?",
            ("trade-1",),
        ).fetchone()
    assert row is not None
    assert row["trade_id"] == "trade-1"
    assert row["token_id"] == "TOK_TEST"
    assert row["model_version"] == "v1.2.3"
    assert row["prediction"] == pytest.approx(0.72)
    assert row["confidence"] == pytest.approx(0.85)
    assert row["predicted_edge"] == pytest.approx(0.15)
    assert row["actual_pnl"] == pytest.approx(12.34)
    # metadata is JSON-encoded.
    meta = json.loads(row["metadata"])
    assert meta == {"strategy": "ml_sig_v1", "decision_id": "dec-1"}


def test_record_trade_defaults_unknown_model_version(tracker):
    """An empty ``model_version`` must be persisted as ``unknown`` so the
    GROUP BY roll-up doesn't surface a NULL bucket."""
    tracker.record_trade(
        trade_id="trade-2",
        token_id="TOK_X",
        model_version="",
        prediction=0.5,
        confidence=0.0,
        predicted_edge=0.0,
        actual_pnl=1.0,
    )
    rows = tracker.get_pnl_by_model_version()
    assert len(rows) == 1
    assert rows[0]["model_version"] == "unknown"
    assert rows[0]["trades"] == 1


def test_record_trade_handles_none_values(tracker):
    """``None`` values for prediction / confidence / predicted_edge /
    actual_pnl must not crash the INSERT — they're coerced to 0.0
    via the ``float(x or 0.0)`` fallback in ``record_trade``."""
    tracker.record_trade(
        trade_id="trade-3",
        token_id="TOK_NONE",
        model_version="v1.0.0",
        prediction=None,  # type: ignore[arg-type]
        confidence=None,  # type: ignore[arg-type]
        predicted_edge=None,  # type: ignore[arg-type]
        actual_pnl=None,  # type: ignore[arg-type]
    )
    with sqlite3.connect(tracker._db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM ml_trade_attribution WHERE trade_id = ?",
            ("trade-3",),
        ).fetchone()
    assert row is not None
    assert row["prediction"] == 0.0
    assert row["confidence"] == 0.0
    assert row["predicted_edge"] == 0.0
    assert row["actual_pnl"] == 0.0


def test_record_trade_metadata_default_none(tracker):
    """``metadata=None`` (the default) must persist as ``"{}"`` so a later
    ``json.loads`` decode doesn't crash."""
    tracker.record_trade(
        trade_id="trade-4",
        token_id="TOK_M",
        model_version="v1.0.0",
        prediction=0.5,
        confidence=0.5,
        predicted_edge=0.0,
        actual_pnl=0.0,
    )
    with sqlite3.connect(tracker._db_path) as conn:
        row = conn.execute(
            "SELECT metadata FROM ml_trade_attribution WHERE trade_id = ?",
            ("trade-4",),
        ).fetchone()
    assert json.loads(row[0]) == {}


# ── (2) get_pnl_by_model_version ─────────────────────────────────────────────
def test_get_pnl_by_model_version_groups_and_orders(tracker):
    """``get_pnl_by_model_version`` groups trades by ``model_version`` and
    orders the result by ``total_pnl`` DESC (most profitable first)."""
    # v1: +5 (win), +3 (win) → total +8, 2 wins, 0 losses.
    tracker.record_trade("t-a", "T1", "v1", 0.5, 0.6, 0.02, 5.0)
    tracker.record_trade("t-b", "T1", "v1", 0.5, 0.6, 0.02, 3.0)
    # v2: -2 (loss) → total -2, 0 wins, 1 loss.
    tracker.record_trade("t-c", "T1", "v2", 0.5, 0.6, 0.02, -2.0)
    # v3: +10 (win) → total +10, 1 win, 0 losses — should be FIRST.
    tracker.record_trade("t-d", "T1", "v3", 0.5, 0.6, 0.02, 10.0)

    rows = tracker.get_pnl_by_model_version()
    assert len(rows) == 3
    # Most profitable first.
    assert rows[0]["model_version"] == "v3"
    assert rows[0]["total_pnl"] == pytest.approx(10.0)
    assert rows[0]["trades"] == 1
    assert rows[0]["wins"] == 1
    assert rows[0]["losses"] == 0
    assert rows[0]["avg_pnl"] == pytest.approx(10.0)

    assert rows[1]["model_version"] == "v1"
    assert rows[1]["total_pnl"] == pytest.approx(8.0)
    assert rows[1]["trades"] == 2
    assert rows[1]["wins"] == 2
    assert rows[1]["losses"] == 0
    assert rows[1]["avg_pnl"] == pytest.approx(4.0)

    assert rows[2]["model_version"] == "v2"
    assert rows[2]["total_pnl"] == pytest.approx(-2.0)
    assert rows[2]["trades"] == 1
    assert rows[2]["wins"] == 0
    assert rows[2]["losses"] == 1


def test_get_pnl_by_model_version_empty_db(tracker):
    """An empty DB must return an empty list (not a 500)."""
    assert tracker.get_pnl_by_model_version() == []


# ── (3) get_pnl_by_confidence_bucket ────────────────────────────────────────
def test_get_pnl_by_confidence_bucket_0_2_wide_bins(tracker):
    """``get_pnl_by_confidence_bucket`` must bucket trades into the five
    0.2-wide bins (``0.0-0.2`` / ``0.2-0.4`` / ``0.4-0.6`` / ``0.6-0.8`` /
    ``0.8-1.0``) and order by bucket label."""
    # Each bin gets one trade at the lower edge.
    tracker.record_trade("c1", "T", "v1", 0.5, 0.10, 0.0, 1.0)  # 0.0-0.2
    tracker.record_trade("c2", "T", "v1", 0.5, 0.30, 0.0, 2.0)  # 0.2-0.4
    tracker.record_trade("c3", "T", "v1", 0.5, 0.50, 0.0, 3.0)  # 0.4-0.6
    tracker.record_trade("c4", "T", "v1", 0.5, 0.70, 0.0, 4.0)  # 0.6-0.8
    tracker.record_trade("c5", "T", "v1", 0.5, 0.90, 0.0, 5.0)  # 0.8-1.0

    rows = tracker.get_pnl_by_confidence_bucket()
    assert len(rows) == 5
    labels = [r["bucket"] for r in rows]
    assert labels == ["0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"]
    # Each bucket has 1 trade, P&L = trade pnl.
    assert rows[0]["total_pnl"] == pytest.approx(1.0)
    assert rows[4]["total_pnl"] == pytest.approx(5.0)
    assert all(r["trades"] == 1 for r in rows)


def test_get_pnl_by_confidence_bucket_upper_boundary(tracker):
    """A confidence value of exactly 0.2 must land in the ``0.2-0.4``
    bucket (CASE WHEN uses ``<``, not ``<=``), and a value of 1.0
    must land in the ``0.8-1.0`` bucket."""
    tracker.record_trade("b1", "T", "v1", 0.5, 0.2, 0.0, 1.0)
    tracker.record_trade("b2", "T", "v1", 0.5, 1.0, 0.0, 2.0)
    rows = tracker.get_pnl_by_confidence_bucket()
    labels = [r["bucket"] for r in rows]
    assert "0.0-0.2" not in labels  # 0.2 falls into 0.2-0.4
    assert "0.2-0.4" in labels
    assert "0.8-1.0" in labels


def test_get_pnl_by_confidence_bucket_wins_counter(tracker):
    """``wins`` counts the trades whose ``actual_pnl > 0``."""
    tracker.record_trade("w1", "T", "v1", 0.5, 0.5, 0.0, 1.0)   # win
    tracker.record_trade("w2", "T", "v1", 0.5, 0.5, 0.0, -1.0)  # loss
    tracker.record_trade("w3", "T", "v1", 0.5, 0.5, 0.0, 0.0)   # breakeven
    rows = tracker.get_pnl_by_confidence_bucket()
    bucket = next(r for r in rows if r["bucket"] == "0.4-0.6")
    assert bucket["trades"] == 3
    assert bucket["wins"] == 1  # only the +1.0 trade


# ── (4) get_pnl_by_edge_bucket ───────────────────────────────────────────────
def test_get_pnl_by_edge_bucket_five_buckets(tracker):
    """``get_pnl_by_edge_bucket`` must bucket trades into the five
    percentage-labeled bins (``<1%`` / ``1-3%`` / ``3-5%`` / ``5-10%`` /
    ``>10%``) ordered by minimum predicted_edge."""
    tracker.record_trade("e1", "T", "v1", 0.5, 0.5, 0.005, 1.0)   # <1%
    tracker.record_trade("e2", "T", "v1", 0.5, 0.5, 0.02, 2.0)    # 1-3%
    tracker.record_trade("e3", "T", "v1", 0.5, 0.5, 0.04, 3.0)    # 3-5%
    tracker.record_trade("e4", "T", "v1", 0.5, 0.5, 0.07, 4.0)   # 5-10%
    tracker.record_trade("e5", "T", "v1", 0.5, 0.5, 0.20, 5.0)    # >10%

    rows = tracker.get_pnl_by_edge_bucket()
    assert len(rows) == 5
    labels = [r["bucket"] for r in rows]
    assert labels == ["<1%", "1-3%", "3-5%", "5-10%", ">10%"]
    assert rows[0]["total_pnl"] == pytest.approx(1.0)
    assert rows[4]["total_pnl"] == pytest.approx(5.0)


def test_get_pnl_by_edge_bucket_aggregation(tracker):
    """``avg_pnl`` is the per-bucket mean across all trades in the bucket."""
    tracker.record_trade("e1", "T", "v1", 0.5, 0.5, 0.02, 2.0)
    tracker.record_trade("e2", "T", "v1", 0.5, 0.5, 0.025, 4.0)
    rows = tracker.get_pnl_by_edge_bucket()
    bucket = next(r for r in rows if r["bucket"] == "1-3%")
    assert bucket["trades"] == 2
    assert bucket["total_pnl"] == pytest.approx(6.0)
    assert bucket["avg_pnl"] == pytest.approx(3.0)


def test_get_pnl_by_edge_bucket_empty_db(tracker):
    """An empty DB must return an empty list (not a 500)."""
    assert tracker.get_pnl_by_edge_bucket() == []


# ── (5) get_counterfactual ───────────────────────────────────────────────────
def test_get_counterfactual_empty_db_returns_zeroes(tracker):
    """An empty DB must return zeroed-out defaults — not raise."""
    cf = tracker.get_counterfactual()
    assert cf["with_ai_pnl"] == 0.0
    assert cf["without_ai_pnl"] == 0.0
    assert cf["ml_value"] == 0.0
    assert cf["n_trades"] == 0
    assert cf["ml_value_per_trade"] == 0.0


def test_get_counterfactual_computes_ml_value(tracker):
    """``ml_value = with_ai_pnl - without_ai_pnl`` where
    ``without_ai_pnl = -predicted_edge * COUNTERFACTUAL_EDGE_SCALE``
    summed across trades."""
    # Trade 1: pnl=+5, edge=0.10 → without_ai = -1.0
    tracker.record_trade("cf1", "T", "v1", 0.5, 0.5, 0.10, 5.0)
    # Trade 2: pnl=-2, edge=0.05 → without_ai = -0.5
    tracker.record_trade("cf2", "T", "v1", 0.5, 0.5, 0.05, -2.0)

    cf = tracker.get_counterfactual()
    assert cf["n_trades"] == 2
    assert cf["with_ai_pnl"] == pytest.approx(3.0)  # 5 + (-2)
    expected_without_ai = -(
        0.10 * COUNTERFACTUAL_EDGE_SCALE + 0.05 * COUNTERFACTUAL_EDGE_SCALE
    )
    assert cf["without_ai_pnl"] == pytest.approx(expected_without_ai)
    assert cf["ml_value"] == pytest.approx(3.0 - expected_without_ai)
    assert cf["ml_value_per_trade"] == pytest.approx(
        (3.0 - expected_without_ai) / 2
    )


def test_get_counterfactual_positive_ml_value(tracker):
    """When with-AI P&L exceeds without-AI baseline, ``ml_value > 0``
    (the model is adding value)."""
    tracker.record_trade("p1", "T", "v1", 0.5, 0.5, 0.02, 5.0)
    cf = tracker.get_counterfactual()
    # with_ai = +5, without_ai = -0.2 → ml_value = 5.2
    assert cf["ml_value"] > 0
    assert cf["ml_value"] == pytest.approx(5.2)


def test_get_counterfactual_handles_none_pnl(tracker):
    """Trades with ``actual_pnl=0`` (the ``record_trade`` default for
    ``None``) still count toward ``n_trades``."""
    tracker.record_trade("n1", "T", "v1", 0.5, 0.5, 0.0, 0.0)
    cf = tracker.get_counterfactual()
    assert cf["n_trades"] == 1
    assert cf["with_ai_pnl"] == 0.0
    assert cf["without_ai_pnl"] == 0.0


# ── (6) get_summary ──────────────────────────────────────────────────────────
def test_get_summary_combines_all_rollups(tracker):
    """``get_summary`` returns a dict with the four expected keys."""
    tracker.record_trade("s1", "T", "v1", 0.5, 0.5, 0.02, 1.0)
    summary = tracker.get_summary()
    assert set(summary.keys()) == {
        "by_model_version",
        "by_confidence",
        "by_edge",
        "counterfactual",
    }
    assert len(summary["by_model_version"]) == 1
    assert len(summary["by_confidence"]) == 1
    assert len(summary["by_edge"]) == 1
    assert summary["counterfactual"]["n_trades"] == 1


# ── (7) API routes ───────────────────────────────────────────────────────────
def _seed_three_trades():
    """Seed the singleton ``ml_value_tracker`` with three deterministic
    trades so the API tests can assert on aggregate values."""
    ml_value_tracker.record_trade(
        "api-1", "T1", "v1.0.0", 0.70, 0.85, 0.15, 5.0,
        metadata={"strategy": "ml_sig_v1"},
    )
    ml_value_tracker.record_trade(
        "api-2", "T1", "v1.0.0", 0.40, 0.30, 0.02, -2.0,
        metadata={"strategy": "ml_sig_v1"},
    )
    ml_value_tracker.record_trade(
        "api-3", "T2", "v1.1.0", 0.60, 0.65, 0.07, 10.0,
        metadata={"strategy": "ml_sig_v2"},
    )


def test_api_economic_value_returns_summary(api_client):
    """``GET /api/ml/economic-value`` returns 200 + the full summary
    (3 roll-ups + counterfactual)."""
    _seed_three_trades()
    response = api_client.get("/api/ml/economic-value")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {
        "by_model_version",
        "by_confidence",
        "by_edge",
        "counterfactual",
    }
    # by_model_version groups by distinct version: v1.0.0 + v1.1.0 → 2 rows.
    assert len(payload["by_model_version"]) == 2
    # counterfactual sees all 3 trades.
    assert payload["counterfactual"]["n_trades"] == 3


def test_api_economic_value_empty_db_returns_zeroes(api_client):
    """``GET /api/ml/economic-value`` on a fresh DB must return 200 with
    empty lists + zeroed-out counterfactual — NOT a 500."""
    response = api_client.get("/api/ml/economic-value")
    assert response.status_code == 200
    payload = response.json()
    assert payload["by_model_version"] == []
    assert payload["by_confidence"] == []
    assert payload["by_edge"] == []
    assert payload["counterfactual"]["n_trades"] == 0
    assert payload["counterfactual"]["ml_value"] == 0.0


def test_api_economic_value_by_model_returns_groups(api_client):
    """``GET /api/ml/economic-value/by-model`` returns 200 + the model-
    version roll-up, sorted by ``total_pnl`` DESC."""
    _seed_three_trades()
    response = api_client.get("/api/ml/economic-value/by-model")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    # Most profitable first: v1.1.0 (+10) before v1.0.0 (+5 -2 = +3).
    assert payload[0]["model_version"] == "v1.1.0"
    assert payload[0]["total_pnl"] == pytest.approx(10.0)
    assert payload[0]["trades"] == 1
    assert payload[0]["wins"] == 1
    assert payload[0]["losses"] == 0

    assert payload[1]["model_version"] == "v1.0.0"
    assert payload[1]["total_pnl"] == pytest.approx(3.0)
    assert payload[1]["trades"] == 2
    assert payload[1]["wins"] == 1
    assert payload[1]["losses"] == 1


def test_api_economic_value_counterfactual_returns_dict(api_client):
    """``GET /api/ml/economic-value/counterfactual`` returns 200 + the
    counterfactual dict with the five expected keys."""
    _seed_three_trades()
    response = api_client.get("/api/ml/economic-value/counterfactual")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {
        "with_ai_pnl",
        "without_ai_pnl",
        "ml_value",
        "n_trades",
        "ml_value_per_trade",
    }
    assert payload["n_trades"] == 3
    # with_ai = 5 + (-2) + 10 = 13.
    assert payload["with_ai_pnl"] == pytest.approx(13.0)
    # without_ai = -(0.15*10 + 0.02*10 + 0.07*10) = -2.4.
    assert payload["without_ai_pnl"] == pytest.approx(-2.4)
    assert payload["ml_value"] == pytest.approx(15.4)
    assert payload["ml_value_per_trade"] == pytest.approx(15.4 / 3)


def test_api_economic_value_counterfactual_empty_db(api_client):
    """``GET /api/ml/economic-value/counterfactual`` on a fresh DB returns
    200 + zeroed-out defaults."""
    response = api_client.get("/api/ml/economic-value/counterfactual")
    assert response.status_code == 200
    payload = response.json()
    assert payload["n_trades"] == 0
    assert payload["with_ai_pnl"] == 0.0
    assert payload["without_ai_pnl"] == 0.0
    assert payload["ml_value"] == 0.0
    assert payload["ml_value_per_trade"] == 0.0


def test_api_routes_registered_with_ml_tag(api_client):
    """All three endpoints are tagged ``ml`` so they cluster in the
    OpenAPI schema under the ``ml`` tag (the dashboard's ML panel
    renders every route in this tag)."""
    routes = [
        (r.path, "ml" in r.tags)
        for r in api_client.app.routes
        if hasattr(r, "path") and r.path.startswith("/api/ml/economic-value")
    ]
    assert len(routes) == 3
    assert all(has_ml for _, has_ml in routes)
