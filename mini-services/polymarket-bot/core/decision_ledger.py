"""
core/decision_ledger.py — SQLite-backed Unified Decision Ledger.

Links every stage of the trading pipeline via a single ``decision_id`` so the
full lifecycle of any prediction → order → fill (or rejection along the way)
can be reconstructed for any token or strategy:

    MARKET_SNAPSHOT → INTELLIGENCE_SNAPSHOT → FEATURE_SNAPSHOT →
    PREDICTION → SIGNAL → RISK_APPROVED | RISK_REJECTED → ORDER → FILL →
    POSITION → OUTCOME → P&L

W19-3 — the ledger now records all 12 canonical stages from the God Mode §51
"complete decision chain" requirement. The original 6 stages
(PREDICTION / SIGNAL / RISK_APPROVED / RISK_REJECTED / ORDER / FILL) are
preserved verbatim; the 6 new stages (MARKET_SNAPSHOT, INTELLIGENCE_SNAPSHOT,
FEATURE_SNAPSHOT, POSITION, OUTCOME, PNL) are ADDITIVE — recorded as
fire-and-forget best-effort writes alongside the existing chain so the system
can finally answer "Why did the bot make this trade?" for the full chain
from market data to realized P&L.

Stages are emitted by the strategy / risk / paper-sim / settlement layers:

  - ``strategies/signal_trader.py::_ml_signal``  → MARKET_SNAPSHOT +
                                                    INTELLIGENCE_SNAPSHOT +
                                                    FEATURE_SNAPSHOT +
                                                    PREDICTION + SIGNAL
                                                    (or RISK_REJECTED via
                                                    ``record_rejection()`` on
                                                    any early-exit path)
  - ``strategies/base.py::submit_order``         → RISK_APPROVED / RISK_REJECTED
  - ``paper/simulator.py::_execute_fill``        → FILL (with realised P&L) +
                                                    POSITION
  - ``core/settlement.py::_process_resolved_market``
                                                → OUTCOME + PNL

Schema (additive — independent SQLite db so the audit trail's immutability
contract is not perturbed):

  decision_events   (id, timestamp, decision_id, stage, token_id, strategy,
                     pnl, data_json)            — ordered stage chain
  decision_rejections (id, timestamp, decision_id, token_id, strategy,
                       predicted_edge, confidence, reason, market_mid)
                                                — fast filtered rejection view

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose:

  GET /api/decision/{token_id}                     recent decision events for a token
  GET /api/decisions/rejected                      recent rejected decisions
  GET /api/decision/{correlation_id}/full-chain   full 12-stage chain keyed by stage
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


# ── W11-9: query timing decorator ───────────────────────────────────────────
# Lightweight instrumentation for the most commonly-called read paths. Wraps
# async / sync query methods, measures wall time, and emits a WARNING when a
# single call exceeds ``_SLOW_QUERY_THRESHOLD`` (100 ms — the SLO threshold
# surfaced by the dashboard's "recent decisions" feed). Failed queries (the
# underlying methods swallow their own persistence errors and return []) are
# still timed so a slow failure path surfaces in the log alongside the
# exception traceback. The decorator is import-safe and never re-raises —
# it preserves the wrapped function's return value / exception semantics
# verbatim.
_SLOW_QUERY_THRESHOLD = 0.100  # seconds


def timed_query(func):
    """Log a warning when ``func`` takes longer than 100 ms.

    Works for both ``def`` and ``async def`` callables — the wrapper
    inspects the return value and ``await``s it only when it's an awaitable
    so synchronous query methods (e.g. ``AlertEngine.get_recent``) can be
    decorated without forcing an ``async`` rewrite.
    """
    import functools

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def _async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.time() - start
                if duration > _SLOW_QUERY_THRESHOLD:
                    log.warning(
                        "[decision_ledger] slow query %s: %.3fs",
                        func.__name__,
                        duration,
                    )

        return _async_wrapper

    @functools.wraps(func)
    def _sync_wrapper(*args, **kwargs):
        start = time.time()
        try:
            return func(*args, **kwargs)
        finally:
            duration = time.time() - start
            if duration > _SLOW_QUERY_THRESHOLD:
                log.warning(
                    "[decision_ledger] slow query %s: %.3fs",
                    func.__name__,
                    duration,
                )

    return _sync_wrapper

# ── Canonical stage names ───────────────────────────────────────────────────
# Single source of truth across the pipeline — every emitter references these
# so the spelling is stable in queries / dashboards.
#
# W19-3 — the 6 new stages extend the original 6-stage chain into the full
# 12-stage lifecycle described in God Mode §51:
#
#   MARKET_SNAPSHOT → INTELLIGENCE_SNAPSHOT → FEATURE_SNAPSHOT →
#   PREDICTION → SIGNAL → RISK_APPROVED | RISK_REJECTED → ORDER → FILL →
#   POSITION → OUTCOME → PNL
#
# The original 6 stages are preserved verbatim below; the 6 new stages
# follow. Together they answer the full "Why did the bot make this trade?"
# chain from raw market data through to realised P&L.
STAGE_PREDICTION = "PREDICTION"
STAGE_SIGNAL = "SIGNAL"
STAGE_RISK_APPROVED = "RISK_APPROVED"
STAGE_RISK_REJECTED = "RISK_REJECTED"
STAGE_ORDER = "ORDER"
STAGE_FILL = "FILL"

# W19-3 — the 6 new stages that close the gaps surfaced by the God Mode §51
# assessment. Each is recorded as a fire-and-forget best-effort write at its
# canonical point in the trading pipeline (see the module docstring for the
# full stage→emitter map). All 12 stages share the same ``decision_id``
# correlation key so a single ``get_full_chain(decision_id)`` call
# reconstructs the complete decision lifecycle.
STAGE_MARKET_SNAPSHOT = "MARKET_SNAPSHOT"
STAGE_INTELLIGENCE_SNAPSHOT = "INTELLIGENCE_SNAPSHOT"
STAGE_FEATURE_SNAPSHOT = "FEATURE_SNAPSHOT"
STAGE_POSITION = "POSITION"
STAGE_OUTCOME = "OUTCOME"
STAGE_PNL = "PNL"

# Canonical 12-stage ordering — the dashboard's "decision timeline" view sorts
# events against this list so a chain with all 12 stages renders in
# chronological-business-order (not insertion order, which can vary when
# POSITION is recorded after FILL but OUTCOME lands hours/days later via
# settlement). Stages absent from a chain are simply skipped by the renderer.
CANONICAL_STAGE_ORDER: tuple[str, ...] = (
    STAGE_MARKET_SNAPSHOT,
    STAGE_INTELLIGENCE_SNAPSHOT,
    STAGE_FEATURE_SNAPSHOT,
    STAGE_PREDICTION,
    STAGE_SIGNAL,
    STAGE_RISK_APPROVED,  # one of RISK_APPROVED / RISK_REJECTED
    STAGE_RISK_REJECTED,
    STAGE_ORDER,
    STAGE_FILL,
    STAGE_POSITION,
    STAGE_OUTCOME,
    STAGE_PNL,
)

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
                # ── W11-9: additional indexes for common query patterns ──
                # (stage, timestamp DESC) — used by dashboards filtering
                # "recent PREDICTION / FILL events across all tokens". The
                # bare ``(stage)`` index above can't satisfy the ORDER BY
                # without a separate sort pass; the compound variant does.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dec_stage_ts "
                    "ON decision_events(stage, timestamp DESC)"
                )
                # (timestamp DESC) — recent-decisions feed (no WHERE filter).
                # Without this index SQLite must sort the full table on every
                # ``ORDER BY timestamp DESC`` query.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dec_ts "
                    "ON decision_events(timestamp DESC)"
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
                # ── W11-9: additional rejection-table indexes ──
                # (timestamp DESC) — ``get_rejections(limit=N)`` is a
                # no-WHERE recent-first query; without this index SQLite
                # must sort the whole rejection table on every call.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rej_ts "
                    "ON decision_rejections(timestamp DESC)"
                )
                # (reason, timestamp DESC) — future "recent rejections
                # by reason code" queries (operator dashboard filters
                # by ``low_confidence`` / ``wide_spread`` / etc.).
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rej_reason_ts "
                    "ON decision_rejections(reason, timestamp DESC)"
                )
                # (strategy, timestamp DESC) — strategy-filtered rejection
                # views (e.g. "show me recent ml_sig_v1 rejections").
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rej_strategy_ts "
                    "ON decision_rejections(strategy, timestamp DESC)"
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
        # V14: auto-stamp ``model_version`` on every PREDICTION stage event
        # so the audit trail records which ML model produced each
        # prediction. A caller-supplied ``model_version`` kwarg (e.g. for
        # replay / back-fill of historical events) is preserved verbatim.
        # Resolution is lazy + best-effort: if the registry can't be
        # imported (e.g. read-only sandbox where ``_load_from_disk`` would
        # raise ``PermissionError``), the helper returns ``"unknown"`` and
        # the trade-decision pipeline keeps flowing.
        if stage == STAGE_PREDICTION and "model_version" not in data:
            data["model_version"] = _resolve_active_model_version()
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

    # ── W19-3: 6 new stage-recording helpers ──────────────────────────────
    #
    # Each helper is a thin wrapper over ``record()`` that:
    #   1. Filters out any reserved-key collision (``decision_id`` / ``stage``
    #      / ``token_id`` / ``strategy`` / ``pnl``) from the caller-supplied
    #      payload dict before ``**`` expansion so a snapshot dict that
    #      happens to carry a ``token_id`` key (e.g. an order-book snapshot
    #      that records its own ``token_id`` field) doesn't raise
    #      ``TypeError: got multiple values for keyword argument``.
    #   2. Forwards the filtered payload as ``**data`` so the snapshot is
    #      stored as top-level keys in ``data_json`` (queryable by the
    #      dashboard without a nested-dict hop).
    #   3. Defaults ``strategy`` + ``pnl`` to sensible no-op values so the
    #      caller only has to supply ``correlation_id`` + ``token_id`` +
    #      ``snapshot``.
    #
    # ``correlation_id`` is the W19-3 spec name for the cross-stage trace
    # key. In this codebase it is identical to the existing ``decision_id``
    # (the spec uses the two names interchangeably); the helpers below
    # accept ``correlation_id`` as the param name to match the spec, then
    # pass it through to ``record()`` as ``decision_id``.

    async def record_market_snapshot(
        self,
        correlation_id: str,
        token_id: str,
        snapshot: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record the market state at decision time.

        Captures the order-book snapshot (best_bid / best_ask / mid / spread
        / top-of-book depth) at the moment the strategy began evaluating the
        token — the first stage of the 12-stage chain. Emitted by
        ``signal_trader._ml_signal`` before ``ml_model.predict`` is called.
        """
        if not correlation_id:
            return
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_MARKET_SNAPSHOT,
            token_id=token_id,
            strategy=strategy,
            pnl=0.0,
            **_strip_reserved_keys(snapshot),
        )

    async def record_intelligence_snapshot(
        self,
        correlation_id: str,
        token_id: str,
        snapshot: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record news/sentiment/intelligence at decision time.

        Captures the market metadata available at decision time (slug,
        volume24hr, liquidity, outstanding shares, active/closed flags,
        end_date, sentiment scores if available, etc.). Emitted by
        ``signal_trader._ml_signal`` alongside ``record_market_snapshot``.
        """
        if not correlation_id:
            return
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_INTELLIGENCE_SNAPSHOT,
            token_id=token_id,
            strategy=strategy,
            pnl=0.0,
            **_strip_reserved_keys(snapshot),
        )

    async def record_feature_snapshot(
        self,
        correlation_id: str,
        token_id: str,
        features: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record the ML features at prediction time.

        Captures the feature vector (and any feature-store metadata like
        ``feature_set_version`` / ``n_features``) that was fed to
        ``ml_model.predict``. Emitted by ``signal_trader._ml_signal`` right
        before the PREDICTION stage. The features dict is the single source
        of truth for "what the model saw" when reconstructing a trade
        decision post-hoc — without it, a SHAP / drift investigation would
        have to re-derive features from the market snapshot, which is
        lossy (feature engineering is non-reversible).
        """
        if not correlation_id:
            return
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_FEATURE_SNAPSHOT,
            token_id=token_id,
            strategy=strategy,
            pnl=0.0,
            **_strip_reserved_keys(features),
        )

    async def record_position(
        self,
        correlation_id: str,
        token_id: str,
        position: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record the position state after fill.

        Captures the post-fill Position snapshot (yes_shares /
        avg_entry_price / total_invested / opened_at / strategy / paper
        flag) so the chain shows the actual exposure the bot took on this
        decision. Emitted by ``paper_sim._execute_fill`` immediately after
        the FILL stage.
        """
        if not correlation_id:
            return
        # The position dict may carry a ``pnl`` key (e.g. when the fill
        # realised P&L on a closing SELL) — promote it to the dedicated
        # ``pnl`` column rather than letting it land in ``data_json``.
        pnl_value = 0.0
        payload = dict(position)
        if "pnl" in payload:
            try:
                pnl_value = float(payload.pop("pnl") or 0.0)
            except (TypeError, ValueError):
                pnl_value = 0.0
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_POSITION,
            token_id=token_id,
            strategy=strategy,
            pnl=pnl_value,
            **_strip_reserved_keys(payload),
        )

    async def record_outcome(
        self,
        correlation_id: str,
        token_id: str,
        outcome: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record the market resolution outcome.

        Captures the resolved market outcome (``resolved_yes`` bool,
        resolution_price, market slug, resolution timestamp) when a market
        settles. Emitted by ``settlement._process_resolved_market`` after
        the YES/NO position is settled in DataStore. Pairs with
        ``record_pnl`` — the OUTCOME event records what the market
        resolved to; the PNL event records the realised P&L that resulted.
        """
        if not correlation_id:
            return
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_OUTCOME,
            token_id=token_id,
            strategy=strategy,
            pnl=0.0,
            **_strip_reserved_keys(outcome),
        )

    async def record_pnl(
        self,
        correlation_id: str,
        token_id: str,
        pnl: dict[str, Any],
        strategy: str | None = None,
    ) -> None:
        """Record the realized P&L.

        Captures the realised P&L that resulted from the market resolution
        (realized_pnl, payout, invested_cost, shares, exit_price). Emitted
        by ``settlement._process_resolved_market`` immediately after
        ``record_outcome``. The ``pnl`` column on the row is populated
        from the dict's ``realized_pnl`` (or ``pnl``) key so the
        dashboard's "P&L per decision" aggregation query (``SELECT
        SUM(pnl) FROM decision_events WHERE stage='PNL'``) works
        out-of-the-box.
        """
        if not correlation_id:
            return
        pnl_value = 0.0
        payload = dict(pnl)
        # Promote the realised-pnl scalar to the dedicated column.
        for key in ("realized_pnl", "pnl", "pnl_amount"):
            if key in payload:
                try:
                    pnl_value = float(payload.pop(key) or 0.0)
                except (TypeError, ValueError):
                    pnl_value = 0.0
                break
        await self.record(
            decision_id=correlation_id,
            stage=STAGE_PNL,
            token_id=token_id,
            strategy=strategy,
            pnl=pnl_value,
            **_strip_reserved_keys(payload),
        )

    # ── Reads ──────────────────────────────────────────────────────────────

    @timed_query
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

    @timed_query
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

    @timed_query
    async def get_full_chain(
        self, correlation_id: str
    ) -> dict[str, dict[str, Any]]:
        """Reconstruct the complete 12-stage decision chain.

        Returns a ``{stage_name: stage_event}`` dict for the supplied
        ``correlation_id`` (a.k.a. ``decision_id`` in this codebase — the
        two names are interchangeable per the W19-3 spec). The 12 canonical
        stages are:

            MARKET_SNAPSHOT → INTELLIGENCE_SNAPSHOT → FEATURE_SNAPSHOT →
            PREDICTION → SIGNAL → RISK_APPROVED | RISK_REJECTED → ORDER →
            FILL → POSITION → OUTCOME → PNL

        Stages that have not yet been recorded for this ``correlation_id``
        are simply absent from the returned dict — callers that want to
        assert a "complete 12-stage chain" should compare the returned
        dict's key set against ``CANONICAL_STAGE_ORDER`` (or the subset of
        stages they care about; e.g. a rejected decision legitimately
        lacks ORDER / FILL / POSITION / OUTCOME / PNL).

        When the same stage name appears multiple times in the chain
        (unusual but possible — e.g. a strategy that re-records
        MARKET_SNAPSHOT after a partial fill to capture the post-fill
        book state), the LAST event wins (later timestamp). The
        chronological order is preserved by ``get_chain`` (ASC by
        timestamp, tiebroken by ``id ASC``), so iterating the dict values
        gives insertion order.

        Args:
            correlation_id: The cross-stage trace key (``decision_id``).
                An empty / falsy input returns an empty dict (no rows
                fetched, no error raised).

        Returns:
            ``dict`` keyed by stage name. Each value is the full row dict
            (``timestamp`` / ``decision_id`` / ``stage`` / ``token_id`` /
            ``strategy`` / ``pnl`` / ``data_json`` / ``data``). Empty
            dict on any persistence error or when no events exist for
            ``correlation_id``.
        """
        if not correlation_id:
            return {}
        stages = await self.get_chain(correlation_id)
        chain: dict[str, dict[str, Any]] = {}
        for stage in stages:
            # Last-write-wins for repeated stage names. The underlying
            # ``get_chain`` returns events in chronological order so the
            # latest event for any given stage overwrites earlier ones —
            # matching the dashboard's "show me the latest snapshot for
            # each stage" rendering expectation.
            chain[stage["stage"]] = stage
        return chain

    @timed_query
    async def get_latest_decision_id_for_token(
        self, token_id: str, stage: str | None = None
    ) -> str | None:
        """Return the most recent ``decision_id`` recorded for ``token_id``.

        W19-3 — used by ``core/settlement._process_resolved_market`` to
        look up the originating decision chain for a settled position
        (settlement receives only ``token_id`` from the Gamma API; the
        originating ``decision_id`` lives in this ledger). Optionally
        filtered by ``stage`` so callers can pin the lookup to a specific
        stage (e.g. ``stage=STAGE_FILL`` to find the decision chain that
        actually resulted in a filled position, ignoring earlier
        PREDICTION-only chains that never traded).

        Args:
            token_id: Polymarket condition token id.
            stage: Optional stage filter. ``None`` searches all stages.

        Returns:
            The most recent ``decision_id`` string for the token (or
            ``None`` if no events exist or the query fails). The lookup
            is best-effort: any SQLite error is logged and ``None``
            returned so the settlement pipeline never blocks on a
            ledger hiccup.
        """
        if not token_id:
            return None

        def _fetch() -> str | None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    if stage:
                        cursor.execute(
                            "SELECT decision_id FROM decision_events "
                            "WHERE token_id = ? AND stage = ? "
                            "ORDER BY timestamp DESC, id DESC LIMIT 1",
                            (token_id, stage),
                        )
                    else:
                        cursor.execute(
                            "SELECT decision_id FROM decision_events "
                            "WHERE token_id = ? "
                            "ORDER BY timestamp DESC, id DESC LIMIT 1",
                            (token_id,),
                        )
                    row = cursor.fetchone()
                    return row[0] if row else None
            except Exception as e:
                log.error(
                    "[decision_ledger] get_latest_decision_id_for_token "
                    "failed token=%s: %s",
                    token_id,
                    e,
                )
                return None

        return await asyncio.to_thread(_fetch)

    @timed_query
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

    @timed_query
    async def get_rejections_page(
        self,
        limit: int = 50,
        cursor: str | None = None,
    ) -> "Page":
        """Cursor-paginated fetch of recent rejection records.

        W16-5 — wraps :func:`core.pagination.paginate_query` against the
        ``decision_rejections`` table. The base ``SELECT`` includes the
        ``INTEGER PRIMARY KEY`` ``id`` column (which the legacy
        :meth:`get_rejections` SELECT deliberately omitted) so
        :func:`paginate_query` has a tiebreaker for rows that share a
        timestamp. The wire payload carries the SAME rejection-schema
        fields as the legacy method (``timestamp``, ``decision_id``,
        ``token_id``, ``strategy``, ``predicted_edge``, ``confidence``,
        ``reason``, ``market_mid``) plus the new ``id`` column (an
        integer; harmless for callers that ignore unknown fields, and
        useful for any future caller that wants a stable row identity).

        Args:
            limit:  Page size (clamped to ``[1, 100]`` by
                    :func:`paginate_query`). The route-level ``Query``
                    constraint allows up to 500 for backward compat.
            cursor: Opaque cursor from a previous response's
                    ``next_cursor`` field. ``None`` returns the first
                    page.

        Returns:
            :class:`core.pagination.Page` whose ``items`` are the
            rejection rows (most recent first).
        """
        from core.pagination import Page, paginate_query

        base_query = (
            "SELECT id, timestamp, decision_id, token_id, strategy, "
            "predicted_edge, confidence, reason, market_mid "
            "FROM decision_rejections WHERE 1=1"
        )

        def _fetch() -> Page:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    return paginate_query(
                        conn,
                        base_query,
                        (),
                        cursor=cursor,
                        limit=limit,
                        cursor_column="timestamp",
                        id_column="id",
                        reverse=True,
                    )
            except Exception as e:
                log.error(
                    "[decision_ledger] get_rejections_page failed: %s",
                    e,
                )
                return Page(items=[], next_cursor=None, has_more=False)

        return await asyncio.to_thread(_fetch)

    @timed_query
    async def get_prediction_history(
        self, token_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Return the most recent PREDICTION-stage events for ``token_id``.

        Surfaces the model-version lineage per token: every row carries the
        same shape as :meth:`get_chain_by_token` (with the decoded ``data``
        payload) PLUS a top-level convenience field ``model_version`` lifted
        out of ``data`` for fast dashboard / audit filtering without a
        second ``data["model_version"]`` lookup.

        Pre-V14 rows (or callers that bypassed the auto-stamp by passing
        ``model_version=None``) surface with ``model_version=None`` rather
        than being filtered out — preserving a complete prediction history
        for the token even across the V14 cutover.

        Args:
            token_id: Polymarket condition token id to filter on. An empty
                ``token_id`` returns ``[]`` (mirrors the empty-input guard
                used by :meth:`get_chain_by_token`).
            limit: Maximum number of PREDICTION events to return. Defaults
                to 10 (the recent-predictions use case surfaced by the
                operator dashboard). Clamped to ``int`` for SQL safety.

        Returns:
            list[dict]: Most-recent-first PREDICTION events. Empty list on
            any persistence error or when no PREDICTION events exist for
            ``token_id``.
        """
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
                        WHERE token_id = ? AND stage = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (token_id, STAGE_PREDICTION, int(limit)),
                    )
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error(
                    "[decision_ledger] get_prediction_history failed token=%s: %s",
                    token_id,
                    e,
                )
                return []
            for r in rows:
                r["data"] = _safe_json(r.get("data_json"))
                # Lift ``model_version`` out of the decoded payload so callers
                # can filter / group without a second dict lookup. Defaults
                # to ``None`` when absent (pre-V14 rows or callers that
                # explicitly passed ``model_version=None``).
                data = r.get("data") or {}
                r["model_version"] = data.get("model_version")
            return rows

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

      GET /api/decision/{correlation_id}/full-chain
          Full 12-stage decision chain keyed by stage name
          (W19-3 — MARKET_SNAPSHOT → INTELLIGENCE_SNAPSHOT →
          FEATURE_SNAPSHOT → PREDICTION → SIGNAL → RISK_* → ORDER → FILL →
          POSITION → OUTCOME → PNL). 404 if no events recorded.
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

    @app.get("/api/decision/{correlation_id}/full-chain", tags=["decisions"])
    async def _decision_full_chain_for_correlation_id(correlation_id: str):
        """Return the complete 12-stage decision chain keyed by stage name.

        W19-3 — closes the God Mode §51 "Why did the bot make this trade?"
        gap. Surfaces every stage recorded against ``correlation_id`` as a
        ``{stage_name: stage_event}`` dict so the caller can answer
        end-to-end questions about a decision without chaining 6+ separate
        ``get_chain`` + filter calls.

        Stages absent from the chain (e.g. POSITION / OUTCOME / PNL for a
        decision that hasn't yet filled / settled) are simply absent from
        the returned dict.
        """
        chain = await decision_ledger.get_full_chain(correlation_id)
        if not chain:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no decision chain recorded for correlation_id "
                    f"{correlation_id}"
                ),
            )
        return {
            "correlation_id": correlation_id,
            "count": len(chain),
            "stages": chain,
        }

    @app.get("/api/decisions/rejected", tags=["decisions"])
    async def _rejected_decisions(
        limit: int = Query(50, ge=1, le=500, description="Max rejections to return"),
        cursor: str | None = Query(
            None,
            description=(
                "Opaque base64 cursor from a previous response's "
                "``next_cursor`` field. Omit for the first page (newest "
                "rejections). W16-5."
            ),
        ),
    ):
        """Return recent rejected decisions (most recent first).

        W16-5 — supports cursor-based pagination via the optional
        ``cursor`` query param. When omitted, the first page is
        returned — fully backward compatible with the pre-pagination
        wire shape (``{count, rejections}`` plus the new
        ``next_cursor`` / ``has_more`` fields).
        """
        page = await decision_ledger.get_rejections_page(
            limit=limit,
            cursor=cursor,
        )
        return {
            "count": len(page.items),
            "rejections": page.items,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }


def _safe_json(raw: str | None) -> Any:
    """Best-effort JSON decode for the ``data_json`` column."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# Reserved-key set for the W19-3 ``_strip_reserved_keys`` helper. These
# are the kwargs that ``DecisionLedger.record()`` accepts by name — if a
# caller-supplied snapshot dict happens to carry one of these keys, the
# ``**`` expansion would raise
# ``TypeError: got multiple values for keyword argument``. The helper
# defensively drops them so a snapshot that (legitimately) carries its own
# ``token_id`` field (e.g. an order-book snapshot) doesn't crash the
# caller. The caller's intent (the snapshot) is preserved verbatim
# through the ``token_id`` positional arg on every ``record_*`` helper.
_RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"decision_id", "stage", "token_id", "strategy", "pnl"}
)


def _strip_reserved_keys(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` with reserved keys dropped.

    Used by the W19-3 ``record_market_snapshot`` /
    ``record_intelligence_snapshot`` / ``record_feature_snapshot`` /
    ``record_position`` / ``record_outcome`` / ``record_pnl`` helpers
    before ``**`` expansion into :meth:`DecisionLedger.record`. Defensive
    only — typical caller payloads don't carry these keys, but
    order-book / position snapshots routinely include ``token_id`` /
    ``strategy`` / ``pnl`` as data fields, and the resulting
    ``TypeError`` would propagate out of the helper and (without the
    strip) break the calling pipeline. With the strip, the snapshot's
    own copy is silently dropped in favour of the explicit positional
    args passed to ``record()``.

    Args:
        payload: The caller-supplied snapshot dict (or ``None``).

    Returns:
        A fresh dict with the same key/value pairs as ``payload`` minus
        any reserved keys. ``None`` / non-dict input returns an empty
        dict so the ``**`` expansion downstream is a no-op (no kwargs
        forwarded) rather than a ``TypeError``.
    """
    if not payload or not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS}


def _resolve_active_model_version() -> str:
    """
    Lazily import the ML model registry and return its currently-active
    version string.

    Returns ``"unknown"`` on ANY failure (import error, missing attribute,
    registry-init exception, etc.) so callers — chiefly
    :meth:`DecisionLedger.record` when stamping PREDICTION events — never
    block the trading pipeline on a registry hiccup.

    Why lazy / per-call (not at module import time):

      - ``core.decision_ledger`` is imported very early in the boot
        sequence (the singleton ``decision_ledger = DecisionLedger()`` is
        constructed at module import). Importing ``ml.model_registry``
        eagerly here would force the registry's disk-backed seed file
        (``/app/data/model_registry.json``) to be loaded — or, if the
        file is absent, force ``_load_from_disk`` to call
        ``register_version("v1.0.0", ...)`` which in turn calls
        ``_save_to_disk`` (``REGISTRY_FILE.parent.mkdir(...)``). In a
        read-only sandbox that mkdir raises ``PermissionError`` OUTSIDE
        the save's try/except — crashing the entire decision_ledger
        module import. Deferring the import to call time confines the
        blast radius to a single ``record()`` invocation (which then
        falls back to ``"unknown"``).
      - It also avoids an import-cycle risk: ``ml.model_registry`` lives
        under the ``ml/`` package which itself may transitively import
        ``core/`` modules in a future refactor.
    """
    try:
        from ml.model_registry import model_registry  # lazy import — avoids
        # circular-import / read-only-sandbox blast radius at module load.
        return model_registry.active_version
    except Exception as e:
        log.warning(
            "[decision_ledger] could not resolve active model_version "
            "(defaulting to 'unknown'): %s",
            e,
        )
        return "unknown"


__all__ = [
    "DB_PATH",
    "DecisionLedger",
    "decision_ledger",
    "register_routes",
    "timed_query",
    "STAGE_PREDICTION",
    "STAGE_SIGNAL",
    "STAGE_RISK_APPROVED",
    "STAGE_RISK_REJECTED",
    "STAGE_ORDER",
    "STAGE_FILL",
    # W19-3 — 6 new stages that close the God Mode §51 "complete decision
    # chain" gap.
    "STAGE_MARKET_SNAPSHOT",
    "STAGE_INTELLIGENCE_SNAPSHOT",
    "STAGE_FEATURE_SNAPSHOT",
    "STAGE_POSITION",
    "STAGE_OUTCOME",
    "STAGE_PNL",
    "CANONICAL_STAGE_ORDER",
    "REASON_LOW_CONFIDENCE",
    "REASON_WIDE_SPREAD",
    "REASON_NEUTRAL_ZONE",
    "REASON_INSUFFICIENT_KELLY_EDGE",
]
