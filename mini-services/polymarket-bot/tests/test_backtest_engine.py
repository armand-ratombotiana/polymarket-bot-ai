"""
tests/test_backtest_engine.py — Unit tests for
``backtesting/engine.py::run_realistic_backtest``.

Scope: pure-Python verification of the realistic-backtest public surface
delivered by T4 (God Mode §78). The fixture module calls the engine once
with a deterministic strategy string (``"mm"`` market-maker archetype)
over a 9-day window at 50 bps base slippage; the engine internally seeds
an ``np.random.RandomState`` from the strategy name's hash, so output is
reproducible run-to-run for the same strategy string within a single
Python process.

Six contract requirements are asserted (one test each, in spec order):

  1. ``run_realistic_backtest`` returns a dict with exactly the four
     documented top-level keys: ``trades``, ``equity_curve``,
     ``metrics``, ``look_ahead_bias``.
  2. ``metrics`` contains ``win_rate`` / ``sharpe`` / ``max_drawdown`` /
     ``profit_factor`` (all numeric).
  3. ``look_ahead_bias`` contains ``total_violations`` (int ≥ 0) and the
     matching ``violations`` list.
  4. ``equity_curve`` is a non-empty list of per-step snapshots.
  5. Every trade carries an entry price (realized fill
     ``avg_fill_price``) and an exit price (binary-market settlement
     ``actual_outcome`` ∈ {0.0, 1.0}).
  6. Slippage is applied: at least one trade's realized fill price
     differs from its decision-time signal price (``decision_mid``).

Two bonus tests strengthen (2) and (6):
  - ``win_rate`` lies in the unit interval [0, 1].
  - Mean |fill − signal| gap grows monotonically with ``slippage_bps``
    (the RNG seed is identical for two same-strategy backtests within
    one process, so trade sequences match and only the slippage
    coefficient varies — a clean A/B comparison).

Synchronous — no event loop, no DB I/O, no network. No
``pytestmark = pytest.mark.asyncio`` is needed (the engine is fully
synchronous).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Defensive: ``tests/conftest.py`` already does this for the full suite,
# but a sibling test file invoked directly
# (``python -m pytest tests/test_backtest_engine.py``) should still boot
# hermetic to ``/tmp`` rather than clobber any real persisted state in
# the repo's ``data/`` directory. ``setdefault`` lets the conftest's
# redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_backtest_tests")
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
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``core.*``, …) regardless of the cwd pytest was
# launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from backtesting.engine import run_realistic_backtest  # noqa: E402


# ── Shared fixture: a single deterministic realistic backtest ─────────────
# Strategy ``"mm"`` (market-maker archetype, ``trade_frequency=0.80``)
# over 9 days = 216 hourly steps yields ~150-180 trades — comfortably
# above the 30-trade threshold that activates the LE_03 (unrealistic
# win-rate) and LE_06 (perfect-calibration) aggregate look-ahead checks,
# so the ``look_ahead_bias`` block is exercised through its full code
# path. ``module`` scope: the engine is deterministic for a given
# strategy string within one Python process, so computing the result
# once and reusing across the eight read-only assertions below is both
# fast and reproducible.
@pytest.fixture(scope="module")
def backtest_result() -> dict:
    return run_realistic_backtest(
        strategy="mm",
        start_date="2025-01-01",
        end_date="2025-01-10",
        capital=1000.0,
        slippage_bps=50.0,
    )


# ── (1) Return shape: four top-level keys ──────────────────────────────────
def test_backtest_returns_four_top_level_keys(backtest_result: dict) -> None:
    """``run_realistic_backtest`` must return a dict with exactly the
    four documented top-level keys: ``trades``, ``equity_curve``,
    ``metrics``, and ``look_ahead_bias``. (U7 contract requirement (1).)"""
    result = backtest_result
    assert isinstance(result, dict), f"expected dict, got {type(result).__name__}"
    assert set(result.keys()) == {
        "trades",
        "equity_curve",
        "metrics",
        "look_ahead_bias",
    }, f"unexpected top-level keys: {sorted(result.keys())}"


# ── (2) Metrics block exposes the four institutional performance stats ────
def test_metrics_block_has_required_fields(backtest_result: dict) -> None:
    """``metrics`` must contain ``win_rate``, ``sharpe``,
    ``max_drawdown``, and ``profit_factor`` — all numeric.
    (U7 contract requirement (2).)"""
    metrics = backtest_result["metrics"]
    assert isinstance(metrics, dict), f"metrics is not a dict: {type(metrics).__name__}"
    for key in ("win_rate", "sharpe", "max_drawdown", "profit_factor"):
        assert key in metrics, f"missing required metric: {key}"
        value = metrics[key]
        assert isinstance(value, (int, float)), (
            f"metric {key!r} must be numeric, got {type(value).__name__}: {value!r}"
        )
        # NaN / Inf would propagate into dashboards as garbage — reject.
        import math as _math
        assert _math.isfinite(value), f"metric {key!r} is non-finite: {value!r}"


def test_metrics_win_rate_in_unit_interval(backtest_result: dict) -> None:
    """``win_rate`` is a probability ∈ [0, 1]. Strengthens requirement (2)
    with a sanity bound on the metric's value domain."""
    wr = backtest_result["metrics"]["win_rate"]
    assert 0.0 <= wr <= 1.0, f"win_rate out of [0, 1]: {wr}"


def test_metrics_max_drawdown_non_negative(backtest_result: dict) -> None:
    """``max_drawdown`` is reported as a percentage of peak equity and
    must be non-negative (a drawdown can be zero but never negative)."""
    mdd = backtest_result["metrics"]["max_drawdown"]
    assert mdd >= 0.0, f"max_drawdown must be ≥ 0, got {mdd}"


# ── (3) look_ahead_bias block exposes total_violations ───────────────────
def test_look_ahead_bias_has_total_violations(backtest_result: dict) -> None:
    """``look_ahead_bias`` must contain ``total_violations`` (int ≥ 0)
    and the matching per-violation ``violations`` list.
    (U7 contract requirement (3).)"""
    lah = backtest_result["look_ahead_bias"]
    assert isinstance(lah, dict), f"look_ahead_bias is not a dict: {type(lah).__name__}"
    assert "total_violations" in lah, (
        f"look_ahead_bias missing 'total_violations'; got {sorted(lah.keys())}"
    )
    tv = lah["total_violations"]
    assert isinstance(tv, int), (
        f"total_violations must be int, got {type(tv).__name__}: {tv!r}"
    )
    # bool is a subclass of int — exclude it explicitly so a future bug
    # that returns ``True`` instead of ``1`` is caught.
    assert not isinstance(tv, bool), "total_violations must be int, not bool"
    assert tv >= 0, f"total_violations must be ≥ 0, got {tv}"
    # The violations list must be present and length-consistent with
    # ``total_violations``.
    assert "violations" in lah, "look_ahead_bias missing 'violations' list"
    violations = lah["violations"]
    assert isinstance(violations, list), (
        f"violations must be a list, got {type(violations).__name__}"
    )
    assert len(violations) == tv, (
        f"len(violations)={len(violations)} != total_violations={tv}"
    )


# ── (4) equity_curve is non-empty ──────────────────────────────────────────
def test_equity_curve_non_empty(backtest_result: dict) -> None:
    """``equity_curve`` must be a non-empty list of per-step equity
    snapshots. The engine seeds it with one initial point at ``step=0``
    and appends every 6 steps thereafter, so any backtest over ≥1 day
    yields multiple points. (U7 contract requirement (4).)"""
    ec = backtest_result["equity_curve"]
    assert isinstance(ec, list), f"equity_curve is not a list: {type(ec).__name__}"
    assert len(ec) > 0, "equity_curve is empty — engine failed to seed initial point"
    # Each snapshot carries step / ts / equity / drawdown (the documented
    # per-point shape). Spot-check the first and last points so a future
    # regression that drops a field is caught immediately.
    for idx in (0, len(ec) - 1):
        pt = ec[idx]
        assert isinstance(pt, dict), f"equity_curve[{idx}] is not a dict: {pt!r}"
        for field in ("step", "ts", "equity", "drawdown"):
            assert field in pt, f"equity_curve[{idx}] missing field {field!r}: {pt!r}"
        assert isinstance(pt["equity"], (int, float)), (
            f"equity_curve[{idx}].equity must be numeric: {pt['equity']!r}"
        )
        assert isinstance(pt["drawdown"], (int, float)), (
            f"equity_curve[{idx}].drawdown must be numeric: {pt['drawdown']!r}"
        )


# ── (5) Trades carry entry + exit prices ───────────────────────────────────
def test_trades_have_entry_and_exit_prices(backtest_result: dict) -> None:
    """Every trade must carry an entry price (the realized fill price
    ``avg_fill_price`` — what the strategy paid to acquire shares) and an
    exit price (the binary-market settlement ``actual_outcome`` ∈
    {0.0, 1.0} — $1.00 per share on a win, $0.00 on a loss). Both must
    be valid floating-point values within the binary-market price range
    [0.0, 1.0]. (U7 contract requirement (5).)"""
    trades = backtest_result["trades"]
    assert isinstance(trades, list), f"trades is not a list: {type(trades).__name__}"
    assert len(trades) > 0, (
        "backtest produced zero trades — fixture is degenerate; widen the "
        "date window or pick a higher-frequency archetype"
    )
    for i, t in enumerate(trades):
        # Entry price = realized fill price (what was paid per share).
        assert "avg_fill_price" in t, f"trades[{i}] missing avg_fill_price: {t!r}"
        entry = t["avg_fill_price"]
        assert isinstance(entry, (int, float)) and not isinstance(entry, bool), (
            f"trades[{i}].avg_fill_price must be numeric: {entry!r}"
        )
        assert 0.0 <= entry <= 1.0, (
            f"trades[{i}].avg_fill_price out of [0, 1]: {entry}"
        )
        # Exit price = binary settlement ($1.00 win / $0.00 loss).
        assert "actual_outcome" in t, f"trades[{i}] missing actual_outcome: {t!r}"
        exit_price = t["actual_outcome"]
        assert exit_price in (0.0, 1.0), (
            f"trades[{i}].actual_outcome not a binary settlement (0.0/1.0): {exit_price}"
        )


# ── (6) Slippage is applied (fill price != signal price) ─────────────────
def test_slippage_applied_fill_differs_from_signal(backtest_result: dict) -> None:
    """The realized fill price must differ from the decision-time signal
    price for at least one trade — proof that the synthetic order-book
    spread + execution-delay drift + square-root market-impact slippage
    is actually applied to fills rather than the strategy filling at the
    decision-time mid. (U7 contract requirement (6).)

    Signal price = ``decision_mid`` (the mid the strategy observed at
    decision time). Fill price = ``avg_fill_price`` (the volume-weighted
    average ask walked through the realized post-delay book, plus impact).

    Empirically EVERY trade differs — the spread is always ≥ 2 bps and
    impact is always ≥ 0 — so the stronger "all trades differ" invariant
    also holds; we assert "at least one" to match the task spec literally
    and stay robust to any future degenerate-fill edge cases.
    """
    trades = backtest_result["trades"]
    assert len(trades) > 0, "no trades produced — cannot verify slippage"
    # Sanity: every trade carries both the signal and fill price fields
    # (otherwise the comparison below would silently fall back to the
    # default-None branch and the assertion would be meaningless).
    for i, t in enumerate(trades):
        assert "decision_mid" in t, f"trades[{i}] missing decision_mid: {t!r}"
        assert "avg_fill_price" in t, f"trades[{i}] missing avg_fill_price: {t!r}"

    differing = [t for t in trades if t["avg_fill_price"] != t["decision_mid"]]
    assert len(differing) > 0, (
        "slippage not applied: avg_fill_price == decision_mid for every trade "
        f"({len(trades)} trades inspected)"
    )


def test_slippage_gap_grows_monotonically_with_bps() -> None:
    """Higher ``slippage_bps`` → larger mean |fill − signal| gap.

    The engine seeds its internal RNG from ``hash(strategy_name)``, so
    two backtests with the same strategy string within one Python
    process produce IDENTICAL trade sequences (same decision_mids, same
    p_models, same outcomes) — only the slippage coefficient varies.
    This makes the A/B comparison a clean isolation of the slippage
    model's response to its sole tunable parameter.

    Strengthens requirement (6) by confirming the slippage knob scales
    in the expected direction (not just that slippage is non-zero).
    """
    common = {
        "strategy": "mm",
        "start_date": "2025-01-01",
        "end_date": "2025-01-04",
        "capital": 1000.0,
    }
    low_slip = run_realistic_backtest(slippage_bps=5.0, **common)
    high_slip = run_realistic_backtest(slippage_bps=200.0, **common)

    def mean_abs_gap(res: dict) -> float:
        gaps = [abs(t["avg_fill_price"] - t["decision_mid"]) for t in res["trades"]]
        return sum(gaps) / len(gaps) if gaps else 0.0

    # Both runs share the same RNG seed → same number of trades; guard
    # against a zero-trade degenerate case before the comparison.
    assert len(low_slip["trades"]) > 0, "low-slippage backtest produced no trades"
    assert len(high_slip["trades"]) > 0, "high-slippage backtest produced no trades"

    low_gap = mean_abs_gap(low_slip)
    high_gap = mean_abs_gap(high_slip)
    assert high_gap > low_gap, (
        f"slippage gap did not grow with bps: low(5bps)={low_gap:.6f} "
        f"vs high(200bps)={high_gap:.6f}"
    )
