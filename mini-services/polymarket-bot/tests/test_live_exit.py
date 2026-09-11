"""
W18-5 — Tests for live TP/SL exit submission (P0-C05 fix).

Scope
-----
Verifies that ``PositionManager.evaluate_positions`` routes TP/SL exit
orders to the correct execution venue based on ``settings.paper_trade``:

  * Paper mode (``settings.paper_trade is True``): exits route through
    ``paper_sim.create_order`` (unchanged behaviour — the simulator
    builds the local ``Order``, runs the slippage model, records the
    ORDER stage in the decision ledger).
  * Live mode (``settings.paper_trade is False``): exits route through
    ``clob_client.create_order`` — a real EIP-712 signed order is
    submitted to the Polymarket CLOB. The server response is mapped to
    a local ``Order`` added to ``store.open_orders`` so the
    ``active_exit_order_id`` tracker can cancel it later.
  * Live failure: if ``clob_client.create_order`` raises (or returns
    ``None``), ``submit_exit_order`` returns ``None``, the position
    manager logs a warning, ``active_exit_order_id`` is left unchanged,
    and ``evaluate_positions`` does NOT crash (the next loop tick will
    retry the exit if the trigger still holds).

Five tests:
  (1) TP exit calls paper_sim in paper mode.
  (2) SL exit calls paper_sim in paper mode.
  (3) TP exit calls clob_client in live mode (settings.paper_trade flipped).
  (4) SL exit calls clob_client in live mode (settings.paper_trade flipped).
  (5) Live exit failure does not crash the position manager
      (``active_exit_order_id`` stays ``None``; the loop is unbroken).

Approach
--------
The risk gate, paper simulator, and CLOB client are swapped out via
``monkeypatch.setattr`` on the module-level bindings the production code
path uses (``risk.manager.risk_manager``, ``paper.simulator.paper_sim``,
``core.execution_interface.clob_client``). ``AsyncMock`` captures call
args and assert-await counts. ``settings.paper_trade`` is flipped via
``monkeypatch.setattr(settings, "paper_trade", False)`` for live-mode
tests so the original True value is restored automatically after the
test (no state leakage into the next sibling test).

Env-var redirection and the autouse singleton-reset live in
``tests/conftest.py`` (T15); this file mirrors the env-var redirect block
purely so it remains self-contained when imported outside the pytest
runner (an IDE that doesn't load conftest first). The redirect uses
``setdefault`` so it never clobbers a path the conftest already set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/live_exit_tests")
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
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    # Force the canonical trading mode to paper + live disabled so risk-gate
    # short-circuits don't fire at the shadow / live-trading gates. Live-mode
    # tests below override ``settings.paper_trade`` in-memory.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-live-exit",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``) regardless of the cwd pytest was
# launched from. Mirrors the bootstrap pattern in every existing sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from config import settings  # noqa: E402
from core.data_store import (  # noqa: E402
    Order,
    OrderBook,
    Position,
    PriceLevel,
    Side,
    store,
)
from core.position_manager import PositionManager  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` declares ``testpaths = tests`` and
# ``asyncio_mode`` is left at the pytest-asyncio default (``strict``); the
# module-level ``pytestmark`` idiom opts every async test in without
# editing ``pytest.ini`` / ``pyproject.toml``.
pytestmark = pytest.mark.asyncio


# ── Test fixtures ────────────────────────────────────────────────────────────
_TOKEN_ID = "0xtest_live_exit_token_id_deadbeefcafe000000000000beef"


def _book(best_bid: float, best_ask: float) -> OrderBook:
    """Build a two-sided OrderBook with a single price level per side."""
    return OrderBook(
        token_id=_TOKEN_ID,
        bids=[PriceLevel(price=best_bid, size=100.0)],
        asks=[PriceLevel(price=best_ask, size=100.0)],
    )


def _seed_position(entry_price: float = 0.50, shares: float = 10.0) -> Position:
    """Seed a long Position into ``store.positions`` for ``_TOKEN_ID``.

    Default ``entry_price=0.50`` produces:
      - take_profit_price = min(0.50 * 1.25, 0.99) = 0.625
      - stop_loss_price    = max(0.50 * 0.95, 0.01) = 0.475
    """
    pos = Position(
        token_id=_TOKEN_ID,
        yes_shares=shares,
        avg_entry_price=entry_price,
    )
    store.positions[_TOKEN_ID] = pos
    return pos


def _seed_book_for_tp(book_best_bid: float = 0.69) -> OrderBook:
    """Seed an OrderBook whose mid triggers TP (mid=0.70 >= 0.625)."""
    book = _book(best_bid=book_best_bid, best_ask=book_best_bid + 0.02)
    store.order_books[_TOKEN_ID] = book
    return book


def _seed_book_for_sl(book_best_bid: float = 0.39) -> OrderBook:
    """Seed an OrderBook whose mid triggers SL (mid=0.40 <= 0.475)."""
    book = _book(best_bid=book_best_bid, best_ask=book_best_bid + 0.02)
    store.order_books[_TOKEN_ID] = book
    return book


def _mock_risk_approved():
    """Build a risk-manager mock whose ``check_order`` returns (True, "OK")."""
    return SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))


def _mock_paper_sim_returning(sentinel_order: Order):
    """Build a paper_sim mock whose ``create_order`` returns ``sentinel_order``."""
    return SimpleNamespace(
        create_order=AsyncMock(return_value=sentinel_order),
        cancel_order=AsyncMock(return_value=True),
    )


def _mock_clob_returning(resp: dict | None):
    """Build a clob_client mock whose ``create_order`` returns ``resp``."""
    return SimpleNamespace(
        create_order=AsyncMock(return_value=resp),
        cancel_order=AsyncMock(return_value=True),
    )


# ── (1) TP exit calls paper_sim in paper mode ───────────────────────────────
async def test_tp_exit_calls_paper_sim_in_paper_mode(monkeypatch):
    """In paper mode (``settings.paper_trade is True``), a TP trigger must
    route the exit order to ``paper_sim.create_order`` — never to
    ``clob_client.create_order``.

    The exit payload carries:
      - ``token_id`` of the held position,
      - ``side=Side.SELL`` (long close),
      - ``price=book.best_bid`` (MARKETABLE — crosses the spread),
      - ``size=pos.yes_shares`` (full close),
      - ``strategy="position_manager_tp"``,
      - ``decision_id`` propagated from the pre-check Order.

    The returned Order's ``order_id`` populates ``active_exit_order_id``
    so the next exit cycle can cancel the prior stale order (R1).
    """
    # Belt-and-braces: the conftest's TRADING_MODE=paper redirect must
    # have surfaced in settings.paper_trade at module-load time.
    assert settings.paper_trade is True

    # Seed position + TP-triggering book (mid=0.70 >= TP=0.625).
    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_tp(book_best_bid=0.69)

    # Sentinel Order returned by paper_sim.create_order.
    sentinel = Order(
        order_id="paper-sentinel-tp",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.69,
        size=10.0,
        strategy="position_manager_tp",
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    mock_clob = _mock_clob_returning({"orderID": "should-not-be-called"})
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    await pm.evaluate_positions()

    # paper_sim.create_order was awaited exactly once with the exit payload.
    mock_paper.create_order.assert_awaited_once()
    call_kwargs = mock_paper.create_order.await_args.kwargs
    assert call_kwargs.get("strategy") == "position_manager_tp"
    args_arg = mock_paper.create_order.await_args.args[0]
    assert args_arg.token_id == _TOKEN_ID
    assert args_arg.side == Side.SELL
    assert args_arg.price == 0.69  # best_bid (marketable)
    assert args_arg.size == 10.0

    # clob_client.create_order was NEVER awaited (paper mode short-circuit).
    mock_clob.create_order.assert_not_awaited()

    # active_exit_order_id is set to the sentinel's order_id (so the next
    # exit cycle can cancel the stale order before re-submitting — R1).
    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id == "paper-sentinel-tp"


# ── (2) SL exit calls paper_sim in paper mode ───────────────────────────────
async def test_sl_exit_calls_paper_sim_in_paper_mode(monkeypatch):
    """In paper mode, an SL trigger must route the exit order to
    ``paper_sim.create_order`` — never to ``clob_client.create_order``.

    The exit payload mirrors the TP case (SELL at best_bid for a long
    close); only the trigger condition (``mid <= stop_loss_price``) and
    the strategy attribution (``"position_manager_sl"``) differ.
    """
    assert settings.paper_trade is True

    # Seed position + SL-triggering book (mid=0.40 <= SL=0.475).
    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_sl(book_best_bid=0.39)

    sentinel = Order(
        order_id="paper-sentinel-sl",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.39,
        size=10.0,
        strategy="position_manager_sl",
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    mock_clob = _mock_clob_returning({"orderID": "should-not-be-called"})
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    await pm.evaluate_positions()

    mock_paper.create_order.assert_awaited_once()
    call_kwargs = mock_paper.create_order.await_args.kwargs
    assert call_kwargs.get("strategy") == "position_manager_sl"
    args_arg = mock_paper.create_order.await_args.args[0]
    assert args_arg.token_id == _TOKEN_ID
    assert args_arg.side == Side.SELL
    assert args_arg.price == 0.39  # best_bid (marketable)
    assert args_arg.size == 10.0

    mock_clob.create_order.assert_not_awaited()

    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id == "paper-sentinel-sl"


# ── (3) TP exit calls clob_client in live mode ──────────────────────────────
async def test_tp_exit_calls_clob_client_in_live_mode(monkeypatch):
    """In live mode (``settings.paper_trade is False``), a TP trigger must
    route the exit order to ``clob_client.create_order`` — never to
    ``paper_sim.create_order``.

    This is the P0-C05 fix's headline assertion: prior to W18-5, the
    position manager unconditionally called ``paper_sim.create_order``
    regardless of ``settings.paper_trade``, so live TP/SL exits never
    reached the exchange. With the unified execution interface, the
    ``settings.paper_trade`` branch inside ``submit_exit_order`` routes
    to the right venue.

    The CLOB response dict (``{"orderID": "..."}``) is mapped to a
    local ``Order`` whose ``order_id`` populates
    ``active_exit_order_id``.
    """
    # Flip paper_trade to False — monkeypatch restores the True value
    # after the test so no state leaks to sibling tests.
    monkeypatch.setattr(settings, "paper_trade", False)

    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_tp(book_best_bid=0.69)

    sentinel = Order(
        order_id="paper-should-not-be-called",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.69,
        size=10.0,
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    mock_clob = _mock_clob_returning({"orderID": "live-test-tp-1"})
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    await pm.evaluate_positions()

    # clob_client.create_order was awaited exactly once with the exit args.
    mock_clob.create_order.assert_awaited_once()
    args_arg = mock_clob.create_order.await_args.args[0]
    # args is an OrderArgs dataclass with token_id / price / side / size.
    assert args_arg.token_id == _TOKEN_ID
    assert args_arg.side == Side.SELL
    assert args_arg.price == 0.69  # best_bid (marketable)
    assert args_arg.size == 10.0

    # paper_sim.create_order was NEVER awaited (live mode short-circuit).
    mock_paper.create_order.assert_not_awaited()

    # active_exit_order_id is set to the CLOB-returned orderID.
    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id == "live-test-tp-1"

    # The local Order was added to store.open_orders so future cancels
    # against ``active_exit_order_id`` can find it.
    assert "live-test-tp-1" in store.open_orders
    assert store.open_orders["live-test-tp-1"].paper is False
    assert store.open_orders["live-test-tp-1"].strategy == "position_manager_tp"


# ── (4) SL exit calls clob_client in live mode ──────────────────────────────
async def test_sl_exit_calls_clob_client_in_live_mode(monkeypatch):
    """In live mode, an SL trigger must route to ``clob_client.create_order``
    (same paper/live branch as the TP case; only the trigger condition
    and strategy attribution differ).
    """
    monkeypatch.setattr(settings, "paper_trade", False)

    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_sl(book_best_bid=0.39)

    sentinel = Order(
        order_id="paper-should-not-be-called",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.39,
        size=10.0,
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    mock_clob = _mock_clob_returning({"orderID": "live-test-sl-1"})
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    await pm.evaluate_positions()

    mock_clob.create_order.assert_awaited_once()
    args_arg = mock_clob.create_order.await_args.args[0]
    assert args_arg.token_id == _TOKEN_ID
    assert args_arg.side == Side.SELL
    assert args_arg.price == 0.39  # best_bid (marketable)
    assert args_arg.size == 10.0

    mock_paper.create_order.assert_not_awaited()

    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id == "live-test-sl-1"

    assert "live-test-sl-1" in store.open_orders
    assert store.open_orders["live-test-sl-1"].paper is False
    assert store.open_orders["live-test-sl-1"].strategy == "position_manager_sl"


# ── (5) Live exit failure does not crash the position manager ───────────────
async def test_live_exit_failure_does_not_crash(monkeypatch):
    """When ``clob_client.create_order`` raises (network error, signing
    failure, ``CircuitBreakerOpenError``, ``RuntimeError`` from missing
    creds, etc.), ``submit_exit_order`` catches the exception, logs it,
    and returns ``None``. The position manager must NOT crash — the
    surrounding ``try/except`` swallows the failure, the loop continues
    to the next position / next tick, and ``active_exit_order_id`` is
    left unchanged so the next tick will retry the exit if the trigger
    still holds.
    """
    monkeypatch.setattr(settings, "paper_trade", False)

    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_tp(book_best_bid=0.69)

    # clob_client.create_order raises a RuntimeError (the same exception
    # the real ClobClient raises when ``_creds`` is None and a live order
    # is attempted — the typical live-mode-without-creds failure mode).
    mock_clob = SimpleNamespace(
        create_order=AsyncMock(side_effect=RuntimeError("Not authenticated. Call derive_api_key() first.")),
        cancel_order=AsyncMock(return_value=False),
    )
    sentinel = Order(
        order_id="paper-should-not-be-called",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.69,
        size=10.0,
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    # Must NOT raise — the surrounding try/except in evaluate_positions
    # catches the propagated None (after submit_exit_order swallows the
    # RuntimeError internally).
    await pm.evaluate_positions()

    # clob_client.create_order WAS awaited (the attempt was made and
    # failed). The execution interface caught the RuntimeError and
    # returned None to the position manager.
    mock_clob.create_order.assert_awaited_once()
    # paper_sim.create_order was NOT awaited (live mode short-circuit
    # even on failure — no fallback to paper).
    mock_paper.create_order.assert_not_awaited()

    # active_exit_order_id stays None — the prior order was never
    # successfully submitted, so the next tick's "cancel stale" branch
    # is skipped (no stale order to cancel).
    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id is None

    # Belt-and-braces: the loop is unbroken — a second evaluation with
    # the same fixture should also complete without raising (the next
    # tick retries the exit; ``mock_clob`` will raise again; the
    # try/except catches it again).
    await pm.evaluate_positions()
    assert mock_clob.create_order.await_count == 2
    assert managed.active_exit_order_id is None  # still None


# ── (6) Live exit returning None does not crash either ──────────────────────
async def test_live_exit_returning_none_does_not_crash(monkeypatch):
    """When ``clob_client.create_order`` returns ``None`` (the real
    ``ClobClient.create_order``'s signature returns ``dict | None``, with
    ``None`` indicating a 4xx/5xx HTTP rejection or signing failure),
    ``submit_exit_order`` returns ``None`` and the position manager
    handles it gracefully — logs a warning, leaves
    ``active_exit_order_id`` unchanged, and continues the loop.
    """
    monkeypatch.setattr(settings, "paper_trade", False)

    _seed_position(entry_price=0.50, shares=10.0)
    _seed_book_for_sl(book_best_bid=0.39)

    mock_clob = _mock_clob_returning(resp=None)  # CLOB rejected the order.
    sentinel = Order(
        order_id="paper-should-not-be-called",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.39,
        size=10.0,
        paper=True,
    )
    mock_paper = _mock_paper_sim_returning(sentinel)
    monkeypatch.setattr("paper.simulator.paper_sim", mock_paper)
    monkeypatch.setattr("core.execution_interface.clob_client", mock_clob)
    monkeypatch.setattr("risk.manager.risk_manager", _mock_risk_approved())

    pm = PositionManager()
    # Must NOT raise — submit_exit_order returned None, position_manager
    # logged a warning and skipped the active_exit_order_id assignment.
    await pm.evaluate_positions()

    mock_clob.create_order.assert_awaited_once()
    mock_paper.create_order.assert_not_awaited()

    managed = pm.managed_positions[_TOKEN_ID]
    assert managed.active_exit_order_id is None  # no order to track
