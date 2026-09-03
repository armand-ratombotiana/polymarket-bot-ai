"""
core/attribution.py — Performance Attribution Engine.

Slices the realised-P&L roll-up from ``core/closed_positions`` across seven
orthogonal dimensions so a portfolio manager can answer questions like:

  - Which *strategy* is making the money?            → ``by_strategy``
  - Does higher *ML confidence* actually pay?        → ``by_confidence_bucket``
  - Does the *edge* bucket (p_yes − market_mid)
    at signal time predict realised P&L?              → ``by_edge_bucket``
  - Are *deep-NO* calls (p_yes < 0.2) profitable
    or are we just collecting pennies?               → ``by_probability_band``
  - Does *liquidity* at signal time matter
    (thin markets → wider slippage)?                  → ``by_liquidity_level``
  - How does *holding period* drive P&L
    (intraday vs. multi-day swings)?                  → ``by_holding_period``
  - Are *long-YES* (BUY-open) trades winning more
    than *short-YES* (SELL-open) ones?                → ``by_trade_direction``

All seven roll-ups are returned by ``get_full_attribution()`` (the endpoint
``GET /api/attribution`` calls this directly). Each per-bucket row carries::

    {
        "bucket":              str,    # bucket label
        "count":               int,
        "total_pnl":           float,
        "avg_pnl":             float,
        "win_rate":            float,  # 0..1
        "wins":                int,
        "losses":              int,
        "avg_holding_seconds": float,
        "gross_profit":        float,  # sum of +pnl
        "gross_loss":          float,  # sum of |−pnl|
        "profit_factor":       float | None,  # None when no losses
        "capital_deployed":    float,  # sum of entry_price × shares
    }

Bucket conventions (single source of truth — referenced by the dashboard and
``api/server.py``):

  - **confidence_bucket**:    ``low`` (<0.50), ``medium`` [0.50, 0.70),
                              ``high`` [0.70, 0.85), ``very_high`` (≥0.85),
                              ``unknown`` (NULL).
  - **edge_bucket**:          ``negative`` (<0),  ``small`` [0, 2ct),
                              ``medium`` [2ct, 5ct), ``large`` [5ct, 10ct),
                              ``very_large`` (≥10ct), ``unknown`` (NULL).
  - **probability_band**:     ``deep_no`` (<0.20), ``no`` [0.20, 0.40),
                              ``neutral`` [0.40, 0.60), ``yes`` [0.60, 0.80),
                              ``strong_yes`` (≥0.80), ``unknown`` (NULL).
  - **liquidity_level**:      ``thin`` (<$1k), ``low`` [$1k, $10k),
                              ``medium`` [$10k, $50k), ``high`` [$50k, $200k),
                              ``very_high`` (≥$200k), ``unknown`` (NULL).
  - **holding_period**:       ``intraday`` (<1h), ``short`` [1h, 1d),
                              ``medium`` [1d, 7d), ``long`` (≥7d).
  - **trade_direction**:      ``BUY`` / ``SELL`` / ``unknown`` (long YES vs.
                              long NO / synthetic short — derived from the
                              opening trade's ``direction`` column).
  - **strategy**:             free-form string; nulls bucket as ``unknown``.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Callable

from core.closed_positions import closed_positions

log = logging.getLogger(__name__)


# ── Bucket classifiers ───────────────────────────────────────────────────────
# Each classifier is a pure (row_dict) -> str function. They're public so the
# dashboard / tests can replicate the bucket logic without re-implementing it.

def classify_confidence(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    if confidence < 0.50:
        return "low"
    if confidence < 0.70:
        return "medium"
    if confidence < 0.85:
        return "high"
    return "very_high"


def classify_edge(edge: float | None) -> str:
    if edge is None:
        return "unknown"
    if edge < 0.0:
        return "negative"
    if edge < 0.02:
        return "small"
    if edge < 0.05:
        return "medium"
    if edge < 0.10:
        return "large"
    return "very_large"


def classify_probability(p_yes: float | None) -> str:
    if p_yes is None:
        return "unknown"
    if p_yes < 0.20:
        return "deep_no"
    if p_yes < 0.40:
        return "no"
    if p_yes < 0.60:
        return "neutral"
    if p_yes < 0.80:
        return "yes"
    return "strong_yes"


def classify_liquidity(liquidity: float | None) -> str:
    if liquidity is None:
        return "unknown"
    if liquidity < 1_000:
        return "thin"
    if liquidity < 10_000:
        return "low"
    if liquidity < 50_000:
        return "medium"
    if liquidity < 200_000:
        return "high"
    return "very_high"


def classify_holding_period(holding_seconds: float | None) -> str:
    if holding_seconds is None:
        return "unknown"
    s = float(holding_seconds)
    if s < 3_600:           # < 1 hour
        return "intraday"
    if s < 86_400:          # < 1 day
        return "short"
    if s < 604_800:         # < 7 days
        return "medium"
    return "long"


def classify_trade_direction(direction: str | None) -> str:
    """Normalise direction to ``BUY`` / ``SELL`` / ``unknown``."""
    if not direction:
        return "unknown"
    d = str(direction).strip().upper()
    if d in ("BUY", "LONG", "LONG_YES"):
        return "BUY"
    if d in ("SELL", "SHORT", "LONG_NO"):
        return "SELL"
    return "unknown"


# Ordered bucket labels per dimension (used to stabilise output order —
# ``unknown`` always trails the real buckets so the dashboard's "by X" tables
# present meaningful buckets first).
CONFIDENCE_BUCKETS = ["low", "medium", "high", "very_high", "unknown"]
EDGE_BUCKETS = ["negative", "small", "medium", "large", "very_large", "unknown"]
PROBABILITY_BANDS = ["deep_no", "no", "neutral", "yes", "strong_yes", "unknown"]
LIQUIDITY_LEVELS = ["thin", "low", "medium", "high", "very_high", "unknown"]
HOLDING_PERIODS = ["intraday", "short", "medium", "long", "unknown"]
TRADE_DIRECTIONS = ["BUY", "SELL", "unknown"]


# ── Core aggregation ────────────────────────────────────────────────────────

def _aggregate_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the standard P&L roll-up for a single bucket's rows."""
    count = len(rows)
    if count == 0:
        return _empty_bucket("")

    pnls = [float(r.get("pnl") or 0.0) for r in rows]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(-p for p in pnls if p < 0)
    holdings = [float(r.get("holding_seconds") or 0.0) for r in rows]
    capital = sum(
        float(r.get("entry_price") or 0.0) * float(r.get("shares") or 0.0)
        for r in rows
    )
    profit_factor = (
        None if gross_loss <= 0 else round(gross_profit / gross_loss, 4)
    )

    return {
        "count": count,
        "total_pnl": round(total_pnl, 4),
        "avg_pnl": round(total_pnl / count, 4),
        "win_rate": round(wins / count, 4),
        "wins": wins,
        "losses": losses,
        "avg_holding_seconds": round(sum(holdings) / count, 2) if holdings else 0.0,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": profit_factor,
        "capital_deployed": round(capital, 4),
    }


def _empty_bucket(name: str) -> dict[str, Any]:
    return {
        "bucket": name,
        "count": 0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "avg_holding_seconds": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "profit_factor": None,
        "capital_deployed": 0.0,
    }


def _slice(
    rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    ordered_labels: list[str],
) -> list[dict[str, Any]]:
    """
    Group ``rows`` by ``key_fn(row) -> label`` and return one bucket dict per
    label in ``ordered_labels`` (any labels with no rows still appear, as
    zeroed-out buckets, so dashboards get a stable schema regardless of which
    buckets happen to be populated in the current data set).
    """
    by_label: dict[str, list[dict[str, Any]]] = {label: [] for label in ordered_labels}
    extras: list[str] = []
    for r in rows:
        label = key_fn(r)
        if label in by_label:
            by_label[label].append(r)
        else:
            extras.append(label)

    out = []
    for label in ordered_labels:
        bucket = _aggregate_bucket(by_label[label])
        bucket["bucket"] = label
        out.append(bucket)
    # Trailing buckets for unexpected labels (defensive — shouldn't happen
    # with the current classifiers but kept so we never silently drop rows).
    for label in extras:
        bucket = _aggregate_bucket([r for r in rows if key_fn(r) == label])
        bucket["bucket"] = label
        out.append(bucket)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

async def _all_rows() -> list[dict[str, Any]]:
    """
    Fetch every recorded closed position (no LIMIT) for full-dimension
    aggregation. Capped at 10 000 rows defensively — the journal is append-
    only and the dashboard rarely inspects more than a few thousand closed
    positions at once.
    """
    return await closed_positions.get_closed_positions(limit=10_000, strategy=None)


async def attribute_by_strategy() -> list[dict[str, Any]]:
    """
    Group closed positions by ``strategy`` column.

    Returns one bucket per distinct strategy (sorted by total_pnl desc so the
    most profitable strategy is first). Buckets with zero rows are NOT
    included (unlike the fixed-vocabulary dimensions, the strategy space is
    open-ended so we can't pre-list empty buckets).
    """
    rows = await _all_rows()
    by_strat: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        label = r.get("strategy") or "unknown"
        by_strat.setdefault(label, []).append(r)
    out = []
    for label, subset in by_strat.items():
        bucket = _aggregate_bucket(subset)
        bucket["bucket"] = label
        out.append(bucket)
    out.sort(key=lambda b: b["total_pnl"], reverse=True)
    return out


async def attribute_by_confidence_bucket() -> list[dict[str, Any]]:
    """Group closed positions by ML confidence bucket at signal time."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_confidence(r.get("confidence")),
        CONFIDENCE_BUCKETS,
    )


async def attribute_by_edge_bucket() -> list[dict[str, Any]]:
    """Group closed positions by predicted_edge bucket at signal time."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_edge(r.get("predicted_edge")),
        EDGE_BUCKETS,
    )


async def attribute_by_probability_band() -> list[dict[str, Any]]:
    """Group closed positions by raw model p_yes band at signal time."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_probability(r.get("p_yes")),
        PROBABILITY_BANDS,
    )


async def attribute_by_liquidity_level() -> list[dict[str, Any]]:
    """Group closed positions by market liquidity level at signal time."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_liquidity(r.get("liquidity")),
        LIQUIDITY_LEVELS,
    )


async def attribute_by_holding_period() -> list[dict[str, Any]]:
    """Group closed positions by holding-period bucket."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_holding_period(r.get("holding_seconds")),
        HOLDING_PERIODS,
    )


async def attribute_by_trade_direction() -> list[dict[str, Any]]:
    """Group closed positions by opening-trade direction (BUY / SELL)."""
    rows = await _all_rows()
    return _slice(
        rows,
        lambda r: classify_trade_direction(r.get("direction")),
        TRADE_DIRECTIONS,
    )


async def get_full_attribution() -> dict[str, Any]:
    """
    Return all seven attribution dimensions in a single payload.

    Shape::

        {
            "summary":   { ... closed_positions.get_closed_stats() ... },
            "by_strategy":            [ {bucket, count, total_pnl, ...}, ... ],
            "by_confidence_bucket":   [ ... ],
            "by_edge_bucket":         [ ... ],
            "by_probability_band":   [ ... ],
            "by_liquidity_level":     [ ... ],
            "by_holding_period":      [ ... ],
            "by_trade_direction":     [ ... ],
            "bucket_definitions": {  # for dashboard legend / UI copy
                "confidence_bucket":   [...labels],
                "edge_bucket":         [...labels],
                "probability_band":    [...labels],
                "liquidity_level":     [...labels],
                "holding_period":      [...labels],
                "trade_direction":     [...labels],
            },
        }
    """
    summary, by_strat, by_conf, by_edge, by_prob, by_liq, by_hold, by_dir = (
        await asyncio.gather(
            closed_positions.get_closed_stats(),
            attribute_by_strategy(),
            attribute_by_confidence_bucket(),
            attribute_by_edge_bucket(),
            attribute_by_probability_band(),
            attribute_by_liquidity_level(),
            attribute_by_holding_period(),
            attribute_by_trade_direction(),
        )
    )

    return {
        "summary": summary,
        "by_strategy": by_strat,
        "by_confidence_bucket": by_conf,
        "by_edge_bucket": by_edge,
        "by_probability_band": by_prob,
        "by_liquidity_level": by_liq,
        "by_holding_period": by_hold,
        "by_trade_direction": by_dir,
        "bucket_definitions": {
            "confidence_bucket": CONFIDENCE_BUCKETS,
            "edge_bucket": EDGE_BUCKETS,
            "probability_band": PROBABILITY_BANDS,
            "liquidity_level": LIQUIDITY_LEVELS,
            "holding_period": HOLDING_PERIODS,
            "trade_direction": TRADE_DIRECTIONS,
        },
    }


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append the attribution endpoint to a FastAPI app.

    Endpoint (auth-protected by the caller's existing middleware):

      GET /api/attribution
          Full seven-dimension P&L attribution roll-up across all closed
          positions. Returns the dict shape documented on
          ``get_full_attribution()``.
    """
    @app.get("/api/attribution", tags=["analytics"])
    async def _attribution():
        """Return P&L attribution across strategy / confidence / edge /
        probability / liquidity / holding-period / direction dimensions."""
        return await get_full_attribution()


__all__ = [
    "register_routes",
    "get_full_attribution",
    "attribute_by_strategy",
    "attribute_by_confidence_bucket",
    "attribute_by_edge_bucket",
    "attribute_by_probability_band",
    "attribute_by_liquidity_level",
    "attribute_by_holding_period",
    "attribute_by_trade_direction",
    "classify_confidence",
    "classify_edge",
    "classify_probability",
    "classify_liquidity",
    "classify_holding_period",
    "classify_trade_direction",
    "CONFIDENCE_BUCKETS",
    "EDGE_BUCKETS",
    "PROBABILITY_BANDS",
    "LIQUIDITY_LEVELS",
    "HOLDING_PERIODS",
    "TRADE_DIRECTIONS",
]
