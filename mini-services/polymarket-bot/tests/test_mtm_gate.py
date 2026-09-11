"""
tests/test_mtm_gate.py — W18-6 P0-C06 fix: MTM risk gate fails closed.

The mark-to-market risk gate at section 6e of
``risk/manager.py::InstitutionalRiskEngine._check_order_impl`` re-checks
the $25 total-exposure cap on a mark-to-market basis so unrealized gains
cannot silently widen true risk past the cost-basis cap enforced at
section 5. The W17-8 P0-C06 assessment found that the gate's
``try: ... except: pass`` block was silently failing OPEN (allowing every
order through whenever the MTM computation raised) — a broken price
feed, a broken MTM module, or a simple type error would let orders pass
with NO mark-to-market supervision, the exact opposite of the gate's
purpose. This file pins the W18-6 fail-closed contract end-to-end.

Coverage map (5 test cases, mirroring the task spec):

  (1) ``test_mtm_gate_passes_when_within_threshold``
      — Fresh store (no positions): MTM total = $0; a small paper BUY
        passes the $25 cap.
  (2) ``test_mtm_gate_blocks_when_mtm_exceeds_threshold``
      — One position marked at $30 (60 shares × $0.50 mid, cost basis
        $18); a $1 BUY on a DIFFERENT token is rejected at the MTM
        gate with the canonical "Mark-to-market exposure" message.
  (3) ``test_mtm_gate_fails_closed_when_computation_raises``
      — ``compute_mark_to_market_exposure`` monkeypatched to raise
        ``RuntimeError``; the gate returns ``(False, ...)`` and emits
        the metric + alert + ERROR log.
  (4) ``test_mtm_gate_fails_closed_when_price_data_missing``
      — ``compute_mark_to_market_exposure`` monkeypatched to raise
        ``ValueError("missing price data for token X")``; same
        fail-closed contract — the exception type does NOT matter.
  (5) ``test_mtm_gate_logging_on_fail_closed``
      — caplog assertion: the fail-closed path logs at ERROR with the
        canonical "MTM gate FAILED CLOSED" prefix and the
        "RECOMMENDATION" string so a tail of the bot log surfaces
        exactly what to investigate (order_books / MTM module).

Test isolation: relies on the autouse ``_reset_store_factory_defaults``
fixture from ``tests/conftest.py`` to reset the global ``store`` and
``risk_manager`` singletons between tests. Each test additionally
clears ``store.order_books`` / ``store.market_slugs`` / positions at
entry so a prior test's MTM-input state cannot perturb the path under
test. Module-level env redirects (the same pattern as
``tests/test_risk_manager.py``) ensure every persisted-state path
points at ``/tmp`` and the trading mode is forced to paper.
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
_TMP_ROOT = Path("/tmp/mtm_gate_tests")
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
    # Force paper mode + live disabled so the shadow / live-trading gates
    # inside ``check_order`` don't short-circuit before the MTM gate.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, str(_val))

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``risk.*``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_store import (  # noqa: E402
    BANKROLL_BASELINE,
    Order,
    OrderBook,
    PriceLevel,
    Position,
    Side,
    store,
)
from core.safety import clear_kill_switch  # noqa: E402
from risk.manager import OPERATING_CAPITAL, risk_manager  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this module.
pytestmark = pytest.mark.asyncio


# ── Fixture: reset shared state before every test ──────────────────────────
@pytest.fixture(autouse=True)
def reset_mtm_test_state():
    """Reset the global ``store`` and ``risk_manager`` to a clean baseline.

    Mirrors ``reset_risk_and_store_state`` in ``tests/test_risk_manager.py``
    so a prior test's positions / order_books / kill-switch / cooldown state
    cannot leak into the next. The autouse ``_reset_store_factory_defaults``
    fixture from ``tests/conftest.py`` ALSO runs, but is intentionally
    additive (the per-module fixture is stricter about clearing the
    ``order_books`` / ``market_slugs`` maps that the MTM gate reads from).
    """
    _clear_kill_switch_safe()
    _reset_store_to_baseline()
    _reset_risk_engine_state()

    yield  # ── test runs ──

    _clear_kill_switch_safe()


def _clear_kill_switch_safe() -> None:
    """Remove the durable kill-switch marker file (best-effort)."""
    try:
        clear_kill_switch()
    except OSError:
        # Belt-and-braces: direct unlink if clear_kill_switch raises on a
        # read-only /tmp.
        for p in (
            Path(os.environ.get("KILL_SWITCH_PATH", "/tmp/mtm_kill_switch")),
            Path(os.environ.get("KILL_SWITCH_REASON_PATH", "/tmp/mtm_kill_switch.reason")),
        ):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _reset_store_to_baseline() -> None:
    """Restore ``store`` to a freshly-bootstrapped baseline.

    Clears every container the MTM gate reads from (``positions``,
    ``order_books``, ``market_slugs``, ``open_orders``, ``trades``,
    ``event_log``) and resets every scalar the surrounding risk gates
    consult (``daily_pnl`` / ``weekly_pnl`` / ``peak_equity`` / etc.).
    """
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
    """Restore ``risk_manager`` to its post-ctor state."""
    risk_manager.observation_only = False
    risk_manager.observation_reason = ""
    risk_manager._strategy_cooldowns.clear()


# ── Helpers ────────────────────────────────────────────────────────────────
def _paper_buy_order(
    *,
    strategy: str = "mtm_test_strategy",
    price: float = 0.50,
    size: float = 2.0,
    token_id: str = "tok-new",
    order_id: str | None = None,
) -> Order:
    """Build a minimal paper BUY order that passes every ``check_order`` gate
    NOT under test, so the MTM gate is the only path that can reject.

    Defaults: cost = price * size = 0.50 * 2.0 = $1.00 — well under the
    $3.00 per-market cap, the $5.00 absolute cap, the $15.00 per-strategy
    cap, the $8.00 correlated-group cap, the $25.00 cost-basis total-open-
    risk cap (section 5), the $10.00 pending-order-capital cap, and the
    $60.00 deployable ceiling.
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


def _add_position_with_book(
    *,
    token_id: str,
    yes_shares: float,
    avg_entry_price: float,
    mid: float,
    strategy: str = "other_strategy",
) -> None:
    """Stage a position + matching order book so the MTM gate sees a marked
    value of ``yes_shares * mid`` for that token.

    The position's ``total_invested`` is set to ``yes_shares *
    avg_entry_price`` (cost basis); the order book's best_bid / best_ask
    straddle ``mid`` so ``book.mid`` resolves to ``mid``.

    Used by the MTM-blocks-on-exceeded-threshold test to construct a
    position whose mark-to-market value is large enough to breach the
    $25 cap WITHOUT first tripping the section-5 cost-basis $25 cap
    (which would short-circuit before the MTM gate).
    """
    pos = Position(
        token_id=token_id,
        market_slug="",
        yes_shares=yes_shares,
        no_shares=0.0,
        avg_entry_price=avg_entry_price,
        total_invested=yes_shares * avg_entry_price,
        realised_pnl=0.0,
        strategy=strategy,
    )
    store.positions[token_id] = pos

    # Build a book whose mid resolves to ``mid`` (best_bid + best_ask) / 2.
    spread = 0.02
    book = OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=mid - spread / 2, size=100.0)],
        asks=[PriceLevel(price=mid + spread / 2, size=100.0)],
    )
    store.order_books[token_id] = book


# ── (1) MTM gate passes when MTM is within threshold ───────────────────────
async def test_mtm_gate_passes_when_within_threshold():
    """Fresh store, no positions: MTM total = $0; a small paper BUY must
    pass the $25 MTM cap (and every other gate).

    Verifies the W18-6 fix did NOT regress the happy path — the gate
    still allows legitimate trades when MTM exposure is well under the
    $25 ceiling. The legacy implementation appeared to "pass" this test
    too, but only because the broken ``from core.portfolio import ...``
    import raised ImportError, was swallowed by the bare ``except:``,
    and the gate was skipped entirely — so the assertion was vacuously
    true. Under the W18-6 fix the MTM function actually runs and
    returns ``total_exposure_mark = 0.0``, so this test now exercises
    the gate's happy path for real.
    """
    # Sanity: no positions → MTM total must be zero.
    assert len(store.positions) == 0
    assert len(store.order_books) == 0

    allowed, reason = await risk_manager.check_order(_paper_buy_order())

    assert allowed is True, (
        f"Small paper BUY on a fresh store must be allowed; "
        f"got allowed={allowed}, reason={reason!r}"
    )
    assert reason == "OK"


# ── (2) MTM gate blocks when MTM exceeds threshold ─────────────────────────
async def test_mtm_gate_blocks_when_mtm_exceeds_threshold():
    """A position marked at $30 (60 shares × $0.50 mid, cost basis $18)
    plus a $1 BUY on a DIFFERENT token must be rejected at the MTM
    gate.

    Setup carefully avoids tripping earlier gates:

      * Cost-basis exposure ($18) + order cost ($1) = $19 < $25 (section 5
        cost-basis cap) and < $60 (section 4 deployable cap).
      * The new order is on a DIFFERENT token than the existing position
        so section 6 per-market / absolute / normal caps (which all key
        on ``order.token_id``) see ``market_exp = 0`` and pass.
      * The new order's strategy is DIFFERENT from the existing
        position's strategy so section 6c per-strategy cap sees
        ``strat_exp = 0`` and passes.
      * ``store.market_slugs`` is empty so section 6d correlated-group
        cap is skipped (slug resolves to "").
      * No drawdown: ``peak_equity = OPERATING_CAPITAL = $100`` so the
        MDD gate does not trip.
      * ``daily_pnl = 0`` so the daily / weekly loss stops do not trip.

    The MTM gate is therefore the FIRST gate that can reject — the
    assertion pins the W18-6 fail-closed fix's happy BLOCKING path
    (the gate actually runs and rejects when MTM total exceeds $25,
    rather than silently passing as the legacy implementation did).
    """
    # Baseline sanity: peak at operating capital, daily_pnl flat.
    store.peak_equity = float(OPERATING_CAPITAL)  # $100
    store.daily_pnl = 0.0

    # Stage a position whose mark-to-market value ($30) exceeds the
    # $25 cap, while its cost-basis exposure ($18) stays under the
    # section-5 cost-basis $25 cap so the MTM gate is the first to
    # reject.
    _add_position_with_book(
        token_id="tok-existing",
        yes_shares=60.0,
        avg_entry_price=0.30,  # cost basis = $18
        mid=0.50,               # marked value = $30
        strategy="other_strategy",
    )

    # Confirm the test's MTM math is right.
    from core.portfolio_mark_to_market import compute_mark_to_market_exposure
    mtm = compute_mark_to_market_exposure()
    assert mtm["total_exposure_mark"] == 30.0, (
        f"Expected $30 marked exposure from the staged position; got "
        f"${mtm['total_exposure_mark']}"
    )

    # Order on a DIFFERENT token + DIFFERENT strategy so per-market /
    # per-strategy / correlated-group caps don't short-circuit before
    # the MTM gate.
    order = _paper_buy_order(
        token_id="tok-new",
        strategy="mtm_test_strategy",
        price=0.50,
        size=2.0,  # cost = $1.00
    )

    allowed, reason = await risk_manager.check_order(order)

    assert allowed is False, (
        "Expected MTM gate to reject the order (marked exposure $30 + "
        "order $1 = $31 > $25 cap); got allowed=True"
    )
    assert "Mark-to-market" in reason, (
        f"Rejection reason should mention 'Mark-to-market'; got {reason!r}"
    )
    assert "$25.00" in reason, (
        f"Rejection reason should mention the $25.00 cap; got {reason!r}"
    )
    # The MTM gate does NOT arm the kill switch — it's a per-order
    # rejection, not a system-wide halt. The kill switch is reserved
    # for daily-loss / weekly-loss / MDD breaches.
    assert store.kill_switch_active is False


# ── (3) MTM gate fails CLOSED when computation raises ───────────────────────
async def test_mtm_gate_fails_closed_when_computation_raises(monkeypatch, caplog):
    """When ``compute_mark_to_market_exposure`` raises ANY exception, the
    gate must FAIL CLOSED: return ``(False, ...)`` with the canonical
    "MTM risk gate failed closed" reason, increment the
    ``mtm_gate_failures_total`` Prometheus counter, and fire a CRITICAL
    alert via ``alert_engine.fire_alert``.

    The legacy ``except: pass`` would have returned ``(True, "OK")``
    (after passing through sections 7+12), letting the order through
    with NO MTM supervision — the exact P0-C06 fail-open bug.
    """
    # Sanity: baseline state — peak at operating capital, no positions.
    store.peak_equity = float(OPERATING_CAPITAL)
    store.daily_pnl = 0.0
    assert len(store.positions) == 0

    # Monkeypatch the MTM function to raise a generic RuntimeError.
    def _raise_runtime():
        raise RuntimeError("simulated MTM module failure")

    monkeypatch.setattr(
        "core.portfolio_mark_to_market.compute_mark_to_market_exposure",
        _raise_runtime,
    )

    # Intercept the Prometheus counter increment so we can assert it fired.
    inc_calls: list[bool] = []
    from core.prometheus_metrics import mtm_gate_failures_total as _metric

    def _fake_inc():
        inc_calls.append(True)

    monkeypatch.setattr(_metric, "inc", _fake_inc)

    # Intercept alert_engine.fire_alert so we can assert the alert's
    # severity + payload without touching the real SQLite store.
    fired_alerts: list = []
    from core.alerting import alert_engine as _alert_engine

    def _fake_fire(alert):
        fired_alerts.append(alert)
        return True

    monkeypatch.setattr(_alert_engine, "fire_alert", _fake_fire)

    # Capture ERROR-level log records.
    import logging as _logging
    with caplog.at_level(_logging.ERROR, logger="risk.manager"):
        allowed, reason = await risk_manager.check_order(_paper_buy_order())

    # ── Assert: order rejected (FAIL CLOSED). ────────────────────────────
    assert allowed is False, (
        "MTM gate must FAIL CLOSED when the MTM computation raises — "
        "expected allowed=False, got True"
    )
    assert "MTM risk gate failed closed" in reason, (
        f"Rejection reason should mention 'MTM risk gate failed closed'; "
        f"got {reason!r}"
    )
    assert "RuntimeError" in reason, (
        f"Rejection reason should embed the exception type; got {reason!r}"
    )

    # ── Assert: Prometheus counter incremented. ─────────────────────────
    assert inc_calls == [True], (
        f"mtm_gate_failures_total.inc() should have been called exactly "
        f"once; got calls={inc_calls!r}"
    )

    # ── Assert: CRITICAL alert fired via alert_engine.fire_alert. ───────
    assert len(fired_alerts) == 1, (
        f"alert_engine.fire_alert should have been called exactly once; "
        f"got calls={fired_alerts!r}"
    )
    alert = fired_alerts[0]
    assert alert.category == "risk"
    assert alert.name == "mtm_gate_fail_closed"
    assert alert.severity == "critical", (
        f"Alert severity must be CRITICAL for an MTM fail-closed event; "
        f"got {alert.severity!r}"
    )
    assert "MTM risk gate failed closed" in alert.message
    assert "RuntimeError" in alert.message or "RuntimeError" in str(alert.metadata)
    assert alert.metadata.get("exception") == "RuntimeError('simulated MTM module failure')"

    # ── Assert: store event log captured the halt. ─────────────────────
    recent = await store.get_recent_events(n=20)
    mtm_event = next(
        (e for e in recent if "MTM gate FAILED CLOSED" in e), None
    )
    assert mtm_event is not None, (
        f"store.log_event should have recorded the MTM fail-closed; "
        f"recent events: {recent!r}"
    )
    assert "RuntimeError" in mtm_event

    # ── Assert: ERROR log emitted with canonical prefix + RECOMMENDATION.
    error_records = [r for r in caplog.records if r.levelno == _logging.ERROR]
    assert len(error_records) >= 1, (
        f"At least one ERROR log record should have been emitted by the "
        f"fail-closed path; got records={caplog.records!r}"
    )
    mtm_log = next(
        (r for r in error_records if "MTM gate FAILED CLOSED" in r.getMessage()),
        None,
    )
    assert mtm_log is not None, (
        "An ERROR log record with 'MTM gate FAILED CLOSED' must be emitted"
    )
    assert "RECOMMENDATION" in mtm_log.getMessage(), (
        "The ERROR log must include a 'RECOMMENDATION' string so operators "
        "see what to investigate (order_books / MTM module / positions)."
    )


# ── (4) MTM gate fails CLOSED when price data is missing ───────────────────
async def test_mtm_gate_fails_closed_when_price_data_missing(monkeypatch, caplog):
    """Same fail-closed contract as test (3), but the simulated exception
    models a "missing price data" failure (``ValueError`` raised by a
    hypothetical broken-price-feed path).

    Pins that the W18-6 fail-closed contract is exception-type
    agnostic: the gate does NOT special-case "missing data" vs "broken
    module" vs "type error" — ANY exception means the MTM computation
    could not produce a trustworthy number, so the gate MUST block.
    """
    store.peak_equity = float(OPERATING_CAPITAL)
    store.daily_pnl = 0.0

    def _raise_missing_data():
        raise ValueError("missing price data for token tok-existing")

    monkeypatch.setattr(
        "core.portfolio_mark_to_market.compute_mark_to_market_exposure",
        _raise_missing_data,
    )

    # Re-use the same interceptors as test (3) to assert the metric +
    # alert fired.
    inc_calls: list[bool] = []
    from core.prometheus_metrics import mtm_gate_failures_total as _metric

    monkeypatch.setattr(_metric, "inc", lambda: inc_calls.append(True))

    fired_alerts: list = []
    from core.alerting import alert_engine as _alert_engine

    monkeypatch.setattr(_alert_engine, "fire_alert", lambda a: fired_alerts.append(a) or True)

    import logging as _logging
    with caplog.at_level(_logging.ERROR, logger="risk.manager"):
        allowed, reason = await risk_manager.check_order(_paper_buy_order())

    # ── Assert: order rejected (FAIL CLOSED) for a ValueError too. ──────
    assert allowed is False, (
        "MTM gate must FAIL CLOSED for a ValueError (missing price data) "
        "too, not just for RuntimeError"
    )
    assert "MTM risk gate failed closed" in reason
    assert "ValueError" in reason, (
        f"Rejection reason should embed the exception type (ValueError); "
        f"got {reason!r}"
    )
    assert "missing price data" in reason, (
        f"Rejection reason should embed the exception message; got {reason!r}"
    )

    # ── Assert: metric + alert + log fired (same contract as test 3). ──
    assert inc_calls == [True]
    assert len(fired_alerts) == 1
    alert = fired_alerts[0]
    assert alert.severity == "critical"
    assert "missing price data" in alert.message or "missing price data" in str(alert.metadata)
    assert alert.metadata.get("exception") == (
        "ValueError('missing price data for token tok-existing')"
    )

    recent = await store.get_recent_events(n=20)
    assert any("MTM gate FAILED CLOSED" in e for e in recent)

    error_records = [
        r for r in caplog.records
        if r.levelno == _logging.ERROR and "MTM gate FAILED CLOSED" in r.getMessage()
    ]
    assert len(error_records) >= 1


# ── (5) Test logging on fail-closed ─────────────────────────────────────────
async def test_mtm_gate_logging_on_fail_closed(monkeypatch, caplog):
    """The fail-closed path must emit an ERROR log record whose message
    contains:

      * the canonical prefix "MTM gate FAILED CLOSED" so a tail of the
        bot log surfaces the halt at a glance;
      * the repr of the exception that caused the failure;
      * the literal "RECOMMENDATION" string so operators see what to
        investigate (order_books / MTM module / position integrity)
        without digging into source code.

    Pins the operator-visibility contract: a fail-closed event with NO
    log is functionally identical to a silent skip from the operator's
    perspective — the order is rejected but nobody knows WHY. The log
    message is the bridge between "the gate blocked the trade" and
    "the operator must repair the price feed before resuming".
    """
    store.peak_equity = float(OPERATING_CAPITAL)
    store.daily_pnl = 0.0

    def _raise_type_error():
        raise TypeError("bad operand type for Decimal: 'NoneType'")

    monkeypatch.setattr(
        "core.portfolio_mark_to_market.compute_mark_to_market_exposure",
        _raise_type_error,
    )
    # Silence the metric + alert side-effects so the test asserts ONLY
    # the log contract (the metric + alert assertions are covered by
    # tests 3 + 4 — we don't need to repeat them here).
    monkeypatch.setattr(
        "core.prometheus_metrics.mtm_gate_failures_total.inc",
        lambda: None,
    )
    from core.alerting import alert_engine as _alert_engine
    monkeypatch.setattr(_alert_engine, "fire_alert", lambda a: True)

    import logging as _logging
    with caplog.at_level(_logging.ERROR, logger="risk.manager"):
        allowed, reason = await risk_manager.check_order(_paper_buy_order())

    assert allowed is False
    assert "MTM risk gate failed closed" in reason

    # ── Assert: exactly one ERROR record was emitted by the MTM gate. ──
    mtm_error_records = [
        r for r in caplog.records
        if r.levelno == _logging.ERROR and "MTM gate FAILED CLOSED" in r.getMessage()
    ]
    assert len(mtm_error_records) == 1, (
        f"Exactly one ERROR record from the MTM gate expected; got "
        f"{len(mtm_error_records)}"
    )

    log_msg = mtm_error_records[0].getMessage()
    # Canonical prefix — operators grep for this in `tail -F`.
    assert "MTM gate FAILED CLOSED" in log_msg
    # Exception repr embedded — operators see the root cause inline.
    assert "TypeError" in log_msg, (
        f"Log message should embed the exception repr; got {log_msg!r}"
    )
    assert "bad operand type" in log_msg, (
        f"Log message should embed the exception message; got {log_msg!r}"
    )
    # Action recommendation — operators see what to investigate.
    assert "RECOMMENDATION" in log_msg, (
        "The log MUST include a RECOMMENDATION string pointing operators "
        "at the price feeds / MTM module / position integrity — without it "
        "the halt is opaque."
    )
    # Concrete pointers to the modules to check.
    assert "order_books" in log_msg, (
        "The log MUST mention order_books so operators know to check the "
        "price feed first."
    )
    assert "core.portfolio_mark_to_market" in log_msg, (
        "The log MUST mention the MTM module path so operators know where "
        "to debug the computation itself."
    )

    # The log record carries exc_info=True so the full traceback is
    # available alongside the formatted message (matches the
    # ``log.error(..., exc_info=True)`` call site in risk/manager.py).
    assert mtm_error_records[0].exc_info is not None, (
        "The ERROR log must carry exc_info=True so the full traceback is "
        "available — operators need the stack to find WHERE the MTM "
        "computation raised, not just WHAT it raised."
    )


# ── Regression guard: legacy fail-open contract is dead ────────────────────
async def test_legacy_fail_open_contract_is_dead(monkeypatch):
    """Regression guard for the legacy fail-open behavior.

    Before the W18-6 fix, the MTM gate was wrapped in
    ``try: ... except: pass`` — an exception inside the MTM computation
    was silently swallowed and the order was allowed through with NO MTM
    supervision. This test asserts that contract is DEAD: an exception
    in the MTM computation MUST result in ``allowed=False`` (the gate
    fails closed), NOT ``allowed=True`` (the legacy fail-open behavior).

    If a future refactor reintroduces the ``except: pass`` pattern (or
    any variant that swallows the exception and returns True), this
    test will fail loudly.
    """
    store.peak_equity = float(OPERATING_CAPITAL)
    store.daily_pnl = 0.0

    def _raise_anything():
        raise Exception("arbitrary failure — must trigger fail-closed")

    monkeypatch.setattr(
        "core.portfolio_mark_to_market.compute_mark_to_market_exposure",
        _raise_anything,
    )
    monkeypatch.setattr(
        "core.prometheus_metrics.mtm_gate_failures_total.inc",
        lambda: None,
    )
    from core.alerting import alert_engine as _alert_engine
    monkeypatch.setattr(_alert_engine, "fire_alert", lambda a: True)

    allowed, _reason = await risk_manager.check_order(_paper_buy_order())

    # THE REGRESSION GUARD: allowed MUST be False. The legacy
    # implementation returned True here (silently swallowing the
    # exception) — that was the P0-C06 fail-open bug.
    assert allowed is False, (
        "REGRESSION: the MTM gate returned allowed=True when the MTM "
        "computation raised — this is the exact P0-C06 fail-open bug "
        "the W18-6 fix was introduced to prevent. The except block must "
        "return False (fail-closed), not fall through to the section-7+ "
        "checks that return True."
    )
