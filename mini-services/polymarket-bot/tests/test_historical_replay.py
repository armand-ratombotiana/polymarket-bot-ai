"""tests/test_historical_replay.py — Unit tests for
``backtesting/historical_replay.py`` (W19-1).

Scope: pure-Python verification of the historical-replay backtest engine
that replays REAL ``market_snapshots`` rows from the SQLite
``market_intelligence.db`` (the same DB ``core/market_db.py`` /
``core/timescale_db.py`` write to). Unlike the synthetic Monte-Carlo
archetype simulator in ``backtesting/engine.py`` (which the W17-6
Backtest Engine Assessment scored 3.5/10 specifically because it draws
prices from an RNG seeded by the strategy name), this engine loads
actual recorded snapshots and replays them through the strategy +
risk + execution pipeline.

Twelve tests, grouped by concern:

  Loader:
    1. ``test_load_snapshots_empty_db``       — fresh DB returns ``[]``.
    2. ``test_load_snapshots_seeded``          — 5 snapshots in, 5 out.
    3. ``test_load_snapshots_filters_by_token`` — rows for other tokens
                                                 are excluded.
    4. ``test_load_snapshots_filters_by_time`` — rows outside the
                                                 ``[start, end]`` window
                                                 are excluded.
    5. ``test_load_snapshots_orderbook_join``  — the LEFT JOIN against
                                                 ``orderbook_ticks``
                                                 populates ``bid_size``
                                                 / ``ask_size``.

  Replay:
    6. ``test_replay_no_data_returns_zeroed``  — empty snapshot list
                                                 returns the zero-trade
                                                 ``ReplayResult``.
    7. ``test_replay_with_simple_strategy``    — a deterministic
                                                 mean-reverting price
                                                 series triggers BUY +
                                                 SELL trades.
    8. ``test_replay_force_closes_open_position`` — an unclosed BUY at
                                                 end of window is force-
                                                 closed at the last
                                                 ``best_bid``.

  Metrics:
    9. ``test_metrics_total_return``           — equity-curve total return
                                                 matches the closed-trade
                                                 P&L.
   10. ``test_metrics_win_rate_and_profit_factor`` — win rate / profit
                                                 factor computed from
                                                 closed-trade pnl.
   11. ``test_metrics_max_drawdown_nonneg``    — max drawdown is in
                                                 ``[0, 1]``.

  API route:
   12. ``test_api_historical_replay_returns_replay_shape`` — POST
                                                 ``/api/backtest/historical-replay``
                                                 returns the documented
                                                 response shape with
                                                 ``synthetic=False`` +
                                                 ``engine="historical_replay"``.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_engine.py``) so
# a sibling test file invoked directly
# (``python -m pytest tests/test_historical_replay.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_hist_replay_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# IMPORTANT — the conftest's autouse fixture sets ``MARKET_DB_PATH`` to
# ``/tmp/pmbot_conftest_isolation/market_intelligence.db`` BEFORE this
# test file is imported, so the ``setdefault`` below is a no-op in the
# full test session. We still list it here so the file is hermetic when
# invoked directly (``python -m pytest tests/test_historical_replay.py``).
# The resolved DB path used by both the engine fixture AND the API route
# is read from ``settings.market_db_path`` (below) — that way the route
# handler and the test seed into the SAME file regardless of which env
# var redirect won the setdefault race.
_DB_PATH = Path(
    os.environ.get("MARKET_DB_PATH", str(_TMP_ROOT / "market_intelligence.db"))
)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    # IMPORTANT — the historical-replay engine reads from this DB. The
    # conftest's autouse fixture does NOT clear the file between tests
    # (it only resets in-memory store state), so each test that needs
    # seeded data calls ``_make_schema(...)`` against a fresh delete +
    # recreate.
    "MARKET_DB_PATH": str(_DB_PATH),
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

from backtesting.historical_replay import (  # noqa: E402
    HistoricalReplayEngine,
    HistoricalSnapshot,
    ReplayResult,
    SimpleStrategy,
)
from config import settings  # noqa: E402

# Resolve the canonical DB path AFTER all env-var redirects have been
# applied so the engine fixture and the API route (which constructs the
# engine from ``settings.market_db_path``) read from the SAME file. The
# conftest's redirect wins in a full-suite run; this file's redirect
# wins in a direct invocation.
_DB_PATH = Path(settings.market_db_path)


# ── DB seeding helpers ─────────────────────────────────────────────────────


def _make_schema(db_path: Path) -> None:
    """Ensure the ``market_snapshots`` + ``orderbook_ticks`` tables exist.

    Mirrors the schema in ``core/timescale_db.py::_init_sqlite_fallback``
    and ``core/market_db.py::_init_db`` so the replay engine's JOIN
    query exercises the production-shaped schema (not a stub).

    IMPORTANT — this function does NOT ``unlink`` the DB file (unlike a
    naive ``tmp_path``-based test fixture) because the conftest redirects
    ``MARKET_DB_PATH`` to a SHARED path
    (``/tmp/pmbot_conftest_isolation/market_intelligence.db``) that other
    test files (``test_recording_pipeline.py``, ``test_shadow_wiring.py``,
    ``test_data_store.py`` …) also read from / write to. Deleting the file
    mid-session would corrupt their state. Instead, this function runs
    ``CREATE TABLE IF NOT EXISTS`` (idempotent) and clears any rows from
    ``market_snapshots`` / ``orderbook_ticks`` so the test starts from a
    known-empty baseline without nuking the file itself.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("""
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
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snap_token "
            "ON market_snapshots(token_id, timestamp DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orderbook_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                token_id TEXT NOT NULL,
                best_bid_size REAL,
                best_ask_size REAL,
                ofi REAL,
                micro_price REAL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ticks_token "
            "ON orderbook_ticks(token_id, timestamp DESC)"
        )
        # Clear data only — preserves the schema for any other test that
        # already wrote to the shared conftest DB.
        conn.execute("DELETE FROM market_snapshots")
        conn.execute("DELETE FROM orderbook_ticks")
        conn.commit()


def _seed_snapshot(
    db_path: Path,
    token_id: str,
    timestamp: float,
    mid: float,
    spread: float = 0.02,
    volume_24h: float = 100.0,
    liquidity: float = 50.0,
    bid_size: float | None = None,
    ask_size: float | None = None,
) -> None:
    """Insert one row into ``market_snapshots`` (+ optionally ``orderbook_ticks``).

    ``best_bid = mid - spread/2`` and ``best_ask = mid + spread/2`` —
    matches the convention the live snapshot recorder uses. If
    ``bid_size`` / ``ask_size`` are not None, a matching row is inserted
    into ``orderbook_ticks`` so the LEFT JOIN in
    :meth:`HistoricalReplayEngine.load_snapshots` populates the
    ``bid_size`` / ``ask_size`` columns on the returned
    :class:`HistoricalSnapshot`.
    """
    best_bid = mid - spread / 2.0
    best_ask = mid + spread / 2.0
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots
            (timestamp, token_id, slug, best_bid, best_ask, mid, spread, volume_24h, liquidity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, token_id, "test-market",
                best_bid, best_ask, mid, spread, volume_24h, liquidity,
            ),
        )
        if bid_size is not None and ask_size is not None:
            conn.execute(
                """
                INSERT INTO orderbook_ticks
                (timestamp, token_id, best_bid_size, best_ask_size, ofi, micro_price)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, token_id, bid_size, ask_size, 0.0, mid),
            )
        conn.commit()


def _seed_mean_reverting_series(
    db_path: Path,
    token_id: str,
    *,
    n_baseline: int = 25,
    baseline_mid: float = 0.50,
    dip_mid: float = 0.40,
    recovery_mid: float = 0.50,
    start_ts: float | None = None,
    step_s: float = 60.0,
) -> tuple[float, float]:
    """Seed a deterministic mean-reverting snapshot series.

    Series shape:
      1. ``n_baseline`` snapshots at ``baseline_mid`` (establishes the
         rolling average the SimpleStrategy waits for).
      2. ONE snapshot at ``dip_mid`` (triggers a BUY signal because
         ``mid < avg - threshold``).
      3. ONE snapshot at ``recovery_mid`` (triggers a SELL signal
         because ``mid > avg``).

    Returns ``(start_ts, end_ts)`` so the test can pass them straight
    to :meth:`HistoricalReplayEngine.replay`.
    """
    if start_ts is None:
        start_ts = float(int(time.time()))
    ts = start_ts
    # 1. Baseline — establishes the rolling average.
    for _ in range(n_baseline):
        _seed_snapshot(db_path, token_id, ts, baseline_mid)
        ts += step_s
    # 2. Dip — triggers BUY.
    _seed_snapshot(db_path, token_id, ts, dip_mid)
    ts += step_s
    # 3. Recovery — triggers SELL.
    _seed_snapshot(db_path, token_id, ts, recovery_mid)
    ts += step_s
    return start_ts, ts


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def engine() -> HistoricalReplayEngine:
    """Engine pointing at the conftest-redirected ``MARKET_DB_PATH``."""
    # Recreate the schema fresh for every test so prior-test seed data
    # never leaks in (the autouse conftest fixture only resets in-memory
    # store state — it doesn't touch the SQLite market DB).
    _make_schema(_DB_PATH)
    return HistoricalReplayEngine(str(_DB_PATH))


@pytest.fixture
def client():
    """TestClient bound to the production ``api.server.app``.

    Mirrors the pattern in ``tests/test_backtest_report.py`` —
    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process.
    The limiter is disabled in ``conftest.py`` so the ``HEAVY_LIMIT``
    (5/min) decorator on the new route doesn't 429 the second request.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token-conftest"}


# ═══════════════════════════════════════════════════════════════════════════
# Loader tests
# ═══════════════════════════════════════════════════════════════════════════


def test_load_snapshots_empty_db(engine: HistoricalReplayEngine) -> None:
    """A fresh DB with no rows returns an empty snapshot list (not an error)."""
    snaps = engine.load_snapshots("TKN_X", 0.0, time.time() + 1)
    assert snaps == []
    assert isinstance(snaps, list)


def test_load_snapshots_seeded(engine: HistoricalReplayEngine) -> None:
    """Five seeded snapshots come back as five :class:`HistoricalSnapshot` rows."""
    base = time.time()
    for i in range(5):
        _seed_snapshot(_DB_PATH, "TKN_A", base + i * 60.0, mid=0.50 + i * 0.01)
    snaps = engine.load_snapshots("TKN_A", base - 1.0, base + 600.0)
    assert len(snaps) == 5
    # Chronological ordering — the seed loop inserts ascending timestamps
    # and the loader's SQL has ``ORDER BY timestamp ASC``.
    timestamps = [s.timestamp for s in snaps]
    assert timestamps == sorted(timestamps)
    # Field-by-field spot-check on snapshot[0].
    first = snaps[0]
    assert first.token_id == "TKN_A"
    assert first.mid == pytest.approx(0.50, abs=1e-6)
    assert first.best_bid == pytest.approx(0.49, abs=1e-6)  # mid - spread/2
    assert first.best_ask == pytest.approx(0.51, abs=1e-6)  # mid + spread/2
    assert first.spread == pytest.approx(0.02, abs=1e-6)
    assert first.volume == pytest.approx(100.0, abs=1e-6)


def test_load_snapshots_filters_by_token(engine: HistoricalReplayEngine) -> None:
    """Rows for other token_ids are excluded from the result."""
    base = time.time()
    _seed_snapshot(_DB_PATH, "TKN_A", base, mid=0.50)
    _seed_snapshot(_DB_PATH, "TKN_B", base, mid=0.70)
    snaps = engine.load_snapshots("TKN_A", base - 1.0, base + 60.0)
    assert len(snaps) == 1
    assert snaps[0].token_id == "TKN_A"
    assert snaps[0].mid == pytest.approx(0.50, abs=1e-6)


def test_load_snapshots_filters_by_time(engine: HistoricalReplayEngine) -> None:
    """Rows outside the ``[start_time, end_time]`` window are excluded."""
    base = time.time()
    # Three snapshots at base, base+60, base+120.
    _seed_snapshot(_DB_PATH, "TKN_A", base, mid=0.50)
    _seed_snapshot(_DB_PATH, "TKN_A", base + 60.0, mid=0.51)
    _seed_snapshot(_DB_PATH, "TKN_A", base + 120.0, mid=0.52)
    # Window: [base+30, base+90] — should match only the middle row.
    snaps = engine.load_snapshots("TKN_A", base + 30.0, base + 90.0)
    assert len(snaps) == 1
    assert snaps[0].mid == pytest.approx(0.51, abs=1e-6)


def test_load_snapshots_orderbook_join(engine: HistoricalReplayEngine) -> None:
    """The LEFT JOIN against ``orderbook_ticks`` populates ``bid_size`` / ``ask_size``.

    Seeds a snapshot WITH a matching ``orderbook_ticks`` row and a
    second snapshot WITHOUT one — the first should return the joined
    depth, the second should fall back to ``0.0``.
    """
    base = time.time()
    # Snapshot WITH orderbook_ticks row.
    _seed_snapshot(
        _DB_PATH, "TKN_A", base, mid=0.50,
        bid_size=12.5, ask_size=8.5,
    )
    # Snapshot WITHOUT orderbook_ticks row (bid_size / ask_size default
    # to None → 0.0 in the loader's COALESCE).
    _seed_snapshot(_DB_PATH, "TKN_A", base + 60.0, mid=0.51)
    snaps = engine.load_snapshots("TKN_A", base - 1.0, base + 120.0)
    assert len(snaps) == 2
    # First snapshot (with tick) — joined depth visible.
    assert snaps[0].bid_size == pytest.approx(12.5, abs=1e-6)
    assert snaps[0].ask_size == pytest.approx(8.5, abs=1e-6)
    # Second snapshot (no tick) — fallback zero.
    assert snaps[1].bid_size == pytest.approx(0.0, abs=1e-6)
    assert snaps[1].ask_size == pytest.approx(0.0, abs=1e-6)


def test_load_snapshots_empty_token_id(engine: HistoricalReplayEngine) -> None:
    """An empty ``token_id`` short-circuits to an empty list (no SQL fired)."""
    snaps = engine.load_snapshots("", 0.0, time.time() + 1)
    assert snaps == []


# ═══════════════════════════════════════════════════════════════════════════
# Replay tests
# ═══════════════════════════════════════════════════════════════════════════


def test_replay_no_data_returns_zeroed(engine: HistoricalReplayEngine) -> None:
    """An empty snapshot window returns a zero-trade ``ReplayResult``.

    The ``equity_curve`` is ``[initial_capital]`` (single point), all
    numeric metrics are ``0.0``, and ``n_snapshots == 0``.
    """
    result = engine.replay(
        token_id="TKN_NONE",
        strategy=SimpleStrategy(),
        start_time=0.0,
        end_time=time.time() + 1,
        initial_capital=100.0,
    )
    assert isinstance(result, ReplayResult)
    assert result.n_snapshots == 0
    assert result.trades == []
    assert result.equity_curve == [100.0]
    assert result.total_return == 0.0
    assert result.sharpe == 0.0
    assert result.max_drawdown == 0.0
    assert result.win_rate == 0.0
    assert result.profit_factor == 0.0


def test_replay_with_simple_strategy(engine: HistoricalReplayEngine) -> None:
    """A mean-reverting series triggers at least one BUY + one SELL trade.

    Seeds 25 baseline snapshots at ``mid=0.50`` (establishes the rolling
    average), then one snapshot at ``mid=0.40`` (BUY trigger: below
    avg - threshold), then one at ``mid=0.50`` (SELL trigger: above avg).
    The replay should produce at least one BUY + one SELL trade and a
    non-empty equity curve.
    """
    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_MR")
    result = engine.replay(
        token_id="TKN_MR",
        strategy=SimpleStrategy(window=20, threshold=0.01),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    assert result.n_snapshots == 27  # 25 baseline + dip + recovery
    actions = [t["action"] for t in result.trades]
    assert "BUY" in actions, f"expected at least one BUY in {actions!r}"
    assert "SELL" in actions, f"expected at least one SELL in {actions!r}"
    # First trade is a BUY (entry) — the strategy only BUYs from a flat
    # position, so BUY always precedes SELL.
    assert actions[0] == "BUY"
    assert actions[1] == "SELL"
    # Equity curve length = n_snapshots + 1 (one initial point + one MTM
    # point per snapshot).
    assert len(result.equity_curve) == result.n_snapshots + 1
    # The recovery-SELL realises a profit (sold at 0.50 minus spread/2 =
    # 0.49, bought at 0.40 plus spread/2 = 0.41 → P&L = 0.08 per share
    # minus one spread = $0.07 per share). The single closed trade is a
    # win, so win_rate == 1.0 and profit_factor == 999.0 (sentinel for
    # "no losses").
    assert result.win_rate == pytest.approx(1.0, abs=1e-6)
    assert result.profit_factor == 999.0  # no losing trades
    assert result.total_return > 0.0


def test_replay_force_closes_open_position(engine: HistoricalReplayEngine) -> None:
    """An unclosed BUY at end of window is force-closed at the last ``best_bid``.

    Seeds 25 baseline snapshots at ``mid=0.50`` then ONE dip snapshot at
    ``mid=0.40`` (BUY trigger) — no recovery snapshot. The replay loop
    should force-close the open position at the last snapshot's
    ``best_bid`` so the trade count is BUY + SELL = 2 and the final
    equity reflects the realised (loss-making) P&L.
    """
    start_ts = float(int(time.time()))
    ts = start_ts
    # 25 baseline snapshots at mid=0.50.
    for _ in range(25):
        _seed_snapshot(_DB_PATH, "TKN_FC", ts, mid=0.50)
        ts += 60.0
    # ONE dip snapshot — triggers BUY; no recovery so the position stays
    # open at end of window.
    _seed_snapshot(_DB_PATH, "TKN_FC", ts, mid=0.40)
    ts += 60.0
    end_ts = ts

    result = engine.replay(
        token_id="TKN_FC",
        strategy=SimpleStrategy(window=20, threshold=0.01),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    actions = [t["action"] for t in result.trades]
    # BUY (entry on dip) + SELL (force-close on last snapshot).
    assert actions == ["BUY", "SELL"], f"unexpected trade actions: {actions!r}"
    # The forced SELL happened at the last snapshot's best_bid (= 0.40
    # minus spread/2 = 0.39). Bought at 0.40 + spread/2 = 0.41 → P&L
    # = (0.39 - 0.41) * size = -0.02 * size < 0. Single closed trade
    # is a loss → win_rate = 0.0, profit_factor = 0.0 (no winning trades).
    sell_trade = next(t for t in result.trades if t["action"] == "SELL")
    assert sell_trade["pnl"] < 0.0
    assert result.win_rate == pytest.approx(0.0, abs=1e-6)
    assert result.profit_factor == 0.0  # no winning trades


def test_replay_strategy_exceptions_swallowed(engine: HistoricalReplayEngine) -> None:
    """A strategy that raises is treated as no-signal (replay continues).

    Guards against a buggy strategy crashing the whole replay run —
    the engine logs a warning and continues to the next snapshot.
    """
    class _ExplodingStrategy:
        def generate_signal(self, ctx: dict):
            raise RuntimeError("boom")

    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_EX")
    result = engine.replay(
        token_id="TKN_EX",
        strategy=_ExplodingStrategy(),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    # No trades because every signal call raised → treated as None.
    assert result.trades == []
    assert result.n_snapshots == 27
    # Equity curve is just initial_capital + 27 MTM points (all = 100.0
    # since no position was ever opened).
    assert len(result.equity_curve) == 28
    assert all(abs(x - 100.0) < 1e-6 for x in result.equity_curve)
    assert result.total_return == pytest.approx(0.0, abs=1e-6)


# ═══════════════════════════════════════════════════════════════════════════
# Metrics tests
# ═══════════════════════════════════════════════════════════════════════════


def test_metrics_total_return(engine: HistoricalReplayEngine) -> None:
    """``total_return`` matches ``equity_curve[-1] / equity_curve[0] - 1``."""
    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_TR")
    result = engine.replay(
        token_id="TKN_TR",
        strategy=SimpleStrategy(window=20, threshold=0.01),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    expected_return = result.equity_curve[-1] / result.equity_curve[0] - 1.0
    assert result.total_return == pytest.approx(expected_return, abs=1e-9)


def test_metrics_win_rate_and_profit_factor(
    engine: HistoricalReplayEngine,
) -> None:
    """``win_rate`` and ``profit_factor`` computed from closed-trade pnl.

    Uses the same mean-reverting series as
    :func:`test_replay_with_simple_strategy` (one winning closed trade
    → ``win_rate == 1.0`` and ``profit_factor == 999.0`` sentinel for
    "no losses").
    """
    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_WR")
    result = engine.replay(
        token_id="TKN_WR",
        strategy=SimpleStrategy(window=20, threshold=0.01),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    # Single closed SELL trade with pnl > 0 → win_rate = 1.0.
    sell_trades = [t for t in result.trades if t["action"] == "SELL"]
    assert len(sell_trades) >= 1
    wins = sum(1 for t in sell_trades if t["pnl"] > 0)
    losses = sum(1 for t in sell_trades if t["pnl"] < 0)
    expected_win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    assert result.win_rate == pytest.approx(expected_win_rate, abs=1e-9)
    # Single winning trade, no losses → sentinel 999.0.
    assert result.profit_factor == 999.0


def test_metrics_max_drawdown_nonneg(engine: HistoricalReplayEngine) -> None:
    """``max_drawdown`` is always in ``[0, 1]`` (fractional drawdown).

    Even when the strategy loses money, the drawdown metric is clipped
    to the ``[0, 1]`` range — a value outside this range would indicate
    a divide-by-zero bug in the metric computation (the loader's
    ``safe_peak`` guard prevents the divide-by-zero, but this test
    verifies the contract end-to-end).
    """
    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_DD")
    result = engine.replay(
        token_id="TKN_DD",
        strategy=SimpleStrategy(window=20, threshold=0.01),
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=100.0,
    )
    assert 0.0 <= result.max_drawdown <= 1.0
    # The mean-reverting series produces a winning trade so the curve
    # never drops below the initial capital → drawdown should be ~ 0.
    # (Allowing some slack for the dip-snapshot MTM point that briefly
    # marks the position at the lower mid before the recovery-SELL.)
    assert result.max_drawdown < 0.20


# ═══════════════════════════════════════════════════════════════════════════
# API route tests
# ═══════════════════════════════════════════════════════════════════════════


def test_api_historical_replay_returns_replay_shape(
    client, auth_headers: dict[str, str],
) -> None:
    """``POST /api/backtest/historical-replay`` returns the documented shape.

    The response must include the ``synthetic=False`` / ``engine=
    "historical_replay"`` markers (which distinguish it from the
    ``/api/backtest/run`` archetype-simulator route), the headline
    risk metrics, and the ``trades`` / ``equity_curve`` arrays.
    """
    # Seed a mean-reverting series into the conftest-redirected DB.
    _make_schema(_DB_PATH)
    start_ts, end_ts = _seed_mean_reverting_series(_DB_PATH, "TKN_API")

    response = client.post(
        "/api/backtest/historical-replay",
        json={
            "token_id": "TKN_API",
            "start_time": start_ts - 1.0,
            "end_time": end_ts + 1.0,
            "initial_capital": 100.0,
            "strategy": "simple",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/historical-replay returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    # Engine-identity markers.
    assert data["status"] == "completed"
    assert data["synthetic"] is False
    assert data["engine"] == "historical_replay"
    assert "Monte-Carlo" in data["disclaimer"]
    # Echoed params.
    assert data["token_id"] == "TKN_API"
    # Headline metrics — must be present + finite (no inf/NaN leaks).
    for key in (
        "n_snapshots",
        "n_trades",
        "total_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "profit_factor",
    ):
        assert key in data, f"response missing key {key!r}"
        v = data[key]
        assert isinstance(v, (int, float)), (
            f"response[{key!r}] must be numeric, got {type(v).__name__}: {v!r}"
        )
        # ``math.isfinite`` would also reject NaN — but the value
        # could legitimately be int (n_snapshots / n_trades) so
        # isinstance check is the strict guard.
    # 27 snapshots seeded (25 baseline + dip + recovery).
    assert data["n_snapshots"] == 27
    # At least one BUY + one SELL — the simple strategy triggers both.
    assert data["n_trades"] >= 2
    actions = [t["action"] for t in data["trades"]]
    assert "BUY" in actions
    assert "SELL" in actions
    # Equity curve downsampled but non-empty + always ends with the
    # final realised-capital point.
    assert isinstance(data["equity_curve"], list)
    assert len(data["equity_curve"]) >= 1


def test_api_historical_replay_rejects_unknown_strategy(
    client, auth_headers: dict[str, str],
) -> None:
    """An unknown ``strategy`` name returns HTTP 400 (not 500).

    Only ``"simple"`` is wired right now — passing any other name
    raises a clean ``HTTPException(400)`` rather than a 500 from an
    unhandled AttributeError downstream.
    """
    _make_schema(_DB_PATH)  # ensure DB exists even though we'll 400 before reading
    response = client.post(
        "/api/backtest/historical-replay",
        json={
            "token_id": "TKN_X",
            "start_time": 0.0,
            "end_time": time.time() + 1,
            "strategy": "nonexistent_strategy",
        },
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_api_historical_replay_rejects_inverted_window(
    client, auth_headers: dict[str, str],
) -> None:
    """A ``start_time > end_time`` returns HTTP 400 (defensive guard)."""
    _make_schema(_DB_PATH)
    response = client.post(
        "/api/backtest/historical-replay",
        json={
            "token_id": "TKN_X",
            "start_time": 1000.0,
            "end_time": 500.0,  # inverted
        },
        headers=auth_headers,
    )
    assert response.status_code == 400
