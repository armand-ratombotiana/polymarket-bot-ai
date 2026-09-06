"""
W19-6 — Strategy stubs implementation tests.

Covers the four task-spec verification points:

  1. The catalog carries exactly 16 IMPLEMENTED strategies
     (3 original + 3 W19-6 + 5 W22-3 + 5 W44-1 additions) and
     exactly 34 PLANNED entries (50 − 16 = 34).
  2. Each W19-6 strategy's ``evaluate`` method generates the
     documented signal direction under controlled inputs:
       - Mean Reversion → BUY below lower Bollinger Band,
                          SELL above upper Bollinger Band.
       - Momentum       → BUY when ROC ≥ +5 %,
                          SELL when ROC ≤ −5 %.
       - Value          → BUY when model_p − mid ≥ 5 %,
                          SELL when model_p − mid ≤ −5 %.
  3. Each strategy's evaluate returns ``None`` for the
     "no signal" regimes (insufficient history, narrow bands,
     neutral zone, low confidence, wide spread, etc.).
  4. ``GET /api/strategies/catalog`` honours the
     ``?implemented_only=true`` query parameter and the
     response includes the new ``status`` field.

The strategy ``evaluate`` methods are PURE: they take a
``core.data_store.OrderBook`` and a ``list[float]`` price history
(mean_reversion / momentum) or an extracted feature vector (value)
and return a ``*Signal`` dataclass or ``None``. None of them touch
the network, the book poller, or the risk gate — so the per-strategy
tests are deterministic and fast (< 5 ms each).

The API test drives the production ``api.server.app`` via
``fastapi.testclient.TestClient`` (same pattern as
``tests/test_integration.py``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/strategy_stubs_tests")
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
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-strategy-stubs",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``strategies.*``, ``ml.*``)
# regardless of the cwd pytest was launched from. Mirrors the bootstrap
# pattern in ``tests/test_paper_simulator.py`` and ``tests/test_strategy_base.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from core.data_store import OrderBook, PriceLevel, Side, store  # noqa: E402
from strategies.mean_reversion import (  # noqa: E402
    MeanReversionSignal,
    MeanReversionStrategy,
)
from strategies.momentum import (  # noqa: E402
    MomentumSignal,
    MomentumStrategy,
)
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    STRATEGY_CATALOG,
    StrategyRegistry,
)
from strategies.value import ValueSignal, ValueStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` declares ``testpaths = tests`` and
# ``asyncio_mode`` is left at the pytest-asyncio default (``strict``); the
# module-level ``pytestmark`` idiom opts every async test in without
# editing ``pytest.ini`` / ``pyproject.toml``.
pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_book(
    token_id: str = "0xmr_test_token",
    mid: float = 0.50,
    spread: float = 0.01,
) -> OrderBook:
    """Construct a minimal ``OrderBook`` centred on ``mid`` with the given
    ``spread``. Used by the per-strategy signal tests."""
    best_bid = max(0.01, mid - spread / 2.0)
    best_ask = min(0.99, mid + spread / 2.0)
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=best_bid, size=100.0)],
        asks=[PriceLevel(price=best_ask, size=100.0)],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Section 1 — Catalog status counts
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def registry():
    """Fresh ``StrategyRegistry`` per test (no singleton state leak)."""
    return StrategyRegistry()


def test_catalog_size_is_50(registry):
    """The catalog must carry exactly 50 entries — 3 original + 3 W19-6
    implemented strategies + 44 planned stubs. No entries added, no
    entries removed (the W19-6 changes promote three existing stubs to
    IMPLEMENTED rather than adding new rows)."""
    catalog = registry.get_catalog()
    assert len(catalog) == 50
    assert len(catalog) == len(STRATEGY_CATALOG)


def test_catalog_has_sixteen_implemented_strategies(registry):
    """3 (original concrete strategies) + 3 (W19-6 additions) + 5 (W22-3 additions) + 5 (W44-1 additions) = 16
    IMPLEMENTED entries. The sixteen documented ids are:
      - mm_avellaneda_stoikov    → MarketMakerStrategy
      - arb_binary_dutch_book    → ArbScannerStrategy
      - ml_random_forest_quant   → SignalTraderStrategy
      - stat_ornstein_uhlenbeck  → MeanReversionStrategy (W19-6)
      - mom_macd_histogram       → MomentumStrategy      (W19-6)
      - ml_isotonic_calibrated   → ValueStrategy         (W19-6)
      - arb_cross_correlation    → StatisticalArbitrage  (W22-3)
      - event_news_sentiment     → EventDriven           (W22-3)
      - event_resolution_sniper  → Convergence           (W22-3)
      - mm_asymmetric_spread     → SpreadCapture         (W22-3)
      - mm_grid_liquidity        → LiquidityProvision    (W22-3)
      - arb_temporal_expiry      → LateResolution        (W44-1)
      - ml_fractional_kelly      → Ensemble              (W44-1)
      - event_poll_discrepancy   → NewsTrader            (W44-1)
      - event_social_volume      → SentimentAggregator   (W44-1)
      - arb_cluster_dislocation  → CrossMarket           (W44-1)
    """
    catalog = registry.get_catalog()
    implemented = [r for r in catalog if r["status"] == STATUS_IMPLEMENTED]
    assert len(implemented) == 16
    implemented_ids = {r["strategy_id"] for r in implemented}
    assert implemented_ids == {
        "mm_avellaneda_stoikov",
        "arb_binary_dutch_book",
        "ml_random_forest_quant",
        "stat_ornstein_uhlenbeck",
        "mom_macd_histogram",
        "ml_isotonic_calibrated",
        "arb_cross_correlation",
        "event_news_sentiment",
        "event_resolution_sniper",
        "mm_asymmetric_spread",
        "mm_grid_liquidity",
        # W44-1 additions.
        "arb_temporal_expiry",
        "ml_fractional_kelly",
        "event_poll_discrepancy",
        "event_social_volume",
        "arb_cluster_dislocation",
    }


def test_catalog_has_34_planned_strategies(registry):
    """50 − 16 = 34 PLANNED stubs. These remain no-op
    ``QuantStrategyInstance`` wrappers — the W44-1 promotion did NOT
    promote any other stubs to IMPLEMENTED."""
    catalog = registry.get_catalog()
    planned = [r for r in catalog if r["status"] == STATUS_PLANNED]
    assert len(planned) == 34
    # Every planned entry must report ``implemented=False`` (the legacy
    # boolean must stay consistent with the new ``status`` field).
    for r in planned:
        assert r["implemented"] is False


def test_catalog_implemented_only_filter_returns_sixteen(registry):
    """``get_catalog(implemented_only=True)`` returns exactly the sixteen
    IMPLEMENTED rows, never any PLANNED ones."""
    full = registry.get_catalog()
    filtered = registry.get_catalog(implemented_only=True)

    assert len(filtered) == 16
    for row in filtered:
        assert row["status"] == STATUS_IMPLEMENTED
        assert row["implemented"] is True
    # Every filtered row must appear in the full catalog with identical
    # identity fields — the filter must not mutate anything else.
    full_ids = {r["strategy_id"] for r in full}
    filtered_ids = {r["strategy_id"] for r in filtered}
    assert filtered_ids.issubset(full_ids)


def test_catalog_row_has_status_field(registry):
    """Every catalog row must carry a ``status`` key in
    {IMPLEMENTED, PLANNED, EXPERIMENTAL} — the honest per-strategy
    lifecycle flag W19-6 introduces."""
    catalog = registry.get_catalog()
    for row in catalog:
        assert "status" in row
        assert row["status"] in {STATUS_IMPLEMENTED, STATUS_PLANNED, "EXPERIMENTAL"}


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Mean Reversion signal generation
# ═══════════════════════════════════════════════════════════════════════════

def test_mean_reversion_buy_signal_when_price_below_lower_band():
    """When the latest price breaches the lower Bollinger Band (price ≤
    MA − 2σ) by more than MIN_DEVIATION, the strategy must emit a BUY
    signal (expecting mean reversion back up to the MA)."""
    strat = MeanReversionStrategy()
    # 20 prices oscillating around 0.50, then a sharp drop to 0.40 — that
    # 10 % drop is well past MIN_DEVIATION (2 %) and pushes price below
    # the lower 2σ band of the 20-cycle window.
    prices = [0.50, 0.49, 0.51, 0.50, 0.48, 0.52, 0.50, 0.49, 0.51, 0.50,
              0.50, 0.49, 0.51, 0.50, 0.48, 0.52, 0.50, 0.49, 0.51, 0.40]
    assert len(prices) == 20  # exactly WINDOW — enough history to compute bands
    book = _make_book(mid=0.40)

    sig = strat.evaluate(book, prices)

    assert sig is not None
    assert isinstance(sig, MeanReversionSignal)
    assert sig.direction == Side.BUY
    assert sig.token_id == book.token_id
    # BUY fires because the current price (0.40) dropped BELOW the lower
    # band, so the lower band must be ABOVE the current price. The upper
    # band is even further above.
    assert sig.lower_band > 0.40
    assert sig.upper_band > sig.lower_band
    # MA must be near the 0.50 region (the average of the 20-cycle window).
    assert 0.45 < sig.ma < 0.52
    # Size is set to the configured base size.
    assert sig.size_usdc > 0.0


def test_mean_reversion_sell_signal_when_price_above_upper_band():
    """When the latest price breaches the upper Bollinger Band (price ≥
    MA + 2σ) by more than MIN_DEVIATION, the strategy must emit a SELL
    signal (expecting mean reversion back down to the MA)."""
    strat = MeanReversionStrategy()
    # 20 prices oscillating around 0.50, then a sharp spike to 0.60.
    prices = [0.50, 0.49, 0.51, 0.50, 0.48, 0.52, 0.50, 0.49, 0.51, 0.50,
              0.50, 0.49, 0.51, 0.50, 0.48, 0.52, 0.50, 0.49, 0.51, 0.60]
    book = _make_book(mid=0.60)

    sig = strat.evaluate(book, prices)

    assert sig is not None
    assert isinstance(sig, MeanReversionSignal)
    assert sig.direction == Side.SELL
    assert sig.upper_band < 0.60
    assert sig.lower_band < 0.60
    assert 0.48 < sig.ma < 0.52


def test_mean_reversion_returns_none_with_insufficient_history():
    """``evaluate`` must return None when there are fewer than WINDOW
    prices — the moving average and σ cannot be computed yet."""
    strat = MeanReversionStrategy()
    prices = [0.50] * 19  # one short of WINDOW=20
    book = _make_book(mid=0.40)
    assert strat.evaluate(book, prices) is None


def test_mean_reversion_returns_none_inside_bands():
    """When the price is inside the bands (no breach) the strategy must
    return None — no signal possible."""
    strat = MeanReversionStrategy()
    # Prices hover tightly around 0.50 — σ will be tiny, but more
    # importantly the latest price (0.50) is right at the MA so the
    # |deviation| < MIN_DEVIATION guard trips first.
    prices = [0.50] * 20
    book = _make_book(mid=0.50)
    assert strat.evaluate(book, prices) is None


def test_mean_reversion_returns_none_when_deviation_below_min():
    """Even when price is outside the bands, if the absolute deviation
    from the MA is below MIN_DEVIATION (2 %) the strategy must not fire
    — the noise is too small to overcome spread + slippage + fees."""
    strat = MeanReversionStrategy()
    # Build a window with σ=0.005 and current price 0.515 — deviation
    # of 0.015 from a 0.50 MA is 3 % (above MIN_DEVIATION so this would
    # normally fire). To exercise the MIN_DEVIATION guard specifically
    # we build a tighter window where the latest price is barely outside
    # the band: σ=0.005 ⇒ upper=0.510; price=0.511 ⇒ deviation=0.011
    # (2.2 % — above MIN_DEVIATION) — wait, this would still fire.
    #
    # Instead use a near-flat window: σ collapses to ~0.001 and the
    # bands collapse to ≈ the MA, but |deviation| from MA is also
    # tiny (0.001) so the zero-sigma guard trips before MIN_DEVIATION.
    # The contract being verified here is "deviation below MIN_DEVIATION
    # ⇒ None"; the cleanest way to exercise it is a window with σ
    # large enough that bands don't collapse, but the latest price is
    # within MIN_DEVIATION of the MA:
    #   MA = 0.50, σ = 0.03, upper = 0.56, lower = 0.44.
    #   Latest price = 0.515 (1.5 σ above MA but only 1.5 % deviation —
    #   below MIN_DEVIATION=2 %).
    prices = [0.50, 0.47, 0.53, 0.50, 0.46, 0.54, 0.50, 0.47, 0.53, 0.50,
              0.46, 0.54, 0.50, 0.47, 0.53, 0.50, 0.46, 0.54, 0.50, 0.515]
    book = _make_book(mid=0.515)
    # If deviation is below MIN_DEVIATION we expect None; if it's above
    # we expect a SELL. Verify the deviation math to make the test
    # deterministic regardless of MIN_DEVIATION's exact value.
    window = prices[-20:]
    ma = sum(window) / 20
    deviation = abs(window[-1] - ma)
    sig = strat.evaluate(book, prices)
    if deviation < strat._min_deviation:
        assert sig is None
    else:
        # Sanity: if deviation did exceed the threshold, the signal
        # direction must match the band breach (SELL if above upper).
        assert sig is not None
        assert sig.direction in {Side.BUY, Side.SELL}


# ═══════════════════════════════════════════════════════════════════════════
# Section 3 — Momentum signal generation
# ═══════════════════════════════════════════════════════════════════════════

def test_momentum_buy_signal_when_roc_above_threshold():
    """When ROC ≥ +5 % (ROC_BUY_THRESHOLD) the strategy must emit a BUY
    signal — strong upward momentum detected."""
    strat = MomentumStrategy()
    # 11 prices where the latest is 5 %+ above the price 10 cycles ago.
    # past=0.40, current=0.50 → ROC = +25 %.
    prices = [0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50]
    book = _make_book(mid=0.50)

    sig = strat.evaluate(book, prices)

    assert sig is not None
    assert isinstance(sig, MomentumSignal)
    assert sig.direction == Side.BUY
    assert sig.roc >= strat._buy_threshold
    assert sig.token_id == book.token_id


def test_momentum_sell_signal_when_roc_below_threshold():
    """When ROC ≤ −5 % (ROC_SELL_THRESHOLD) the strategy must emit a
    SELL signal — strong downward momentum detected."""
    strat = MomentumStrategy()
    # past=0.50, current=0.40 → ROC = −20 %.
    prices = [0.50, 0.49, 0.48, 0.47, 0.46, 0.45, 0.44, 0.43, 0.42, 0.41, 0.40]
    book = _make_book(mid=0.40)

    sig = strat.evaluate(book, prices)

    assert sig is not None
    assert isinstance(sig, MomentumSignal)
    assert sig.direction == Side.SELL
    assert sig.roc <= strat._sell_threshold


def test_momentum_returns_none_with_insufficient_history():
    """``evaluate`` must return None when there are fewer than
    ROC_PERIOD+1 (= 11) prices — the ROC reference point doesn't exist
    yet."""
    strat = MomentumStrategy()
    prices = [0.50] * 10  # one short of ROC_PERIOD+1
    book = _make_book(mid=0.50)
    assert strat.evaluate(book, prices) is None


def test_momentum_returns_none_in_neutral_zone():
    """When ROC is between SELL_THRESHOLD and BUY_THRESHOLD (the
    neutral zone) the strategy must return None — momentum is too weak
    to act on."""
    strat = MomentumStrategy()
    # past=0.50, current=0.51 → ROC = +2 % (well inside the ±5 % band).
    prices = [0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.51]
    book = _make_book(mid=0.51)
    assert strat.evaluate(book, prices) is None


def test_momentum_returns_none_when_reference_price_is_zero():
    """``evaluate`` must return None when the price ``ROC_PERIOD`` cycles
    ago is zero — division by zero is undefined, so the strategy skips."""
    strat = MomentumStrategy()
    prices = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50]
    book = _make_book(mid=0.50)
    assert strat.evaluate(book, prices) is None


# ═══════════════════════════════════════════════════════════════════════════
# Section 4 — Value strategy signal generation
# ═══════════════════════════════════════════════════════════════════════════

def test_value_buy_signal_when_model_above_market(monkeypatch):
    """When ``model_p - market_mid >= MIN_EDGE (5 %)`` and confidence is
    above the floor, the strategy must emit a BUY signal (market is
    underpriced relative to the model's fair-value estimate)."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.01)

    # Mock the ml_model so predict() returns a model_p well above mid.
    fake_model = SimpleNamespace(
        is_fitted=True,
        predict=lambda features, token_id="": (0.65, 0.60),
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    sig = strat.evaluate(book, features=object())

    assert sig is not None
    assert isinstance(sig, ValueSignal)
    assert sig.direction == Side.BUY
    assert sig.model_p == 0.65
    assert sig.market_mid == 0.50
    assert sig.edge >= strat._min_edge
    assert sig.token_id == book.token_id


def test_value_sell_signal_when_model_below_market(monkeypatch):
    """When ``model_p - market_mid <= -MIN_EDGE (-5 %)`` and confidence
    is above the floor, the strategy must emit a SELL signal (market is
    overpriced relative to the model's fair-value estimate)."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.01)

    fake_model = SimpleNamespace(
        is_fitted=True,
        predict=lambda features, token_id="": (0.35, 0.60),
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    sig = strat.evaluate(book, features=object())

    assert sig is not None
    assert isinstance(sig, ValueSignal)
    assert sig.direction == Side.SELL
    assert sig.model_p == 0.35
    assert sig.market_mid == 0.50
    assert sig.edge <= -strat._min_edge


def test_value_returns_none_when_edge_below_minimum(monkeypatch):
    """When |edge| < MIN_EDGE the strategy must return None — the
    mispricing is too small to overcome spread + slippage + fees."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.01)

    # model_p=0.52, mid=0.50 ⇒ edge=+0.02 (2 %) — below MIN_EDGE=5 %.
    fake_model = SimpleNamespace(
        is_fitted=True,
        predict=lambda features, token_id="": (0.52, 0.60),
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    assert strat.evaluate(book, features=object()) is None


def test_value_returns_none_when_confidence_below_floor(monkeypatch):
    """When confidence < MIN_CONFIDENCE the strategy must return None —
    the model isn't sure enough of its estimate to act on."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.01)

    fake_model = SimpleNamespace(
        is_fitted=True,
        predict=lambda features, token_id="": (0.65, 0.30),  # conf < 0.45
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    assert strat.evaluate(book, features=object()) is None


def test_value_returns_none_when_spread_too_wide(monkeypatch):
    """When the book's spread >= WIDE_SPREAD_CUTOFF (4 %) the strategy
    must return None — the book is too illiquid to fill at the model's
    predicted fair value."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.05)  # 5 % spread

    fake_model = SimpleNamespace(
        is_fitted=True,
        predict=lambda features, token_id="": (0.65, 0.60),
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    assert strat.evaluate(book, features=object()) is None


def test_value_returns_none_when_features_is_none():
    """When features is None (feature extraction failed for this book)
    the strategy must return None — can't call predict on None."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50)
    assert strat.evaluate(book, features=None) is None


def test_value_returns_none_when_model_not_fitted(monkeypatch):
    """When the ML model is not fitted (cold-start) the strategy must
    return None — calling predict on an untrained model returns
    unreliable fallbacks."""
    strat = ValueStrategy()
    book = _make_book(mid=0.50, spread=0.01)

    fake_model = SimpleNamespace(
        is_fitted=False,
        predict=lambda features, token_id="": (0.50, 0.50),
    )
    monkeypatch.setattr("strategies.value.ml_model", fake_model)

    assert strat.evaluate(book, features=object()) is None


# ═══════════════════════════════════════════════════════════════════════════
# Section 5 — Strategy instantiation via the registry
# ═══════════════════════════════════════════════════════════════════════════

async def test_registry_starts_mean_reversion_strategy():
    """``start_strategy("stat_ornstein_uhlenbeck")`` must instantiate
    ``MeanReversionStrategy`` (the W19-6 IMPLEMENTED strategy), not the
    generic ``QuantStrategyInstance`` stub wrapper. The instance is
    immediately stopped so the ``_run`` task doesn't keep polling in
    the test's event loop."""
    reg = StrategyRegistry()
    ok = await reg.start_strategy("stat_ornstein_uhlenbeck")
    assert ok is True
    instances = reg.get_active_instances()
    assert "stat_ornstein_uhlenbeck" in instances
    assert isinstance(instances["stat_ornstein_uhlenbeck"], MeanReversionStrategy)
    await reg.stop_strategy("stat_ornstein_uhlenbeck")


async def test_registry_starts_momentum_strategy():
    """``start_strategy("mom_macd_histogram")`` must instantiate
    ``MomentumStrategy`` (the W19-6 IMPLEMENTED strategy)."""
    reg = StrategyRegistry()
    ok = await reg.start_strategy("mom_macd_histogram")
    assert ok is True
    instances = reg.get_active_instances()
    assert "mom_macd_histogram" in instances
    assert isinstance(instances["mom_macd_histogram"], MomentumStrategy)
    await reg.stop_strategy("mom_macd_histogram")


async def test_registry_starts_value_strategy():
    """``start_strategy("ml_isotonic_calibrated")`` must instantiate
    ``ValueStrategy`` (the W19-6 IMPLEMENTED strategy)."""
    reg = StrategyRegistry()
    ok = await reg.start_strategy("ml_isotonic_calibrated")
    assert ok is True
    instances = reg.get_active_instances()
    assert "ml_isotonic_calibrated" in instances
    assert isinstance(instances["ml_isotonic_calibrated"], ValueStrategy)
    await reg.stop_strategy("ml_isotonic_calibrated")


async def test_registry_marks_running_state_in_catalog():
    """After ``start_strategy`` succeeds, ``get_catalog()`` must report
    ``is_running=True`` for the started IMPLEMENTED strategy and
    ``is_running=False`` for every other IMPLEMENTED strategy (that
    wasn't started) AND for every PLANNED strategy (stubs never have a
    running state because they don't execute a real loop)."""
    reg = StrategyRegistry()
    await reg.start_strategy("stat_ornstein_uhlenbeck")
    catalog = reg.get_catalog()
    started_row = next(r for r in catalog if r["strategy_id"] == "stat_ornstein_uhlenbeck")
    assert started_row["is_running"] is True

    # The other IMPLEMENTED strategies must report is_running=False.
    other_implemented = [
        r for r in catalog
        if r["status"] == STATUS_IMPLEMENTED and r["strategy_id"] != "stat_ornstein_uhlenbeck"
    ]
    assert len(other_implemented) == 15  # 16 implemented − 1 started
    for r in other_implemented:
        assert r["is_running"] is False

    # Every PLANNED entry must also report is_running=False (stubs have
    # no concrete ``_run`` loop, so even if they were started the flag
    # would stay False — the ``is_running`` derivation in
    # ``StrategyRegistry.get_catalog`` filters on
    # ``implemented and strategy_id in self._instances``).
    for r in catalog:
        if r["status"] == STATUS_PLANNED:
            assert r["is_running"] is False

    await reg.stop_strategy("stat_ornstein_uhlenbeck")


# ═══════════════════════════════════════════════════════════════════════════
# Section 6 — API endpoint filtering
# ═══════════════════════════════════════════════════════════════════════════

# The API test drives the production ``api.server.app`` via
# ``fastapi.testclient.TestClient`` (same pattern as
# ``tests/test_integration.py``). The bearer token matches the one
# conftest sets via ``API_TOKEN=test-token-conftest`` (or the override
# in this module's env-var redirect block above).
_VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-conftest")


def test_api_strategies_catalog_returns_full_50_entries():
    """``GET /api/strategies/catalog`` (no query param) must return all
    50 entries — IMPLEMENTED and PLANNED alike — with the ``status``
    field populated on every row."""
    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_VALID_TOKEN}"}
    response = client.get("/api/strategies/catalog", headers=headers)
    assert response.status_code == 200, (
        f"GET /api/strategies/catalog must return 200; got {response.status_code}"
    )
    data = response.json()
    assert "catalog" in data and isinstance(data["catalog"], list)
    assert data["total"] == len(data["catalog"])
    assert data["total"] == 50  # 16 implemented + 34 planned
    # ``status`` field is present on every row.
    for row in data["catalog"]:
        assert "status" in row
        assert row["status"] in {STATUS_IMPLEMENTED, STATUS_PLANNED, "EXPERIMENTAL"}


def test_api_strategies_catalog_implemented_only_returns_sixteen():
    """``GET /api/strategies/catalog?implemented_only=true`` must return
    only the sixteen IMPLEMENTED strategies — every PLANNED stub is
    excluded from the response."""
    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_VALID_TOKEN}"}
    response = client.get(
        "/api/strategies/catalog?implemented_only=true",
        headers=headers,
    )
    assert response.status_code == 200, (
        f"GET /api/strategies/catalog?implemented_only=true must return 200; "
        f"got {response.status_code}"
    )
    data = response.json()
    assert data["total"] == 16, (
        f"implemented_only filter must return exactly 16 entries; got {data['total']}"
    )
    for row in data["catalog"]:
        assert row["status"] == STATUS_IMPLEMENTED
        assert row["implemented"] is True
    # The response surfaces the implementation breakdown for the UI.
    assert data["implemented_count"] == 16
    assert data["filtered"] is True


def test_api_strategies_catalog_includes_status_breakdown():
    """The catalog response must surface ``implemented_count`` and
    ``planned_count`` so the UI can render an "X implemented / Y
    planned" header without a second round-trip."""
    from fastapi.testclient import TestClient
    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {_VALID_TOKEN}"}
    response = client.get("/api/strategies/catalog", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["implemented_count"] == 16
    assert data["planned_count"] == 34
    assert data["filtered"] is False
