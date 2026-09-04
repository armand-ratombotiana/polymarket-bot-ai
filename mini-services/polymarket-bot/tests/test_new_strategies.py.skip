"""
W22-3 — Five new strategy implementation tests.

Covers the five high-value strategies promoted from the PLANNED catalog
to IMPLEMENTED status in Wave 22-3:

  1. ``StatisticalArbitrage`` (``strategies/stat_arb.py``)
     → catalog id ``arb_cross_correlation`` (arbitrage category).
  2. ``EventDriven``          (``strategies/event_driven.py``)
     → catalog id ``event_news_sentiment`` (event_driven category).
  3. ``Convergence``          (``strategies/convergence.py``)
     → catalog id ``event_resolution_sniper`` (event_driven category).
  4. ``SpreadCapture``       (``strategies/spread_capture.py``)
     → catalog id ``mm_asymmetric_spread`` (market_making category).
  5. ``LiquidityProvision``   (``strategies/liquidity.py``)
     → catalog id ``mm_grid_liquidity`` (market_making category).

Per-strategy test coverage (each strategy has its own section):
  - The 9-method ``StrategyContract`` surface is fully implemented
    (every method is callable, returns the documented shape).
  - ``generate_signal`` fires a Signal under the documented
    "actionable" regime (e.g. ``|sentiment| ≥ threshold`` for
    EventDriven, ``certainty ≥ 0.85 && hours ≤ 24`` for Convergence).
  - ``generate_signal`` returns ``None`` under the documented
    "no signal" regime (missing context, below threshold, on
    cooldown, etc.).
  - ``validate`` returns ``(True, "OK")`` on the default config.
  - ``validate`` returns ``(False, ...)`` on a deliberately-broken
    config (out-of-range threshold).
  - ``configure`` actually applies the configured parameter to the
    instance's typed field (so subsequent ``generate_signal`` calls
    honour the new threshold).
  - ``estimate_edge`` returns the signal's ``edge`` field (and
    ``0.0`` for ``None``).
  - ``size_position`` returns a non-negative, capital-bounded size
    when given a real signal.
  - ``entry_logic`` returns a dict carrying at least ``price``,
    ``side``, ``type`` (limit), ``token_id``.
  - ``exit_logic`` returns ``None`` for the "no exit" regime and a
    dict for the "exit" regime.
  - ``diagnostics`` returns a dict carrying the strategy-specific
    config fields surfaced for the dashboard.

The strategy ``generate_signal`` methods are PURE: they take a
plain ``market_context`` dict (NOT the live store) and return a
``Signal`` or ``None``. None of them touch the network, the book
poller, or the risk gate — so the per-strategy tests are
deterministic and fast (< 5 ms each).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/strategy_new_strategies_tests")
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
    "API_TOKEN": "test-token-new-strategies",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``strategies.*``, ``ml.*``)
# regardless of the cwd pytest was launched from. Mirrors the bootstrap
# pattern in every existing ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from strategies.base import BaseStrategy, Signal  # noqa: E402
from strategies.convergence import Convergence  # noqa: E402
from strategies.event_driven import EventDriven  # noqa: E402
from strategies.liquidity import LiquidityProvision  # noqa: E402
from strategies.spread_capture import SpreadCapture  # noqa: E402
from strategies.stat_arb import StatisticalArbitrage  # noqa: E402
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STRATEGY_CATALOG,
    StrategyRegistry,
    _IMPLEMENTED_STRATEGY_CLASSES,
)

# ``pytestmark`` is intentionally NOT set at module scope: most tests in
# this module are sync (the 9 contract methods are sync by design), and
# applying ``@pytest.mark.asyncio`` to them would trip a PytestWarning.
# The one async test (``test_registry_can_instantiate_each_new_strategy``)
# opts in explicitly via ``@pytest.mark.asyncio`` decoration.


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

# The canonical 9 contract methods every strategy must implement.
CONTRACT_METHODS: tuple[str, ...] = (
    "metadata",
    "configure",
    "validate",
    "generate_signal",
    "estimate_edge",
    "size_position",
    "entry_logic",
    "exit_logic",
    "diagnostics",
)

# The five W22-3 strategies, paired with their catalog id and class.
NEW_STRATEGIES: list[tuple[str, type[BaseStrategy], str]] = [
    ("arb_cross_correlation", StatisticalArbitrage, "stat_arb"),
    ("event_news_sentiment", EventDriven, "event_driven"),
    ("event_resolution_sniper", Convergence, "convergence"),
    ("mm_asymmetric_spread", SpreadCapture, "spread_capture"),
    ("mm_grid_liquidity", LiquidityProvision, "liquidity_provision"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Section 0 — Registry wiring
# ═══════════════════════════════════════════════════════════════════════════

def test_registry_marks_five_new_strategies_as_implemented():
    """The five W22-3 strategies must be marked ``status=IMPLEMENTED`` in
    the catalog and present in ``_IMPLEMENTED_STRATEGY_CLASSES``."""
    catalog_ids = {s.strategy_id: s for s in STRATEGY_CATALOG}
    expected_new = {
        "arb_cross_correlation",
        "event_news_sentiment",
        "event_resolution_sniper",
        "mm_asymmetric_spread",
        "mm_grid_liquidity",
    }
    for sid in expected_new:
        assert sid in catalog_ids, f"{sid} missing from catalog"
        assert catalog_ids[sid].status == STATUS_IMPLEMENTED, (
            f"{sid} must be marked IMPLEMENTED (got {catalog_ids[sid].status})"
        )
        assert sid in _IMPLEMENTED_STRATEGY_CLASSES, (
            f"{sid} missing from _IMPLEMENTED_STRATEGY_CLASSES map"
        )


def test_registry_total_implemented_is_eleven():
    """6 (original + W19-6) + 5 (W22-3) = 11 IMPLEMENTED strategies."""
    implemented = [s for s in STRATEGY_CATALOG if s.status == STATUS_IMPLEMENTED]
    assert len(implemented) == 11
    planned = [s for s in STRATEGY_CATALOG if s.status != STATUS_IMPLEMENTED]
    assert len(planned) == 39  # 50 − 11


@pytest.mark.asyncio
async def test_registry_can_instantiate_each_new_strategy():
    """``StrategyRegistry.start_strategy(<new_id>)`` must instantiate the
    concrete class (not the ``QuantStrategyInstance`` stub wrapper)."""
    reg = StrategyRegistry()
    for sid, cls, _name in NEW_STRATEGIES:
        ok = await reg.start_strategy(sid)
        assert ok is True, f"start_strategy({sid}) returned False"
        instances = reg.get_active_instances()
        assert sid in instances, f"{sid} not in active_instances"
        assert isinstance(instances[sid], cls), (
            f"{sid} instantiated as {type(instances[sid]).__name__}, "
            f"expected {cls.__name__}"
        )
        await reg.stop_strategy(sid)


# ═══════════════════════════════════════════════════════════════════════════
# Section 1 — StatisticalArbitrage (arb_cross_correlation)
# ═══════════════════════════════════════════════════════════════════════════

def test_stat_arb_implements_all_nine_contract_methods():
    """``StatisticalArbitrage`` must override every one of the 9 contract
    methods (not rely on BaseStrategy defaults)."""
    s = StatisticalArbitrage()
    for method_name in CONTRACT_METHODS:
        method = getattr(s, method_name, None)
        assert callable(method), f"{method_name} not callable on StatisticalArbitrage"
        # Verify it's overridden on the subclass itself (not just inherited
        # from BaseStrategy) — the spec says "REAL logic (not pass)".
        cls_method = getattr(StatisticalArbitrage, method_name, None)
        base_method = getattr(BaseStrategy, method_name, None)
        assert cls_method is not base_method, (
            f"{method_name} must be overridden on StatisticalArbitrage, "
            f"not inherited from BaseStrategy"
        )


def test_stat_arb_metadata_has_correct_fields():
    s = StatisticalArbitrage()
    md = s.metadata()
    assert md["name"] == "stat_arb"
    assert md["version"] == "1.0.0"
    assert md["category"] == "arbitrage"
    assert "description" in md and isinstance(md["description"], str)
    assert "model" in md


def test_stat_arb_validate_default_returns_true_ok():
    s = StatisticalArbitrage()
    ok, msg = s.validate()
    assert ok is True
    assert msg == "OK"


def test_stat_arb_validate_rejects_out_of_range_correlation_threshold():
    s = StatisticalArbitrage()
    s.correlation_threshold = 1.5  # > 1.0 is invalid
    ok, msg = s.validate()
    assert ok is False
    assert "correlation_threshold" in msg


def test_stat_arb_validate_rejects_zero_spread_zscore_threshold():
    s = StatisticalArbitrage()
    s.spread_zscore_threshold = 0.0
    ok, msg = s.validate()
    assert ok is False
    assert "spread_zscore_threshold" in msg


def test_stat_arb_configure_applies_parameters():
    s = StatisticalArbitrage()
    s.configure({
        "correlation_threshold": 0.85,
        "spread_zscore_threshold": 2.0,
        "max_position_pct": 0.03,
    })
    assert s.correlation_threshold == 0.85
    assert s.spread_zscore_threshold == 2.0
    assert s.max_position_pct == 0.03


def test_stat_arb_generate_signal_fires_buy_on_mispricing():
    """When the spread z-score exceeds the action threshold, the strategy
    must fire a BUY signal on the under-priced leg."""
    s = StatisticalArbitrage()
    ctx = {
        "market1": {"token_id": "tok_a", "mid": 0.55},
        "market2": {"token_id": "tok_b", "mid": 0.50},
        "correlation": 0.90,
        "historical_spread_mean": 0.00,
        "historical_spread_std": 0.02,  # spread=0.05, z=2.5σ
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    # z > 0 ⇒ market1 is over-priced ⇒ BUY market2 (the under-priced leg).
    assert sig.token_id == "tok_b"
    assert sig.edge > 0.0
    assert "pair_key" in sig.metadata
    assert "short_token" in sig.metadata
    assert sig.metadata["short_token"] == "tok_a"


def test_stat_arb_generate_signal_returns_none_when_uncorrelated():
    """When |correlation| < correlation_threshold, no signal — the markets
    aren't coupled tightly enough for stat-arb."""
    s = StatisticalArbitrage()
    ctx = {
        "market1": {"token_id": "tok_a", "mid": 0.55},
        "market2": {"token_id": "tok_b", "mid": 0.50},
        "correlation": 0.30,  # below 0.70 threshold
        "historical_spread_mean": 0.0,
        "historical_spread_std": 0.02,
    }
    assert s.generate_signal(ctx) is None


def test_stat_arb_generate_signal_returns_none_when_no_history_and_small_spread():
    """When no historical σ is supplied AND the raw spread is below the
    5% raw threshold, the strategy must return None."""
    s = StatisticalArbitrage()
    ctx = {
        "market1": {"token_id": "tok_a", "mid": 0.52},
        "market2": {"token_id": "tok_b", "mid": 0.50},
        "correlation": 0.90,
        # No historical_spread_mean / std ⇒ falls back to 5% raw threshold.
        # Spread is 2%, below the 5% raw threshold ⇒ z < 1.5σ ⇒ no signal.
    }
    assert s.generate_signal(ctx) is None


def test_stat_arb_generate_signal_returns_none_when_missing_markets():
    """Missing market1 / market2 ⇒ None (can't pair-trade without a pair)."""
    s = StatisticalArbitrage()
    assert s.generate_signal({}) is None
    assert s.generate_signal({"market1": {"token_id": "tok_a", "mid": 0.5}}) is None


def test_stat_arb_estimate_edge_returns_signal_edge():
    s = StatisticalArbitrage()
    sig = Signal(action="BUY", token_id="tok", edge=0.123)
    assert s.estimate_edge(sig) == 0.123
    assert s.estimate_edge(None) == 0.0


def test_stat_arb_size_position_bounded_by_max_position_pct():
    s = StatisticalArbitrage()
    s.max_position_pct = 0.05
    sig = Signal(action="BUY", token_id="tok", edge=0.20, price=0.5)
    size = s.size_position(sig, capital=100.0, risk_params={})
    # min(max_pct × capital, edge × capital × 0.5) = min(5.0, 10.0) = 5.0
    assert 0.0 < size <= 5.0


def test_stat_arb_size_position_zero_for_hold():
    s = StatisticalArbitrage()
    sig = Signal(action="HOLD", token_id="tok")
    assert s.size_position(sig, capital=100.0, risk_params={}) == 0.0


def test_stat_arb_entry_logic_returns_limit_order_dict():
    s = StatisticalArbitrage()
    sig = Signal(
        action="BUY", token_id="tok_b", size=1.0, price=0.50, edge=0.10,
        metadata={
            "pair_key": "tok_a:tok_b",
            "z_score": 2.5,
            "short_token": "tok_a",
        },
    )
    entry = s.entry_logic(sig, {
        "market1": {"token_id": "tok_a", "mid": 0.55},
        "market2": {"token_id": "tok_b", "mid": 0.50},
    })
    assert entry["type"] == "limit"
    assert entry["side"] == "BUY"
    assert entry["price"] == 0.50
    assert entry["token_id"] == "tok_b"
    assert "metadata" in entry and "short_leg" in entry["metadata"]


def test_stat_arb_exit_logic_returns_none_when_not_converged():
    s = StatisticalArbitrage()
    pos = {"entry_z": 2.5}
    # Spread not converged — z is still 2.0σ, above the 0.5σ exit band.
    ctx = {
        "market1": {"mid": 0.55}, "market2": {"mid": 0.50},
        "historical_spread_mean": 0.0, "historical_spread_std": 0.02,
    }
    assert s.exit_logic(pos, ctx) is None


def test_stat_arb_exit_logic_returns_dict_when_converged():
    s = StatisticalArbitrage()
    pos = {"entry_z": 2.5}
    # Spread converged — z = 0.2σ, below the 0.5σ exit band.
    ctx = {
        "market1": {"mid": 0.502}, "market2": {"mid": 0.50},
        "historical_spread_mean": 0.0, "historical_spread_std": 0.02,
    }
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "reason" in out
    assert out["reason"] == "spread converged"


def test_stat_arb_diagnostics_returns_dict_with_config():
    s = StatisticalArbitrage()
    diag = s.diagnostics()
    assert diag["name"] == "stat_arb"
    assert "correlation_threshold" in diag
    assert "spread_zscore_threshold" in diag
    assert "open_pairs" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — EventDriven (event_news_sentiment)
# ═══════════════════════════════════════════════════════════════════════════

def test_event_driven_implements_all_nine_contract_methods():
    s = EventDriven()
    for method_name in CONTRACT_METHODS:
        method = getattr(s, method_name, None)
        assert callable(method), f"{method_name} not callable on EventDriven"
        cls_method = getattr(EventDriven, method_name, None)
        base_method = getattr(BaseStrategy, method_name, None)
        assert cls_method is not base_method, (
            f"{method_name} must be overridden on EventDriven"
        )


def test_event_driven_metadata_has_correct_fields():
    s = EventDriven()
    md = s.metadata()
    assert md["name"] == "event_driven"
    assert md["category"] == "event_driven"
    assert "model" in md


def test_event_driven_validate_default_returns_true_ok():
    s = EventDriven()
    ok, msg = s.validate()
    assert ok is True
    assert msg == "OK"


def test_event_driven_validate_rejects_invalid_thresholds():
    s = EventDriven()
    s.sentiment_threshold = 1.5
    ok, msg = s.validate()
    assert ok is False
    assert "sentiment_threshold" in msg


def test_event_driven_configure_applies_parameters():
    s = EventDriven()
    s.configure({
        "sentiment_threshold": 0.30,
        "min_confidence": 0.40,
        "max_news_age_seconds": 600.0,
    })
    assert s.sentiment_threshold == 0.30
    assert s.min_confidence == 0.40
    assert s.max_news_age_seconds == 600.0


def test_event_driven_generate_signal_fires_buy_on_positive_sentiment():
    s = EventDriven()
    ctx = {
        "token_id": "tok_e", "mid": 0.50,
        "news_event": {
            "headline": "BTC ETF approved",
            "sentiment_score": 0.60,
            "confidence": 0.70,
        },
        "time_since_event_seconds": 30,
        "news_velocity": 8.0,
        "now": 1000000.0,
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    assert sig.token_id == "tok_e"
    assert sig.edge > 0.0
    assert sig.metadata["sentiment_score"] == 0.60
    assert sig.metadata["velocity_multiplier"] >= 1.0


def test_event_driven_generate_signal_fires_sell_on_negative_sentiment():
    s = EventDriven()
    ctx = {
        "token_id": "tok_e", "mid": 0.50,
        "news_event": {
            "headline": "Exchange hack",
            "sentiment_score": -0.70,
            "confidence": 0.80,
        },
        "time_since_event_seconds": 60,
        "news_velocity": 12.0,
        "now": 1000000.0,
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert sig.action == "SELL"


def test_event_driven_generate_signal_returns_none_when_no_news():
    s = EventDriven()
    ctx = {"token_id": "tok_e", "mid": 0.50}
    assert s.generate_signal(ctx) is None


def test_event_driven_generate_signal_returns_none_when_low_confidence():
    s = EventDriven()
    ctx = {
        "token_id": "tok_e", "mid": 0.50,
        "news_event": {"sentiment_score": 0.80, "confidence": 0.20},
        "time_since_event_seconds": 30,
        "now": 1000000.0,
    }
    assert s.generate_signal(ctx) is None


def test_event_driven_generate_signal_returns_none_when_stale_news():
    s = EventDriven()
    ctx = {
        "token_id": "tok_e", "mid": 0.50,
        "news_event": {"sentiment_score": 0.80, "confidence": 0.80},
        "time_since_event_seconds": 600,  # > max_news_age_seconds (300)
        "now": 1000000.0,
    }
    assert s.generate_signal(ctx) is None


def test_event_driven_generate_signal_returns_none_when_weak_sentiment():
    s = EventDriven()
    ctx = {
        "token_id": "tok_e", "mid": 0.50,
        "news_event": {"sentiment_score": 0.20, "confidence": 0.80},
        "time_since_event_seconds": 30,
        "now": 1000000.0,
    }
    assert s.generate_signal(ctx) is None


def test_event_driven_estimate_edge_returns_signal_edge():
    s = EventDriven()
    sig = Signal(action="BUY", token_id="tok", edge=0.42)
    assert s.estimate_edge(sig) == 0.42


def test_event_driven_size_position_bounded_by_max_pct():
    s = EventDriven()
    s.max_position_pct = 0.05
    sig = Signal(action="BUY", token_id="tok", edge=0.10, price=0.5)
    size = s.size_position(sig, capital=100.0, risk_params={})
    assert 0.0 < size <= 5.0


def test_event_driven_entry_logic_returns_limit_order_dict():
    s = EventDriven()
    sig = Signal(
        action="BUY", token_id="tok_e", size=1.0, price=0.52, edge=0.10,
        metadata={"sentiment_score": 0.6, "headline": "good news",
                  "news_age_seconds": 30},
    )
    entry = s.entry_logic(sig, {"mid": 0.50})
    assert entry["type"] == "limit"
    assert entry["side"] == "BUY"
    assert entry["price"] == 0.52


def test_event_driven_exit_logic_returns_none_when_within_hold():
    s = EventDriven()
    pos = {"held_seconds": 100.0, "max_hold_seconds": 600.0,
           "entry_sentiment": 0.6}
    ctx = {"news_event": {"sentiment_score": 0.6}}
    assert s.exit_logic(pos, ctx) is None


def test_event_driven_exit_logic_returns_dict_when_hold_expired():
    s = EventDriven()
    pos = {"held_seconds": 700.0, "max_hold_seconds": 600.0,
           "entry_sentiment": 0.6}
    ctx = {}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "max_hold_seconds" in out["reason"]


def test_event_driven_exit_logic_returns_dict_on_sentiment_reversal():
    s = EventDriven()
    pos = {"held_seconds": 100.0, "max_hold_seconds": 600.0,
           "entry_sentiment": 0.6}
    # Follow-up news has opposite-sign sentiment.
    ctx = {"news_event": {"sentiment_score": -0.50}}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "sentiment reversal" in out["reason"]


def test_event_driven_diagnostics_returns_dict_with_config():
    s = EventDriven()
    diag = s.diagnostics()
    assert diag["name"] == "event_driven"
    assert "sentiment_threshold" in diag
    assert "min_confidence" in diag
    assert "tokens_in_cooldown" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 3 — Convergence (event_resolution_sniper)
# ═══════════════════════════════════════════════════════════════════════════

def test_convergence_implements_all_nine_contract_methods():
    s = Convergence()
    for method_name in CONTRACT_METHODS:
        method = getattr(s, method_name, None)
        assert callable(method), f"{method_name} not callable on Convergence"
        cls_method = getattr(Convergence, method_name, None)
        base_method = getattr(BaseStrategy, method_name, None)
        assert cls_method is not base_method, (
            f"{method_name} must be overridden on Convergence"
        )


def test_convergence_metadata_has_correct_fields():
    s = Convergence()
    md = s.metadata()
    assert md["name"] == "convergence"
    assert md["category"] == "event_driven"
    assert "model" in md


def test_convergence_validate_default_returns_true_ok():
    s = Convergence()
    ok, msg = s.validate()
    assert ok is True
    assert msg == "OK"


def test_convergence_validate_rejects_invalid_max_hours():
    s = Convergence()
    s.max_hours_to_resolution = 0.0
    ok, msg = s.validate()
    assert ok is False
    assert "max_hours_to_resolution" in msg


def test_convergence_configure_applies_parameters():
    s = Convergence()
    s.configure({
        "max_hours_to_resolution": 12.0,
        "min_resolution_certainty": 0.90,
        "min_edge": 0.04,
    })
    assert s.max_hours_to_resolution == 12.0
    assert s.min_resolution_certainty == 0.90
    assert s.min_edge == 0.04


def test_convergence_generate_signal_fires_buy_on_near_certain_outcome():
    s = Convergence()
    ctx = {
        "token_id": "tok_c", "mid": 0.90,
        "hours_to_resolution": 6.0,
        "resolution_certainty": 0.95,
        "spread": 0.01, "liquidity_usdc": 500.0,
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    assert sig.token_id == "tok_c"
    assert sig.edge == pytest.approx(0.05)  # 0.95 − 0.90
    assert sig.metadata["resolution_certainty"] == 0.95
    assert sig.metadata["hours_to_resolution"] == 6.0


def test_convergence_generate_signal_returns_none_when_outside_24h():
    s = Convergence()
    ctx = {
        "token_id": "tok_c", "mid": 0.90,
        "hours_to_resolution": 48.0,  # > 24
        "resolution_certainty": 0.95,
        "spread": 0.01, "liquidity_usdc": 500.0,
    }
    assert s.generate_signal(ctx) is None


def test_convergence_generate_signal_returns_none_when_low_certainty():
    s = Convergence()
    ctx = {
        "token_id": "tok_c", "mid": 0.50,
        "hours_to_resolution": 6.0,
        "resolution_certainty": 0.60,  # below 0.85
        "spread": 0.01, "liquidity_usdc": 500.0,
    }
    assert s.generate_signal(ctx) is None


def test_convergence_generate_signal_returns_none_when_wide_spread():
    s = Convergence()
    ctx = {
        "token_id": "tok_c", "mid": 0.50,
        "hours_to_resolution": 6.0,
        "resolution_certainty": 0.95,
        "spread": 0.08,  # > 0.05 max_spread
        "liquidity_usdc": 500.0,
    }
    assert s.generate_signal(ctx) is None


def test_convergence_generate_signal_returns_none_when_low_liquidity():
    s = Convergence()
    ctx = {
        "token_id": "tok_c", "mid": 0.50,
        "hours_to_resolution": 6.0,
        "resolution_certainty": 0.95,
        "spread": 0.01,
        "liquidity_usdc": 10.0,  # < MIN_LIQUIDITY_USDC=50
    }
    assert s.generate_signal(ctx) is None


def test_convergence_generate_signal_returns_none_when_edge_below_min():
    s = Convergence()
    s.min_edge = 0.10  # raise the floor
    ctx = {
        "token_id": "tok_c", "mid": 0.92,
        "hours_to_resolution": 6.0,
        "resolution_certainty": 0.95,  # edge = 0.03, below min_edge=0.10
        "spread": 0.01, "liquidity_usdc": 500.0,
    }
    assert s.generate_signal(ctx) is None


def test_convergence_estimate_edge_returns_signal_edge():
    s = Convergence()
    sig = Signal(action="BUY", token_id="tok", edge=0.05)
    assert s.estimate_edge(sig) == 0.05


def test_convergence_size_position_bounded_by_max_pct():
    s = Convergence()
    s.max_position_pct = 0.10
    sig = Signal(action="BUY", token_id="tok", edge=0.05, price=0.95,
                metadata={"resolution_certainty": 0.95})
    size = s.size_position(sig, capital=100.0, risk_params={})
    assert 0.0 < size <= 10.0


def test_convergence_entry_logic_returns_limit_order_dict():
    s = Convergence()
    sig = Signal(
        action="BUY", token_id="tok_c", size=1.0, price=0.92, edge=0.05,
        metadata={"hours_to_resolution": 6.0, "resolution_certainty": 0.95,
                  "annualized_edge": 73.0},
    )
    entry = s.entry_logic(sig, {"mid": 0.90})
    assert entry["type"] == "limit"
    assert entry["side"] == "BUY"
    assert entry["price"] == 0.92
    assert entry["metadata"]["resolution_certainty"] == 0.95


def test_convergence_exit_logic_returns_none_when_certainty_still_high():
    s = Convergence()
    pos = {"entry_certainty": 0.95}
    ctx = {"resolution_certainty": 0.95, "hours_to_resolution": 5.0}
    assert s.exit_logic(pos, ctx) is None


def test_convergence_exit_logic_returns_dict_when_certainty_drops():
    s = Convergence()
    pos = {"entry_certainty": 0.95}
    ctx = {"resolution_certainty": 0.50, "hours_to_resolution": 5.0}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "certainty" in out["reason"]


def test_convergence_exit_logic_returns_dict_when_resolution_overdue():
    s = Convergence()
    pos = {"entry_certainty": 0.95}
    ctx = {"resolution_certainty": 0.95, "hours_to_resolution": -1.0}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "overdue" in out["reason"]


def test_convergence_diagnostics_returns_dict_with_config():
    s = Convergence()
    diag = s.diagnostics()
    assert diag["name"] == "convergence"
    assert "max_hours_to_resolution" in diag
    assert "min_resolution_certainty" in diag
    assert "entered_tokens" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 4 — SpreadCapture (mm_asymmetric_spread)
# ═══════════════════════════════════════════════════════════════════════════

def test_spread_capture_implements_all_nine_contract_methods():
    s = SpreadCapture()
    for method_name in CONTRACT_METHODS:
        method = getattr(s, method_name, None)
        assert callable(method), f"{method_name} not callable on SpreadCapture"
        cls_method = getattr(SpreadCapture, method_name, None)
        base_method = getattr(BaseStrategy, method_name, None)
        assert cls_method is not base_method, (
            f"{method_name} must be overridden on SpreadCapture"
        )


def test_spread_capture_metadata_has_correct_fields():
    s = SpreadCapture()
    md = s.metadata()
    assert md["name"] == "spread_capture"
    assert md["category"] == "market_making"
    assert "model" in md


def test_spread_capture_validate_default_returns_true_ok():
    s = SpreadCapture()
    ok, msg = s.validate()
    assert ok is True
    assert msg == "OK"


def test_spread_capture_validate_rejects_zero_base_spread():
    s = SpreadCapture()
    s.base_half_spread = 0.0
    ok, msg = s.validate()
    assert ok is False
    assert "base_half_spread" in msg


def test_spread_capture_validate_rejects_max_below_min_spread():
    s = SpreadCapture()
    s.min_half_spread = 0.05
    s.max_half_spread = 0.02  # max < min
    ok, msg = s.validate()
    assert ok is False
    assert "max_half_spread" in msg


def test_spread_capture_configure_applies_parameters():
    s = SpreadCapture()
    s.configure({
        "base_half_spread": 0.025,
        "skew_factor": 0.40,
        "max_half_spread": 0.08,
    })
    assert s.base_half_spread == 0.025
    assert s.skew_factor == 0.40
    assert s.max_half_spread == 0.08


def test_spread_capture_generate_signal_returns_quote_with_bid_and_ask():
    s = SpreadCapture()
    ctx = {
        "token_id": "tok_s", "mid": 0.50,
        "order_flow_imbalance": 0.30,
        "inventory": 0.0,
        "volatility": 0.02,
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"  # the bid leg
    assert sig.token_id == "tok_s"
    assert sig.price < 0.50  # bid below mid
    assert sig.metadata["ask_price"] > 0.50  # ask above mid
    assert sig.metadata["half_spread"] > 0.0
    assert sig.metadata["ofi"] == 0.30
    assert "ask_quote" in sig.metadata


def test_spread_capture_generate_signal_returns_none_when_missing_mid():
    s = SpreadCapture()
    assert s.generate_signal({"token_id": "tok_s"}) is None


def test_spread_capture_generate_signal_returns_none_when_spread_collapses():
    """When the computed half-spread collapses below ``min_half_spread``,
    the strategy must skip — degenerate regime."""
    s = SpreadCapture()
    s.base_half_spread = 0.001  # below the min_half_spread floor
    s.min_half_spread = 0.005
    ctx = {
        "token_id": "tok_s", "mid": 0.50,
        "order_flow_imbalance": 0.0, "inventory": 0.0, "volatility": 0.0,
    }
    assert s.generate_signal(ctx) is None


def test_spread_capture_estimate_edge_returns_signal_edge():
    s = SpreadCapture()
    sig = Signal(action="BUY", token_id="tok", edge=0.015)
    assert s.estimate_edge(sig) == 0.015


def test_spread_capture_size_position_bounded_by_max_pct():
    s = SpreadCapture()
    s.max_position_pct = 0.05
    sig = Signal(action="BUY", token_id="tok", edge=0.015, price=0.5)
    size = s.size_position(sig, capital=100.0, risk_params={"quote_size_usdc": 5.0})
    assert 0.0 < size <= 5.0


def test_spread_capture_entry_logic_returns_post_only_limit_order():
    s = SpreadCapture()
    sig = Signal(
        action="BUY", token_id="tok_s", size=1.0, price=0.48, edge=0.015,
        metadata={
            "half_spread": 0.02, "ofi": 0.3,
            "ask_quote": {"token_id": "tok_s", "price": 0.52, "side": "SELL"},
        },
    )
    entry = s.entry_logic(sig, {"mid": 0.50})
    assert entry["type"] == "limit"
    assert entry["side"] == "BUY"
    assert entry["price"] == 0.48
    assert entry["post_only"] is True  # MM quotes are always post-only
    assert "ask_quote" in entry["metadata"]


def test_spread_capture_exit_logic_returns_none_when_inventory_low():
    s = SpreadCapture()
    pos = {"inventory_shares": 5.0, "max_inventory_shares": 100.0,
           "entry_ofi": 0.3, "entry_volatility": 0.02}
    ctx = {"volatility": 0.02, "order_flow_imbalance": 0.3}
    assert s.exit_logic(pos, ctx) is None


def test_spread_capture_exit_logic_returns_dict_when_inventory_at_cap():
    s = SpreadCapture()
    pos = {"inventory_shares": 100.0, "max_inventory_shares": 100.0,
           "entry_ofi": 0.0}
    ctx = {"volatility": 0.02, "order_flow_imbalance": 0.0}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "inventory at cap" in out["reason"]


def test_spread_capture_exit_logic_returns_dict_when_vol_too_high():
    s = SpreadCapture()
    pos = {"inventory_shares": 5.0, "max_inventory_shares": 100.0,
           "entry_ofi": 0.0, "max_volatility": 0.10}
    ctx = {"volatility": 0.20, "order_flow_imbalance": 0.0}
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "volatility" in out["reason"].lower()


def test_spread_capture_diagnostics_returns_dict_with_config():
    s = SpreadCapture()
    diag = s.diagnostics()
    assert diag["name"] == "spread_capture"
    assert "base_half_spread" in diag
    assert "skew_factor" in diag
    assert "open_quotes" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 5 — LiquidityProvision (mm_grid_liquidity)
# ═══════════════════════════════════════════════════════════════════════════

def test_liquidity_provision_implements_all_nine_contract_methods():
    s = LiquidityProvision()
    for method_name in CONTRACT_METHODS:
        method = getattr(s, method_name, None)
        assert callable(method), f"{method_name} not callable on LiquidityProvision"
        cls_method = getattr(LiquidityProvision, method_name, None)
        base_method = getattr(BaseStrategy, method_name, None)
        assert cls_method is not base_method, (
            f"{method_name} must be overridden on LiquidityProvision"
        )


def test_liquidity_provision_metadata_has_correct_fields():
    s = LiquidityProvision()
    md = s.metadata()
    assert md["name"] == "liquidity_provision"
    assert md["category"] == "market_making"
    assert "model" in md


def test_liquidity_provision_validate_default_returns_true_ok():
    s = LiquidityProvision()
    ok, msg = s.validate()
    assert ok is True
    assert msg == "OK"


def test_liquidity_provision_validate_rejects_zero_grid_levels():
    s = LiquidityProvision()
    s.grid_levels = 0
    ok, msg = s.validate()
    assert ok is False
    assert "grid_levels" in msg


def test_liquidity_provision_validate_rejects_max_step_below_min():
    s = LiquidityProvision()
    s.min_grid_step_pct = 0.05
    s.max_grid_step_pct = 0.02  # max < min
    ok, msg = s.validate()
    assert ok is False
    assert "max_grid_step_pct" in msg


def test_liquidity_provision_configure_applies_parameters():
    s = LiquidityProvision()
    s.configure({
        "grid_levels": 7,
        "grid_step_pct": 0.025,
        "level_size_usdc": 2.0,
    })
    assert s.grid_levels == 7
    assert s.grid_step_pct == 0.025
    assert s.level_size_usdc == 2.0


def test_liquidity_provision_generate_signal_returns_grid_signal():
    s = LiquidityProvision()
    ctx = {
        "token_id": "tok_l", "mid": 0.50,
        "liquidity_usdc": 1000.0,
        "mean_reversion_score": 0.70,
        "volatility": 0.02,
    }
    sig = s.generate_signal(ctx)
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"  # nearest bid
    assert sig.token_id == "tok_l"
    assert sig.price < 0.50  # bid below mid
    assert sig.metadata["grid_center"] == 0.50
    assert sig.metadata["n_bid_levels"] > 0
    assert sig.metadata["n_ask_levels"] > 0
    assert len(sig.metadata["bids"]) > 0
    assert len(sig.metadata["asks"]) > 0
    assert sig.metadata["mean_reversion_score"] == 0.70


def test_liquidity_provision_generate_signal_returns_none_when_low_liquidity():
    s = LiquidityProvision()
    ctx = {
        "token_id": "tok_l", "mid": 0.50,
        "liquidity_usdc": 50.0,  # < MIN_MARKET_LIQUIDITY_USDC=100
        "mean_reversion_score": 0.70,
    }
    assert s.generate_signal(ctx) is None


def test_liquidity_provision_generate_signal_returns_none_when_low_mr_score():
    s = LiquidityProvision()
    ctx = {
        "token_id": "tok_l", "mid": 0.50,
        "liquidity_usdc": 1000.0,
        "mean_reversion_score": 0.30,  # below 0.45
    }
    assert s.generate_signal(ctx) is None


def test_liquidity_provision_generate_signal_returns_none_when_missing_mid():
    s = LiquidityProvision()
    assert s.generate_signal({"token_id": "tok_l"}) is None


def test_liquidity_provision_estimate_edge_returns_signal_edge():
    s = LiquidityProvision()
    sig = Signal(action="BUY", token_id="tok", edge=0.10)
    assert s.estimate_edge(sig) == 0.10


def test_liquidity_provision_size_position_uses_grid_level_count():
    s = LiquidityProvision()
    s.level_size_usdc = 1.0
    sig = Signal(
        action="BUY", token_id="tok", edge=0.10,
        metadata={"n_bid_levels": 5, "n_ask_levels": 5, "level_size_usdc": 1.0},
    )
    size = s.size_position(sig, capital=1000.0, risk_params={})
    # level_size × n_levels = 1.0 × 10 = 10.0
    assert size == 10.0


def test_liquidity_provision_entry_logic_returns_post_only_limit_order():
    s = LiquidityProvision()
    sig = Signal(
        action="BUY", token_id="tok_l", size=1.0, price=0.48, edge=0.05,
        metadata={
            "grid_center": 0.50, "grid_step": 0.02,
            "bids": [{"level": 1, "price": 0.48, "side": "BUY"}],
            "asks": [{"level": 1, "price": 0.52, "side": "SELL"}],
            "level_size_usdc": 1.0,
        },
    )
    entry = s.entry_logic(sig, {"mid": 0.50})
    assert entry["type"] == "limit"
    assert entry["side"] == "BUY"
    assert entry["price"] == 0.48
    assert entry["post_only"] is True
    assert "bids" in entry["metadata"]
    assert "asks" in entry["metadata"]


def test_liquidity_provision_exit_logic_returns_none_when_mr_still_high():
    s = LiquidityProvision()
    pos = {"entry_volatility": 0.02, "grid_center": 0.50, "grid_step": 0.02}
    ctx = {
        "mean_reversion_score": 0.80, "volatility": 0.02, "mid": 0.50,
    }
    assert s.exit_logic(pos, ctx) is None


def test_liquidity_provision_exit_logic_returns_dict_when_mr_drops():
    s = LiquidityProvision()
    pos = {"entry_volatility": 0.02, "grid_center": 0.50, "grid_step": 0.02}
    ctx = {"mean_reversion_score": 0.20}  # below 0.45
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "trending" in out["reason"]


def test_liquidity_provision_exit_logic_returns_dict_when_grid_misaligned():
    s = LiquidityProvision()
    pos = {"entry_volatility": 0.02, "grid_center": 0.50, "grid_step": 0.02}
    ctx = {
        "mean_reversion_score": 0.80,
        "volatility": 0.02,
        "mid": 0.55,  # > 1 step from 0.50 center
    }
    out = s.exit_logic(pos, ctx)
    assert out is not None
    assert "re-center" in out["reason"]


def test_liquidity_provision_diagnostics_returns_dict_with_config():
    s = LiquidityProvision()
    diag = s.diagnostics()
    assert diag["name"] == "liquidity_provision"
    assert "grid_levels" in diag
    assert "grid_step_pct" in diag
    assert "open_grids" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 6 — Catalog size invariant (no regressions)
# ═══════════════════════════════════════════════════════════════════════════

def test_catalog_size_remains_50():
    """The catalog must still have exactly 50 entries — the W22-3 changes
    PROMOTE five existing PLANNED entries to IMPLEMENTED, they do not
    ADD new rows."""
    assert len(STRATEGY_CATALOG) == 50


def test_no_strategy_left_in_both_states():
    """Every catalog entry is either IMPLEMENTED or PLANNED — never both.
    Also: the 5 W22-3 entries must be IMPLEMENTED; the 39 others must
    be PLANNED."""
    impl = [s for s in STRATEGY_CATALOG if s.status == STATUS_IMPLEMENTED]
    planned = [s for s in STRATEGY_CATALOG if s.status != STATUS_IMPLEMENTED]
    assert len(impl) == 11
    assert len(planned) == 39
    # No id appears in both sets.
    impl_ids = {s.strategy_id for s in impl}
    planned_ids = {s.strategy_id for s in planned}
    assert impl_ids.isdisjoint(planned_ids)
