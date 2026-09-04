"""
core/rejected_opportunities.py — SQLite-backed Rejected Opportunity Journal.

W22-4 — God Mode §74. Persists every opportunity the risk system rejects
so the platform can later evaluate two questions the existing trade journal
cannot answer:

  1. Did the risk system reject GOOD trades? (missed P&L — counterfactual
     ``would_have_pnl`` populated when the market resolves, exposed via
     the ``resolved_opportunities`` roll-up in ``get_analytics``).
  2. Did the system correctly AVOID bad opportunities? (saved capital —
     the same ``would_have_pnl`` column read as "how much we'd have
     lost had we traded this"). The two questions are answered by the
     SAME column — a negative ``would_have_pnl`` means the rejection
     SAVED capital; a positive value means the rejection COST P&L.

The store is wired into ``risk/manager.InstitutionalRiskEngine.check_order``
exactly the same way ``core.shadow_trading.record_shadow_trade`` is wired:
on any ``return False, reason`` path inside ``_check_order_impl``, the
public ``check_order`` wrapper schedules an async fire-and-forget recording
via ``asyncio.create_task(record_rejected_opportunity(...))``. The
recording is wrapped in ``try/except: pass`` so it can never alter the
rejection return value or block the caller — mirrors the contract on
``core.shadow_trading.record_shadow_trade``, ``core.decision_ledger.record``,
and ``core.closed_positions.record_closed_position``.

The store complements — does NOT replace — the existing decision-ledger
``RISK_REJECTED`` stage (``core/decision_ledger.py``). The decision ledger
records the rejection event itself for audit trail reconstruction
(``get_full_chain(decision_id)``); this store records the SIGNAL fields
(price, size, predicted_edge, confidence) + the rejection reason + a
back-fillable ``market_outcome`` / ``would_have_pnl`` so post-hoc
counterfactual analytics ("how much did the risk system leave on the
table?") can be computed without re-joining the ledger against the
market-resolution tables.

Schema (additive — independent SQLite db co-resident with the decision
ledger so the audit-trail immutability contract is not perturbed; mirrors
the ``core/shadow_trading.py`` + ``core/closed_positions.py`` async +
``asyncio.to_thread`` convention so the databases coexist without schema
contention)::

    rejected_opportunities (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp                 REAL    NOT NULL,             -- epoch seconds
        token_id                  TEXT    NOT NULL,
        strategy                  TEXT,
        signal_action             TEXT,                          -- BUY / SELL
        signal_price              REAL,                          -- intended limit price
        signal_size               REAL,                          -- intended trade size
        predicted_edge            REAL,                          -- p_yes − market_mid
        confidence                REAL,                          -- ML confidence [0..1]
        rejection_reason          TEXT    NOT NULL,              -- short slug
        rejection_details        TEXT,                          -- JSON: full message + extras
        market_price_at_rejection REAL,                         -- order book mid at reject time
        market_outcome            REAL,                          -- 1 (YES) / 0 (NO) — back-filled
        would_have_pnl            REAL,                          -- counterfactual P&L — back-filled
        correlation_id            TEXT                           -- decision_id cross-ref
    )

Indexes:
    (token_id)                — back-fill by token_id when a market resolves
    (rejection_reason)         — GROUP BY reason for analytics
    (timestamp DESC)           — most-recent-first global feed

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at
startup to expose::

    GET /api/rejected-opportunities             recent rejections (filterable)
    GET /api/rejected-opportunities/analytics   aggregate analytics roll-up

The settlement layer (``core/settlement.py``) is the canonical call site
for ``update_outcome(token_id, final_price, outcome)`` — when a market
resolves, every rejected opportunity on that token_id is back-filled with
its ``market_outcome`` and ``would_have_pnl`` so the analytics endpoint
can answer the two governing questions above. (W22-4 leaves the
settlement-layer call as a documented next step — the store is
operational and the API surface is live; settlement wiring is the
subject of a follow-up task so this commit stays single-purpose.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── DB_PATH ──────────────────────────────────────────────────────────────────
# Per the God Mode §74 spec, the rejected-opportunities store co-resides in
# the SAME directory as the canonical decision ledger (the env var
# ``DECISION_LEDGER_DB_PATH`` names a *file*, so we take its parent and
# append ``rejected_opportunities.db``). Mirrors the ``core/shadow_trading.py``
# line-71-74 convention so every decision-derived artefact (stage events,
# shadow trades, rejected opportunities) lives under one configurable root
# while remaining in a separate db file so the decision ledger's
# immutability contract is not perturbed.
#
# An operator can override the path explicitly via
# ``REJECTED_OPPORTUNITIES_DB_PATH`` (mirrors the override convention on
# ``CLOSED_POSITIONS_DB_PATH`` / ``EXECUTION_QUALITY_DB_PATH`` /
# ``OBSERVABILITY_DB_PATH``). When the override is unset — the common
# case — the path inherits the conftest ``DECISION_LEDGER_DB_PATH``
# redirect automatically, so the test suite runs hermetic to ``/tmp``
# without any conftest.py edit (per the W22-4 "Do NOT edit existing
# files" constraint on tests/conftest.py).
_REJECTED_OPP_OVERRIDE = os.environ.get("REJECTED_OPPORTUNITIES_DB_PATH")
if _REJECTED_OPP_OVERRIDE:
    DB_PATH: Path = Path(_REJECTED_OPP_OVERRIDE)
else:
    _DECISION_LEDGER_DB_PATH = Path(
        os.environ.get("DECISION_LEDGER_DB_PATH", "/app/data/decision_ledger.db")
    )
    DB_PATH = _DECISION_LEDGER_DB_PATH.parent / "rejected_opportunities.db"


# ── Rejection reason vocabulary ──────────────────────────────────────────────
# Centralised slug vocabulary so the API / dashboards can map reason codes
# to human-readable copy without coupling to the risk-manager message
# strings. The risk layer returns English sentences like "Daily loss stop
# reached ($2.00)"; ``_categorize_reason`` maps each known message pattern
# to a short slug so the analytics roll-up groups by ``daily_loss_stop``
# rather than by 100 distinct "$N.NN" interpolated strings.
#
# The mapping is intentionally defensive: any unmapped message falls back
# to ``"other"`` so the roll-up never silently drops a category. The slug
# is stored in ``rejection_reason`` (the GROUP BY column); the original
# English message is preserved verbatim in ``rejection_details`` JSON.
_REASON_KILL_SWITCH = "kill_switch"
_REASON_SHADOW_MODE = "shadow_mode"
_REASON_OBSERVATION_ONLY = "observation_only"
_REASON_EXPOSURE_NOT_RECONCILED = "exposure_not_reconciled"
_REASON_LIVE_TRADING_DISABLED = "live_trading_disabled"
_REASON_STRATEGY_COOLDOWN = "strategy_cooldown"
_REASON_DAILY_LOSS_STOP = "daily_loss_stop"
_REASON_WEEKLY_LOSS_STOP = "weekly_loss_stop"
_REASON_MAX_DRAWDOWN = "max_drawdown"
_REASON_CASH_RESERVE = "cash_reserve"
_REASON_TOTAL_OPEN_RISK = "total_open_risk"
_REASON_PER_MARKET_CAP = "per_market_cap"
_REASON_ABSOLUTE_POSITION_CAP = "absolute_position_cap"
_REASON_NORMAL_POSITION_CAP = "normal_position_cap"
_REASON_STRATEGY_EXPOSURE = "strategy_exposure"
_REASON_CORRELATED_EXPOSURE = "correlated_exposure"
_REASON_MTM_EXPOSURE = "mtm_exposure"
_REASON_MTM_GATE_FAILED = "mtm_gate_failed"
_REASON_MAX_OPEN_POSITIONS = "max_open_positions"
_REASON_PENDING_ORDER_CAPITAL = "pending_order_capital"
_REASON_MAX_OPEN_ORDERS = "max_open_orders"
_REASON_INVALID_PRICE = "invalid_price"
_REASON_INSUFFICIENT_SIZE = "insufficient_size"
_REASON_BANKROLL_CEILING = "bankroll_ceiling"
_REASON_OTHER = "other"

# Order matters: more-specific patterns first so e.g. "Mark-to-market
# exposure" doesn't shadow "MTM risk gate failed closed" (both contain
# the substring "MTM"). Each entry is ``(regex, slug)``.
_REASON_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"shadow trading mode", re.IGNORECASE), _REASON_SHADOW_MODE),
    (re.compile(r"kill switch", re.IGNORECASE), _REASON_KILL_SWITCH),
    (re.compile(r"observation-only", re.IGNORECASE), _REASON_OBSERVATION_ONLY),
    (re.compile(r"exposure not reconciled", re.IGNORECASE), _REASON_EXPOSURE_NOT_RECONCILED),
    (re.compile(r"live trading is disabled", re.IGNORECASE), _REASON_LIVE_TRADING_DISABLED),
    (re.compile(r"per-trade-loss cooldown", re.IGNORECASE), _REASON_STRATEGY_COOLDOWN),
    (re.compile(r"daily loss stop", re.IGNORECASE), _REASON_DAILY_LOSS_STOP),
    (re.compile(r"weekly loss stop", re.IGNORECASE), _REASON_WEEKLY_LOSS_STOP),
    (re.compile(r"max drawdown", re.IGNORECASE), _REASON_MAX_DRAWDOWN),
    (re.compile(r"cash reserve", re.IGNORECASE), _REASON_CASH_RESERVE),
    (re.compile(r"total open risk cap", re.IGNORECASE), _REASON_TOTAL_OPEN_RISK),
    (re.compile(r"absolute position cap", re.IGNORECASE), _REASON_ABSOLUTE_POSITION_CAP),
    (re.compile(r"normal position cap", re.IGNORECASE), _REASON_NORMAL_POSITION_CAP),
    (re.compile(r"per-market position cap", re.IGNORECASE), _REASON_PER_MARKET_CAP),
    (re.compile(r"strategy exposure cap", re.IGNORECASE), _REASON_STRATEGY_EXPOSURE),
    (re.compile(r"correlated exposure cap", re.IGNORECASE), _REASON_CORRELATED_EXPOSURE),
    (re.compile(r"MTM risk gate failed", re.IGNORECASE), _REASON_MTM_GATE_FAILED),
    (re.compile(r"mark-to-market exposure", re.IGNORECASE), _REASON_MTM_EXPOSURE),
    (re.compile(r"max simultaneous open positions", re.IGNORECASE), _REASON_MAX_OPEN_POSITIONS),
    (re.compile(r"pending order capital cap", re.IGNORECASE), _REASON_PENDING_ORDER_CAPITAL),
    (re.compile(r"max open orders", re.IGNORECASE), _REASON_MAX_OPEN_ORDERS),
    (re.compile(r"out of valid bounds", re.IGNORECASE), _REASON_INVALID_PRICE),
    (re.compile(r"below minimum liquidity", re.IGNORECASE), _REASON_INSUFFICIENT_SIZE),
    (re.compile(r"bankroll ceiling", re.IGNORECASE), _REASON_BANKROLL_CEILING),
]


def _categorize_reason(raw_reason: str) -> str:
    """Map a free-text risk-manager rejection message to a short slug.

    Returns one of the ``_REASON_*`` constants — never raises (an unmapped
    message falls back to ``_REASON_OTHER`` so the analytics roll-up never
    silently drops a category).
    """
    if not raw_reason:
        return _REASON_OTHER
    try:
        for pattern, slug in _REASON_PATTERNS:
            if pattern.search(raw_reason):
                return slug
    except Exception:  # noqa: BLE001 — defensive: regex must never raise
        pass
    return _REASON_OTHER


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass
class RejectedOpportunity:
    """A single opportunity the risk system rejected.

    The dataclass is the on-the-wire shape ``record`` accepts. The
    ``market_outcome`` and ``would_have_pnl`` fields are ``None`` at
    record time and back-filled later via ``update_outcome`` when the
    market resolves — so the store can answer both governing questions
    (did the risk system reject good trades? did it correctly avoid bad
    ones?) from the SAME column once the market settles.
    """

    timestamp: float
    token_id: str
    strategy: str
    signal_action: str  # "BUY" / "SELL"
    signal_price: float
    signal_size: float
    predicted_edge: float
    confidence: float
    rejection_reason: str  # short slug (e.g. "daily_loss_stop")
    rejection_details: dict
    market_price_at_rejection: Optional[float]  # None when no order-book mid available
    market_outcome: Optional[float] = None  # 1 (YES) / 0 (NO) — back-filled
    would_have_pnl: Optional[float] = None  # counterfactual P&L — back-filled


# ── Schema ───────────────────────────────────────────────────────────────────

def _init_db() -> None:
    """Create the ``rejected_opportunities`` table + indexes. Safe on every boot.

    Mirrors ``core/shadow_trading._init_db`` and
    ``core.closed_positions._init_db`` — module-level singleton initialiser
    called at import time so the store is ready the moment any caller
    imports the module. Failures are logged at ``error`` level and
    swallowed so a transient FS issue can never break the trading
    pipeline's import path.
    """
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS rejected_opportunities (
                    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp                   REAL    NOT NULL,
                    token_id                    TEXT    NOT NULL,
                    strategy                    TEXT,
                    signal_action               TEXT,
                    signal_price                REAL,
                    signal_size                 REAL,
                    predicted_edge              REAL,
                    confidence                  REAL,
                    rejection_reason            TEXT    NOT NULL,
                    rejection_details           TEXT,
                    market_price_at_rejection   REAL,
                    market_outcome              REAL,
                    would_have_pnl              REAL,
                    correlation_id              TEXT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ro_token "
                "ON rejected_opportunities(token_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ro_reason "
                "ON rejected_opportunities(rejection_reason)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ro_ts "
                "ON rejected_opportunities(timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ro_corr "
                "ON rejected_opportunities(correlation_id)"
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 — defensive: import must never crash
        log.error("[rejected_opportunities] init failed (%s): %s", DB_PATH, e)


# Initialise on module import — mirrors the ``decision_ledger`` /
# ``shadow_trading`` / ``closed_positions`` convention so the store is
# ready the moment any caller imports the module.
_init_db()


# ── Store ─────────────────────────────────────────────────────────────────────

# Sentinel for "no explicit db_path override — use the module global
# (read at call time so monkeypatching ``core.rejected_opportunities.DB_PATH``
# from tests is honoured even when the singleton was constructed before
# the patch — mirrors the call-time lookup that ``core.shadow_trading``
# does on every function via the bare ``DB_PATH`` global reference).
_DEFAULT_SENTINEL: Any = object()


class RejectedOpportunityStore:
    """Asynchronous, SQLite-backed rejected-opportunity journal.

    All writes are fire-and-forget from the caller's perspective: every
    public method swallows its own persistence errors (logged at
    ``error`` level) so a store hiccup can never break the trading
    pipeline. Reads return plain ``list[dict]`` rows (most recent first
    where applicable) or a zeroed-out analytics payload on failure —
    the caller never sees a 500 from a store read.

    Mirrors the contract on ``core.shadow_trading`` and
    ``core.closed_positions.ClosedPositionsStore``.
    """

    def __init__(self, db_path: Path | Any = _DEFAULT_SENTINEL) -> None:
        # Allow callers (tests / operators) to override the DB path on a
        # per-instance basis. When unset (sentinel), falls back to the
        # module-level ``DB_PATH`` global at *call time* — the same
        # lookup path every public function in ``core.shadow_trading``
        # uses (each function resolves ``DB_PATH`` from the module
        # namespace at *call time*, not at import time).
        self._db_path_override: Path | None = (
            db_path if db_path is not _DEFAULT_SENTINEL else None
        )
        # Re-init the schema on the override path so the table exists
        # even if the module-import-time ``_init_db()`` ran against the
        # conftest-redirected default path. No-op when the path is the
        # default (CREATE TABLE IF NOT EXISTS is idempotent).
        if self._db_path_override is not None:
            try:
                self._db_path_override.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(self._db_path_override) as conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS rejected_opportunities (
                            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp                   REAL    NOT NULL,
                            token_id                    TEXT    NOT NULL,
                            strategy                    TEXT,
                            signal_action               TEXT,
                            signal_price                REAL,
                            signal_size                 REAL,
                            predicted_edge              REAL,
                            confidence                  REAL,
                            rejection_reason            TEXT    NOT NULL,
                            rejection_details           TEXT,
                            market_price_at_rejection   REAL,
                            market_outcome              REAL,
                            would_have_pnl              REAL,
                            correlation_id              TEXT
                        );
                        CREATE INDEX IF NOT EXISTS idx_ro_token
                            ON rejected_opportunities(token_id);
                        CREATE INDEX IF NOT EXISTS idx_ro_reason
                            ON rejected_opportunities(rejection_reason);
                        CREATE INDEX IF NOT EXISTS idx_ro_ts
                            ON rejected_opportunities(timestamp DESC);
                        CREATE INDEX IF NOT EXISTS idx_ro_corr
                            ON rejected_opportunities(correlation_id);
                        """
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[rejected_opportunities] per-instance init failed (%s): %s",
                    self._db_path_override, e,
                )

    @property
    def db_path(self) -> Path:
        """Resolve the DB path at call time so monkeypatching ``DB_PATH``
        mid-test (mirror of ``core.shadow_trading`` test isolation) is
        honoured even when the singleton was constructed before the patch.

        Returns the per-instance override (if any) or the current value
        of the module-level ``DB_PATH`` global — Python's name-lookup
        semantics resolve ``DB_PATH`` to the live module attribute at
        every call, so ``monkeypatch.setattr("core.rejected_opportunities
        .DB_PATH", new_path)`` is picked up automatically.
        """
        if self._db_path_override is not None:
            return self._db_path_override
        return DB_PATH

    # ── Writes ────────────────────────────────────────────────────────────

    async def record(
        self,
        opp: RejectedOpportunity,
        correlation_id: str | None = None,
    ) -> int | None:
        """Persist a single rejected opportunity.

        Args:
            opp: the ``RejectedOpportunity`` dataclass carrying every
                caller-supplied field (``token_id``, ``strategy``,
                ``signal_action``, ``signal_price``, ``signal_size``,
                ``predicted_edge``, ``confidence``, ``rejection_reason``,
                ``rejection_details``, ``market_price_at_rejection``).
            correlation_id: optional cross-reference to the unified
                decision ledger (``decision_id``). Empty / ``None`` is
                stored as ``NULL`` so legacy / manual rejections without
                a decision-id are still recordable.

        ``rejection_reason`` is normalised via ``_categorize_reason``
        BEFORE the insert so the GROUP BY column is always a short slug
        (e.g. ``"daily_loss_stop"``), never a free-text interpolated
        sentence. The original message is preserved verbatim inside
        ``rejection_details`` JSON under the ``"raw_message"`` key.

        Persistence failures are logged at ``error`` level and swallowed
        — the trading pipeline never blocks on a store write (same
        fire-and-forget contract as ``decision_ledger.record``,
        ``shadow_trading.record_shadow_trade``, and
        ``closed_positions.record_closed_position``).

        Returns the inserted row ``id`` (or ``None`` on failure) so
        callers can cross-link the row to other ledgers if desired.
        """
        ts = float(opp.timestamp) if opp.timestamp is not None else time.time()
        token_id_s = str(opp.token_id or "")
        strategy_s = str(opp.strategy or "")
        action_s = _normalise_side(opp.signal_action)
        price_f = _safe_float(opp.signal_price)
        size_f = _safe_float(opp.signal_size)
        edge_f = _safe_float(opp.predicted_edge)
        conf_f = _safe_float(opp.confidence)
        reason_slug = _categorize_reason(opp.rejection_reason)
        # If the caller already passed a short slug (no spaces), use it
        # verbatim — they're an informed caller (e.g. a unit test or a
        # bespoke reject path) and we shouldn't second-guess the slug.
        if opp.rejection_reason and " " not in opp.rejection_reason.strip():
            reason_slug = opp.rejection_reason.strip()

        details_dict = dict(opp.rejection_details) if opp.rejection_details else {}
        # Preserve the original message verbatim so the audit trail
        # keeps the human-readable reason even after we group by the
        # short slug.
        if opp.rejection_reason and "raw_message" not in details_dict:
            details_dict["raw_message"] = opp.rejection_reason
        details_json = json.dumps(details_dict, default=str) if details_dict else None

        market_price_f = _safe_float(opp.market_price_at_rejection)
        corr_id = str(correlation_id) if correlation_id else None
        row_id: int | None = None

        db_path = self.db_path

        def _insert() -> None:
            nonlocal row_id
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO rejected_opportunities
                        (timestamp, token_id, strategy, signal_action, signal_price,
                         signal_size, predicted_edge, confidence, rejection_reason,
                         rejection_details, market_price_at_rejection, market_outcome,
                         would_have_pnl, correlation_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts, token_id_s, strategy_s, action_s, price_f, size_f,
                            edge_f, conf_f, reason_slug, details_json, market_price_f,
                            opp.market_outcome, opp.would_have_pnl, corr_id,
                        ),
                    )
                    conn.commit()
                    row_id = cursor.lastrowid
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[rejected_opportunities] record failed token=%s strategy=%s reason=%s: %s",
                    token_id_s, strategy_s, reason_slug, e,
                )

        await asyncio.to_thread(_insert)
        return row_id

    async def update_outcome(
        self,
        token_id: str,
        final_price: float,
        outcome: int,
    ) -> int:
        """Back-fill ``market_outcome`` + ``would_have_pnl`` for every
        unresolved rejected opportunity on ``token_id``.

        Called by the settlement layer (``core/settlement.py``) when a
        market resolves — every prior rejection on that token is updated
        with the counterfactual P&L it WOULD have produced had the
        order been allowed to fill at ``signal_price``.

        The counterfactual P&L formula mirrors the one in the spec
        template::

            BUY : would_have_pnl = (final_price - signal_price) * signal_size
            SELL: would_have_pnl = (signal_price - final_price) * signal_size

        A ``BUY`` at 0.40 that resolves to 1.00 yields
        ``(1.00 - 0.40) * size = +0.60 * size`` — a positive
        ``would_have_pnl`` means the risk system COST P&L by rejecting
        the trade (a missed winner). A negative value means the
        rejection SAVED capital (correctly avoided a losing trade).

        Args:
            token_id: the market that resolved.
            final_price: final price at resolution (typically 0.0 or
                1.0 for binary markets, but the formula generalises to
                any resolution price).
            outcome: ``1`` (YES resolved) or ``0`` (NO resolved).

        Returns the number of rows updated (so the settlement layer can
        log "back-filled N rejected opportunities for token X"). Zero on
        error or when no unresolved rejections exist for the token.
        """
        token_id_s = str(token_id or "")
        final_price_f = _safe_float(final_price)
        if final_price_f is None:
            log.warning(
                "[rejected_opportunities] update_outcome: final_price not coercible (%r); skipping token=%s",
                final_price, token_id_s,
            )
            return 0
        outcome_i = int(outcome) if outcome is not None else None
        updated = 0
        db_path = self.db_path

        def _update() -> None:
            nonlocal updated
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    rows = cursor.execute(
                        """
                        SELECT id, signal_action, signal_price, signal_size
                        FROM rejected_opportunities
                        WHERE token_id = ? AND market_outcome IS NULL
                        """,
                        (token_id_s,),
                    ).fetchall()

                    for id_, action, sig_price, sig_size in rows:
                        sig_price_f = _safe_float(sig_price)
                        sig_size_f = _safe_float(sig_size)
                        if sig_price_f is None or sig_size_f is None:
                            # Can't compute counterfactual without
                            # signal price/size — skip this row but
                            # continue with the rest. The row stays
                            # NULL so a later fix-up pass can pick it
                            # up if the caller ever back-fills the
                            # missing fields.
                            continue
                        action_str = (action or "").upper()
                        if action_str == "SELL":
                            would_have_pnl = (sig_price_f - final_price_f) * sig_size_f
                        else:
                            # BUY or any unknown action treated as BUY
                            # (the BUY side is the default the risk
                            # gate rejects on; SELL rejections are
                            # rare and explicitly handled above).
                            would_have_pnl = (final_price_f - sig_price_f) * sig_size_f

                        cursor.execute(
                            """
                            UPDATE rejected_opportunities
                            SET market_outcome = ?, would_have_pnl = ?
                            WHERE id = ?
                            """,
                            (outcome_i, float(would_have_pnl), id_),
                        )
                        updated += 1
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[rejected_opportunities] update_outcome failed token=%s: %s",
                    token_id_s, e,
                )

        await asyncio.to_thread(_update)
        if updated:
            log.info(
                "[rejected_opportunities] updated %d rejected opportunities for token=%s",
                updated, token_id_s,
            )
        return updated

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get_recent(
        self,
        limit: int = 50,
        reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent rejected opportunities (most recent first).

        Args:
            limit: max rows to return (clamped to ``[1, 1000]``).
            reason: optional filter on ``rejection_reason`` slug. ``None``
                / ``""`` returns across all reasons.

        Returns a list of plain ``dict`` rows mirroring the
        ``rejected_opportunities`` schema. The ``rejection_details``
        column is decoded from JSON to a dict when present (so the
        caller doesn't have to re-parse). Empty list on error (the
        caller never sees a 500 — consistent with the read-path
        contract on ``shadow_trading`` and ``closed_positions``).
        """
        limit = max(1, min(1000, int(limit)))
        if reason is not None:
            reason = str(reason).strip() or None

        db_path = self.db_path

        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    if reason:
                        cursor.execute(
                            """
                            SELECT * FROM rejected_opportunities
                            WHERE rejection_reason = ?
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            (reason, limit),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT * FROM rejected_opportunities
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            (limit,),
                        )
                    rows = [dict(r) for r in cursor.fetchall()]
                # Decode the JSON ``rejection_details`` column on the
                # way out so the caller gets a structured dict, not a
                # raw JSON string.
                for r in rows:
                    raw = r.get("rejection_details")
                    if raw:
                        try:
                            r["rejection_details"] = json.loads(raw)
                        except Exception:  # noqa: BLE001 — defensive
                            # Leave the raw string in place; the
                            # caller can re-parse if needed.
                            pass
                    else:
                        r["rejection_details"] = None
                return rows
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[rejected_opportunities] get_recent failed: %s", e
                )
                return []

        return await asyncio.to_thread(_fetch)

    async def get_analytics(self, hours: float = 24) -> dict[str, Any]:
        """Aggregate analytics over the trailing ``hours`` window.

        Returns a payload with four top-level keys::

            {
              "total_rejections":      int,
              "by_reason":             [{"rejection_reason": str,
                                         "count": int,
                                         "avg_edge": float}, ...],
              "by_strategy":           [{"strategy": str,
                                         "count": int,
                                         "avg_edge": float}, ...],
              "resolved_opportunities": {
                  "total":                int,
                  "would_have_won":       int,     # would_have_pnl > 0
                  "total_would_have_pnl":  float,   # sum of would_have_pnl
                  "avg_would_have_pnl":    float,   # mean of would_have_pnl
              },
              "period_hours":          float,
            }

        ``total_rejections`` counts every rejection in the trailing
        window (resolved or not). ``resolved_opportunities`` aggregates
        ONLY the rows where ``would_have_pnl IS NOT NULL`` (i.e. the
        market resolved and the counterfactual P&L has been
        back-filled). A positive ``total_would_have_pnl`` means the
        risk system COST P&L over the window (rejected more winners
        than losers); a negative value means it SAVED capital
        (rejected more losers than winners).

        On any error the function returns a zeroed-out payload (so the
        HTTP handler never 500s) — mirrors the ``_empty_stats``
        contract on ``core.closed_positions``.
        """
        try:
            hours_f = float(hours)
        except (TypeError, ValueError):
            hours_f = 24.0
        if hours_f < 0:
            hours_f = 0.0
        cutoff = time.time() - hours_f * 3600.0
        db_path = self.db_path

        def _fetch() -> dict[str, Any]:
            empty_resolved = {
                "total": 0,
                "would_have_won": 0,
                "total_would_have_pnl": 0.0,
                "avg_would_have_pnl": 0.0,
            }
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()

                    total = cursor.execute(
                        "SELECT COUNT(*) FROM rejected_opportunities WHERE timestamp > ?",
                        (cutoff,),
                    ).fetchone()[0]

                    by_reason = cursor.execute(
                        """
                        SELECT rejection_reason,
                               COUNT(*)                  AS count,
                               COALESCE(AVG(predicted_edge), 0.0) AS avg_edge
                        FROM rejected_opportunities
                        WHERE timestamp > ?
                        GROUP BY rejection_reason
                        ORDER BY count DESC
                        """,
                        (cutoff,),
                    ).fetchall()

                    by_strategy = cursor.execute(
                        """
                        SELECT strategy,
                               COUNT(*)                  AS count,
                               COALESCE(AVG(predicted_edge), 0.0) AS avg_edge
                        FROM rejected_opportunities
                        WHERE timestamp > ?
                          AND strategy IS NOT NULL AND strategy <> ''
                        GROUP BY strategy
                        ORDER BY count DESC
                        """,
                        (cutoff,),
                    ).fetchall()

                    resolved = cursor.execute(
                        """
                        SELECT
                            COUNT(*)                                            AS total,
                            COALESCE(SUM(CASE WHEN would_have_pnl > 0 THEN 1 ELSE 0 END), 0) AS would_have_won,
                            COALESCE(SUM(would_have_pnl), 0.0)                  AS total_would_have_pnl,
                            COALESCE(AVG(would_have_pnl), 0.0)                  AS avg_would_have_pnl
                        FROM rejected_opportunities
                        WHERE timestamp > ? AND would_have_pnl IS NOT NULL
                        """,
                        (cutoff,),
                    ).fetchone()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[rejected_opportunities] get_analytics failed: %s", e
                )
                return {
                    "total_rejections": 0,
                    "by_reason": [],
                    "by_strategy": [],
                    "resolved_opportunities": empty_resolved,
                    "period_hours": hours_f,
                }

            return {
                "total_rejections": int(total or 0),
                "by_reason": [
                    {
                        "rejection_reason": r["rejection_reason"],
                        "count": int(r["count"]),
                        "avg_edge": float(r["avg_edge"] or 0.0),
                    }
                    for r in by_reason
                ],
                "by_strategy": [
                    {
                        "strategy": r["strategy"],
                        "count": int(r["count"]),
                        "avg_edge": float(r["avg_edge"] or 0.0),
                    }
                    for r in by_strategy
                ],
                "resolved_opportunities": {
                    "total": int(resolved["total"]) if resolved else 0,
                    "would_have_won": int(resolved["would_have_won"]) if resolved else 0,
                    "total_would_have_pnl": float(resolved["total_would_have_pnl"]) if resolved else 0.0,
                    "avg_would_have_pnl": float(resolved["avg_would_have_pnl"]) if resolved else 0.0,
                },
                "period_hours": hours_f,
            }

        return await asyncio.to_thread(_fetch)


# Module-level singleton — mirrors the convention on
# ``core.closed_positions.closed_positions`` and
# ``core.shadow_trading`` (the latter uses module-level functions instead
# of a class instance, but the singleton-on-import contract is the same).
rejected_opportunities_store = RejectedOpportunityStore()


# ── Convenience function ──────────────────────────────────────────────────────
# Mirror of ``core.shadow_trading.record_shadow_trade`` — accepts primitive
# kwargs (not a dataclass) so the risk-manager wiring is a one-liner. The
# ``rejection_reason`` arg accepts EITHER a short slug ("daily_loss_stop")
# OR a free-text risk-manager message ("Daily loss stop reached ($2.00)") —
# the slug is derived via ``_categorize_reason`` when the input contains
# spaces, used verbatim otherwise.

async def record_rejected_opportunity(
    token_id: str,
    strategy: str,
    signal_action: str,
    signal_price: float,
    signal_size: float,
    predicted_edge: float,
    confidence: float,
    rejection_reason: str,
    rejection_details: dict | None = None,
    market_price_at_rejection: float | None = None,
    correlation_id: str | None = None,
    timestamp: float | None = None,
) -> int | None:
    """Persist a single rejected opportunity (kwargs-style convenience API).

    Wraps ``RejectedOpportunityStore.record`` so the risk-manager wiring
    is a one-liner — mirrors ``core.shadow_trading.record_shadow_trade``
    in shape and contract.

    Args mirror the ``RejectedOpportunity`` dataclass fields; see the
    dataclass docstring for field semantics.

    Returns the inserted row ``id`` (or ``None`` on failure).
    """
    opp = RejectedOpportunity(
        timestamp=timestamp if timestamp is not None else time.time(),
        token_id=token_id,
        strategy=strategy,
        signal_action=signal_action,
        signal_price=signal_price,
        signal_size=signal_size,
        predicted_edge=predicted_edge,
        confidence=confidence,
        rejection_reason=rejection_reason,
        rejection_details=rejection_details or {},
        # Pass ``None`` through so SQLite stores NULL when no order-book
        # mid was available at reject time (mirrors the spec template's
        # ``market_price_at_rejection`` slot, which is back-fillable
        # alongside ``market_outcome`` / ``would_have_pnl`` if a later
        # reconciliation pass recovers the mid).
        market_price_at_rejection=market_price_at_rejection,
    )
    return await rejected_opportunities_store.record(opp, correlation_id=correlation_id)


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """Append rejected-opportunity inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing
    ``enforce_api_auth`` middleware — neither path is in
    ``PUBLIC_PATHS``):

      GET /api/rejected-opportunities
          Recent rejected opportunities (most recent first). Query params:

          - ``limit`` (1..1000, default 50) — max rows to return
          - ``reason`` (optional)          — filter to a single reason slug

          Returns ``{count, opportunities[]}`` — each row carries the
          raw ``rejected_opportunities`` columns (``timestamp``,
          ``token_id``, ``strategy``, ``signal_action``, ``signal_price``,
          ``signal_size``, ``predicted_edge``, ``confidence``,
          ``rejection_reason``, ``rejection_details`` (decoded JSON),
          ``market_price_at_rejection``, ``market_outcome``,
          ``would_have_pnl``, ``correlation_id``).

      GET /api/rejected-opportunities/analytics
          Aggregate analytics roll-up over the trailing ``hours`` window
          (default 24). Query param:

          - ``hours`` (0..720, default 24) — trailing window in hours

          Returns the dict shape documented on
          ``RejectedOpportunityStore.get_analytics``.
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/rejected-opportunities", tags=["analytics"])
    async def _list_rejected_opportunities(
        limit: int = Query(50, ge=1, le=1000, description="Max rejections to return"),
        reason: str | None = Query(None, description="Filter by rejection_reason slug"),
    ):
        """Return recent rejected opportunities (most recent first)."""
        rows = await rejected_opportunities_store.get_recent(limit=limit, reason=reason)
        return {"count": len(rows), "opportunities": rows}

    @app.get("/api/rejected-opportunities/analytics", tags=["analytics"])
    async def _rejected_opportunities_analytics(
        hours: float = Query(24.0, ge=0.0, le=720.0, description="Trailing window in hours"),
    ):
        """Aggregate analytics over the trailing ``hours`` window."""
        return await rejected_opportunities_store.get_analytics(hours=hours)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise_side(side: Any) -> str:
    """Normalise ``side`` to an upper-case string ("BUY" / "SELL" / "").

    Accepts plain strings ("buy", "BUY"), ``Side.BUY``-style enums (reads
    ``.value`` when present), and ``None`` (returns ""). Non-string,
    non-enum inputs are stringified and upper-cased as a fallback.
    Mirrors ``core.shadow_trading._normalise_side`` verbatim so the two
    stores can share the same side-input contract.
    """
    try:
        if hasattr(side, "value"):
            return str(side.value or "").upper()
        return str(side or "").upper()
    except Exception:  # noqa: BLE001
        return ""


def _safe_float(v: Any) -> float | None:
    """Coerce to float; return ``None`` on failure (so SQLite stores NULL).

    Mirrors ``core.shadow_trading._safe_float`` and
    ``core.closed_positions._safe_float`` — same NaN check, same
    TypeError/ValueError swallow, same ``None``-passthrough.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check
        return None
    return f


__all__ = [
    "DB_PATH",
    "RejectedOpportunity",
    "RejectedOpportunityStore",
    "rejected_opportunities_store",
    "record_rejected_opportunity",
    "register_routes",
    # Reason vocabulary — exported so dashboards / API consumers can
    # map the slugs back to human-readable copy without coupling to the
    # risk-manager message strings.
    "_REASON_KILL_SWITCH",
    "_REASON_SHADOW_MODE",
    "_REASON_OBSERVATION_ONLY",
    "_REASON_EXPOSURE_NOT_RECONCILED",
    "_REASON_LIVE_TRADING_DISABLED",
    "_REASON_STRATEGY_COOLDOWN",
    "_REASON_DAILY_LOSS_STOP",
    "_REASON_WEEKLY_LOSS_STOP",
    "_REASON_MAX_DRAWDOWN",
    "_REASON_CASH_RESERVE",
    "_REASON_TOTAL_OPEN_RISK",
    "_REASON_PER_MARKET_CAP",
    "_REASON_ABSOLUTE_POSITION_CAP",
    "_REASON_NORMAL_POSITION_CAP",
    "_REASON_STRATEGY_EXPOSURE",
    "_REASON_CORRELATED_EXPOSURE",
    "_REASON_MTM_EXPOSURE",
    "_REASON_MTM_GATE_FAILED",
    "_REASON_MAX_OPEN_POSITIONS",
    "_REASON_PENDING_ORDER_CAPITAL",
    "_REASON_MAX_OPEN_ORDERS",
    "_REASON_INVALID_PRICE",
    "_REASON_INSUFFICIENT_SIZE",
    "_REASON_BANKROLL_CEILING",
    "_REASON_OTHER",
]
