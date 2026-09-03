"""
core/decision_ledger.py — SQLite-backed Unified Decision Ledger.

Links every stage of the trading pipeline via a single ``decision_id`` so the
full lifecycle of any prediction → order → fill (or rejection along the way)
can be reconstructed for any token or strategy:

    PREDICTION → SIGNAL → RISK_APPROVED | RISK_REJECTED → ORDER → FILL

Stages are emitted by the strategy / risk / paper-sim layers:

  - ``strategies/signal_trader.py::_ml_signal``  → PREDICTION + SIGNAL
                                                    (or RISK_REJECTED via
                                                    ``record_rejection()`` on
                                                    any early-exit path)
  - ``strategies/base.py::submit_order``         → RISK_APPROVED / RISK_REJECTED
  - ``paper/simulator.py::_execute_fill``        → FILL (with realised P&L)

Schema (additive — independent SQLite db so the audit trail's immutability
contract is not perturbed):

  decision_events   (id, timestamp, decision_id, stage, token_id, strategy,
                     pnl, data_json)            — ordered stage chain
  decision_rejections (id, timestamp, decision_id, token_id, strategy,
                       predicted_edge, confidence, reason, market_mid)
                                                — fast filtered rejection view

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose:

  GET /api/decision/{token_id}     recent decision events for a token
  GET /api/decisions/rejected      recent rejected decisions
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DECISION_LEDGER_DB_PATH", "/app/data/decision_ledger.db"))

# ── Canonical stage names ───────────────────────────────────────────────────
# Single source of truth across the pipeline — every emitter references these
# so the spelling is stable in queries / dashboards.
STAGE_PREDICTION = "PREDICTION"
STAGE_SIGNAL = "SIGNAL"
STAGE_RISK_APPROVED = "RISK_APPROVED"
STAGE_RISK_REJECTED = "RISK_REJECTED"
STAGE_ORDER = "ORDER"
STAGE_FILL = "FILL"

# Rejection reason vocabulary (mirrors the early-exit branches in
# ``signal_trader._ml_signal``). Centralised here so the API / dashboards can
# map reason codes to human-readable copy without coupling to the strategy
# module.
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_WIDE_SPREAD = "wide_spread"
REASON_NEUTRAL_ZONE = "neutral_zone"
REASON_INSUFFICIENT_KELLY_EDGE = "insufficient_kelly_edge"


class DecisionLedger:
    """
    Asynchronous, SQLite-backed unified decision ledger.

    All writes are fire-and-forget from the caller's perspective: every public
    method swallows its own persistence errors (logged at ``error`` level) so a
    ledger hiccup can never break the trading pipeline. Reads return plain
    ``list[dict]`` rows (most recent first where applicable).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables + indexes if absent. Safe to call on every boot."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS decision_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        decision_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        token_id TEXT,
                        strategy TEXT,
                        pnl REAL DEFAULT 0.0,
                        data_json TEXT
                    )
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dec_id "
                    "ON decision_events(decision_id, timestamp ASC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dec_token "
                    "ON decision_events(token_id, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dec_stage "
                    "ON decision_events(stage)"
                )

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS decision_rejections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        decision_id TEXT,
                        token_id TEXT,
                        strategy TEXT,
                        predicted_edge REAL,
                        confidence REAL,
                        reason TEXT,
                        market_mid REAL
                    )
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rej_token "
                    "ON decision_rejections(token_id, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rej_decision "
                    "ON decision_rejections(decision_id)"
                )
                conn.commit()
        except Exception as e:
            log.error("[decision_ledger] Init failed (%s): %s", self._db_path, e)

    # ── Writes ────────────────────────────────────────────────────────────

    @staticmethod
    def new_decision_id() -> str:
        """Generate a fresh, globally-unique ``decision_id`` (sortable prefix)."""
        return f"dec-{uuid.uuid4().hex}"

    async def record(
        self,
        decision_id: str,
        stage: str,
        token_id: str | None = None,
        strategy: str | None = None,
        pnl: float = 0.0,
        **data: Any,
    ) -> None:
        """
        Persist a single stage event for ``decision_id``.

        ``**data`` is serialised to JSON (with ``default=str`` so dataclasses
        / Decimals / enums don't blow up). Any persistence failure is logged
        and swallowed — the trading pipeline never blocks on ledger writes.
        """
        if not decision_id:
            # Skip silently — a missing decision_id means the caller didn't
            # participate in the unified ledger (e.g. legacy / manual orders).
            return
        ts = time.time()
        payload = json.dumps(data, default=str) if data else None

        def _insert() -> None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO decision_events
                        (timestamp, decision_id, stage, token_id, strategy, pnl, data_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts,
                            decision_id,
                            stage,
                            token_id,
                            strategy,
                            float(pnl or 0.0),
                            payload,
                        ),
                    )
                    conn.commit()
            except Exception as e:
                log.error(
                    "[decision_ledger] record failed stage=%s decision=%s: %s",
                    stage,
                    decision_id,
                    e,
                )

        await asyncio.to_thread(_insert)

    async def record_rejection(
        self,
        token_id: str,
        strategy: str,
        predicted_edge: float,
        confidence: float,
        reason: str,
        market_mid: float | None = None,
        decision_id: str = "",
    ) -> None:
        """
        Record a rejection event.

        Writes a row into ``decision_rejections`` (for fast filtered listing)
        AND emits a ``RISK_REJECTED`` stage event on the main ``decision_events``
        chain so the originating ``decision_id`` (if provided) has a complete
        end-to-end audit trail when inspected via ``get_chain()``.
        """
        ts = time.time()
        # 1. Main-chain stage event (best-effort; skipped if no decision_id).
        if decision_id:
            await self.record(
                decision_id=decision_id,
                stage=STAGE_RISK_REJECTED,
                token_id=token_id,
                strategy=strategy,
                pnl=0.0,
                predicted_edge=float(predicted_edge or 0.0),
                confidence=float(confidence or 0.0),
                reason=reason,
                market_mid=market_mid,
            )

        # 2. Rejections table row (always — even without a decision_id, so
        # manual / external rejections are still surfaced in the dashboard).
        def _insert_rej() -> None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO decision_rejections
                        (timestamp, decision_id, token_id, strategy,
                         predicted_edge, confidence, reason, market_mid)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts,
                            decision_id or "",
                            token_id,
                            strategy,
                            float(predicted_edge or 0.0),
                            float(confidence or 0.0),
                            reason,
                            float(market_mid) if market_mid is not None else None,
                        ),
                    )
                    conn.commit()
            except Exception as e:
                log.error(
                    "[decision_ledger] record_rejection failed token=%s reason=%s: %s",
                    token_id,
                    reason,
                    e,
                )

        await asyncio.to_thread(_insert_rej)

    # ── Reads ──────────────────────────────────────────────────────────────

    async def get_chain(self, decision_id: str) -> list[dict[str, Any]]:
        """Return the ordered stage chain for a single ``decision_id``."""
        if not decision_id:
            return []

        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT timestamp, decision_id, stage, token_id,
                               strategy, pnl, data_json
                        FROM decision_events
                        WHERE decision_id = ?
                        ORDER BY timestamp ASC, id ASC
                        """,
                        (decision_id,),
                    )
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error("[decision_ledger] get_chain failed decision=%s: %s", decision_id, e)
                return []
            # Decode data_json into ``data`` for caller convenience (raw
            # ``data_json`` is preserved on the row for transparency).
            for r in rows:
                r["data"] = _safe_json(r.get("data_json"))
            return rows

        return await asyncio.to_thread(_fetch)

    async def get_chain_by_token(
        self, token_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return recent stage events for a token (most recent first)."""
        if not token_id:
            return []

        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT timestamp, decision_id, stage, token_id,
                               strategy, pnl, data_json
                        FROM decision_events
                        WHERE token_id = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (token_id, int(limit)),
                    )
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error(
                    "[decision_ledger] get_chain_by_token failed token=%s: %s",
                    token_id,
                    e,
                )
                return []
            for r in rows:
                r["data"] = _safe_json(r.get("data_json"))
            return rows

        return await asyncio.to_thread(_fetch)

    async def get_rejections(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent rejection records (most recent first)."""
        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT timestamp, decision_id, token_id, strategy,
                               predicted_edge, confidence, reason, market_mid
                        FROM decision_rejections
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (int(limit),),
                    )
                    return [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error("[decision_ledger] get_rejections failed: %s", e)
                return []

        return await asyncio.to_thread(_fetch)


# Module-level singleton (mirrors the ``audit_logger`` / ``timescale_db``
# convention so importers can grab the instance at module import time).
decision_ledger = DecisionLedger()


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append decision-ledger inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/decision/{token_id}
          Recent decision events for a token (PREDICTION → SIGNAL →
          RISK_* → ORDER → FILL). 404 if no events recorded.

      GET /api/decisions/rejected
          Recent rejected decisions (most recent first).
    """
    from fastapi import HTTPException, Query  # local import — FastAPI is optional at module load

    @app.get("/api/decision/{token_id}", tags=["decisions"])
    async def _decision_chain_for_token(
        token_id: str,
        limit: int = Query(50, ge=1, le=500, description="Max events to return"),
    ):
        """Return the recent decision-event chain for ``token_id``."""
        chain = await decision_ledger.get_chain_by_token(token_id, limit=limit)
        if not chain:
            raise HTTPException(
                status_code=404,
                detail=f"no decision events recorded for token {token_id}",
            )
        # Decode data_json for caller convenience.
        for row in chain:
            raw = row.get("data_json")
            row["data"] = _safe_json(raw)
        return {"token_id": token_id, "count": len(chain), "events": chain}

    @app.get("/api/decisions/rejected", tags=["decisions"])
    async def _rejected_decisions(
        limit: int = Query(50, ge=1, le=500, description="Max rejections to return"),
    ):
        """Return recent rejected decisions (most recent first)."""
        rows = await decision_ledger.get_rejections(limit=limit)
        return {"count": len(rows), "rejections": rows}


def _safe_json(raw: str | None) -> Any:
    """Best-effort JSON decode for the ``data_json`` column."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


__all__ = [
    "DB_PATH",
    "DecisionLedger",
    "decision_ledger",
    "register_routes",
    "STAGE_PREDICTION",
    "STAGE_SIGNAL",
    "STAGE_RISK_APPROVED",
    "STAGE_RISK_REJECTED",
    "STAGE_ORDER",
    "STAGE_FILL",
    "REASON_LOW_CONFIDENCE",
    "REASON_WIDE_SPREAD",
    "REASON_NEUTRAL_ZONE",
    "REASON_INSUFFICIENT_KELLY_EDGE",
]
