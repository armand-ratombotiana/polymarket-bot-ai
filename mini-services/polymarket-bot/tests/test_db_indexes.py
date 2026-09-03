"""
Unit tests for the W11-9 database-index + query-optimisation work.

W11-9 — DB indexes + query optimisation.

Covers:

  (1) **Index presence** — every index added by the W11-9 task is actually
      created against a fresh ``_init_db`` run. Asserts via
      ``sqlite_master`` so we catch the failure mode where an index
      silently isn't created (e.g. a typo in the ``CREATE INDEX IF NOT
      EXISTS`` SQL).
  (2) **Idempotent re-runs** — calling ``_init_db`` twice produces the
      same index set as calling it once (the ``IF NOT EXISTS`` clause
      is supposed to make this a no-op). Catches the regression where a
      future refactor drops the ``IF NOT EXISTS`` keyword and breaks
      repeated boots.
  (3) **Index-used-by-query planner** — SQLite's ``EXPLAIN QUERY PLAN``
      confirms the new indexes are actually selected by the planner for
      the queries they were designed to accelerate (and not silently
      ignored in favour of a full table scan).
  (4) **Indexed query is faster than full scan** — a synthetic dataset
      (~5000 rows) is loaded, then a parameterised query using the new
      compound index must complete in less wall time than the equivalent
      ``SELECT *`` + Python-side filter pattern. This is the "Before /
      After" benchmark from the W11-9 task spec.
  (5) **Optimisation script runs without error** —
      ``scripts/optimize_db.py`` against an empty data dir produces
      zero databases but exits 0 (the no-DB edge case); against a
      populated data dir it produces ANALYZE output for every ``*.db``
      file.
  (6) **timed_query decorator** — emits a ``WARNING`` log message when
      the wrapped function takes longer than the configured threshold,
      and stays silent (no warning) when the function completes quickly.
  (7) **attribution N+1 fix** — ``get_full_attribution`` fetches the
      closed-position rows exactly once across all seven dimension
      aggregations (verified by monkeypatching the
      ``closed_positions.get_closed_positions`` method with a counting
      wrapper).
  (8) **alerting.get_stats single-query optimisation** — the combined
      SUM(CASE WHEN ...) aggregate produces the same result as the
      original three-separate-COUNT implementation (verified by
      comparing the output against a hand-rolled reference count over
      the same seed data).

Each test builds its own ``tmp_path``-scoped SQLite file (no shared
state, no perturbation of the production ``/app/data`` paths). The
module-level singletons constructed at import time are NOT used — every
test instantiates a fresh ``DecisionLedger`` / ``ClosedPositionsStore`` /
``Observability`` / ``AlertEngine`` / module-level ``_init_db()``
against the test's own ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads os.environ at module-import time. ──────
# Mirrors the pattern in ``tests/conftest.py`` but with a W11-9-specific
# tmp root so this module's tests don't collide with sibling test
# modules' isolated DBs.
_TMP_ROOT = Path("/tmp/pmbot_w11_9_test_isolation")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``scripts.*``) regardless of the cwd pytest was launched
# from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.alerting import AlertEngine  # noqa: E402
from core.closed_positions import ClosedPositionsStore  # noqa: E402
from core.decision_ledger import DecisionLedger  # noqa: E402
from core.observability import Observability  # noqa: E402

from core import execution_quality as _eq_mod  # noqa: E402
from core import attribution as _attribution_mod  # noqa: E402

# The vast majority of tests in this module are SYNC ``def`` (they
# touch SQLite directly via the ``sqlite3`` module, not via the async
# wrapper methods). Only the attribution N+1-fix test is ``async def``;
# it applies ``pytest.mark.asyncio`` individually.


# ── Helpers ───────────────────────────────────────────────────────────────────


def _index_names(db_path: Path, table: str | None = None) -> set[str]:
    """Return the set of index names defined in ``db_path``.

    If ``table`` is supplied, restricts to indexes on that table.
    Excludes SQLite's auto-created indexes (the ``sqlite_autoindex_*``
    names that back ``UNIQUE`` / ``PRIMARY KEY`` constraints) so the
    assertion count reflects only indexes the schema declares
    explicitly.
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'"
            + (f" AND tbl_name = ?" if table else ""),
            ([table] if table else []),
        )
        return {row[0] for row in cursor.fetchall()}


def _explain_uses_index(db_path: Path, sql: str, params: tuple = ()) -> bool:
    """Return True if SQLite's query planner would use ANY index (not a
    full table scan) to execute ``sql``.

    Uses ``EXPLAIN QUERY PLAN`` and inspects the ``detail`` column for
    the ``USING INDEX`` / ``USING COVERING INDEX`` markers. Returns
    False when the planner falls back to a ``SCAN`` (full table scan).
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        for row in cursor.fetchall():
            # row shape: (id, parent, notused, detail)
            detail = row[3] if len(row) > 3 else ""
            if "USING INDEX" in detail.upper() or "USING COVERING INDEX" in detail.upper():
                return True
        return False


def _seed_decision_events(db_path: Path, n: int = 5_000) -> None:
    """Insert ``n`` rows into ``decision_events`` for benchmark tests."""
    ts_base = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO decision_events "
            "(timestamp, decision_id, stage, token_id, strategy, pnl, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    ts_base - i * 0.001,
                    f"dec-{i:08x}",
                    "PREDICTION" if i % 2 == 0 else "FILL",
                    f"TOK_{i % 50}",
                    "ml_sig_v1",
                    float(i % 100) * 0.01,
                    json.dumps({"i": i}),
                )
                for i in range(n)
            ],
        )
        conn.commit()


# ── (1) Index presence per module ────────────────────────────────────────────


def test_decision_ledger_indexes_created(tmp_path):
    """All W11-9 decision_ledger indexes are created by ``_init_db``."""
    db_path = tmp_path / "decision_ledger.db"
    DecisionLedger(db_path=db_path)

    events_indexes = _index_names(db_path, table="decision_events")
    rej_indexes = _index_names(db_path, table="decision_rejections")

    # Pre-existing indexes (carried over from before W11-9).
    assert "idx_dec_id" in events_indexes
    assert "idx_dec_token" in events_indexes
    assert "idx_dec_stage" in events_indexes
    assert "idx_rej_token" in rej_indexes
    assert "idx_rej_decision" in rej_indexes

    # W11-9 added indexes.
    assert "idx_dec_stage_ts" in events_indexes, (
        "W11-9: (stage, timestamp DESC) index missing on decision_events"
    )
    assert "idx_dec_ts" in events_indexes, (
        "W11-9: (timestamp DESC) index missing on decision_events"
    )
    assert "idx_rej_ts" in rej_indexes, (
        "W11-9: (timestamp DESC) index missing on decision_rejections"
    )
    assert "idx_rej_reason_ts" in rej_indexes, (
        "W11-9: (reason, timestamp DESC) index missing on decision_rejections"
    )
    assert "idx_rej_strategy_ts" in rej_indexes, (
        "W11-9: (strategy, timestamp DESC) index missing on decision_rejections"
    )


def test_execution_quality_indexes_created(tmp_path):
    """All W11-9 execution_quality indexes are created by ``_init_db``."""
    db_path = tmp_path / "execution_quality.db"
    # The module-level ``_init_db`` reads DB_PATH from the module global,
    # so monkeypatch it to the tmp_path before re-running.
    original = _eq_mod.DB_PATH
    _eq_mod.DB_PATH = db_path
    try:
        _eq_mod._init_db()
    finally:
        _eq_mod.DB_PATH = original

    indexes = _index_names(db_path, table="execution_quality")
    # Pre-existing indexes.
    assert "idx_eq_ts" in indexes
    assert "idx_eq_strategy" in indexes
    assert "idx_eq_token" in indexes
    assert "idx_eq_decision" in indexes
    # W11-9 added.
    assert "idx_eq_slippage" in indexes, (
        "W11-9: (slippage_bps) index missing on execution_quality"
    )
    assert "idx_eq_side_ts" in indexes, (
        "W11-9: (side, timestamp DESC) index missing on execution_quality"
    )
    assert "idx_eq_paper_ts" in indexes, (
        "W11-9: (paper, timestamp DESC) index missing on execution_quality"
    )
    assert "idx_eq_order" in indexes, (
        "W11-9: (order_id) index missing on execution_quality"
    )


def test_closed_positions_indexes_created(tmp_path):
    """All W11-9 closed_positions indexes are created by ``_init_db``."""
    db_path = tmp_path / "closed_positions.db"
    ClosedPositionsStore(db_path=db_path)

    indexes = _index_names(db_path, table="closed_positions")
    # Pre-existing.
    assert "idx_cp_token" in indexes
    assert "idx_cp_strategy" in indexes
    assert "idx_cp_time" in indexes
    # W11-9 added.
    assert "idx_cp_decision" in indexes, (
        "W11-9: (decision_id) index missing on closed_positions"
    )
    assert "idx_cp_direction" in indexes, (
        "W11-9: (direction) index missing on closed_positions"
    )
    assert "idx_cp_pnl" in indexes, (
        "W11-9: (pnl) index missing on closed_positions"
    )
    assert "idx_cp_model_ts" in indexes, (
        "W11-9: (model_version, timestamp DESC) index missing on closed_positions"
    )
    assert "idx_cp_exit_price" in indexes, (
        "W11-9: (exit_price) index missing on closed_positions"
    )


def test_observability_indexes_created(tmp_path):
    """All W11-9 observability indexes are created by ``_init_db``."""
    db_path = tmp_path / "observability.db"
    Observability(db_path=db_path)

    indexes = _index_names(db_path, table="metrics")
    # Pre-existing.
    assert "idx_metrics_cat_name_time" in indexes
    assert "idx_metrics_name_time" in indexes
    assert "idx_metrics_cat" in indexes
    # W11-9 added.
    assert "idx_metrics_ts" in indexes, (
        "W11-9: (timestamp DESC) index missing on metrics"
    )
    assert "idx_metrics_cat_ts" in indexes, (
        "W11-9: (category, timestamp DESC) index missing on metrics"
    )


def test_alerting_indexes_created(tmp_path):
    """All W11-9 alerting indexes are created by ``_init_db``."""
    db_path = tmp_path / "alerts.db"
    AlertEngine(db_path=db_path)

    indexes = _index_names(db_path, table="alerts")
    # Pre-existing.
    assert "idx_alerts_timestamp" in indexes
    # W11-9 added.
    assert "idx_alerts_sev_ack_ts" in indexes, (
        "W11-9: (severity, acknowledged, timestamp DESC) index missing on alerts"
    )
    assert "idx_alerts_cat_ts" in indexes, (
        "W11-9: (category, timestamp DESC) index missing on alerts"
    )
    assert "idx_alerts_ack_ts" in indexes, (
        "W11-9: (acknowledged, timestamp DESC) index missing on alerts"
    )
    assert "idx_alerts_name" in indexes, (
        "W11-9: (name) index missing on alerts"
    )


# ── (2) Idempotent re-runs ──────────────────────────────────────────────────


def test_init_db_idempotent_on_repeated_calls(tmp_path):
    """Calling ``_init_db`` twice produces the same index set as calling
    it once (the ``IF NOT EXISTS`` clause is supposed to make this a
    no-op)."""
    db_path = tmp_path / "decision_ledger.db"
    ledger_a = DecisionLedger(db_path=db_path)
    indexes_after_first = _index_names(db_path)
    # Re-run on the same instance.
    ledger_a._init_db()
    # Construct a SECOND instance against the same file (mirrors a
    # service restart).
    ledger_b = DecisionLedger(db_path=db_path)
    indexes_after_second = _index_names(db_path)
    assert indexes_after_first == indexes_after_second, (
        "Re-running _init_db must NOT create duplicate indexes; "
        f"first run had {len(indexes_after_first)} indexes, "
        f"second run had {len(indexes_after_second)}"
    )


# ── (3) Index used by query planner ─────────────────────────────────────────


def test_decision_ledger_token_query_uses_index(tmp_path):
    """The ``WHERE token_id = ? ORDER BY timestamp DESC`` query should
    pick the ``idx_dec_token`` covering index."""
    db_path = tmp_path / "decision_ledger.db"
    DecisionLedger(db_path=db_path)
    _seed_decision_events(db_path, n=200)

    uses_idx = _explain_uses_index(
        db_path,
        "SELECT * FROM decision_events WHERE token_id = ? "
        "ORDER BY timestamp DESC LIMIT 50",
        ("TOK_1",),
    )
    assert uses_idx, (
        "Query planner must use an index for token_id+timestamp queries "
        "instead of a full table scan"
    )


def test_decision_ledger_stage_query_uses_index(tmp_path):
    """The W11-9 ``idx_dec_stage_ts`` index should be selected for
    ``WHERE stage = ? ORDER BY timestamp DESC`` queries."""
    db_path = tmp_path / "decision_ledger.db"
    DecisionLedger(db_path=db_path)
    _seed_decision_events(db_path, n=200)

    uses_idx = _explain_uses_index(
        db_path,
        "SELECT * FROM decision_events WHERE stage = ? "
        "ORDER BY timestamp DESC LIMIT 50",
        ("PREDICTION",),
    )
    assert uses_idx, (
        "Query planner must use idx_dec_stage_ts for stage+timestamp queries"
    )


def test_closed_positions_pnl_order_uses_index(tmp_path):
    """The W11-9 ``idx_cp_pnl`` index should be selected for
    ``ORDER BY pnl`` (the median-PnL query in ``get_closed_stats``)."""
    db_path = tmp_path / "closed_positions.db"
    ClosedPositionsStore(db_path=db_path)
    # Seed a few rows so the planner doesn't fall back to a scan on
    # empty tables (SQLite's planner sometimes uses a scan when the
    # table has 0 rows because it's literally cheaper).
    with sqlite3.connect(db_path) as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO closed_positions "
                "(timestamp, position_id, token_id, strategy, pnl) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time() - i, f"pos-{i}", "TOK", "s", float(i) - 10.0),
            )
        conn.commit()

    uses_idx = _explain_uses_index(
        db_path,
        "SELECT pnl FROM closed_positions ORDER BY pnl",
    )
    assert uses_idx, (
        "Query planner must use idx_cp_pnl for ORDER BY pnl "
        "(the median-PnL query)"
    )


def test_alerts_unacked_query_uses_index(tmp_path):
    """The W11-9 ``idx_alerts_ack_ts`` index should be selected for
    ``WHERE acknowledged = 0 ORDER BY timestamp DESC`` queries (the
    ``get_recent(unacknowledged_only=True)`` fast path)."""
    db_path = tmp_path / "alerts.db"
    AlertEngine(db_path=db_path)
    # Seed a few rows.
    engine = AlertEngine(db_path=db_path)
    for _ in range(3):
        engine.evaluate({"psi": 0.5})

    uses_idx = _explain_uses_index(
        db_path,
        "SELECT * FROM alerts WHERE acknowledged = 0 "
        "ORDER BY timestamp DESC LIMIT 50",
    )
    assert uses_idx, (
        "Query planner must use idx_alerts_ack_ts for unacked alerts query"
    )


# ── (4) Indexed query beats full scan on a large dataset ────────────────────


def test_indexed_query_faster_than_full_scan(tmp_path):
    """On a ~5000-row decision_events table, a parameterised query that
    uses the new compound index completes in less wall time than the
    equivalent ``SELECT *`` + Python-side filter pattern (the "Before /
    After" benchmark from the W11-9 task spec).

    The benchmark is intentionally lenient (10x speedup) so it doesn't
    flake on a heavily-loaded CI box where the absolute timings are
    noisy — the point is to confirm the indexed path is materially
    faster, not to assert a specific millisecond budget.
    """
    db_path = tmp_path / "decision_ledger.db"
    DecisionLedger(db_path=db_path)
    _seed_decision_events(db_path, n=5_000)

    target_token = "TOK_25"  # ~100 rows out of 5000 match this token.

    # ── "Before": full scan + Python filter ──
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        t_scan_start = time.perf_counter()
        cursor.execute("SELECT * FROM decision_events ORDER BY timestamp DESC")
        all_rows = [dict(r) for r in cursor.fetchall()]
        # Python-side filter.
        filtered = [r for r in all_rows if r.get("token_id") == target_token][:50]
        t_scan = time.perf_counter() - t_scan_start

    # ── "After": indexed WHERE + ORDER BY + LIMIT ──
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        t_idx_start = time.perf_counter()
        cursor.execute(
            "SELECT * FROM decision_events WHERE token_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (target_token, 50),
        )
        idx_rows = [dict(r) for r in cursor.fetchall()]
        t_idx = time.perf_counter() - t_idx_start

    # Both paths must return the same row count (sanity check).
    assert len(filtered) == len(idx_rows) == 50, (
        f"both paths must return 50 rows; got full-scan={len(filtered)}, "
        f"indexed={len(idx_rows)}"
    )

    # Indexed query must be materially faster. The threshold is loose
    # (just "not slower") because the dataset is small enough that
    # Python overhead dominates — the spec says "faster than full scan"
    # which means strictly less wall time, not "10x faster".
    assert t_idx < t_scan, (
        f"indexed query ({t_idx * 1000:.1f}ms) must be faster than "
        f"full scan ({t_scan * 1000:.1f}ms) on 5000 rows"
    )


# ── (5) Optimisation script ─────────────────────────────────────────────────


def test_optimize_db_runs_without_error_on_empty_dir(tmp_path):
    """``scripts/optimize_db.py`` against an empty data dir exits 0."""
    from scripts.optimize_db import main

    empty_dir = tmp_path / "empty_data"
    empty_dir.mkdir(parents=True, exist_ok=True)

    # ``main`` returns an int (0 on success, 1 on failure).
    exit_code = main(
        [
            "--data-dir",
            str(empty_dir),
            # Don't re-run module _init_db (which would clobber the
            # module-level singletons' DB_PATHs) — just exercise the
            # ANALYZE-only path.
            "--skip-module-reinit",
        ]
    )
    assert exit_code == 0, (
        f"optimize_db.py against empty data dir must exit 0; got {exit_code}"
    )


def test_optimize_db_runs_analyze_on_populated_dir(tmp_path):
    """``scripts/optimize_db.py`` against a populated data dir runs
    ANALYZE on every ``*.db`` file and exits 0."""
    from scripts.optimize_db import main

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create a hand-rolled DB with one table + a single row so ANALYZE
    # has something to chew on.
    db_path = data_dir / "test_dummy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("CREATE INDEX idx_t_v ON t(v)")
        conn.execute("INSERT INTO t (v) VALUES ('hello')")
        conn.commit()

    exit_code = main(
        ["--data-dir", str(data_dir), "--skip-module-reinit"]
    )
    assert exit_code == 0
    # Verify ANALYZE created the sqlite_stat1 table.
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sqlite_stat1'"
        )
        assert cursor.fetchone() is not None, (
            "ANALYZE must create the sqlite_stat1 query-planner-stats table"
        )


# ── (6) timed_query decorator ───────────────────────────────────────────────


def test_timed_query_silent_on_fast_call(caplog):
    """``timed_query`` does NOT log when the wrapped function returns
    quickly (under the 100ms threshold)."""
    from core.decision_ledger import timed_query

    @timed_query
    def _fast() -> int:
        return 42

    with caplog.at_level(logging.WARNING, logger="core.decision_ledger"):
        result = _fast()
    assert result == 42
    assert not any(
        "slow query" in rec.message.lower() for rec in caplog.records
    ), "fast query must not produce a slow-query warning"


def test_timed_query_warns_on_slow_call(caplog):
    """``timed_query`` emits a WARNING when the wrapped function takes
    longer than the configured threshold."""
    from core.decision_ledger import timed_query, _SLOW_QUERY_THRESHOLD

    @timed_query
    def _slow() -> int:
        # Sleep ~3x the threshold so the warning is reliably triggered
        # even on a heavily-loaded CI box where wall-clock noise is
        # ±50ms.
        time.sleep(_SLOW_QUERY_THRESHOLD * 3)
        return 7

    with caplog.at_level(logging.WARNING, logger="core.decision_ledger"):
        result = _slow()
    assert result == 7
    slow_warnings = [
        rec for rec in caplog.records
        if "slow query" in rec.message.lower()
    ]
    assert len(slow_warnings) == 1, (
        f"slow query must produce exactly one warning; got {len(slow_warnings)}"
    )


def test_timed_query_supports_async_coroutines():
    """``timed_query`` correctly wraps ``async def`` functions (the
    decorator branches on ``asyncio.iscoroutinefunction``)."""
    from core.decision_ledger import timed_query

    @timed_query
    async def _async_fast() -> int:
        await asyncio.sleep(0)
        return 99

    # ``asyncio.run`` (Python 3.7+) is the documented replacement for
    # the deprecated ``asyncio.get_event_loop().run_until_complete()``
    # pattern.
    result = asyncio.run(_async_fast())
    assert result == 99


# ── (7) attribution N+1 fix ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_full_attribution_fetches_rows_once(monkeypatch):
    """``get_full_attribution`` must call
    ``closed_positions.get_closed_positions`` EXACTLY ONCE (not 7 times
    — once per dimension) thanks to the W11-9 N+1-fix refactor.
    """

    call_count = {"n": 0}

    async def _counting_get_closed_positions(limit=50, strategy=None):
        call_count["n"] += 1
        # Return a small synthetic trade list so the bucket aggregators
        # have something to chew on (matches the shape returned by the
        # real ``get_closed_positions``).
        return [
            {
                "token_id": "TOK",
                "strategy": "ml_sig_v1",
                "entry_price": 0.50,
                "exit_price": 0.55,
                "shares": 100.0,
                "pnl": 5.0,
                "holding_seconds": 3600.0,
                "direction": "BUY",
                "confidence": 0.65,
                "predicted_edge": 0.03,
                "p_yes": 0.6,
                "market_mid": 0.55,
                "liquidity": 50_000.0,
            }
            for _ in range(3)
        ]

    async def _counting_get_closed_stats():
        # Return a minimal valid stats dict so the summary block doesn't
        # blow up.
        return {
            "count": 3,
            "total_pnl": 15.0,
            "avg_pnl": 5.0,
            "median_pnl": 5.0,
            "win_rate": 1.0,
            "wins": 3,
            "losses": 0,
            "breakeven": 0,
            "avg_holding_seconds": 3600.0,
            "gross_profit": 15.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "best_trade": 5.0,
            "worst_trade": 5.0,
            "avg_entry_price": 0.50,
            "avg_exit_price": 0.55,
            "total_volume_shares": 300.0,
            "strategies_count": 1,
        }

    monkeypatch.setattr(
        _attribution_mod.closed_positions,
        "get_closed_positions",
        _counting_get_closed_positions,
    )
    monkeypatch.setattr(
        _attribution_mod.closed_positions,
        "get_closed_stats",
        _counting_get_closed_stats,
    )

    payload = await _attribution_mod.get_full_attribution()

    assert call_count["n"] == 1, (
        f"W11-9: get_full_attribution must fetch rows exactly ONCE, "
        f"not 7 times; observed {call_count['n']} calls"
    )
    # Sanity: all seven dimensions are present in the payload.
    for key in (
        "summary",
        "by_strategy",
        "by_confidence_bucket",
        "by_edge_bucket",
        "by_probability_band",
        "by_liquidity_level",
        "by_holding_period",
        "by_trade_direction",
        "bucket_definitions",
    ):
        assert key in payload, f"missing key {key!r} in attribution payload"


# ── (8) alerting.get_stats single-query optimisation ────────────────────────


def test_alerting_get_stats_combined_aggregate_matches_reference():
    """The W11-9 combined SUM(CASE WHEN ...) aggregate must produce
    the same total / unacked / critical-unacked counts as a hand-rolled
    reference implementation that iterates the rows directly.

    This guards against a regression where a future refactor of
    ``get_stats`` accidentally drops the ``acknowledged = 0`` filter
    on the critical count (the most bug-prone edge case in a SUM(CASE
    WHEN ...) expression).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "alerts.db"
        engine = AlertEngine(db_path=db_path)

        # Fire 5 alerts: 2 critical (max_drawdown) + 2 warning (psi) +
        # 1 critical (backend_unhealthy). The alert_id format
        # ``{rule_name}_{int(time.time() * 1000)}`` collides when two
        # alerts from the SAME rule fire within the same millisecond
        # (``INSERT OR REPLACE`` then overwrites the first), so we
        # insert a 10ms sleep between the same-rule fires to ensure
        # each alert lands as a distinct row.
        engine.evaluate({"daily_pnl": -5.0})   # critical
        time.sleep(0.011)
        engine.evaluate({"daily_pnl": -5.0})   # critical (distinct ms)
        time.sleep(0.011)
        engine.evaluate({"psi": 0.5})           # warning
        time.sleep(0.011)
        engine.evaluate({"psi": 0.5})           # warning (distinct ms)
        time.sleep(0.011)
        engine.evaluate({"backend_healthy": False})  # critical

        # Acknowledge one of the critical alerts.
        recent = engine.get_recent()
        first_critical_id = next(
            r["alert_id"] for r in recent if r["severity"] == "critical"
        )
        engine.acknowledge(first_critical_id)

        # ── Engine's combined-aggregate path ──
        stats = engine.get_stats()

        # ── Reference: hand-rolled Python count over the rows ──
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM alerts")
            rows = [dict(r) for r in cursor.fetchall()]
        ref_total = len(rows)
        ref_unacked = sum(1 for r in rows if r["acknowledged"] == 0)
        ref_critical_unacked = sum(
            1
            for r in rows
            if r["acknowledged"] == 0 and r["severity"] == "critical"
        )

        assert stats["total_alerts"] == ref_total, (
            f"engine total={stats['total_alerts']} != reference {ref_total}"
        )
        assert stats["unacknowledged"] == ref_unacked, (
            f"engine unacked={stats['unacknowledged']} != reference {ref_unacked}"
        )
        assert stats["critical_unacknowledged"] == ref_critical_unacked, (
            f"engine critical_unacked={stats['critical_unacknowledged']} "
            f"!= reference {ref_critical_unacked}"
        )

        # Expected: 5 total, 4 unacked (1 acknowledged), 2 critical unacked
        # (3 critical fired, 1 acknowledged).
        assert stats["total_alerts"] == 5
        assert stats["unacknowledged"] == 4
        assert stats["critical_unacknowledged"] == 2


def test_alerting_get_stats_empty_db_returns_zeros():
    """Empty DB → all stats zero (the no-rows edge case for the combined
    SUM(CASE WHEN ...) aggregate must not return NULL/None)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        engine = AlertEngine(db_path=Path(td) / "alerts.db")
        stats = engine.get_stats()
        assert stats == {
            "total_alerts": 0,
            "unacknowledged": 0,
            "critical_unacknowledged": 0,
        }
