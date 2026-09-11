"""
core/shadow_trading.py — SQLite-backed Shadow Trading Journal (God Mode §75).

Shadow mode records **counterfactual trades** — the orders the bot WOULD have
placed if it were in paper or live mode — without ever touching the order
book. The journal lets the pipeline be benchmarked against the live / paper
P&L it would have produced, surfacing strategies whose theoretical edge
doesn't survive contact with real fills (slippage, rejection, adverse
selection) without risking capital.

The module is the persistence layer for ``settings.trading_mode == "shadow"``
(see ``config.py`` + ``risk/manager.check_order``). When the bot is in shadow
mode, ``check_order`` short-circuits every order with a "shadow mode" reason
so no order is submitted to the paper / live venue, and the strategy /
risk layer calls ``record_shadow_trade(...)`` to log what it WOULD have
done. The originating ``decision_id`` is preserved on every shadow row so
the full PREDICTION → SIGNAL → RISK_APPROVED → SHADOW_TRADE chain is
recoverable via ``core/decision_ledger.get_chain(decision_id)``.

Schema (additive — independent SQLite db at the same directory as the
decision ledger so the audit-trail immutability contract is not perturbed;
mirrors the ``core/decision_ledger.py`` + ``core/closed_positions.py`` async
+ ``asyncio.to_thread`` convention so the three databases coexist without
schema contention)::

    shadow_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       REAL    NOT NULL,                  -- epoch seconds
        decision_id     TEXT,                                -- cross-ref → decision_ledger
        token_id        TEXT,
        strategy        TEXT,
        side            TEXT,                                -- BUY / SELL
        price           REAL,                                -- intended limit price
        size            REAL,                                -- intended trade size (shares)
        predicted_edge  REAL,                                -- p_yes − market_mid at signal time
        confidence      REAL                                 -- ML confidence at signal time [0..1]
    )

Indexes:
    (timestamp DESC)             — most-recent-first global feed
    (strategy, timestamp DESC)    — per-strategy feed
    (token_id, timestamp DESC)    — per-token feed
    (decision_id)                 — decision-ledger cross-ref

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose::

    GET /api/shadow/trades        recent counterfactual trades (filterable)
    GET /api/shadow/comparison    shadow-vs-live side-by-side comparison
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── DB_PATH ──────────────────────────────────────────────────────────────────
# Per the God Mode §75 spec, the shadow journal co-resides in the SAME
# directory as the canonical decision ledger (the env var
# ``DECISION_LEDGER_DB_PATH`` names a *file*, so we take its parent and
# append ``shadow_trades.db``). This keeps every decision-derived artefact
# (stage events, rejections, shadow trades) under one configurable root,
# while remaining in a separate db file so the decision ledger's
# immutability contract is not perturbed.
_DECISION_LEDGER_DB_PATH = Path(
    os.environ.get("DECISION_LEDGER_DB_PATH", "/app/data/decision_ledger.db")
)
DB_PATH: Path = _DECISION_LEDGER_DB_PATH.parent / "shadow_trades.db"


# ── Schema ──────────────────────────────────────────────────────────────────

def _init_db() -> None:
    """Create the ``shadow_trades`` table + indexes. Safe to call on every boot."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL    NOT NULL,
                    decision_id     TEXT,
                    token_id        TEXT,
                    strategy        TEXT,
                    side            TEXT,
                    price           REAL,
                    size            REAL,
                    predicted_edge  REAL,
                    confidence      REAL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_time "
                "ON shadow_trades(timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_strategy "
                "ON shadow_trades(strategy, timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_token "
                "ON shadow_trades(token_id, timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_decision "
                "ON shadow_trades(decision_id)"
            )
            conn.commit()
    except Exception as e:
        log.error("[shadow_trading] init failed (%s): %s", DB_PATH, e)


# Initialise on module import — mirrors the ``decision_ledger`` /
# ``audit_logger`` / ``execution_quality`` convention so the store is ready
# the moment any caller imports the module.
_init_db()


# ── Writes ──────────────────────────────────────────────────────────────────

async def record_shadow_trade(
    decision_id: str,
    token_id: str,
    strategy: str,
    side: str,
    price: float,
    size: float,
    predicted_edge: float,
    confidence: float,
) -> int | None:
    """
    Persist a single counterfactual trade.

    Called by the strategy / risk layer when
    ``settings.trading_mode == "shadow"`` to log what the bot WOULD have
    done had it been in paper / live mode. The originating ``decision_id``
    is preserved so the full PREDICTION → SIGNAL → RISK_APPROVED →
    SHADOW_TRADE chain can be reconstructed via
    ``core/decision_ledger.get_chain(decision_id)``.

    Args:
        decision_id:    cross-reference to the unified decision ledger
                        (may be ``""`` for shadow rows originating outside
                        the decision ledger — e.g. manual probes).
        token_id:       Polymarket conditional-token id the trade targets.
        strategy:       strategy name (e.g. ``"signal_trader"``).
        side:           ``"BUY"`` / ``"SELL"``. Accepts ``Side.BUY``-style
                        enums transparently (reads ``.value`` when present)
                        and normalises to upper-case so downstream filters
                        on ``side`` are stable.
        price:          intended limit price (0..1 for binary markets).
        size:           intended trade size (shares / contracts).
        predicted_edge: ``p_yes − market_mid`` at signal time.
        confidence:     ML confidence at signal time (0..1).

    Side is normalised to upper-case ("BUY" / "SELL") for stable downstream
    filtering. Numeric inputs are coerced via ``_safe_float`` so ``None`` /
    NaN / non-numeric values are stored as SQL ``NULL`` rather than
    crashing the persistence path.

    Persistence failures are logged at ``error`` level and swallowed —
    the trading pipeline never blocks on shadow-journal writes (same
    fire-and-forget contract as ``decision_ledger.record`` and
    ``closed_positions.record_closed_position``).

    Returns the inserted row ``id`` (or ``None`` on failure) so callers can
    cross-link the shadow row to other ledgers if desired.
    """
    ts = time.time()
    decision_id_s = str(decision_id or "")
    token_id_s = str(token_id or "")
    strategy_s = str(strategy or "")
    side_str = _normalise_side(side)
    price_f = _safe_float(price)
    size_f = _safe_float(size)
    edge_f = _safe_float(predicted_edge)
    conf_f = _safe_float(confidence)
    row_id: int | None = None

    def _insert() -> None:
        nonlocal row_id
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO shadow_trades
                    (timestamp, decision_id, token_id, strategy, side,
                     price, size, predicted_edge, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts, decision_id_s, token_id_s, strategy_s, side_str,
                        price_f, size_f, edge_f, conf_f,
                    ),
                )
                conn.commit()
                row_id = cursor.lastrowid
        except Exception as e:
            log.error(
                "[shadow_trading] record failed decision=%s token=%s strategy=%s: %s",
                decision_id_s, token_id_s, strategy_s, e,
            )

    await asyncio.to_thread(_insert)
    return row_id


# ── Reads ───────────────────────────────────────────────────────────────────

async def get_shadow_trades(
    limit: int = 50,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return recent shadow trades (most recent first).

    Args:
        limit:   max rows to return (clamped to ``[1, 1000]`` for safety).
        strategy: optional filter. ``None`` / ``""`` returns across all
                 strategies; a non-empty value filters to that strategy only.

    Returns a list of plain ``dict`` rows mirroring the ``shadow_trades``
    schema (``id``, ``timestamp``, ``decision_id``, ``token_id``,
    ``strategy``, ``side``, ``price``, ``size``, ``predicted_edge``,
    ``confidence``). Empty list on error (the caller never sees a 500 —
    consistent with the read-path contract on ``decision_ledger`` and
    ``closed_positions``).
    """
    limit = max(1, min(1000, int(limit)))
    if strategy is not None:
        strategy = str(strategy).strip() or None

    def _fetch() -> list[dict[str, Any]]:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if strategy:
                    cursor.execute(
                        """
                        SELECT * FROM shadow_trades
                        WHERE strategy = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (strategy, limit),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM shadow_trades
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (limit,),
                    )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error("[shadow_trading] get_shadow_trades failed: %s", e)
            return []

    return await asyncio.to_thread(_fetch)


async def get_shadow_vs_live_comparison() -> dict[str, Any]:
    """
    Side-by-side comparison of counterfactual (shadow) trades against live
    closed positions.

    Aggregates both ledgers across the same dimensions (count, total size /
    volume, average predicted edge / P&L, average confidence, win rate) so
    a strategy whose shadow edge looks promising but whose live P&L
    underperforms can be flagged for review without risking capital on the
    experiment.

    The live side is sourced from ``core.closed_positions.closed_positions``
    (the canonical closed-position journal — see ``core/closed_positions.py``)
    via a lazy import so this module has no hard import-time dependency on
    the closed-positions store. If the closed-positions store is empty /
    unavailable, the live side is reported as zeroed-out so the comparison
    still returns (no 500s on a fresh deployment where no live trades have
    closed yet).

    Returns::

        {
          "shadow": {
              "count":              int,
              "total_size":         float,   # sum of size (shares)
              "avg_predicted_edge": float,
              "avg_confidence":     float,
              "by_side":             {"BUY": n, "SELL": n},
              "by_strategy": {strategy: {
                  "count":      int,
                  "total_size": float,
                  "avg_edge":   float,
                  "avg_conf":   float,
              }, ...},
          },
          "live": {
              "count":                int,
              "total_pnl":            float,
              "avg_pnl":              float,
              "win_rate":             float,    # 0..1
              "total_volume_shares":  float,
              "by_strategy": {strategy: {
                  "count":       int,
                  "total_pnl":   float,
                  "avg_pnl":     float,
                  "win_rate":    float,
              }, ...},
          },
          "strategies": [
              {
                  "strategy":          str,
                  "shadow_count":      int,
                  "live_count":        int,
                  "shadow_avg_edge":   float,
                  "live_avg_pnl":      float,
                  "shadow_total_size": float,
                  "live_total_pnl":    float,
              },
              ...
          ],
        }
    """
    # ── Shadow side ───────────────────────────────────────────────────────
    shadow_summary = await _shadow_summary()

    # ── Live side (lazy import so this module is decoupled at import time) ──
    live_summary = await _live_summary()

    # ── Per-strategy merge ────────────────────────────────────────────────
    shadow_by_strat = shadow_summary["by_strategy"]
    live_by_strat = live_summary["by_strategy"]
    strategies = sorted(set(shadow_by_strat) | set(live_by_strat))
    rows: list[dict[str, Any]] = []
    for s in strategies:
        sh = shadow_by_strat.get(s, {})
        lv = live_by_strat.get(s, {})
        rows.append({
            "strategy": s,
            "shadow_count": int(sh.get("count") or 0),
            "live_count": int(lv.get("count") or 0),
            "shadow_avg_edge": _round(sh.get("avg_edge")),
            "live_avg_pnl": _round(lv.get("avg_pnl")),
            "shadow_total_size": _round(sh.get("total_size")),
            "live_total_pnl": _round(lv.get("total_pnl")),
        })

    return {
        "shadow": {
            "count": shadow_summary["count"],
            "total_size": _round(shadow_summary["total_size"]),
            "avg_predicted_edge": _round(shadow_summary["avg_edge"]),
            "avg_confidence": _round(shadow_summary["avg_conf"]),
            "by_side": shadow_summary["by_side"],
            "by_strategy": {
                k: {
                    "count": int(v.get("count") or 0),
                    "total_size": _round(v.get("total_size")),
                    "avg_edge": _round(v.get("avg_edge")),
                    "avg_conf": _round(v.get("avg_conf")),
                }
                for k, v in shadow_by_strat.items()
            },
        },
        "live": {
            "count": live_summary["count"],
            "total_pnl": _round(live_summary["total_pnl"]),
            "avg_pnl": _round(live_summary["avg_pnl"]),
            "win_rate": _round(live_summary["win_rate"]),
            "total_volume_shares": _round(live_summary["total_volume_shares"]),
            "by_strategy": {
                k: {
                    "count": int(v.get("count") or 0),
                    "total_pnl": _round(v.get("total_pnl")),
                    "avg_pnl": _round(v.get("avg_pnl")),
                    "win_rate": _round(v.get("win_rate")),
                }
                for k, v in live_by_strat.items()
            },
        },
        "strategies": rows,
    }


# ── Internal aggregations ───────────────────────────────────────────────────

async def _shadow_summary() -> dict[str, Any]:
    """Aggregate the ``shadow_trades`` table (count / size / edge / conf)."""
    def _fetch() -> dict[str, Any]:
        empty: dict[str, Any] = {
            "count": 0,
            "total_size": 0.0,
            "avg_edge": 0.0,
            "avg_conf": 0.0,
            "by_side": {"BUY": 0, "SELL": 0},
            "by_strategy": {},
        }
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        COUNT(*)                              AS count,
                        COALESCE(SUM(size), 0.0)              AS total_size,
                        COALESCE(AVG(predicted_edge), 0.0)    AS avg_edge,
                        COALESCE(AVG(confidence), 0.0)        AS avg_conf
                    FROM shadow_trades
                    """
                )
                agg = dict(cursor.fetchone() or {})

                cursor.execute(
                    """
                    SELECT
                        UPPER(COALESCE(side, '')) AS side,
                        COUNT(*)                  AS count
                    FROM shadow_trades
                    GROUP BY UPPER(COALESCE(side, ''))
                    """
                )
                by_side = {"BUY": 0, "SELL": 0}
                for r in cursor.fetchall():
                    s = r["side"]
                    if s in by_side:
                        by_side[s] = int(r["count"])

                cursor.execute(
                    """
                    SELECT
                        strategy,
                        COUNT(*)                              AS count,
                        COALESCE(SUM(size), 0.0)              AS total_size,
                        COALESCE(AVG(predicted_edge), 0.0)    AS avg_edge,
                        COALESCE(AVG(confidence), 0.0)        AS avg_conf
                    FROM shadow_trades
                    WHERE strategy IS NOT NULL AND strategy <> ''
                    GROUP BY strategy
                    """
                )
                by_strategy: dict[str, dict[str, Any]] = {}
                for r in cursor.fetchall():
                    by_strategy[r["strategy"]] = {
                        "count": int(r["count"]),
                        "total_size": float(r["total_size"] or 0.0),
                        "avg_edge": float(r["avg_edge"] or 0.0),
                        "avg_conf": float(r["avg_conf"] or 0.0),
                    }
        except Exception as e:
            log.error("[shadow_trading] _shadow_summary failed: %s", e)
            return empty

        count = int(agg.get("count") or 0)
        if count == 0:
            return empty
        return {
            "count": count,
            "total_size": float(agg.get("total_size") or 0.0),
            "avg_edge": float(agg.get("avg_edge") or 0.0),
            "avg_conf": float(agg.get("avg_conf") or 0.0),
            "by_side": by_side,
            "by_strategy": by_strategy,
        }

    return await asyncio.to_thread(_fetch)


async def _live_summary() -> dict[str, Any]:
    """
    Aggregate closed_positions via the canonical ``closed_positions`` store.

    The import is performed inside this function so a missing / broken
    closed-positions store can never break the shadow-comparison endpoint
    — the live side simply reports zeros if the dependency is unavailable.
    """
    empty: dict[str, Any] = {
        "count": 0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "win_rate": 0.0,
        "total_volume_shares": 0.0,
        "by_strategy": {},
    }
    try:
        # Lazy import — keeps this module decoupled from closed_positions
        # at import time. If the closed-positions store is missing / broken,
        # we return the empty summary rather than propagating the failure.
        from core.closed_positions import closed_positions  # type: ignore
    except Exception as e:
        log.warning("[shadow_trading] closed_positions import failed: %s", e)
        return empty

    try:
        stats = await closed_positions.get_closed_stats()
    except Exception as e:
        log.error("[shadow_trading] closed_positions.get_closed_stats failed: %s", e)
        return empty

    # Pull all closed positions for the per-strategy breakdown. The
    # closed_positions store exposes ``get_closed_positions(limit=...)``
    # returning rows newest-first; we cap at a generous 1000-row slice so
    # the strategy-level roll-up is bounded even for very active deployments.
    try:
        rows = await closed_positions.get_closed_positions(limit=1000)
    except Exception as e:
        log.error("[shadow_trading] closed_positions.get_closed_positions failed: %s", e)
        rows = []

    by_strategy: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = (r.get("strategy") or "").strip()
        if not s:
            continue
        pnl = float(r.get("pnl") or 0.0)
        shares = float(r.get("shares") or 0.0)
        d = by_strategy.setdefault(
            s,
            {
                "count": 0,
                "total_pnl": 0.0,
                "wins": 0,
                "total_volume_shares": 0.0,
            },
        )
        d["count"] += 1
        d["total_pnl"] += pnl
        d["total_volume_shares"] += shares
        if pnl > 0:
            d["wins"] += 1
    for s, d in by_strategy.items():
        c = d["count"]
        d["avg_pnl"] = (d["total_pnl"] / c) if c else 0.0
        d["win_rate"] = (d["wins"] / c) if c else 0.0

    return {
        "count": int(stats.get("count") or 0),
        "total_pnl": float(stats.get("total_pnl") or 0.0),
        "avg_pnl": float(stats.get("avg_pnl") or 0.0),
        "win_rate": float(stats.get("win_rate") or 0.0),
        "total_volume_shares": float(stats.get("total_volume_shares") or 0.0),
        "by_strategy": by_strategy,
    }


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append shadow-trading inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/shadow/trades
          Recent counterfactual trades (most recent first). Query params:

          - ``limit``    (1..500, default 50)   — max rows to return
          - ``strategy`` (optional)             — filter to a single strategy

          Returns ``{count, trades[]}`` — each row carries the raw
          ``shadow_trades`` columns (``timestamp``, ``decision_id``,
          ``token_id``, ``strategy``, ``side``, ``price``, ``size``,
          ``predicted_edge``, ``confidence``).

      GET /api/shadow/comparison
          Shadow-vs-live side-by-side comparison (see
          ``get_shadow_vs_live_comparison`` for the payload shape).
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/shadow/trades", tags=["shadow"])
    async def _shadow_trades(
        limit: int = Query(50, ge=1, le=500, description="Max shadow trades to return"),
        strategy: str | None = Query(None, description="Filter by strategy name"),
    ):
        """Return recent counterfactual trades (most recent first)."""
        rows = await get_shadow_trades(limit=limit, strategy=strategy)
        return {"count": len(rows), "trades": rows}

    @app.get("/api/shadow/comparison", tags=["shadow"])
    async def _shadow_comparison():
        """Shadow-vs-live side-by-side comparison."""
        return await get_shadow_vs_live_comparison()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise_side(side: Any) -> str:
    """
    Normalise ``side`` to an upper-case string ("BUY" / "SELL" / "").

    Accepts plain strings ("buy", "BUY"), ``Side.BUY``-style enums (reads
    ``.value`` when present), and ``None`` (returns ""). Non-string,
    non-enum inputs are stringified and upper-cased as a fallback.
    """
    try:
        if hasattr(side, "value"):
            return str(side.value or "").upper()
        return str(side or "").upper()
    except Exception:
        return ""


def _safe_float(v: Any) -> float | None:
    """Coerce to float; return ``None`` on failure (so SQLite stores NULL)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check
        return None
    return f


def _round(v: Any, ndigits: int = 4) -> float:
    """Best-effort round to N digits — never raises (returns 0.0 on bad input)."""
    try:
        return round(float(v or 0.0), ndigits)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "DB_PATH",
    "record_shadow_trade",
    "get_shadow_trades",
    "get_shadow_vs_live_comparison",
    "register_routes",
]
