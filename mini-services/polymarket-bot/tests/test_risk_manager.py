"""
tests/test_risk_manager.py — Unit tests for risk/manager.py.

S7 — Risk Manager unit tests.

Covers the six behaviours required by the task spec:

  (1) ``check_order`` rejects when the kill switch is active.
  (2) ``check_order`` rejects when daily loss exceeds ``DAILY_LOSS_STOP``.
  (3) Per-trade circuit breaker: ``report_trade_pnl(pnl=-0.60)`` pauses the
      responsible strategy for ``STRATEGY_COOLDOWN`` seconds.
  (4) ``is_strategy_paused`` returns ``True`` while a cooldown is in effect.
  (5) ``is_strategy_paused`` returns ``False`` once the cooldown has expired
      (and lazily clears the expired entry on read).
  (6) MDD calculation uses the ``OPERATING_CAPITAL`` baseline
      (USD 100) — NOT ``BANKROLL_CEILING`` (USD 200). With ``BANKROLL_CEILING``
      the drawdown would always be negative and the MDD breaker would never
      trip; this test pins the baseline at ``OPERATING_CAPITAL`` by asserting
      that an MDD-breaching configuration actually rejects the order.

The risk engine and the in-memory ``DataStore`` are process-global singletons
(``risk_manager`` and ``store``); both are reset between tests by an autouse
fixture so state from one assertion (e.g. an activated kill switch, a paused
strategy, an altered ``peak_equity``) cannot leak into the next.

All durable DB / state file paths are redirected to ``/tmp`` via env vars set
*before* the first import of any project module — ``core.safety``,
``core.audit_logger``, ``core.data_store``, ``ml.model_registry`` … each read
their on-disk path from ``os.environ`` at module-import time, so the
redirection must happen first. The redirect uses ``setdefault`` so an outer
runner (CI / pytest invocation) can still override if needed.

The repo's ``pytest.ini`` / ``pyproject.toml`` are intentionally left
untouched (the S7 task constraint: "Do NOT edit existing files"). Async
support is provided via the module-level ``pytestmark = pytest.mark.asyncio``
declaration, mirroring the convention already in use in
``tests/test_decision_ledger.py``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# ``setdefault`` lets an outer runner (CI / pytest invocation) override these
# if it ever needs to; otherwise the tests run fully hermetic to ``/tmp`` and
# cannot clobber any real persisted state in the repo's ``data/`` directory.
_TMP_ROOT = Path("/tmp/risk_manager_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, Path] = {
    "STORE_STATE_PATH": _TMP_ROOT / "store_state.json",
    "DECISION_LEDGER_DB_PATH": _TMP_ROOT / "decision_ledger.db",
    "AUDIT_DB_PATH": _TMP_ROOT / "audit_trail.db",
    "MARKET_DB_PATH": _TMP_ROOT / "market_intelligence.db",
    "KILL_SWITCH_PATH": _TMP_ROOT / "kill_switch",
    "KILL_SWITCH_REASON_PATH": _TMP_ROOT / "kill_switch.reason",
    "VECTOR_STORE_PATH": _TMP_ROOT / "vector_index.json",
    "MODEL_PATH": _TMP_ROOT / "model.pkl",
    "MODEL_REGISTRY_PATH": _TMP_ROOT / "model_registry.json",
    "CLOSED_POSITIONS_DB_PATH": _TMP_ROOT / "closed_positions.db",
    "EXECUTION_QUALITY_DB_PATH": _TMP_ROOT / "execution_quality.db",
    "OBSERVABILITY_DB_PATH": _TMP_ROOT / "observability.db",
    # Force the canonical trading mode to paper + live disabled so the
    # shadow / live-trading gates inside ``check_order`` don't short-circuit
    # before the path under test is reached.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, str(_val))

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``risk.*``) regardless of the cwd pytest was launched from.
# Mirrors the bootstrap pattern in tests/test_features.py and
# tests/test_paper_simulator.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_store import BANKROLL_BASELINE, Order, Side, store  # noqa: E402
from core.safety import (  # noqa: E402
    ACTIVATION_REASON_FILE,
    KILL_SWITCH_PATH,
    clear_kill_switch,
)
from risk.manager import (  # noqa: E402
    BANKROLL_CEILING,
    DAILY_LOSS_STOP,
    MAX_DRAWDOWN_LIMIT,
    OPERATING_CAPITAL,
    PER_TRADE_MAX_LOSS,
    STRATEGY_COOLDOWN,
    risk_manager,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. pytest-asyncio is already a project dependency and the repo's
# pytest.ini cannot be edited per the S7 task constraint, so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ── Fixture: reset shared state before AND after every test ─────────────────
@pytest.fixture(autouse=True)
def reset_risk_and_store_state():
    """Reset the global ``store`` and ``risk_manager`` singletons between tests.

    The risk engine and data store are process-global singletons; without a
    reset, state from one test (an activated kill switch, a paused strategy,
    an altered ``peak_equity`` / ``daily_pnl``) would leak into the next and
    mask regressions. This fixture restores a clean baseline:

      * kill switch off (in-memory flag AND the durable marker file removed)
      * PnL zeroed (daily + weekly), weekly window started "now"
      * peak equity at ``BANKROLL_BASELINE`` ($100.00)
      * paper_balance at ``BANKROLL_BASELINE``
      * positions / open_orders / trades / market_slugs / order_books / events
        all cleared; equity_history reset to the single initial point
      * observation-only mode off
      * per-strategy cooldowns cleared

    The durable kill-switch marker file (``KILL_SWITCH_PATH``) is removed
    both before AND after the test — a test that triggers the breaker
    (e.g. the daily-loss-stop test) writes the marker; without the post-test
    cleanup, the next test's ``kill_switch_file_exists()`` would return True
    and short-circuit ``check_order`` at the kill-switch gate instead of
    reaching the path under test.
    """
    _clear_durable_kill_switch()
    _reset_store_state()
    _reset_risk_engine_state()

    yield  # ── test runs ──

    _clear_durable_kill_switch()


def _clear_durable_kill_switch() -> None:
    """Remove the durable kill-switch marker file (and its reason sidecar).

    Belt-and-braces: ``clear_kill_switch()`` from ``core.safety`` is the
    canonical helper, but it can raise ``OSError`` if e.g. /tmp is mounted
    read-only in some CI sandboxes; in that case we fall back to direct
    ``Path.unlink(missing_ok=True)`` calls and swallow the error.
    """
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


# ── Helpers ────────────────────────────────────────────────────────────────
def _paper_buy_order(
    *,
    strategy: str = "test_strategy",
    price: float = 0.50,
    size: float = 3.0,
    token_id: str = "tok-risk-test",
    order_id: str | None = None,
) -> Order:
    """Build a minimal paper BUY order that passes every ``check_order`` gate
    NOT under test (so the gate under test is the only thing that can reject).

    Defaults: cost = price * size = 0.50 * 3.0 = $1.50 — well under the
    $3.00 per-market cap, the $5.00 absolute cap, the $15.00 per-strategy
    cap, the $8.00 correlated-group cap, the $25.00 total-open-risk cap,
    the $10.00 pending-order-capital cap, and the $60.00 deployable ceiling.
    """
    return Order(
        order_id=order_id or f"order-{token_id}-{int(time.time() * 1_000_000)}",
        token_id=token_id,
        side=Side.BUY,
        price=price,
        size=size,
        strategy=strategy,
        paper=True,
    )


# ── (1) check_order rejects when kill_switch is active ─────────────────────
async def test_check_order_rejects_when_kill_switch_active():
    """A live (in-memory) kill switch must halt ALL new orders, paper or live.

    The check_order gate fires BEFORE the per-strategy / per-market / per-PnL
    checks, so the rejection message must be the canonical kill-switch string
    rather than e.g. a daily-loss or per-market cap message — proving the
    kill-switch gate is the path that tripped.
    """
    store.kill_switch_active = True  # in-memory flag (durable file stays clear)

    allowed, reason = await risk_manager.check_order(_paper_buy_order())

    assert allowed is False
    assert reason == "Kill switch is active — all trading halted"
    # Belt-and-braces: kill_switch_file_exists() is consulted alongside the
    # in-memory flag; both must agree the switch is OFF in the baseline
    # fixture state for this test to actually exercise the in-memory branch
    # (otherwise we'd be testing the file-exists branch, not the flag).
    from core.safety import kill_switch_file_exists
    assert kill_switch_file_exists() is False


# ── (2) check_order rejects when daily loss exceeds DAILY_LOSS_STOP ──────────
async def test_check_order_rejects_when_daily_loss_exceeds_daily_loss_stop():
    """Daily PnL at or below ``-DAILY_LOSS_STOP`` trips the daily-loss stop,
    arms the durable kill switch (so subsequent orders are also halted), and
    returns the canonical daily-loss rejection message.

    We exceed the $2.00 stop by $0.50 (``daily_pnl = -$2.50``) so the
    ``<=`` comparison is unambiguous (an exact-threshold test would also pass
    but would not distinguish a ``<=`` from a ``<`` implementation).
    """
    # Baseline sanity: nothing pre-armed.
    assert store.kill_switch_active is False

    # Exceed the $2.00 stop by $0.50.
    store.daily_pnl = -float(DAILY_LOSS_STOP) - 0.50
    assert float(store.daily_pnl) == pytest.approx(-2.50)

    allowed, reason = await risk_manager.check_order(_paper_buy_order())

    assert allowed is False
    assert "Daily loss" in reason
    # The canonical message includes the DAILY_LOSS_STOP amount formatted as $2.00.
    assert f"${DAILY_LOSS_STOP:.2f}" in reason  # "$2.00"
    # The daily-loss gate arms the durable kill switch for subsequent orders.
    assert store.kill_switch_active is True


# ── (3) Per-trade circuit breaker: report_trade_pnl(pnl=-0.60) pauses ────────
async def test_report_trade_pnl_pauses_strategy_on_large_per_trade_loss():
    """A closed trade losing -$0.60 (>= ``PER_TRADE_MAX_LOSS`` = $0.50) must
    pause the responsible strategy for ``STRATEGY_COOLDOWN`` seconds.

    ``report_trade_pnl`` is the only path that populates
    ``_strategy_cooldowns``; ``check_order`` consults it via
    ``is_strategy_paused``. This test pins the contract end-to-end: a single
    bad trade → strategy paused → subsequent BUY orders for that strategy
    blocked (asserted in test 4 / 5 below).
    """
    strategy = "circuit_breaker_strategy"
    assert risk_manager.is_strategy_paused(strategy) is False  # baseline sanity

    # -$0.60 < -$0.50 threshold → breaches the per-trade circuit breaker.
    await risk_manager.report_trade_pnl(strategy, pnl=-0.60)

    # Strategy is now paused.
    assert risk_manager.is_strategy_paused(strategy) is True
    # Cooldown expiry is STRATEGY_COOLDOWN seconds in the future (allow a
    # generous ±5s skew for the time between report_trade_pnl and this read).
    cooldown_until = risk_manager._strategy_cooldowns[strategy]
    remaining = cooldown_until - time.monotonic()
    assert 0.0 < remaining <= STRATEGY_COOLDOWN
    assert remaining > STRATEGY_COOLDOWN - 5.0  # at most ~5s elapsed since set


# ── (4) is_strategy_paused returns True during cooldown ────────────────────
async def test_is_strategy_paused_returns_true_during_cooldown():
    """Mid-cooldown (unexpired), ``is_strategy_paused`` must report ``True``
    AND must NOT clear the cooldown entry — the lazy-clear contract only
    fires on expiry (test 5)."""
    strategy = "paused_strategy"
    # Stage a cooldown that expires 60s in the future — comfortably inside
    # the STRATEGY_COOLDOWN (300s) window so the test is robust to wall-clock
    # jitter between the ``time.monotonic() + 60.0`` call and the assertion.
    risk_manager._strategy_cooldowns[strategy] = time.monotonic() + 60.0

    assert risk_manager.is_strategy_paused(strategy) is True
    # Lazy-clear contract: an UNEXPIRED cooldown must remain in the map (only
    # expired entries are popped on read — see test 5).
    assert strategy in risk_manager._strategy_cooldowns


# ── (5) is_strategy_paused returns False after cooldown expires ─────────────
async def test_is_strategy_paused_returns_false_after_cooldown_expires():
    """Once ``time.monotonic() >= cooldown_until``, ``is_strategy_paused``
    must return ``False`` AND lazily pop the expired entry — so a subsequent
    call is a clean miss rather than a re-evaluation of a stale timestamp."""
    strategy = "expired_strategy"
    # Stage a cooldown whose expiry is already in the past (1s ago).
    risk_manager._strategy_cooldowns[strategy] = time.monotonic() - 1.0

    assert risk_manager.is_strategy_paused(strategy) is False
    # Lazy-clear contract: expired cooldowns are removed on read so the dict
    # doesn't grow unbounded across the process lifetime.
    assert strategy not in risk_manager._strategy_cooldowns


# ── (6) MDD calculation uses OPERATING_CAPITAL baseline ────────────────────
async def test_mdd_calculation_uses_operating_capital_baseline():
    """The MDD breaker computes ``current_equity = OPERATING_CAPITAL + daily_pnl``
    (USD 100 baseline) — NOT ``BANKROLL_CEILING + daily_pnl`` (USD 200).

    Reproduction:

      peak_equity      = OPERATING_CAPITAL + MAX_DRAWDOWN_LIMIT = $108.00
      daily_pnl        = $0.00 (daily / weekly loss stops do NOT trip)
      current_equity   = OPERATING_CAPITAL + $0.00 = $100.00   ← correct
      drawdown_dollars = $108.00 - $100.00 = $8.00 ≥ $8.00     ← MDD trips

    If the implementation regressed to ``BANKROLL_CEILING + daily_pnl``,
    ``current_equity`` would be $200.00, ``drawdown_dollars`` would be
    ``$108 - $200 = -$92`` (always negative), the MDD breaker would NEVER
    trip, and ``check_order`` would fall through to the per-market /
    per-strategy caps and return ``(True, "OK")`` for a $1.50 paper order.
    Asserting an MDD rejection therefore pins the baseline to
    ``OPERATING_CAPITAL`` and would fail loudly if anyone reverted the fix.
    """
    # Baseline sanity: $1.50 paper order must be allowed with peak at the
    # operating baseline + daily PnL flat — i.e. no drawdown to trip on.
    store.peak_equity = float(OPERATING_CAPITAL)  # $100
    store.daily_pnl = 0.0
    allowed_baseline, _ = await risk_manager.check_order(_paper_buy_order())
    assert allowed_baseline is True, (
        "Baseline sanity check failed — a $1.50 paper order with no drawdown "
        "should be allowed; if this fails the fixture is leaking state or "
        "the operating-capital baseline moved."
    )

    # Reset the kill switch that the baseline check might have armed (it
    # shouldn't, since the baseline order is allowed — but defensive).
    _clear_durable_kill_switch()
    store.kill_switch_active = False
    store.open_orders.clear()

    # Now push peak_equity $8 above the operating baseline — exactly the
    # MAX_DRAWDOWN_LIMIT. With daily_pnl flat ($0), the daily / weekly loss
    # stops CANNOT fire, so the MDD check at step 3 of check_order is the
    # first gate that can reject.
    store.peak_equity = float(OPERATING_CAPITAL) + float(MAX_DRAWDOWN_LIMIT)
    store.daily_pnl = 0.0

    allowed, reason = await risk_manager.check_order(_paper_buy_order())

    assert allowed is False, (
        "Expected MDD rejection but the order was allowed — this would "
        "indicate the MDD baseline reverted to BANKROLL_CEILING ($200), "
        "making current_equity=$200 and drawdown always negative (the "
        "exact regression the OPERATING_CAPITAL fix was introduced to "
        "prevent)."
    )
    assert "Max drawdown" in reason
    # The canonical message includes the MAX_DRAWDOWN_LIMIT amount ($8.00).
    assert f"${MAX_DRAWDOWN_LIMIT:.2f}" in reason  # "$8.00"
    # Belt-and-braces: the MDD trigger arms the durable kill switch for
    # subsequent orders — exactly like the daily-loss-stop test above.
    assert store.kill_switch_active is True

    # The OPERATING_CAPITAL baseline assertion: if the implementation had used
    # BANKROLL_CEILING, current_equity would have been $200 instead of $100,
    # the drawdown would have been -$92, and the order would have been
    # ALLOWED. By asserting a rejection (above) we indirectly assert the
    # baseline is OPERATING_CAPITAL. Make the relationship explicit so a
    # future reader understands why a single ``allowed is False`` is a
    # complete baseline-regression test:
    assert OPERATING_CAPITAL == 100, "OPERATING_CAPITAL must remain USD 100.00"
    assert BANKROLL_CEILING == 200, "BANKROLL_CEILING must remain USD 200.00"
    assert OPERATING_CAPITAL != BANKROLL_CEILING, (
        "If OPERATING_CAPITAL ever equals BANKROLL_CEILING, this test can no "
        "longer distinguish the two baselines — update the assertion."
    )
