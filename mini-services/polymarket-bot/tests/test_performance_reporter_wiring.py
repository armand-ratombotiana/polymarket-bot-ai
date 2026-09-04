"""tests/test_performance_reporter_wiring.py — W25-6 wiring tests.

Scope: end-to-end HTTP verification of the three ``/api/performance/*``
routes in ``api/server.py`` that surface the honest per-category
performance breakdown the W24-5 reporter produces.

The reporter itself (``core/performance_reporter.PerformanceReporter``)
has full unit-test coverage in ``tests/test_performance_reporter.py``
(19 tests including two API-route smoke tests against
``GET /api/performance/report`` and ``GET /api/performance/paper``).
This file widens the API-route coverage with five wiring-focused tests:

  1. ``test_api_performance_report_returns_all_categories`` — the
     report payload carries every category key (paper_trading /
     backtest / walk_forward / live / disclaimer) AND the disclaimer
     string is the honest-reporting reminder.
  2. ``test_api_performance_paper_returns_metrics_with_ci`` — the
     paper-trading metrics dict includes the 95% Wilson-score CI
     (``win_rate_ci_95``) plus the raw win_rate / n_trades / p_value
     that downstream dashboards (the React ``AnalyticsPanel``)
     render into the per-category KPI strip.
  3. ``test_api_performance_backtest_returns_summary_when_empty`` — on
     a fresh deployment with no backtest experiments yet, the route
     returns the empty-state payload
     (``{"category": "backtest", "n_experiments": 0, "message": ...}``)
     so the dashboard renders the empty state rather than crashing.
  4. ``test_api_performance_backtest_returns_best_experiment_summary`` —
     after seeding the experiment store with two runs (different
     ``total_return`` values), the route returns the higher one as
     ``best_return`` plus the matching ``best_sharpe`` / ``best_strategy``
     AND the cardinal-sin disclaimer that backtest performance does
     NOT guarantee future results.
  5. ``test_api_performance_report_disclaimer_present_and_honest`` —
     the disclaimer text is present, is a non-empty string, and calls
     out the cardinal sin ("backtest does NOT guarantee future") that
     the W24-5 reporter exists to prevent.

All five tests drive the production ``api.server.app`` through
``TestClient`` (so the full request → middleware → route → response
cycle is exercised) and isolate the closed_positions + experiment
stores via the same monkeypatch-on-tmp-path fixtures used by the
W24-5 unit tests.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` so a sibling test file invoked directly
# (``python -m pytest tests/test_performance_reporter_wiring.py``) boots
# hermetic to ``/tmp`` rather than clobbering any real persisted state in
# the repo's ``data/`` directory. ``setdefault`` lets the conftest's
# redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_performance_reporter_wiring_tests")
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
    # The experiment store reads EXPERIMENT_DB at import time. Without this
    # redirect the singleton would try to mkdir ``/app/data`` (read-only in
    # the sandbox) — same defensive pattern as ``tests/test_experiment_store.py``.
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``api.*``, ``backtesting.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from backtesting.experiment_store import (  # noqa: E402
    BacktestExperiment,
    experiment_store,
)


# ═══════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ═══════════════════════════════════════════════════════════════════════════


_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_backtest_report.py`` /
    ``tests/test_performance_reporter.py``.

    The limiter is disabled in ``conftest.py`` so the ``READ_LIMIT``
    (120/min) decorator on the three new routes doesn't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token matching the conftest-set ``API_TOKEN``."""
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture
def isolated_closed_positions(monkeypatch, tmp_path):
    """Patch ``core.closed_positions.closed_positions`` with a fresh
    ``ClosedPositionsStore`` whose SQLite file lives under ``tmp_path``.

    Without this fixture the closed_positions singleton would resolve
    to the conftest-redirected ``/tmp/pmbot_conftest_isolation/closed_positions.db``
    file, which is shared across every test in the session — any prior
    test that wrote a closed position would leak rows into the metrics
    computation and break the ``n_trades == 0`` assertion on an empty
    store. This fixture gives each test a hermetic DB.

    Mirrors the fixture in ``tests/test_performance_reporter.py`` so the
    two modules don't share closed_positions state.
    """
    from core.closed_positions import ClosedPositionsStore

    fresh = ClosedPositionsStore(tmp_path / "wiring_isolated.db")
    monkeypatch.setattr("core.closed_positions.closed_positions", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _reset_experiment_store():
    """Reset the experiment_store singleton's DB to empty before every test.

    The conftest's autouse ``_reset_store_factory_defaults`` resets the
    in-memory store singletons but does NOT touch the experiment-store
    SQLite file. Without this reset, an experiment saved by
    ``test_api_performance_backtest_returns_best_experiment_summary``
    would still be present when
    ``test_api_performance_backtest_returns_summary_when_empty`` runs,
    breaking the empty-store assertion. Mirrors the autouse fixture in
    ``tests/test_experiment_store.py``.
    """
    # The singleton can be ``None`` if import-time construction failed
    # (shouldn't happen under the redirected env, but be defensive).
    if experiment_store is None:
        yield
        return
    try:
        with sqlite3.connect(experiment_store._db_path) as conn:
            conn.execute("DELETE FROM experiments")
            conn.commit()
    except sqlite3.Error:
        # If the schema isn't there (first run), the DELETE fails —
        # the store's __init__ will have created the schema, so this
        # should not happen in practice, but be defensive.
        pass
    yield


def _make_experiment(
    *,
    experiment_id: str | None = None,
    strategy: str = "mm",
    total_return: float = 0.10,
    sharpe: float = 1.5,
    sortino: float = 2.0,
    calmar: float = 1.2,
    max_drawdown: float = 0.08,
    win_rate: float = 0.6,
    profit_factor: float = 1.8,
    n_trades: int = 25,
    initial_capital: float = 10000.0,
    final_equity: float = 11000.0,
) -> BacktestExperiment:
    """Build a ``BacktestExperiment`` with sane defaults for tests.

    Mirrors the helper in ``tests/test_experiment_store.py`` so the two
    modules can share the same row schema without re-defining it.
    """
    return BacktestExperiment(
        experiment_id=experiment_id or str(uuid.uuid4())[:12],
        strategy=strategy,
        strategy_version="1.0.0",
        start_time=time.time() - 3600.0,
        end_time=time.time(),
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return=total_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_factor=profit_factor,
        n_trades=n_trades,
        config={"strategy_id": strategy, "days": 30},
        created_at=time.time(),
        equity_curve=[10000.0, 10500.0, 11000.0],
        trades=[{"action": "BUY", "price": 0.5, "size": 100, "pnl": 0.0}],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — GET /api/performance/report returns all categories separately
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_performance_report_returns_all_categories(
    client, auth_headers, isolated_closed_positions
) -> None:
    """``GET /api/performance/report`` returns 200 + a JSON payload whose
    top-level keys include every category (``paper_trading`` /
    ``backtest`` / ``walk_forward`` / ``live``) AND the honest-reporting
    ``disclaimer``.

    The W24-5 reporter NEVER combines metrics across categories — each
    is reported separately with its own 95% confidence interval + p-value
    vs the 50% coin-flip null. The wiring contract: every category key
    is present in the response so the frontend ``AnalyticsPanel`` can
    render all four KPI strips without conditional logic for missing
    categories.
    """
    response = client.get(
        "/api/performance/report",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/report returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()

    # Every category must be reported SEPARATELY — no merging allowed.
    expected_keys = {
        "paper_trading",
        "backtest",
        "walk_forward",
        "live",
        "disclaimer",
    }
    actual_keys = set(data.keys())
    missing = expected_keys - actual_keys
    assert not missing, (
        f"GET /api/performance/report missing category keys: {missing}; "
        f"actual keys: {sorted(actual_keys)}"
    )

    # Paper-trading is computed inline (the canonical honest, current view)
    # so it must be a populated dict, NOT a string pointer.
    paper = data["paper_trading"]
    assert isinstance(paper, dict), (
        f"paper_trading should be a dict (computed inline); "
        f"got {type(paper).__name__}"
    )
    assert paper.get("category") == "paper", (
        f"paper_trading.category should be 'paper'; got {paper.get('category')!r}"
    )

    # Backtest / walk-forward / live are pointers to their dedicated
    # endpoints (each has its own request shape, computing them inline
    # would be wasteful). They MUST be present (string form) so the
    # frontend can show the pointer.
    for key in ("backtest", "walk_forward", "live"):
        assert isinstance(data[key], str), (
            f"{key} should be a string pointer to the dedicated endpoint; "
            f"got {type(data[key]).__name__}: {data[key]!r}"
        )
        assert len(data[key]) > 0, f"{key} pointer string should be non-empty"


# ═══════════════════════════════════════════════════════════════════════════
# Test 2 — GET /api/performance/paper returns metrics with confidence interval
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_performance_paper_returns_metrics_with_ci(
    client, auth_headers, isolated_closed_positions
) -> None:
    """``GET /api/performance/paper`` returns 200 + the full
    ``PerformanceMetrics.to_dict`` shape, including the 95% Wilson-score
    confidence interval (``win_rate_ci_95``) that downstream dashboards
    render next to the headline win rate.

    The CI is the contract that distinguishes the W24-5 honest reporter
    from the legacy ``GET /api/analytics`` rollup — a 7-of-7 win streak
    yields a 100% win rate but a CI spanning [59%, 100%] (Wilson score
    at n=7, p=1.0), which is the honest signal that the sample is too
    small to draw conclusions. Without this assertion the CI could be
    silently dropped by a future refactor and the dashboard would
    over-confidence a small-sample fluke.
    """
    response = client.get(
        "/api/performance/paper",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/paper returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert data["category"] == "paper"

    # The 95% confidence interval is the headline honesty signal — it
    # MUST be present, formatted as a string "[lower%, upper%]" so the
    # dashboard can drop it straight into a KPI tile without re-formatting.
    assert "win_rate_ci_95" in data, (
        "paper metrics missing 'win_rate_ci_95' — the 95% Wilson-score "
        "confidence interval is the W24-5 contract that distinguishes "
        "honest reporting from naive point estimates."
    )
    ci = data["win_rate_ci_95"]
    assert isinstance(ci, str), (
        f"win_rate_ci_95 should be a formatted string like "
        f"'[55.0%, 75.0%]'; got {type(ci).__name__}: {ci!r}"
    )
    # The CI string format is "[LL.L%, UU.U%]" — bracket-delimited,
    # two percentage values separated by a comma.
    assert ci.startswith("[") and ci.endswith("]"), (
        f"win_rate_ci_95 should be bracket-delimited '[LL%, UU%]'; got {ci!r}"
    )
    assert "%" in ci, (
        f"win_rate_ci_95 should contain '%'; got {ci!r}"
    )

    # Every other PerformanceMetrics field is present too — sanity-check
    # the canonical field set the frontend ``AnalyticsPanel`` depends on.
    for key in (
        "win_rate",
        "profit_factor",
        "expectancy",
        "max_drawdown",
        "sharpe_ratio",
        "sortino_ratio",
        "open_exposure",
        "capital_utilization",
        "avg_slippage_bps",
        "total_fees",
        "n_trades",
        "n_wins",
        "n_losses",
        "avg_win",
        "avg_loss",
        "avg_hold_time_hours",
        "p_value",
        "is_statistically_significant",
    ):
        assert key in data, (
            f"paper metrics missing key {key!r} — the frontend "
            f"AnalyticsPanel expects this field in the per-category KPI strip."
        )

    # On an empty store, n_trades=0 + is_statistically_significant=False
    # (the 30-trade minimum guard prevents a small-sample fluke from
    # being flagged as significant).
    assert data["n_trades"] == 0, (
        f"isolated_closed_positions fixture should yield n_trades=0; "
        f"got {data['n_trades']}"
    )
    assert data["is_statistically_significant"] is False, (
        "is_statistically_significant should be False on an empty store "
        "(the 30-trade minimum guard prevents significance at n=0)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Test 3 — GET /api/performance/backtest returns summary when empty
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_performance_backtest_returns_summary_when_empty(
    client, auth_headers
) -> None:
    """``GET /api/performance/backtest`` returns 200 + the empty-state
    payload when the experiment store has no rows.

    The empty-state contract: ``{"category": "backtest",
    "n_experiments": 0, "message": "No backtest experiments yet"}``.
    Returning a 404 here would be misleading (the request itself is
    valid; the store just hasn't been populated yet) and would crash
    the dashboard rather than rendering the empty-state UI.
    """
    response = client.get(
        "/api/performance/backtest",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/backtest returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert data["category"] == "backtest", (
        f"category should be 'backtest'; got {data.get('category')!r}"
    )
    assert data["n_experiments"] == 0, (
        f"empty experiment store should yield n_experiments=0; "
        f"got {data['n_experiments']}"
    )
    # The empty-state message is the user-facing string the dashboard
    # renders when no backtest has been run yet. It must be present and
    # non-empty so the trader sees a meaningful message rather than a
    # blank tile.
    assert "message" in data, (
        "empty-state response missing 'message' field — the dashboard "
        "needs this string to render the empty-state UI."
    )
    assert isinstance(data["message"], str) and len(data["message"]) > 0, (
        f"empty-state message should be a non-empty string; got {data['message']!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — GET /api/performance/backtest returns best-experiment summary
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_performance_backtest_returns_best_experiment_summary(
    client, auth_headers
) -> None:
    """``GET /api/performance/backtest`` returns 200 + the best-of-N
    summary when the experiment store has at least one row.

    Seeds two experiments with different ``total_return`` values, then
    asserts the route returns the higher one as ``best_return`` plus the
    matching ``best_sharpe`` / ``best_strategy``. The cardinal-sin
    ``disclaimer`` field is ALWAYS present (even with rows) — the
    reminder that backtest performance does NOT guarantee future results
    must be re-stated every time the metric is shown.
    """
    # Seed two experiments — different total_return values so the
    # ``max`` selection picks a deterministic winner.
    assert experiment_store is not None, (
        "experiment_store singleton must be constructed at import time "
        "(EXPERIMENT_DB env redirect is set at the top of this file)."
    )
    exp_lo = _make_experiment(
        strategy="mm_avellaneda_stoikov",
        total_return=0.05,
        sharpe=0.85,
    )
    exp_hi = _make_experiment(
        strategy="arb_binary_dutch_book",
        total_return=0.18,
        sharpe=1.95,
    )
    experiment_store.save(exp_lo)
    experiment_store.save(exp_hi)

    response = client.get(
        "/api/performance/backtest",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/backtest returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert data["category"] == "backtest"
    assert data["n_experiments"] == 2, (
        f"n_experiments should reflect both seeded runs (2); got "
        f"{data['n_experiments']}"
    )
    # The best experiment is the one with the higher total_return —
    # the ``max(...key=total_return)`` selection picks exp_hi.
    assert data["best_return"] == pytest.approx(0.18, abs=1e-9), (
        f"best_return should be the higher of the two seeded runs (0.18); "
        f"got {data['best_return']}"
    )
    assert data["best_sharpe"] == pytest.approx(1.95, abs=1e-9), (
        f"best_sharpe should match the winning experiment's sharpe (1.95); "
        f"got {data['best_sharpe']}"
    )
    assert data["best_strategy"] == "arb_binary_dutch_book", (
        f"best_strategy should be the winning experiment's strategy "
        f"('arb_binary_dutch_book'); got {data['best_strategy']!r}"
    )
    # The disclaimer is ALWAYS present — the cardinal-sin reminder must
    # be re-stated every time the backtest metric is shown, not only
    # when the store is empty.
    assert "disclaimer" in data, (
        "non-empty backtest response missing 'disclaimer' field — the "
        "cardinal-sin reminder (backtest ≠ future results) must be "
        "re-stated every time the metric is shown."
    )
    disclaimer = data["disclaimer"]
    assert isinstance(disclaimer, str) and len(disclaimer) > 0, (
        f"disclaimer should be a non-empty string; got {disclaimer!r}"
    )
    # The disclaimer text must call out the cardinal sin specifically.
    assert "NOT guarantee" in disclaimer, (
        f"disclaimer should warn 'backtest does NOT guarantee future "
        f"results'; got: {disclaimer!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Test 5 — GET /api/performance/report carries the disclaimer
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_performance_report_disclaimer_present_and_honest(
    client, auth_headers, isolated_closed_positions
) -> None:
    """``GET /api/performance/report`` returns 200 + a ``disclaimer``
    field that calls out the cardinal sin: backtest performance does
    NOT guarantee future results.

    The disclaimer is the entire point of the W24-5 reporter — it's
    the contract that distinguishes honest reporting from the legacy
    ``GET /api/analytics`` rollup which silently mixes backtest /
    paper / live metrics into a single misleading number. The wiring
    contract: the disclaimer is ALWAYS present (string, non-empty)
    AND mentions "NOT guarantee" so the dashboard can colour-code the
    metric by honest-reporting status.
    """
    response = client.get(
        "/api/performance/report",
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"GET /api/performance/report returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()

    # The disclaimer field must be present at the top level.
    assert "disclaimer" in data, (
        "GET /api/performance/report response missing 'disclaimer' field "
        "— the honest-reporting reminder is the entire point of the "
        "W24-5 reporter."
    )
    disclaimer = data["disclaimer"]

    # Type check — must be a non-empty string (not None / not a dict).
    assert isinstance(disclaimer, str), (
        f"disclaimer should be a string; got {type(disclaimer).__name__}"
    )
    assert len(disclaimer) > 0, "disclaimer should be a non-empty string"

    # The cardinal sin: backtest performance does NOT guarantee future
    # results. The disclaimer MUST call this out specifically — a
    # generic "performance may vary" disclaimer is NOT enough.
    assert "NOT guarantee" in disclaimer, (
        f"disclaimer should warn 'backtest does NOT guarantee future "
        f"results'; got: {disclaimer!r}"
    )

    # The disclaimer must also mention that paper / live metrics are
    # the honest signal (the "Only paper-trading and live performance
    # reflects actual system behaviour" callout). This is the second
    # half of the W24-5 contract — without it, a trader could read
    # "backtest does NOT guarantee future results" and conclude that
    # NO category is honest.
    assert (
        "paper" in disclaimer.lower() and "live" in disclaimer.lower()
    ), (
        f"disclaimer should mention both 'paper' and 'live' categories "
        f"as the honest signal; got: {disclaimer!r}"
    )
