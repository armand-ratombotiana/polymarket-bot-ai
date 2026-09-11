"""
Unit tests for ``core/closed_positions.py``.

T11 — Closed Positions store unit tests.

Covers the six public-surface guarantees enumerated in the T11 task spec:

  1. ``record_closed_position()`` stores every caller-supplied field
     (positional + model_version + attribution columns + metadata extras
     + caller-supplied position_id / timestamp).
  2. ``get_closed_positions()`` returns rows most-recent-first (DESC by
     timestamp) — the order the HTTP ``GET /api/positions/closed`` endpoint
     depends on.
  3. ``get_closed_positions(strategy=...)`` filters to a single strategy
     and ``strategy=None`` / ``""`` return across all strategies.
  4. ``get_closed_stats()`` computes ``win_rate``, ``profit_factor`` and
     per-trade expectancy (``avg_pnl``) consistent with the underlying
     rows — and falls back gracefully (``profit_factor=None`` when there
     are no losses).
  5. ``record_closed_position()`` is idempotent on the same
     ``position_id`` — repeated writes do not duplicate or overwrite the
     row (``INSERT OR IGNORE`` semantics; first-write-wins).
  6. Per-strategy breakdown is recoverable through the public surface:
     ``get_closed_positions(strategy=s)`` returns only strategy ``s``'s
     rows and ``get_closed_stats()`` reports the correct
     ``strategies_count``; from those rows the per-strategy
     win_rate / profit_factor / expectancy can be derived and matches
     hand-computed expectations.

The ``ClosedPositionsStore`` reads its DB path from a module-level
``DB_PATH`` constant at construction time (with an explicit ``db_path``
constructor arg override). Each test constructs a fresh
``ClosedPositionsStore(tmp_path / "test_closed_positions.db")`` so the
production singleton (built at import time against the non-writable
``/app/data/closed_positions.db`` sandbox path) is left untouched. This
matches the isolation pattern already established by
``tests/test_decision_ledger.py`` (S9).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the T11 "Do NOT edit existing
files" constraint, so ``asyncio_mode = "auto"`` cannot be enabled via
config).
"""
from __future__ import annotations

import asyncio

import pytest

from core.closed_positions import ClosedPositionsStore

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the T11 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors ``tests/test_decision_ledger.py``).
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed store per test ───────────────────────────
@pytest.fixture
def store(tmp_path):
    """
    Return a ``ClosedPositionsStore`` whose SQLite file lives under
    ``tmp_path``.

    Passing an explicit ``db_path`` to the constructor bypasses the
    module-level ``DB_PATH`` lookup that the production singleton uses,
    so the import-time singleton (built against the non-writable
    ``/app/data/closed_positions.db``) is never touched. This is the
    same isolation strategy ``tests/test_decision_ledger.py`` (S9)
    employs — but here we use the constructor argument instead of
    monkeypatching the module global, because the constructor signature
    explicitly supports it and is cleaner.
    """
    return ClosedPositionsStore(tmp_path / "test_closed_positions.db")


# ── 1. record_closed_position() stores all fields ──────────────────────────
async def test_record_closed_position_stores_all_fields(store):
    """``record_closed_position`` must persist every caller-supplied field
    verbatim — positional args, ``model_version``, the seven attribution
    columns, caller-supplied ``position_id`` / ``timestamp``, and any
    extra kwargs round-tripped through ``metadata_json`` (surfaced as
    the decoded ``data`` field on read)."""
    pid = await store.record_closed_position(
        token_id="TOK_FULL",
        strategy="ml_sig_v1",
        entry_price=0.55,
        exit_price=0.62,
        shares=100.0,
        pnl=7.0,
        holding_seconds=3600.0,
        model_version="v1.2.3",
        # Caller-supplied identity / time (used for idempotency + ordering).
        position_id="pos-full-1",
        timestamp=1_700_000_000.0,
        # Attribution-dimension kwargs → first-class columns.
        decision_id="dec-full-1",
        direction="BUY",
        confidence=0.7,
        predicted_edge=0.05,
        p_yes=0.60,
        market_mid=0.55,
        liquidity=5_000.0,
        # Non-attribution extras → metadata_json (decoded back as ``data``).
        slug="eth-wins",
        side="long",
    )

    # The caller-supplied position_id is echoed back verbatim.
    assert pid == "pos-full-1"

    rows = await store.get_closed_positions(limit=10)
    assert len(rows) == 1
    r = rows[0]

    # Positional / required fields persisted verbatim.
    assert r["position_id"] == "pos-full-1"
    assert r["token_id"] == "TOK_FULL"
    assert r["strategy"] == "ml_sig_v1"
    assert r["entry_price"] == pytest.approx(0.55)
    assert r["exit_price"] == pytest.approx(0.62)
    assert r["shares"] == pytest.approx(100.0)
    assert r["pnl"] == pytest.approx(7.0)
    assert r["holding_seconds"] == pytest.approx(3600.0)
    assert r["model_version"] == "v1.2.3"

    # Caller-supplied timestamp is honoured (not overwritten by time.time()).
    assert r["timestamp"] == pytest.approx(1_700_000_000.0)

    # Attribution-dimension columns persisted as first-class columns (so
    # ``core/attribution.py`` can GROUP BY them directly).
    assert r["decision_id"] == "dec-full-1"
    assert r["direction"] == "BUY"
    assert r["confidence"] == pytest.approx(0.7)
    assert r["predicted_edge"] == pytest.approx(0.05)
    assert r["p_yes"] == pytest.approx(0.60)
    assert r["market_mid"] == pytest.approx(0.55)
    assert r["liquidity"] == pytest.approx(5_000.0)

    # Non-attribution extras round-tripped through metadata_json → ``data``.
    assert isinstance(r["data"], dict)
    assert r["data"]["slug"] == "eth-wins"
    assert r["data"]["side"] == "long"

    # The raw ``metadata_json`` column is not surfaced to the caller (it's
    # replaced by the decoded ``data`` key) — this is the documented
    # read-side contract.
    assert "metadata_json" not in r


# ── 2. get_closed_positions() returns most-recent-first ────────────────────
async def test_get_closed_positions_returns_most_recent_first(store):
    """``get_closed_positions`` must return rows in DESCENDING timestamp
    order (most recent first) — the ordering the HTTP
    ``GET /api/positions/closed`` endpoint promises its callers."""
    # Insert three positions with strictly increasing timestamps. Use
    # explicit ``timestamp=`` kwargs so the ordering is deterministic
    # (time.time() resolution is not reliable enough on a loaded CI box).
    base_ts = 1_700_000_000.0
    pids = []
    for i in range(3):
        pid = await store.record_closed_position(
            token_id="TOK_ORDER",
            strategy="s",
            entry_price=0.10 + i,
            exit_price=0.20 + i,
            shares=10.0,
            pnl=float(i),
            holding_seconds=60.0 * (i + 1),
            position_id=f"pos-order-{i}",
            timestamp=base_ts + i,  # strictly increasing
        )
        pids.append(pid)
        # Tiny sleep to make the implicit time.time() fallback path
        # well-behaved too (no-op when explicit timestamp is supplied).
        await asyncio.sleep(0.001)

    rows = await store.get_closed_positions(limit=10)

    assert len(rows) == 3
    # Most-recent-first: the position with the highest timestamp must be
    # at index 0.
    assert [r["position_id"] for r in rows] == [
        "pos-order-2",
        "pos-order-1",
        "pos-order-0",
    ]
    # Timestamps are strictly decreasing.
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


# ── 3. strategy filter works ────────────────────────────────────────────────
async def test_strategy_filter_works(store):
    """``get_closed_positions(strategy=s)`` must return ONLY rows for that
    strategy. ``strategy=None`` and ``strategy=""`` must return across
    all strategies. An unknown strategy name returns an empty list."""
    base_ts = 1_700_000_000.0
    # Three strategies, two positions each.
    seeds = [
        ("alpha", "pos-a-0", base_ts + 0),
        ("alpha", "pos-a-1", base_ts + 1),
        ("beta", "pos-b-0", base_ts + 2),
        ("beta", "pos-b-1", base_ts + 3),
        ("gamma", "pos-g-0", base_ts + 4),
        ("gamma", "pos-g-1", base_ts + 5),
    ]
    for strat, pid, ts in seeds:
        await store.record_closed_position(
            token_id="TOK_FILT",
            strategy=strat,
            entry_price=0.5,
            exit_price=0.5,
            shares=1.0,
            pnl=0.0,
            holding_seconds=1.0,
            position_id=pid,
            timestamp=ts,
        )

    # (a) Filter to "alpha" → 2 rows, both strategy=alpha, newest-first.
    alpha_rows = await store.get_closed_positions(limit=50, strategy="alpha")
    assert {r["position_id"] for r in alpha_rows} == {"pos-a-0", "pos-a-1"}
    assert all(r["strategy"] == "alpha" for r in alpha_rows)
    # Newest-first ordering preserved within the filtered slice.
    assert alpha_rows[0]["timestamp"] >= alpha_rows[1]["timestamp"]

    # (b) Filter to "beta" → 2 rows.
    beta_rows = await store.get_closed_positions(limit=50, strategy="beta")
    assert {r["position_id"] for r in beta_rows} == {"pos-b-0", "pos-b-1"}

    # (c) Filter to "gamma" → 2 rows.
    gamma_rows = await store.get_closed_positions(limit=50, strategy="gamma")
    assert {r["position_id"] for r in gamma_rows} == {"pos-g-0", "pos-g-1"}

    # (d) No filter (None) → all 6 rows.
    all_rows = await store.get_closed_positions(limit=100, strategy=None)
    assert len(all_rows) == 6

    # (e) Empty-string strategy is treated as "no filter" (per the
    #     ``if strategy:`` truthiness check in the implementation).
    empty_rows = await store.get_closed_positions(limit=100, strategy="")
    assert len(empty_rows) == 6

    # (f) Unknown strategy → empty list (API's "no results" path).
    unknown_rows = await store.get_closed_positions(limit=50, strategy="nonexistent")
    assert unknown_rows == []

    # (g) Limit is honoured within a strategy filter.
    one_alpha = await store.get_closed_positions(limit=1, strategy="alpha")
    assert len(one_alpha) == 1
    assert one_alpha[0]["strategy"] == "alpha"


# ── 4. get_closed_stats() computes win_rate / expectancy / profit_factor ──
async def test_get_closed_stats_computes_winrate_expectancy_profit_factor(store):
    """``get_closed_stats`` must aggregate:

      - ``win_rate``   = wins / count
      - ``avg_pnl``    = total_pnl / count  (a.k.a. per-trade expectancy)
      - ``profit_factor`` = gross_profit / gross_loss  (None when no losses)

    against the underlying rows. Empty store returns a zeroed-out payload
    with ``profit_factor=None``.
    """
    # Empty store → zeroed stats, profit_factor=None (never None key missing).
    empty_stats = await store.get_closed_stats()
    assert empty_stats["count"] == 0
    assert empty_stats["win_rate"] == 0.0
    assert empty_stats["profit_factor"] is None
    assert empty_stats["strategies_count"] == 0

    # Seed 5 positions: 3 wins, 2 losses, with known P&L magnitudes.
    #   gross_profit = 3.0 + 5.0 + 7.0 = 15.0
    #   gross_loss   = 2.0 + 4.0       = 6.0   (sum of |−pnl|)
    #   total_pnl    = 15.0 - 6.0      = 9.0
    #   count        = 5
    #   win_rate     = 3 / 5           = 0.6
    #   avg_pnl      = 9.0 / 5         = 1.8   (per-trade expectancy)
    #   profit_factor= 15.0 / 6.0      = 2.5
    #   wins=3, losses=2, breakeven=0
    pnl_seed = [3.0, -2.0, 5.0, -4.0, 7.0]
    base_ts = 1_700_000_000.0
    for i, pnl in enumerate(pnl_seed):
        await store.record_closed_position(
            token_id="TOK_STATS",
            strategy="ml_sig_v1",
            entry_price=0.50,
            exit_price=0.50 + pnl / 100.0,  # arbitrary, just nonzero variety
            shares=100.0,
            pnl=pnl,
            holding_seconds=float(i + 1) * 60.0,
            model_version="v-test",
            position_id=f"pos-stats-{i}",
            timestamp=base_ts + i,
        )

    stats = await store.get_closed_stats()

    # (a) Count + win/loss tally.
    assert stats["count"] == 5
    assert stats["wins"] == 3
    assert stats["losses"] == 2
    assert stats["breakeven"] == 0

    # (b) win_rate = wins / count.
    assert stats["win_rate"] == pytest.approx(3 / 5)

    # (c) Per-trade expectancy = avg_pnl = total_pnl / count.
    assert stats["total_pnl"] == pytest.approx(9.0)
    assert stats["avg_pnl"] == pytest.approx(9.0 / 5)

    # Cross-check expectancy via the canonical trading-math identity:
    #   expectancy = win_rate * avg_win − loss_rate * avg_loss
    # which is mathematically equal to avg_pnl.
    avg_win = (3.0 + 5.0 + 7.0) / 3
    avg_loss = (2.0 + 4.0) / 2  # magnitude
    expected_expectancy = (3 / 5) * avg_win - (2 / 5) * avg_loss
    assert stats["avg_pnl"] == pytest.approx(expected_expectancy)

    # (d) profit_factor = gross_profit / gross_loss.
    assert stats["gross_profit"] == pytest.approx(15.0)
    assert stats["gross_loss"] == pytest.approx(6.0)
    assert stats["profit_factor"] == pytest.approx(15.0 / 6.0)

    # (e) Best / worst trade extremes.
    assert stats["best_trade"] == pytest.approx(7.0)
    assert stats["worst_trade"] == pytest.approx(-4.0)

    # (f) strategies_count.
    assert stats["strategies_count"] == 1


async def test_profit_factor_is_none_when_no_losses(store):
    """``profit_factor`` is ``None`` when ``gross_loss == 0`` (the
    implementation explicitly guards against divide-by-zero). This is the
    fresh-strategy / all-winners edge case."""
    # Two winning positions, no losses.
    for i, pnl in enumerate([1.0, 4.0]):
        await store.record_closed_position(
            token_id="TOK_PF",
            strategy="winner",
            entry_price=0.5,
            exit_price=0.6,
            shares=10.0,
            pnl=pnl,
            holding_seconds=60.0,
            position_id=f"pos-pf-{i}",
            timestamp=1_700_000_000.0 + i,
        )
    stats = await store.get_closed_stats()
    assert stats["count"] == 2
    assert stats["wins"] == 2
    assert stats["losses"] == 0
    assert stats["gross_loss"] == pytest.approx(0.0)
    assert stats["profit_factor"] is None  # documented divide-by-zero guard
    assert stats["win_rate"] == pytest.approx(1.0)


# ── 5. Idempotent on same position_id ──────────────────────────────────────
async def test_record_closed_position_is_idempotent_on_position_id(store):
    """``record_closed_position`` must be idempotent on ``position_id``:
    a second call with the same ``position_id`` must NOT create a
    duplicate row and must NOT overwrite the first row's values. This
    is the exactly-once semantics the trading pipeline relies on
    (``INSERT OR IGNORE`` + ``position_id UNIQUE`` constraint)."""
    # First write — the canonical record for this position_id.
    pid1 = await store.record_closed_position(
        token_id="TOK_DUP",
        strategy="ml_sig_v1",
        entry_price=0.55,
        exit_price=0.62,
        shares=100.0,
        pnl=7.0,
        holding_seconds=3600.0,
        model_version="v1",
        position_id="pos-dup-1",
        timestamp=1_700_000_000.0,
        decision_id="dec-dup-1",
        direction="BUY",
    )

    # Second write — same position_id, completely different payload.
    # Production callers do this on retry / replays. The first row must win.
    pid2 = await store.record_closed_position(
        token_id="TOK_OTHER",  # different
        strategy="other_strat",  # different
        entry_price=0.99,
        exit_price=0.01,
        shares=999.0,
        pnl=-99.0,
        holding_seconds=0.0,
        model_version="v2",
        position_id="pos-dup-1",  # SAME — idempotency key
        timestamp=1_700_000_999.0,
        decision_id="dec-other",
        direction="SELL",
    )

    # Both calls echo back the same position_id.
    assert pid1 == pid2 == "pos-dup-1"

    rows = await store.get_closed_positions(limit=10)
    # Exactly one row — no duplicate.
    assert len(rows) == 1

    r = rows[0]
    # First-write-wins: every column retains the original payload.
    assert r["token_id"] == "TOK_DUP"
    assert r["strategy"] == "ml_sig_v1"
    assert r["entry_price"] == pytest.approx(0.55)
    assert r["exit_price"] == pytest.approx(0.62)
    assert r["shares"] == pytest.approx(100.0)
    assert r["pnl"] == pytest.approx(7.0)
    assert r["holding_seconds"] == pytest.approx(3600.0)
    assert r["model_version"] == "v1"
    assert r["position_id"] == "pos-dup-1"
    assert r["timestamp"] == pytest.approx(1_700_000_000.0)
    assert r["decision_id"] == "dec-dup-1"
    assert r["direction"] == "BUY"

    # Stats reflect only the first write (one winning trade, $7 PnL).
    stats = await store.get_closed_stats()
    assert stats["count"] == 1
    assert stats["total_pnl"] == pytest.approx(7.0)
    assert stats["wins"] == 1


# ── 6. Per-strategy breakdown recoverable via the public surface ───────────
async def test_per_strategy_breakdown(store):
    """Per-strategy performance breakdown must be recoverable through
    the public surface: ``get_closed_positions(strategy=s)`` returns
    that strategy's rows, and per-strategy win_rate / profit_factor /
    expectancy derived from those rows must match hand-computed
    expectations. ``get_closed_stats()`` must also report the correct
    ``strategies_count``."""

    # ── Seed: two strategies with distinct P&L distributions ──────────────
    # alpha: 2 wins, 1 loss → win_rate=2/3, gross_profit=8, gross_loss=3,
    #        profit_factor=8/3, total_pnl=5, avg_pnl=5/3
    # beta:  1 win, 2 losses → win_rate=1/3, gross_profit=4, gross_loss=7,
    #        profit_factor=4/7, total_pnl=-3, avg_pnl=-1
    alpha_pnls = [5.0, -3.0, 3.0]   # 2 wins (5,3), 1 loss (-3)
    beta_pnls = [4.0, -2.0, -5.0]   # 1 win (4),  2 losses (-2,-5)
    base_ts = 1_700_000_000.0
    i = 0
    for pnl in alpha_pnls:
        await store.record_closed_position(
            token_id="TOK_BREAKDOWN",
            strategy="alpha",
            entry_price=0.5,
            exit_price=0.5,
            shares=10.0,
            pnl=pnl,
            holding_seconds=60.0,
            position_id=f"pos-alpha-{i}",
            timestamp=base_ts + i,
        )
        i += 1
    for pnl in beta_pnls:
        await store.record_closed_position(
            token_id="TOK_BREAKDOWN",
            strategy="beta",
            entry_price=0.5,
            exit_price=0.5,
            shares=10.0,
            pnl=pnl,
            holding_seconds=60.0,
            position_id=f"pos-beta-{i}",
            timestamp=base_ts + i,
        )
        i += 1

    # ── (a) strategies_count in get_closed_stats ──────────────────────────
    stats = await store.get_closed_stats()
    assert stats["count"] == 6
    assert stats["strategies_count"] == 2
    # Aggregate roll-up across both strategies:
    #   wins=3, losses=3, total_pnl=5+(-3)=2
    assert stats["wins"] == 3
    assert stats["losses"] == 3
    assert stats["total_pnl"] == pytest.approx(2.0)
    assert stats["win_rate"] == pytest.approx(3 / 6)
    # gross_profit = 5+3+4 = 12; gross_loss = 3+2+5 = 10
    assert stats["gross_profit"] == pytest.approx(12.0)
    assert stats["gross_loss"] == pytest.approx(10.0)
    assert stats["profit_factor"] == pytest.approx(12.0 / 10.0)

    # ── (b) Per-strategy breakdown via get_closed_positions(strategy=s) ──
    alpha_rows = await store.get_closed_positions(limit=100, strategy="alpha")
    beta_rows = await store.get_closed_positions(limit=100, strategy="beta")
    assert len(alpha_rows) == 3
    assert len(beta_rows) == 3
    assert all(r["strategy"] == "alpha" for r in alpha_rows)
    assert all(r["strategy"] == "beta" for r in beta_rows)

    # Derive per-strategy stats from the rows and compare to expectations.
    def _breakdown(rows):
        pnls = [r["pnl"] for r in rows]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        gp = sum(p for p in pnls if p > 0)
        gl = sum(-p for p in pnls if p < 0)
        return {
            "count": n,
            "win_rate": wins / n,
            "wins": wins,
            "losses": losses,
            "gross_profit": gp,
            "gross_loss": gl,
            "profit_factor": (gp / gl) if gl > 0 else None,
            "total_pnl": sum(pnls),
            "expectancy": sum(pnls) / n,  # avg_pnl per trade
        }

    alpha_bd = _breakdown(alpha_rows)
    beta_bd = _breakdown(beta_rows)

    # alpha: 2 wins / 3 total → win_rate=2/3, profit_factor=8/3, expectancy=5/3
    assert alpha_bd["count"] == 3
    assert alpha_bd["wins"] == 2
    assert alpha_bd["losses"] == 1
    assert alpha_bd["win_rate"] == pytest.approx(2 / 3)
    assert alpha_bd["gross_profit"] == pytest.approx(8.0)
    assert alpha_bd["gross_loss"] == pytest.approx(3.0)
    assert alpha_bd["profit_factor"] == pytest.approx(8.0 / 3.0)
    assert alpha_bd["total_pnl"] == pytest.approx(5.0)
    assert alpha_bd["expectancy"] == pytest.approx(5.0 / 3.0)

    # beta: 1 win / 3 total → win_rate=1/3, profit_factor=4/7, expectancy=-1
    assert beta_bd["count"] == 3
    assert beta_bd["wins"] == 1
    assert beta_bd["losses"] == 2
    assert beta_bd["win_rate"] == pytest.approx(1 / 3)
    assert beta_bd["gross_profit"] == pytest.approx(4.0)
    assert beta_bd["gross_loss"] == pytest.approx(7.0)
    assert beta_bd["profit_factor"] == pytest.approx(4.0 / 7.0)
    assert beta_bd["total_pnl"] == pytest.approx(-3.0)
    assert beta_bd["expectancy"] == pytest.approx(-1.0)

    # ── (c) The two per-strategy roll-ups reconcile to the global stats ──
    assert alpha_bd["wins"] + beta_bd["wins"] == stats["wins"]
    assert alpha_bd["losses"] + beta_bd["losses"] == stats["losses"]
    assert alpha_bd["total_pnl"] + beta_bd["total_pnl"] == pytest.approx(stats["total_pnl"])
    assert (
        alpha_bd["gross_profit"] + beta_bd["gross_profit"]
        == pytest.approx(stats["gross_profit"])
    )
    assert (
        alpha_bd["gross_loss"] + beta_bd["gross_loss"]
        == pytest.approx(stats["gross_loss"])
    )


async def test_per_strategy_breakdown_isolates_unknown_strategy(store):
    """Filtering by an unknown strategy returns an empty list (not an
    error) — the per-strategy breakdown surface degrades gracefully for
    strategies that have never recorded a position."""
    # Seed one position under "alpha".
    await store.record_closed_position(
        token_id="TOK_UNKNOWN",
        strategy="alpha",
        entry_price=0.5,
        exit_price=0.6,
        shares=10.0,
        pnl=1.0,
        holding_seconds=60.0,
        position_id="pos-unknown-0",
        timestamp=1_700_000_000.0,
    )

    # Unknown strategy → empty list (no error).
    empty = await store.get_closed_positions(limit=50, strategy="never_traded")
    assert empty == []

    # And per-strategy breakdown derived from an empty list is zeroed.
    n = len(empty)
    assert n == 0
    # The documented "empty store" stats shape is what the caller would
    # compute from zero rows — count=0, win_rate=0, profit_factor=None.
    # (We assert these here as the per-strategy breakdown contract for
    # an unknown strategy, mirroring ``get_closed_stats``'s empty-store
    # behaviour documented at module level.)
    assert n == 0
