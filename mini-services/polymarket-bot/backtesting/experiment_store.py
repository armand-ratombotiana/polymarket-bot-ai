"""Backtest experiment store — persists every run for comparison.

W20-3 — every backtest invocation (synthetic MC archetype simulation via
``POST /api/backtest/run`` and historical replay via
``POST /api/backtest/historical-replay``) is materialised into a
:class:`BacktestExperiment` row and saved to a dedicated SQLite DB
(``EXPERIMENT_DB`` env var → ``/app/data/backtest_experiments.db`` by
default) so cross-run comparison is possible. Previously (W17-6 God Mode
assessment §33) every ``run_backtest`` call returned an ephemeral dict
that was lost — no experiment registry existed, so no A/B comparison
between strategies / parameter sweeps was possible.

Schema (single ``experiments`` table + three indexes)::

    experiments
        experiment_id   TEXT PRIMARY KEY
        strategy        TEXT NOT NULL
        strategy_version TEXT
        start_time      REAL
        end_time        REAL
        initial_capital REAL
        final_equity    REAL
        total_return    REAL
        sharpe          REAL
        sortino         REAL
        calmar          REAL
        max_drawdown    REAL
        win_rate        REAL
        profit_factor   REAL
        n_trades        INTEGER
        config          TEXT   (JSON blob, ≤ 10 KB)
        created_at      REAL
        equity_curve    TEXT   (JSON blob, ≤ 10 KB)
        trades          TEXT   (JSON blob, ≤ 10 KB)

Three indexes for the common query patterns:

    idx_exp_strategy  (strategy)             — filter by strategy
    idx_exp_created    (created_at DESC)      — list newest-first
    idx_exp_return     (total_return DESC)    — leaderboard / compare

Public surface
~~~~~~~~~~~~~~

* :class:`BacktestExperiment`  — dataclass row shape.
* :class:`ExperimentStore`     — SQLite-backed save/get/list/compare.
* ``experiment_store``         — module-level singleton (constructed
  against ``EXPERIMENT_DB`` at import time; the init is fault-tolerant
  — a read-only ``/app/data`` is logged but does NOT crash the import).

The store is intentionally synchronous (single ``sqlite3.connect`` per
call) so it composes with the existing ``asyncio.to_thread(...)``
wrappers in ``api/server.py``'s backtest routes.

The ``BacktestExperiment`` shape is shared between both backtest engines
(synthetic MC ``BacktestEngine.run_backtest().to_dict()`` AND
``HistoricalReplayEngine.replay()`` via :class:`ReplayResult`). The
``_persist_backtest_experiment`` helper in ``api/server.py`` performs
the shape coercion at the API boundary so the store stays engine-agnostic.

The note on ``backtracking/`` vs ``backtesting/`` — the original task
spec wrote ``backtracking/experiment_store.py`` which is a typo (no
``backtracking/`` package exists in the repo); this module lives at
``backtesting/experiment_store.py`` alongside the other backtest modules
(``engine.py``, ``historical_replay.py``, ``report.py``, ``advanced.py``)
so the import path ``backtesting.experiment_store`` matches its siblings.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EXPERIMENT_DB = Path(
    os.environ.get("EXPERIMENT_DB", "/app/data/backtest_experiments.db")
)


@dataclass
class BacktestExperiment:
    """Materialised backtest run — one row in the ``experiments`` table.

    Field semantics match the headline risk metrics both engines compute:

      * ``total_return``   — fractional return (e.g. ``0.12`` for +12 %).
        The synthetic MC engine reports ``roi_pct`` (percentage); the
        historical-replay engine reports ``total_return`` (fractional).
        The API-boundary helper normalises both to the fractional form
        before constructing this dataclass.
      * ``sharpe`` / ``sortino`` / ``calmar`` — annualised risk-adjusted
        return ratios. Both engines compute these with a
        ``sqrt(252)`` (or ``sqrt(24 * 365)`` for hourly MC steps)
        annualisation factor.
      * ``max_drawdown``   — fractional drawdown (``0.15`` for 15 %).
        The MC engine reports ``max_drawdown_pct`` (percentage); the
        historical-replay engine reports ``max_drawdown`` (fractional).
        The API-boundary helper normalises both to fractional.
      * ``win_rate``       — fraction in ``[0, 1]``.
      * ``profit_factor``  — gross profit / gross loss (≥ 0; ``999.0``
        sentinel for "no losing trades").
      * ``n_trades``       — count of closed trades (BUY+SELL pairs).
      * ``equity_curve``   — list of float equity points (or per-step
        dicts ``{"step": ..., "equity": ...}``; the store JSON-encodes
        whatever is passed and caps the blob at 10 KB).
      * ``trades``         — list of trade dicts (capped at 10 KB).
    """

    experiment_id: str
    strategy: str
    strategy_version: str
    start_time: float
    end_time: float
    initial_capital: float
    final_equity: float
    total_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    n_trades: int
    config: dict
    created_at: float
    equity_curve: Optional[list[float]] = None
    trades: Optional[list[dict]] = None


class ExperimentStore:
    """SQLite-backed experiment persistence."""

    # 10 KB cap on the JSON-encoded ``equity_curve`` / ``trades`` blobs
    # so a single experiment row never exceeds ~ 30 KB total (a 1000-trade
    # backtest with full per-trade detail would otherwise produce a
    # multi-MB row that degrades SQLite's B-tree page locality). The
    # headline metrics above are NOT capped — they're the load-bearing
    # comparison surface and always serialise in < 1 KB.
    _BLOB_CAP_BYTES = 10_000

    def __init__(self, db_path: Path = EXPERIMENT_DB):
        self._db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Create the ``experiments`` table + indexes if absent.

        Fault-tolerant: a read-only parent directory (the common case in
        the sandbox where ``/app/data`` is not writable) logs a warning
        and returns — the singleton is still constructed; subsequent
        ``save()`` calls will then raise ``sqlite3.Error``, which the API
        route surfaces as a 500. This matches the import-time pattern in
        ``core/decision_ledger.py`` and ``core/immutable_audit.py``
        (swallow init errors so the test session never crashes on
        import; defer to first write to surface the real failure).
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "ExperimentStore: cannot create %s (%s); "
                "experiments will fail to save.",
                self._db_path.parent, exc,
            )
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS experiments (
                        experiment_id TEXT PRIMARY KEY,
                        strategy TEXT NOT NULL,
                        strategy_version TEXT,
                        start_time REAL,
                        end_time REAL,
                        initial_capital REAL,
                        final_equity REAL,
                        total_return REAL,
                        sharpe REAL,
                        sortino REAL,
                        calmar REAL,
                        max_drawdown REAL,
                        win_rate REAL,
                        profit_factor REAL,
                        n_trades INTEGER,
                        config TEXT,
                        created_at REAL,
                        equity_curve TEXT,
                        trades TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_exp_strategy
                        ON experiments(strategy);
                    CREATE INDEX IF NOT EXISTS idx_exp_created
                        ON experiments(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_exp_return
                        ON experiments(total_return DESC);
                    """
                )
        except sqlite3.Error as exc:
            logger.warning(
                "ExperimentStore: schema init failed for %s (%s).",
                self._db_path, exc,
            )

    def save(self, exp: BacktestExperiment) -> str:
        """Save an experiment. Returns the ``experiment_id``.

        ``INSERT OR REPLACE`` so a caller-supplied ``experiment_id`` that
        collides with an existing row overwrites it (idempotent re-runs of
        the same strategy + params don't accumulate duplicate rows).
        """
        equity_curve_json = json.dumps(exp.equity_curve or [])
        trades_json = json.dumps(exp.trades or [])
        config_json = json.dumps(exp.config or {})
        # Cap the blobs so a single experiment never exceeds ~ 30 KB.
        if len(equity_curve_json) > self._BLOB_CAP_BYTES:
            equity_curve_json = equity_curve_json[: self._BLOB_CAP_BYTES]
            logger.warning(
                "ExperimentStore.save: equity_curve JSON for %s truncated "
                "to %d bytes (cap=%d).",
                exp.experiment_id, len(equity_curve_json),
                self._BLOB_CAP_BYTES,
            )
        if len(trades_json) > self._BLOB_CAP_BYTES:
            trades_json = trades_json[: self._BLOB_CAP_BYTES]
            logger.warning(
                "ExperimentStore.save: trades JSON for %s truncated to "
                "%d bytes (cap=%d).",
                exp.experiment_id, len(trades_json),
                self._BLOB_CAP_BYTES,
            )
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO experiments
                (experiment_id, strategy, strategy_version, start_time, end_time,
                 initial_capital, final_equity, total_return, sharpe, sortino, calmar,
                 max_drawdown, win_rate, profit_factor, n_trades, config, created_at,
                 equity_curve, trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exp.experiment_id, exp.strategy, exp.strategy_version,
                    exp.start_time, exp.end_time, exp.initial_capital,
                    exp.final_equity, exp.total_return, exp.sharpe,
                    exp.sortino, exp.calmar, exp.max_drawdown,
                    exp.win_rate, exp.profit_factor, exp.n_trades,
                    config_json, exp.created_at,
                    equity_curve_json, trades_json,
                ),
            )
        logger.info(
            "Saved experiment %s (strategy=%s return=%.4f sharpe=%.3f trades=%d)",
            exp.experiment_id, exp.strategy, exp.total_return,
            exp.sharpe, exp.n_trades,
        )
        return exp.experiment_id

    def get(self, experiment_id: str) -> Optional[dict]:
        """Fetch one experiment by ``experiment_id``; ``None`` if absent.

        The JSON-encoded ``config`` / ``equity_curve`` / ``trades`` blobs
        are decoded back to native Python types. A truncated blob (see
        ``save``) returns the truncated string verbatim — the caller can
        detect truncation by checking whether the JSON parses cleanly
        (``json.loads`` raises ``JSONDecodeError`` on a truncation point
        inside the blob; this method catches that and falls back to an
        empty container so callers don't crash).
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["config"] = _safe_json_loads(result.get("config"), default={})
            result["equity_curve"] = _safe_json_loads(
                result.get("equity_curve"), default=[]
            )
            result["trades"] = _safe_json_loads(result.get("trades"), default=[])
            return result

    def list_experiments(
        self, strategy: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """List experiments newest-first, optionally filtered by strategy.

        ``limit`` is clamped to ``[1, 1000]`` to bound the response size —
        the JSON-encoded ``equity_curve`` / ``trades`` blobs make each row
        large (up to ~ 30 KB), so a 1000-row ceiling caps the worst-case
        response at ~ 30 MB.
        """
        limit = max(1, min(int(limit), 1000))
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if strategy:
                rows = conn.execute(
                    "SELECT * FROM experiments WHERE strategy = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (strategy, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            # Decode JSON blobs so the list output matches the ``get``
            # output shape — every row has ``config`` / ``equity_curve``
            # / ``trades`` as native Python types, not raw JSON strings.
            out: list[dict] = []
            for row in rows:
                d = dict(row)
                d["config"] = _safe_json_loads(d.get("config"), default={})
                d["equity_curve"] = _safe_json_loads(
                    d.get("equity_curve"), default=[]
                )
                d["trades"] = _safe_json_loads(d.get("trades"), default=[])
                out.append(d)
            return out

    def compare(self, experiment_ids: list[str]) -> dict:
        """Compare multiple experiments by their headline risk metrics.

        Returns a dict with:
          * ``count``            — number of experiments actually found.
          * ``best_return``      — max ``total_return`` across the set.
          * ``best_sharpe``      — max ``sharpe`` across the set.
          * ``lowest_drawdown``  — min ``max_drawdown`` across the set
            (drawdown is non-negative; lower is better).
          * ``experiments``      — list of {id, strategy, return, sharpe,
            max_drawdown, win_rate} summary dicts, one per found
            experiment.

        Missing IDs are silently dropped (logged at INFO). If no IDs
        match, returns ``{"error": "No experiments found", "count": 0,
        "experiments": []}`` rather than raising — the API route surfaces
        that as a 200 with the error body (a 404 would be misleading
        because the request itself was valid; the caller can distinguish
        by checking ``count == 0``).
        """
        experiments: list[dict] = []
        for eid in experiment_ids:
            e = self.get(eid)
            if e is None:
                logger.info(
                    "ExperimentStore.compare: experiment_id=%s not found; "
                    "skipping.", eid,
                )
                continue
            experiments.append(e)
        if not experiments:
            return {
                "error": "No experiments found",
                "count": 0,
                "experiments": [],
            }
        return {
            "count": len(experiments),
            "best_return": max(e["total_return"] for e in experiments),
            "best_sharpe": max(e["sharpe"] for e in experiments),
            "lowest_drawdown": min(e["max_drawdown"] for e in experiments),
            "experiments": [
                {
                    "id": e["experiment_id"],
                    "strategy": e["strategy"],
                    "return": e["total_return"],
                    "sharpe": e["sharpe"],
                    "max_drawdown": e["max_drawdown"],
                    "win_rate": e["win_rate"],
                }
                for e in experiments
            ],
        }


def _safe_json_loads(raw: Optional[str], default):
    """``json.loads`` that returns ``default`` on ``None`` / parse error.

    The ``equity_curve`` / ``trades`` blobs are capped at 10 KB on write,
    so a blob truncated mid-JSON would raise ``JSONDecodeError`` on read.
    Rather than propagate that to the API consumer (a single bad row
    would 500 the whole ``/api/backtest/experiments`` listing), we
    return the caller-supplied default — the headline metrics above are
    always present and untruncated, so the comparison / list endpoints
    stay useful even with a corrupted blob.
    """
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "ExperimentStore: failed to decode JSON blob (%.80r...); "
            "returning default %r.", raw, default,
        )
        return default


# Module-level singleton. Constructed at import time against
# ``EXPERIMENT_DB``; the constructor swallows any OSError / sqlite3
# errors so importing this module never crashes the test session even
# when ``/app/data`` is read-only (mirrors the import-time pattern in
# ``core/decision_ledger.py`` and ``core/immutable_audit.py``).
try:
    experiment_store = ExperimentStore(EXPERIMENT_DB)
except Exception as exc:  # pragma: no cover — defensive: never crash on import
    logger.warning(
        "experiment_store singleton init failed (%s); "
        "callers must construct their own ExperimentStore.", exc,
    )
    experiment_store = None  # type: ignore[assignment]
