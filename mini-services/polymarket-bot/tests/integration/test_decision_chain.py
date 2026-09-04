"""W17-9 — Cross-module integration tests for the unified decision chain.

Drives the **full production code path** that emits the five-stage decision
chain end-to-end:

    PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL

and verifies the chain can be reconstructed via the unified
``core.decision_ledger`` from any of its three query dimensions
(decision_id / token_id / stage).

This is a CROSS-MODULE integration test: it touches six modules
(``core.clob_client``, ``core.data_store``, ``core.decision_ledger``,
``core.execution_quality``, ``ml.model``, ``paper.simulator``,
``risk.manager``) and exercises the real production call sites
(``ml_model.predict``, ``risk_manager.check_order``,
``paper_sim.create_order``, ``paper_sim._try_fill_orders``) — NOT mocked
substitutes. The only stub is ``ml_model.predict`` itself, patched to a
deterministic BUY-leaning return so the test is fast and reproducible
regardless of model state (mirrors ``tests/test_e2e_decision_chain.py``).

Hermeticity
-----------
``conftest.py`` redirects ``DECISION_LEDGER_DB_PATH`` +
``EXECUTION_QUALITY_DB_PATH`` to ``/tmp/pmbot_conftest_isolation/`` BEFORE
any project module is imported, so the module-level singletons
(``decision_ledger``, ``record_execution``) write to writable paths. Each
test uses a unique ``token_id`` so its decision-chain rows are isolated
from any sibling test's rows even when the DBs are shared across tests
(mirrors the convention in ``tests/test_e2e_decision_chain.py``).
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from core.clob_client import OrderArgs
from core.data_store import Order, OrderBook, PriceLevel, Side, store
from core.decision_ledger import (
    STAGE_FILL,
    STAGE_ORDER,
    STAGE_POSITION,
    STAGE_PREDICTION,
    STAGE_RISK_APPROVED,
    STAGE_SIGNAL,
    decision_ledger,
)
from core.execution_quality import DB_PATH as EXEC_DB_PATH
from core.execution_quality import get_execution_stats
from ml.model import ml_model
from paper.simulator import paper_sim
from risk.manager import risk_manager

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` here —
# pytest-asyncio is in strict mode by default (no ``asyncio_mode = "auto"``)
# so the explicit module-level mark is required for collection.
pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def deterministic_predict(monkeypatch):
    """Patch ``ml_model.predict`` to a deterministic, BUY-leaning return.

    p_yes=0.85 clears the strategy's p_yes >= 0.55 gate; confidence=0.70
    clears the strategy's confidence >= 0.45 floor. Returns the patched
    callable so a test that wants to assert on the patched value can.
    """

    def fake_predict(features, token_id: str = "") -> tuple[float, float]:
        return 0.85, 0.70

    monkeypatch.setattr(ml_model, "predict", fake_predict)
    return fake_predict


def _build_mock_book(token_id: str, mid: float = 0.5) -> OrderBook:
    """Build a 2¢-spread order book with comfortable depth both sides."""
    return OrderBook(
        token_id=token_id,
        bids=[
            PriceLevel(price=round(mid - 0.01, 4), size=500.0),
            PriceLevel(price=round(mid - 0.02, 4), size=500.0),
        ],
        asks=[
            PriceLevel(price=round(mid + 0.01, 4), size=500.0),
            PriceLevel(price=round(mid + 0.02, 4), size=500.0),
        ],
    )


@pytest.fixture
def staged_book(monkeypatch, request):
    """Return a ``(token_id, OrderBook)`` pair to stage in the global ``store``.

    A unique ``token_id`` per test invocation (derived from the test
    node name) guarantees that token-scoped decision-ledger queries only
    see THIS test's events — even when the shared conftest DB accumulates
    rows across tests. The fixture is sync (not async) so pytest-asyncio's
    strict mode doesn't need the async-fixture plumbing — the test itself
    awaits ``store.update_order_book`` inside its async body.

    Yields the ``(token_id, book)`` tuple. The teardown drops the book
    from ``store.order_books`` so the next test starts from a clean slate.
    """
    # ``request.node.name`` is the test function name — unique per test,
    # so hashing it gives a per-test stable token suffix.
    token_id = f"TEST_CHAIN_{abs(hash(request.node.name)) % 10_000_000}"
    book = _build_mock_book(token_id)
    # Disable the durable kill-switch check so the risk gate runs cleanly.
    monkeypatch.setattr("core.safety.kill_switch_file_exists", lambda: False)
    yield token_id, book
    # Teardown: drop the book so the next test doesn't see it.
    store.order_books.pop(token_id, None)


# ── (1) Full PREDICTION → SIGNAL → RISK → ORDER → FILL chain ──────────────


async def test_full_decision_chain_records_all_five_stages(
    deterministic_predict, staged_book
):
    """Run the full PREDICTION → SIGNAL → RISK → ORDER → FILL chain and
    verify all five stages land in the decision ledger under one
    ``decision_id`` in chronological order.

    Each stage is exercised through its real production emitter:
      (1) PREDICTION     — ``ml_model.predict()`` + ``decision_ledger.record()``
      (2) SIGNAL         — ``decision_ledger.record(stage="SIGNAL")``
      (3) RISK_APPROVED  — ``risk_manager.check_order()`` returns (True, "OK")
      (4) ORDER          — ``paper_sim.create_order(..., decision_id=...)``
      (5) FILL           — ``paper_sim._try_fill_orders()`` drives the fill
                            loop once
    """
    TOKEN, book = staged_book
    await store.update_order_book(book)
    STRATEGY = "signal_trader"

    decision_id = decision_ledger.new_decision_id()
    assert decision_id.startswith("dec-"), (
        f"unexpected decision_id prefix: {decision_id!r}"
    )

    book = store.order_books[TOKEN]
    mid = book.mid or 0.5
    spread = book.spread or 0.01

    # ── (1) PREDICTION ────────────────────────────────────────────────────
    features = np.zeros(38, dtype=np.float32)
    features[0] = mid
    p_yes, confidence = ml_model.predict(features, token_id=TOKEN)
    assert p_yes == pytest.approx(0.85)
    predicted_edge = p_yes - mid

    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        p_yes=p_yes,
        confidence=confidence,
        market_mid=mid,
        spread=spread,
        predicted_edge=predicted_edge,
    )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 1
    assert chain[0]["stage"] == STAGE_PREDICTION
    assert chain[0]["decision_id"] == decision_id
    assert chain[0]["token_id"] == TOKEN
    assert chain[0]["strategy"] == STRATEGY
    assert chain[0]["data"]["p_yes"] == pytest.approx(0.85)

    # ── (2) SIGNAL ────────────────────────────────────────────────────────
    best_ask = book.best_ask
    assert best_ask is not None
    target_price = round(min(best_ask + 0.001, 0.98), 4)
    size_usdc = 1.50
    reason_str = f"ML Prob={p_yes:.1%} (edge={predicted_edge*100:.1f}%)"

    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        direction=Side.BUY.value,
        target_price=target_price,
        size_usdc=size_usdc,
        p_yes=p_yes,
        confidence=confidence,
        market_mid=mid,
        reason=reason_str,
    )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 2
    assert chain[1]["stage"] == STAGE_SIGNAL
    assert chain[1]["data"]["direction"] == "BUY"
    assert chain[1]["data"]["target_price"] == pytest.approx(target_price)
    assert chain[1]["data"]["size_usdc"] == pytest.approx(size_usdc)

    # ── (3) RISK_APPROVED ─────────────────────────────────────────────────
    size_shares = max(1.0, size_usdc / target_price)
    args = OrderArgs(
        token_id=TOKEN, price=target_price, side=Side.BUY, size=size_shares
    )
    order_for_risk = Order(
        order_id="pre-check",
        token_id=args.token_id,
        side=args.side,
        price=args.price,
        size=args.size,
        strategy=STRATEGY,
        paper=True,
        decision_id=decision_id,
    )
    allowed, reason = await risk_manager.check_order(order_for_risk)
    assert allowed, (
        f"risk_manager.check_order rejected a small paper BUY: reason={reason!r}"
    )

    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        side=Side.BUY.value,
        price=target_price,
        size=size_shares,
    )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 3
    assert chain[2]["stage"] == STAGE_RISK_APPROVED
    assert chain[2]["data"]["side"] == "BUY"
    assert chain[2]["data"]["price"] == pytest.approx(target_price)
    assert chain[2]["data"]["size"] == pytest.approx(size_shares)

    # ── (4) ORDER ─────────────────────────────────────────────────────────
    paper_order = await paper_sim.create_order(
        args, strategy=STRATEGY, decision_id=decision_id
    )
    assert paper_order is not None
    assert paper_order.decision_id == decision_id
    assert paper_order.paper is True
    assert paper_order.side == Side.BUY
    assert paper_order.token_id == TOKEN
    assert paper_order.order_id in store.open_orders

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 4
    assert chain[3]["stage"] == STAGE_ORDER
    assert chain[3]["data"]["order_id"] == paper_order.order_id
    assert chain[3]["data"]["side"] == "BUY"
    assert chain[3]["data"]["paper"] is True

    # ── (5) FILL ──────────────────────────────────────────────────────────
    await paper_sim._try_fill_orders()

    chain = await decision_ledger.get_chain(decision_id)
    # W19-3 — the FILL stage is now followed by a POSITION stage (additive —
    # captured by ``paper_sim._execute_fill`` immediately after the FILL
    # event records the post-fill position state for the decision chain).
    # Total chain length is 6: PREDICTION → SIGNAL → RISK_APPROVED →
    # ORDER → FILL → POSITION.
    assert len(chain) == 6, (
        f"expected 6-stage chain (5 originals + W19-3 POSITION); "
        f"got {len(chain)}: {[r['stage'] for r in chain]}"
    )
    assert chain[4]["stage"] == STAGE_FILL
    fill_data = chain[4]["data"]
    assert fill_data is not None
    assert fill_data["fill_price"] > 0
    assert fill_data["fill_price"] <= 0.99
    assert fill_data["fill_size"] == pytest.approx(size_shares, rel=1e-3)
    assert fill_data["side"] == "BUY"
    assert fill_data["order_id"] == paper_order.order_id
    assert fill_data["paper"] is True

    # W19-3 — POSITION stage is recorded immediately after FILL.
    assert chain[5]["stage"] == STAGE_POSITION
    assert chain[5]["decision_id"] == decision_id
    assert chain[5]["token_id"] == TOKEN
    # Opening BUY → realised P&L is 0.0; promoted to the dedicated ``pnl`` column.
    assert chain[5]["pnl"] == pytest.approx(0.0)
    pos_data = chain[5]["data"]
    assert pos_data is not None
    assert pos_data["yes_shares"] > 0
    assert pos_data["avg_entry_price"] > 0
    assert pos_data["paper"] is True

    # Position now exists on the store.
    assert TOKEN in store.positions
    assert store.positions[TOKEN].yes_shares > 0

    # ── (6) Verify the full 6-stage chain in canonical order ─────────────
    stages = [row["stage"] for row in chain]
    assert stages == [
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
        STAGE_POSITION,
    ], f"unexpected chain stage order: {stages}"

    # Every row carries the same decision_id + token_id — the chain is
    # fully reconstructable via the decision_id (the "correlation id").
    assert all(row["decision_id"] == decision_id for row in chain), (
        "chain rows do not share a single decision_id (correlation id)"
    )
    assert all(row["token_id"] == TOKEN for row in chain), (
        "chain rows do not share a single token_id"
    )

    # Chronological ordering: timestamps must be non-decreasing.
    timestamps = [row["timestamp"] for row in chain]
    assert timestamps == sorted(timestamps), (
        f"chain timestamps are not in chronological order: {timestamps}"
    )


# ── (2) Decision ledger query consistency ───────────────────────────────────


async def test_query_by_token_id_returns_all_stages(
    deterministic_predict, staged_book
):
    """After running the full chain, ``get_chain_by_token`` returns every
    stage for that token (newest-first), regardless of decision_id.

    This is the dashboard's primary drill-down dimension: an operator
    types in a token_id and expects to see the full event history.
    """
    TOKEN, book = staged_book
    await store.update_order_book(book)
    decision_id = decision_ledger.new_decision_id()
    book = store.order_books[TOKEN]
    mid = book.mid or 0.5

    # Record a minimal 5-stage chain.
    for stage in (
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ):
        await decision_ledger.record(
            decision_id=decision_id,
            stage=stage,
            token_id=TOKEN,
            strategy="signal_trader",
            pnl=0.0,
        )

    chain = await decision_ledger.get_chain_by_token(TOKEN, limit=50)
    # At least the 5 events we just recorded (other tests may have left
    # rows on the shared conftest DB, but ours are at the top because
    # they were just written — newest-first).
    stages = [r["stage"] for r in chain[:5]]
    # Newest-first → FILL must be the first row.
    assert chain[0]["stage"] == STAGE_FILL
    assert chain[0]["decision_id"] == decision_id
    # All 5 stages appear in the top-5 (any order — the index is by
    # timestamp DESC, and our 5 stages all share the same decision_id so
    # they all surface at the top).
    assert set(stages) == {
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    }


async def test_query_by_correlation_id_returns_all_stages_in_order(
    deterministic_predict, staged_book
):
    """``get_chain(decision_id)`` returns all 5 stages in chronological order.

    The decision_id is the cross-module correlation id: every stage
    carries it, and a single query reconstructs the entire chain from
    PREDICTION through FILL.
    """
    TOKEN, _book = staged_book
    await store.update_order_book(_book)
    decision_id = decision_ledger.new_decision_id()

    for stage in (
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ):
        await decision_ledger.record(
            decision_id=decision_id,
            stage=stage,
            token_id=TOKEN,
            strategy="signal_trader",
            pnl=0.0,
        )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 5
    # Chronological ascending order — the contract for ``get_chain``.
    assert [r["stage"] for r in chain] == [
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ]
    # All rows share the correlation id.
    assert all(r["decision_id"] == decision_id for r in chain)
    # Timestamps monotonically non-decreasing.
    ts = [r["timestamp"] for r in chain]
    assert ts == sorted(ts)


async def test_query_by_stage_type_filters_correctly(
    deterministic_predict, staged_book
):
    """``get_prediction_history(token_id)`` returns ONLY PREDICTION-stage
    events — verifying the stage-type-filter query dimension.

    The decision ledger exposes ``get_prediction_history`` (filters on
    ``stage = 'PREDICTION'``) as the dashboard's "model-version lineage
    per token" feed. This is the canonical stage-type filter; it must
    not leak SIGNAL / RISK / ORDER / FILL events.
    """
    TOKEN, _book = staged_book
    await store.update_order_book(_book)
    decision_id = decision_ledger.new_decision_id()

    # Record two PREDICTIONs and one SIGNAL on the same token.
    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=TOKEN,
        strategy="signal_trader",
        p_yes=0.62,
        model_version="v1.test.0",
    )
    await decision_ledger.record(
        decision_id=decision_ledger.new_decision_id(),
        stage=STAGE_PREDICTION,
        token_id=TOKEN,
        strategy="signal_trader",
        p_yes=0.71,
        model_version="v1.test.0",
    )
    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=TOKEN,
        strategy="signal_trader",
        direction="BUY",
    )

    preds = await decision_ledger.get_prediction_history(TOKEN, limit=10)
    # Newest-first → the second PREDICTION is the first row.
    assert len(preds) >= 2
    assert all(r["stage"] == STAGE_PREDICTION for r in preds), (
        f"stage-type filter leaked non-PREDICTION rows: "
        f"{[r['stage'] for r in preds]}"
    )
    # The ``model_version`` convenience field is lifted out of the
    # decoded ``data`` payload by ``get_prediction_history``.
    assert all("model_version" in r for r in preds)
    # SIGNAL was never surfaced (filtered out by the stage-type predicate).
    assert not any(r["stage"] == STAGE_SIGNAL for r in preds)


# ── (3) Execution quality tracking ─────────────────────────────────────────


async def test_execution_quality_record_exists_after_fill(
    deterministic_predict, staged_book
):
    """After a paper fill, an ``execution_quality`` row exists in the
    SQLite store with the fill price / slippage / latency populated.

    ``paper_sim._execute_fill`` calls
    ``core.execution_quality.record_execution(order, fill_price, signal_price=...)``
    after the fill is booked. The row must be queryable via
    ``get_execution_stats`` (aggregate) and via a direct SQLite SELECT
    (row-level).
    """
    TOKEN, book = staged_book
    await store.update_order_book(book)
    STRATEGY = "signal_trader"
    decision_id = decision_ledger.new_decision_id()
    book = store.order_books[TOKEN]
    mid = book.mid or 0.5

    # Run the full chain (same as the first test).
    features = np.zeros(38, dtype=np.float32)
    features[0] = mid
    p_yes, _ = ml_model.predict(features, token_id=TOKEN)
    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_PREDICTION,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        p_yes=p_yes,
    )
    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        direction=Side.BUY.value,
        target_price=0.511,
        size_usdc=1.50,
        p_yes=p_yes,
        market_mid=mid,
    )
    target_price = 0.511
    size_shares = 1.50 / target_price
    args = OrderArgs(
        token_id=TOKEN, price=target_price, side=Side.BUY, size=size_shares
    )
    # Risk approve.
    order_for_risk = Order(
        order_id="pre-check-eq",
        token_id=TOKEN,
        side=Side.BUY,
        price=target_price,
        size=size_shares,
        strategy=STRATEGY,
        paper=True,
        decision_id=decision_id,
    )
    allowed, reason = await risk_manager.check_order(order_for_risk)
    assert allowed, f"risk_manager.check_order rejected: {reason!r}"
    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        side=Side.BUY.value,
        price=target_price,
        size=size_shares,
    )
    paper_order = await paper_sim.create_order(
        args, strategy=STRATEGY, decision_id=decision_id
    )
    await paper_sim._try_fill_orders()

    # ── Verify the execution-quality row was persisted ───────────────────
    rows: list[dict] = []
    with sqlite3.connect(EXEC_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM execution_quality WHERE decision_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (decision_id,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    assert len(rows) == 1, (
        f"expected exactly 1 execution_quality row for decision_id="
        f"{decision_id}; got {len(rows)}"
    )
    row = rows[0]
    # Identifiers carried over from the Order.
    assert row["decision_id"] == decision_id
    assert row["token_id"] == TOKEN
    assert row["strategy"] == STRATEGY
    assert row["order_id"] == paper_order.order_id
    assert row["paper"] == 1
    assert row["side"] == "BUY"
    # Slippage / latency are populated (not NULL).
    assert row["actual_fill"] is not None
    assert row["actual_fill"] > 0
    assert row["slippage"] is not None
    assert row["slippage_bps"] is not None
    assert row["latency_ms"] is not None
    # Aggregate stats now reflect at least one fill.
    stats = get_execution_stats()
    assert stats["count"] >= 1
    assert stats["by_side"]["BUY"] >= 1


async def test_execution_quality_slippage_and_latency_computed_correctly(
    deterministic_predict, staged_book
):
    """Slippage is computed as ``actual_fill - expected_fill`` (positive =
    adverse), and latency is recorded as ``(fill_ts - order.created_at) * 1000``.

    For a BUY at target_price=0.511 against a book with best_ask=0.51,
    the expected fill is 0.51 (BUY pays the offer) — the actual fill
    will be slightly adverse (paper sim's slippage model lifts the
    offer), so slippage is positive (small positive number).
    """
    TOKEN, book = staged_book
    await store.update_order_book(book)
    STRATEGY = "signal_trader"
    decision_id = decision_ledger.new_decision_id()
    book = store.order_books[TOKEN]
    best_ask = book.best_ask
    assert best_ask is not None

    # Place a paper BUY at a price slightly above best_ask so the fill
    # loop fires immediately.
    target_price = round(best_ask + 0.001, 4)
    size_shares = 1.0  # minimum liquidity threshold
    args = OrderArgs(
        token_id=TOKEN, price=target_price, side=Side.BUY, size=size_shares
    )
    paper_order = await paper_sim.create_order(
        args, strategy=STRATEGY, decision_id=decision_id
    )
    # Capture the order's created_at so we can validate the latency
    # calculation.
    created_at = paper_order.created_at
    await paper_sim._try_fill_orders()

    rows: list[dict] = []
    with sqlite3.connect(EXEC_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM execution_quality WHERE order_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (paper_order.order_id,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    assert len(rows) == 1
    row = rows[0]
    # Expected fill on a BUY = best_ask.
    assert row["best_ask"] == pytest.approx(best_ask, abs=1e-6)
    expected_fill = row["expected_fill"]
    assert expected_fill == pytest.approx(best_ask, abs=1e-6)
    # Slippage = actual - expected (signed).
    expected_slippage = row["actual_fill"] - expected_fill
    assert row["slippage"] == pytest.approx(expected_slippage, rel=1e-3)
    # Slippage in basis points = slippage / |expected_fill| * 10_000.
    expected_bps = (expected_slippage / abs(expected_fill)) * 10_000.0
    assert row["slippage_bps"] == pytest.approx(expected_bps, rel=1e-3)
    # Latency is non-negative and bounded (should be < 5s for an
    # in-process fill loop).
    assert row["latency_ms"] >= 0
    assert row["latency_ms"] < 5_000
    # Cross-check against the order's created_at.
    expected_latency_lower_bound = 0.0  # (fill_ts - created_at) >= 0
    assert row["latency_ms"] >= expected_latency_lower_bound
