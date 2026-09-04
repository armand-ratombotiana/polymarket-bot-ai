"""ML economic value tracker — measures whether ML actually adds value.

W19-4 — God Mode §16 found ML economic value is unmeasured. There was no
P&L by model version, no P&L by confidence / predicted-edge bucket, and no
"with AI vs without AI" counterfactual. The seven-dimension attribution
engine in ``core/attribution.py`` slices realised P&L across strategy,
confidence, edge, probability band, liquidity, holding period, and trade
direction — but it does NOT slice P&L by *model version*, and it does NOT
compute a counterfactual ("what would we have made without the model?").

This module closes that gap. It mirrors the ``core/closed_positions.py``
shape (module-level singleton, ``_init_db`` idempotent schema setup,
async-friendly sync surface, ``register_routes(app)`` FastAPI wiring)
so the trade-recording call sites in ``paper/simulator.py`` and
``core/settlement.py`` can drop in a single ``record_trade(...)`` call
alongside the existing ``closed_positions.record_closed_position(...)``
call without perturbing the journaling contract.

Schema (independent SQLite db at ``ML_VALUE_DB`` so the closed-positions
journal's immutability contract is not perturbed)::

    ml_trade_attribution (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id        TEXT,
        token_id        TEXT,
        model_version   TEXT,
        prediction      REAL,         -- raw model p_yes at signal time
        confidence      REAL,         -- ML confidence at signal time [0..1]
        predicted_edge  REAL,         -- p_yes − market_mid
        actual_pnl      REAL,         -- realised P&L on the round-trip trade
        timestamp       REAL,
        metadata        TEXT          -- JSON catch-all for extras
    )

Three roll-ups + one counterfactual::

    get_pnl_by_model_version()    GROUP BY model_version ORDER BY total_pnl DESC
    get_pnl_by_confidence_bucket()  CASE-bucketed confidence (0.2-wide bins)
    get_pnl_by_edge_bucket()        CASE-bucketed predicted_edge
    get_counterfactual()            with-AI P&L vs without-AI baseline

The counterfactual baseline is intentionally crude — without AI, we would
have traded at the market mid (no edge) so the per-trade "no-information"
P&L is approximately ``-predicted_edge * 10`` (a rough proxy for the
slippage + adverse-selection drag a no-model trader would pay). The
constant 10 is documented inline so an operator can swap in a calibrated
model when one becomes available.

Three HTTP endpoints (auth enforced by the caller's existing
``enforce_api_auth`` middleware — these paths are NOT in ``PUBLIC_PATHS``)::

    GET /api/ml/economic-value                full summary (3 roll-ups + counterfactual)
    GET /api/ml/economic-value/by-model       P&L grouped by model version
    GET /api/ml/economic-value/counterfactual with-AI vs without-AI counterfactual
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Module-level DB path — mirrors the ``CLOSED_POSITIONS_DB_PATH`` /
# ``AUDIT_DB_PATH`` / ``DECISION_LEDGER_DB_PATH`` convention so the conftest
# env-var redirect (``/tmp/pmbot_conftest_isolation/ml_economic_value.db``)
# picks this up automatically without a sibling test-module needing to
# monkeypatch anything.
ML_VALUE_DB = Path(
    os.environ.get("ML_VALUE_DB", "/app/data/ml_economic_value.db")
)

# W19-4 — Counterfactual scaling constant. Without an ML signal, a trader
# picks the market mid as the entry, so the per-trade "no-information" P&L is
# the negative of the *realised* edge scaled by an assumed 10-share ticket.
# Documented inline so the operator can swap in a calibrated model when one
# becomes available (e.g. regress the residual against the predicted_edge
# and use the fitted coefficient).
COUNTERFACTUAL_EDGE_SCALE = 10.0


class MLEconomicValueTracker:
    """Tracks the economic value of ML predictions.

    Singleton-friendly (module-level ``ml_value_tracker`` instance below)
    but every method is safe to call on a fresh instance — tests construct
    one per ``tmp_path`` so the production singleton (built against the
    non-writable ``/app/data/ml_economic_value.db`` sandbox path) is left
    untouched. Mirrors the ``ClosedPositionsStore(tmp_path / "test.db")``
    isolation pattern in ``tests/test_closed_positions.py``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path: Path = Path(db_path) if db_path else ML_VALUE_DB
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the ``ml_trade_attribution`` table + indexes if absent.

        Safe to call on every boot — uses ``CREATE TABLE IF NOT EXISTS`` +
        ``CREATE INDEX IF NOT EXISTS`` so a re-init against an existing DB
        is a no-op. Mirrors the closed-positions init pattern.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ml_trade_attribution (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id        TEXT,
                        token_id        TEXT,
                        model_version   TEXT,
                        prediction      REAL,
                        confidence      REAL,
                        predicted_edge  REAL,
                        actual_pnl      REAL,
                        timestamp       REAL,
                        metadata        TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_mla_version
                        ON ml_trade_attribution(model_version);
                    CREATE INDEX IF NOT EXISTS idx_mla_confidence
                        ON ml_trade_attribution(confidence);
                    CREATE INDEX IF NOT EXISTS idx_mla_edge
                        ON ml_trade_attribution(predicted_edge);
                    CREATE INDEX IF NOT EXISTS idx_mla_token
                        ON ml_trade_attribution(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_mla_time
                        ON ml_trade_attribution(timestamp DESC);
                    """
                )
        except Exception as e:  # pragma: no cover — defensive
            log.error("[ml_value] _init_db failed (%s): %s", self._db_path, e)

    # ── Writes ───────────────────────────────────────────────────────────

    def record_trade(
        self,
        trade_id: str,
        token_id: str,
        model_version: str,
        prediction: float,
        confidence: float,
        predicted_edge: float,
        actual_pnl: float,
        metadata: dict | None = None,
    ) -> None:
        """Record a closed trade's ML attribution.

        Fire-and-forget — a journaling hiccup is swallowed (logged at
        ``error`` level) so the trading pipeline never breaks on an ML-value
        recorder failure. Mirrors the ``closed_positions.record_closed_position``
        persistence-error contract.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO ml_trade_attribution
                        (trade_id, token_id, model_version, prediction,
                         confidence, predicted_edge, actual_pnl,
                         timestamp, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(trade_id),
                        str(token_id),
                        str(model_version or "unknown"),
                        float(prediction or 0.0),
                        float(confidence or 0.0),
                        float(predicted_edge or 0.0),
                        float(actual_pnl or 0.0),
                        time.time(),
                        json.dumps(metadata or {}, default=str),
                    ),
                )
        except Exception as e:  # pragma: no cover — defensive
            log.error(
                "[ml_value] record_trade failed trade_id=%s token=%s: %s",
                trade_id, token_id, e,
            )

    # ── Reads ────────────────────────────────────────────────────────────

    def get_pnl_by_model_version(self) -> list[dict[str, Any]]:
        """P&L grouped by model version (most profitable first).

        Returns one row per distinct ``model_version`` with::

            {
                "model_version": str,
                "trades":        int,
                "total_pnl":     float,
                "avg_pnl":       float,
                "wins":          int,
                "losses":        int,
            }
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    COALESCE(model_version, 'unknown') AS model_version,
                    COUNT(*)                                AS trades,
                    COALESCE(SUM(actual_pnl), 0.0)          AS total_pnl,
                    COALESCE(AVG(actual_pnl), 0.0)          AS avg_pnl,
                    SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN actual_pnl < 0 THEN 1 ELSE 0 END) AS losses
                FROM ml_trade_attribution
                GROUP BY model_version
                ORDER BY total_pnl DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pnl_by_confidence_bucket(self) -> list[dict[str, Any]]:
        """P&L grouped by confidence bucket (0.0–0.2, 0.2–0.4, …).

        The bucket boundaries are 0.2-wide bins over the [0, 1] confidence
        range — coarser than the four-bucket scheme ``core/attribution.py``
        uses (``low`` / ``medium`` / ``high`` / ``very_high``) so an
        operator can spot a monotonic confidence → P&L gradient at a
        glance (the granularity that matters for "does higher confidence
        actually pay?" is the trend, not the per-bucket label).

        Returns one row per non-empty bucket ordered by bucket label.
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN confidence < 0.2 THEN '0.0-0.2'
                        WHEN confidence < 0.4 THEN '0.2-0.4'
                        WHEN confidence < 0.6 THEN '0.4-0.6'
                        WHEN confidence < 0.8 THEN '0.6-0.8'
                        ELSE '0.8-1.0'
                    END AS bucket,
                    COUNT(*)                                AS trades,
                    COALESCE(SUM(actual_pnl), 0.0)          AS total_pnl,
                    COALESCE(AVG(actual_pnl), 0.0)          AS avg_pnl,
                    SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) AS wins
                FROM ml_trade_attribution
                WHERE confidence IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pnl_by_edge_bucket(self) -> list[dict[str, Any]]:
        """P&L grouped by predicted-edge bucket (``<1%``, ``1–3%``, …).

        Bucket boundaries mirror the closed-positions attribution engine's
        ``classify_edge`` thresholds but use percentage labels (``<1%``,
        ``1-3%``, ``3-5%``, ``5-10%``, ``>10%``) instead of the
        ``negative`` / ``small`` / ``medium`` / ``large`` / ``very_large``
        names — the percentage labels are more readable in the dashboard
        P&L heatmap.
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN predicted_edge < 0.01 THEN '<1%'
                        WHEN predicted_edge < 0.03 THEN '1-3%'
                        WHEN predicted_edge < 0.05 THEN '3-5%'
                        WHEN predicted_edge < 0.10 THEN '5-10%'
                        ELSE '>10%'
                    END AS bucket,
                    COUNT(*)                                AS trades,
                    COALESCE(SUM(actual_pnl), 0.0)          AS total_pnl,
                    COALESCE(AVG(actual_pnl), 0.0)          AS avg_pnl
                FROM ml_trade_attribution
                WHERE predicted_edge IS NOT NULL
                GROUP BY bucket
                ORDER BY MIN(predicted_edge)
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_counterfactual(self) -> dict[str, Any]:
        """Compute the with-AI vs without-AI counterfactual.

        Without AI: assume we would have traded at the market mid price
        (no edge) — so the per-trade "no-information" P&L is approximately
        ``-predicted_edge * COUNTERFACTUAL_EDGE_SCALE`` (the adverse-
        selection + slippage drag a no-model trader would pay on a 10-
        share ticket). With AI: actual P&L with model-driven entries.

        The constant ``COUNTERFACTUAL_EDGE_SCALE`` is module-level so an
        operator can swap it for a calibrated coefficient when one
        becomes available. The baseline is intentionally crude — the
        point of the counterfactual is to surface a *directional* answer
        to "is the model adding value", not a precise dollar figure.

        Returns::

            {
                "with_ai_pnl":         float,
                "without_ai_pnl":       float,
                "ml_value":             float,  # with_ai − without_ai
                "n_trades":             int,
                "ml_value_per_trade":   float,
            }
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT actual_pnl, predicted_edge, confidence
                FROM ml_trade_attribution
                WHERE actual_pnl IS NOT NULL
                """
            ).fetchall()

        if not rows:
            return {
                "with_ai_pnl": 0.0,
                "without_ai_pnl": 0.0,
                "ml_value": 0.0,
                "n_trades": 0,
                "ml_value_per_trade": 0.0,
            }

        with_ai_pnl = sum(float(r["actual_pnl"] or 0.0) for r in rows)
        without_ai_pnl = sum(
            -float(r["predicted_edge"] or 0.0) * COUNTERFACTUAL_EDGE_SCALE
            for r in rows
        )
        ml_value = with_ai_pnl - without_ai_pnl
        n = len(rows)
        return {
            "with_ai_pnl": float(with_ai_pnl),
            "without_ai_pnl": float(without_ai_pnl),
            "ml_value": float(ml_value),
            "n_trades": n,
            "ml_value_per_trade": float(ml_value / n) if n else 0.0,
        }

    def get_summary(self) -> dict[str, Any]:
        """Return the full ML economic-value payload.

        Combines the three roll-ups + the counterfactual in a single
        dict so ``GET /api/ml/economic-value`` can return one object.
        """
        return {
            "by_model_version": self.get_pnl_by_model_version(),
            "by_confidence": self.get_pnl_by_confidence_bucket(),
            "by_edge": self.get_pnl_by_edge_bucket(),
            "counterfactual": self.get_counterfactual(),
        }


# Module-level singleton — mirrors the closed_positions /
# decision_ledger / audit_logger convention. The conftest env-var
# redirect (`/tmp/pmbot_conftest_isolation/ml_economic_value.db`) picks
# this up automatically so the test suite never touches the real
# ``/app/data/ml_economic_value.db`` sandbox path.
ml_value_tracker = MLEconomicValueTracker()


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """Append the ML-economic-value endpoints to a FastAPI app.

    Three endpoints (auth enforced by the caller's existing
    ``enforce_api_auth`` middleware — these paths are NOT in
    ``PUBLIC_PATHS``)::

        GET /api/ml/economic-value                full summary
        GET /api/ml/economic-value/by-model       P&L by model version
        GET /api/ml/economic-value/counterfactual  with-AI vs without-AI

    Pure addition — does not touch any existing route, middleware, or
    decorator. Same registration pattern as the sibling
    ``register_routes`` blocks (alias imported under ``_register_*`` in
    ``api/server.py``).
    """
    @app.get("/api/ml/economic-value", tags=["ml"])
    async def _ml_economic_value():
        """Full ML economic-value summary (3 roll-ups + counterfactual)."""
        return ml_value_tracker.get_summary()

    @app.get("/api/ml/economic-value/by-model", tags=["ml"])
    async def _ml_value_by_model():
        """P&L grouped by model version (most profitable first)."""
        return ml_value_tracker.get_pnl_by_model_version()

    @app.get("/api/ml/economic-value/counterfactual", tags=["ml"])
    async def _ml_counterfactual():
        """With-AI vs without-AI counterfactual P&L."""
        return ml_value_tracker.get_counterfactual()


__all__ = [
    "ML_VALUE_DB",
    "COUNTERFACTUAL_EDGE_SCALE",
    "MLEconomicValueTracker",
    "ml_value_tracker",
    "register_routes",
]
