"""
Unit tests for ``core/attribution.py``.

U1 — Performance Attribution Engine unit tests.

Covers the seven guarantees enumerated in the U1 task spec:

  1. ``attribute_by_strategy()`` groups trades correctly — one bucket per
     distinct strategy, sorted by ``total_pnl`` desc (most profitable
     first), with rows missing the ``strategy`` field rolled into the
     ``unknown`` bucket.
  2. ``attribute_by_confidence_bucket()`` buckets trades into the four
     confidence ranges (``low`` <0.50, ``medium`` [0.50, 0.70),
     ``high`` [0.70, 0.85), ``very_high`` ≥0.85) + ``unknown`` for
     NULL confidence — the boundaries documented on
     ``classify_confidence``.
  3. ``attribute_by_trade_direction()`` splits BUY vs SELL (with
     ``unknown`` fallback for missing / unrecognised direction values),
     using ``classify_trade_direction``'s synonym map (``LONG`` /
     ``LONG_YES`` → BUY, ``SHORT`` / ``LONG_NO`` → SELL).
  4. ``get_full_attribution()`` returns all seven attribution dimensions
     in a single payload (``summary`` + 7 ``by_*`` lists +
     ``bucket_definitions`` legend).
  5. ``profit_factor`` is ``None`` when ``gross_loss == 0`` (the
     no-loss edge case — the documented divide-by-zero guard on
     ``_aggregate_bucket``).
  6. Per-bucket expectancy derived from the roll-up equals
     ``(win_rate * avg_win) + (loss_rate * avg_loss)`` (with
     ``avg_loss`` taken as the signed, i.e. negative, average loss)
     and matches the bucket's own ``avg_pnl`` field — the canonical
     trading-math identity.
  7. Empty trades → all seven dimensions return zeroed-out buckets
     (``count=0``, ``total_pnl=0.0``, ``profit_factor=None``) — the
     fresh-deployment contract.

Mocking strategy
----------------
The attribution engine reads from the ``closed_positions`` singleton
(an instance of ``ClosedPositionsStore`` from ``core.closed_positions``)
via the async methods ``get_closed_positions(limit=..., strategy=None)``
(the seven ``attribute_by_*`` rolls-ups) and ``get_closed_stats()``
(the ``get_full_attribution`` ``summary`` block). To make these tests
hermetic — independent of any on-disk SQLite journal and the
import-time singleton's mutable state — we monkeypatch those two async
methods on the ``core.attribution.closed_positions`` module-level
singleton so they return a fixture-defined list of trade dicts. The
``set_trades`` injector (returned by the ``set_trades`` fixture) is the
"mocked store.trades" surface the U1 task spec refers to: the
attribution engine's data source is mocked to a deterministic in-memory
list of seed trades. No real DB is hit; the production singleton's
state is never mutated (``monkeypatch`` restores the original methods at
teardown); every test is deterministic over the fixture's seed rows.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the U1 "Do NOT edit existing
files" constraint, so ``asyncio_mode = "auto"`` cannot be enabled via
config — mirrors the convention already used by
``tests/test_closed_positions.py`` and the other Wave 3 test modules).
"""
from __future__ import annotations

from typing import Any

import pytest

from core import attribution as attribution_mod
from core.attribution import (
    CONFIDENCE_BUCKETS,
    EDGE_BUCKETS,
    HOLDING_PERIODS,
    LIQUIDITY_LEVELS,
    PROBABILITY_BANDS,
    TRADE_DIRECTIONS,
    attribute_by_confidence_bucket,
    attribute_by_strategy,
    attribute_by_trade_direction,
    get_full_attribution,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the U1 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors ``tests/test_closed_positions.py`` and the other Wave 3 tests).
pytestmark = pytest.mark.asyncio


# ── Helpers / fixtures ─────────────────────────────────────────────────────


def _trade(**overrides: Any) -> dict[str, Any]:
    """Construct a single trade dict shaped like a row returned by
    ``closed_positions.get_closed_positions``.

    Every column ``core/attribution.py`` reads is set to a sensible
    default so the bucket classifiers run unconditionally; tests override
    only the fields they care about (``pnl``, ``strategy``,
    ``confidence``, ``direction``, ``predicted_edge``, ``p_yes``,
    ``liquidity``, ``holding_seconds``, ``entry_price``, ``shares``).
    """
    row: dict[str, Any] = {
        "position_id": "pos-default",
        "token_id": "TOK_TEST",
        "strategy": "ml_sig_v1",
        "entry_price": 0.50,
        "exit_price": 0.55,
        "shares": 100.0,
        "pnl": 0.0,
        "holding_seconds": 3600.0,
        "timestamp": 1_700_000_000.0,
        "model_version": "v-test",
        "decision_id": None,
        "direction": None,
        "confidence": None,
        "predicted_edge": None,
        "p_yes": None,
        "market_mid": None,
        "liquidity": None,
        "data": {},
    }
    row.update(overrides)
    return row


@pytest.fixture
def set_trades(monkeypatch):
    """Replace ``closed_positions.get_closed_positions`` and
    ``closed_positions.get_closed_stats`` on the
    ``core.attribution.closed_positions`` singleton with deterministic
    stubs that return whatever trades the test injects via the returned
    ``set_trades(trades, stats=None)`` callable.

    This is the "mocked store.trades" surface the U1 task spec refers
    to — the attribution engine's data source is mocked to a
    test-controlled in-memory list of trade dicts. No real DB is hit;
    the production singleton's state is never mutated (``monkeypatch``
    restores the original methods at teardown).
    """
    state: dict[str, Any] = {"trades": [], "stats": None}

    async def _mock_get_closed_positions(limit: int = 10_000, strategy=None):
        rows = list(state["trades"])
        if strategy:
            rows = [
                r for r in rows if (r.get("strategy") or "unknown") == strategy
            ]
        return rows

    async def _mock_get_closed_stats():
        if state["stats"] is not None:
            return state["stats"]
        return _derive_stats(state["trades"])

    monkeypatch.setattr(
        attribution_mod.closed_positions,
        "get_closed_positions",
        _mock_get_closed_positions,
    )
    monkeypatch.setattr(
        attribution_mod.closed_positions,
        "get_closed_stats",
        _mock_get_closed_stats,
    )

    def _set(trades, stats=None):
        state["trades"] = list(trades)
        state["stats"] = stats

    return _set


def _derive_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive a ``get_closed_stats``-shaped summary dict from the
    mock trades. Mirrors the production aggregate so
    ``get_full_attribution()``'s ``summary`` block is well-formed in
    test scenarios that don't override the stats explicitly.
    """
    if not trades:
        return {
            "count": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "median_pnl": 0.0,
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "avg_holding_seconds": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "avg_entry_price": 0.0,
            "avg_exit_price": 0.0,
            "total_volume_shares": 0.0,
            "strategies_count": 0,
        }
    pnls = [float(t.get("pnl") or 0.0) for t in trades]
    count = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(-p for p in losses)
    strategies = {(t.get("strategy") or "unknown") for t in trades}
    return {
        "count": count,
        "total_pnl": round(sum(pnls), 4),
        "avg_pnl": round(sum(pnls) / count, 4),
        "median_pnl": 0.0,
        "win_rate": round(len(wins) / count, 4),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": sum(1 for p in pnls if p == 0),
        "avg_holding_seconds": 0.0,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": (
            None if gross_loss <= 0 else round(gross_profit / gross_loss, 4)
        ),
        "best_trade": round(max(pnls), 4),
        "worst_trade": round(min(pnls), 4),
        "avg_entry_price": 0.0,
        "avg_exit_price": 0.0,
        "total_volume_shares": 0.0,
        "strategies_count": len(strategies),
    }


# ── 1. attribute_by_strategy groups trades correctly ───────────────────────


async def test_attribute_by_strategy_groups_trades_correctly(set_trades):
    """``attribute_by_strategy()`` must group trades by their ``strategy``
    field, sort the buckets by ``total_pnl`` desc (most profitable
    first), and roll rows with missing ``strategy`` into the ``unknown``
    bucket. Each bucket's roll-up must match the hand-computed P&L
    statistics for its seed trades.
    """
    set_trades([
        _trade(position_id="p-a1", strategy="alpha", pnl=5.0),
        _trade(position_id="p-a2", strategy="alpha", pnl=-2.0),
        _trade(position_id="p-a3", strategy="alpha", pnl=3.0),
        _trade(position_id="p-b1", strategy="beta", pnl=-3.0),
        _trade(position_id="p-b2", strategy="beta", pnl=-1.0),
        _trade(position_id="p-u1", strategy=None, pnl=1.0),
    ])

    out = await attribute_by_strategy()

    # Three buckets, sorted by total_pnl desc:
    #   alpha  → 5 + (-2) + 3 = +6   (most profitable, first)
    #   unknown→ 1                = +1
    #   beta   → -3 + -1          = -4  (least profitable, last)
    labels = [b["bucket"] for b in out]
    assert labels == ["alpha", "unknown", "beta"]

    by_label = {b["bucket"]: b for b in out}

    # alpha: 3 trades, 2 wins (5, 3), 1 loss (-2).
    #   total_pnl=6, gross_profit=8, gross_loss=2, profit_factor=4,
    #   win_rate=2/3, avg_pnl=6/3=2.0.
    alpha = by_label["alpha"]
    assert alpha["count"] == 3
    assert alpha["wins"] == 2
    assert alpha["losses"] == 1
    assert alpha["total_pnl"] == pytest.approx(6.0)
    assert alpha["gross_profit"] == pytest.approx(8.0)
    assert alpha["gross_loss"] == pytest.approx(2.0)
    assert alpha["profit_factor"] == pytest.approx(8.0 / 2.0)
    # win_rate is rounded to 4 dp in _aggregate_bucket (round(wins/count, 4))
    # → round(2/3, 4) = 0.6667.
    assert alpha["win_rate"] == pytest.approx(round(2 / 3, 4), abs=1e-4)
    assert alpha["avg_pnl"] == pytest.approx(2.0)

    # beta: 2 trades, 0 wins, 2 losses.
    #   total_pnl=-4, gross_profit=0, gross_loss=4, profit_factor=0.0
    #   (gross_loss > 0 but gross_profit == 0 → 0/4 = 0.0, NOT None).
    beta = by_label["beta"]
    assert beta["count"] == 2
    assert beta["wins"] == 0
    assert beta["losses"] == 2
    assert beta["total_pnl"] == pytest.approx(-4.0)
    assert beta["gross_profit"] == pytest.approx(0.0)
    assert beta["gross_loss"] == pytest.approx(4.0)
    assert beta["profit_factor"] == pytest.approx(0.0)
    assert beta["win_rate"] == pytest.approx(0.0)

    # unknown: 1 trade, 1 win. gross_loss == 0 → profit_factor=None.
    unknown = by_label["unknown"]
    assert unknown["count"] == 1
    assert unknown["wins"] == 1
    assert unknown["losses"] == 0
    assert unknown["total_pnl"] == pytest.approx(1.0)
    assert unknown["profit_factor"] is None


# ── 2. attribute_by_confidence_bucket buckets into ranges ─────────────────


async def test_attribute_by_confidence_bucket_buckets_into_ranges(set_trades):
    """``attribute_by_confidence_bucket()`` must bucket trades into the
    four confidence ranges plus the ``unknown`` fallback, using the
    boundaries documented on ``classify_confidence`` (``low`` <0.50,
    ``medium`` [0.50, 0.70), ``high`` [0.70, 0.85), ``very_high``
    ≥0.85). The output must include every fixed-vocabulary bucket
    (zeroed-out when no rows land there) so the dashboard schema is
    stable regardless of which buckets happen to be populated.
    """
    set_trades([
        _trade(position_id="p-low", confidence=0.30, pnl=1.0),
        _trade(position_id="p-med", confidence=0.60, pnl=2.0),
        _trade(position_id="p-high", confidence=0.75, pnl=3.0),
        _trade(position_id="p-vh", confidence=0.90, pnl=4.0),
        _trade(position_id="p-null", confidence=None, pnl=5.0),
    ])
    out = await attribute_by_confidence_bucket()

    # Fixed-vocabulary ordering preserved.
    labels = [b["bucket"] for b in out]
    assert labels == CONFIDENCE_BUCKETS

    by_label = {b["bucket"]: b for b in out}

    # Each populated bucket has count=1 with its seeded pnl.
    assert by_label["low"]["count"] == 1
    assert by_label["low"]["total_pnl"] == pytest.approx(1.0)

    assert by_label["medium"]["count"] == 1
    assert by_label["medium"]["total_pnl"] == pytest.approx(2.0)

    assert by_label["high"]["count"] == 1
    assert by_label["high"]["total_pnl"] == pytest.approx(3.0)

    assert by_label["very_high"]["count"] == 1
    assert by_label["very_high"]["total_pnl"] == pytest.approx(4.0)

    assert by_label["unknown"]["count"] == 1
    assert by_label["unknown"]["total_pnl"] == pytest.approx(5.0)

    # ── Boundary tests: the half-open intervals must classify their
    #    low-edge value into the upper bucket (0.50→medium, 0.70→high,
    #    0.85→very_high).
    set_trades([
        _trade(position_id="p-b-050", confidence=0.50, pnl=0.0),
        _trade(position_id="p-b-070", confidence=0.70, pnl=0.0),
        _trade(position_id="p-b-085", confidence=0.85, pnl=0.0),
    ])
    out = await attribute_by_confidence_bucket()
    by_label = {b["bucket"]: b for b in out}
    assert by_label["low"]["count"] == 0
    assert by_label["medium"]["count"] == 1   # 0.50 is the low edge of medium
    assert by_label["high"]["count"] == 1     # 0.70 is the low edge of high
    assert by_label["very_high"]["count"] == 1  # 0.85 is the low edge of very_high
    assert by_label["unknown"]["count"] == 0


# ── 3. attribute_by_trade_direction (BUY vs SELL) ───────────────────────────


async def test_attribute_by_trade_direction_buy_vs_sell(set_trades):
    """``attribute_by_trade_direction()`` must classify each trade's
    ``direction`` field via ``classify_trade_direction``: ``BUY``,
    ``LONG``, ``LONG_YES`` → ``BUY``; ``SELL``, ``SHORT``, ``LONG_NO``
    → ``SELL``; missing / unrecognised → ``unknown``. Output is the
    fixed-vocabulary list ``["BUY", "SELL", "unknown"]``.
    """
    set_trades([
        _trade(position_id="p-b1", direction="BUY", pnl=1.0),
        _trade(position_id="p-b2", direction="LONG", pnl=2.0),
        _trade(position_id="p-b3", direction="LONG_YES", pnl=3.0),
        _trade(position_id="p-s1", direction="SELL", pnl=-1.0),
        _trade(position_id="p-s2", direction="SHORT", pnl=-2.0),
        _trade(position_id="p-s3", direction="LONG_NO", pnl=-3.0),
        _trade(position_id="p-u1", direction=None, pnl=0.0),
        _trade(position_id="p-u2", direction="", pnl=0.0),
        _trade(position_id="p-u3", direction="WAT", pnl=0.0),
    ])
    out = await attribute_by_trade_direction()

    labels = [b["bucket"] for b in out]
    assert labels == TRADE_DIRECTIONS

    by_label = {b["bucket"]: b for b in out}

    # BUY bucket: 3 trades (BUY, LONG, LONG_YES), total_pnl = 1+2+3 = 6.
    assert by_label["BUY"]["count"] == 3
    assert by_label["BUY"]["wins"] == 3
    assert by_label["BUY"]["losses"] == 0
    assert by_label["BUY"]["total_pnl"] == pytest.approx(6.0)
    assert by_label["BUY"]["win_rate"] == pytest.approx(1.0)

    # SELL bucket: 3 trades (SELL, SHORT, LONG_NO), total_pnl = -6.
    assert by_label["SELL"]["count"] == 3
    assert by_label["SELL"]["wins"] == 0
    assert by_label["SELL"]["losses"] == 3
    assert by_label["SELL"]["total_pnl"] == pytest.approx(-6.0)
    assert by_label["SELL"]["win_rate"] == pytest.approx(0.0)

    # unknown bucket: 3 trades (None, "", "WAT"), total_pnl = 0.
    assert by_label["unknown"]["count"] == 3
    assert by_label["unknown"]["wins"] == 0
    assert by_label["unknown"]["losses"] == 0
    assert by_label["unknown"]["total_pnl"] == pytest.approx(0.0)
    # All three trades are breakeven (pnl == 0) — gross_loss == 0 →
    # profit_factor is None (the documented no-loss divide-by-zero guard).
    assert by_label["unknown"]["profit_factor"] is None


# ── 4. get_full_attribution returns all 7 dimensions ───────────────────────


async def test_get_full_attribution_returns_all_seven_dimensions(set_trades):
    """``get_full_attribution()`` must return a payload containing the
    ``summary`` block, the seven ``by_*`` dimension lists, and the
    ``bucket_definitions`` legend (which enumerates the 6 fixed-vocabulary
    dimensions — strategy is open-ended so it's not in the legend).

    The seven ``by_*`` dimensions are: ``by_strategy``,
    ``by_confidence_bucket``, ``by_edge_bucket``, ``by_probability_band``,
    ``by_liquidity_level``, ``by_holding_period``, ``by_trade_direction``.
    """
    set_trades([
        _trade(
            position_id="p-1",
            strategy="alpha",
            direction="BUY",
            confidence=0.75,
            predicted_edge=0.06,
            p_yes=0.65,
            liquidity=5_000.0,
            holding_seconds=3600.0,
            pnl=2.0,
        ),
        _trade(
            position_id="p-2",
            strategy="beta",
            direction="SELL",
            confidence=0.30,
            predicted_edge=-0.01,
            p_yes=0.30,
            liquidity=500.0,
            holding_seconds=100_000.0,
            pnl=-1.0,
        ),
    ])
    out = await get_full_attribution()

    # Summary block present (delegated to closed_positions.get_closed_stats).
    assert "summary" in out
    assert out["summary"]["count"] == 2
    assert out["summary"]["wins"] == 1
    assert out["summary"]["losses"] == 1
    assert out["summary"]["strategies_count"] == 2

    # The seven attribution dimensions.
    expected_dims = [
        "by_strategy",
        "by_confidence_bucket",
        "by_edge_bucket",
        "by_probability_band",
        "by_liquidity_level",
        "by_holding_period",
        "by_trade_direction",
    ]
    assert len(expected_dims) == 7  # sanity — seven dimensions enumerated
    for dim in expected_dims:
        assert dim in out, f"missing dimension: {dim}"
        assert isinstance(out[dim], list)
        # Every bucket in every dimension must carry the standard roll-up
        # fields (count, total_pnl, win_rate, profit_factor, …) so the
        # dashboard can render every row uniformly.
        for bucket in out[dim]:
            assert "bucket" in bucket
            assert "count" in bucket
            assert "total_pnl" in bucket
            assert "avg_pnl" in bucket
            assert "win_rate" in bucket
            assert "profit_factor" in bucket
            assert "gross_profit" in bucket
            assert "gross_loss" in bucket

    # bucket_definitions legend: the 6 fixed-vocabulary dimensions only
    # (strategy is open-ended so it's intentionally excluded).
    assert "bucket_definitions" in out
    defs = out["bucket_definitions"]
    assert set(defs.keys()) == {
        "confidence_bucket",
        "edge_bucket",
        "probability_band",
        "liquidity_level",
        "holding_period",
        "trade_direction",
    }
    assert defs["confidence_bucket"] == CONFIDENCE_BUCKETS
    assert defs["edge_bucket"] == EDGE_BUCKETS
    assert defs["probability_band"] == PROBABILITY_BANDS
    assert defs["liquidity_level"] == LIQUIDITY_LEVELS
    assert defs["holding_period"] == HOLDING_PERIODS
    assert defs["trade_direction"] == TRADE_DIRECTIONS

    # Spot-check one populated dimension: by_strategy has exactly two
    # buckets (alpha, beta), sorted by total_pnl desc (alpha=+2 first,
    # beta=-1 second).
    by_strat = out["by_strategy"]
    assert {b["bucket"] for b in by_strat} == {"alpha", "beta"}
    assert by_strat[0]["bucket"] == "alpha"
    assert by_strat[0]["total_pnl"] == pytest.approx(2.0)
    assert by_strat[1]["bucket"] == "beta"
    assert by_strat[1]["total_pnl"] == pytest.approx(-1.0)


# ── 5. profit_factor handles no-loss case ───────────────────────────────────


async def test_profit_factor_handles_no_loss_case(set_trades):
    """``profit_factor`` must be ``None`` when ``gross_loss == 0`` (the
    documented divide-by-zero guard on ``_aggregate_bucket``). This is
    the all-winners / fresh-strategy edge case. The guard fires
    identically across every dimension's roll-up (strategy, confidence,
    direction, …).
    """
    # All winning trades — no losses anywhere.
    set_trades([
        _trade(
            position_id="p-w1",
            strategy="winner",
            direction="BUY",
            confidence=0.80,
            pnl=1.0,
        ),
        _trade(
            position_id="p-w2",
            strategy="winner",
            direction="BUY",
            confidence=0.80,
            pnl=4.0,
        ),
        _trade(
            position_id="p-w3",
            strategy="winner",
            direction="BUY",
            confidence=0.80,
            pnl=2.0,
        ),
    ])

    # (a) Strategy dimension — the "winner" bucket is all wins.
    by_strat = await attribute_by_strategy()
    assert len(by_strat) == 1
    winner = by_strat[0]
    assert winner["bucket"] == "winner"
    assert winner["count"] == 3
    assert winner["wins"] == 3
    assert winner["losses"] == 0
    assert winner["gross_loss"] == pytest.approx(0.0)
    assert winner["gross_profit"] == pytest.approx(7.0)
    assert winner["profit_factor"] is None  # documented divide-by-zero guard

    # (b) Confidence dimension — 0.80 falls in "high" [0.70, 0.85).
    by_conf = await attribute_by_confidence_bucket()
    high = next(b for b in by_conf if b["bucket"] == "high")
    assert high["count"] == 3
    assert high["losses"] == 0
    assert high["gross_loss"] == pytest.approx(0.0)
    assert high["gross_profit"] == pytest.approx(7.0)
    assert high["profit_factor"] is None

    # (c) Trade direction dimension — all 3 in BUY.
    by_dir = await attribute_by_trade_direction()
    buy = next(b for b in by_dir if b["bucket"] == "BUY")
    assert buy["count"] == 3
    assert buy["losses"] == 0
    assert buy["gross_loss"] == pytest.approx(0.0)
    assert buy["gross_profit"] == pytest.approx(7.0)
    assert buy["profit_factor"] is None


# ── 6. expectancy = (win_rate * avg_win) + (loss_rate * avg_loss) ────────────


async def test_expectancy_identity_holds(set_trades):
    """The per-bucket expectancy (== the bucket's ``avg_pnl`` field)
    must equal ``(win_rate * avg_win) + (loss_rate * avg_loss)`` — the
    canonical trading-math identity — where ``avg_loss`` is the
    *signed* (i.e. negative) average loss and ``loss_rate = 1 -
    win_rate``.

    Seed: pnls = [3, -2, 5, -4, 7]
      wins   = 3 (3, 5, 7)   losses = 2 (-2, -4)
      win_rate = 3/5 = 0.6   loss_rate = 2/5 = 0.4
      gross_profit = 15      gross_loss   = 6
      avg_win  = 15/3 = 5.0  avg_loss_signed = -6/2 = -3.0
      expectancy = 0.6*5 + 0.4*(-3) = 3 - 1.2 = 1.8
      avg_pnl  = 9/5 = 1.8  ✓
    """
    set_trades([
        _trade(position_id="p-1", strategy="ml_sig_v1",
               direction="BUY", confidence=0.75, pnl=3.0),
        _trade(position_id="p-2", strategy="ml_sig_v1",
               direction="BUY", confidence=0.75, pnl=-2.0),
        _trade(position_id="p-3", strategy="ml_sig_v1",
               direction="BUY", confidence=0.75, pnl=5.0),
        _trade(position_id="p-4", strategy="ml_sig_v1",
               direction="BUY", confidence=0.75, pnl=-4.0),
        _trade(position_id="p-5", strategy="ml_sig_v1",
               direction="BUY", confidence=0.75, pnl=7.0),
    ])
    by_dir = await attribute_by_trade_direction()
    buy = next(b for b in by_dir if b["bucket"] == "BUY")

    # Bucket roll-up sanity.
    assert buy["count"] == 5
    assert buy["wins"] == 3
    assert buy["losses"] == 2
    assert buy["win_rate"] == pytest.approx(3 / 5)
    assert buy["gross_profit"] == pytest.approx(15.0)
    assert buy["gross_loss"] == pytest.approx(6.0)

    # The canonical identity:
    #   expectancy = win_rate * avg_win + loss_rate * avg_loss
    # with ``avg_loss`` taken as the signed (negative) average loss.
    win_rate = buy["win_rate"]
    loss_rate = 1.0 - win_rate
    avg_win = buy["gross_profit"] / buy["wins"]               # positive
    avg_loss_signed = -buy["gross_loss"] / buy["losses"]      # negative
    expected_expectancy = win_rate * avg_win + loss_rate * avg_loss_signed

    assert expected_expectancy == pytest.approx(1.8)
    # The bucket's own avg_pnl field must equal this expectancy.
    assert buy["avg_pnl"] == pytest.approx(expected_expectancy)
    # Cross-check against total_pnl / count.
    assert buy["avg_pnl"] == pytest.approx(9.0 / 5)
    assert buy["total_pnl"] == pytest.approx(9.0)


# ── 7. Empty trades returns zeros ───────────────────────────────────────────


async def test_empty_trades_returns_zeros(set_trades):
    """An empty trade set must produce:

      * ``attribute_by_strategy()`` → empty list (no buckets to roll up,
        since the strategy space is open-ended — there's no fixed
        vocabulary to pre-list empty buckets for).
      * The six fixed-vocabulary dimensions → their full bucket lists
        with every bucket zeroed-out (``count=0``, ``total_pnl=0.0``,
        ``avg_pnl=0.0``, ``win_rate=0.0``, ``gross_profit=0.0``,
        ``gross_loss=0.0``, ``profit_factor=None``,
        ``capital_deployed=0.0``) — the dashboard-stable-schema contract
        so a fresh deployment still shows every bucket row.
      * ``get_full_attribution()`` → ``summary`` with zeroed stats
        (``count=0``, ``win_rate=0.0``, ``profit_factor=None``) and
        every ``by_*`` dimension zeroed as above.
    """
    set_trades([])

    # (a) attribute_by_strategy → empty list.
    by_strat = await attribute_by_strategy()
    assert by_strat == []

    # (b) Fixed-vocabulary dimensions: each returns the full bucket list,
    #     every bucket zeroed-out.
    by_conf = await attribute_by_confidence_bucket()
    assert [b["bucket"] for b in by_conf] == CONFIDENCE_BUCKETS
    for bucket in by_conf:
        assert bucket["count"] == 0
        assert bucket["total_pnl"] == 0.0
        assert bucket["avg_pnl"] == 0.0
        assert bucket["win_rate"] == 0.0
        assert bucket["wins"] == 0
        assert bucket["losses"] == 0
        assert bucket["gross_profit"] == 0.0
        assert bucket["gross_loss"] == 0.0
        assert bucket["profit_factor"] is None
        assert bucket["capital_deployed"] == 0.0
        assert bucket["avg_holding_seconds"] == 0.0

    # (c) Trade direction dimension: same zeroed-out contract.
    by_dir = await attribute_by_trade_direction()
    assert [b["bucket"] for b in by_dir] == TRADE_DIRECTIONS
    for bucket in by_dir:
        assert bucket["count"] == 0
        assert bucket["total_pnl"] == 0.0
        assert bucket["profit_factor"] is None

    # (d) get_full_attribution: zeroed summary + zeroed every dimension.
    full = await get_full_attribution()
    assert full["summary"]["count"] == 0
    assert full["summary"]["win_rate"] == 0.0
    assert full["summary"]["profit_factor"] is None
    assert full["summary"]["strategies_count"] == 0
    assert full["summary"]["total_pnl"] == 0.0
    assert full["summary"]["gross_profit"] == 0.0
    assert full["summary"]["gross_loss"] == 0.0

    # by_strategy is empty (open-ended vocabulary).
    assert full["by_strategy"] == []

    # The six fixed-vocabulary dimensions: each its full bucket list,
    # every bucket zeroed-out.
    assert len(full["by_confidence_bucket"]) == len(CONFIDENCE_BUCKETS)
    assert len(full["by_edge_bucket"]) == len(EDGE_BUCKETS)
    assert len(full["by_probability_band"]) == len(PROBABILITY_BANDS)
    assert len(full["by_liquidity_level"]) == len(LIQUIDITY_LEVELS)
    assert len(full["by_holding_period"]) == len(HOLDING_PERIODS)
    assert len(full["by_trade_direction"]) == len(TRADE_DIRECTIONS)

    # Spot-check: every bucket across every fixed-vocabulary dimension
    # is zeroed-out.
    for dim_key, expected_len in [
        ("by_confidence_bucket", len(CONFIDENCE_BUCKETS)),
        ("by_edge_bucket", len(EDGE_BUCKETS)),
        ("by_probability_band", len(PROBABILITY_BANDS)),
        ("by_liquidity_level", len(LIQUIDITY_LEVELS)),
        ("by_holding_period", len(HOLDING_PERIODS)),
        ("by_trade_direction", len(TRADE_DIRECTIONS)),
    ]:
        assert len(full[dim_key]) == expected_len
        for bucket in full[dim_key]:
            assert bucket["count"] == 0
            assert bucket["total_pnl"] == 0.0
            assert bucket["profit_factor"] is None

    # bucket_definitions legend still present even on empty trades.
    assert "bucket_definitions" in full
    assert full["bucket_definitions"]["confidence_bucket"] == CONFIDENCE_BUCKETS
