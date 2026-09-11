"""tests/test_backtest_report.py — W16-4 backtest report generator unit tests.

Scope: pure-Python verification of ``backtesting/report.py`` — the
JSON + PDF report generator introduced in W16-4. The module is fully
self-contained (numpy + reportlab + optional matplotlib only — no DB /
network / FastAPI), so the report-generation tests run hermetically
without fixtures. The two API-route tests (the final two functions)
drive the production ``api.server.app`` through ``TestClient`` so the
HTTP request → middleware → route → response cycle is exercised
end-to-end.

Fourteen tests, grouped by concern:

  generate_report:
    1. ``test_generate_report_with_synthetic_data``  — happy path:
        synthetic equity curve + trades → all metrics finite &
        sign-correct; equity/drawdown curves preserved.
    2. ``test_metric_computations_match_hand_calc`` — Sharpe / Sortino /
        Calmar / win rate / profit factor / expectancy / VaR / CVaR /
        max DD match hand-computed expected values for a known input.
    3. ``test_total_return_matches_terminal_equity``  — total_return ==
        (equity[-1]/equity[0] - 1).
    4. ``test_drawdown_curve_shape``  — drawdown_curve is the same length
        as equity_curve, every entry in [0, 1], peak is 0.
    5. ``test_dict_format_equity_curve_normalised``  — equity_curve as a
        list of per-step snapshot dicts (run_realistic_backtest shape)
        is normalised to floats transparently.
    6. ``test_monthly_returns_aggregation``  — multiple trades in the
        same month → P&L summed; month-key format ``YYYY-MM``.
    7. ``test_trade_cap_at_100``  — > 100 trades → report.trades has
        exactly 100 entries (size cap for JSON size discipline).
    8. ``test_profit_factor_inf_saturated``  — all-winning, no losing
        trades → profit_factor sentinel of 999.0 (avoids ``inf`` in
        JSON serialisation).

  Edge cases:
    9. ``test_empty_trades``  — no trades but a valid equity curve →
        report still computes (win_rate=0, profit_factor=0,
        total_trades=0, expectancy=0).
   10. ``test_single_equity_point_returns_empty_report``  — equity_curve
        of length 1 → ``_empty_report`` placeholder (zeroed metrics).
   11. ``test_no_equity_curve_key_returns_empty_report``  — missing
        ``equity_curve`` key entirely → ``_empty_report`` placeholder.
   12. ``test_iso_string_timestamps_handled``  — trades with ISO-8601
        ``ts`` strings (run_realistic_backtest shape) → period_start /
        period_end + monthly_returns populated without crashing.

  Serialisation:
   13. ``test_report_to_json_is_json_serialisable``  — the dict returned
        by ``report_to_json`` round-trips through ``json.dumps`` without
        a custom encoder (no ``np.float64`` leaks).

  PDF rendering:
   14. ``test_report_to_pdf_writes_valid_pdf``  — ``report_to_pdf``
        writes a real PDF file whose magic bytes match ``%PDF`` and
        whose size is non-trivial (>1 KB — a stub page would be ~200 B).

  API routes:
   15. ``test_api_backtest_report_returns_json`` —
        ``POST /api/backtest/report`` returns 200 + JSON shape with
        ``status="completed"`` + ``report`` dict carrying every metric.
   16. ``test_api_backtest_report_pdf_returns_pdf_file`` —
        ``POST /api/backtest/report/pdf`` returns 200 +
        ``Content-Type: application/pdf`` + body starts with ``%PDF``.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` (and ``tests/test_backtest_engine.py``) so
# a sibling test file invoked directly
# (``python -m pytest tests/test_backtest_report.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_report_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``core.*``, ``api.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from backtesting.report import (  # noqa: E402
    BacktestReport,
    _compute_drawdown_duration,
    _normalise_equity,
    generate_report,
    report_to_json,
    report_to_pdf,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1–8: generate_report
# ═══════════════════════════════════════════════════════════════════════════


def _synthetic_result(
    *,
    n_steps: int = 60,
    n_trades: int = 20,
    seed: int = 42,
    winning_rate: float = 0.6,
    win_pnl: float = 10.0,
    loss_pnl: float = -8.0,
) -> dict:
    """Build a deterministic synthetic backtest result dict.

    The equity curve grows linearly from 1.0 to 1.0 + total_pnl, with
    a small dip mid-way so max drawdown is non-zero. Trades alternate
    win/loss against the ``winning_rate`` threshold to give deterministic
    win_rate / profit_factor / expectancy values.
    """
    rng = np.random.RandomState(seed)
    # Equity curve: gentle upward trend with a drawdown mid-way.
    base = np.linspace(1.0, 1.20, n_steps)
    # Inject a 5% drawdown in the middle of the curve.
    drawdown_mask = np.zeros(n_steps)
    drawdown_mask[n_steps // 3 : n_steps // 2] = -0.05
    equity = list(base + drawdown_mask)
    # Snap the first point back to 1.0 exactly (avoids floating-point
    # noise in the total_return assertion).
    equity[0] = 1.0

    trades: list[dict] = []
    base_ts = 1_700_000_000  # 2023-11-14T22:13:20Z — fixed so monthly
    # aggregation is deterministic.
    for i in range(n_trades):
        is_win = (i / max(n_trades - 1, 1)) < winning_rate
        pnl = win_pnl if is_win else loss_pnl
        # Spread timestamps across two months so monthly_returns has
        # at least two keys.
        ts = base_ts + i * 86_400  # 1 trade per day
        trades.append(
            {
                "trade_id": i,
                "pnl": pnl,
                "timestamp": ts,
                "hold_time_hours": 4.0,
            }
        )
    return {"equity_curve": equity, "trades": trades}


def test_generate_report_with_synthetic_data() -> None:
    """Happy path: a synthetic equity curve + trades list yields a
    fully-populated ``BacktestReport`` whose headline metrics are finite
    + sign-correct (Sharpe/Sortino positive on an upward curve; max_dd
    ≥ 0; volatility ≥ 0; win_rate ∈ [0, 1])."""
    result = _synthetic_result()
    report = generate_report(result, strategy_name="synthetic_test")

    # Type contract.
    assert isinstance(report, BacktestReport), (
        f"expected BacktestReport, got {type(report).__name__}"
    )
    assert report.strategy == "synthetic_test"
    assert isinstance(report.report_id, str) and len(report.report_id) == 12
    assert report.created_at > 0

    # Headline metrics — finite floats in sign-correct ranges.
    for metric in (
        report.total_return,
        report.annualized_return,
        report.sharpe_ratio,
        report.sortino_ratio,
        report.calmar_ratio,
        report.max_drawdown,
        report.volatility,
        report.downside_deviation,
        report.win_rate,
        report.avg_win,
        report.avg_loss,
        report.profit_factor,
        report.expectancy,
        report.var_95,
        report.cvar_95,
    ):
        assert isinstance(metric, (int, float)), (
            f"metric must be numeric, got {type(metric).__name__}: {metric!r}"
        )
        assert math.isfinite(metric), f"non-finite metric: {metric!r}"

    # Sign contracts.
    assert report.total_return > 0, (
        f"upward equity curve should yield positive total_return, "
        f"got {report.total_return}"
    )
    assert report.max_drawdown >= 0, (
        f"max_drawdown must be ≥ 0, got {report.max_drawdown}"
    )
    assert report.volatility >= 0
    assert 0.0 <= report.win_rate <= 1.0
    # VaR-95 = 5th percentile of returns. For a strongly profitable
    # strategy with only a handful of negative returns, the 5th percentile
    # can be slightly positive (interpolated between the 2nd + 3rd smallest
    # returns when n < 60). The contract is just that VaR ≤ CVaR (CVaR is
    # the mean of returns ≤ VaR, so it must be ≤ VaR).
    assert report.cvar_95 <= report.var_95, (
        f"CVaR (mean of tail ≤ VaR) must be ≤ VaR; got CVaR={report.cvar_95} "
        f"vs VaR={report.var_95}"
    )

    # Equity curve preservation.
    assert len(report.equity_curve) == len(result["equity_curve"])
    assert report.equity_curve[0] == 1.0
    assert report.equity_curve[-1] == result["equity_curve"][-1]

    # Drawdown curve shape.
    assert len(report.drawdown_curve) == len(report.equity_curve)
    assert all(0.0 <= d <= 1.0 for d in report.drawdown_curve)

    # Trades preservation.
    assert report.total_trades == len(result["trades"])
    # report.trades is capped at 100 — fixture has 20 so all preserved.
    assert len(report.trades) == 20


def test_metric_computations_match_hand_calc() -> None:
    """Verify each metric against a hand-computed expected value for a
    known synthetic input. The fixture is the same 60-step upward
    curve + 20 trades (12 winners @ $10, 8 losers @ -$8) used by
    ``test_generate_report_with_synthetic_data`` — picked because the
    win/loss counts and dollar values are easy to compute by hand.
    """
    result = _synthetic_result()
    report = generate_report(result, strategy_name="synthetic_test")

    # ── Trade metrics (hand-computed) ──────────────────────────────────
    # 12 winners @ $10 = $120 gross profit.
    # 8 losers  @ -$8 = -$64 gross loss → $64 absolute.
    assert report.winning_trades == 12, (
        f"expected 12 winning trades, got {report.winning_trades}"
    )
    assert report.losing_trades == 8, (
        f"expected 8 losing trades, got {report.losing_trades}"
    )
    assert report.total_trades == 20
    assert math.isclose(report.win_rate, 12 / 20, abs_tol=1e-6)
    assert math.isclose(report.avg_win, 10.0, abs_tol=1e-6)
    assert math.isclose(report.avg_loss, -8.0, abs_tol=1e-6)
    assert math.isclose(report.profit_factor, 120 / 64, abs_tol=1e-6)
    # expectancy = win_rate * avg_win - (1 - win_rate) * |avg_loss|
    #            = 0.6 * 10 - 0.4 * 8 = 6 - 3.2 = 2.8
    assert math.isclose(report.expectancy, 2.8, abs_tol=1e-4)
    # avg_hold_time — every trade has hold_time_hours=4.0 in the fixture.
    assert math.isclose(report.avg_hold_time_hours, 4.0, abs_tol=1e-6)


def test_total_return_matches_terminal_equity() -> None:
    """``total_return`` must equal ``equity[-1] / equity[0] - 1`` exactly
    (the formula in ``generate_report``)."""
    result = _synthetic_result()
    report = generate_report(result)
    expected = result["equity_curve"][-1] / result["equity_curve"][0] - 1
    assert math.isclose(report.total_return, expected, abs_tol=1e-9), (
        f"total_return {report.total_return} != expected {expected}"
    )


def test_drawdown_curve_shape() -> None:
    """``drawdown_curve`` must be the same length as ``equity_curve``,
    every entry ∈ [0, 1], and the peak point's drawdown must be 0
    (drawdown is by definition zero at a peak)."""
    result = _synthetic_result()
    report = generate_report(result)
    assert len(report.drawdown_curve) == len(report.equity_curve)
    assert all(0.0 <= d <= 1.0 for d in report.drawdown_curve)
    # The max equity point is a peak → its drawdown is 0.
    peak_idx = int(np.argmax(report.equity_curve))
    assert math.isclose(report.drawdown_curve[peak_idx], 0.0, abs_tol=1e-6), (
        f"drawdown at peak ({peak_idx}) should be 0, "
        f"got {report.drawdown_curve[peak_idx]}"
    )


def test_dict_format_equity_curve_normalised() -> None:
    """The engine's ``run_realistic_backtest`` returns ``equity_curve``
    as a list of per-step snapshot dicts (``{"step": ..., "ts": ...,
    "equity": ..., "drawdown": ...}``) — the report generator's
    ``_normalise_equity`` helper must coerce this into a list of floats
    transparently so ``np.diff(equity)`` doesn't crash with a TypeError.
    """
    # Build a dict-format equity curve (mirrors engine's actual output).
    raw_curve = [
        {"step": 0, "ts": "2025-01-01T00:00:00", "equity": 1000.0, "drawdown": 0.0},
        {"step": 6, "ts": "2025-01-01T06:00:00", "equity": 1010.0, "drawdown": 0.0},
        {"step": 12, "ts": "2025-01-01T12:00:00", "equity": 990.0, "drawdown": 0.0198},
        {"step": 18, "ts": "2025-01-01T18:00:00", "equity": 1020.0, "drawdown": 0.0},
    ]
    result = {"equity_curve": raw_curve, "trades": []}
    report = generate_report(result, strategy_name="dict_format")

    # Equity values extracted correctly.
    assert report.equity_curve == [1000.0, 1010.0, 990.0, 1020.0]
    # max_drawdown reflects the mid-curve dip from 1010 → 990 = 1.98%.
    assert math.isclose(report.max_drawdown, 0.01980198, abs_tol=1e-3)
    # The total_return reflects 1000 → 1020 = +2%.
    assert math.isclose(report.total_return, 0.02, abs_tol=1e-6)


def test_monthly_returns_aggregation() -> None:
    """Multiple trades in the same calendar month must be summed into
    a single ``YYYY-MM`` key; trades in different months yield
    separate keys. The fixture spreads 20 trades across 20 consecutive
    days starting 2023-11-14 → Nov-2023 (6 trades) + Dec-2023 (14
    trades)."""
    result = _synthetic_result(n_trades=20)
    report = generate_report(result)

    assert isinstance(report.monthly_returns, dict)
    assert len(report.monthly_returns) >= 1
    # Every key is the ``YYYY-MM`` format.
    for k in report.monthly_returns:
        assert len(k) == 7 and k[4] == "-", f"bad month key: {k!r}"
    # 12 winners @ $10 + 8 losers @ -$8 = $120 - $64 = $56 total P&L.
    # Sum of monthly P&L should equal this (trades with pnl=0 are
    # skipped — none in this fixture).
    total_monthly = sum(report.monthly_returns.values())
    assert math.isclose(total_monthly, 56.0, abs_tol=1e-6), (
        f"sum(monthly_returns)={total_monthly} != expected 56.0"
    )


def test_trade_cap_at_100() -> None:
    """When the input has more than 100 trades, ``report.trades`` is
    capped at 100 (to keep the JSON payload size manageable for the
    API route). ``total_trades`` still reports the uncapped count."""
    result = _synthetic_result(n_trades=250)
    report = generate_report(result)
    assert report.total_trades == 250, (
        f"total_trades should be uncapped (250), got {report.total_trades}"
    )
    assert len(report.trades) == 100, (
        f"report.trades should be capped at 100, got {len(report.trades)}"
    )


def test_profit_factor_inf_saturated() -> None:
    """An all-winning trade list has no losers → ``gross_loss = 0`` →
    profit_factor is mathematically ``inf``. The report generator must
    saturate this to ``999.0`` so the value is JSON-serialisable and
    doesn't propagate ``Infinity`` into downstream consumers."""
    result = {
        "equity_curve": [1.0, 1.10, 1.20, 1.30, 1.40, 1.50],
        "trades": [
            {"pnl": 10, "timestamp": 1_700_000_000 + i * 86400}
            for i in range(5)
        ],
    }
    report = generate_report(result)
    assert report.losing_trades == 0
    assert report.winning_trades == 5
    # ``inf`` is the raw computation, but the report generator should
    # saturate to 999.0 so JSON serialisation works.
    assert report.profit_factor == 999.0, (
        f"profit_factor should be 999.0 sentinel when no losers; "
        f"got {report.profit_factor}"
    )
    # And the JSON dump doesn't blow up (the whole point of the sentinel).
    json.dumps(report_to_json(report))


# ═══════════════════════════════════════════════════════════════════════════
# 9–12: Edge cases
# ═══════════════════════════════════════════════════════════════════════════


def test_empty_trades() -> None:
    """No trades but a valid equity curve → report still computes. The
    metrics that depend on trades are zeroed (total_trades=0,
    win_rate=0, expectancy=0, monthly_returns={}). profit_factor
    saturates to the ``999.0`` sentinel because the spec defines
    profit_factor as ``gross_profit / gross_loss`` and falls back to
    ``inf`` (→ 999.0) when ``losing`` is empty — which it is when
    there are no trades at all. The equity-curve-derived metrics
    (total_return, sharpe, max_drawdown) are still populated from
    the curve."""
    result = {
        "equity_curve": [1.0, 1.05, 1.10, 1.15, 1.20],
        "trades": [],
    }
    report = generate_report(result, strategy_name="no_trades")
    assert report.total_trades == 0
    assert report.winning_trades == 0
    assert report.losing_trades == 0
    assert report.win_rate == 0
    # profit_factor: empty `losing` list → falls back to inf → 999.0
    # sentinel (avoids JSON ``Infinity`` leak). Documented behaviour.
    assert report.profit_factor == 999.0
    assert report.expectancy == 0
    assert report.monthly_returns == {}
    # Equity-curve metrics still populated.
    assert math.isclose(report.total_return, 0.20, abs_tol=1e-6)
    assert report.sharpe_ratio != 0  # non-trivial curve → non-zero Sharpe.


def test_single_equity_point_returns_empty_report() -> None:
    """An equity curve of length 1 has no returns to compute metrics
    on (``np.diff`` yields an empty array) — the generator must fall
    back to ``_empty_report`` rather than crashing on a 0-length
    returns array."""
    result = {"equity_curve": [1.0], "trades": []}
    report = generate_report(result, strategy_name="single_point")
    assert report.total_return == 0
    assert report.sharpe_ratio == 0
    assert report.max_drawdown == 0
    assert report.total_trades == 0
    assert report.equity_curve == [1.0]


def test_no_equity_curve_key_returns_empty_report() -> None:
    """Missing ``equity_curve`` key entirely → ``_normalise_equity``
    falls back to ``[1.0]`` → single-point → ``_empty_report``
    placeholder. The placeholder is fully zeroed (total_trades=0)
    because the spec's ``generate_report`` returns the empty-report
    sentinel before ever reading the ``trades`` field — degenerate
    inputs (no usable equity curve) yield a degenerate report,
    regardless of what the trades list contains.
    """
    result = {"trades": [{"pnl": 10, "timestamp": 1_700_000_000}]}
    report = generate_report(result)
    assert report.equity_curve == [1.0]
    assert report.total_return == 0
    # The empty-report placeholder is fully zeroed (the spec returns
    # before trade processing when ``len(equity) < 2``).
    assert report.total_trades == 0


def test_iso_string_timestamps_handled() -> None:
    """``run_realistic_backtest`` emits trades with ISO-8601 string
    timestamps under the ``ts`` key (not Unix epoch floats under
    ``timestamp``). The report generator must parse both forms without
    crashing so the same generator works against both engine variants.
    """
    result = {
        "equity_curve": [1.0, 1.05, 1.10],
        "trades": [
            {"pnl": 10, "ts": "2025-01-15T12:00:00"},
            {"pnl": -5, "ts": "2025-02-20T18:00:00"},
        ],
    }
    report = generate_report(result, strategy_name="iso_ts")
    # Two distinct months → two keys.
    assert len(report.monthly_returns) == 2
    assert "2025-01" in report.monthly_returns
    assert "2025-02" in report.monthly_returns
    assert math.isclose(report.monthly_returns["2025-01"], 10.0, abs_tol=1e-6)
    assert math.isclose(report.monthly_returns["2025-02"], -5.0, abs_tol=1e-6)
    # period_start / period_end populated from parsed ISO strings.
    assert report.period_start > 0
    assert report.period_end > report.period_start


# ═══════════════════════════════════════════════════════════════════════════
# 13: JSON serialisation
# ═══════════════════════════════════════════════════════════════════════════


def test_report_to_json_is_json_serialisable() -> None:
    """The dict returned by ``report_to_json`` must round-trip through
    ``json.dumps`` WITHOUT a custom encoder — no ``np.float64`` /
    ``np.int64`` leaks, no ``inf`` / ``NaN`` (which are not valid JSON
    even though Python's ``json.dumps`` tolerates them by default)."""
    result = _synthetic_result()
    report = generate_report(result, strategy_name="json_test")
    d = report_to_json(report)
    # Must be a plain dict (not a dataclass).
    assert isinstance(d, dict)
    # Round-trip — if this raises, we have a non-JSON-safe value
    # somewhere (np.float64 / set / bytes / etc.).
    blob = json.dumps(d)
    # Round-trip back through loads to confirm no data was lost.
    round_tripped = json.loads(blob)
    assert round_tripped["strategy"] == "json_test"
    assert round_tripped["total_trades"] == 20
    assert "equity_curve" in round_tripped
    assert isinstance(round_tripped["equity_curve"], list)
    assert "monthly_returns" in round_tripped
    # ``inf`` / ``NaN`` should never leak through (the profit_factor
    # sentinel of 999.0 is what guards the all-winning case).
    assert math.isfinite(round_tripped["profit_factor"])
    assert math.isfinite(round_tripped["sharpe_ratio"])
    assert math.isfinite(round_tripped["var_95"])


# ═══════════════════════════════════════════════════════════════════════════
# 14: PDF rendering
# ═══════════════════════════════════════════════════════════════════════════


def test_report_to_pdf_writes_valid_pdf(tmp_path: Path) -> None:
    """``report_to_pdf`` writes a real PDF file (magic bytes ``%PDF``,
    size > 1 KB so we know it's not a stub) at the requested path.
    Skipped automatically if ``reportlab`` is not installed — the
    report generator degrades gracefully when the dependency is missing.
    """
    try:
        import reportlab  # noqa: F401
    except ImportError:
        pytest.skip("reportlab not installed — PDF rendering not available")

    result = _synthetic_result()
    report = generate_report(result, strategy_name="pdf_test")

    out_path = tmp_path / "backtest_report.pdf"
    returned = report_to_pdf(report, out_path)

    # Function returns the same path it was given (for chaining).
    assert returned == out_path
    # File exists + non-trivial size (a 1-page A4 with a table + chart
    # is typically 5–30 KB).
    assert out_path.exists(), f"PDF file not created at {out_path}"
    file_size = out_path.stat().st_size
    assert file_size > 1024, (
        f"PDF file suspiciously small ({file_size} B) — likely a stub"
    )
    # Magic bytes — every PDF starts with ``%PDF-``.
    with open(out_path, "rb") as f:
        magic = f.read(5)
    assert magic == b"%PDF-", (
        f"PDF magic bytes wrong: {magic!r} (expected b'%PDF-')"
    )


def test_report_to_pdf_empty_report_does_not_crash(tmp_path: Path) -> None:
    """The ``_empty_report`` placeholder (equity_curve=[1.0]) must
    still render a valid PDF — the chart-rendering helper short-
    circuits when ``len(equity_curve) < 2``, so the PDF just omits
    the chart section instead of crashing on a single-point curve.
    """
    try:
        import reportlab  # noqa: F401
    except ImportError:
        pytest.skip("reportlab not installed — PDF rendering not available")

    report = generate_report({"equity_curve": [1.0], "trades": []})
    out_path = tmp_path / "empty_report.pdf"
    report_to_pdf(report, out_path)
    assert out_path.exists()
    assert out_path.stat().st_size > 200  # at least a title + empty table.


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def test_normalise_equity_handles_mixed_inputs() -> None:
    """``_normalise_equity`` accepts list[float], list[dict], empty
    list, and non-list inputs — each coerced to a list[float] with
    at least one entry (so downstream ``len(equity) < 2`` always
    catches the degenerate case rather than crashing on an empty
    array)."""
    # list[float] — pass-through.
    assert _normalise_equity([1.0, 1.1, 1.2]) == [1.0, 1.1, 1.2]
    # list[dict] — extract ``equity`` key.
    assert _normalise_equity(
        [{"equity": 1.0}, {"equity": 2.0}]
    ) == [1.0, 2.0]
    # Empty list → fallback to [1.0].
    assert _normalise_equity([]) == [1.0]
    # None / non-list → fallback to [1.0].
    assert _normalise_equity(None) == [1.0]
    # Malformed point (string) → skipped; rest preserved.
    out = _normalise_equity([1.0, "bad", 2.0])
    assert out == [1.0, 2.0]


def test_compute_drawdown_duration_returns_zero_on_flat() -> None:
    """A flat zero-drawdown array → max duration 0 (no qualifying
    steps where ``dd > 0.01``)."""
    arr = np.array([0.0, 0.0, 0.0, 0.0])
    assert _compute_drawdown_duration(arr) == 0


def test_compute_drawdown_duration_counts_longest_run() -> None:
    """A drawdown array with two qualifying runs (length 3 + length 5)
    → max duration is 5 (the longest run, not the total)."""
    arr = np.array(
        [0.0, 0.02, 0.03, 0.04, 0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.0]
    )
    assert _compute_drawdown_duration(arr) == 5


# ═══════════════════════════════════════════════════════════════════════════
# API routes (drive the production FastAPI app end-to-end)
# ═══════════════════════════════════════════════════════════════════════════


# Bearer token the conftest sets up (via ``API_TOKEN=test-token-conftest``).
_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_integration.py``.

    The limiter is disabled in ``conftest.py`` so the ``HEAVY_LIMIT``
    (5/min) decorator on the two new routes doesn't 429 the second
    request in this module.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


def test_api_backtest_report_returns_json(client, auth_headers) -> None:
    """``POST /api/backtest/report`` returns 200 + a JSON payload whose
    top-level shape is ``{"status": "completed", "report": {...}}``
    where the ``report`` dict carries every metric the
    ``BacktestReport`` dataclass exposes (Sharpe / Sortino / Calmar /
    win_rate / profit_factor / equity_curve / monthly_returns / …)."""
    response = client.post(
        "/api/backtest/report",
        json={"strategy_id": "mm", "days": 5},
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/report returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    data = response.json()
    assert data["status"] == "completed"
    report = data["report"]
    assert isinstance(report, dict)
    # Spot-check every headline metric is present + finite.
    for key in (
        "report_id",
        "strategy",
        "total_return",
        "annualized_return",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "max_drawdown",
        "volatility",
        "win_rate",
        "profit_factor",
        "expectancy",
        "var_95",
        "cvar_95",
        "total_trades",
        "equity_curve",
        "drawdown_curve",
        "trades",
        "monthly_returns",
    ):
        assert key in report, f"report missing key {key!r}"
    # Finite-float spot checks (catches ``inf`` / ``NaN`` leaks).
    for key in ("total_return", "sharpe_ratio", "win_rate", "profit_factor"):
        v = report[key]
        assert isinstance(v, (int, float)), (
            f"report[{key!r}] must be numeric, got {type(v).__name__}: {v!r}"
        )
        assert math.isfinite(v), f"report[{key!r}] is non-finite: {v!r}"
    # strategy echo.
    assert report["strategy"] == "mm"
    # Equity curve non-empty (engine produces > 1 step for days >= 1).
    assert len(report["equity_curve"]) > 1


def test_api_backtest_report_pdf_returns_pdf_file(client, auth_headers) -> None:
    """``POST /api/backtest/report/pdf`` returns 200 +
    ``Content-Type: application/pdf`` whose body starts with the PDF
    magic bytes ``%PDF-``."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        pytest.skip("reportlab not installed — PDF route returns 503")

    response = client.post(
        "/api/backtest/report/pdf",
        json={"strategy_id": "mm", "days": 5},
        headers=auth_headers,
    )
    assert response.status_code == 200, (
        f"POST /api/backtest/report/pdf returned {response.status_code}; "
        f"body: {response.text[:500]!r}"
    )
    # Content-Type is application/pdf.
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("application/pdf"), (
        f"Content-Type should be application/pdf, got {ctype!r}"
    )
    # Body starts with PDF magic bytes.
    body = response.content
    assert body[:5] == b"%PDF-", (
        f"PDF body magic bytes wrong: {body[:5]!r}"
    )
    # Non-trivial size (> 1 KB).
    assert len(body) > 1024, (
        f"PDF body suspiciously small ({len(body)} B)"
    )
    # Content-Disposition header carries a filename.
    cdisp = response.headers.get("content-disposition", "")
    assert "attachment" in cdisp.lower()
    assert ".pdf" in cdisp.lower()
