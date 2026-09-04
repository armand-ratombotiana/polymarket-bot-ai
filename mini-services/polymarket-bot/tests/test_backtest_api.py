"""tests/test_backtest_api.py — W20-1 walk-forward CV + Monte Carlo API routes.

Scope: end-to-end HTTP verification of the two ``/api/backtest/*`` routes
added by W20-1 in ``api/server.py``. Both routes delegate to the pure-
Python helpers in ``backtesting/advanced.py``
(``walk_forward_analysis`` / ``monte_carlo_simulation``) which already
have full unit-test coverage in ``tests/test_advanced_backtest.py``
(12 tests). This file verifies the HTTP surface — request shaping,
response shaping, error-handling, and param overrides — that the unit
suite doesn't reach.

Eight tests, grouped by concern:

  Walk-forward route (``POST /api/backtest/walk-forward``):
    1. ``test_walk_forward_returns_200_synthetic_fallback`` — empty body
         + empty ``ml_feature_store`` → 200 with the synthetic-fallback
         payload (``source`` in the synthetic_* set, ``n_windows >= 1``).
    2. ``test_walk_forward_response_shape`` — response carries every
         documented field (``n_windows`` / ``mean_auc`` / ``std_auc`` /
         ``mean_brier`` / ``sharpe_ratio`` / ``sortino_ratio`` /
         ``calmar_ratio`` / ``max_drawdown`` / ``windows``).
    3. ``test_walk_forward_with_real_features`` — seed the
         ``ml_feature_store`` table with 1500 labelled feature rows,
         call the route → 200 with ``source == "real"``.
    4. ``test_walk_forward_param_override`` — custom
         ``train_window`` / ``test_window`` / ``step`` echoed through
         to the result (``n_windows`` reflects the smaller windows).

  Monte-carlo route (``POST /api/backtest/monte-carlo``):
    5. ``test_monte_carlo_returns_200_no_positions`` — empty body +
         empty ``closed_positions`` table → 200 with ``error`` field
         (no positions to simulate from).
    6. ``test_monte_carlo_response_shape`` — when positions ARE
         seeded, the response carries every documented field
         (``n_simulations`` / ``expected_return`` / ``worst_case`` /
         ``best_case`` / ``probability_of_ruin`` / ``percentiles``).
    7. ``test_monte_carlo_with_seeded_positions`` — seed 5 deterministic
         closed positions, call the route → 200 with
         ``n_simulations == 100`` (custom) and ``n_returns >= 5``.
    8. ``test_monte_carlo_param_override`` — pass ``n_simulations=100``
         / ``initial_capital=200.0`` / ``ruin_threshold=0.7`` → response
         ``n_simulations`` matches.

The conftest redirects ``MARKET_DB_PATH`` / ``CLOSED_POSITIONS_DB_PATH``
to ``/tmp/pmbot_conftest_isolation/...`` so the route's
``SQLITE_FALLBACK_PATH`` import (resolved at module-load time) and the
``closed_positions`` singleton (also constructed at import time) both
read from the same writable path the tests seed. The shared limiter is
disabled by ``conftest.py`` so the ``HEAVY_LIMIT`` (5/min) decorator on
both routes doesn't 429 the second request in this module.

Tests are SYNC ``def test_...`` — ``TestClient`` bridges each request
into the async route handlers (mirrors
``tests/test_backtest_report.py``).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_report.py``) so
# a sibling test file invoked directly
# (``python -m pytest tests/test_backtest_api.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_backtest_api_tests")
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
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
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


# ── Shared fixtures ─────────────────────────────────────────────────────────
# Bearer token the conftest sets up (via ``API_TOKEN=test-token-conftest``).
_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_backtest_report.py``.

    The limiter is disabled in ``conftest.py`` so the ``HEAVY_LIMIT``
    (5/min) decorator on the two new routes doesn't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


# ── Helpers ────────────────────────────────────────────────────────────────


def _market_db_path() -> Path:
    """Resolve the SQLite path the route will read from.

    The route imports ``SQLITE_FALLBACK_PATH`` from
    ``core.timescale_db`` at route-handler invocation time; that constant
    is computed at module-load time as
    ``Path(os.environ.get("MARKET_DB_PATH", ...))``. Because conftest
    sets ``MARKET_DB_PATH`` BEFORE ``core.timescale_db`` is imported
    (via the env-redirect block at the top of this file plus the
    conftest's own redirect), the singleton's ``_sqlite_path`` and the
    constant ``SQLITE_FALLBACK_PATH`` both point at the conftest's
    shared /tmp path.
    """
    from core.timescale_db import SQLITE_FALLBACK_PATH

    return Path(SQLITE_FALLBACK_PATH)


def _closed_positions_db_path() -> Path:
    """Resolve the SQLite path the ``closed_positions`` singleton reads from."""
    from core.closed_positions import DB_PATH

    return Path(DB_PATH)


def _ensure_ml_feature_store(db_path: Path) -> None:
    """Make sure the ``ml_feature_store`` table exists in ``db_path``.

    The table is normally created by ``timescale_db._init_db`` at
    singleton construction time. But if the singleton was constructed
    against a different DB path (e.g. the conftest ran first and
    resolved ``MARKET_DB_PATH`` to a different value), the table might
    be missing in OUR redirected path. This helper is idempotent —
    ``CREATE TABLE IF NOT EXISTS`` is a no-op if the table already
    exists.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_feature_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                token_id TEXT NOT NULL,
                features_json TEXT NOT NULL,
                p_pred REAL,
                confidence REAL,
                outcome_resolved INTEGER DEFAULT NULL
            )
        """)
        conn.commit()


def _ensure_closed_positions_schema(db_path: Path) -> None:
    """Make sure the ``closed_positions`` table exists in ``db_path``.

    Idempotent — the schema mirrors the production one in
    ``core/closed_positions.py::ClosedPositionsStore._init_db``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       REAL    NOT NULL,
                position_id     TEXT    NOT NULL UNIQUE,
                token_id        TEXT    NOT NULL,
                strategy        TEXT,
                entry_price     REAL,
                exit_price      REAL,
                shares          REAL,
                pnl             REAL    DEFAULT 0.0,
                holding_seconds REAL    DEFAULT 0.0,
                model_version   TEXT,
                decision_id     TEXT,
                direction       TEXT,
                confidence      REAL,
                predicted_edge  REAL,
                p_yes           REAL,
                market_mid      REAL,
                liquidity       REAL,
                metadata_json   TEXT
            )
        """)
        conn.commit()


def _seed_labelled_features(
    db_path: Path, n: int = 1500, base_ts: float = 1_700_000_000.0
) -> int:
    """Insert ``n`` labelled feature rows into ``ml_feature_store``.

    Features are 38-element arrays (matches ``N_FEATURES`` in
    ``ml/features.py``) of small uniform floats so the
    ``RandomForestClassifier`` can learn a non-trivial signal without
    overfitting — gives the walk-forward routine a real AUC > 0.5 to
    report.

    Returns the number of rows actually inserted (may be < ``n`` if
    some inserts hit the unique constraint, though we use unique
    ``token_id`` values so this shouldn't happen).
    """
    from ml.features import N_FEATURES

    _ensure_ml_feature_store(db_path)
    rng = _deterministic_rng(seed=42)
    inserted = 0
    with sqlite3.connect(db_path) as conn:
        for i in range(n):
            # Logistic-regression-flavored target so the model can
            # actually learn signal (mirrors the synthetic-dataset
            # fixture in tests/test_advanced_backtest.py).
            feats = [rng.uniform(-1.0, 1.0) for _ in range(N_FEATURES)]
            log_odds = 2.0 * feats[0] + 1.0 * feats[1] - 0.5 * feats[2]
            prob = 1.0 / (1.0 + math.exp(-log_odds))
            label = 1 if rng.uniform(0.0, 1.0) < prob else 0
            feats_json = json.dumps([float(x) for x in feats])
            conn.execute(
                "INSERT INTO ml_feature_store "
                "(timestamp, token_id, features_json, p_pred, confidence, outcome_resolved) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    base_ts + float(i),
                    f"w20-tok-{i}",
                    feats_json,
                    prob,
                    0.1,
                    label,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def _seed_closed_positions(
    db_path: Path,
    n: int = 5,
    base_ts: float = 1_800_000_000.0,
    position_id_prefix: str = "w20-pos",
) -> int:
    """Insert ``n`` deterministic closed-position rows.

    Each row has a positive ROI (entry=0.50, exit=0.55, shares=10,
    pnl=+0.50 → ROI = 0.50 / (0.50 * 10) = +0.10) so the
    ``monte_carlo_simulation`` produces a positive expected_return
    we can assert on.
    """
    _ensure_closed_positions_schema(db_path)
    inserted = 0
    with sqlite3.connect(db_path) as conn:
        for i in range(n):
            conn.execute(
                """
                INSERT OR IGNORE INTO closed_positions
                (timestamp, position_id, token_id, strategy,
                 entry_price, exit_price, shares, pnl,
                 holding_seconds, model_version, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    base_ts + float(i),
                    f"{position_id_prefix}-{i}",
                    f"w20-tok-{i}",
                    "w20_test_strategy",
                    0.50,           # entry_price
                    0.55,           # exit_price
                    10.0,           # shares
                    0.50,           # pnl (positive → winning trade)
                    3600.0,         # holding_seconds
                    "w20-test-v1",  # model_version
                    "BUY",          # direction
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def _deterministic_rng(seed: int):
    """Tiny LCG so we don't pull in numpy here (keeps the helper light).

    Returns a small RNG facade exposing ``.uniform(a, b)`` and
    ``.random()`` so callers can drop-in replace ``random.Random`` while
    keeping the deterministic LCG stream (no dependency on numpy).
    """
    return _LCGRng(seed)


class _LCGRng:
    """Minimal ``random.Random``-like facade over the LCG stream."""

    def __init__(self, seed: int):
        self._state = seed

    def _next(self) -> float:
        self._state = (1103515245 * self._state + 12345) & 0x7FFFFFFF
        return self._state / 0x7FFFFFFF

    def random(self) -> float:
        return self._next()

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self._next()


def _clear_ml_feature_store(db_path: Path) -> None:
    """Delete all rows from ``ml_feature_store`` (preserves schema)."""
    _ensure_ml_feature_store(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM ml_feature_store")
        conn.commit()


def _clear_closed_positions(db_path: Path) -> None:
    """Delete all rows from ``closed_positions`` (preserves schema).

    Used by the "no positions" monte-carlo test to guarantee the
    route sees an empty trade history regardless of any rows seeded by
    sibling test modules sharing the conftest's redirected DB.
    """
    _ensure_closed_positions_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM closed_positions")
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Walk-forward route
# ═══════════════════════════════════════════════════════════════════════════


# ── (1) Walk-forward returns 200 with synthetic fallback ────────────────────
def test_walk_forward_returns_200_synthetic_fallback(client, auth_headers) -> None:
    """``POST /api/backtest/walk-forward`` returns 200 even when the
    ``ml_feature_store`` table is empty — the route falls back to
    ``ml.model._synthetic_training_data`` so the response always
    carries a non-trivial ``n_windows >= 1`` rather than 500-ing."""
    _clear_ml_feature_store(_market_db_path())

    response = client.post(
        "/api/backtest/walk-forward",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/walk-forward returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    # Synthetic fallback path sets ``source`` to one of the
    # ``synthetic_*`` markers (the exact value depends on whether the
    # ``ml_feature_store`` table existed at all — both branches land in
    # the synthetic fallback in this test because we cleared all rows).
    assert data.get("source", "").startswith("synthetic"), (
        f"expected source to start with 'synthetic' on empty feature store, "
        f"got {data.get('source')!r}"
    )
    # Walk-forward on 3000 synthetic samples with the default
    # train=1000 / test=200 / step=200 windows should produce
    # floor((3000 - 1000 - 200) / 200) + 1 = 10 windows.
    assert data["n_windows"] >= 1, (
        f"expected n_windows >= 1 on synthetic fallback, got {data['n_windows']}"
    )


# ── (2) Walk-forward response shape ─────────────────────────────────────────
def test_walk_forward_response_shape(client, auth_headers) -> None:
    """Response carries every documented field with the correct type:
    ``source`` (str), ``n_samples`` (int), ``n_windows`` (int),
    ``mean_auc`` (float), ``std_auc`` (float), ``mean_brier`` (float),
    ``sharpe_ratio`` (float), ``sortino_ratio`` (float),
    ``calmar_ratio`` (float), ``max_drawdown`` (float),
    ``windows`` (list[dict])."""
    _clear_ml_feature_store(_market_db_path())

    response = client.post(
        "/api/backtest/walk-forward",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    # Every documented top-level field is present.
    expected_keys = {
        "source", "n_samples", "n_windows", "mean_auc", "std_auc",
        "mean_brier", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
        "max_drawdown", "windows",
    }
    assert expected_keys.issubset(data.keys()), (
        f"missing keys: {expected_keys - set(data.keys())}"
    )

    # Type + finite-float checks (catches inf / NaN leaks into JSON).
    assert isinstance(data["source"], str)
    assert isinstance(data["n_samples"], int)
    assert isinstance(data["n_windows"], int)
    assert isinstance(data["windows"], list)
    assert len(data["windows"]) <= 20, (
        f"windows list should be capped at 20 entries, got {len(data['windows'])}"
    )
    for key in (
        "mean_auc", "std_auc", "mean_brier",
        "sharpe_ratio", "sortino_ratio", "calmar_ratio", "max_drawdown",
    ):
        v = data[key]
        assert isinstance(v, (int, float)), (
            f"{key!r} must be numeric, got {type(v).__name__}: {v!r}"
        )
        assert math.isfinite(v), f"{key!r} is non-finite: {v!r}"

    # AUC is bounded in [0, 1]; Brier is bounded in [0, 0.25] on the
    # synthetic dataset (the model isn't learning anything real, so
    # Brier hovers near the 0.25 always-predict-0.5 baseline).
    assert 0.0 <= data["mean_auc"] <= 1.0
    assert 0.0 <= data["mean_brier"] <= 0.25 + 1e-6

    # If any per-window entries are returned, verify their shape too.
    if data["windows"]:
        w = data["windows"][0]
        expected_window_keys = {
            "window", "train_start", "train_end", "test_start",
            "test_end", "n_train", "n_test", "auc", "brier",
            "mean_prediction", "actual_positive_rate",
        }
        assert expected_window_keys.issubset(w.keys()), (
            f"window dict missing keys: {expected_window_keys - set(w.keys())}"
        )


# ── (3) Walk-forward with real labelled features ───────────────────────────
def test_walk_forward_with_real_features(client, auth_headers) -> None:
    """When the ``ml_feature_store`` table has >= 1200 labelled rows
    (the default ``train_window + test_window = 1000 + 200``), the
    route uses them and reports ``source == "real"``.

    Seeds 1500 rows so the default windows produce multiple folds and
    the response's ``n_windows`` is non-trivial (floor((1500 - 1000 -
    200) / 200) + 1 = 2 windows).
    """
    db = _market_db_path()
    _clear_ml_feature_store(db)
    n_seeded = _seed_labelled_features(db, n=1500)
    assert n_seeded == 1500

    response = client.post(
        "/api/backtest/walk-forward",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    assert data["source"] == "real", (
        f"expected source='real' with 1500 labelled rows, "
        f"got {data['source']!r}"
    )
    # n_samples should reflect the seeded count (capped at 5000 by the
    # LIMIT in the route's SELECT).
    assert data["n_samples"] == 1500, (
        f"expected n_samples=1500, got {data['n_samples']}"
    )
    # Expected window count: floor((1500 - 1000 - 200) / 200) + 1 = 2.
    assert data["n_windows"] == 2, (
        f"expected 2 windows on 1500 samples with default params, "
        f"got {data['n_windows']}"
    )
    # Logistic-regression-flavored features → AUC should beat random.
    assert data["mean_auc"] > 0.5, (
        f"mean_auc {data['mean_auc']:.4f} should beat random (0.5) on "
        f"the learnable seeded dataset"
    )

    # Cleanup so sibling tests don't see our seeded rows.
    _clear_ml_feature_store(db)


# ── (4) Walk-forward param override ────────────────────────────────────────
def test_walk_forward_param_override(client, auth_headers) -> None:
    """Custom ``train_window`` / ``test_window`` / ``step`` params are
    threaded through to ``walk_forward_analysis`` — the response's
    ``n_windows`` reflects the smaller windows.

    With 1500 seeded rows + ``train=500`` / ``test=100`` / ``step=100``:
    floor((1500 - 500 - 100) / 100) + 1 = 10 windows.
    """
    db = _market_db_path()
    _clear_ml_feature_store(db)
    _seed_labelled_features(db, n=1500)

    response = client.post(
        "/api/backtest/walk-forward",
        json={
            "train_window": 500,
            "test_window": 100,
            "step": 100,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    assert data["source"] == "real"
    assert data["n_windows"] == 10, (
        f"expected 10 windows with train=500/test=100/step=100 on 1500 "
        f"samples, got {data['n_windows']}"
    )
    # Each surviving window's n_train / n_test should reflect the
    # overridden params.
    for w in data["windows"]:
        assert w["n_train"] == 500
        assert w["n_test"] == 100

    _clear_ml_feature_store(db)


# ═══════════════════════════════════════════════════════════════════════════
# Monte-carlo route
# ═══════════════════════════════════════════════════════════════════════════


# ── (5) Monte-carlo returns 200 with no positions ──────────────────────────
def test_monte_carlo_returns_200_no_positions(client, auth_headers) -> None:
    """``POST /api/backtest/monte-carlo`` returns 200 (NOT 500) when the
    ``closed_positions`` table is empty — the route reports an
    ``error`` string and ``n_simulations == 0`` so a caller can
    distinguish "no data yet" from a real server error."""
    db = _closed_positions_db_path()
    _clear_closed_positions(db)

    response = client.post(
        "/api/backtest/monte-carlo",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/monte-carlo returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert "error" in data, (
        f"expected 'error' field on empty positions, got keys {list(data.keys())}"
    )
    assert data["n_simulations"] == 0


# ── (6) Monte-carlo response shape with seeded positions ───────────────────
def test_monte_carlo_response_shape(client, auth_headers) -> None:
    """When positions ARE seeded, the response carries every documented
    field with the correct type: ``n_simulations`` (int),
    ``expected_return`` (float), ``worst_case`` (float),
    ``best_case`` (float), ``probability_of_ruin`` (float),
    ``percentiles`` (dict with p5/p25/p50/p75/p95 keys)."""
    db = _closed_positions_db_path()
    _clear_closed_positions(db)
    _seed_closed_positions(db, n=5)

    response = client.post(
        "/api/backtest/monte-carlo",
        json={"n_simulations": 100},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    expected_keys = {
        "n_simulations", "n_positions", "n_returns",
        "expected_return", "worst_case", "best_case",
        "probability_of_ruin", "percentiles",
    }
    assert expected_keys.issubset(data.keys()), (
        f"missing keys: {expected_keys - set(data.keys())}"
    )

    assert isinstance(data["n_simulations"], int)
    assert isinstance(data["n_positions"], int)
    assert isinstance(data["n_returns"], int)
    assert isinstance(data["percentiles"], dict)

    # Finite-float spot checks.
    for key in (
        "expected_return", "worst_case", "best_case", "probability_of_ruin",
    ):
        v = data[key]
        assert isinstance(v, (int, float)), (
            f"{key!r} must be numeric, got {type(v).__name__}"
        )
        assert math.isfinite(v), f"{key!r} is non-finite: {v!r}"

    # Percentile dict shape.
    p = data["percentiles"]
    assert set(p.keys()) == {"p5", "p25", "p50", "p75", "p95"}, (
        f"unexpected percentile keys: {sorted(p.keys())}"
    )
    # Monotonic ordering.
    assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"], (
        f"percentiles not monotonically ordered: {p}"
    )
    # Probability of ruin is in [0, 1].
    assert 0.0 <= data["probability_of_ruin"] <= 1.0

    _clear_closed_positions(db)


# ── (7) Monte-carlo with seeded positions ───────────────────────────────────
def test_monte_carlo_with_seeded_positions(client, auth_headers) -> None:
    """Seed 5 deterministic winning closed positions (each with ROI =
    +0.10), call the route with ``n_simulations=100`` → response has
    ``n_simulations == 100``, ``n_returns >= 5``, and a positive
    ``expected_return`` (every simulation compounds the same +10% per
    trade, so the distribution collapses to a single point)."""
    db = _closed_positions_db_path()
    _clear_closed_positions(db)
    _seed_closed_positions(db, n=5)

    response = client.post(
        "/api/backtest/monte-carlo",
        json={"n_simulations": 100, "initial_capital": 100.0},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    # No "error" key on the happy path.
    assert "error" not in data, (
        f"unexpected 'error' field on happy path: {data.get('error')!r}"
    )
    assert data["n_simulations"] == 100, (
        f"expected n_simulations=100, got {data['n_simulations']}"
    )
    assert data["n_returns"] >= 5, (
        f"expected n_returns >= 5 (we seeded 5 positions), "
        f"got {data['n_returns']}"
    )
    # All 5 seeded positions have ROI = +0.10 (pnl=0.50, cost=5.0).
    # 100 bootstrap resamples of [0.10, 0.10, 0.10, 0.10, 0.10] each of
    # length 5 → final_return = (1.10**5) - 1 = 0.61051 in EVERY
    # simulation. So expected_return == worst_case == best_case ==
    # 0.61051 (within float tolerance).
    expected_final = (1.10 ** 5) - 1.0
    assert math.isclose(data["expected_return"], expected_final, abs_tol=1e-6), (
        f"expected_return {data['expected_return']:.6f} should equal "
        f"(1.10**5)-1 = {expected_final:.6f} on the all-winning seeded dataset"
    )
    assert math.isclose(data["worst_case"], expected_final, abs_tol=1e-6)
    assert math.isclose(data["best_case"], expected_final, abs_tol=1e-6)
    # Every simulation compounds to +61% → never crosses the ruin
    # threshold (0.5 * 100 = 50 < 161).
    assert data["probability_of_ruin"] == 0.0

    _clear_closed_positions(db)


# ── (8) Monte-carlo param override ──────────────────────────────────────────
def test_monte_carlo_param_override(client, auth_headers) -> None:
    """Custom ``n_simulations`` / ``initial_capital`` / ``ruin_threshold``
    params are threaded through to ``monte_carlo_simulation`` — the
    response's ``n_simulations`` reflects the override.

    Uses a guaranteed-losing position series (pnl = -0.50, cost = 5.0 →
    ROI = -0.10 per trade) with ``ruin_threshold=0.95`` so every
    simulation is marked as ruin → ``probability_of_ruin == 1.0``.
    """
    db = _closed_positions_db_path()
    _clear_closed_positions(db)

    # Seed 5 LOSING positions (pnl = -0.50 → ROI = -0.10 per trade).
    with sqlite3.connect(db) as conn:
        for i in range(5):
            conn.execute(
                """
                INSERT OR IGNORE INTO closed_positions
                (timestamp, position_id, token_id, strategy,
                 entry_price, exit_price, shares, pnl,
                 holding_seconds, model_version, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1_800_001_000.0 + float(i),
                    f"w20-ruin-{i}",
                    f"w20-ruin-tok-{i}",
                    "w20_test_strategy",
                    0.50, 0.45, 10.0, -0.50,
                    3600.0, "w20-test-v1", "BUY",
                ),
            )
        conn.commit()

    response = client.post(
        "/api/backtest/monte-carlo",
        json={
            "n_simulations": 50,
            "initial_capital": 100.0,
            "ruin_threshold": 0.95,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text[:500]
    data = response.json()

    assert "error" not in data, (
        f"unexpected 'error' on losing-position series: {data.get('error')!r}"
    )
    assert data["n_simulations"] == 50, (
        f"expected n_simulations=50, got {data['n_simulations']}"
    )
    # 5 losing trades, each ROI = -0.10 → final_return = (0.90**5)-1 = -0.4095
    # → final_value = 100 * (1 + -0.4095) = 59.05 < 0.95 * 100 = 95 → ruin.
    assert data["probability_of_ruin"] == 1.0, (
        f"expected probability_of_ruin=1.0 on guaranteed-losing series, "
        f"got {data['probability_of_ruin']}"
    )

    _clear_closed_positions(db)
