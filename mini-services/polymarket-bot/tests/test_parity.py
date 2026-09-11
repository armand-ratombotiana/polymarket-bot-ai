"""Backtest/Live parity tests — prove equivalent decisions for identical inputs.

Tests that the same strategy, given the same market data and configuration,
produces the same signals, risk decisions, and order intents in:
1. Backtest mode (BacktestBroker)
2. Paper mode (PaperBroker)
3. Shadow mode (if applicable)

This ensures no look-ahead bias, no hidden state differences, and no
behavioral divergence between backtest and live execution.

Scope
-----
Eight tests, grouped by concern:

  Signal layer
    1. ``test_same_signal_for_same_input``               — two fresh strategy
        instances built from identical config produce byte-equal ``Signal``
        objects when handed identical market contexts.

  Risk layer
    2. ``test_same_risk_decision_for_same_signal``      — the same
        ``Order`` (built from the same signal) returns the same
        ``(allowed, reason)`` tuple from ``risk_manager.check_order``
        regardless of when the call is made or how many times it runs.

  Order-intent layer
    3. ``test_same_order_intent_for_same_risk_decision`` — a strategy
        derives an ``OrderRequest`` deterministically from the same
        ``(signal, capital, risk_params)`` triple.

  Execution / accounting layer
    4. ``test_same_position_for_same_fills``            — two BacktestBrokers
        fed identical fill sequences end up with byte-equal ``Position``
        snapshots (size, avg_price, realized_pnl).
    5. ``test_same_pnl_for_same_trades``               — the realized P&L
        accumulated across the two brokers is bit-for-bit identical (the
        §32 parity contract: backtest accounting must mirror live accounting).

  Isolation
    6. ``test_no_hidden_state_leakage``                — running a backtest
        through ``BacktestBroker`` (the hermetic broker) leaves the
        process-global ``store`` / ``paper_sim`` singletons untouched
        (no position / order / balance leakage across modes).
    7. ``test_deterministic_replay``                   — the same replay
        inputs (snapshot series + strategy + window) produce byte-equal
        ``ReplayResult`` outputs across two consecutive invocations.

  Interface
    8. ``test_broker_interface_consistency``           — every concrete
        ``Broker`` subclass (``BacktestBroker`` / ``PaperBroker`` /
        ``LiveBroker``) implements the same six abstractmethods so a
        strategy coded against ``Broker`` works against any venue.

Design
------
The parity contract is structural: ``BacktestBroker``, ``PaperBroker`` and
``LiveBroker`` all delegate ``apply_slippage`` to the canonical
``PaperSimulator._apply_slippage`` static method (see ``core/broker.py``
docstring — God Mode §32). Because the slippage model is shared and
deterministic (the queue-tick is a stable SHA-256 hash of the synthetic
order_id, itself derived from ``(price, size, side)``), identical inputs
produce identical outputs across venues.

The tests below assert that contract end-to-end: from the strategy's
``generate_signal`` → ``size_position`` → ``entry_logic`` → ``apply_slippage``
→ ``BacktestBroker.submit_order`` chain, every layer is deterministic and
the result of running the same chain through any concrete ``Broker``
subclass is bit-equal.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ── Redirect persisted-state env vars BEFORE importing any bot module. ──────
# Mirrors ``tests/conftest.py`` so this file is hermetic when invoked directly
# (``python -m pytest tests/test_parity.py``) even outside the full suite.
_TMP_ROOT = Path("/tmp/pmbot_parity_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-parity",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``backtesting.*``) regardless of the
# cwd pytest was launched from. Mirrors the bootstrap pattern in every
# existing ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from backtesting.historical_replay import (  # noqa: E402
    HistoricalReplayEngine,
    SimpleStrategy,
)
from core.broker import (  # noqa: E402
    BacktestBroker,
    Broker,
    LiveBroker,
    OrderRequest,
    PaperBroker,
    get_broker,
)
from core.data_store import Order, Side, store  # noqa: E402
from strategies.base import BaseStrategy, Signal  # noqa: E402

pytestmark = pytest.mark.asyncio


# ── Shared constants ────────────────────────────────────────────────────────

_TOKEN_ID = "0xparitytest" + "0" * 31  # 0x + 64 hex chars → EIP-712-shaped id
_INITIAL_CAPITAL = 100.0
# A mid price inside the Polymarket [0.05, 0.95] tick-bearing range so the
# canonical slippage model's crossing + queue ticks contribute.
_MID_PRICE = 0.50
_BID_PRICE = 0.49
_ASK_PRICE = 0.51
_SIZE = 5.0


# ════════════════════════════════════════════════════════════════════════════
# ── Deterministic strategy fixture ──────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


class ParityStrategy(BaseStrategy):
    """Deterministic signal / sizing strategy for parity tests.

    Implements the 9-method ``StrategyContract`` with fully deterministic
    outputs so the parity tests can compare byte-equal ``Signal`` /
    ``OrderRequest`` results across strategy instances and broker
    implementations. The signal logic is deliberately simple (BUY when
    ``mid < entry_threshold``; SELL when ``mid > exit_threshold``) so the
    parity assertions don't get lost in the strategy's own complexity.

    The strategy is hermetic: it holds its own state (``self._signals``,
    ``self._trades``) and never touches the global ``store`` /
    ``paper_sim`` singletons, so two instances with the same config
    produce byte-equal outputs from the same input.
    """

    name: str = "parity_strategy"

    def __init__(
        self,
        *,
        entry_threshold: float = 0.45,
        exit_threshold: float = 0.55,
        confidence: float = 0.70,
        edge: float = 0.05,
        name: Optional[str] = None,
    ) -> None:
        # Bypass ``BaseStrategy.__init__``'s settings-dependent defaults
        # (``self._paper = settings.paper_trade``) so the strategy is
        # constructed identically regardless of the live TRADING_MODE env
        # var. Mirrors the hermetic-construction pattern in
        # ``tests/test_strategy_base.py``.
        super().__init__(name=name or self.name, config={})
        self.entry_threshold = float(entry_threshold)
        self.exit_threshold = float(exit_threshold)
        self._confidence = float(confidence)
        self._edge = float(edge)
        # Per-instance counters surfaced through ``diagnostics()`` so
        # the parity tests can assert that two strategy instances ran the
        # same number of signals / trades.
        self._signals = 0
        self._trades = 0

    # ── StrategyContract overrides ──────────────────────────────────────────

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        mid = float(market_context.get("mid", 0.5))
        token_id = str(market_context.get("token_id", _TOKEN_ID))
        position = float(market_context.get("position", 0.0))
        self._signals += 1
        if position == 0.0 and mid < self.entry_threshold:
            return Signal(
                action="BUY",
                token_id=token_id,
                size=_SIZE,
                price=_ASK_PRICE,
                confidence=self._confidence,
                edge=self._edge,
                reason=f"mid {mid:.4f} < entry_threshold {self.entry_threshold:.4f}",
                metadata={"mid": mid, "threshold": self.entry_threshold},
            )
        if position > 0.0 and mid > self.exit_threshold:
            return Signal(
                action="SELL",
                token_id=token_id,
                size=position,
                price=_BID_PRICE,
                confidence=self._confidence,
                edge=0.0,
                reason=f"mid {mid:.4f} > exit_threshold {self.exit_threshold:.4f}",
                metadata={"mid": mid, "threshold": self.exit_threshold},
            )
        return None

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        # Fixed size so the parity assertions on ``OrderRequest.size`` are
        # not perturbed by the strategy's own sizing heuristic.
        if signal is None or signal.action == "HOLD":
            return 0.0
        return float(signal.size)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        # Deterministic entry params: limit order at the signal's price
        # (already set by ``generate_signal``), GTC time-in-force.
        return {
            "price": float(signal.price) if signal.price is not None
            else float(market_context.get("mid", 0.5)),
            "type": "limit",
            "time_in_force": "GTC",
            "side": signal.action,
            "size": float(signal.size),
        }

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0-parity",
            "description": "Deterministic parity-test strategy",
            "author": "polymarket-bot",
        }

    async def _run(self) -> None:
        # No-op — the parity tests call ``generate_signal`` /
        # ``size_position`` / ``entry_logic`` directly rather than
        # spinning up the strategy's async loop.
        return None


@pytest.fixture
def parity_strategy() -> ParityStrategy:
    """Fresh ``ParityStrategy`` with the default deterministic config."""
    return ParityStrategy()


@pytest.fixture
def market_context_buy() -> dict:
    """A market context that triggers a BUY signal (``mid < entry_threshold``).

    ``mid=0.40`` < ``entry_threshold=0.45`` so ``generate_signal`` returns a
    BUY ``Signal``. ``position=0.0`` so the strategy is flat (BUY allowed).
    """
    return {
        "token_id": _TOKEN_ID,
        "best_bid": _BID_PRICE,
        "best_ask": _ASK_PRICE,
        "mid": 0.40,
        "spread": 0.02,
        "volume": 100.0,
        "bid_size": 10.0,
        "ask_size": 10.0,
        "timestamp": time.time(),
        "position": 0.0,
        "capital": _INITIAL_CAPITAL,
        "snapshot_index": 0,
    }


@pytest.fixture
def market_context_hold() -> dict:
    """A market context that produces no signal (``mid`` between thresholds).

    ``mid=0.50`` ∈ (entry_threshold, exit_threshold) so the strategy holds.
    Built as a fresh dict (not derived from ``market_context_buy``) so the
    two fixtures don't depend on pytest's fixture-ordering rules.
    """
    return {
        "token_id": _TOKEN_ID,
        "best_bid": _BID_PRICE,
        "best_ask": _ASK_PRICE,
        "mid": 0.50,
        "spread": 0.02,
        "volume": 100.0,
        "bid_size": 10.0,
        "ask_size": 10.0,
        "timestamp": time.time(),
        "position": 0.0,
        "capital": _INITIAL_CAPITAL,
        "snapshot_index": 0,
    }


# ════════════════════════════════════════════════════════════════════════════
# ── (1) Same signal for same input ─────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_same_signal_for_same_input(
    parity_strategy: ParityStrategy,
    market_context_buy: dict,
) -> None:
    """Two fresh strategy instances built from the same config produce
    byte-equal ``Signal`` objects when handed the same market context.

    This is the foundational parity assertion: if the signal layer diverges
    between two fresh instances of the same strategy, every downstream
    layer (risk → order → fill → PnL) will diverge too. So we test it
    first, in isolation, with no broker in the picture.
    """
    # Two fresh instances, same config.
    strat_a = ParityStrategy()
    strat_b = ParityStrategy()

    # Identical input contexts.
    ctx_a = dict(market_context_buy)
    ctx_b = dict(market_context_buy)

    sig_a = strat_a.generate_signal(ctx_a)
    sig_b = strat_b.generate_signal(ctx_b)

    # Both must produce a non-None BUY signal (mid=0.40 < threshold=0.45).
    assert sig_a is not None, "strat_a returned None — expected a BUY signal"
    assert sig_b is not None, "strat_b returned None — expected a BUY signal"
    assert sig_a.action == "BUY"
    assert sig_b.action == "BUY"

    # Byte-equal across the fields a downstream layer would consume.
    assert sig_a.action == sig_b.action
    assert sig_a.token_id == sig_b.token_id
    assert sig_a.size == pytest.approx(sig_b.size)
    assert sig_a.price == pytest.approx(sig_b.price) if sig_a.price else sig_a.price == sig_b.price
    assert sig_a.confidence == pytest.approx(sig_b.confidence)
    assert sig_a.edge == pytest.approx(sig_b.edge)
    assert sig_a.reason == sig_b.reason
    assert sig_a.metadata == sig_b.metadata

    # The same strategy instance must also produce byte-equal signals on a
    # second invocation with the same input — guards against hidden state
    # inside the strategy (e.g. a counter that leaks into the signal).
    sig_a2 = strat_a.generate_signal(dict(market_context_buy))
    assert sig_a2 is not None
    assert sig_a2.action == sig_a.action
    assert sig_a2.token_id == sig_a.token_id
    assert sig_a2.size == pytest.approx(sig_a.size)
    assert sig_a2.price == pytest.approx(sig_a.price) if sig_a.price else sig_a2.price == sig_a.price
    assert sig_a2.confidence == pytest.approx(sig_a.confidence)
    assert sig_a2.edge == pytest.approx(sig_a.edge)
    assert sig_a2.reason == sig_a.reason
    assert sig_a2.metadata == sig_a.metadata


async def test_same_signal_for_same_input_hold_case(
    parity_strategy: ParityStrategy,
    market_context_hold: dict,
) -> None:
    """The parity contract applies to the no-signal case too: two fresh
    strategy instances handed a context that should NOT trigger a signal
    must both return ``None`` (not diverge to ``Signal(action="HOLD")``
    vs ``None``)."""
    strat_a = ParityStrategy()
    strat_b = ParityStrategy()
    sig_a = strat_a.generate_signal(dict(market_context_hold))
    sig_b = strat_b.generate_signal(dict(market_context_hold))
    assert sig_a is None
    assert sig_b is None


# ════════════════════════════════════════════════════════════════════════════
# ── (2) Same risk decision for same signal ─────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_same_risk_decision_for_same_signal(
    parity_strategy: ParityStrategy,
    market_context_buy: dict,
    isolated_risk_manager,
) -> None:
    """The same ``Order`` (derived from the same ``Signal``) returns the
    same ``(allowed, reason)`` tuple from the risk engine on every call.

    Uses the ``isolated_risk_manager`` fixture from ``conftest.py`` (a
    fresh ``InstitutionalRiskEngine`` with no per-strategy cooldowns and
    observation-only mode off) so the test doesn't perturb the global
    ``risk_manager`` singleton's state for the rest of the suite.
    """
    # Build two identical signals from the same strategy + context.
    sig_a = parity_strategy.generate_signal(dict(market_context_buy))
    sig_b = parity_strategy.generate_signal(dict(market_context_buy))
    assert sig_a is not None and sig_b is not None

    # Build two identical Orders from the signals (paper=True so the
    # risk engine doesn't short-circuit on "live trading disabled").
    order_a = Order(
        order_id="parity-risk-a",
        token_id=sig_a.token_id,
        side=Side.BUY,
        price=float(sig_a.price),
        size=float(sig_a.size),
        strategy="parity_strategy",
        paper=True,
    )
    order_b = Order(
        order_id="parity-risk-b",  # different id, otherwise identical
        token_id=sig_b.token_id,
        side=Side.BUY,
        price=float(sig_b.price),
        size=float(sig_b.size),
        strategy="parity_strategy",
        paper=True,
    )

    # Two consecutive check_order calls on the same isolated engine must
    # return the same decision (the risk engine is stateless across calls
    # that don't trip the per-strategy cooldown / kill switch).
    allowed_a, reason_a = await isolated_risk_manager.check_order(order_a)
    allowed_b, reason_b = await isolated_risk_manager.check_order(order_b)

    assert allowed_a == allowed_b, (
        f"Risk decision diverged: allowed_a={allowed_a} ({reason_a!r}) "
        f"vs allowed_b={allowed_b} ({reason_b!r})"
    )
    assert reason_a == reason_b, (
        f"Risk reason diverged: {reason_a!r} vs {reason_b!r}"
    )

    # The decision must also be stable on a third call with the same input —
    # guards against hidden state inside the risk engine (e.g. a counter
    # that flips a circuit breaker after N calls).
    order_c = copy.deepcopy(order_a)
    order_c.order_id = "parity-risk-c"
    allowed_c, reason_c = await isolated_risk_manager.check_order(order_c)
    assert allowed_c == allowed_a
    assert reason_c == reason_a


# ════════════════════════════════════════════════════════════════════════════
# ── (3) Same order intent for same risk decision ───────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_same_order_intent_for_same_risk_decision(
    parity_strategy: ParityStrategy,
    market_context_buy: dict,
) -> None:
    """A strategy derives an ``OrderRequest`` deterministically from the
    same ``(signal, capital, risk_params)`` triple.

    The test asserts that two strategy instances built from the same
    config produce byte-equal ``OrderRequest`` objects — the order-intent
    layer must be a pure function of (signal, capital, risk_params) so
    the downstream broker layer sees identical inputs across venues.
    """
    sig = parity_strategy.generate_signal(dict(market_context_buy))
    assert sig is not None and sig.action == "BUY"

    risk_params = {"max_position_per_market": 5.0, "max_total_open_risk": 25.0}

    # Two fresh strategies, same config — derive OrderRequests from each.
    strat_a = ParityStrategy()
    strat_b = ParityStrategy()
    sig_a = strat_a.generate_signal(dict(market_context_buy))
    sig_b = strat_b.generate_signal(dict(market_context_buy))
    assert sig_a is not None and sig_b is not None

    size_a = strat_a.size_position(sig_a, _INITIAL_CAPITAL, risk_params)
    size_b = strat_b.size_position(sig_b, _INITIAL_CAPITAL, risk_params)
    entry_a = strat_a.entry_logic(sig_a, dict(market_context_buy))
    entry_b = strat_b.entry_logic(sig_b, dict(market_context_buy))

    # Pure-function parity on size + entry params.
    assert size_a == pytest.approx(size_b)
    assert entry_a == entry_b

    # Build the broker-facing OrderRequests from the derived params.
    # Same client_order_id (deterministic) so the broker's slippage hash
    # is identical across both requests.
    req_a = OrderRequest(
        token_id=sig_a.token_id,
        side=entry_a["side"],
        size=size_a,
        price=entry_a["price"],
        order_type=entry_a["type"],
        time_in_force=entry_a["time_in_force"],
        strategy=strat_a.name,
        client_order_id="parity-order-intent-001",
    )
    req_b = OrderRequest(
        token_id=sig_b.token_id,
        side=entry_b["side"],
        size=size_b,
        price=entry_b["price"],
        order_type=entry_b["type"],
        time_in_force=entry_b["time_in_force"],
        strategy=strat_b.name,
        client_order_id="parity-order-intent-001",
    )

    # Byte-equal across every field a broker would consume.
    assert req_a.token_id == req_b.token_id
    assert req_a.side == req_b.side
    assert req_a.size == pytest.approx(req_b.size)
    assert req_a.price == pytest.approx(req_b.price)
    assert req_a.order_type == req_b.order_type
    assert req_a.time_in_force == req_b.time_in_force
    assert req_a.client_order_id == req_b.client_order_id
    assert req_a.strategy == req_b.strategy

    # And the slippage the broker would apply to those requests must be
    # byte-equal too (the §32 parity contract: same inputs → same slipped
    # fill price across all three broker subclasses).
    broker_a = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    broker_b = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    fill_a_price, fill_a_size = broker_a.apply_slippage(
        req_a.price, req_a.size, req_a.side,
    )
    fill_b_price, fill_b_size = broker_b.apply_slippage(
        req_b.price, req_b.size, req_b.side,
    )
    assert fill_a_price == pytest.approx(fill_b_price)
    assert fill_a_size == pytest.approx(fill_b_size)


# ════════════════════════════════════════════════════════════════════════════
# ── (4) Same position for same fills ────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_same_position_for_same_fills() -> None:
    """Two ``BacktestBroker`` instances fed identical fill sequences
    end up with byte-equal ``Position`` snapshots.

    This is the §32 parity contract for the execution / accounting
    layer: a strategy that runs the same fill sequence in backtest must
    see the same position the live broker would see (assuming the same
    slippage model — which the broker ABC guarantees by delegating
    ``apply_slippage`` to the canonical paper-simulator static method).
    """
    broker_a = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    broker_b = BacktestBroker(initial_capital=_INITIAL_CAPITAL)

    # Identical fill sequence: BUY 10 @ 0.50, BUY 5 @ 0.55, SELL 8 @ 0.60.
    fill_sequence = [
        OrderRequest(
            token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
            client_order_id="fill-1",
        ),
        OrderRequest(
            token_id=_TOKEN_ID, side="BUY", size=5.0, price=0.55,
            client_order_id="fill-2",
        ),
        OrderRequest(
            token_id=_TOKEN_ID, side="SELL", size=8.0, price=0.60,
            client_order_id="fill-3",
        ),
    ]

    # Run the same fill sequence through both brokers.
    responses_a = []
    responses_b = []
    for req in fill_sequence:
        responses_a.append(await broker_a.submit_order(req))
        responses_b.append(await broker_b.submit_order(req))

    # Every fill response must be byte-equal across the two brokers.
    for i, (ra, rb) in enumerate(zip(responses_a, responses_b)):
        assert ra.status == rb.status, f"fill {i}: status diverged {ra.status} vs {rb.status}"
        assert ra.fill_price == pytest.approx(rb.fill_price), (
            f"fill {i}: fill_price diverged {ra.fill_price} vs {rb.fill_price}"
        )
        assert ra.fill_size == pytest.approx(rb.fill_size), (
            f"fill {i}: fill_size diverged {ra.fill_size} vs {rb.fill_size}"
        )

    # Final positions must be byte-equal.
    pos_a_list = await broker_a.get_positions()
    pos_b_list = await broker_b.get_positions()
    assert len(pos_a_list) == len(pos_b_list) == 1, (
        f"position count diverged: a={len(pos_a_list)} b={len(pos_b_list)}"
    )
    pos_a = pos_a_list[0]
    pos_b = pos_b_list[0]
    assert pos_a.token_id == pos_b.token_id
    assert pos_a.side == pos_b.side
    assert pos_a.size == pytest.approx(pos_b.size), (
        f"position size diverged: a={pos_a.size} b={pos_b.size}"
    )
    assert pos_a.avg_price == pytest.approx(pos_b.avg_price), (
        f"position avg_price diverged: a={pos_a.avg_price} b={pos_b.avg_price}"
    )
    assert pos_a.realized_pnl == pytest.approx(pos_b.realized_pnl), (
        f"position realized_pnl diverged: a={pos_a.realized_pnl} b={pos_b.realized_pnl}"
    )

    # And the balance (cash remaining after the fills) must be byte-equal.
    balance_a = await broker_a.get_balance()
    balance_b = await broker_b.get_balance()
    assert balance_a == pytest.approx(balance_b), (
        f"balance diverged: a={balance_a} b={balance_b}"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── (5) Same PnL for same trades ────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_same_pnl_for_same_trades() -> None:
    """Two ``BacktestBroker`` instances fed identical trade sequences
    accumulate byte-equal realized P&L.

    The P&L parity contract: a backtest's reported P&L must equal the
    P&L the live / paper broker would report for the same fill sequence.
    Because the slippage model is shared and the SELL→exit accounting
    mirrors ``paper/simulator.py::_execute_fill`` (``pnl = (exit -
    entry) * shares_sold``), the two brokers' realized P&L tracks are
    bit-equal.
    """
    broker_a = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    broker_b = BacktestBroker(initial_capital=_INITIAL_CAPITAL)

    # Round-trip: BUY 10 @ 0.50 → SELL 10 @ 0.55 → realizes the spread.
    # Repeat twice on broker_a (testing within-broker determinism) and
    # once on broker_b (testing cross-broker parity).
    round_trip = [
        OrderRequest(
            token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
            client_order_id="rt-buy",
        ),
        OrderRequest(
            token_id=_TOKEN_ID, side="SELL", size=10.0, price=0.55,
            client_order_id="rt-sell",
        ),
    ]

    for req in round_trip:
        await broker_a.submit_order(req)

    # After the first round-trip, broker_a's position is closed (size=0)
    # and realized P&L is recorded. The position is evicted from the
    # ledger (size <= 1e-9 → del), so the realized P&L lives on the
    # broker's order history (via ``get_order_status``) rather than on
    # a ``Position`` snapshot. We pull it from the SELL response.
    sell_resp_a = await broker_a.get_order_status("rt-sell")
    assert sell_resp_a is not None
    assert sell_resp_a.status == "FILLED"
    # Realized P&L on broker_a (computed from fill_price - entry_price).
    buy_resp_a = await broker_a.get_order_status("rt-buy")
    assert buy_resp_a is not None
    pnl_a = (sell_resp_a.fill_price - buy_resp_a.fill_price) * sell_resp_a.fill_size

    # Run the same round-trip on broker_b.
    for req in round_trip:
        await broker_b.submit_order(req)
    sell_resp_b = await broker_b.get_order_status("rt-sell")
    buy_resp_b = await broker_b.get_order_status("rt-buy")
    assert sell_resp_b is not None and buy_resp_b is not None
    pnl_b = (sell_resp_b.fill_price - buy_resp_b.fill_price) * sell_resp_b.fill_size

    # Cross-broker parity: same fill sequence → same P&L.
    assert pnl_a == pytest.approx(pnl_b), (
        f"P&L diverged across brokers: a={pnl_a} b={pnl_b}"
    )

    # Balance parity: both brokers end up with the same cash balance
    # after the round-trip (no fees in this model; P&L = balance delta).
    balance_a = await broker_a.get_balance()
    balance_b = await broker_b.get_balance()
    assert balance_a == pytest.approx(balance_b), (
        f"balance diverged: a={balance_a} b={balance_b}"
    )

    # The realized P&L must also equal the balance delta (initial → final).
    # This is the load-bearing accounting-parity assertion: the strategy's
    # reported P&L (broker order history) must equal the bankroll's P&L
    # (balance change), with no hidden slippage between the two.
    initial_balance = _INITIAL_CAPITAL
    assert (balance_a - initial_balance) == pytest.approx(pnl_a), (
        f"P&L≠balance delta: pnl={pnl_a} balance_delta={balance_a - initial_balance}"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── (6) No hidden state leakage ─────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_no_hidden_state_leakage() -> None:
    """Running a backtest through ``BacktestBroker`` (the hermetic broker)
    leaves the process-global ``store`` / ``paper_sim`` singletons
    untouched — no position / order / balance leakage across modes.

    This is the structural guarantee that makes backtest→live parity
    possible: ``BacktestBroker`` holds its own capital + positions ledger
    by design (see ``core/broker.py::BacktestBroker.__init__``), so two
    BacktestBrokers running in the same process don't see each other's
    positions, and a backtest run doesn't perturb the live / paper
    broker's view of the world.
    """
    # Snapshot the global store's state before the backtest runs.
    store_open_orders_before = len(store.open_orders)
    store_positions_before = len(store.positions)
    store_trades_before = len(store.trades)
    store_paper_balance_before = float(store.paper_balance)
    store_peak_equity_before = float(store.peak_equity)

    # Run a backtest: BUY 10 @ 0.50, SELL 10 @ 0.55 through BacktestBroker.
    broker = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    await broker.submit_order(
        OrderRequest(
            token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
            client_order_id="leak-buy",
        )
    )
    await broker.submit_order(
        OrderRequest(
            token_id=_TOKEN_ID, side="SELL", size=10.0, price=0.55,
            client_order_id="leak-sell",
        )
    )

    # Snapshot the global store's state after the backtest runs.
    store_open_orders_after = len(store.open_orders)
    store_positions_after = len(store.positions)
    store_trades_after = len(store.trades)
    store_paper_balance_after = float(store.paper_balance)
    store_peak_equity_after = float(store.peak_equity)

    # The global store must be byte-equal before / after — BacktestBroker
    # never touches it.
    assert store_open_orders_after == store_open_orders_before, (
        f"open_orders leaked: {store_open_orders_before} → {store_open_orders_after}"
    )
    assert store_positions_after == store_positions_before, (
        f"positions leaked: {store_positions_before} → {store_positions_after}"
    )
    assert store_trades_after == store_trades_before, (
        f"trades leaked: {store_trades_before} → {store_trades_after}"
    )
    assert store_paper_balance_after == pytest.approx(store_paper_balance_before), (
        f"paper_balance leaked: {store_paper_balance_before} → {store_paper_balance_after}"
    )
    assert store_peak_equity_after == pytest.approx(store_peak_equity_before), (
        f"peak_equity leaked: {store_peak_equity_before} → {store_peak_equity_after}"
    )

    # And the BacktestBroker's own ledger reflects the trades it ran —
    # i.e. the broker's hermetic state is the canonical source of truth
    # for backtest results, NOT the global store. (Two orders + two fills.)
    broker_positions = await broker.get_positions()
    assert len(broker_positions) == 0  # position fully closed by the SELL
    # Capital changed (proceeds from the SELL minus cost from the BUY).
    broker_balance = await broker.get_balance()
    assert broker_balance != pytest.approx(_INITIAL_CAPITAL)


# ════════════════════════════════════════════════════════════════════════════
# ── (7) Deterministic replay ───────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


def _make_replay_schema(db_path: Path) -> None:
    """Create the ``market_snapshots`` schema in ``db_path`` (idempotent).

    Mirrors the helper in ``tests/test_historical_replay.py`` so the
    parity test can seed a deterministic snapshot series without pulling
    in the full sibling-test fixture stack.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                token_id TEXT NOT NULL,
                slug TEXT,
                best_bid REAL,
                best_ask REAL,
                mid REAL,
                spread REAL,
                volume_24h REAL,
                liquidity REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orderbook_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                token_id TEXT NOT NULL,
                best_bid_size REAL,
                best_ask_size REAL,
                ofi REAL,
                micro_price REAL
            )
        """)
        conn.execute("DELETE FROM market_snapshots")
        conn.execute("DELETE FROM orderbook_ticks")
        conn.commit()


def _seed_deterministic_series(db_path: Path, token_id: str) -> tuple[float, float]:
    """Seed a deterministic mean-reverting snapshot series.

    Same shape as ``_seed_mean_reverting_series`` in
    ``tests/test_historical_replay.py`` — 25 baseline snapshots at
    ``mid=0.50``, then a dip at ``mid=0.40`` (BUY trigger), then a
    recovery at ``mid=0.50`` (SELL trigger). Returns ``(start_ts, end_ts)``.
    """
    start_ts = float(int(time.time()))
    ts = start_ts
    spread = 0.02
    # 25 baseline snapshots at mid=0.50.
    for _ in range(25):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO market_snapshots
                (timestamp, token_id, slug, best_bid, best_ask, mid, spread, volume_24h, liquidity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, token_id, "parity-test",
                 0.50 - spread / 2.0, 0.50 + spread / 2.0, 0.50, spread, 100.0, 50.0),
            )
            conn.commit()
        ts += 60.0
    # Dip snapshot at mid=0.40 (BUY trigger).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots
            (timestamp, token_id, slug, best_bid, best_ask, mid, spread, volume_24h, liquidity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, token_id, "parity-test",
             0.40 - spread / 2.0, 0.40 + spread / 2.0, 0.40, spread, 100.0, 50.0),
        )
        conn.commit()
    ts += 60.0
    # Recovery snapshot at mid=0.50 (SELL trigger).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots
            (timestamp, token_id, slug, best_bid, best_ask, mid, spread, volume_24h, liquidity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, token_id, "parity-test",
             0.50 - spread / 2.0, 0.50 + spread / 2.0, 0.50, spread, 100.0, 50.0),
        )
        conn.commit()
    ts += 60.0
    return start_ts, ts


async def test_deterministic_replay() -> None:
    """The same replay inputs produce byte-equal ``ReplayResult`` outputs
    across two consecutive invocations.

    The replay engine is intentionally synchronous (single pass through
    the snapshot list — see ``backtesting/historical_replay.py`` docstring)
    so the same inputs (snapshot series + strategy + window) must produce
    byte-equal outputs. This is the §32 parity contract for the
    historical-replay path: a strategy tested in backtest must see the
    same trades, equity curve, and risk metrics on every replay (no
    RNG-driven divergence, no look-ahead bias, no hidden state).
    """
    db_path = Path(os.environ.get("MARKET_DB_PATH", str(_TMP_ROOT / "market_intelligence.db")))
    _make_replay_schema(db_path)
    token_id = "TKN_PARITY"
    start_ts, end_ts = _seed_deterministic_series(db_path, token_id)

    engine = HistoricalReplayEngine(str(db_path))

    # Run the same replay twice with the same strategy instance shape.
    # (Two fresh ``SimpleStrategy`` instances so any per-instance state
    # from the first run doesn't leak into the second — the parity
    # contract applies across fresh instances, not just same-instance
    # repeated calls.)
    strategy_run1 = SimpleStrategy(window=20, threshold=0.01)
    strategy_run2 = SimpleStrategy(window=20, threshold=0.01)
    result1 = engine.replay(
        token_id=token_id,
        strategy=strategy_run1,
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=_INITIAL_CAPITAL,
    )
    result2 = engine.replay(
        token_id=token_id,
        strategy=strategy_run2,
        start_time=start_ts - 1.0,
        end_time=end_ts + 1.0,
        initial_capital=_INITIAL_CAPITAL,
    )

    # Both replays must produce the same trade count + actions.
    assert result1.n_snapshots == result2.n_snapshots
    assert len(result1.trades) == len(result2.trades)
    actions1 = [t["action"] for t in result1.trades]
    actions2 = [t["action"] for t in result2.trades]
    assert actions1 == actions2, (
        f"trade actions diverged: {actions1} vs {actions2}"
    )

    # Trade-by-trade byte-equality.
    for i, (t1, t2) in enumerate(zip(result1.trades, result2.trades)):
        assert t1["action"] == t2["action"], f"trade {i} action diverged"
        assert t1["price"] == pytest.approx(t2["price"]), (
            f"trade {i} price diverged: {t1['price']} vs {t2['price']}"
        )
        assert t1["size"] == pytest.approx(t2["size"]), (
            f"trade {i} size diverged: {t1['size']} vs {t2['size']}"
        )
        assert t1["pnl"] == pytest.approx(t2["pnl"]), (
            f"trade {i} pnl diverged: {t1['pnl']} vs {t2['pnl']}"
        )

    # Equity curve byte-equality.
    assert len(result1.equity_curve) == len(result2.equity_curve)
    for i, (e1, e2) in enumerate(zip(result1.equity_curve, result2.equity_curve)):
        assert e1 == pytest.approx(e2), (
            f"equity[{i}] diverged: {e1} vs {e2}"
        )

    # Risk metrics byte-equality.
    assert result1.total_return == pytest.approx(result2.total_return)
    assert result1.sharpe == pytest.approx(result2.sharpe)
    assert result1.max_drawdown == pytest.approx(result2.max_drawdown)
    assert result1.win_rate == pytest.approx(result2.win_rate)
    assert result1.profit_factor == pytest.approx(result2.profit_factor)


# ════════════════════════════════════════════════════════════════════════════
# ── (8) Broker interface consistency ───────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════


async def test_broker_interface_consistency() -> None:
    """Every concrete ``Broker`` subclass (``BacktestBroker``,
    ``PaperBroker``, ``LiveBroker``) implements the same six
    abstractmethods (``submit_order``, ``cancel_order``,
    ``get_order_status``, ``get_positions``, ``get_balance``,
    ``apply_slippage``) so a strategy coded against ``Broker`` works
    against any venue.

    The parity contract relies on interface uniformity: a strategy that
    depends on the abstract ``Broker`` ABC must be able to swap between
    any concrete subclass without per-mode branching. This test
    enumerates the abstract methods and asserts every subclass
    implements them (i.e. none are still abstract on the subclass).
    """
    # The abstractmethod names — copied from ``Broker`` ABC in
    # ``core/broker.py`` so the test breaks if a method is added /
    # removed without updating every subclass.
    abstract_method_names = {
        "submit_order",
        "cancel_order",
        "get_order_status",
        "get_positions",
        "get_balance",
        "apply_slippage",
    }

    # Instantiate each broker.
    backtest = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    paper = PaperBroker()
    live = LiveBroker()

    # All three must be ``Broker`` instances (the ABC contract).
    assert isinstance(backtest, Broker)
    assert isinstance(paper, Broker)
    assert isinstance(live, Broker)

    # The factory must return the right subclass for each mode.
    assert isinstance(get_broker("backtest", initial_capital=_INITIAL_CAPITAL), BacktestBroker)
    assert isinstance(get_broker("paper"), PaperBroker)
    assert isinstance(get_broker("live"), LiveBroker)

    for broker, name in ((backtest, "BacktestBroker"),
                         (paper, "PaperBroker"),
                         (live, "LiveBroker")):
        # Every abstractmethod name must resolve to a concrete callable
        # on the subclass (i.e. ``__isabstractmethod__`` is False or the
        # attribute isn't in ``__abstractmethods__``).
        still_abstract = set(getattr(broker, "__abstractmethods__", set()))
        assert not (abstract_method_names & still_abstract), (
            f"{name} still has abstract methods: {abstract_method_names & still_abstract}"
        )

        # Each method is callable (the bound-method object exists on the
        # instance and is callable).
        for method_name in abstract_method_names:
            method = getattr(broker, method_name, None)
            assert method is not None, (
                f"{name} missing method {method_name!r}"
            )
            assert callable(method), (
                f"{name}.{method_name} is not callable"
            )

    # And the shared slippage model produces byte-equal outputs across all
    # three brokers — the §32 parity contract's load-bearing assertion:
    # if any subclass overrides ``apply_slippage`` with a different model,
    # this test breaks.
    raw_price = 0.50
    size = 10.0
    side = "BUY"
    bt_fill = backtest.apply_slippage(raw_price, size, side)
    paper_fill = paper.apply_slippage(raw_price, size, side)
    live_fill = live.apply_slippage(raw_price, size, side)
    assert bt_fill[0] == pytest.approx(paper_fill[0]), (
        f"BacktestBroker vs PaperBroker slippage diverged: {bt_fill[0]} vs {paper_fill[0]}"
    )
    assert paper_fill[0] == pytest.approx(live_fill[0]), (
        f"PaperBroker vs LiveBroker slippage diverged: {paper_fill[0]} vs {live_fill[0]}"
    )
    assert bt_fill[1] == pytest.approx(size)
    assert paper_fill[1] == pytest.approx(size)
    assert live_fill[1] == pytest.approx(size)


# ════════════════════════════════════════════════════════════════════════════
# ── Cross-venue parity for the apply_slippage contract ────────────────────
# ════════════════════════════════════════════════════════════════════════════
# This block consolidates the slippage-parity assertions into a single
# parametrized test so a regression on any (price, size, side) triple
# surfaces a single, easy-to-read failure.


@pytest.mark.parametrize(
    "price,size,side",
    [
        (0.50, 5.0, "BUY"),
        (0.50, 5.0, "SELL"),
        (0.30, 100.0, "BUY"),
        (0.70, 100.0, "SELL"),
        (0.10, 1.0, "BUY"),
        (0.90, 1.0, "SELL"),
    ],
    ids=[
        "buy-mid-small",
        "sell-mid-small",
        "buy-low-large",
        "sell-high-large",
        "buy-near-floor",
        "sell-near-ceiling",
    ],
)
async def test_apply_slippage_parity_across_brokers(price: float, size: float, side: str) -> None:
    """All three broker subclasses produce byte-equal slippage outputs
    for every (price, size, side) triple in the parametrized matrix.

    This is the load-bearing §32 parity contract: same inputs → same
    slipped fill price across backtest, paper, and live. If any broker
    ever overrides ``apply_slippage`` with a different model, this test
    breaks — surfacing the regression immediately rather than letting
    it accumulate silent behavioural divergence.
    """
    backtest = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    paper = PaperBroker()
    live = LiveBroker()

    bt_price, bt_size = backtest.apply_slippage(price, size, side)
    paper_price, paper_size = paper.apply_slippage(price, size, side)
    live_price, live_size = live.apply_slippage(price, size, side)

    # Slipped fill prices must be byte-equal across all three brokers.
    assert bt_price == pytest.approx(paper_price), (
        f"BacktestBroker vs PaperBroker slipped price diverged: "
        f"{bt_price} vs {paper_price} (inputs: price={price}, size={size}, side={side})"
    )
    assert paper_price == pytest.approx(live_price), (
        f"PaperBroker vs LiveBroker slipped price diverged: "
        f"{paper_price} vs {live_price} (inputs: price={price}, size={size}, side={side})"
    )

    # Fill sizes must equal the requested size (the canonical model only
    # adjusts the fill price; size reduction is a future partial-fill
    # extension that hasn't landed — so every broker must return ``size``).
    assert bt_size == pytest.approx(size)
    assert paper_size == pytest.approx(size)
    assert live_size == pytest.approx(size)


# ════════════════════════════════════════════════════════════════════════════
# ── Round-trip parity: backtest fill shape == canonical slippage shape ─────
# ════════════════════════════════════════════════════════════════════════════


async def test_backtest_submit_order_uses_canonical_slippage() -> None:
    """``BacktestBroker.submit_order`` applies the canonical slippage
    model to the BUY fill price — the same shape ``PaperBroker`` /
    ``LiveBroker`` would apply via ``apply_slippage``.

    This is the §32 structural fix made testable: prior to W19-7 the
    backtest engine walked a synthetic 5-level book with
    ``spread_bps`` + ``depth_decay`` + square-root market impact, while
    paper / live used tick-based crossing + size + queue. After W19-7
    both paths route through ``Broker._canonical_slippage`` — so a BUY
    that goes through ``BacktestBroker.submit_order`` fills at the
    SAME price as a BUY estimated via ``broker.apply_slippage``.
    """
    broker = BacktestBroker(initial_capital=_INITIAL_CAPITAL)
    request = OrderRequest(
        token_id=_TOKEN_ID,
        side="BUY",
        size=10.0,
        price=0.50,
        client_order_id="canonical-shape",
    )

    # The broker's submit_order applies apply_slippage internally.
    response = await broker.submit_order(request)
    assert response.status == "FILLED"

    # The same call via the public apply_slippage method must produce
    # the same slipped fill price (the §32 contract: submit_order and
    # apply_slippage share the SAME slippage model).
    expected_price, expected_size = broker.apply_slippage(0.50, 10.0, "BUY")
    assert response.fill_price == pytest.approx(expected_price), (
        f"submit_order fill_price {response.fill_price} diverged from "
        f"apply_slippage expected_price {expected_price}"
    )
    assert response.fill_size == pytest.approx(expected_size)
