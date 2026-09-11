"""
Unit + integration tests for ``core/alerting.py``.

W10-7 — Threshold-based alerting system.

Covers:

  (1) ``AlertEngine.evaluate(metrics)`` fires every rule whose condition
      is truthy against the supplied metrics dict — one test per default
      rule (7 rules) so a regression that silently breaks one rule's
      condition lambda is isolated to that test.
  (2) ``evaluate({})`` against an empty metrics dict fires ZERO alerts
      — every default rule's ``.get(...)`` returns the documented safe
      default (0 / False / True).
  (3) ``evaluate(good_metrics)`` against a metrics dict that satisfies
      every threshold fires ZERO alerts.
  (4) ``evaluate`` is per-rule failure-isolated: a rule whose message
      template references a missing key does NOT prevent sibling rules
      from firing.
  (5) ``AlertEngine._store`` persists the full Alert shape; the
      ``get_recent(limit, unacknowledged_only)`` accessor returns newest
      first, with the JSON ``metadata`` column decoded back to a dict.
  (6) ``acknowledge(alert_id)`` flips the ``acknowledged`` flag on one
      row and returns True; on an unknown ``alert_id`` returns False.
  (7) ``acknowledge_all()`` flips every unacknowledged row and returns
      the count.
  (8) ``get_stats()`` returns the total / unacked / critical-unacked
      counts.
  (9) API routes via ``TestClient``:
        GET  /api/alerts                          200 + alerts + stats
        GET  /api/alerts/                         trailing-slash alias 200
        GET  /api/alerts/stats                    200 + stats shape
        POST /api/alerts/{id}/acknowledge         200 + ok=True
        POST /api/alerts/{unknown}/acknowledge    404
        POST /api/alerts/acknowledge-all          200 + acknowledged=count
        POST /api/alerts/evaluate                 200 + fired + alerts list

Each test constructs a fresh ``AlertEngine`` against a ``tmp_path``-scoped
SQLite file (no shared state across tests). The module-level singleton
``alert_engine`` is monkeypatched to a fresh tmp-path instance per test
in the API-route tests so the ``register_routes(app)`` routes hit an
isolated DB.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect ALERT_DB_PATH to /tmp BEFORE importing the module. ─────────────
# The alert_engine singleton is constructed at import time and reads its
# DB path from this env var (falling back to ``/app/data/alerts.db``).
# Redirecting keeps the import-time ``_init_db`` call hermetic — it never
# touches the production path, even if the sandbox mounts ``/app/data``
# writable. ``setdefault`` lets an outer runner override if needed.
_TMP_ROOT = Path("/tmp/alerting_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ALERT_DB_PATH", str(_TMP_ROOT / "alerts.db"))

# Make the polymarket-bot package root importable as top-level modules
# (``core.alerting``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.alerting import (  # noqa: E402
    ALERT_DB_PATH,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Alert,
    AlertEngine,
    alert_engine,
    register_routes,
)

# All tests in this module are SYNC ``def`` (not ``async def``) so they run
# cleanly under ``TestClient``'s sync portal — mirrors the convention in
# ``tests/test_live_safety_gate_api.py`` / ``tests/test_decision_ledger.py``
# for their sync tests. No ``pytestmark = pytest.mark.asyncio`` is needed.


# ── Fixture: fresh tmp-path AlertEngine per test ────────────────────────────
@pytest.fixture
def engine(tmp_path):
    """Fresh ``AlertEngine`` against ``tmp_path / alerts.db``.

    Each test gets its own SQLite file so there is zero state leakage
    between tests. The module-level singleton ``alert_engine`` is left
    untouched — we never record or read from it in the unit tests.
    """
    db_path = tmp_path / "alerts.db"
    return AlertEngine(db_path=db_path)


# ── (1) Per-rule firing ────────────────────────────────────────────────────
def test_rule_max_drawdown_exceeded_fires(engine):
    """Rule ``max_drawdown_exceeded`` fires when ``daily_pnl < -2.0``."""
    fired = engine.evaluate({"daily_pnl": -5.0})
    names = [a.name for a in fired]
    assert "max_drawdown_exceeded" in names, (
        f"daily_pnl=-5.0 (< -$2.00 threshold) must fire max_drawdown_exceeded; "
        f"got fired={names}"
    )
    alert = next(a for a in fired if a.name == "max_drawdown_exceeded")
    assert alert.severity == SEVERITY_CRITICAL
    assert alert.category == "risk"
    assert alert.value == -5.0
    assert alert.threshold == -2.0
    assert "-5.00" in alert.message, (
        f"message must format the daily_pnl value; got {alert.message!r}"
    )


def test_rule_kill_switch_activated_fires(engine):
    """Rule ``kill_switch_activated`` fires when ``kill_switch_active=True``."""
    fired = engine.evaluate({"kill_switch_active": True})
    names = [a.name for a in fired]
    assert "kill_switch_activated" in names
    alert = next(a for a in fired if a.name == "kill_switch_activated")
    assert alert.severity == SEVERITY_CRITICAL
    assert alert.category == "risk"
    assert alert.value == 1.0  # bool True coerced to 1.0
    assert "Kill switch is active" in alert.message


def test_rule_model_drift_detected_fires(engine):
    """Rule ``model_drift_detected`` fires when ``psi > 0.25``."""
    fired = engine.evaluate({"psi": 0.5})
    names = [a.name for a in fired]
    assert "model_drift_detected" in names
    alert = next(a for a in fired if a.name == "model_drift_detected")
    assert alert.severity == SEVERITY_WARNING
    assert alert.category == "ml"
    assert alert.value == 0.5
    assert alert.threshold == 0.25
    assert "0.500" in alert.message


def test_rule_model_stale_fires(engine):
    """Rule ``model_stale`` fires when ``model_age_hours > 24``."""
    fired = engine.evaluate({"model_age_hours": 50})
    names = [a.name for a in fired]
    assert "model_stale" in names
    alert = next(a for a in fired if a.name == "model_stale")
    assert alert.severity == SEVERITY_WARNING
    assert alert.category == "ml"
    assert alert.value == 50.0
    assert alert.threshold == 24
    assert "50h" in alert.message


def test_rule_high_latency_fires(engine):
    """Rule ``high_latency`` fires when ``api_latency_ms > 1000``."""
    fired = engine.evaluate({"api_latency_ms": 2000})
    names = [a.name for a in fired]
    assert "high_latency" in names
    alert = next(a for a in fired if a.name == "high_latency")
    assert alert.severity == SEVERITY_WARNING
    assert alert.category == "system"
    assert alert.value == 2000.0
    assert alert.threshold == 1000
    assert "2000ms" in alert.message


def test_rule_backend_unhealthy_fires(engine):
    """Rule ``backend_unhealthy`` fires when ``backend_healthy is False``."""
    fired = engine.evaluate({"backend_healthy": False})
    names = [a.name for a in fired]
    assert "backend_unhealthy" in names
    alert = next(a for a in fired if a.name == "backend_unhealthy")
    assert alert.severity == SEVERITY_CRITICAL
    assert alert.category == "system"
    assert alert.value == 0.0  # bool False coerced to 0.0
    assert "Backend health check failed" in alert.message


def test_rule_data_stale_fires(engine):
    """Rule ``data_stale`` fires when ``data_staleness_seconds > 60``."""
    fired = engine.evaluate({"data_staleness_seconds": 120})
    names = [a.name for a in fired]
    assert "data_stale" in names
    alert = next(a for a in fired if a.name == "data_stale")
    assert alert.severity == SEVERITY_WARNING
    assert alert.category == "data"
    assert alert.value == 120.0
    assert alert.threshold == 60
    assert "120s" in alert.message


# ── (2) Empty metrics dict fires zero alerts ────────────────────────────────
def test_evaluate_empty_metrics_fires_nothing(engine):
    """Empty metrics dict → every rule's ``.get(...)`` returns its safe
    default (0 / False / True), so zero alerts fire."""
    fired = engine.evaluate({})
    assert fired == [], (
        f"empty metrics must fire zero alerts (every default-rule condition "
        f"must be falsy on the documented safe default); got {len(fired)} fired"
    )


# ── (3) Healthy metrics dict fires zero alerts ─────────────────────────────
def test_evaluate_healthy_metrics_fires_nothing(engine):
    """A metrics dict that satisfies every threshold fires ZERO alerts."""
    fired = engine.evaluate(
        {
            "daily_pnl": 1.5,                # > -2.0  → max_drawdown_exceeded NOT fired
            "kill_switch_active": False,     # falsy    → kill_switch NOT fired
            "psi": 0.05,                    # ≤ 0.25  → model_drift NOT fired
            "model_age_hours": 5,           # ≤ 24    → model_stale NOT fired
            "api_latency_ms": 100,          # ≤ 1000  → high_latency NOT fired
            "backend_healthy": True,        # truthy   → backend_unhealthy NOT fired
            "data_staleness_seconds": 5,    # ≤ 60    → data_stale NOT fired
        }
    )
    assert fired == [], (
        f"healthy metrics must fire zero alerts; got {[a.name for a in fired]}"
    )


# ── (4) Per-rule failure isolation ──────────────────────────────────────────
def test_evaluate_isolates_per_rule_failures(engine, monkeypatch):
    """A rule whose ``condition`` raises must NOT prevent sibling rules
    from firing.

    Injects a poisoned rule whose ``condition`` raises RuntimeError; the
    ``model_stale`` rule (with healthy metrics) should still fire when its
    condition is truthy.
    """
    poisoned_rule = {
        "name": "poisoned_rule",
        "category": "system",
        "severity": SEVERITY_WARNING,
        "condition": lambda metrics: (_ for _ in ()).throw(RuntimeError("boom")),
        "message": "should never be formatted",
        "threshold": None,
        "metric_key": None,
    }
    monkeypatch.setattr(engine, "_rules", [poisoned_rule] + engine._rules)

    fired = engine.evaluate({"psi": 0.5})  # only model_drift_detected should fire
    names = [a.name for a in fired]
    assert "model_drift_detected" in names, (
        "sibling rule must still fire even when a poisoned rule raises"
    )
    assert "poisoned_rule" not in names, (
        "poisoned rule must NOT appear in fired list (its condition raised)"
    )


# ── (5) Store / get_recent ─────────────────────────────────────────────────
def test_store_and_get_recent_returns_newest_first(engine):
    """``_store`` persists; ``get_recent`` returns newest first."""
    # Fire three alerts at increasing timestamps.
    engine.evaluate({"psi": 0.5})           # fires model_drift_detected
    time.sleep(0.01)
    engine.evaluate({"api_latency_ms": 2000})  # fires high_latency
    time.sleep(0.01)
    engine.evaluate({"data_staleness_seconds": 120})  # fires data_stale

    recent = engine.get_recent(limit=50)
    assert len(recent) == 3, f"expected 3 stored alerts; got {len(recent)}"
    # Newest first — data_stale fired last.
    assert recent[0]["name"] == "data_stale"
    assert recent[1]["name"] == "high_latency"
    assert recent[2]["name"] == "model_drift_detected"
    # Timestamps strictly descending.
    assert recent[0]["timestamp"] >= recent[1]["timestamp"] >= recent[2]["timestamp"]
    # Metadata column decoded back to a dict.
    for r in recent:
        assert isinstance(r["metadata"], dict), (
            f"metadata column must be decoded to dict; got {type(r['metadata'])}"
        )
        assert "metrics" in r["metadata"]


def test_get_recent_limit_caps_rows(engine):
    """``limit=N`` caps the returned row count."""
    for i in range(5):
        engine.evaluate({"psi": 0.5 + i * 0.01})
    recent = engine.get_recent(limit=2)
    assert len(recent) == 2, f"limit=2 must cap to 2 rows; got {len(recent)}"


def test_get_recent_unacknowledged_only_filters(engine):
    """``unacknowledged_only=True`` filters out acknowledged rows."""
    engine.evaluate({"psi": 0.5})  # alert #1
    engine.evaluate({"api_latency_ms": 2000})  # alert #2
    all_alerts = engine.get_recent()
    assert len(all_alerts) == 2
    # Acknowledge the first.
    first_id = all_alerts[0]["alert_id"]
    assert engine.acknowledge(first_id) is True
    # Filter to unacknowledged — only 1 row remains.
    unacked = engine.get_recent(unacknowledged_only=True)
    assert len(unacked) == 1, (
        f"after acknowledging 1 of 2, unacked count must be 1; got {len(unacked)}"
    )
    assert unacked[0]["alert_id"] != first_id


# ── (6) Acknowledge single ─────────────────────────────────────────────────
def test_acknowledge_single_flips_flag(engine):
    """``acknowledge(alert_id)`` returns True + flips the row's flag."""
    fired = engine.evaluate({"psi": 0.5})
    assert len(fired) == 1
    alert_id = fired[0].alert_id
    # Pre-condition: alert is unacknowledged.
    rows = engine.get_recent()
    assert rows[0]["acknowledged"] == 0
    # Acknowledge.
    assert engine.acknowledge(alert_id) is True
    # Post-condition: alert is acknowledged.
    rows = engine.get_recent()
    assert rows[0]["acknowledged"] == 1


def test_acknowledge_unknown_id_returns_false(engine):
    """``acknowledge(unknown_id)`` returns False (no row updated)."""
    assert engine.acknowledge("nonexistent_alert_id_xyz") is False


# ── (7) Acknowledge all ────────────────────────────────────────────────────
def test_acknowledge_all_clears_unacked(engine):
    """``acknowledge_all()`` flips every unacked row + returns the count."""
    # Fire 3 alerts.
    engine.evaluate({"psi": 0.5})
    engine.evaluate({"api_latency_ms": 2000})
    engine.evaluate({"data_staleness_seconds": 120})
    stats_before = engine.get_stats()
    assert stats_before["unacknowledged"] == 3

    count = engine.acknowledge_all()
    assert count == 3, f"acknowledge_all must return 3 (rows updated); got {count}"

    stats_after = engine.get_stats()
    assert stats_after["unacknowledged"] == 0


def test_acknowledge_all_idempotent_when_empty(engine):
    """``acknowledge_all()`` on an empty DB returns 0 (no rows updated)."""
    assert engine.acknowledge_all() == 0


# ── (8) get_stats ──────────────────────────────────────────────────────────
def test_get_stats_empty_db(engine):
    """Empty DB → all stats zero."""
    stats = engine.get_stats()
    assert stats == {
        "total_alerts": 0,
        "unacknowledged": 0,
        "critical_unacknowledged": 0,
    }


def test_get_stats_counts_critical_unacked(engine):
    """``critical_unacknowledged`` counts CRITICAL-severity unacked rows."""
    # Fire 1 critical (max_drawdown_exceeded) + 1 warning (model_drift).
    engine.evaluate({"daily_pnl": -5.0})   # critical
    engine.evaluate({"psi": 0.5})          # warning
    stats = engine.get_stats()
    assert stats["total_alerts"] == 2
    assert stats["unacknowledged"] == 2
    assert stats["critical_unacknowledged"] == 1, (
        f"1 critical unacked alert expected; got {stats['critical_unacknowledged']}"
    )
    # Acknowledge-all → critical_unacknowledged drops to 0.
    engine.acknowledge_all()
    stats = engine.get_stats()
    assert stats["total_alerts"] == 2  # total unchanged
    assert stats["unacknowledged"] == 0
    assert stats["critical_unacknowledged"] == 0


# ── (9) API routes via TestClient ───────────────────────────────────────────
def _build_client_with_isolated_engine(tmp_path, monkeypatch):
    """Build a TestClient against a minimal FastAPI app with ONLY the
    alerting routes registered, and a fresh ``alert_engine`` singleton
    pointing at ``tmp_path / api_alerts.db``.

    The module-level singleton ``alert_engine`` is monkeypatched in
    ``core.alerting`` so the route handlers (which reference
    ``alert_engine`` directly via closure) hit the isolated DB. Teardown
    auto-reverts the monkeypatch.
    """
    db_path = tmp_path / "api_alerts.db"
    fresh = AlertEngine(db_path=db_path)
    monkeypatch.setattr("core.alerting.alert_engine", fresh)

    app = FastAPI()
    register_routes(app)
    return TestClient(app), fresh


def test_api_get_alerts_empty(tmp_path, monkeypatch):
    """GET /api/alerts on a fresh DB returns 200 + empty list + zero stats."""
    client, _ = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    response = client.get("/api/alerts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["alerts"] == []
    assert body["stats"]["total_alerts"] == 0
    assert body["stats"]["unacknowledged"] == 0
    assert body["stats"]["critical_unacknowledged"] == 0


def test_api_get_alerts_returns_fired_alerts(tmp_path, monkeypatch):
    """GET /api/alerts returns alerts fired via the engine + the stats
    reflect the unacked count."""
    client, engine = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    # Fire two alerts directly via the engine.
    engine.evaluate({"psi": 0.5})              # warning
    engine.evaluate({"daily_pnl": -5.0})       # critical

    response = client.get("/api/alerts")
    assert response.status_code == 200
    body = response.json()
    assert len(body["alerts"]) == 2
    assert body["stats"]["total_alerts"] == 2
    assert body["stats"]["unacknowledged"] == 2
    assert body["stats"]["critical_unacknowledged"] == 1


def test_api_get_alerts_trailing_slash_alias(tmp_path, monkeypatch):
    """GET /api/alerts/ (trailing slash) is the same route as no-slash."""
    client, _ = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    response = client.get("/api/alerts/")
    assert response.status_code == 200
    body = response.json()
    assert "alerts" in body
    assert "stats" in body


def test_api_get_alerts_unacknowledged_only_filter(tmp_path, monkeypatch):
    """GET /api/alerts?unacknowledged_only=true filters acknowledged rows."""
    client, engine = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    engine.evaluate({"psi": 0.5})
    engine.evaluate({"api_latency_ms": 2000})
    # Acknowledge the first fired alert.
    first_id = engine.get_recent()[0]["alert_id"]
    engine.acknowledge(first_id)

    response = client.get("/api/alerts?unacknowledged_only=true")
    assert response.status_code == 200
    body = response.json()
    assert len(body["alerts"]) == 1, (
        f"1 of 2 alerts acknowledged → unacked list len=1; got {len(body['alerts'])}"
    )


def test_api_get_stats_endpoint(tmp_path, monkeypatch):
    """GET /api/alerts/stats returns the stats dict directly."""
    client, engine = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    engine.evaluate({"daily_pnl": -5.0})  # critical
    response = client.get("/api/alerts/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_alerts"] == 1
    assert stats["unacknowledged"] == 1
    assert stats["critical_unacknowledged"] == 1


def test_api_acknowledge_single(tmp_path, monkeypatch):
    """POST /api/alerts/{id}/acknowledge returns 200 + ok=True and flips
    the row's flag."""
    client, engine = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    fired = engine.evaluate({"psi": 0.5})
    alert_id = fired[0].alert_id

    response = client.post(f"/api/alerts/{alert_id}/acknowledge")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify the row is now acknowledged.
    rows = engine.get_recent()
    assert rows[0]["acknowledged"] == 1


def test_api_acknowledge_unknown_returns_404(tmp_path, monkeypatch):
    """POST /api/alerts/{unknown}/acknowledge returns 404."""
    client, _ = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    response = client.post("/api/alerts/nonexistent_id/acknowledge")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_api_acknowledge_all(tmp_path, monkeypatch):
    """POST /api/alerts/acknowledge-all returns 200 + acknowledged=N."""
    client, engine = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    # Fire 3 alerts.
    engine.evaluate({"psi": 0.5})
    engine.evaluate({"api_latency_ms": 2000})
    engine.evaluate({"data_staleness_seconds": 120})

    response = client.post("/api/alerts/acknowledge-all")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["acknowledged"] == 3, (
        f"3 alerts should be acknowledged; got {body['acknowledged']}"
    )
    # Verify stats now show 0 unacked.
    stats = engine.get_stats()
    assert stats["unacknowledged"] == 0


def test_api_evaluate_endpoint_returns_200(tmp_path, monkeypatch):
    """POST /api/alerts/evaluate returns 200 with ``fired`` and ``alerts``
    keys, even when the observability store is empty (best-effort metrics
    gather → empty dict → zero alerts fire)."""
    client, _ = _build_client_with_isolated_engine(tmp_path, monkeypatch)
    response = client.post("/api/alerts/evaluate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "fired" in body
    assert "alerts" in body
    assert isinstance(body["fired"], int)
    assert isinstance(body["alerts"], list)
