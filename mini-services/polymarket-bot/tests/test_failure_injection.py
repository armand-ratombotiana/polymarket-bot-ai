"""
tests/test_failure_injection.py — S11 Failure Injection Tests.

Verifies the trading pipeline fails safely (no crash, graceful logging /
handling) under 8 representative failure modes:

  1. Gamma API unavailable        — ``ConnectionError`` on ``gamma_client.get_markets``
  2. SQLite unavailable           — ``DECISION_LEDGER_DB_PATH`` -> ``/dev/null``
  3. Malformed market data        — dict missing every expected key
  4. Stale order book             — ``book.updated_at`` > 120 s ago
  5. ML model exception           — ``ml_model.predict`` raises ``RuntimeError``
  6. Invalid signal (negative size) — order blocked by the risk gate's
                                     minimum-size rule
  7. Insufficient balance         — ``store.paper_balance = 0`` at submission
  8. Concurrent duplicate signal  — same ``token_id`` + strategy submitted
                                     twice; the second is a no-op

For every scenario the assertion contract is the same:

  * No unhandled exception propagates out of the strategy / paper / risk layer
    (the scan loop, order path, or ledger write swallows the failure).
  * The error is either logged (``caplog`` assertion) or surfaced via a
    structured return value (``None`` / rejected ``Order``).
  * The system remains in a consistent state for the next scan cycle —
    e.g. a broken ledger write never tears down the strategy's in-memory
    signal cache or the global ``store`` singletons.

Conventions mirror the existing S6 / S7 / S9 test files:

  * All durable DB / state file paths are redirected to ``/tmp`` via
    ``os.environ.setdefault`` BEFORE the first import of any project module
    (every path-reading module — ``core.safety``, ``core.audit_logger``,
    ``core.data_store``, ``core.decision_ledger``, ``ml.model_registry`` …
    resolves its on-disk path at module-import time).
  * ``sys.path`` is bootstrapped so the test runs regardless of the cwd
    pytest was launched from.
  * ``pytestmark = pytest.mark.asyncio`` applies the asyncio marker to every
    ``async def test_...`` in this module (the repo's ``pytest.ini`` cannot
    be edited per the S11 task constraint "Do NOT edit existing files").
  * An ``autouse`` fixture resets the global ``store`` / ``risk_manager`` /
    ``paper_sim`` / ``market_discovery`` singletons between tests so state
    from one assertion (an activated kill switch, a paused strategy, a
    leftover position) cannot leak into the next.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# ``setdefault`` lets an outer runner (CI / pytest invocation / a sibling test
# file imported earlier in the session) override these if it needs to; otherwise
# the tests run fully hermetic to /tmp and cannot clobber any real persisted
# state in the repo's ``data/`` directory. Mirrors the bootstrap pattern in
# ``tests/test_risk_manager.py`` and ``tests/test_paper_simulator.py``.
_TMP_ROOT = Path("/tmp/failure_injection_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    # Force the canonical trading mode to paper + live disabled so the
    # shadow / live-trading gates inside ``check_order`` don't short-circuit
    # before the path under test is reached.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-s11",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``strategies.*``, ``paper.*``, ``ml.*``, ``risk.*``) regardless
# of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

# ── Project imports (after env bootstrap) ──────────────────────────────────
from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import (  # noqa: E402
    BANKROLL_BASELINE,
    OrderBook,
    PriceLevel,
    Side,
    store,
)
from core.decision_ledger import (  # noqa: E402
    DecisionLedger,
    decision_ledger,
)
from core.gamma_client import gamma_client  # noqa: E402
from core.market_discovery import market_discovery  # noqa: E402
from core.safety import (  # noqa: E402
    ACTIVATION_REASON_FILE,
    KILL_SWITCH_PATH,
    clear_kill_switch,
)
from ml.model import ml_model  # noqa: E402
from paper.simulator import paper_sim  # noqa: E402
from risk.manager import risk_manager  # noqa: E402
from strategies.signal_trader import (  # noqa: E402
    MarketSignal,
    SignalTraderStrategy,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` are intentionally
# left untouched (S11 constraint: "Do NOT edit existing files").
pytestmark = pytest.mark.asyncio


# ── Fixture: reset shared state before AND after every test ─────────────────
@pytest.fixture(autouse=True)
def _reset_global_state():
    """Reset the global singletons between tests.

    The strategy / risk engine / data store / paper simulator / market
    discovery catalog are all process-global singletons. Without a reset,
    state from one test (an activated kill switch, a paused strategy, a
    leftover position, a stale catalog entry, an altered ``peak_equity``)
    would leak into the next and mask regressions. This fixture restores
    a clean baseline:

      * kill switch off (in-memory flag AND the durable marker file removed)
      * PnL zeroed (daily + weekly), weekly window started "now"
      * peak equity at ``BANKROLL_BASELINE`` ($100.00)
      * paper_balance at ``BANKROLL_BASELINE``
      * positions / open_orders / trades / market_slugs / order_books /
        events all cleared; equity_history reset to the single initial point
      * observation-only mode off
      * per-strategy cooldowns cleared
      * paper_sim virtual balance reset to ``BANKROLL_BASELINE``
      * market_discovery.catalog cleared

    The durable kill-switch marker file is removed both before AND after the
    test — a test that triggers the breaker (via the daily-loss or MDD gates)
    writes the marker; without the post-test cleanup the next test's
    ``kill_switch_file_exists()`` would return True and short-circuit
    ``check_order`` at the kill-switch gate instead of reaching the path
    under test.
    """
    _clear_durable_kill_switch()
    _reset_store_state()
    _reset_risk_engine_state()
    _reset_paper_simulator_state()
    _reset_market_discovery_state()

    yield  # ── test runs ──

    _clear_durable_kill_switch()
    _reset_store_state()
    _reset_risk_engine_state()
    _reset_paper_simulator_state()
    _reset_market_discovery_state()


def _clear_durable_kill_switch() -> None:
    """Remove the durable kill-switch marker file (and its reason sidecar)."""
    try:
        clear_kill_switch()
    except OSError:
        for p in (KILL_SWITCH_PATH, ACTIVATION_REASON_FILE):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _reset_store_state() -> None:
    """Restore the global ``store`` to a freshly-bootstrapped baseline."""
    store.kill_switch_active = False
    store.daily_pnl = 0.0
    store.weekly_pnl = 0.0
    store.week_window_started_at = time.time()
    store.paper_balance = BANKROLL_BASELINE
    store.peak_equity = BANKROLL_BASELINE
    store.session_start = time.time()
    store.open_orders.clear()
    store.order_history.clear()
    store.positions.clear()
    store.trades.clear()
    store.market_slugs.clear()
    store.order_books.clear()
    store.event_log.clear()
    store.equity_history = [
        {"timestamp": time.time(), "equity": BANKROLL_BASELINE, "pnl": 0.0}
    ]


def _reset_risk_engine_state() -> None:
    """Restore the global ``risk_manager`` to its post-ctor state."""
    risk_manager.observation_only = False
    risk_manager.observation_reason = ""
    risk_manager._strategy_cooldowns.clear()


def _reset_paper_simulator_state() -> None:
    """Restore ``paper_sim``'s virtual balance to the baseline."""
    paper_sim._virtual_balance_usdc = BANKROLL_BASELINE


def _reset_market_discovery_state() -> None:
    """Clear the in-memory market catalog so each test seeds its own markets."""
    market_discovery.catalog.clear()


# ── Helpers ────────────────────────────────────────────────────────────────
def _make_book(
    token_id: str = "TOK_TEST",
    bid_price: float = 0.55,
    bid_size: float = 100.0,
    ask_price: float = 0.57,
    ask_size: float = 100.0,
    updated_at: float | None = None,
) -> OrderBook:
    """Build a minimal two-sided OrderBook for tests."""
    bids = [PriceLevel(price=bid_price, size=bid_size)] if bid_price is not None else []
    asks = [PriceLevel(price=ask_price, size=ask_size)] if ask_price is not None else []
    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        updated_at=updated_at if updated_at is not None else time.time(),
    )


def _basic_market() -> dict:
    """A minimal valid market dict with the keys ``extract_features`` reads."""
    return {
        "slug": "test-market",
        "volume24hr": 1_000.0,
        "volume": 7_000.0,
        "liquidity": 5_000.0,
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. Gamma API unavailable (ConnectionError)
# ────────────────────────────────────────────────────────────────────────────
async def test_01_api_unavailable_does_not_crash_scan(caplog, monkeypatch):
    """Gamma API ``ConnectionError`` is caught — ``_scan_markets`` returns.

    The strategy's scan loop falls back to ``gamma_client.get_markets`` when
    ``market_discovery.catalog`` is empty (the first-startup race window).
    If the Gamma API is unreachable the call raises ``ConnectionError``; the
    scan loop catches it, logs at DEBUG, and returns without raising —
    leaving the strategy ready for the next scan cycle.
    """
    # Empty catalog forces the Gamma-API fallback path.
    market_discovery.catalog.clear()

    async def _raise_connection_error(*args, **kwargs):
        raise ConnectionError("Gamma API unreachable (test injection)")

    monkeypatch.setattr(gamma_client, "get_markets", _raise_connection_error)

    strategy = SignalTraderStrategy()
    with caplog.at_level("DEBUG", logger="strategies.signal_trader"):
        # Must not raise — the scan loop swallows the ConnectionError.
        await strategy._scan_markets()

    # Graceful handling: the Gamma fallback failure is logged at DEBUG.
    assert any(
        "Gamma fallback failed" in r.message for r in caplog.records
    ), "Expected a 'Gamma fallback failed' DEBUG log when the API is unreachable"


# ────────────────────────────────────────────────────────────────────────────
# 2. SQLite unavailable (DB_PATH -> /dev/null)
# ────────────────────────────────────────────────────────────────────────────
async def test_02_sqlite_unavailable_ledger_does_not_crash(caplog, monkeypatch):
    """``DECISION_LEDGER_DB_PATH`` -> ``/dev/null`` — ledger writes fail
    silently (logged at ERROR) and the strategy still returns a signal.

    ``sqlite3.connect('/dev/null')`` succeeds but every ``CREATE TABLE`` /
    ``INSERT`` fails with ``OperationalError`` (read-only device). The
    decision ledger wraps every write in ``try/except``, logs at ERROR, and
    returns normally — so the strategy's fire-and-forget ``_emit_ledger``
    pattern never propagates the failure into the scan loop.
    """
    # ── (a) A fresh DecisionLedger pointed at /dev/null ──────────────────
    broken_ledger = DecisionLedger(Path("/dev/null"))
    with caplog.at_level("ERROR", logger="core.decision_ledger"):
        # Must not raise even though the DB is unwritable.
        await broken_ledger.record(
            decision_id="dec-sqlite-test",
            stage="PREDICTION",
            token_id="TOK_SQLITE",
            strategy="signal_trader",
            pnl=0.0,
            p_yes=0.7,
            confidence=0.6,
        )
    # Graceful handling: the persistence error is logged at ERROR.
    assert any(
        "record failed" in r.message for r in caplog.records
    ), "Expected a 'record failed' ERROR log when the SQLite DB is unavailable"

    # ── (b) The global decision_ledger is also broken ───────────────────
    # Monkeypatch the singleton's _db_path so the strategy's fire-and-forget
    # writes hit /dev/null too. The strategy must still return a signal.
    original_path = decision_ledger._db_path
    monkeypatch.setattr(decision_ledger, "_db_path", Path("/dev/null"))

    try:
        strategy = SignalTraderStrategy()
        book = _make_book(token_id="TOK_SQLITE")
        await store.update_order_book(book)
        features = __import__("numpy").zeros(38, dtype="float32")
        features[0] = 0.56  # mid_price

        # Mock the ML model to return a strong BUY signal so _ml_signal
        # reaches the SIGNAL-stage ledger write (which will fail silently).
        from unittest.mock import patch
        with patch.object(ml_model, "predict", return_value=(0.85, 0.70)):
            with caplog.at_level("ERROR", logger="core.decision_ledger"):
                sig = strategy._ml_signal(
                    "TOK_SQLITE", "sqlite-test", _basic_market(), book, features
                )

        # The strategy MUST still return a signal despite the broken ledger.
        assert sig is not None, "Strategy must return a signal even when the ledger DB is broken"
        assert sig.decision_id, "Signal must carry a decision_id even when the ledger is broken"

        # Let the fire-and-forget ledger writes flush so their ERROR logs land.
        await __import__("asyncio").sleep(0.3)

        # The ledger writes failed but were logged (not raised).
        ledger_errors = [
            r for r in caplog.records if "record failed" in r.message
        ]
        assert ledger_errors, "Expected ledger write failures to be logged at ERROR"
    finally:
        # Restore the global ledger's path so subsequent tests use the temp DB.
        decision_ledger._db_path = original_path


# ────────────────────────────────────────────────────────────────────────────
# 3. Malformed market data (dict with missing keys)
# ────────────────────────────────────────────────────────────────────────────
async def test_03_malformed_market_data_does_not_crash(caplog):
    """A market dict missing every expected key is skipped gracefully.

    ``extract_features`` reads ``market.get("volume24hr") or 0.0`` (etc.) so
    missing keys default to 0.0; ``extract_token_ids`` returns ``[]`` for a
    dict without a ``tokens`` array; ``slug = mkt.get("slug") or … or
    token_id[:12]`` falls back to a truncated token id. None of these raise
    on a malformed dict — the market is simply skipped (returns ``None``)
    and the scan continues with the next market.
    """
    strategy = SignalTraderStrategy()
    book = _make_book(token_id="TOK_MALFORMED")
    await store.update_order_book(book)

    # Adversarial market dict: no volume, no liquidity, no slug, no tokens.
    malformed_market = {"unexpected_key": "value"}

    with caplog.at_level("DEBUG", logger="strategies.signal_trader"):
        # Must not raise — _evaluate_market returns None for the bad market.
        result = await strategy._evaluate_market(malformed_market, token_id="TOK_MALFORMED")

    # Graceful handling: the market is skipped (None or a signal, but no crash).
    assert result is None or hasattr(result, "token_id"), (
        "Malformed market data must not crash _evaluate_market"
    )


# ────────────────────────────────────────────────────────────────────────────
# 4. Stale order book (updated_at > 120s ago)
# ────────────────────────────────────────────────────────────────────────────
async def test_04_stale_order_book_does_not_crash(caplog):
    """An order book whose ``updated_at`` is > 120 s old is handled
    gracefully — the strategy does not crash and either returns a signal
    or ``None`` for the stale market.

    NOTE: The current ``SignalTraderStrategy._evaluate_market`` does NOT
    explicitly check ``book.updated_at`` — the ``book_stall_seconds``
    setting (default 120 s) is consumed by the watchdog / book-poller
    circuit breaker, not by the strategy. This test pins the current
    behaviour: a stale book is processed without crashing. A future
    hardening could add an explicit staleness gate inside
    ``_evaluate_market`` (out of scope for S11).
    """
    strategy = SignalTraderStrategy()
    # Stale book: updated_at 200 s ago (> 120 s stall threshold).
    stale_book = _make_book(
        token_id="TOK_STALE",
        updated_at=time.time() - 200,
    )
    await store.update_order_book(stale_book)

    with caplog.at_level("DEBUG", logger="strategies.signal_trader"):
        # Must not raise even though the book is stale.
        result = await strategy._evaluate_market(
            _basic_market(), token_id="TOK_STALE"
        )

    # Graceful handling: no crash — the strategy returns None or a signal.
    assert result is None or hasattr(result, "token_id"), (
        "Stale order book must not crash _evaluate_market"
    )


# ────────────────────────────────────────────────────────────────────────────
# 5. ML model exception (predict raises)
# ────────────────────────────────────────────────────────────────────────────
async def test_05_model_exception_does_not_crash_scan(caplog, monkeypatch):
    """``ml_model.predict`` raising ``RuntimeError`` — the per-market
    ``try/except`` inside ``_scan_markets`` catches it, logs at DEBUG, and
    continues with the next market. The scan loop never propagates the
    failure.
    """
    # Seed the catalog so _scan_markets iterates at least one market.
    market_discovery.catalog.clear()
    market_discovery.catalog["TOK_MODEL_EX"] = _basic_market()
    # Seed the book so _evaluate_market reaches the predict() call.
    await store.update_order_book(_make_book(token_id="TOK_MODEL_EX"))

    def _raise_on_predict(features, token_id=""):
        raise RuntimeError("model inference failed (test injection)")

    monkeypatch.setattr("ml.model.ml_model.predict", _raise_on_predict)

    strategy = SignalTraderStrategy()
    with caplog.at_level("DEBUG", logger="strategies.signal_trader"):
        # Must not raise — the scan loop's per-market try/except catches it.
        await strategy._scan_markets()

    # Graceful handling: the per-market evaluation error is logged at DEBUG.
    assert any(
        "Market evaluation error" in r.message for r in caplog.records
    ), "Expected a 'Market evaluation error' DEBUG log when ml_model.predict raises"


# ────────────────────────────────────────────────────────────────────────────
# 6. Invalid signal (negative size)
# ────────────────────────────────────────────────────────────────────────────
async def test_06_invalid_signal_negative_size_rejected(caplog):
    """An ``OrderArgs`` with a negative size is blocked by the risk gate's
    minimum-size rule (``order.size < 0.5``) and ``submit_order`` returns
    ``None``. The strategy logs the block and remains ready for the next
    signal.
    """
    strategy = SignalTraderStrategy()
    args = OrderArgs(
        token_id="TOK_NEG_SIZE",
        price=0.58,
        side=Side.BUY,
        size=-5.0,  # negative — below the 0.5 minimum-liquidity threshold
    )

    with caplog.at_level("DEBUG", logger="strategies.base"):
        # Must not raise — the risk gate rejects and submit_order returns None.
        order = await strategy.submit_order(args, decision_id="dec-neg-size-test")

    # Graceful handling: order is None (rejected), no crash, no partial state.
    assert order is None, "submit_order must return None for a negative-size order"

    # The rejection is logged via store.log_event (visible in store.event_log).
    events = await store.get_recent_events(n=10)
    assert any("Risk block" in e for e in events), (
        "Expected a 'Risk block' event log entry for the rejected order"
    )

    # No order was added to the open-orders book.
    assert len(store.open_orders) == 0, (
        "A rejected (negative-size) order must not appear in store.open_orders"
    )


# ────────────────────────────────────────────────────────────────────────────
# 7. Insufficient balance (paper_balance = 0)
# ────────────────────────────────────────────────────────────────────────────
async def test_07_insufficient_balance_does_not_crash(caplog):
    """``store.paper_balance = 0`` — the order path completes without
    crashing.

    NOTE: The current ``InstitutionalRiskEngine.check_order`` uses
    ``BANKROLL_BASELINE`` (USD 100) for sizing and checks
    ``total_exposure`` against ``MAX_DEPLOYABLE_CAPITAL`` (USD 60), but
    does NOT explicitly consult ``store.paper_balance`` before allowing an
    order. This test pins the current behaviour: a zero-balance state does
    not crash the system — the order either passes the existing exposure
    gates (and is created, leaving balance tracking to record the
    resulting negative balance) or is rejected by some other risk gate.
    Either way, no unhandled exception propagates. A future hardening
    could add an explicit paper-balance gate (out of scope for S11).
    """
    original_balance = store.paper_balance
    original_kill = store.kill_switch_active
    store.paper_balance = 0.0
    store.kill_switch_active = False
    order = None
    try:
        strategy = SignalTraderStrategy()
        args = OrderArgs(
            token_id="TOK_BROKE",
            price=0.50,
            side=Side.BUY,
            size=2.0,  # cost = $1.00 — within per-market / deployable caps
        )

        with caplog.at_level("DEBUG", logger="strategies.base"):
            # Must not raise — the system handles a zero paper_balance
            # without crashing, even though no explicit balance gate exists.
            order = await strategy.submit_order(args, decision_id="dec-broke-test")

        # Graceful handling: either rejected by a risk gate (None) or
        # created (Order) — but no crash, no unhandled exception.
        assert order is None or hasattr(order, "order_id"), (
            "submit_order must return None or an Order even with paper_balance=0"
        )
    finally:
        # Clean up any order that was created so it doesn't leak into the
        # next test via the global store (the autouse fixture also resets,
        # but this is belt-and-braces).
        if order is not None and hasattr(order, "order_id"):
            store.open_orders.pop(order.order_id, None)
        store.paper_balance = original_balance
        store.kill_switch_active = original_kill


# ────────────────────────────────────────────────────────────────────────────
# 8. Concurrent duplicate signal (same token_id + strategy)
# ────────────────────────────────────────────────────────────────────────────
async def test_08_concurrent_duplicate_signal_no_double_order(caplog):
    """Two ``MarketSignal``s for the same ``token_id`` from the same
    strategy — ``_act_on_signal`` skips the second when the first order is
    still open (``sig.token_id in self._active_signals`` and the order id
    is still in ``store.open_orders``). Exactly one paper order is created.
    """
    strategy = SignalTraderStrategy()
    token_id = "TOK_DUPLICATE"
    book = _make_book(token_id=token_id)
    await store.update_order_book(book)
    # No existing position so the first signal can actually fire.
    store.positions.pop(token_id, None)

    sig = MarketSignal(
        token_id=token_id,
        slug="dup-test",
        direction=Side.BUY,
        confidence=0.90,
        target_price=0.57,
        size_usdc=1.0,
        reason="duplicate-signal-test",
        ml_score=0.85,
        source="ml",
        decision_id="dec-dup-1",
    )

    # First signal — should create exactly one paper order.
    await strategy._act_on_signal(sig)
    orders_after_first = len(store.open_orders)
    assert orders_after_first == 1, (
        f"First signal should create exactly 1 order, got {orders_after_first}"
    )

    # Second signal for the same token_id + strategy — must be a no-op
    # (the first order is still open, so _act_on_signal returns early).
    sig2 = MarketSignal(
        token_id=token_id,
        slug="dup-test",
        direction=Side.BUY,
        confidence=0.95,  # higher confidence — would normally win
        target_price=0.57,
        size_usdc=1.0,
        reason="duplicate-signal-test",
        ml_score=0.90,
        source="ml",
        decision_id="dec-dup-2",
    )
    await strategy._act_on_signal(sig2)
    orders_after_second = len(store.open_orders)

    # Graceful handling: no duplicate order created.
    assert orders_after_second == orders_after_first, (
        "Duplicate signal for the same token_id+strategy must not create a "
        f"second order (first={orders_after_first}, second={orders_after_second})"
    )

    # The first order's id is cached in _active_signals.
    assert token_id in strategy._active_signals, (
        "First signal should have populated _active_signals[token_id]"
    )
