"""
tests/test_e2e_decision_chain.py — End-to-end integration test for the
unified decision ledger chain.

Drives the full trading-pipeline decision chain end-to-end and asserts that
every stage lands in ``core.decision_ledger`` under a single ``decision_id``
in the canonical order:

    PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL

Each stage is exercised through the real production code path that emits it:

    (1) PREDICTION     — ``ml_model.predict()`` + ``decision_ledger.record()``
                         (mirrors ``signal_trader._ml_signal``)
    (2) SIGNAL         — ``decision_ledger.record(stage="SIGNAL", ...)``
                         (mirrors ``signal_trader._ml_signal`` after all gates)
    (3) RISK_APPROVED  — ``risk_manager.check_order()`` returns (True, "OK")
                         then ``decision_ledger.record(stage="RISK_APPROVED")``
                         (mirrors ``strategies/base.submit_order``)
    (4) ORDER          — ``paper_sim.create_order(args, decision_id=...)``
                         (mirrors ``strategies/base.submit_order`` paper path)
    (5) FILL           — ``paper_sim._try_fill_orders()`` drives the fill loop
                         once; the resulting ``_execute_fill`` records the FILL
                         stage with realised P&L.

The ml model's ``predict`` is patched to a deterministic (BUY-leaning)
return so the test is fast and reproducible regardless of model state —
the test scope is the decision-ledger plumbing, not the ML inference
correctness (mirrors the verification approach in worklog entry R11+R12).

Note: There is no project-wide ``conftest.py`` yet, so the path/env setup
and the DataStore-reset fixture are inlined here. They are written so they
can be lifted into a future ``tests/conftest.py`` verbatim if shared
fixtures are wanted by later tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

# ── Sandbox path setup — MUST run BEFORE importing any project module that
# reads ``os.environ`` at module load time (config, core.data_store,
# core.decision_ledger, ml.model, ml.model_registry, core.safety,
# core.audit_logger, ...). The project defaults to ``/app/data/*`` paths
# which are not writable in the test sandbox, so we redirect every data
# path into a per-test-run directory under the project's own ``data/`` tree.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEST_DATA_DIR = Path(
    os.environ.get("PMBOT_TEST_DATA_DIR", str(_ROOT / "data" / "test_run"))
)
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Use the project's cached model.pkl when available so we don't pay the
# ~7s retrain cost on every test run. Falls back to a fresh path if absent.
_CACHED_MODEL = _ROOT / "data" / "model.pkl"

os.environ.setdefault("DECISION_LEDGER_DB_PATH", str(_TEST_DATA_DIR / "decision_ledger.db"))
os.environ.setdefault("STORE_STATE_PATH", str(_TEST_DATA_DIR / "store_state.json"))
os.environ.setdefault("MODEL_PATH", str(_CACHED_MODEL))
os.environ.setdefault("MODEL_REGISTRY_PATH", str(_TEST_DATA_DIR / "model_registry.json"))
os.environ.setdefault("KILL_SWITCH_PATH", str(_TEST_DATA_DIR / "kill_switch"))
os.environ.setdefault("KILL_SWITCH_REASON_PATH", str(_TEST_DATA_DIR / "kill_switch.reason"))
os.environ.setdefault("AUDIT_DB_PATH", str(_TEST_DATA_DIR / "audit_trail.db"))
os.environ.setdefault("MARKET_DB_PATH", str(_TEST_DATA_DIR / "market_intelligence.db"))
os.environ.setdefault("VECTOR_STORE_PATH", str(_TEST_DATA_DIR / "vector_index.json"))

# Project imports — safe now that env paths are redirected into the sandbox.
from core.clob_client import OrderArgs
from core.data_store import Order, OrderBook, PriceLevel, Side, store
from core.decision_ledger import (
    STAGE_FILL,
    STAGE_ORDER,
    STAGE_PREDICTION,
    STAGE_RISK_APPROVED,
    STAGE_SIGNAL,
    decision_ledger,
)
from ml.model import ml_model
from paper.simulator import paper_sim
from risk.manager import risk_manager


# ── Fixtures ─────────────────────────────────────────────────────────────────
# (Inlined because no project-wide conftest.py exists yet — see module docstring.)

@pytest.fixture
def fresh_store():
    """Reset the in-memory DataStore singleton between tests so positions,
    orders, P&L, and books from prior tests do not bleed into this one.

    The DataStore is a module-level singleton (``core.data_store.store``);
    we mutate its public containers in place rather than swapping the
    singleton, so every consumer (risk_manager, paper_sim, strategies)
    keeps seeing the same instance.
    """
    store.open_orders.clear()
    store.order_history.clear()
    store.positions.clear()
    store.trades.clear()
    store.event_log.clear()
    store.order_books.clear()
    store.market_slugs.clear()
    store.daily_pnl = 0.0
    store.weekly_pnl = 0.0
    store.paper_balance = 100.0
    store.peak_equity = 100.0
    store.equity_history.clear()
    store.kill_switch_active = False
    # Also clear any per-strategy cooldowns the risk engine may have accumulated.
    risk_manager._strategy_cooldowns.clear()
    risk_manager.observation_only = False
    risk_manager.observation_reason = ""
    yield store
    # teardown — leave the store clean for the next test
    store.open_orders.clear()
    store.positions.clear()
    store.order_books.clear()
    risk_manager._strategy_cooldowns.clear()


@pytest.fixture
def mock_book():
    """Mock order book: mid=0.5, 2¢ spread, comfortable 500-share depth both sides.

    mid=0.5 is the requested initial condition. With a deterministic
    p_yes=0.85 from the patched ``ml_model.predict``, the predicted edge
    is +0.35 (a strong BUY), well past the 0.55 p_yes threshold used in
    ``signal_trader._ml_signal``.
    """
    return OrderBook(
        token_id="TEST_TOKEN_E2E",
        bids=[
            PriceLevel(price=0.49, size=500.0),
            PriceLevel(price=0.48, size=500.0),
        ],
        asks=[
            PriceLevel(price=0.51, size=500.0),
            PriceLevel(price=0.52, size=500.0),
        ],
    )


@pytest.fixture
def deterministic_predict(monkeypatch):
    """Patch ``ml_model.predict`` to a deterministic, BUY-leaning return so the
    test is fast and reproducible regardless of model state.

    The test still calls ``ml_model.predict(features, token_id=...)``
    syntactically (matching the production call site in
    ``signal_trader._ml_signal``); only the inner inference is stubbed.
    """
    def fake_predict(features, token_id: str = "") -> tuple[float, float]:
        # p_yes=0.85, confidence=|0.85-0.5|*2=0.70 — a strong BUY signal that
        # clears the strategy's p_yes >= 0.55 gate and confidence >= 0.45 floor.
        return 0.85, 0.70

    monkeypatch.setattr(ml_model, "predict", fake_predict)
    return fake_predict


# ── Test ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_decision_chain(fresh_store, mock_book, deterministic_predict):
    """Drive the full PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL chain
    and verify every stage lands in the decision ledger under one
    ``decision_id`` in chronological order.
    """
    TOKEN = "TEST_TOKEN_E2E"
    STRATEGY = "signal_trader"

    # ─── (0) Mint a fresh decision_id and stage the mock book ──────────────
    decision_id = decision_ledger.new_decision_id()
    assert decision_id.startswith("dec-"), f"unexpected decision_id prefix: {decision_id!r}"
    assert len(decision_id) > len("dec-"), "decision_id should carry a uuid hex tail"

    await store.update_order_book(mock_book)
    # Sanity check the mock book meets the requested initial condition (mid=0.5).
    assert mock_book.mid is not None
    assert mock_book.mid == pytest.approx(0.5)
    assert mock_book.spread == pytest.approx(0.02)

    # ─── (1) PREDICTION stage ─────────────────────────────────────────────
    # Mirror signal_trader._ml_signal: call ml_model.predict(), then emit the
    # PREDICTION stage to the decision ledger with the resulting p_yes /
    # confidence / market_mid / spread / predicted_edge.
    features = np.zeros(38, dtype=np.float32)
    features[0] = mock_book.mid or 0.5  # mid_price feature (index 0)
    p_yes, confidence = ml_model.predict(features, token_id=TOKEN)
    assert p_yes == pytest.approx(0.85)
    assert confidence == pytest.approx(0.70)

    mid = mock_book.mid or 0.5
    spread = mock_book.spread or 0.01
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
    assert len(chain) == 1, f"expected 1 stage after PREDICTION, got {len(chain)}"
    assert chain[0]["stage"] == STAGE_PREDICTION
    assert chain[0]["decision_id"] == decision_id
    assert chain[0]["token_id"] == TOKEN
    assert chain[0]["strategy"] == STRATEGY
    assert chain[0]["data"]["p_yes"] == pytest.approx(0.85)
    assert chain[0]["data"]["confidence"] == pytest.approx(0.70)
    assert chain[0]["data"]["market_mid"] == pytest.approx(0.5)

    # ─── (2) SIGNAL stage ─────────────────────────────────────────────────
    # p_yes=0.85 >= 0.55 → direction BUY. Target price = best_ask + 1 tick
    # (matches signal_trader._ml_signal's BUY pricing logic).
    direction = Side.BUY
    best_ask = mock_book.best_ask
    assert best_ask is not None
    target_price = round(min(best_ask + 0.001, 0.98), 4)  # 0.511
    size_usdc = 1.50
    reason_str = f"ML Prob={p_yes:.1%} (edge={predicted_edge*100:.1f}%)"

    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_SIGNAL,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        direction=direction.value,
        target_price=target_price,
        size_usdc=size_usdc,
        p_yes=p_yes,
        confidence=confidence,
        market_mid=mid,
        reason=reason_str,
    )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 2, f"expected 2 stages after SIGNAL, got {len(chain)}"
    assert chain[1]["stage"] == STAGE_SIGNAL
    assert chain[1]["data"]["direction"] == "BUY"
    assert chain[1]["data"]["target_price"] == pytest.approx(target_price)
    assert chain[1]["data"]["size_usdc"] == pytest.approx(size_usdc)

    # ─── (3) RISK_APPROVED stage ─────────────────────────────────────────
    # Build the OrderArgs + provisional Order, run through the real risk
    # engine (mirrors strategies/base.submit_order's pre-dispatch check), and
    # then record RISK_APPROVED on success.
    size_shares = max(1.0, size_usdc / target_price)
    args = OrderArgs(
        token_id=TOKEN,
        price=target_price,
        side=direction,
        size=size_shares,
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
        f"risk_manager.check_order rejected a small paper BUY on a fresh store: "
        f"reason={reason!r}"
    )

    await decision_ledger.record(
        decision_id=decision_id,
        stage=STAGE_RISK_APPROVED,
        token_id=TOKEN,
        strategy=STRATEGY,
        pnl=0.0,
        side=direction.value,
        price=target_price,
        size=size_shares,
    )

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 3, f"expected 3 stages after RISK_APPROVED, got {len(chain)}"
    assert chain[2]["stage"] == STAGE_RISK_APPROVED
    assert chain[2]["data"]["side"] == "BUY"
    assert chain[2]["data"]["price"] == pytest.approx(target_price)
    assert chain[2]["data"]["size"] == pytest.approx(size_shares)

    # ─── (4) ORDER stage via the paper simulator ─────────────────────────
    # paper_sim.create_order is the production paper-path called by
    # strategies/base.submit_order. It populates the Order with the
    # decision_id, stores it, and emits the ORDER stage to the ledger.
    paper_order = await paper_sim.create_order(
        args, strategy=STRATEGY, decision_id=decision_id
    )
    assert paper_order is not None
    assert paper_order.decision_id == decision_id
    assert paper_order.paper is True
    assert paper_order.side == Side.BUY
    assert paper_order.token_id == TOKEN
    # Order is now staged in the open-orders store, awaiting the fill loop.
    assert paper_order.order_id in store.open_orders

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 4, f"expected 4 stages after ORDER, got {len(chain)}"
    assert chain[3]["stage"] == STAGE_ORDER
    assert chain[3]["data"]["order_id"] == paper_order.order_id
    assert chain[3]["data"]["side"] == "BUY"
    assert chain[3]["data"]["paper"] is True

    # ─── (5) FILL stage via the paper simulator fill loop ────────────────
    # _try_fill_orders iterates store.open_orders; for our BUY at
    # target_price=0.511 with best_ask=0.51 <= 0.511, _can_fill returns 0.51,
    # _apply_slippage shifts the fill price adversely (BUY pays the spread),
    # then _execute_fill records the FILL stage with realised P&L.
    await paper_sim._try_fill_orders()

    chain = await decision_ledger.get_chain(decision_id)
    assert len(chain) == 5, (
        f"expected the full 5-stage chain after FILL, got {len(chain)}: "
        f"{[r['stage'] for r in chain]}"
    )
    assert chain[4]["stage"] == STAGE_FILL
    # Opening BUY → realised P&L is 0.0 (paper_sim only computes P&L on SELL
    # closing a long; see paper/simulator._execute_fill). The `pnl` column
    # IS populated — that's the contract under test.
    assert "pnl" in chain[4]
    assert chain[4]["pnl"] == pytest.approx(0.0)
    fill_data = chain[4]["data"]
    assert fill_data is not None
    assert fill_data["fill_price"] > 0
    assert fill_data["fill_price"] <= 0.99
    assert fill_data["fill_size"] == pytest.approx(size_shares, rel=1e-3)
    assert fill_data["side"] == "BUY"
    assert fill_data["order_id"] == paper_order.order_id
    assert fill_data["paper"] is True
    # The fill should have moved the order out of the open-orders store.
    assert paper_order.order_id not in store.open_orders
    # And the position should now exist on the store.
    assert TOKEN in store.positions
    assert store.positions[TOKEN].yes_shares > 0

    # ─── (6) Verify the full 5-stage chain ───────────────────────────────
    stages = [row["stage"] for row in chain]
    assert stages == [
        STAGE_PREDICTION,
        STAGE_SIGNAL,
        STAGE_RISK_APPROVED,
        STAGE_ORDER,
        STAGE_FILL,
    ], f"unexpected chain stage order: {stages}"

    # Every row carries the same decision_id + token_id.
    assert all(row["decision_id"] == decision_id for row in chain), (
        "chain rows do not share a single decision_id"
    )
    assert all(row["token_id"] == TOKEN for row in chain), (
        "chain rows do not share a single token_id"
    )

    # Chronological ordering: timestamps must be non-decreasing.
    timestamps = [row["timestamp"] for row in chain]
    assert timestamps == sorted(timestamps), (
        f"chain timestamps are not in chronological order: {timestamps}"
    )

    # Token-level feed surfaces all 5 events (most recent first).
    token_chain = await decision_ledger.get_chain_by_token(TOKEN, limit=50)
    assert len(token_chain) >= 5, (
        f"token-level feed should surface all 5 events, got {len(token_chain)}"
    )
    # newest first → FILL must be the first row.
    assert token_chain[0]["stage"] == STAGE_FILL
    assert token_chain[0]["decision_id"] == decision_id
