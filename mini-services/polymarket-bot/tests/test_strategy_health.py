"""
Unit + integration tests for the W24-8 strategy health monitor.

Covers:

  (1) ``StrategyHealthMonitor.check_strategy`` — every threshold branch:
        * healthy strategy → ``HEALTHY`` + strategy NOT disabled
        * low win rate → ``DISABLED`` + registry flag set + alert fired
        * negative expectancy → ``DISABLED``
        * high max drawdown → ``DISABLED``
        * high error rate → ``DISABLED``
        * stale strategy (>24h since last trade) → ``DEGRADED``
        * insufficient trades (<10) → ``DEGRADED`` (not enough data)
        * no trades ever → ``INACTIVE``
        * already-disabled strategy stays ``DISABLED`` on re-check
        * disable is idempotent — re-check on disabled strategy does
          NOT re-fire the alert
  (2) ``StrategyHealthMonitor.get_all_health`` / ``get_summary`` —
      the dict / count payloads surfaced by the API routes.
  (3) ``StrategyRegistry.disable`` / ``enable`` / ``is_disabled`` —
      the sync auto-disable entry point:
        * disable marks the strategy + cancels running instance
        * disable short-circuits ``start_strategy``
        * enable clears the flag so ``start_strategy`` works again
        * disable on an unknown strategy_id returns False
        * disable resolves legacy aliases
  (4) ``AlertEngine.record_alert`` — convenience wrapper around
      ``fire_alert`` that constructs an ``Alert`` from primitive fields.
  (5) API routes via ``TestClient``:
        GET /api/strategies/health           200 + list shape
        GET /api/strategies/health/summary   200 + counts shape

Each test constructs a fresh ``StrategyHealthMonitor`` so there is zero
state leakage between tests. The module-level singleton
``strategy_health_monitor`` is monkeypatched in the API-route tests so
the route handlers (which reference it directly via closure) hit an
isolated monitor instance.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect ALERT_DB_PATH to /tmp BEFORE importing the module. ─────────────
# The alert_engine singleton is constructed at import time and reads its
# DB path from this env var (falling back to ``/app/data/alerts.db``).
# ``/app/data`` is read-only in the sandbox; redirecting keeps the
# import-time ``_init_db`` call hermetic so it doesn't crash on first
# import. ``setdefault`` lets the conftest / an outer runner override
# if it needs to.
_TMP_ROOT = Path("/tmp/strategy_health_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ALERT_DB_PATH", str(_TMP_ROOT / "alerts.db"))

# Make the polymarket-bot package root importable as top-level modules
# (``core.strategy_health``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from fastapi.testclient import TestClient  # noqa: E402

from core.alerting import (  # noqa: E402
    SEVERITY_WARNING,
    AlertEngine,
    alert_engine,
)
from core.strategy_health import (  # noqa: E402
    StrategyHealth,
    StrategyHealthMonitor,
    StrategyHealthStatus,
    strategy_health_monitor,
)
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STRATEGY_CATALOG,
    strategy_registry,
)

# All tests in this module are SYNC ``def`` (not ``async def``) so they
# run cleanly under ``TestClient``'s sync portal — mirrors the
# convention in ``tests/test_alerting.py`` /
# ``tests/test_decision_ledger.py`` for their sync tests. No
# ``pytestmark = pytest.mark.asyncio`` is needed.

VALID_TOKEN = "test-token-conftest"  # set by conftest.py

# A stable test strategy_id from the real catalog so the disable flow
# exercises the actual registry path (not a fabricated id that the
# catalog lookup rejects).
_TEST_STRATEGY_ID = "mm_avellaneda_stoikov"


# ── Fixture: fresh monitor per test ─────────────────────────────────────────
@pytest.fixture
def monitor():
    """Fresh ``StrategyHealthMonitor`` per test (no shared ``_health``).

    The module-level singleton ``strategy_health_monitor`` is left
    untouched — the API-route tests below monkeypatch it explicitly.
    """
    return StrategyHealthMonitor()


# ── Fixture: registry state cleanup ────────────────────────────────────────
@pytest.fixture
def clean_registry():
    """Ensure ``strategy_registry._disabled`` is clean before + after
    the test so a disable from one test doesn't leak into the next.

    Belt-and-braces with the autouse ``_reset_store_factory_defaults``
    conftest fixture (which doesn't reset ``_disabled`` because the
    registry predates the disabled-flag feature and conftest doesn't
    know about it). Run BEFORE the test so the test starts from a
    clean slate; discard AFTER the test for the next test's clean
    baseline.
    """
    strategy_registry._disabled.clear()
    yield strategy_registry
    strategy_registry._disabled.clear()


# ── Helper: build a trade list with given pnls + recent timestamps ─────────
def _trades(pnls: list[float], age_seconds: float = 60.0) -> list[dict]:
    """Build a trade list whose ``closed_at`` is ``now - age_seconds``.

    All trades share the same ``closed_at`` (so the staleness check
    doesn't fire by accident); pass a different ``age_seconds`` for
    stale-strategy tests.
    """
    now = time.time()
    return [
        {"pnl": p, "closed_at": now - age_seconds} for p in pnls
    ]


# ── Helper: trades with monotonic timestamps (oldest first) ─────────────────
def _trades_spread(pnls: list[float], start_age_seconds: float = 600.0) -> list[dict]:
    """Build a trade list whose timestamps span ``start_age_seconds``
    down to ``~0`` (most recent) so the last-trade-time field tracks
    the LAST entry's timestamp.
    """
    now = time.time()
    n = len(pnls)
    if n == 0:
        return []
    return [
        {"pnl": p, "closed_at": now - start_age_seconds * (n - i) / n}
        for i, p in enumerate(pnls)
    ]


# ── (1) check_strategy threshold branches ──────────────────────────────────

def test_healthy_strategy_stays_enabled(monitor, clean_registry):
    """A strategy with win_rate ≥ 30%, expectancy ≥ -$0.05, drawdown
    ≤ 15%, and error rate ≤ 10/h stays ``HEALTHY`` and is NOT disabled
    in the registry."""
    # 11 trades, 10 wins, 1 loss — win_rate = 90.9%, expectancy = +$0.044/trade
    pnls = [0.10, 0.05, -0.02, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04]
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.HEALTHY, (
        f"healthy strategy must stay HEALTHY; got {health.status} "
        f"(win_rate={health.win_rate:.2%}, expectancy=${health.expectancy:.4f}, "
        f"max_dd={health.max_drawdown:.2%})"
    )
    assert health.win_rate >= 0.30
    assert health.expectancy >= -0.05
    assert health.max_drawdown <= 0.15
    assert health.n_trades == 11
    # Registry NOT disabled — healthy strategies keep trading.
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False


def test_low_win_rate_auto_disables(monitor, clean_registry):
    """Win rate below 30% (e.g., 2 wins / 10 trades = 20%) auto-disables
    the strategy in the registry + sets status to DISABLED with the
    win-rate reason in ``disable_reason``."""
    # 2 wins / 10 trades = 20% (below the 30% threshold).
    # Use small absolute pnls so expectancy stays above -$0.05 — we
    # want THIS test to isolate the win-rate branch only (the threshold
    # check order is win_rate → expectancy → drawdown → errors).
    pnls = [0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01]
    # expectancy = (0.01 - 0.01 - 0.01 + 0.01 - 0.01 - 0.01 - 0.01 - 0.01 - 0.01 - 0.01) / 10 = -0.06 / 10 = -0.006
    # (above the -$0.05 threshold so this test isolates win_rate, not expectancy)
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.DISABLED
    assert "Win rate" in health.disable_reason
    assert "30%" in health.disable_reason
    assert health.disable_time > 0
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True


def test_negative_expectancy_auto_disables(monitor, clean_registry):
    """Expectancy below -$0.05 per trade (e.g., -$0.088) auto-disables
    the strategy + the reason mentions ``Expectancy``.

    The trade list is constructed so win_rate stays ≥ 30% so this test
    isolates the expectancy branch only."""
    # 5 wins / 10 trades = 50% (above the 30% threshold).
    # Big losing trades pull expectancy below -$0.05.
    pnls = [0.02, -0.20, 0.01, -0.18, 0.03, -0.22, 0.02, -0.15, 0.04, -0.25]
    # expectancy = (0.02 - 0.20 + 0.01 - 0.18 + 0.03 - 0.22 + 0.02 - 0.15 + 0.04 - 0.25) / 10 = -0.88 / 10 = -0.088
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.DISABLED
    assert "Expectancy" in health.disable_reason
    assert health.expectancy < -0.05
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True


def test_high_drawdown_auto_disables(monitor, clean_registry):
    """Max drawdown above 15% (e.g., 125%) auto-disables the strategy
    + the reason mentions ``Drawdown``.

    Constructed so win_rate ≥ 30% AND expectancy ≥ -$0.05 — isolates
    the drawdown branch only."""
    # 7 wins / 10 trades = 70%. Positive expectancy on average.
    # But the equity curve grows then CRASHES — drawdown > 15%.
    pnls = [0.20, 0.20, -0.10, -0.20, -0.20, 0.05, 0.05, 0.05, 0.05, 0.05]
    # equity = [0, 0.20, 0.40, 0.30, 0.10, -0.10, -0.05, 0.00, 0.05, 0.10, 0.15]
    # peak = 0.40 (at index 2), min after peak = -0.10 (at index 5)
    # max_dd = (0.40 - (-0.10)) / 0.40 = 1.25
    # win_rate = 7/10 = 0.70 (above 30% threshold)
    # expectancy = (0.20 + 0.20 - 0.10 - 0.20 - 0.20 + 0.05 + 0.05 + 0.05 + 0.05 + 0.05) / 10 = 0.15 / 10 = 0.015
    # (above -$0.05 threshold — so this test isolates drawdown)
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.DISABLED
    assert "Drawdown" in health.disable_reason
    assert health.max_drawdown > 0.15
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True


def test_high_error_rate_auto_disables(monitor, clean_registry):
    """Error rate above 10/h auto-disables the strategy + the reason
    mentions ``Error rate``."""
    # Healthy metrics otherwise — win_rate, expectancy, drawdown all pass.
    pnls = [0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04]
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=20)

    assert health.status == StrategyHealthStatus.DISABLED
    assert "Error rate" in health.disable_reason
    assert "20" in health.disable_reason
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True


def test_stale_strategy_marked_degraded(monitor, clean_registry):
    """A strategy whose last trade was >24h ago is marked DEGRADED
    (NOT DISABLED — staleness is a warning, not an auto-disable
    trigger)."""
    # 10 healthy trades, all 30h ago.
    pnls = [0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05]
    trades = _trades(pnls, age_seconds=30 * 3600)  # 30h

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.DEGRADED, (
        f"stale strategy (>24h since last trade) must be DEGRADED; "
        f"got {health.status}"
    )
    assert health.last_trade_time > 0
    # Not disabled — staleness is a warning, not an auto-disable.
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False


def test_insufficient_trades_marked_degraded(monitor, clean_registry):
    """Fewer than ``min_trades_for_eval`` (10) trades → DEGRADED (not
    enough data to evaluate) — does NOT auto-disable even if the
    visible metrics look bad (a single bad trade shouldn't disable)."""
    # 5 trades, all losses — would fail win_rate if evaluated, but we
    # haven't reached the min_trades_for_eval threshold so the strategy
    # is marked DEGRADED instead.
    pnls = [-0.10, -0.20, -0.15, -0.05, -0.10]
    trades = _trades(pnls, age_seconds=60.0)

    health = monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)

    assert health.status == StrategyHealthStatus.DEGRADED
    assert health.n_trades == 5
    assert health.disable_reason == ""  # Not disabled.
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False


def test_no_trades_marked_inactive(monitor, clean_registry):
    """A strategy with zero trades observed is marked INACTIVE (the
    monitor has never seen it) — distinct from DEGRADED (seen but
    insufficient data)."""
    health = monitor.check_strategy(_TEST_STRATEGY_ID, [], errors=0)

    assert health.status == StrategyHealthStatus.INACTIVE
    assert health.n_trades == 0
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False


def test_already_disabled_stays_disabled(monitor, clean_registry):
    """Once a strategy is auto-disabled, subsequent ``check_strategy``
    calls preserve the DISABLED status (operator must explicitly
    ``enable()`` to clear). Metrics are still refreshed so the
    dashboard can render the post-disable values."""
    # First check — disable due to low win rate.
    bad_pnls = [0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01]
    trades_bad = _trades(bad_pnls, age_seconds=60.0)
    health1 = monitor.check_strategy(_TEST_STRATEGY_ID, trades_bad, errors=0)
    assert health1.status == StrategyHealthStatus.DISABLED
    first_disable_time = health1.disable_time

    # Second check — even with healthy metrics, status stays DISABLED.
    good_pnls = [0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04]
    trades_good = _trades(good_pnls, age_seconds=60.0)
    health2 = monitor.check_strategy(_TEST_STRATEGY_ID, trades_good, errors=0)

    assert health2.status == StrategyHealthStatus.DISABLED, (
        "already-disabled strategy must stay DISABLED until operator "
        "explicitly enable()s it in the registry"
    )
    # Metrics refreshed — win_rate reflects the new (healthy) trades.
    assert health2.win_rate == 1.0  # 10/10 wins
    # disable_time preserved from the first disable.
    assert health2.disable_time == first_disable_time


def test_disable_is_idempotent_no_duplicate_alert(monitor, clean_registry, monkeypatch):
    """A second ``check_strategy`` on an already-disabled strategy
    does NOT re-fire the alert (the operator already saw it; the
    dashboard would surface the same alert twice otherwise)."""
    fired = []
    # Patch ``alert_engine.record_alert`` to capture calls without
    # touching the real SQLite store.
    def _capture_record_alert(name, category, severity, message, **kwargs):
        fired.append((name, category, severity, message))

    # Patch BOTH the import path the monitor uses (the module-level
    # ``alert_engine``) AND the module attribute so the lazy import
    # inside ``_disable`` sees the patched instance.
    monkeypatch.setattr(
        "core.alerting.alert_engine.record_alert", _capture_record_alert,
    )

    bad_pnls = [0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01]
    trades = _trades(bad_pnls, age_seconds=60.0)

    # First check — disable + fire alert.
    monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)
    assert len(fired) == 1, "first disable must fire exactly one alert"
    name, category, severity, message = fired[0]
    assert name == "strategy_auto_disabled"
    assert category == "strategy"
    assert severity == SEVERITY_WARNING
    assert _TEST_STRATEGY_ID in message

    # Second check — already disabled, no new alert fired.
    monitor.check_strategy(_TEST_STRATEGY_ID, trades, errors=0)
    assert len(fired) == 1, (
        "idempotent disable — second check_strategy on already-disabled "
        "strategy must NOT re-fire the alert"
    )


# ── (2) get_all_health / get_summary ───────────────────────────────────────

def test_get_all_health_returns_dicts(monitor, clean_registry):
    """``get_all_health`` returns a list of dicts (one per evaluated
    strategy) so the API route can JSON-serialise directly."""
    # Evaluate two strategies — one healthy, one stale.
    healthy_pnls = [0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04]
    monitor.check_strategy("healthy_strat", _trades(healthy_pnls, 60.0), errors=0)

    stale_pnls = [0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05]
    monitor.check_strategy("stale_strat", _trades(stale_pnls, 30 * 3600), errors=0)

    all_health = monitor.get_all_health()
    assert isinstance(all_health, list)
    assert len(all_health) == 2

    by_name = {h["strategy_name"]: h for h in all_health}
    assert set(by_name.keys()) == {"healthy_strat", "stale_strat"}

    # Each dict carries every ``StrategyHealth`` field as a JSON-safe
    # value (status is stringified, not the raw Enum).
    for h in all_health:
        assert isinstance(h["strategy_name"], str)
        assert isinstance(h["status"], str)  # Enum.value, not Enum
        assert h["status"] in {s.value for s in StrategyHealthStatus}
        assert isinstance(h["win_rate"], float)
        assert isinstance(h["expectancy"], float)
        assert isinstance(h["max_drawdown"], float)
        assert isinstance(h["n_trades"], int)
        assert isinstance(h["n_errors"], int)
        assert isinstance(h["last_trade_time"], float)
        assert isinstance(h["last_check"], float)
        assert isinstance(h["disable_reason"], str)
        assert isinstance(h["disable_time"], float)

    assert by_name["healthy_strat"]["status"] == "healthy"
    assert by_name["stale_strat"]["status"] == "degraded"


def test_get_summary_counts_by_status(monitor, clean_registry):
    """``get_summary`` returns counts of healthy / degraded / disabled
    / inactive strategies seen so far."""
    # 1 healthy
    monitor.check_strategy(
        "h1", _trades([0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04], 60.0),
        errors=0,
    )
    # 1 stale (degraded)
    monitor.check_strategy(
        "s1", _trades([0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05], 30 * 3600),
        errors=0,
    )
    # 1 disabled (low win rate)
    monitor.check_strategy(
        "d1", _trades([0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01], 60.0),
        errors=0,
    )
    # 1 inactive (no trades)
    monitor.check_strategy("i1", [], errors=0)

    summary = monitor.get_summary()
    assert summary == {
        "total_strategies": 4,
        "healthy": 1,
        "degraded": 1,
        "disabled": 1,
        "inactive": 1,
    }


def test_get_summary_empty_monitor():
    """``get_summary`` on a fresh monitor returns zero counts."""
    m = StrategyHealthMonitor()
    assert m.get_summary() == {
        "total_strategies": 0,
        "healthy": 0,
        "degraded": 0,
        "disabled": 0,
        "inactive": 0,
    }


# ── (3) StrategyRegistry.disable / enable / is_disabled ────────────────────

def test_registry_disable_marks_disabled(clean_registry):
    """``disable`` adds the strategy_id to ``_disabled`` so subsequent
    ``start_strategy`` short-circuits."""
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False
    ok = clean_registry.disable(_TEST_STRATEGY_ID, reason="manual test")
    assert ok is True
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True


def test_registry_enable_clears_disabled(clean_registry):
    """``enable`` clears the disabled flag so ``start_strategy`` works."""
    clean_registry.disable(_TEST_STRATEGY_ID, reason="manual")
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is True
    ok = clean_registry.enable(_TEST_STRATEGY_ID)
    assert ok is True
    assert clean_registry.is_disabled(_TEST_STRATEGY_ID) is False


def test_registry_enable_unknown_returns_false(clean_registry):
    """``enable`` on a NOT-disabled strategy returns False (no-op,
    not an error)."""
    assert clean_registry.enable(_TEST_STRATEGY_ID) is False


def test_registry_disable_unknown_returns_false(clean_registry):
    """``disable`` on an unknown strategy_id returns False (the
    catalog lookup fails — the disable is a no-op)."""
    ok = clean_registry.disable("totally_fake_strategy_xyz", reason="nope")
    assert ok is False


def test_registry_disable_resolves_legacy_alias(clean_registry):
    """``disable`` resolves legacy aliases (``market_maker`` →
    ``mm_avellaneda_stoikov``) so a disable request by either name
    lands on the same catalog row."""
    ok = clean_registry.disable("market_maker", reason="alias test")
    assert ok is True
    # The canonical id is what's in ``_disabled``.
    assert clean_registry.is_disabled("mm_avellaneda_stoikov") is True
    # The alias also reports disabled.
    assert clean_registry.is_disabled("market_maker") is True


def test_registry_disable_short_circuits_start_strategy(clean_registry):
    """A disabled strategy cannot be started — ``start_strategy``
    returns False WITHOUT instantiating the strategy."""
    clean_registry.disable(_TEST_STRATEGY_ID, reason="manual test")

    import asyncio
    # ``start_strategy`` is async; run it on a fresh loop.
    ok = asyncio.new_event_loop().run_until_complete(
        clean_registry.start_strategy(_TEST_STRATEGY_ID)
    )
    assert ok is False, (
        "disabled strategy must NOT be startable without explicit enable()"
    )
    # Confirm the instance was never created.
    assert _TEST_STRATEGY_ID not in clean_registry.get_active_instances()


def test_registry_disable_adds_is_disabled_to_catalog(clean_registry):
    """``disable`` surfaces an ``is_disabled`` boolean in
    ``get_catalog()`` so the UI can render a DISABLED badge on
    auto-disabled strategies."""
    catalog = clean_registry.get_catalog()
    # Every catalog row carries the new ``is_disabled`` field.
    assert all("is_disabled" in row for row in catalog)
    # By default, nothing is disabled.
    assert all(row["is_disabled"] is False for row in catalog)

    clean_registry.disable(_TEST_STRATEGY_ID, reason="catalog test")
    catalog_after = clean_registry.get_catalog()
    row = next(r for r in catalog_after if r["strategy_id"] == _TEST_STRATEGY_ID)
    assert row["is_disabled"] is True


# ── (4) AlertEngine.record_alert ──────────────────────────────────────────

def test_record_alert_returns_alert_with_id(tmp_path):
    """``record_alert`` constructs an ``Alert`` with a fresh ``alert_id``
    + ``timestamp`` and delegates to ``fire_alert`` (durable SQLite
    store). Returns the constructed ``Alert`` so the caller can log
    / inspect it."""
    engine = AlertEngine(db_path=tmp_path / "test_alerts.db")

    alert = engine.record_alert(
        name="strategy_auto_disabled",
        category="strategy",
        severity=SEVERITY_WARNING,
        message="Strategy 'mm_avellaneda_stoikov' auto-disabled: Win rate 20.0% below 30%",
    )

    assert alert.name == "strategy_auto_disabled"
    assert alert.category == "strategy"
    assert alert.severity == SEVERITY_WARNING
    assert "Win rate 20.0%" in alert.message
    assert alert.alert_id  # UUID4 hex (32 chars)
    assert len(alert.alert_id) == 32
    assert alert.timestamp > 0
    assert alert.acknowledged is False

    # The alert is persisted via ``fire_alert`` → ``_store`` → SQLite.
    recent = engine.get_recent(limit=10)
    matching = [a for a in recent if a["name"] == "strategy_auto_disabled"]
    assert len(matching) == 1
    assert matching[0]["alert_id"] == alert.alert_id


def test_record_alert_accepts_optional_fields(tmp_path):
    """``record_alert`` accepts optional ``value`` / ``threshold`` /
    ``metadata`` so the caller can supply the breach value + the
    threshold it breached (mirrors the ``Alert`` dataclass shape)."""
    engine = AlertEngine(db_path=tmp_path / "test_alerts.db")

    alert = engine.record_alert(
        name="strategy_auto_disabled",
        category="strategy",
        severity=SEVERITY_WARNING,
        message="Win rate 20.0% below 30%",
        value=0.20,
        threshold=0.30,
        metadata={"strategy_id": "mm_avellaneda_stoikov"},
    )

    assert alert.value == 0.20
    assert alert.threshold == 0.30
    assert alert.metadata == {"strategy_id": "mm_avellaneda_stoikov"}


# ── (5) API routes via TestClient ──────────────────────────────────────────

def _build_client_with_isolated_monitor(monkeypatch, fresh_monitor):
    """Build a TestClient against the real ``api.server.app`` (so the
    ``enforce_api_auth`` middleware + auth policy is exercised end-to-end)
    while the ``strategy_health_monitor`` singleton is monkeypatched to
    a fresh instance.

    The route handlers in ``api.server`` reference ``strategy_health_monitor``
    via closure (the module-level ``from core.strategy_health import
    strategy_health_monitor`` import binds the singleton into the
    ``api.server`` namespace at import time), so monkeypatching BOTH:

    * ``core.strategy_health.strategy_health_monitor`` — so any
      downstream import that does ``from core.strategy_health import
      strategy_health_monitor`` (lazy / re-import) sees the fresh
      instance.
    * ``api.server.strategy_health_monitor`` — so the route handler
      closures (which captured the singleton at import time via the
      ``from X import Y`` form) see the fresh instance.

    ...is required. Patching only one would leave a stale reference
    in the other namespace, returning the production singleton's
    (empty) state instead of the fresh instance's evaluated state.
    """
    from api.server import app
    # Patch BOTH namespaces — the canonical module AND the closure
    # capture in ``api.server``.
    monkeypatch.setattr(
        "core.strategy_health.strategy_health_monitor", fresh_monitor,
    )
    monkeypatch.setattr(
        "api.server.strategy_health_monitor", fresh_monitor,
    )
    return TestClient(app, raise_server_exceptions=False), fresh_monitor


def test_api_get_strategy_health_returns_200(monkeypatch):
    """``GET /api/strategies/health`` returns 200 + a JSON list."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    # Empty monitor → empty list.
    assert body == []


def test_api_get_strategy_health_returns_per_strategy_dicts(monkeypatch):
    """``GET /api/strategies/health`` returns one dict per evaluated
    strategy, each carrying the full ``StrategyHealth`` field set
    (status is stringified, not the raw Enum)."""
    fresh = StrategyHealthMonitor()
    # Evaluate one healthy + one disabled strategy.
    fresh.check_strategy(
        "healthy_strat",
        _trades([0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04], 60.0),
        errors=0,
    )
    fresh.check_strategy(
        "bad_strat",
        _trades([0.01, -0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01], 60.0),
        errors=0,
    )

    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2

    by_name = {h["strategy_name"]: h for h in body}
    assert by_name["healthy_strat"]["status"] == "healthy"
    assert by_name["bad_strat"]["status"] == "disabled"
    assert "Win rate" in by_name["bad_strat"]["disable_reason"]


def test_api_get_strategy_health_summary_returns_200(monkeypatch):
    """``GET /api/strategies/health/summary`` returns 200 + the counts
    dict (total / healthy / degraded / disabled / inactive)."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health/summary",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "total_strategies": 0,
        "healthy": 0,
        "degraded": 0,
        "disabled": 0,
        "inactive": 0,
    }


def test_api_get_strategy_health_summary_counts_after_checks(monkeypatch):
    """``GET /api/strategies/health/summary`` reflects the latest
    ``check_strategy`` calls — counts of healthy / degraded / disabled
    / inactive are derived from the in-memory ``_health`` dict."""
    fresh = StrategyHealthMonitor()
    # 1 healthy + 1 stale + 1 disabled + 1 inactive.
    fresh.check_strategy(
        "h1", _trades([0.10, 0.05, 0.08, 0.03, 0.06, 0.04, 0.05, 0.03, 0.02, 0.04], 60.0),
        errors=0,
    )
    fresh.check_strategy(
        "s1", _trades([0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05, 0.04, 0.03, 0.05], 30 * 3600),
        errors=0,
    )
    fresh.check_strategy(
        "d1", _trades([0.01, -0.01, 0.01, -0.01, -0.01, -0.01, -0.01, -0.01, -0.01, 0.01], 60.0),
        errors=0,
    )
    fresh.check_strategy("i1", [], errors=0)

    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get(
        "/api/strategies/health/summary",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "total_strategies": 4,
        "healthy": 1,
        "degraded": 1,
        "disabled": 1,
        "inactive": 1,
    }


def test_api_get_strategy_health_requires_auth(monkeypatch):
    """``GET /api/strategies/health`` requires the bearer-token auth —
    a missing header returns 401 (the ``enforce_api_auth`` middleware
    short-circuits)."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get("/api/strategies/health")  # No auth header.
    assert response.status_code == 401, (
        f"missing auth header must return 401; got {response.status_code}"
    )


def test_api_get_strategy_health_summary_requires_auth(monkeypatch):
    """``GET /api/strategies/health/summary`` also requires the bearer
    token — the auth middleware applies to every non-public path."""
    fresh = StrategyHealthMonitor()
    client, _ = _build_client_with_isolated_monitor(monkeypatch, fresh)
    response = client.get("/api/strategies/health/summary")
    assert response.status_code == 401


# ── Module-level singleton smoke test ──────────────────────────────────────

def test_module_singleton_importable():
    """The module-level ``strategy_health_monitor`` singleton is importable
    + carries the documented public surface (``check_strategy``,
    ``get_all_health``, ``get_summary``)."""
    assert strategy_health_monitor is not None
    assert hasattr(strategy_health_monitor, "check_strategy")
    assert hasattr(strategy_health_monitor, "get_all_health")
    assert hasattr(strategy_health_monitor, "get_summary")
    # ``_thresholds`` is the configurable threshold dict — verified to
    # carry the W24-8 spec's documented defaults so an operator
    # overriding them via construction-time mutation can introspect.
    assert strategy_health_monitor._thresholds["min_win_rate"] == 0.30
    assert strategy_health_monitor._thresholds["min_expectancy"] == -0.05
    assert strategy_health_monitor._thresholds["max_drawdown"] == 0.15
    assert strategy_health_monitor._thresholds["min_trades_for_eval"] == 10
    assert strategy_health_monitor._thresholds["max_errors_per_hour"] == 10
    assert strategy_health_monitor._thresholds["stale_strategy_hours"] == 24


def test_strategy_catalog_has_real_strategy_ids():
    """Sanity check that the test strategy_id we use actually exists in
    the catalog (catches a rename in the catalog before it silently
    breaks every disable-flow test in this module)."""
    ids = {s.strategy_id for s in STRATEGY_CATALOG}
    assert _TEST_STRATEGY_ID in ids, (
        f"test strategy_id '{_TEST_STRATEGY_ID}' not in catalog — "
        f"update _TEST_STRATEGY_ID in this test file. Catalog has {len(ids)} "
        f"strategies."
    )
    # And confirm at least one is IMPLEMENTED (so disable has a real
    # catalog row to mark).
    assert any(s.status == STATUS_IMPLEMENTED for s in STRATEGY_CATALOG)


def test_strategy_health_dataclass_to_dict_stringifies_status():
    """``StrategyHealth.to_dict`` returns a JSON-safe dict — ``status``
    is the Enum's value (string), not the Enum itself (FastAPI's
    default JSON encoder can't serialise an Enum)."""
    h = StrategyHealth(strategy_name="test_strat")
    h.status = StrategyHealthStatus.DISABLED
    h.disable_reason = "Win rate 20.0% below 30%"
    h.disable_time = 1234567890.0

    d = h.to_dict()
    assert d["strategy_name"] == "test_strat"
    assert d["status"] == "disabled"  # str, not Enum
    assert d["disable_reason"] == "Win rate 20.0% below 30%"
    assert d["disable_time"] == 1234567890.0
    # ``status`` is a plain ``str``, not ``StrategyHealthStatus``.
    assert type(d["status"]) is str
