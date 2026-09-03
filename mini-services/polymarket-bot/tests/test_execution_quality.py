"""
Unit tests for ``core/execution_quality.py``.

T12 — Execution Quality Ledger unit tests.

Covers the seven behaviour contracts required by the T12 task spec:

  1. ``record_execution`` persists every column it computes (signal_price,
     decision_price, submitted_price, best_bid, best_ask, expected_fill,
     actual_fill, spread, slippage, slippage_bps, latency_ms, realized_edge,
     paper, data_json + the identity columns order_id / decision_id /
     token_id / strategy / side + the timestamp).
  2. Slippage is computed as ``actual_fill − expected_fill``. For BUY,
     ``expected_fill = best_ask`` (cost of crossing the spread to lift the
     offer); for SELL, ``expected_fill = best_bid`` (proceeds from hitting
     the bid). The T12 task spec phrased this as "BUY: actual-expected,
     SELL: expected-actual"; the implementation uses the same
     ``actual_fill − expected_fill`` expression for both sides (so a SELL
     fill *below* the bid shows up as negative slippage — see the SELL
     test docstring for the full discussion). This test module pins the
     *actual* behaviour of the module under test; the sign discrepancy
     with the spec's SELL wording is flagged in the worklog as a
     candidate follow-up — it cannot be fixed here because the task
     forbids editing ``core/execution_quality.py``.
  3. ``slippage_bps = slippage / abs(expected_fill) × 10_000`` (basis
     points relative to the expected fill magnitude; falls back to 0.0
     when ``expected_fill`` is 0 to avoid a ZeroDivisionError).
  4. ``latency_ms = (now − order.created_at) × 1_000`` (milliseconds,
     non-negative; falls back to 0.0 when ``order.created_at`` is missing
     or unparseable).
  5. ``get_execution_stats`` returns ``count`` / ``avg_slippage_bps`` (mean)
     / ``median_slippage_bps`` / ``p95_slippage_bps`` (nearest-rank
     percentile), plus the auxiliary fields ``worst_slippage_bps``,
     ``avg_latency_ms``, ``avg_realized_edge``, ``total_realized_edge``,
     ``by_side``, and the echoed filter args.
  6. Per-strategy filtering: ``get_execution_stats(strategy="X")`` only
     aggregates rows whose ``strategy`` column matches.
  7. Time-window filtering: ``get_execution_stats(time_window_seconds=N)``
     only aggregates rows whose ``timestamp`` is within the last N seconds
     (rolling window anchored at ``time.time()``).

DB isolation strategy
---------------------
The execution-quality module reads its DB path from a module-level
``DB_PATH`` constant that is resolved from the ``EXECUTION_QUALITY_DB_PATH``
env var at import time. To keep the test suite hermetic we:

  * ``setdefault`` the env var (alongside every other persisted-state env
    var used elsewhere in the polymarket-bot suite — see
    ``tests/test_risk_manager.py`` for the established pattern) BEFORE the
    first import of any project module, so the import-time ``_init_db()``
    call writes its schema under ``/tmp`` and never touches ``/app/data``.
  * Per-test, ``monkeypatch.setattr`` ``core.execution_quality.DB_PATH`` to
    a ``tmp_path``-scoped SQLite file and re-run ``_init_db()`` so every
    test starts with a clean table. ``record_execution`` and
    ``get_execution_stats`` both read ``DB_PATH`` from the module globals
    at call time, so the per-test monkeypatch is sufficient to isolate
    each test without touching the singleton state created at
    module-import time.

The repo's ``pytest.ini`` / ``pyproject.toml`` are intentionally left
untouched (T12 task constraint: "Do NOT edit existing files"). Every
test in this module is synchronous (``record_execution`` and
``get_execution_stats`` are sync), so no ``pytest.mark.asyncio`` marker
is required.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Mirrors the bootstrap pattern in tests/test_risk_manager.py and
# tests/test_failure_injection.py — every path-reading module in the bot
# resolves its on-disk path at module-import time, so the redirect must
# happen first. ``setdefault`` lets an outer runner (CI / pytest invocation
# / a sibling test file imported earlier in the session) override these if
# it needs to; otherwise the tests run fully hermetic to /tmp and cannot
# clobber any real persisted state in the repo's ``data/`` directory.
_TMP_ROOT = Path("/tmp/execution_quality_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    # Force the canonical trading mode to paper + live disabled so any
    # gate inside the import chain (safety / risk_manager) doesn't
    # short-circuit before the path under test is reached.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from. Mirrors the
# bootstrap pattern in tests/test_features.py / tests/test_paper_simulator.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

import core.execution_quality as eq  # noqa: E402
from core.data_store import Order, OrderBook, PriceLevel, Side, store  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def eq_db(monkeypatch, tmp_path):
    """
    Redirect ``core.execution_quality.DB_PATH`` to a fresh SQLite file under
    ``tmp_path`` and (re-)initialise the schema.

    ``record_execution`` and ``get_execution_stats`` both read ``DB_PATH``
    from the module's globals at call time, so a per-test monkeypatch is
    sufficient to give each test an isolated table without touching the
    singleton state created at module-import time.
    """
    db_path = tmp_path / "test_execution_quality.db"
    monkeypatch.setattr(eq, "DB_PATH", db_path)
    eq._init_db()  # creates the table + indexes at the new path
    return db_path


@pytest.fixture
def clean_store():
    """
    Reset the global ``store.order_books`` dict before/after each test so a
    book injected by one assertion cannot leak into the next.

    ``record_execution`` reads ``store.order_books.get(order.token_id)``
    synchronously (the established pattern — see the inline comment in
    ``core/execution_quality.py``), so tests that want the BUY/SELL
    expected-fill path to use a real book need to populate this dict.
    """
    saved = dict(store.order_books)
    store.order_books.clear()
    try:
        yield store
    finally:
        store.order_books.clear()
        store.order_books.update(saved)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_book(token_id: str, best_bid: float, best_ask: float) -> OrderBook:
    """Construct a minimal ``OrderBook`` with single-level bid/ask ladders."""
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=best_bid, size=100.0)],
        asks=[PriceLevel(price=best_ask, size=100.0)],
    )


def _make_order(
    *,
    token_id: str = "TOK_A",
    side: Side = Side.BUY,
    price: float = 0.55,
    size: float = 10.0,
    strategy: str = "ml_sig_v1",
    paper: bool = True,
    decision_id: str = "dec-test",
    order_id: str = "ord-test",
    created_at: float | None = None,
) -> Order:
    """Construct an ``Order`` with deterministic test defaults."""
    return Order(
        order_id=order_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        strategy=strategy,
        paper=paper,
        decision_id=decision_id,
        created_at=created_at if created_at is not None else time.time(),
    )


def _fetch_row(db_path: Path, order_id: str) -> dict:
    """Read back the single ``execution_quality`` row for ``order_id``."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_quality WHERE order_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (order_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"no row found for order_id={order_id!r}"
    return dict(row)


# ── 1. record_execution stores all metrics ─────────────────────────────────

def test_record_execution_stores_all_metrics(eq_db, clean_store):
    """``record_execution`` must persist every column the function computes.

    This is the broadest sanity check: construct an order with a populated
    book, call ``record_execution`` with a known ``fill_price`` and
    ``signal_price``, then read the row back from SQLite and assert each
    column carries the expected value (or a derived value computed from
    the inputs in the same way the function does).
    """
    token_id = "TOK_FULL"
    clean_store.order_books[token_id] = _make_book(token_id, best_bid=0.54, best_ask=0.56)

    created_at = time.time() - 0.25  # 250 ms ago
    order = _make_order(
        token_id=token_id,
        side=Side.BUY,
        price=0.55,           # decision_price / submitted_price
        size=10.0,
        strategy="ml_sig_v1",
        paper=True,
        decision_id="dec-full",
        order_id="ord-full",
        created_at=created_at,
    )

    fill_price = 0.58
    signal_price = 0.52

    t_before = time.time()
    eq.record_execution(order, fill_price, signal_price=signal_price)
    t_after = time.time()

    row = _fetch_row(eq_db, "ord-full")

    # ── Identity columns persisted verbatim ────────────────────────────
    assert row["order_id"] == "ord-full"
    assert row["decision_id"] == "dec-full"
    assert row["token_id"] == "TOK_FULL"
    assert row["strategy"] == "ml_sig_v1"
    assert row["side"] == "BUY"
    assert row["paper"] == 1

    # ── Book snapshot at fill time ─────────────────────────────────────
    assert row["best_bid"] == pytest.approx(0.54)
    assert row["best_ask"] == pytest.approx(0.56)
    assert row["spread"] == pytest.approx(0.02)

    # ── Price tiers ────────────────────────────────────────────────────
    # signal_price passed explicitly → stored verbatim.
    assert row["signal_price"] == pytest.approx(signal_price)
    # decision_price / submitted_price both come from order.price.
    assert row["decision_price"] == pytest.approx(0.55)
    assert row["submitted_price"] == pytest.approx(0.55)

    # ── Expected vs actual fill ───────────────────────────────────────
    # BUY → expected_fill = best_ask.
    expected_fill = 0.56
    assert row["expected_fill"] == pytest.approx(expected_fill)
    assert row["actual_fill"] == pytest.approx(fill_price)

    # ── Slippage math ──────────────────────────────────────────────────
    # Implementation uses (actual − expected) for both BUY and SELL.
    slippage = fill_price - expected_fill
    assert row["slippage"] == pytest.approx(slippage)
    slippage_bps = (slippage / abs(expected_fill)) * 10_000.0
    assert row["slippage_bps"] == pytest.approx(slippage_bps, rel=1e-9)

    # ── Realized edge ──────────────────────────────────────────────────
    # BUY → realized_edge = signal_price − actual_fill.
    assert row["realized_edge"] == pytest.approx(signal_price - fill_price)

    # ── Latency ────────────────────────────────────────────────────────
    # latency_ms = (now − created_at) × 1_000 — bounded by t_before/t_after.
    # SQLite stores REAL with µs precision; a 5 ms slack absorbs jitter.
    min_expected = (t_before - created_at) * 1000.0
    max_expected = (t_after - created_at) * 1000.0
    assert min_expected - 5.0 <= row["latency_ms"] <= max_expected + 5.0
    assert row["latency_ms"] > 0

    # ── data_json auxiliary payload ──────────────────────────────────
    payload = json.loads(row["data_json"])
    assert payload["fill_size"] == pytest.approx(10.0)
    assert payload["size_remaining"] == pytest.approx(10.0)  # size − size_matched(=0)

    # ── timestamp ──────────────────────────────────────────────────────
    assert t_before - 1.0 <= row["timestamp"] <= t_after + 1.0


# ── 2. slippage calculated correctly ───────────────────────────────────────

def test_slippage_buy_uses_actual_minus_expected(eq_db, clean_store):
    """BUY slippage = ``actual_fill − expected_fill`` where ``expected_fill``
    is the best ask (cost of crossing the spread to lift the offer).

    Positive slippage → the buyer paid more than the best ask → adverse.
    """
    token_id = "TOK_BUY"
    clean_store.order_books[token_id] = _make_book(token_id, best_bid=0.50, best_ask=0.55)

    order = _make_order(
        token_id=token_id,
        side=Side.BUY,
        price=0.55,
        order_id="ord-buy",
    )
    eq.record_execution(order, fill_price=0.58, signal_price=0.55)

    row = _fetch_row(eq_db, "ord-buy")
    # expected_fill is the ask; slippage is (actual − expected).
    assert row["expected_fill"] == pytest.approx(0.55)
    assert row["slippage"] == pytest.approx(0.58 - 0.55)
    # Adverse (paid more than the ask) → positive.
    assert row["slippage"] > 0


def test_slippage_sell_uses_actual_minus_expected(eq_db, clean_store):
    """SELL slippage = ``actual_fill − expected_fill`` where ``expected_fill``
    is the best bid (proceeds from hitting the bid).

    The T12 task spec phrased the SELL case as "expected − actual" (so a
    fill *below* the bid would surface as positive = adverse). The
    implementation, however, uses the same ``actual − expected``
    expression for both sides, so a SELL fill below the bid shows up as
    *negative* slippage. This test pins the implementation's actual
    behaviour — the discrepancy with the spec's SELL wording is flagged
    in the worklog and cannot be reconciled here because the task forbids
    editing ``core/execution_quality.py``.
    """
    token_id = "TOK_SELL"
    clean_store.order_books[token_id] = _make_book(token_id, best_bid=0.60, best_ask=0.65)

    order = _make_order(
        token_id=token_id,
        side=Side.SELL,
        price=0.60,
        order_id="ord-sell",
    )
    eq.record_execution(order, fill_price=0.58, signal_price=0.60)

    row = _fetch_row(eq_db, "ord-sell")
    # expected_fill is the bid; implementation uses (actual − expected).
    assert row["expected_fill"] == pytest.approx(0.60)
    assert row["slippage"] == pytest.approx(0.58 - 0.60)
    # Implementation sign: received less than the bid → negative slippage.
    assert row["slippage"] < 0


def test_slippage_buy_favorable_is_negative(eq_db, clean_store):
    """A BUY fill *below* the best ask is favourable → negative slippage."""
    token_id = "TOK_BUY_FAV"
    clean_store.order_books[token_id] = _make_book(token_id, best_bid=0.50, best_ask=0.55)
    order = _make_order(token_id=token_id, side=Side.BUY, order_id="ord-buy-fav")
    eq.record_execution(order, fill_price=0.53, signal_price=0.55)

    row = _fetch_row(eq_db, "ord-buy-fav")
    assert row["slippage"] == pytest.approx(0.53 - 0.55)
    assert row["slippage"] < 0


def test_slippage_falls_back_to_decision_price_when_book_absent(eq_db, clean_store):
    """When no order book is in ``store.order_books``, ``expected_fill``
    falls back to ``decision_price`` (``order.price``) so the slippage
    math degrades gracefully to "actual vs limit" rather than NaN-ing out."""
    # No book registered for TOK_NOBOOK — clean_store has cleared the dict.
    order = _make_order(
        token_id="TOK_NOBOOK",
        side=Side.BUY,
        price=0.50,
        order_id="ord-nobook",
    )
    eq.record_execution(order, fill_price=0.52, signal_price=0.50)

    row = _fetch_row(eq_db, "ord-nobook")
    assert row["best_bid"] is None
    assert row["best_ask"] is None
    assert row["spread"] is None
    # Fallback path: expected_fill = decision_price.
    assert row["expected_fill"] == pytest.approx(0.50)
    assert row["slippage"] == pytest.approx(0.52 - 0.50)


# ── 3. slippage_bps = slippage / abs(expected) × 10_000 ────────────────────

def test_slippage_bps_formula(eq_db, clean_store):
    """``slippage_bps = slippage / abs(expected_fill) × 10_000``."""
    token_id = "TOK_BPS"
    clean_store.order_books[token_id] = _make_book(token_id, best_bid=0.40, best_ask=0.50)
    order = _make_order(token_id=token_id, side=Side.BUY, order_id="ord-bps")
    eq.record_execution(order, fill_price=0.55, signal_price=0.50)

    row = _fetch_row(eq_db, "ord-bps")
    expected = 0.50
    slippage = 0.55 - 0.50
    expected_bps = (slippage / abs(expected)) * 10_000.0
    assert row["slippage_bps"] == pytest.approx(expected_bps, rel=1e-9)


def test_slippage_bps_zero_when_expected_zero(eq_db, clean_store):
    """If ``expected_fill`` is 0 (truthiness guard), slippage_bps falls
    back to 0.0 — protected by the ``if expected_fill`` truthiness check
    in ``record_execution`` so a ZeroDivisionError can never surface."""
    # Force expected_fill = 0 by leaving the book empty AND decision_price = 0.
    order = _make_order(
        token_id="TOK_ZERO",
        side=Side.BUY,
        price=0.0,
        order_id="ord-zero",
    )
    eq.record_execution(order, fill_price=0.0, signal_price=0.0)
    row = _fetch_row(eq_db, "ord-zero")
    assert row["expected_fill"] == pytest.approx(0.0)
    assert row["slippage_bps"] == pytest.approx(0.0)


# ── 4. latency_ms computed ────────────────────────────────────────────────

def test_latency_ms_computed_from_created_at(eq_db, clean_store):
    """``latency_ms = (now − order.created_at) × 1_000`` (milliseconds,
    non-negative)."""
    created_at = time.time() - 0.5  # 500 ms ago
    order = _make_order(
        token_id="TOK_LAT",
        side=Side.BUY,
        order_id="ord-lat",
        created_at=created_at,
    )

    t_before = time.time()
    eq.record_execution(order, fill_price=0.55, signal_price=0.55)
    t_after = time.time()

    row = _fetch_row(eq_db, "ord-lat")
    # Latency bounded by the wall-clock interval measured around the call.
    min_expected = (t_before - created_at) * 1000.0
    max_expected = (t_after - created_at) * 1000.0
    assert min_expected - 5.0 <= row["latency_ms"] <= max_expected + 5.0
    assert row["latency_ms"] > 0


def test_latency_ms_zero_when_created_at_missing(eq_db, clean_store):
    """If ``order.created_at`` is missing, ``getattr(order, "created_at", ts)``
    returns the fallback ``ts`` (the same ``time.time()`` captured inside
    ``record_execution``), so ``latency_ms = (ts − ts) × 1000 = 0``."""
    class _Bare:
        # Duck-typed order object that intentionally omits ``created_at``
        # to exercise the getattr fallback arm.
        order_id = "ord-bare"
        token_id = "TOK_BARE"
        side = Side.BUY
        price = 0.55
        size = 10.0
        strategy = "s"
        paper = True
        decision_id = "dec-bare"

    eq.record_execution(_Bare(), fill_price=0.55, signal_price=0.55)
    row = _fetch_row(eq_db, "ord-bare")
    # latency_ms = (ts − ts) × 1000 = 0.
    assert row["latency_ms"] == pytest.approx(0.0, abs=1e-3)


# ── 5. get_execution_stats returns count / mean / median / p95 ─────────────

def test_get_execution_stats_aggregates(eq_db, clean_store):
    """``get_execution_stats`` must return ``count``, ``avg_slippage_bps``
    (mean), ``median_slippage_bps``, ``p95_slippage_bps``, plus the
    documented auxiliary fields (worst, latency, edge, by_side)."""
    clean_store.order_books["TOK_STATS"] = _make_book("TOK_STATS", best_bid=0.50, best_ask=0.50)

    # Fill prices → slippages (actual − expected, expected=0.50):
    #   fill=0.50 → 0 bps    fill=0.51 → 200 bps    fill=0.52 → 400 bps
    #   fill=0.53 → 600 bps  fill=0.54 → 800 bps
    fills = [0.50, 0.51, 0.52, 0.53, 0.54]
    bps_values = [(f - 0.50) / 0.50 * 10_000 for f in fills]  # [0, 200, 400, 600, 800]
    for i, fp in enumerate(fills):
        order = _make_order(
            token_id="TOK_STATS",
            side=Side.BUY,
            order_id=f"ord-stats-{i}",
        )
        eq.record_execution(order, fill_price=fp, signal_price=0.50)

    stats = eq.get_execution_stats()

    # ── count ────────────────────────────────────────────────────────
    assert stats["count"] == 5

    # ── mean (avg_slippage_bps) ─────────────────────────────────────
    expected_mean = sum(bps_values) / len(bps_values)  # 400.0
    assert stats["avg_slippage_bps"] == pytest.approx(expected_mean, rel=1e-9)

    # ── median ────────────────────────────────────────────────────────
    # statistics.median of [0, 200, 400, 600, 800] = 400.0
    expected_median = sorted(bps_values)[len(bps_values) // 2]  # 400.0
    assert stats["median_slippage_bps"] == pytest.approx(expected_median, rel=1e-9)

    # ── p95 (nearest-rank percentile) ────────────────────────────────
    # _percentile([0, 200, 400, 600, 800], 95):
    #   k = round(0.95 * 4) = round(3.8) = 4 → sorted[4] = 800
    assert stats["p95_slippage_bps"] == pytest.approx(800.0, rel=1e-9)

    # ── worst (max) ───────────────────────────────────────────────────
    assert stats["worst_slippage_bps"] == pytest.approx(800.0, rel=1e-9)

    # ── avg_latency_ms ────────────────────────────────────────────────
    assert stats["avg_latency_ms"] >= 0.0  # all created_at = time.time()
    assert isinstance(stats["avg_latency_ms"], float)

    # ── realized_edge aggregates ──────────────────────────────────────
    # BUY: realized_edge = signal_price − actual_fill = 0.50 − fill
    expected_edges = [0.50 - f for f in fills]  # [0, -0.01, -0.02, -0.03, -0.04]
    assert stats["avg_realized_edge"] == pytest.approx(
        sum(expected_edges) / len(expected_edges), rel=1e-9
    )
    assert stats["total_realized_edge"] == pytest.approx(sum(expected_edges), rel=1e-9)

    # ── by_side ──────────────────────────────────────────────────────
    assert stats["by_side"] == {"BUY": 5, "SELL": 0}

    # ── echo of the filter args ──────────────────────────────────────
    assert stats["strategy"] is None
    assert stats["time_window_seconds"] is None


def test_get_execution_stats_empty_db_returns_zeroed_dict(eq_db, clean_store):
    """When no rows match the filter, ``get_execution_stats`` returns a
    zeroed-out stats dict (NOT a 500 / KeyError) so the HTTP endpoint
    always succeeds."""
    stats = eq.get_execution_stats()
    assert stats["count"] == 0
    assert stats["avg_slippage_bps"] == 0.0
    assert stats["median_slippage_bps"] == 0.0
    assert stats["p95_slippage_bps"] == 0.0
    assert stats["worst_slippage_bps"] == 0.0
    assert stats["avg_latency_ms"] == 0.0
    assert stats["avg_realized_edge"] == 0.0
    assert stats["total_realized_edge"] == 0.0
    assert stats["by_side"] == {"BUY": 0, "SELL": 0}


# ── 6. per-strategy breakdown ─────────────────────────────────────────────

def test_get_execution_stats_filters_by_strategy(eq_db, clean_store):
    """The ``strategy=`` kwarg restricts the aggregate to a single
    strategy name; rows for other strategies are excluded from count /
    mean / median / p95 / by_side."""
    clean_store.order_books["TOK_STRAT"] = _make_book("TOK_STRAT", best_bid=0.50, best_ask=0.50)

    # 3 fills for strategy "alpha" + 2 fills for strategy "beta".
    for i in range(3):
        order = _make_order(
            token_id="TOK_STRAT",
            side=Side.BUY,
            strategy="alpha",
            order_id=f"ord-alpha-{i}",
        )
        eq.record_execution(order, fill_price=0.50 + i * 0.01, signal_price=0.50)
    for i in range(2):
        order = _make_order(
            token_id="TOK_STRAT",
            side=Side.BUY,
            strategy="beta",
            order_id=f"ord-beta-{i}",
        )
        eq.record_execution(order, fill_price=0.60 + i * 0.01, signal_price=0.50)

    # Aggregate over all strategies — count = 5.
    all_stats = eq.get_execution_stats()
    assert all_stats["count"] == 5
    assert all_stats["by_side"] == {"BUY": 5, "SELL": 0}

    # Filter to "alpha" only — count = 3, slippages from fills 0.50/0.51/0.52.
    alpha = eq.get_execution_stats(strategy="alpha")
    assert alpha["count"] == 3
    assert alpha["strategy"] == "alpha"
    # alpha fills: 0.50, 0.51, 0.52 → bps 0, 200, 400 → mean = 200, max = 400.
    assert alpha["avg_slippage_bps"] == pytest.approx(200.0, rel=1e-9)
    assert alpha["worst_slippage_bps"] == pytest.approx(400.0, rel=1e-9)

    # Filter to "beta" only — count = 2.
    beta = eq.get_execution_stats(strategy="beta")
    assert beta["count"] == 2
    assert beta["strategy"] == "beta"
    # beta fills: 0.60, 0.61 → bps 2000, 2200 → mean = 2100.
    assert beta["avg_slippage_bps"] == pytest.approx(2100.0, rel=1e-9)

    # Filter to unknown strategy — empty / zeroed.
    ghost = eq.get_execution_stats(strategy="ghost")
    assert ghost["count"] == 0
    assert ghost["avg_slippage_bps"] == 0.0


# ── 7. time window filtering ─────────────────────────────────────────────

def test_get_execution_stats_filters_by_time_window(eq_db, clean_store):
    """The ``time_window_seconds=`` kwarg restricts the aggregate to rows
    whose ``timestamp`` is within the last N seconds (rolling window
    anchored at ``time.time()``)."""
    clean_store.order_books["TOK_WIN"] = _make_book("TOK_WIN", best_bid=0.50, best_ask=0.50)

    # Write a row, then backdate its timestamp by 100 s to simulate an
    # "old" fill (whose slippage is 0.55 − 0.50 = 0.05 → bps = 1000).
    order_old = _make_order(
        token_id="TOK_WIN",
        side=Side.BUY,
        order_id="ord-old",
    )
    eq.record_execution(order_old, fill_price=0.55, signal_price=0.50)
    with sqlite3.connect(eq_db) as conn:
        conn.execute(
            "UPDATE execution_quality SET timestamp = ? WHERE order_id = ?",
            (time.time() - 100.0, "ord-old"),
        )
        conn.commit()

    # Write a "fresh" row (slippage = 0.51 − 0.50 = 0.01 → bps = 200).
    order_new = _make_order(
        token_id="TOK_WIN",
        side=Side.BUY,
        order_id="ord-new",
    )
    eq.record_execution(order_new, fill_price=0.51, signal_price=0.50)

    # ── No window: both rows counted ─────────────────────────────────
    all_stats = eq.get_execution_stats()
    assert all_stats["count"] == 2

    # ── 30 s window: only the fresh row counted ──────────────────────
    fresh = eq.get_execution_stats(time_window_seconds=30.0)
    assert fresh["count"] == 1
    assert fresh["time_window_seconds"] == 30.0
    # Fresh fill slippage: 0.01 / 0.50 × 10000 = 200 bps.
    assert fresh["avg_slippage_bps"] == pytest.approx(200.0, rel=1e-9)
    assert fresh["worst_slippage_bps"] == pytest.approx(200.0, rel=1e-9)

    # ── 200 s window: both rows counted (old is 100 s back) ─────────
    wide = eq.get_execution_stats(time_window_seconds=200.0)
    assert wide["count"] == 2

    # ── Combined filter: time window AND strategy ────────────────────
    # The fresh row is the only one inside the 30 s window; both rows
    # share strategy="ml_sig_v1" by default.
    combined = eq.get_execution_stats(time_window_seconds=30.0, strategy="ml_sig_v1")
    assert combined["count"] == 1
    assert combined["strategy"] == "ml_sig_v1"
    assert combined["time_window_seconds"] == 30.0
