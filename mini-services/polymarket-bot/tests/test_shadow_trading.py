"""
Unit tests for ``core/shadow_trading.py``.

U3 — Shadow Trading Journal unit tests.

Covers the six public-surface guarantees enumerated in the U3 task spec:

  1. ``record_shadow_trade()`` stores every caller-supplied field
     (``decision_id``, ``token_id``, ``strategy``, ``side``, ``price``,
     ``size``, ``predicted_edge``, ``confidence``) and returns a
     non-``None`` row id.
  2. ``get_shadow_trades()`` returns rows most-recent-first (DESC by
     ``timestamp``) — the ordering the HTTP ``GET /api/shadow/trades``
     endpoint promises its callers.
  3. ``get_shadow_trades(strategy=s)`` filters to a single strategy;
     ``strategy=None`` / ``""`` return across all strategies; an unknown
     strategy returns an empty list.
  4. ``get_shadow_vs_live_comparison()`` returns a payload carrying BOTH
     the shadow side and the live side (plus the per-strategy merge),
     with the documented sub-keys on each side.
  5. The comparison's per-strategy merge supports an unambiguous
     "shadow outperforms" verdict when the shadow side shows positive
     theoretical edge while the live side shows non-positive realized
     P&L for the same strategy.
  6. ``side`` is normalised to upper-case on write — lowercase ``"buy"``
     is stored as ``"BUY"`` (and the ``_normalise_side`` helper is
     exercised directly for the ``"BUY"`` / ``"Sell"`` / ``None`` /
     enum-with-``.value`` variants).

The ``shadow_trading`` module reads its DB path from a module-level
``DB_PATH`` constant at *call time* (every public function looks the
global up afresh on each invocation). Each test monkeypatches
``core.shadow_trading.DB_PATH`` to a fresh ``tmp_path``-scoped SQLite
file and then calls the module's own ``_init_db()`` to (re)create the
``shadow_trades`` schema on the test path. The import-time singleton
``DB_PATH`` (resolved from the conftest's ``DECISION_LEDGER_DB_PATH``
redirect to ``/tmp/pmbot_conftest_isolation/shadow_trades.db``) is
therefore left untouched, and the module-level ``_init_db()`` call that
ran at import time has no bearing on what the tests see.

The live side of ``get_shadow_vs_live_comparison()`` is sourced via a
lazy ``from core.closed_positions import closed_positions`` import
inside ``_live_summary``. To keep tests 4 and 5 hermetic, we monkeypatch
``core.closed_positions.closed_positions`` to a ``_FakeClosedPositions``
double that exposes the two async methods ``_live_summary`` actually
calls (``get_closed_stats`` + ``get_closed_positions``). This isolates
the comparison from any prior test that may have written to the real
closed-positions singleton, and lets us drive the "shadow outperforms"
scenario deterministically.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the U3 "Do NOT edit existing
files" constraint, so ``asyncio_mode = "auto"`` cannot be enabled via
config — mirrors ``tests/test_decision_ledger.py`` (S9) and
``tests/test_closed_positions.py`` (T11)).
"""
from __future__ import annotations

import asyncio

import pytest

from core import shadow_trading
from core.shadow_trading import (
    _normalise_side,
    get_shadow_trades,
    get_shadow_vs_live_comparison,
    record_shadow_trade,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the U3 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors ``tests/test_decision_ledger.py`` (S9) and
# ``tests/test_closed_positions.py`` (T11)).
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed shadow journal per test ──────────────────
@pytest.fixture
def shadow_db(monkeypatch, tmp_path):
    """Point ``core.shadow_trading.DB_PATH`` at a fresh ``tmp_path`` SQLite
    file and (re)initialise the ``shadow_trades`` schema on it.

    The module-level ``DB_PATH`` constant is monkeypatched in place — the
    same global-lookup code path every public function in
    ``core.shadow_trading`` uses (each function resolves ``DB_PATH`` from
    the module namespace at *call time*, not at import time). After
    patching, we explicitly re-run ``shadow_trading._init_db()`` so the
    ``shadow_trades`` table + its four indexes exist on the new path; the
    import-time ``_init_db()`` call only created the schema at the
    conftest-redirected ``/tmp/pmbot_conftest_isolation/shadow_trades.db``
    path, not at this test's ``tmp_path``.

    The module-import-time singleton state (the
    ``/tmp/pmbot_conftest_isolation/shadow_trades.db`` file, plus the
    shadow rows any prior test may have written there) is left untouched.
    """
    db_path = tmp_path / "test_shadow_trades.db"
    monkeypatch.setattr("core.shadow_trading.DB_PATH", db_path)
    shadow_trading._init_db()
    return db_path


# ── Test double for the closed_positions singleton ─────────────────────────
class _FakeClosedPositions:
    """Async test double for ``core.closed_positions.closed_positions``.

    ``shadow_trading._live_summary`` lazy-imports the
    ``closed_positions`` singleton and calls exactly two async methods on
    it — ``get_closed_stats()`` and ``get_closed_positions(limit=...)``.
    This double exposes both so the comparison's live side is hermetic
    and deterministic regardless of what the real closed-positions store
    currently holds.

    The returned rows are deep-copied (``dict(r)``) so a caller mutating
    the returned payload cannot corrupt the fixture's seed data.
    """

    def __init__(self, stats: dict, rows: list[dict]) -> None:
        self._stats = dict(stats)
        self._rows = [dict(r) for r in rows]

    async def get_closed_stats(self) -> dict:
        return dict(self._stats)

    async def get_closed_positions(self, limit: int = 1000) -> list[dict]:
        return [dict(r) for r in self._rows[:limit]]


# ── 1. record_shadow_trade() stores all fields ─────────────────────────────
async def test_record_shadow_trade_stores_all_fields(shadow_db):
    """``record_shadow_trade`` must persist every caller-supplied field
    verbatim — ``decision_id``, ``token_id``, ``strategy``, ``side``,
    ``price``, ``size``, ``predicted_edge``, ``confidence`` — and return
    a non-``None`` integer row id that the caller can cross-link to
    other ledgers."""
    row_id = await record_shadow_trade(
        decision_id="dec-full-1",
        token_id="TOK_FULL",
        strategy="ml_sig_v1",
        side="BUY",
        price=0.55,
        size=100.0,
        predicted_edge=0.08,
        confidence=0.72,
    )

    # The returned row id is a positive integer (autoincrement PK).
    assert row_id is not None
    assert isinstance(row_id, int)
    assert row_id > 0

    rows = await get_shadow_trades(limit=10)
    assert len(rows) == 1
    r = rows[0]

    # Identity columns persisted verbatim.
    assert r["id"] == row_id
    assert r["decision_id"] == "dec-full-1"
    assert r["token_id"] == "TOK_FULL"
    assert r["strategy"] == "ml_sig_v1"
    assert r["side"] == "BUY"

    # Numeric columns persisted with full float fidelity.
    assert r["price"] == pytest.approx(0.55)
    assert r["size"] == pytest.approx(100.0)
    assert r["predicted_edge"] == pytest.approx(0.08)
    assert r["confidence"] == pytest.approx(0.72)

    # ``timestamp`` was auto-set to a recent epoch second by the recorder
    # (callers don't supply it — ``record_shadow_trade`` stamps it with
    # ``time.time()``). Sanity: must be a positive REAL post-2023.
    assert isinstance(r["timestamp"], (int, float))
    assert r["timestamp"] > 1_700_000_000.0


# ── 2. get_shadow_trades() returns most-recent-first ───────────────────────
async def test_get_shadow_trades_returns_most_recent_first(shadow_db):
    """``get_shadow_trades`` must return rows in DESCENDING ``timestamp``
    order (most recent first) — the ordering the HTTP
    ``GET /api/shadow/trades`` endpoint promises its callers.

    ``record_shadow_trade`` does NOT accept a caller-supplied timestamp
    (it stamps each row with ``time.time()`` at write time), so to make
    the ordering assertion deterministic on a loaded CI box we insert
    the three rows with a 5 ms ``asyncio.sleep`` between each insert.
    That guarantees strictly increasing ``time.time()`` values (SQLite
    stores REAL with ~µs precision; 5 ms is a comfortable margin)."""
    inserted_ids: list[int] = []
    for i in range(3):
        rid = await record_shadow_trade(
            decision_id=f"dec-order-{i}",
            token_id="TOK_ORDER",
            strategy="s",
            side="BUY",
            price=0.10 + i,
            size=10.0,
            predicted_edge=0.01 * i,
            confidence=0.5,
        )
        assert rid is not None
        inserted_ids.append(rid)
        # Sleep between inserts so each row lands at a strictly greater
        # ``time.time()`` value (the recorder stamps rows itself).
        await asyncio.sleep(0.005)

    rows = await get_shadow_trades(limit=10)

    assert len(rows) == 3
    # Newest-first: the LAST inserted (highest timestamp) must be at
    # index 0; the FIRST inserted must be at index 2.
    assert [r["id"] for r in rows] == list(reversed(inserted_ids))
    # Timestamps are strictly decreasing.
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)
    # Strictly decreasing (no ties) — the 5 ms sleep guarantees this.
    assert timestamps[0] > timestamps[1] > timestamps[2]


# ── 3. strategy filter works ───────────────────────────────────────────────
async def test_get_shadow_trades_strategy_filter_works(shadow_db):
    """``get_shadow_trades(strategy=s)`` must return ONLY rows for that
    strategy. ``strategy=None`` and ``strategy=""`` return across all
    strategies (the implementation truthiness-checks the filter). An
    unknown strategy returns an empty list (the API's "no results"
    path). The ``limit`` parameter is honoured within a filtered slice."""
    # Three strategies, uneven counts: alpha=2, beta=2, gamma=1.
    seeds = [
        ("alpha", "TOK_A0"),
        ("alpha", "TOK_A1"),
        ("beta", "TOK_B0"),
        ("beta", "TOK_B1"),
        ("gamma", "TOK_G0"),
    ]
    for strat, tok in seeds:
        await record_shadow_trade(
            decision_id=f"dec-{tok}",
            token_id=tok,
            strategy=strat,
            side="BUY",
            price=0.5,
            size=10.0,
            predicted_edge=0.02,
            confidence=0.5,
        )
        # Sleep so the most-recent-first assertion within a strategy
        # filter has a deterministic timestamp ordering.
        await asyncio.sleep(0.005)

    # (a) Filter to "alpha" → 2 rows, all strategy=alpha, newest-first.
    alpha_rows = await get_shadow_trades(limit=50, strategy="alpha")
    assert len(alpha_rows) == 2
    assert all(r["strategy"] == "alpha" for r in alpha_rows)
    # Newest-first ordering preserved within the filtered slice.
    assert alpha_rows[0]["timestamp"] >= alpha_rows[1]["timestamp"]

    # (b) Filter to "beta" → 2 rows.
    beta_rows = await get_shadow_trades(limit=50, strategy="beta")
    assert len(beta_rows) == 2
    assert all(r["strategy"] == "beta" for r in beta_rows)

    # (c) Filter to "gamma" → 1 row.
    gamma_rows = await get_shadow_trades(limit=50, strategy="gamma")
    assert len(gamma_rows) == 1
    assert gamma_rows[0]["strategy"] == "gamma"

    # (d) No filter (None) → all 5 rows.
    all_rows_none = await get_shadow_trades(limit=100, strategy=None)
    assert len(all_rows_none) == 5

    # (e) Empty-string strategy is treated as "no filter" (per the
    #     ``str(strategy).strip() or None`` coercion in the implementation).
    all_rows_empty = await get_shadow_trades(limit=100, strategy="")
    assert len(all_rows_empty) == 5

    # (f) Unknown strategy → empty list (API's "no results" path).
    unknown_rows = await get_shadow_trades(limit=50, strategy="nonexistent")
    assert unknown_rows == []

    # (g) Limit is honoured within a strategy filter — only the single
    #     most-recent alpha row is returned.
    one_alpha = await get_shadow_trades(limit=1, strategy="alpha")
    assert len(one_alpha) == 1
    assert one_alpha[0]["strategy"] == "alpha"
    # The single row returned is the most-recent alpha (TOK_A1, inserted
    # last in the seed loop above).
    assert one_alpha[0]["token_id"] == "TOK_A1"


# ── 4. get_shadow_vs_live_comparison() returns both sides ──────────────────
async def test_comparison_returns_both_sides(shadow_db, monkeypatch):
    """``get_shadow_vs_live_comparison`` must return a payload carrying
    BOTH the shadow side and the live side, each with the documented
    sub-keys, plus the per-strategy merge list (``strategies``).

    To keep the live side deterministic we replace the
    ``core.closed_positions.closed_positions`` singleton with a
    ``_FakeClosedPositions`` double (the lazy ``from core.closed_positions
    import closed_positions`` import inside ``_live_summary`` rebinds to
    our double for the duration of the test)."""
    # ── Seed two shadow trades across two strategies ──────────────────
    await record_shadow_trade(
        decision_id="dec-cmp-1",
        token_id="TOK_A",
        strategy="alpha",
        side="BUY",
        price=0.55,
        size=10.0,
        predicted_edge=0.05,
        confidence=0.7,
    )
    await asyncio.sleep(0.005)
    await record_shadow_trade(
        decision_id="dec-cmp-2",
        token_id="TOK_B",
        strategy="beta",
        side="SELL",
        price=0.45,
        size=5.0,
        predicted_edge=-0.02,
        confidence=0.4,
    )

    # ── Mock the live side: one winning closed position for "alpha" ────
    fake_live = _FakeClosedPositions(
        stats={
            "count": 1,
            "total_pnl": 3.0,
            "avg_pnl": 3.0,
            "win_rate": 1.0,
            "total_volume_shares": 10.0,
        },
        rows=[
            {"strategy": "alpha", "pnl": 3.0, "shares": 10.0},
        ],
    )
    monkeypatch.setattr("core.closed_positions.closed_positions", fake_live)

    cmp = await get_shadow_vs_live_comparison()

    # (a) Top-level shape: must carry both sides + the merge list.
    assert set(cmp.keys()) >= {"shadow", "live", "strategies"}

    # (b) Shadow side reflects the two seeded trades.
    shadow = cmp["shadow"]
    assert set(shadow.keys()) >= {
        "count", "total_size", "avg_predicted_edge", "avg_confidence",
        "by_side", "by_strategy",
    }
    assert shadow["count"] == 2
    assert shadow["total_size"] == pytest.approx(15.0)  # 10.0 + 5.0
    # One BUY + one SELL.
    assert shadow["by_side"]["BUY"] == 1
    assert shadow["by_side"]["SELL"] == 1
    # Two distinct strategies in the per-strategy breakdown.
    assert set(shadow["by_strategy"].keys()) == {"alpha", "beta"}
    alpha_shadow = shadow["by_strategy"]["alpha"]
    assert alpha_shadow["count"] == 1
    assert alpha_shadow["total_size"] == pytest.approx(10.0)
    assert alpha_shadow["avg_edge"] == pytest.approx(0.05)
    assert alpha_shadow["avg_conf"] == pytest.approx(0.7)

    # (c) Live side reflects the fake closed-positions payload.
    live = cmp["live"]
    assert set(live.keys()) >= {
        "count", "total_pnl", "avg_pnl", "win_rate",
        "total_volume_shares", "by_strategy",
    }
    assert live["count"] == 1
    assert live["total_pnl"] == pytest.approx(3.0)
    assert live["avg_pnl"] == pytest.approx(3.0)
    assert live["win_rate"] == pytest.approx(1.0)
    assert live["total_volume_shares"] == pytest.approx(10.0)
    assert "alpha" in live["by_strategy"]
    assert live["by_strategy"]["alpha"]["count"] == 1
    assert live["by_strategy"]["alpha"]["total_pnl"] == pytest.approx(3.0)
    assert live["by_strategy"]["alpha"]["avg_pnl"] == pytest.approx(3.0)
    assert live["by_strategy"]["alpha"]["win_rate"] == pytest.approx(1.0)

    # (d) The per-strategy merge list contains the union of strategies
    #     from both sides (alpha + beta — beta has shadow-only rows;
    #     alpha has both shadow and live rows).
    strat_names = {r["strategy"] for r in cmp["strategies"]}
    assert strat_names == {"alpha", "beta"}

    # Each merge row carries the documented keys.
    for row in cmp["strategies"]:
        assert set(row.keys()) >= {
            "strategy", "shadow_count", "live_count",
            "shadow_avg_edge", "live_avg_pnl",
            "shadow_total_size", "live_total_pnl",
        }

    # alpha merge row: shadow side has 1 trade (edge=0.05, size=10.0),
    # live side has 1 closed position (pnl=3.0).
    alpha_row = next(r for r in cmp["strategies"] if r["strategy"] == "alpha")
    assert alpha_row["shadow_count"] == 1
    assert alpha_row["live_count"] == 1
    assert alpha_row["shadow_avg_edge"] == pytest.approx(0.05)
    assert alpha_row["live_avg_pnl"] == pytest.approx(3.0)
    assert alpha_row["shadow_total_size"] == pytest.approx(10.0)
    assert alpha_row["live_total_pnl"] == pytest.approx(3.0)

    # beta merge row: shadow-only (no live closed positions for beta).
    beta_row = next(r for r in cmp["strategies"] if r["strategy"] == "beta")
    assert beta_row["shadow_count"] == 1
    assert beta_row["live_count"] == 0
    assert beta_row["shadow_avg_edge"] == pytest.approx(-0.02)
    # Live side defaults to 0.0 when the strategy has no closed positions.
    assert beta_row["live_avg_pnl"] == pytest.approx(0.0)
    assert beta_row["shadow_total_size"] == pytest.approx(5.0)
    assert beta_row["live_total_pnl"] == pytest.approx(0.0)


# ── 5. Comparison verdict correct when shadow outperforms ─────────────────
async def test_comparison_verdict_correct_when_shadow_outperforms(
    shadow_db, monkeypatch,
):
    """When the shadow side shows positive theoretical edge for a strategy
    but the live side shows non-positive realized P&L for the same
    strategy, the comparison payload must support an unambiguous
    "shadow outperforms" verdict — i.e. the strategy's theoretical edge
    did NOT survive contact with real fills.

    This is the core diagnostic the shadow-trading journal exists to
    surface (per the module docstring: "surfacing strategies whose
    theoretical edge doesn't survive contact with real fills"). The
    comparison function itself does not return a verdict string — it
    returns the raw shadow + live aggregates + the per-strategy merge
    so the caller can derive the verdict. We exercise that derivation
    here.

    Setup:
      - Shadow: 3 BUY trades for "alpha" with positive predicted_edge
        (0.05) and high confidence (0.7).
      - Live:   1 closed position for "alpha" with a realised loss
        (pnl = -2.0) → win_rate=0.0, avg_pnl=-2.0.

    Expected verdict: shadow_outperforms (shadow has positive edge;
    live has non-positive P&L).
    """
    # ── Seed 3 shadow trades for "alpha" with positive edge ───────────
    for i in range(3):
        await record_shadow_trade(
            decision_id=f"dec-sh-{i}",
            token_id="TOK_SHADOW",
            strategy="alpha",
            side="BUY",
            price=0.55,
            size=10.0,
            predicted_edge=0.05,
            confidence=0.7,
        )
        await asyncio.sleep(0.005)

    # ── Mock the live side: one LOSING closed position for "alpha" ────
    fake_live = _FakeClosedPositions(
        stats={
            "count": 1,
            "total_pnl": -2.0,
            "avg_pnl": -2.0,
            "win_rate": 0.0,
            "total_volume_shares": 10.0,
        },
        rows=[
            {"strategy": "alpha", "pnl": -2.0, "shares": 10.0},
        ],
    )
    monkeypatch.setattr("core.closed_positions.closed_positions", fake_live)

    cmp = await get_shadow_vs_live_comparison()

    # (a) Per-strategy merge for "alpha" carries the expected asymmetry.
    alpha_row = next(r for r in cmp["strategies"] if r["strategy"] == "alpha")
    assert alpha_row["shadow_count"] == 3
    assert alpha_row["live_count"] == 1
    assert alpha_row["shadow_avg_edge"] == pytest.approx(0.05)  # positive
    assert alpha_row["live_avg_pnl"] == pytest.approx(-2.0)     # negative

    # (b) Derive the verdict from the merge row — this is exactly the
    #     derivation a caller (dashboard / alerting) would perform.
    #     "shadow_outperforms" iff shadow shows positive theoretical edge
    #     AND live shows non-positive realised P&L (the strategy's edge
    #     did not survive contact with real fills).
    def _verdict(row: dict) -> str:
        if (
            row["shadow_count"] > 0
            and row["shadow_avg_edge"] > 0
            and row["live_avg_pnl"] <= 0
        ):
            return "shadow_outperforms"
        return "live_outperforms_or_tie"

    assert _verdict(alpha_row) == "shadow_outperforms"

    # (c) Top-level aggregates also reflect the outperformance:
    #     shadow count > live count, and shadow avg edge > live avg pnl.
    assert cmp["shadow"]["count"] > cmp["live"]["count"]
    assert cmp["shadow"]["avg_predicted_edge"] > cmp["live"]["avg_pnl"]
    # Shadow side confirms positive edge across all 3 trades.
    assert cmp["shadow"]["avg_predicted_edge"] == pytest.approx(0.05)
    assert cmp["shadow"]["avg_confidence"] == pytest.approx(0.7)
    # Live side confirms the loss.
    assert cmp["live"]["avg_pnl"] == pytest.approx(-2.0)
    assert cmp["live"]["win_rate"] == pytest.approx(0.0)

    # (d) by_side tally on the shadow side: 3 BUYs.
    assert cmp["shadow"]["by_side"]["BUY"] == 3
    assert cmp["shadow"]["by_side"]["SELL"] == 0


# ── 6. Side normalisation (lowercase "buy" → "BUY") ────────────────────────
async def test_side_normalisation_lowercase_buy_to_uppercase(
    shadow_db, monkeypatch,
):
    """``side`` must be normalised to upper-case on write so downstream
    filters on ``side`` are stable (the comparison's ``by_side`` tally
    relies on this). Lowercase ``"buy"`` is stored as ``"BUY"``; mixed-case
    ``"Sell"`` is stored as ``"SELL"``. The ``_normalise_side`` helper is
    also exercised directly for the ``"BUY"`` / ``"Sell"`` / ``None`` /
    empty-string / enum-with-``.value`` variants."""
    # (a) Lowercase "buy" → stored as "BUY".
    rid_lower = await record_shadow_trade(
        decision_id="dec-norm-1",
        token_id="TOK_NORM_LOWER",
        strategy="s",
        side="buy",
        price=0.5,
        size=10.0,
        predicted_edge=0.0,
        confidence=0.5,
    )
    assert rid_lower is not None

    rows_after_lower = await get_shadow_trades(limit=10)
    assert len(rows_after_lower) == 1
    assert rows_after_lower[0]["side"] == "BUY"

    # (b) Mixed-case "Sell" → stored as "SELL".
    rid_mixed = await record_shadow_trade(
        decision_id="dec-norm-2",
        token_id="TOK_NORM_MIXED",
        strategy="s",
        side="Sell",
        price=0.5,
        size=10.0,
        predicted_edge=0.0,
        confidence=0.5,
    )
    assert rid_mixed is not None

    rows_after_mixed = await get_shadow_trades(limit=10)
    assert len(rows_after_mixed) == 2
    sides = {r["side"] for r in rows_after_mixed}
    assert sides == {"BUY", "SELL"}

    # (c) The shadow-side ``by_side`` tally produced by the comparison
    #     function also reflects the normalised sides (1 BUY + 1 SELL).
    #     We mock the live side to an empty payload so the comparison
    #     completes deterministically without touching the real
    #     closed-positions singleton.
    fake_empty_live = _FakeClosedPositions(
        stats={
            "count": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "win_rate": 0.0,
            "total_volume_shares": 0.0,
        },
        rows=[],
    )
    monkeypatch.setattr(
        "core.closed_positions.closed_positions", fake_empty_live,
    )

    cmp = await get_shadow_vs_live_comparison()

    # The shadow-side by_side tally reflects normalised sides.
    assert cmp["shadow"]["by_side"]["BUY"] == 1
    assert cmp["shadow"]["by_side"]["SELL"] == 1

    # (d) ``_normalise_side`` helper exercised directly with the full
    #     matrix of inputs the docstring promises to accept.
    assert _normalise_side("buy") == "BUY"
    assert _normalise_side("BUY") == "BUY"
    assert _normalise_side("Buy") == "BUY"
    assert _normalise_side("BuY") == "BUY"
    assert _normalise_side("sell") == "SELL"
    assert _normalise_side("SELL") == "SELL"
    assert _normalise_side("Sell") == "SELL"
    assert _normalise_side(None) == ""
    assert _normalise_side("") == ""

    # (e) Side.BUY-style enum (has a ``.value`` attribute) is read via
    #     ``.value`` — per the module docstring: "Accepts Side.BUY-style
    #     enums transparently (reads .value when present)".
    class _FakeSide:
        def __init__(self, v):
            self.value = v

    assert _normalise_side(_FakeSide("buy")) == "BUY"
    assert _normalise_side(_FakeSide("SELL")) == "SELL"
    assert _normalise_side(_FakeSide(None)) == ""

    # (f) Non-string, non-enum input is stringified + upper-cased as a
    #     fallback (the implementation's broad ``except`` clause).
    assert _normalise_side(123) == "123"
