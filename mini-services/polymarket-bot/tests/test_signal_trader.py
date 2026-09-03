"""
tests/test_signal_trader.py — Unit tests for ``strategies/signal_trader.py``.

X14 — Signal Trader strategy unit tests.

Scope: pure-Python / mocked-dependency tests of the three signal-trader
hot-path methods exercised by the scan loop:

  (1) ``_ml_signal`` returns a ``MarketSignal`` when model confidence
      meets/exceeds the configured threshold (and the spread / Kelly /
      allocator gates all pass).
  (2) ``_ml_signal`` returns ``None`` when confidence is below the
      threshold (early rejection at the confidence gate).
  (3) ``_ml_signal`` returns ``None`` when the book spread is at or above
      the 0.04 regime-filter threshold.
  (4) ``_act_on_signal`` creates a new order (and records it in
      ``_active_signals``) when no position or open order exists for the
      token yet.
  (5) ``_act_on_signal`` skips order creation entirely when a position
      already exists for the token (the one-directional-position-per-
      market rule).
  (6) ``_evaluate_market`` returns ``None`` for a market whose order book
      is missing from the store — and proactively enqueues the token for
      polling via ``book_poller.add_tokens``.

Dependencies are mocked at the module-attribute level so the strategy
under test never touches the real ML model (which trains on import
against an 18-second sklearn pipeline), the live Gamma API, or the
book poller's HTTP layer. The global ``store`` singleton is reset to
its factory baseline by the conftest autouse fixture before every test,
and the strategy instance is constructed fresh per test (no shared
mutable state across tests).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (pytest-asyncio is already a project
dependency — see ``tests/test_decision_ledger.py`` for the same idiom).
Sync helpers / fixtures remain plain ``def`` — pytest-asyncio's strict
mode (the package default) does not require sync functions to carry the
marker.
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with ``tests/conftest.py``: conftest sets these first via
# its own ``_ENV_REDIRECTS`` table, but if this module is imported before
# conftest (e.g. by a different runner that does not pick up conftest), the
# ``setdefault`` calls here ensure the strategy import never reaches into
# the repo's real ``data/`` directory (which is read-only in the sandbox
# — see the import-time ``PermissionError: [Errno 13] Permission denied:
# '/app/data'`` raised by ``ml.model_registry.ModelRegistry._save_to_disk``
# when MODEL_REGISTRY_PATH is unset).
_TMP_ROOT = Path("/tmp/signal_trader_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS = {
    "STORE_STATE_PATH": _TMP_ROOT / "store_state.json",
    "DECISION_LEDGER_DB_PATH": _TMP_ROOT / "decision_ledger.db",
    "AUDIT_DB_PATH": _TMP_ROOT / "audit_trail.db",
    "MARKET_DB_PATH": _TMP_ROOT / "market_intelligence.db",
    "KILL_SWITCH_PATH": _TMP_ROOT / "kill_switch",
    "KILL_SWITCH_REASON_PATH": _TMP_ROOT / "kill_switch.reason",
    "VECTOR_STORE_PATH": _TMP_ROOT / "vector_index.json",
    "MODEL_PATH": _TMP_ROOT / "model.pkl",
    "MODEL_REGISTRY_PATH": _TMP_ROOT / "model_registry.json",
    # Force the canonical trading mode to paper + live disabled so any
    # risk-gate plumbing that happens to be reached defaults to a
    # paper-only, no-network path.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, str(_val))

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``ml.*``, ``strategies.*``) when pytest is invoked from a
# different cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from core.data_store import (  # noqa: E402
    Order,
    OrderBook,
    Position,
    PriceLevel,
    Side,
    store,
)
from strategies.signal_trader import (  # noqa: E402
    MarketSignal,
    SignalTraderStrategy,
)


# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the pattern in ``tests/test_decision_ledger.py`` — the
# repo's ``pytest.ini`` cannot be edited per the X14 task constraint
# ("Do NOT edit existing files"), so we use the module-level ``pytestmark``
# idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────
_TOKEN_ID = "0xtest_token_12345abcdef"


def _book(
    bid_price: float = 0.59,
    bid_size: float = 10.0,
    ask_price: float = 0.61,
    ask_size: float = 10.0,
) -> OrderBook:
    """Build a minimal two-sided ``OrderBook`` for ``_ml_signal`` tests.

    Default spread (0.61 − 0.59 = 0.02) is comfortably below the 0.04
    regime-filter threshold so the spread gate never trips for callers
    that use the defaults — tests that *want* the spread gate to trip
    pass tighter / wider books explicitly.
    """
    return OrderBook(
        token_id=_TOKEN_ID,
        bids=[PriceLevel(price=bid_price, size=bid_size)],
        asks=[PriceLevel(price=ask_price, size=ask_size)],
    )


def _features() -> np.ndarray:
    """Dummy 38-dim feature vector — content is irrelevant because
    ``ml_model.predict`` is mocked to return a canned ``(p_yes, confidence)``
    tuple regardless of its first argument."""
    return np.zeros(38, dtype=np.float32)


def _signal(
    token_id: str = _TOKEN_ID,
    direction: Side = Side.BUY,
    decision_id: str = "dec-test-1",
) -> MarketSignal:
    """Build a minimal ``MarketSignal`` for ``_act_on_signal`` tests."""
    return MarketSignal(
        token_id=token_id,
        slug="test-market",
        direction=direction,
        confidence=0.85,
        target_price=0.55,
        size_usdc=2.5,
        reason="test",
        ml_score=0.75,
        source="ml",
        decision_id=decision_id,
    )


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture
def mock_ml_model(monkeypatch):
    """Replace the module-level ``ml_model`` reference in
    ``strategies.signal_trader`` with a ``MagicMock`` so no real sklearn
    inference runs (and no 18-second cold-start training happens on first
    call). Individual tests configure ``predict.return_value`` to drive
    the confidence / direction branches under test."""
    mock = MagicMock()
    mock.predict.return_value = (0.5, 0.5)
    monkeypatch.setattr("strategies.signal_trader.ml_model", mock)
    return mock


@pytest.fixture
def mock_allocate_capital(monkeypatch):
    """Replace ``allocate_capital`` with a stub that always returns $2.50
    so the V2 allocator's safety gates never short-circuit a passing
    ``_ml_signal`` to ``None``. Tests that exercise the allocator-zero
    rejection path (none in X14's six required tests) would override this
    fixture to return ``0.0`` instead."""
    monkeypatch.setattr(
        "strategies.signal_trader.allocate_capital",
        lambda **kwargs: 2.5,
    )
    return None


@pytest.fixture
def mock_book_poller(monkeypatch):
    """Replace ``book_poller`` with a ``MagicMock`` so the
    ``_evaluate_market`` missing-book path doesn't perturb the real poller's
    internal tier1/tier2 token sets (and never kicks off an HTTP poll)."""
    mock = MagicMock()
    monkeypatch.setattr("strategies.signal_trader.book_poller", mock)
    return mock


@pytest.fixture
def mock_gamma_client(monkeypatch):
    """Replace ``gamma_client`` with a ``MagicMock`` so the
    ``_evaluate_market`` ``extract_token_ids`` path doesn't issue live
    Gamma API calls. Default ``extract_token_ids.return_value`` returns
    a single ``_TOKEN_ID`` so the catalog-scan fallback code path is
    exercised without network I/O."""
    mock = MagicMock()
    mock.extract_token_ids.return_value = [_TOKEN_ID]
    monkeypatch.setattr("strategies.signal_trader.gamma_client", mock)
    return mock


@pytest.fixture
def strategy(monkeypatch):
    """Fresh ``SignalTraderStrategy`` per test.

    The strategy's ``_ml_signal`` path calls into ``core.decision_ledger``
    (lazily imported inside the method) to mint a ``decision_id`` and
    then fire-and-forgets ``PREDICTION`` / ``SIGNAL`` stage records via
    ``_emit_ledger``. Under pytest-asyncio the running event loop would
    otherwise schedule those never-awaited coroutines, producing
    ``RuntimeWarning: coroutine 'DecisionLedger.record' was never awaited``
    noise. To keep the test output clean — and to keep the strategy's
    decision-logic test independent of the ledger's SQLite plumbing — we:

      * Neutralise the instance's ``_emit_ledger`` / ``_emit_rejection``
        with ``MagicMock(return_value=None)`` so no async scheduling
        happens.
      * Patch the real ``decision_ledger`` singleton's ``record`` /
        ``record_rejection`` attributes with non-coroutine ``MagicMock``
        stubs so any code path that reaches them (e.g. the PREDICTION
        stage emit, which constructs the coro *before* passing it to
        ``_emit_ledger``) does not produce a stray coroutine object
        even as an intermediate value.

    ``_min_confidence`` is pinned to ``0.65`` explicitly so tests do
    not depend on the env-driven ``settings.signal_min_confidence``
    (default 0.65) — a sibling test that overrides the env var to a
    different value would otherwise flip the confidence gate's trip
    point under this module's tests.
    """
    s = SignalTraderStrategy()
    s._min_confidence = 0.65

    # Neutralise the fire-and-forget emit plumbing at the instance level.
    # ``_emit_ledger`` is a ``@staticmethod``; assigning a MagicMock to the
    # instance attribute shadows the descriptor (instance __dict__ lookups
    # bypass descriptor protocol), so ``self._emit_ledger(coro)`` calls
    # the MagicMock with ``coro`` and returns ``None``.
    monkeypatch.setattr(s, "_emit_ledger", MagicMock(return_value=None))
    monkeypatch.setattr(s, "_emit_rejection", MagicMock(return_value=None))

    # Belt-and-braces: the original ``_emit_rejection`` body imports
    # ``decision_ledger`` and calls ``record_rejection`` to build the
    # coro before passing it to ``_emit_ledger``. We've already replaced
    # ``_emit_rejection`` wholesale above, so the import never runs and
    # ``record_rejection`` is never called — but the PREDICTION/SIGNAL
    # stage emits in ``_ml_signal`` itself do call ``decision_ledger.record``
    # to build their coro. Patch the singleton's ``record`` /
    # ``record_rejection`` to return ``None`` (not a coroutine) so the
    # ``MagicMock(return_value=None)`` ``_emit_ledger`` doesn't receive a
    # stray coroutine object that would otherwise trigger the
    # "coroutine never awaited" warning when the test ends.
    from core.decision_ledger import decision_ledger as real_ledger
    monkeypatch.setattr(real_ledger, "record", MagicMock(return_value=None))
    monkeypatch.setattr(
        real_ledger, "record_rejection", MagicMock(return_value=None)
    )

    # ``new_decision_id`` is a ``@staticmethod`` returning a string — leave
    # it as the real implementation (no DB I/O, just ``uuid.uuid4().hex``).
    return s


# ── (1) _ml_signal returns MarketSignal when confidence >= threshold ────
async def test_ml_signal_returns_signal_when_confidence_above_threshold(
    strategy: SignalTraderStrategy,
    mock_ml_model: MagicMock,
    mock_allocate_capital: None,
) -> None:
    """Confidence 0.85 ≥ ``_min_confidence`` (0.65); p_yes 0.75 ≥ 0.55 → BUY
    direction; tight spread (0.02 < 0.04); allocator stub returns $2.50. All
    gates pass and a ``MarketSignal`` is returned with the propagated
    confidence, ml_score, direction, and size."""
    # p_yes=0.75 (BUY zone), confidence=0.85 (above floor)
    mock_ml_model.predict.return_value = (0.75, 0.85)
    # Tight spread: bid=0.59, ask=0.61 → spread=0.02 (< 0.04 gate)
    book = _book(bid_price=0.59, bid_size=10.0, ask_price=0.61, ask_size=10.0)
    mkt = {"slug": "test-market"}

    sig = strategy._ml_signal(_TOKEN_ID, "test-market", mkt, book, _features())

    assert sig is not None
    assert isinstance(sig, MarketSignal)
    assert sig.token_id == _TOKEN_ID
    assert sig.slug == "test-market"
    # p_yes=0.75 ≥ 0.55 ⇒ BUY direction
    assert sig.direction == Side.BUY
    # Confidence is propagated verbatim
    assert sig.confidence == pytest.approx(0.85)
    # ml_score is the raw p_yes from the model
    assert sig.ml_score == pytest.approx(0.75)
    # Allocator stub returned $2.50 (positive, passes the size_usdc > 0 gate)
    assert sig.size_usdc == pytest.approx(2.5)
    # target_price for BUY = round(min(best_ask + 0.001, 0.98), 4) = 0.611
    assert sig.target_price == pytest.approx(0.611, abs=1e-4)
    assert sig.source == "ml"
    # Decision id is populated by new_decision_id() (a non-empty "dec-…" string)
    assert sig.decision_id.startswith("dec-")
    # Predict was called exactly once with the features + token_id we supplied
    mock_ml_model.predict.assert_called_once()
    call_args, call_kwargs = mock_ml_model.predict.call_args
    assert call_args[0] is not None  # features array
    assert call_kwargs.get("token_id") == _TOKEN_ID


# ── (2) _ml_signal returns None when confidence < threshold ──────────────
async def test_ml_signal_returns_none_when_confidence_below_threshold(
    strategy: SignalTraderStrategy,
    mock_ml_model: MagicMock,
    mock_allocate_capital: None,
) -> None:
    """Confidence 0.30 < ``_min_confidence`` (0.65) — the first gate trips
    and the method short-circuits to ``None`` before touching the spread /
    Kelly / allocator branches. The allocator stub therefore must NOT be
    reached (verified indirectly: returning a positive $2.50 from the
    allocator would otherwise cause the test to fail by producing a
    ``MarketSignal`` we'd then assert against)."""
    # p_yes in BUY zone but confidence far below the floor
    mock_ml_model.predict.return_value = (0.75, 0.30)
    book = _book(bid_price=0.59, bid_size=10.0, ask_price=0.61, ask_size=10.0)

    sig = strategy._ml_signal(_TOKEN_ID, "test-market", {}, book, _features())

    assert sig is None
    mock_ml_model.predict.assert_called_once()


# ── (3) _ml_signal returns None when spread >= 0.04 ─────────────────────
async def test_ml_signal_returns_none_when_spread_at_or_above_threshold(
    strategy: SignalTraderStrategy,
    mock_ml_model: MagicMock,
    mock_allocate_capital: None,
) -> None:
    """Confidence is high (0.85) but the bid/ask spread is 0.05 ≥ 0.04 — the
    regime-filter gate trips *after* the confidence gate but *before* the
    directional / Kelly / allocator analysis. The method returns ``None``
    and the allocator stub is never consulted."""
    # High confidence — passes the confidence gate
    mock_ml_model.predict.return_value = (0.75, 0.85)
    # Wide spread: bid=0.50, ask=0.55 → spread=0.05 ≥ 0.04
    book = _book(bid_price=0.50, bid_size=10.0, ask_price=0.55, ask_size=10.0)

    sig = strategy._ml_signal(_TOKEN_ID, "test-market", {}, book, _features())

    assert sig is None
    mock_ml_model.predict.assert_called_once()


# ── (4) _act_on_signal creates order ────────────────────────────────────
async def test_act_on_signal_creates_order(
    strategy: SignalTraderStrategy,
) -> None:
    """When no position or open order exists for the token,
    ``_act_on_signal`` calls ``submit_order`` with a constructed
    ``OrderArgs`` and records the returned order_id in
    ``_active_signals``.

    ``submit_order`` is mocked (it's a method on the BaseStrategy base
    class that would otherwise route through ``risk_manager.check_order``
    → ``paper_sim.create_order`` / ``clob_client.create_order``). The
    AsyncMock returns a pre-built ``Order`` so we can assert the
    ``order_id`` is propagated to ``_active_signals[token_id]``.
    """
    # Sanity: the autouse conftest reset left a clean slate.
    assert _TOKEN_ID not in store.positions
    assert _TOKEN_ID not in strategy._active_signals
    assert _TOKEN_ID not in store.open_orders

    sig = _signal(decision_id="dec-test-1")
    fake_order = Order(
        order_id="order-xyz",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=5.0,
        paper=True,
        decision_id="dec-test-1",
    )
    # Replace the bound method on the instance with an AsyncMock.
    # ``self.submit_order(args, decision_id=...)`` then awaits the mock,
    # which yields ``fake_order``.
    strategy.submit_order = AsyncMock(return_value=fake_order)

    await strategy._act_on_signal(sig)

    # submit_order was awaited exactly once.
    strategy.submit_order.assert_awaited_once()
    call_args, call_kwargs = strategy.submit_order.call_args
    # First positional arg is the OrderArgs dataclass.
    assert len(call_args) == 1
    order_args = call_args[0]
    assert order_args.token_id == _TOKEN_ID
    assert order_args.side == Side.BUY
    assert order_args.price == pytest.approx(0.55)
    # size_shares = max(1.0, size_usdc / target_price) = max(1.0, 2.5/0.55)
    expected_shares = max(1.0, 2.5 / 0.55)
    assert order_args.size == pytest.approx(expected_shares)
    # decision_id propagated from the signal
    assert call_kwargs.get("decision_id") == "dec-test-1"

    # The returned order_id was registered in the active-signals map.
    assert _TOKEN_ID in strategy._active_signals
    assert strategy._active_signals[_TOKEN_ID] == "order-xyz"


# ── (5) _act_on_signal skips when position exists ───────────────────────
async def test_act_on_signal_skips_when_position_exists(
    strategy: SignalTraderStrategy,
) -> None:
    """When a position already exists for the token, ``_act_on_signal``
    short-circuits at the one-directional-position-per-market guard
    (the second ``if`` in the method body) and never calls
    ``submit_order``. No active-signal entry is created.
    """
    # Pre-populate a position for the token — the autouse conftest reset
    # has cleared store.positions, so we own this mutation.
    store.positions[_TOKEN_ID] = Position(token_id=_TOKEN_ID)
    assert _TOKEN_ID in store.positions

    sig = _signal(decision_id="dec-test-2")
    # AsyncMock — if it's called at all, the test should fail.
    strategy.submit_order = AsyncMock(return_value=None)

    await strategy._act_on_signal(sig)

    # Order creation was skipped entirely.
    strategy.submit_order.assert_not_called()
    # No active-signal entry was created.
    assert _TOKEN_ID not in strategy._active_signals


# ── (6) _evaluate_market returns None for missing book ──────────────────
async def test_evaluate_market_returns_none_for_missing_book(
    strategy: SignalTraderStrategy,
    mock_gamma_client: MagicMock,
    mock_book_poller: MagicMock,
) -> None:
    """When ``store.get_order_book`` returns ``None`` (token not yet
    polled), ``_evaluate_market`` returns ``None`` AND enqueues the token
    for polling via ``book_poller.add_tokens``.

    This test exercises the ``token_id=None`` code path: the market dict
    has no ``token_id`` parameter supplied, so the method falls back to
    ``gamma_client.extract_token_ids(mkt)`` — mocked here to return
    ``[_TOKEN_ID]``. The global ``store`` singleton has been reset by the
    autouse conftest fixture, so ``store.get_order_book(_TOKEN_ID)``
    naturally returns ``None`` (no books populated).
    """
    # Sanity: no books populated by the conftest reset.
    assert _TOKEN_ID not in store.order_books

    mkt = {"slug": "test-market"}

    result = await strategy._evaluate_market(mkt)

    assert result is None
    # gamma_client.extract_token_ids was consulted with the raw market dict.
    mock_gamma_client.extract_token_ids.assert_called_once_with(mkt)
    # book_poller.add_tokens was called with the discovered YES token id.
    mock_book_poller.add_tokens.assert_called_once_with([_TOKEN_ID])
    # The slug was registered for downstream telemetry / logging.
    assert store.market_slugs.get(_TOKEN_ID) == "test-market"


# ── Module-level warning filter ──────────────────────────────────────────
# Suppress the ``RuntimeWarning: coroutine '...' was never awaited`` that
# can leak from the (mocked-away but still coro-producing) decision-ledger
# PREDICTION / SIGNAL stage emits under pytest-asyncio's running loop.
# The fixture above patches ``_emit_ledger`` / ``_emit_rejection`` and the
# singleton's ``record`` / ``record_rejection`` to non-coroutine values,
# so no unawaited coroutines should be produced — but the filter is kept
# as a belt-and-braces to keep the test output clean if a future refactor
# re-introduces an awaitable intermediate.
warnings.filterwarnings(
    "ignore",
    message="coroutine '.*' was never awaited",
    category=RuntimeWarning,
)
