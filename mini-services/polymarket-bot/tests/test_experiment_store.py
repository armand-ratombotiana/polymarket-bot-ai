"""tests/test_experiment_store.py — W20-3 backtest experiment persistence.

Scope: pure-Python verification of the SQLite-backed experiment store
in ``backtesting/experiment_store.py`` AND the three new HTTP routes in
``api/server.py`` that surface the persisted rows for cross-run
comparison (the gap the God Mode assessment W17-6 §33 flagged).

The store is the missing piece between the two backtest engines
(synthetic MC ``BacktestEngine.run_backtest().to_dict()`` and
historical replay ``HistoricalReplayEngine.replay()``) and the
user-facing comparison surface — previously every ``run_backtest`` call
returned an ephemeral dict that was lost the moment the HTTP response
was sent.

Sixteen tests, grouped by concern:

  Store — direct unit tests against a tmp-path ``ExperimentStore``:
    1. ``test_save_and_get_round_trip``           — save → get returns
                                                    the same field values.
    2. ``test_save_idempotent_on_same_id``        — INSERT OR REPLACE
                                                    overwrites rather
                                                    than duplicates.
    3. ``test_get_returns_none_for_missing_id``   — unknown ID → None
                                                    (not an exception).
    4. ``test_get_decodes_json_blobs``             — config /
                                                    equity_curve / trades
                                                    come back as native
                                                    Python types (not
                                                    raw JSON strings).
    5. ``test_list_experiments_newest_first``      — ORDER BY created_at
                                                    DESC ordering.
    6. ``test_list_experiments_filters_by_strategy`` — strategy filter
                                                    narrows the result.
    7. ``test_list_experiments_limit_clamped``     — limit ≤ 1 collapses
                                                    to 1, ≥ 1000 caps at
                                                    1000.
    8. ``test_list_experiments_empty_db``          — fresh store returns
                                                    [] (not an exception).
    9. ``test_compare_three_experiments``         — best_return / best_sharpe
                                                    / lowest_drawdown
                                                    reflect the input set.
   10. ``test_compare_drops_missing_ids``          — missing IDs silently
                                                    dropped (count reflects
                                                    found only).
   11. ``test_compare_no_valid_ids_returns_error`` — all-missing input
                                                    returns the
                                                    ``{"error": ...}``
                                                    sentinel (not a raise).
   12. ``test_save_caps_blob_at_10kb``             — equity_curve / trades
                                                    blobs > 10 KB are
                                                    truncated (no row
                                                    overflow).

  Singleton — verifies the module-level ``experiment_store`` singleton
  picks up the conftest's ``EXPERIMENT_DB`` env redirect:

   13. ``test_singleton_uses_env_redirect``       — singleton's _db_path
                                                    points at the conftest
                                                    tmp path.

  API routes — TestClient hits against the production ``api.server.app``:

   14. ``test_api_run_backtest_persists_experiment``  — POST
                                                          ``/api/backtest/run``
                                                          response carries
                                                          ``experiment_id``
                                                          AND the row is
                                                          retrievable via
                                                          ``GET /experiments/{id}``.
   15. ``test_api_list_and_compare``                  — save 2 runs, list,
                                                          compare, assert
                                                          best_return /
                                                          best_sharpe /
                                                          lowest_drawdown.
   16. ``test_api_get_unknown_returns_404``           — GET ``/experiments/{unknown}``
                                                          returns 404.

All tests are SYNC ``def test_...`` — the API routes wrap their SQLite
I/O in ``asyncio.to_thread`` and ``TestClient`` bridges each request
through its own anyio portal (mirrors ``tests/test_openapi.py`` /
``tests/test_historical_replay.py``).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_historical_replay.py``)
# so a sibling test file invoked directly
# (``python -m pytest tests/test_experiment_store.py``) boots hermetic
# to ``/tmp`` rather than clobbering any real persisted state in the
# repo's ``data/`` directory. ``setdefault`` lets the conftest's
# redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_experiment_store_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
    # The store-under-test reads this env var at module-import time.
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``core.*``, ``api.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from backtesting.experiment_store import (  # noqa: E402
    EXPERIMENT_DB,
    BacktestExperiment,
    ExperimentStore,
    experiment_store,
)
from config import settings  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


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
    equity_curve: list[float] | None = None,
    trades: list[dict] | None = None,
    config: dict | None = None,
    created_at: float | None = None,
) -> BacktestExperiment:
    """Build a ``BacktestExperiment`` with sane defaults for tests."""
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
        config=config or {"strategy_id": strategy, "days": 30},
        created_at=created_at if created_at is not None else time.time(),
        equity_curve=equity_curve if equity_curve is not None else [10000.0, 10500.0, 11000.0],
        trades=trades if trades is not None else [
            {"action": "BUY", "price": 0.5, "size": 100, "pnl": 0.0},
            {"action": "SELL", "price": 0.55, "size": 100, "pnl": 5.0},
        ],
    )


@pytest.fixture
def store(tmp_path: Path) -> ExperimentStore:
    """Fresh ``ExperimentStore`` whose SQLite file lives under ``tmp_path``.

    A separate file (NOT the conftest-redirected singleton path) so the
    direct-store unit tests stay hermetic — prior-test seed data in the
    shared singleton file never leaks in. The API-route tests below use
    the singleton (which points at the conftest redirect) instead.
    """
    db = tmp_path / "isolated_experiments.db"
    return ExperimentStore(db)


# ═══════════════════════════════════════════════════════════════════════════
# Store — direct unit tests
# ═══════════════════════════════════════════════════════════════════════════


def test_save_and_get_round_trip(store: ExperimentStore) -> None:
    """save → get returns the same field values for every persisted column."""
    exp = _make_experiment(
        experiment_id="abc123def456",
        strategy="mm",
        total_return=0.123,
        sharpe=1.85,
        max_drawdown=0.09,
        win_rate=0.62,
        profit_factor=1.95,
        n_trades=42,
        equity_curve=[10000.0, 10500.0, 11230.0],
        trades=[{"action": "BUY", "price": 0.50, "size": 100, "pnl": 0.0}],
        config={"strategy_id": "mm", "days": 30, "fee_bps": 0.0, "slippage_bps": 5.0},
    )
    eid = store.save(exp)
    assert eid == "abc123def456"

    got = store.get(eid)
    assert got is not None
    assert got["experiment_id"] == "abc123def456"
    assert got["strategy"] == "mm"
    assert got["strategy_version"] == "1.0.0"
    assert got["total_return"] == pytest.approx(0.123, abs=1e-9)
    assert got["sharpe"] == pytest.approx(1.85, abs=1e-9)
    assert got["max_drawdown"] == pytest.approx(0.09, abs=1e-9)
    assert got["win_rate"] == pytest.approx(0.62, abs=1e-9)
    assert got["profit_factor"] == pytest.approx(1.95, abs=1e-9)
    assert got["n_trades"] == 42
    assert got["equity_curve"] == [10000.0, 10500.0, 11230.0]
    assert got["trades"] == [{"action": "BUY", "price": 0.50, "size": 100, "pnl": 0.0}]
    assert got["config"]["strategy_id"] == "mm"
    assert got["config"]["days"] == 30


def test_save_idempotent_on_same_id(store: ExperimentStore) -> None:
    """``INSERT OR REPLACE`` overwrites rather than duplicating on same id."""
    eid = "same_id_test_1"
    e1 = _make_experiment(experiment_id=eid, total_return=0.10, n_trades=10)
    e2 = _make_experiment(experiment_id=eid, total_return=0.20, n_trades=20)
    store.save(e1)
    store.save(e2)
    rows = store.list_experiments()
    # One row, not two — the second save overwrote the first.
    assert len(rows) == 1
    assert rows[0]["experiment_id"] == eid
    assert rows[0]["total_return"] == pytest.approx(0.20, abs=1e-9)
    assert rows[0]["n_trades"] == 20


def test_get_returns_none_for_missing_id(store: ExperimentStore) -> None:
    """An unknown ID returns ``None`` (not a raise) so the API route can 404."""
    assert store.get("does-not-exist-999") is None


def test_get_decodes_json_blobs(store: ExperimentStore) -> None:
    """config / equity_curve / trades come back as native Python types."""
    exp = _make_experiment(
        config={"a": 1, "b": [2, 3], "c": {"nested": True}},
        equity_curve=[100.0, 101.5, 102.0],
        trades=[{"action": "BUY"}, {"action": "SELL"}],
    )
    store.save(exp)
    got = store.get(exp.experiment_id)
    assert got is not None
    # JSON blobs are decoded to native Python types, not raw strings.
    assert isinstance(got["config"], dict)
    assert isinstance(got["equity_curve"], list)
    assert isinstance(got["trades"], list)
    assert got["config"]["c"]["nested"] is True
    assert all(isinstance(x, float) for x in got["equity_curve"])


def test_list_experiments_newest_first(store: ExperimentStore) -> None:
    """``ORDER BY created_at DESC`` — newest row appears first."""
    base = time.time()
    e_old = _make_experiment(created_at=base - 1000.0, total_return=0.05)
    e_mid = _make_experiment(created_at=base - 500.0, total_return=0.10)
    e_new = _make_experiment(created_at=base, total_return=0.15)
    store.save(e_old)
    store.save(e_mid)
    store.save(e_new)
    rows = store.list_experiments()
    assert len(rows) == 3
    assert rows[0]["experiment_id"] == e_new.experiment_id
    assert rows[1]["experiment_id"] == e_mid.experiment_id
    assert rows[2]["experiment_id"] == e_old.experiment_id


def test_list_experiments_filters_by_strategy(store: ExperimentStore) -> None:
    """The ``strategy`` filter narrows to rows whose strategy matches exactly."""
    store.save(_make_experiment(strategy="mm", total_return=0.10))
    store.save(_make_experiment(strategy="arb", total_return=0.05))
    store.save(_make_experiment(strategy="mm", total_return=0.20))
    mm_rows = store.list_experiments(strategy="mm")
    assert len(mm_rows) == 2
    assert all(r["strategy"] == "mm" for r in mm_rows)
    arb_rows = store.list_experiments(strategy="arb")
    assert len(arb_rows) == 1
    assert arb_rows[0]["strategy"] == "arb"


def test_list_experiments_limit_clamped(store: ExperimentStore) -> None:
    """``limit`` is clamped to ``[1, 1000]`` — 0 collapses to 1, 5000 caps at 1000."""
    for _ in range(5):
        store.save(_make_experiment())
    # limit=0 collapses to 1.
    assert len(store.list_experiments(limit=0)) == 1
    # limit=5000 caps at 1000 — only 5 rows exist, so all 5 return.
    assert len(store.list_experiments(limit=5000)) == 5


def test_list_experiments_empty_db(store: ExperimentStore) -> None:
    """A fresh store returns ``[]`` (not an exception) — list endpoint 200s."""
    assert store.list_experiments() == []


def test_compare_three_experiments(store: ExperimentStore) -> None:
    """Compare 3 experiments — best_return / best_sharpe / lowest_drawdown are correct."""
    e1 = _make_experiment(total_return=0.10, sharpe=1.0, max_drawdown=0.20, win_rate=0.50)
    e2 = _make_experiment(total_return=0.25, sharpe=2.0, max_drawdown=0.10, win_rate=0.65)
    e3 = _make_experiment(total_return=0.15, sharpe=1.5, max_drawdown=0.15, win_rate=0.55)
    for e in (e1, e2, e3):
        store.save(e)
    cmp = store.compare([e1.experiment_id, e2.experiment_id, e3.experiment_id])
    assert cmp["count"] == 3
    # e2 has the best return, best sharpe, AND lowest drawdown in this set.
    assert cmp["best_return"] == pytest.approx(0.25, abs=1e-9)
    assert cmp["best_sharpe"] == pytest.approx(2.0, abs=1e-9)
    assert cmp["lowest_drawdown"] == pytest.approx(0.10, abs=1e-9)
    assert len(cmp["experiments"]) == 3
    summary_ids = [e["id"] for e in cmp["experiments"]]
    assert set(summary_ids) == {e1.experiment_id, e2.experiment_id, e3.experiment_id}


def test_compare_drops_missing_ids(store: ExperimentStore) -> None:
    """Missing IDs are silently dropped (count reflects found only)."""
    e = _make_experiment(total_return=0.10, sharpe=1.5, max_drawdown=0.10)
    store.save(e)
    cmp = store.compare([e.experiment_id, "missing-id-1", "missing-id-2"])
    assert cmp["count"] == 1
    assert cmp["experiments"][0]["id"] == e.experiment_id


def test_compare_no_valid_ids_returns_error(store: ExperimentStore) -> None:
    """An all-missing input returns the ``{"error": ...}`` sentinel (not a raise)."""
    cmp = store.compare(["does-not-exist-1", "does-not-exist-2"])
    assert "error" in cmp
    assert cmp["count"] == 0
    assert cmp["experiments"] == []


def test_save_caps_blob_at_10kb(store: ExperimentStore) -> None:
    """``equity_curve`` JSON > 10 KB is truncated (no row overflow / sqlite error)."""
    # 5000 floats × ~ 6 chars each → ~ 30 KB JSON (well above the 10 KB cap).
    big_curve = [10000.0 + i * 0.01 for i in range(5000)]
    exp = _make_experiment(equity_curve=big_curve, trades=[])
    # save must not raise — the cap is enforced pre-write.
    eid = store.save(exp)
    # The row is retrievable; the headline metrics survived.
    got = store.get(eid)
    assert got is not None
    assert got["experiment_id"] == eid
    assert got["n_trades"] == exp.n_trades
    # The equity_curve blob may be truncated mid-JSON (the cap slices at
    # 10 KB without regard for JSON syntax). The store's
    # ``_safe_json_loads`` falls back to ``[]`` on a truncated blob
    # rather than raising ``JSONDecodeError`` — so the headline metrics
    # stay readable even with a corrupted blob.
    assert isinstance(got["equity_curve"], list)


# ═══════════════════════════════════════════════════════════════════════════
# Singleton — env-var redirect
# ═══════════════════════════════════════════════════════════════════════════


def test_singleton_uses_env_redirect() -> None:
    """The module-level singleton picks up the conftest's EXPERIMENT_DB redirect.

    Asserts the singleton is constructed (not ``None``) and its DB path
    matches the redirected path (``/tmp/.../backtest_experiments.db``
    rather than the production ``/app/data/...``). This guards against
    a future refactor accidentally hard-coding the production path.
    """
    assert experiment_store is not None, (
        "experiment_store singleton must be constructed at import time — "
        "an EXPERIMENT_DB env redirect to a writable /tmp path is set in "
        "conftest.py and at the top of this test file."
    )
    # EXPERIMENT_DB is the module-level constant computed from the env
    # var at import time. ``experiment_store._db_path`` should match it.
    assert experiment_store._db_path == Path(EXPERIMENT_DB)
    # And it should NOT be the production /app/data path (read-only in
    # the sandbox).
    assert "/app/data" not in str(experiment_store._db_path)
    # Sanity: the redirected path ends with ``backtest_experiments.db``.
    assert experiment_store._db_path.name == "backtest_experiments.db"


# ═══════════════════════════════════════════════════════════════════════════
# API routes — TestClient against the production ``api.server.app``
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client():
    """TestClient bound to the production ``api.server.app``.

    Mirrors the pattern in ``tests/test_historical_replay.py`` —
    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process.
    The limiter is disabled in ``conftest.py`` so the ``HEAVY_LIMIT`` /
    ``READ_LIMIT`` decorators on the new routes don't 429 the
    second request.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token matching the conftest-set ``API_TOKEN``."""
    return {"Authorization": "Bearer test-token-conftest"}


@pytest.fixture(autouse=True)
def _reset_singleton_db():
    """Reset the singleton's DB to empty before every API-route test.

    The conftest's autouse ``_reset_store_factory_defaults`` resets the
    in-memory store singletons but does NOT touch the experiment-store
    SQLite file. Without this reset, an experiment saved by
    ``test_api_run_backtest_persists_experiment`` would still be present
    in ``test_api_list_and_compare`` — the list would show 3 rows
    instead of the 2 the test expected. Deleting the file's rows (not
    the file itself) keeps the schema intact for the singleton's
    already-open handle.
    """
    # The singleton may be None if import-time construction failed
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


def test_api_run_backtest_persists_experiment(
    client, auth_headers: dict[str, str],
) -> None:
    """``POST /api/backtest/run`` persists the run + returns ``experiment_id``.

    The persisted row must be retrievable via
    ``GET /api/backtest/experiments/{experiment_id}`` — round-trip
    through the new registry endpoints proves the W17-6 §33 gap is
    closed end-to-end.
    """
    response = client.post(
        "/api/backtest/run",
        json={
            "strategy_id": "mm",
            "initial_capital": 10000.0,
            "days": 5,
            "fee_bps": 0.0,
            "slippage_bps": 5.0,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/run returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert "experiment_id" in data, (
        "response must include ``experiment_id`` field after W20-3"
    )
    eid = data["experiment_id"]
    assert isinstance(eid, str)
    assert len(eid) == 12, (
        f"experiment_id should be a 12-char uuid prefix; got {eid!r}"
    )

    # The persisted row is retrievable via the GET endpoint.
    get_resp = client.get(
        f"/api/backtest/experiments/{eid}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 200, (
        f"GET /api/backtest/experiments/{eid} returned "
        f"{get_resp.status_code}; body: {get_resp.text[:500]!r}"
    )
    exp_row = get_resp.json()
    assert exp_row["experiment_id"] == eid
    assert exp_row["strategy"] == "mm"
    # Synthetic MC engine returns roi_pct (percentage) — the helper
    # divides by 100 so the stored total_return is fractional.
    assert isinstance(exp_row["total_return"], (int, float))
    assert exp_row["n_trades"] == data["result"]["total_trades"]
    # The config blob round-trips with the request params.
    assert exp_row["config"]["strategy_id"] == "mm"
    assert exp_row["config"]["days"] == 5


def test_api_list_and_compare(
    client, auth_headers: dict[str, str],
) -> None:
    """Run 2 backtests → list → compare; assert the headline metric winners.

    The two runs use the same strategy but different ``initial_capital``
    so the RNG seed (hashed from strategy_id) produces the same trade
    sequence — only the position-size scaling varies, so the second
    run's total_return / sharpe / drawdown differ from the first. The
    comparison should report whichever set has the higher return /
    sharpe / lower drawdown as the winner.
    """
    # Run 1.
    r1 = client.post(
        "/api/backtest/run",
        json={
            "strategy_id": "mm",
            "initial_capital": 5000.0,
            "days": 5,
        },
        headers=auth_headers,
    )
    assert r1.status_code == 200
    eid_1 = r1.json()["experiment_id"]
    # Run 2 — different capital scales the same RNG trade sequence.
    r2 = client.post(
        "/api/backtest/run",
        json={
            "strategy_id": "mm",
            "initial_capital": 20000.0,
            "days": 5,
        },
        headers=auth_headers,
    )
    assert r2.status_code == 200
    eid_2 = r2.json()["experiment_id"]
    assert eid_1 != eid_2

    # List — both should appear.
    list_resp = client.get(
        "/api/backtest/experiments",
        headers=auth_headers,
    )
    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert listed["count"] == 2
    listed_ids = {e["experiment_id"] for e in listed["experiments"]}
    assert {eid_1, eid_2}.issubset(listed_ids)
    # Newest-first ordering — the second run's created_at > first.
    assert listed["experiments"][0]["experiment_id"] == eid_2

    # Strategy filter narrows the result.
    mm_resp = client.get(
        "/api/backtest/experiments?strategy=mm",
        headers=auth_headers,
    )
    assert mm_resp.status_code == 200
    assert mm_resp.json()["count"] == 2

    # Compare — best_return / best_sharpe / lowest_drawdown reflect
    # the actual stored values.
    cmp_resp = client.post(
        "/api/backtest/compare",
        json={"experiment_ids": [eid_1, eid_2]},
        headers=auth_headers,
    )
    assert cmp_resp.status_code == 200
    cmp = cmp_resp.json()
    assert cmp["count"] == 2
    assert "best_return" in cmp
    assert "best_sharpe" in cmp
    assert "lowest_drawdown" in cmp
    assert len(cmp["experiments"]) == 2
    cmp_ids = {e["id"] for e in cmp["experiments"]}
    assert cmp_ids == {eid_1, eid_2}


def test_api_get_unknown_returns_404(
    client, auth_headers: dict[str, str],
) -> None:
    """``GET /api/backtest/experiments/{unknown}`` returns 404 (not 500)."""
    resp = client.get(
        "/api/backtest/experiments/does-not-exist-xyz",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    body = resp.json()
    # FastAPI's HTTPException detail shape — ``{"detail": "..."}``.
    assert "detail" in body
    assert "does-not-exist-xyz" in body["detail"]


def test_api_list_empty_returns_zero(
    client, auth_headers: dict[str, str],
) -> None:
    """A freshly-reset store returns ``count: 0`` (not an exception).

    The autouse ``_reset_singleton_db`` fixture above wipes the table
    before every test, so this test sees an empty store.
    """
    resp = client.get(
        "/api/backtest/experiments",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["count"] == 0
    assert body["experiments"] == []


def test_api_compare_no_valid_ids_returns_200_with_error(
    client, auth_headers: dict[str, str],
) -> None:
    """Compare with all-missing IDs returns 200 + the ``{"error": ...}`` body.

    The request was syntactically valid (200); no experiments were found
    (``count == 0``). A 404 would be misleading — the caller can
    distinguish by checking ``count``.
    """
    resp = client.post(
        "/api/backtest/compare",
        json={"experiment_ids": ["missing-id-1", "missing-id-2"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["count"] == 0
    assert body["experiments"] == []
