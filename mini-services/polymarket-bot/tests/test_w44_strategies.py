"""
tests/test_w44_strategies.py — W44-1 New Strategies Contract Tests.

W44-1 — verifies that the five new strategies promoted from PLANNED
to IMPLEMENTED in this wave each implement the 9-method
``StrategyContract`` ABC with real (non-stub) logic, and that the
``generate_signal`` / ``validate`` / ``configure`` methods behave
correctly under controlled inputs.

Scope (5 strategy classes × 9 contract methods + behavioral tests):

  LateResolution       (arb_temporal_expiry)        — decay-curve trader
  Ensemble             (ml_fractional_kelly)        — multi-strategy aggregator
  NewsTrader           (event_poll_discrepancy)     — polling-discrepancy trader
  SentimentAggregator  (event_social_volume)        — social-sentiment z-score
  CrossMarket          (arb_cluster_dislocation)    — cluster-dislocation trader

Test groups (per strategy):
  (1) All 9 contract methods are present and callable
      (metadata, configure, validate, generate_signal, estimate_edge,
       size_position, entry_logic, exit_logic, diagnostics).
  (2) ``metadata()`` returns a dict with name/version/description/author.
  (3) ``validate()`` returns (True, "OK") with default parameters.
  (4) ``configure({...})`` updates the typed config fields.
  (5) ``generate_signal()`` returns None when required inputs are missing.
  (6) ``generate_signal()`` returns a Signal of the expected action
      (BUY/SELL) under controlled market_context inputs.
  (7) ``estimate_edge()`` returns the signal's edge.
  (8) ``size_position()`` returns a non-negative float bounded by capital.
  (9) ``entry_logic()`` returns a dict with the documented keys.
  (10) ``exit_logic()`` returns None or a dict (never raises).
  (11) ``diagnostics()`` returns a dict carrying the strategy's name.

Plus a registry wiring test asserting the catalog reports 16
IMPLEMENTED and 34 PLANNED entries (5 W44-1 promotions).

Approach
--------
The strategy classes are imported under the same env-var redirect
bootstrap used by every sibling test module (``tests/conftest.py``
+ ``tests/test_strategy_base.py``). The contract methods are SYNC
(no ``await``) — they're designed for introspection from sync
contexts (FastAPI request handlers, REPL, backtest replay) — so the
test functions are plain ``def test_...`` (not ``async def``). This
also verifies the design invariant: no contract method needs an
event loop.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` and
# ``tests/test_strategy_stubs.py``. ``setdefault`` means we never clobber
# a path conftest already set.
_TMP_ROOT = Path("/tmp/w44_strategies_tests")
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
    "API_TOKEN": "test-token-w44-strategies",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# regardless of the cwd pytest was launched from. Mirrors the bootstrap
# pattern in every sibling ``tests/test_*.py`` module.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from strategies.base import Signal  # noqa: E402
from strategies.cross_market import CrossMarket  # noqa: E402
from strategies.ensemble import Ensemble  # noqa: E402
from strategies.late_resolution import LateResolution  # noqa: E402
from strategies.news import NewsTrader  # noqa: E402
from strategies.sentiment import SentimentAggregator  # noqa: E402
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    STRATEGY_CATALOG,
    StrategyRegistry,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. ``asyncio_mode=strict`` (the pytest-asyncio default) requires
# this. We have no async tests in this module (the contract surface is
# SYNC), but the mark is harmless and keeps collection consistent with
# every sibling test module.
pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════════
# Constants — the 5 new (strategy_id, class) tuples promoted in W44-1.
# ═══════════════════════════════════════════════════════════════════════════

W44_STRATEGY_CLASSES = [
    ("arb_temporal_expiry", LateResolution),
    ("ml_fractional_kelly", Ensemble),
    ("event_poll_discrepancy", NewsTrader),
    ("event_social_volume", SentimentAggregator),
    ("arb_cluster_dislocation", CrossMarket),
]

# The 9 contract methods every strategy must implement.
CONTRACT_METHODS = [
    "metadata",
    "configure",
    "validate",
    "generate_signal",
    "estimate_edge",
    "size_position",
    "entry_logic",
    "exit_logic",
    "diagnostics",
]


# ═══════════════════════════════════════════════════════════════════════════
# Section 1 — Registry wiring: 16 IMPLEMENTED, 34 PLANNED.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def registry():
    """Fresh ``StrategyRegistry`` per test (no singleton state leak)."""
    return StrategyRegistry()


def test_registry_catalog_size_is_50(registry):
    """The catalog must carry exactly 50 entries — no rows added or
    removed by the W44-1 promotion."""
    catalog = registry.get_catalog()
    assert len(catalog) == 50
    assert len(catalog) == len(STRATEGY_CATALOG)


def test_registry_catalog_has_16_implemented(registry):
    """3 + 3 + 5 + 5 = 16 IMPLEMENTED entries after the W44-1 wave."""
    catalog = registry.get_catalog()
    implemented = [r for r in catalog if r["status"] == STATUS_IMPLEMENTED]
    assert len(implemented) == 16


def test_registry_catalog_has_34_planned(registry):
    """50 − 16 = 34 PLANNED stubs after the W44-1 wave."""
    catalog = registry.get_catalog()
    planned = [r for r in catalog if r["status"] == STATUS_PLANNED]
    assert len(planned) == 34


def test_w44_strategy_ids_are_implemented(registry):
    """Each of the five W44-1 catalog ids must report
    ``status == IMPLEMENTED``."""
    catalog = registry.get_catalog()
    by_id = {r["strategy_id"]: r for r in catalog}
    for sid, _cls in W44_STRATEGY_CLASSES:
        assert sid in by_id, f"missing catalog entry for {sid}"
        assert by_id[sid]["status"] == STATUS_IMPLEMENTED, (
            f"{sid} must be IMPLEMENTED after W44-1"
        )
        assert by_id[sid]["implemented"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Per-strategy: 9-method contract surface.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_strategy_implements_all_9_contract_methods(strategy_id, strategy_cls):
    """Each W44-1 strategy class must expose all 9 contract methods as
    callable attributes. (BaseStrategy already provides default
    implementations, but each W44-1 strategy overrides them with real
    logic — the test just checks the attributes exist and are callable.)"""
    inst = strategy_cls()
    for method_name in CONTRACT_METHODS:
        method = getattr(inst, method_name, None)
        assert callable(method), (
            f"{strategy_cls.__name__}.{method_name} must be callable"
        )


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_metadata_returns_documented_shape(strategy_id, strategy_cls):
    """``metadata()`` must return a dict with at least name, version,
    description, author — the documented contract surface."""
    inst = strategy_cls()
    md = inst.metadata()
    assert isinstance(md, dict)
    for key in ("name", "version", "description", "author"):
        assert key in md, (
            f"{strategy_cls.__name__}.metadata() must include {key!r}"
        )
    assert md["name"] == inst.name


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_validate_returns_true_with_defaults(strategy_id, strategy_cls):
    """``validate()`` returns ``(True, "OK")`` for the default parameter
    set (every W44-1 strategy ships with sensible defaults)."""
    inst = strategy_cls()
    is_valid, msg = inst.validate()
    assert is_valid is True
    assert msg == "OK"


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_configure_updates_typed_fields(strategy_id, strategy_cls):
    """``configure({...})`` merges the supplied dict into ``self.config``
    AND updates the typed instance fields. The strategy must apply at
    least one override and survive a fresh ``validate()``."""
    inst = strategy_cls()
    # Use a config key shared by every W44-1 strategy: ``scan_interval``
    # and ``max_position_pct``. ``max_position_pct`` is also validated,
    # so this confirms configure() actually mutates the typed field.
    inst.configure({"scan_interval": 99.0, "max_position_pct": 0.05})
    assert inst._interval == 99.0
    assert inst.max_position_pct == 0.05
    # The config dict itself must carry the supplied overrides.
    assert inst.config.get("scan_interval") == 99.0
    assert inst.config.get("max_position_pct") == 0.05
    # A fresh validate must still pass (the override is in the valid range).
    is_valid, _ = inst.validate()
    assert is_valid is True


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_generate_signal_returns_none_for_empty_context(strategy_id, strategy_cls):
    """An empty ``market_context`` dict must yield ``None`` — the
    strategy must not fire on missing inputs."""
    inst = strategy_cls()
    sig = inst.generate_signal({})
    assert sig is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_estimate_edge_returns_signal_edge(strategy_id, strategy_cls):
    """``estimate_edge(signal)`` must return the signal's pre-computed
    edge (or 0.0 for ``None``)."""
    inst = strategy_cls()
    # None-signal contract: must return 0.0 (defensive — never raises).
    assert inst.estimate_edge(None) == 0.0
    # Real signal: edge is surfaced unchanged.
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.5,
                  confidence=0.7, edge=0.123)
    assert inst.estimate_edge(fake) == 0.123


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_size_position_is_bounded_by_capital(strategy_id, strategy_cls):
    """``size_position`` must return a non-negative float that does not
    exceed the supplied capital."""
    inst = strategy_cls()
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.5,
                  confidence=0.7, edge=0.05)
    capital = 1000.0
    size = inst.size_position(fake, capital, {})
    assert isinstance(size, (int, float))
    assert 0.0 <= size <= capital
    # HOLD signal: size must be 0.0.
    hold = Signal(action="HOLD", token_id="t", size=0.0, confidence=0.0,
                  edge=0.0)
    assert inst.size_position(hold, capital, {}) == 0.0
    # None signal: size must be 0.0 (defensive — never raises).
    assert inst.size_position(None, capital, {}) == 0.0


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_entry_logic_returns_dict_with_keys(strategy_id, strategy_cls):
    """``entry_logic`` returns a dict with at least ``price`` and
    ``type`` keys for actionable signals; a ``skip`` flag for HOLD."""
    inst = strategy_cls()
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.55,
                  confidence=0.7, edge=0.05)
    entry = inst.entry_logic(fake, {"mid": 0.50})
    assert isinstance(entry, dict)
    assert "price" in entry
    assert entry.get("type") == "limit"
    # HOLD signal: must include a ``skip`` flag.
    hold_entry = inst.entry_logic(
        Signal(action="HOLD", token_id="t", size=0.0, confidence=0.0, edge=0.0),
        {},
    )
    assert isinstance(hold_entry, dict)
    assert hold_entry.get("skip") is True


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_exit_logic_returns_none_or_dict(strategy_id, strategy_cls):
    """``exit_logic`` must return either ``None`` (no exit decision) or
    a dict carrying a ``reason`` key. It must never raise on empty
    inputs."""
    inst = strategy_cls()
    # Empty position: must return None (defensive — never raises).
    assert inst.exit_logic({}, {}) is None
    assert inst.exit_logic(None, {}) is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W44_STRATEGY_CLASSES)
def test_diagnostics_returns_dict_with_name(strategy_id, strategy_cls):
    """``diagnostics()`` returns a dict carrying at least ``name`` and
    ``stats`` (the base-class shape); the strategy may add more."""
    inst = strategy_cls()
    diag = inst.diagnostics()
    assert isinstance(diag, dict)
    assert diag.get("name") == inst.name
    assert "stats" in diag


# ═══════════════════════════════════════════════════════════════════════════
# Section 3 — LateResolution (arb_temporal_expiry) behavioral tests.
# ═══════════════════════════════════════════════════════════════════════════

def test_late_resolution_buy_when_mid_below_fair():
    """When the observed mid is below the modeled decay-curve fair
    value (market under-pricing a near-certain outcome), the strategy
    must emit a BUY signal."""
    strat = LateResolution()
    # certainty=0.95, hours_left=2.0 (near resolution), mid=0.50.
    # The logistic fair at hours=2 with half_life=6, k=0.5 is:
    #   0.95 / (1 + exp(-0.5 * (2 - 6))) = 0.95 / (1 + exp(2)) ≈ 0.95 / 8.39
    #   ≈ 0.113. mid=0.50 > fair=0.113 → SELL.
    # To trigger BUY, use mid << fair: mid=0.05 << fair=0.113.
    sig = strat.generate_signal({
        "token_id": "0xlate_test_buy",
        "mid": 0.05,
        "hours_to_resolution": 2.0,
        "resolution_certainty": 0.95,
        "spread": 0.01,
        "liquidity_usdc": 500.0,
    })
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    assert sig.token_id == "0xlate_test_buy"
    assert sig.edge > 0
    assert sig.metadata["direction"] == "long_underpriced_outcome"
    assert sig.metadata["modeled_fair"] > 0


def test_late_resolution_sell_when_mid_above_fair():
    """When the observed mid is above the modeled decay-curve fair
    value (market over-pricing), the strategy must emit a SELL."""
    strat = LateResolution()
    # certainty=0.90, hours_left=2.0, mid=0.50. fair ≈ 0.90 / (1 + exp(2))
    # ≈ 0.107. mid=0.50 > fair=0.107 → SELL.
    sig = strat.generate_signal({
        "token_id": "0xlate_test_sell",
        "mid": 0.50,
        "hours_to_resolution": 2.0,
        "resolution_certainty": 0.90,
        "spread": 0.01,
        "liquidity_usdc": 500.0,
    })
    assert sig is not None
    assert sig.action == "SELL"
    assert sig.metadata["direction"] == "short_overpriced_outcome"


def test_late_resolution_returns_none_outside_window():
    """When ``hours_to_resolution > max_hours_to_resolution`` the
    strategy must skip (only act in the final 72h window)."""
    strat = LateResolution()
    sig = strat.generate_signal({
        "token_id": "0xlate_test_none",
        "mid": 0.50,
        "hours_to_resolution": 200.0,  # > 72h window
        "resolution_certainty": 0.95,
    })
    assert sig is None


def test_late_resolution_returns_none_when_certainty_below_floor():
    """``resolution_certainty < min_resolution_certainty`` (0.70) ⇒ skip."""
    strat = LateResolution()
    sig = strat.generate_signal({
        "token_id": "0xlate_test_low_cert",
        "mid": 0.10,
        "hours_to_resolution": 2.0,
        "resolution_certainty": 0.50,  # < 0.70 floor
    })
    assert sig is None


def test_late_resolution_returns_none_when_gap_below_min_edge():
    """When ``|mid - fair| < min_edge`` (2.5%) the strategy must skip —
    the gap is too small to overcome spread + fees + slippage."""
    strat = LateResolution()
    # Pick a mid equal to the modeled fair (zero gap) → must skip.
    # First compute fair for a known set of inputs, then use that exact
    # mid. certainty=0.90, hours_left=24 (long way out) ⇒ fair is very
    # small. mid=0.50 will be much greater than fair ⇒ SELL.
    # Instead use mid close to fair to trigger the min_edge guard.
    # certainty=0.90, hours_left=12 (mid-decay): fair = 0.90 / (1 + exp(-0.5*6))
    # = 0.90 / (1 + exp(-3)) ≈ 0.90 / 1.05 ≈ 0.857. mid=0.86 ⇒ gap ≈ 0.003.
    sig = strat.generate_signal({
        "token_id": "0xlate_test_min_edge",
        "mid": 0.86,
        "hours_to_resolution": 12.0,
        "resolution_certainty": 0.90,
        "spread": 0.01,
        "liquidity_usdc": 500.0,
    })
    assert sig is None


def test_late_resolution_validate_rejects_invalid_params():
    """``validate()`` must return ``(False, ...)`` for out-of-range
    parameters."""
    strat = LateResolution()
    strat.max_hours_to_resolution = -1.0  # invalid: must be > 0
    is_valid, msg = strat.validate()
    assert is_valid is False
    assert "max_hours_to_resolution" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Section 4 — Ensemble (ml_fractional_kelly) behavioral tests.
# ═══════════════════════════════════════════════════════════════════════════

def test_ensemble_buy_when_buy_mass_dominates():
    """When the weighted BUY mass exceeds the SELL mass by ≥ the vote
    margin, the strategy must emit a BUY signal."""
    strat = Ensemble()
    sig = strat.generate_signal({
        "token_id": "0xens_test_buy",
        "mid": 0.50,
        "spread": 0.01,
        "sub_signals": [
            {"action": "BUY", "edge": 0.04, "confidence": 0.7, "weight": 1.0},
            {"action": "BUY", "edge": 0.05, "confidence": 0.6, "weight": 1.0},
            {"action": "SELL", "edge": 0.02, "confidence": 0.55, "weight": 1.0},
        ],
    })
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.token_id == "0xens_test_buy"
    assert sig.edge > 0
    assert sig.metadata["vote_margin"] > strat.min_vote_margin


def test_ensemble_sell_when_sell_mass_dominates():
    """Symmetric case — SELL mass dominates ⇒ SELL signal."""
    strat = Ensemble()
    sig = strat.generate_signal({
        "token_id": "0xens_test_sell",
        "mid": 0.50,
        "spread": 0.01,
        "sub_signals": [
            {"action": "SELL", "edge": 0.04, "confidence": 0.7, "weight": 1.0},
            {"action": "SELL", "edge": 0.05, "confidence": 0.6, "weight": 1.0},
            {"action": "BUY", "edge": 0.02, "confidence": 0.55, "weight": 1.0},
        ],
    })
    assert sig is not None
    assert sig.action == "SELL"


def test_ensemble_returns_none_when_margin_below_threshold():
    """When the weighted vote margin is below ``min_vote_margin`` the
    strategy must skip (no consensus)."""
    strat = Ensemble()
    sig = strat.generate_signal({
        "token_id": "0xens_test_no_margin",
        "mid": 0.50,
        "spread": 0.01,
        # 2 BUY + 2 SELL with equal weights & confidences → margin ≈ 0.
        "sub_signals": [
            {"action": "BUY", "edge": 0.04, "confidence": 0.6, "weight": 1.0},
            {"action": "BUY", "edge": 0.04, "confidence": 0.6, "weight": 1.0},
            {"action": "SELL", "edge": 0.04, "confidence": 0.6, "weight": 1.0},
            {"action": "SELL", "edge": 0.04, "confidence": 0.6, "weight": 1.0},
        ],
    })
    assert sig is None


def test_ensemble_returns_none_with_too_few_sub_signals():
    """``sub_signals`` below ``min_sub_signals`` (default 2) ⇒ skip."""
    strat = Ensemble()
    sig = strat.generate_signal({
        "token_id": "0xens_test_one_sig",
        "mid": 0.50,
        "spread": 0.01,
        "sub_signals": [
            {"action": "BUY", "edge": 0.05, "confidence": 0.9, "weight": 1.0},
        ],
    })
    assert sig is None


def test_ensemble_returns_none_when_sub_signals_low_confidence():
    """Sub-signals below ``min_confidence`` (0.40) are filtered out;
    if that drops the qualifying count below ``min_sub_signals``,
    skip."""
    strat = Ensemble()
    sig = strat.generate_signal({
        "token_id": "0xens_test_low_conf",
        "mid": 0.50,
        "spread": 0.01,
        "sub_signals": [
            {"action": "BUY", "edge": 0.10, "confidence": 0.30, "weight": 1.0},
            {"action": "BUY", "edge": 0.10, "confidence": 0.30, "weight": 1.0},
        ],
    })
    assert sig is None


def test_ensemble_validate_rejects_invalid_kelly_fraction():
    """``kelly_fraction`` outside (0, 1] must fail validate()."""
    strat = Ensemble()
    strat.kelly_fraction = 0.0  # invalid: must be > 0
    is_valid, msg = strat.validate()
    assert is_valid is False
    assert "kelly_fraction" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Section 5 — NewsTrader (event_poll_discrepancy) behavioral tests.
# ═══════════════════════════════════════════════════════════════════════════

def test_news_buy_when_poll_above_mid_plus_moe():
    """When ``poll_probability - mid > moe + min_edge_buffer`` the
    strategy must emit a BUY (market under-prices YES)."""
    strat = NewsTrader()
    # poll=0.65, mid=0.50, moe=0.03, buffer=0.025 → gap=0.15, threshold=0.055.
    sig = strat.generate_signal({
        "token_id": "0xnews_test_buy",
        "mid": 0.50,
        "poll_probability": 0.65,
        "poll_margin_of_error": 0.03,
        "poll_sample_size": 1500,
        "poll_freshness_hours": 12.0,
        "spread": 0.01,
    })
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.token_id == "0xnews_test_buy"
    assert sig.edge > 0
    assert sig.metadata["direction"] == "long_yes_underpriced"


def test_news_sell_when_poll_below_mid_minus_moe():
    """Symmetric case — poll < mid - moe - buffer ⇒ SELL."""
    strat = NewsTrader()
    sig = strat.generate_signal({
        "token_id": "0xnews_test_sell",
        "mid": 0.65,
        "poll_probability": 0.50,
        "poll_margin_of_error": 0.03,
        "poll_sample_size": 1500,
        "poll_freshness_hours": 12.0,
        "spread": 0.01,
    })
    assert sig is not None
    assert sig.action == "SELL"


def test_news_returns_none_when_gap_inside_moe():
    """When ``|poll - mid| <= moe + buffer`` the gap is inside the
    polling margin of error — statistically indistinguishable from
    no gap, so the strategy must skip."""
    strat = NewsTrader()
    sig = strat.generate_signal({
        "token_id": "0xnews_test_inside_moe",
        "mid": 0.50,
        "poll_probability": 0.52,  # gap = 0.02, threshold = 0.03 + 0.025 = 0.055
        "poll_margin_of_error": 0.03,
        "poll_sample_size": 1500,
        "poll_freshness_hours": 12.0,
        "spread": 0.01,
    })
    assert sig is None


def test_news_returns_none_when_sample_too_small():
    """``poll_sample_size < min_sample_size`` (500) ⇒ skip (small-sample
    polls are noise)."""
    strat = NewsTrader()
    sig = strat.generate_signal({
        "token_id": "0xnews_test_small_sample",
        "mid": 0.50,
        "poll_probability": 0.80,
        "poll_margin_of_error": 0.03,
        "poll_sample_size": 100,  # < 500 floor
        "poll_freshness_hours": 12.0,
        "spread": 0.01,
    })
    assert sig is None


def test_news_returns_none_when_poll_too_stale():
    """``poll_freshness_hours > max_poll_age_hours`` (72h) ⇒ skip."""
    strat = NewsTrader()
    sig = strat.generate_signal({
        "token_id": "0xnews_test_stale",
        "mid": 0.50,
        "poll_probability": 0.80,
        "poll_margin_of_error": 0.03,
        "poll_sample_size": 1500,
        "poll_freshness_hours": 200.0,  # > 72h
        "spread": 0.01,
    })
    assert sig is None


def test_news_validate_rejects_invalid_max_moe():
    """``max_moe`` outside (0, 0.5] must fail validate()."""
    strat = NewsTrader()
    strat.max_moe = 0.75  # invalid: must be ≤ 0.5
    is_valid, msg = strat.validate()
    assert is_valid is False
    assert "max_moe" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Section 6 — SentimentAggregator (event_social_volume) behavioral tests.
# ═══════════════════════════════════════════════════════════════════════════

def test_sentiment_buy_when_bullish_zscore_exceeds_threshold():
    """When the current sentiment z-score against its rolling baseline
    exceeds ``+z_score_threshold`` (2.0σ), the strategy must emit BUY."""
    strat = SentimentAggregator()
    # Baseline mean=0.0, std=0.05, current=0.20 → z=4.0 (> 2.0).
    sig = strat.generate_signal({
        "token_id": "0xsent_test_buy",
        "mid": 0.50,
        "current_sentiment": 0.20,
        "baseline_sentiment": 0.0,
        "baseline_std": 0.05,
        "mention_count": 500,
        "source_count": 3,
        "spread": 0.01,
    })
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.token_id == "0xsent_test_buy"
    assert sig.edge > 0
    assert sig.metadata["z_score"] > strat.z_score_threshold


def test_sentiment_sell_when_bearish_zscore_exceeds_threshold():
    """Symmetric case — current z < -2.0σ ⇒ SELL."""
    strat = SentimentAggregator()
    sig = strat.generate_signal({
        "token_id": "0xsent_test_sell",
        "mid": 0.50,
        "current_sentiment": -0.20,
        "baseline_sentiment": 0.0,
        "baseline_std": 0.05,
        "mention_count": 500,
        "source_count": 3,
        "spread": 0.01,
    })
    assert sig is not None
    assert sig.action == "SELL"


def test_sentiment_returns_none_when_zscore_inside_action_band():
    """When ``|z| < z_score_threshold`` the sentiment shift is inside
    the action band — no significant move, skip."""
    strat = SentimentAggregator()
    sig = strat.generate_signal({
        "token_id": "0xsent_test_no_signal",
        "mid": 0.50,
        "current_sentiment": 0.05,  # z=1.0 with baseline 0±0.05 → < 2.0
        "baseline_sentiment": 0.0,
        "baseline_std": 0.05,
        "mention_count": 500,
        "source_count": 3,
        "spread": 0.01,
    })
    assert sig is None


def test_sentiment_returns_none_when_too_few_sources():
    """``source_count < min_source_count`` (2) ⇒ skip (single-source
    sentiment is unreliable)."""
    strat = SentimentAggregator()
    sig = strat.generate_signal({
        "token_id": "0xsent_test_one_source",
        "mid": 0.50,
        "current_sentiment": 0.30,
        "baseline_sentiment": 0.0,
        "baseline_std": 0.05,
        "mention_count": 500,
        "source_count": 1,  # < 2 floor
        "spread": 0.01,
    })
    assert sig is None


def test_sentiment_returns_none_when_sentiment_below_noise_floor():
    """``|current_sentiment| < min_sentiment_magnitude`` (0.15) ⇒ skip
    (pure noise — no actionable signal)."""
    strat = SentimentAggregator()
    sig = strat.generate_signal({
        "token_id": "0xsent_test_noise",
        "mid": 0.50,
        "current_sentiment": 0.05,  # < 0.15 noise floor
        "baseline_sentiment": 0.0,
        "baseline_std": 0.005,  # tiny std so z > 2
        "mention_count": 500,
        "source_count": 3,
        "spread": 0.01,
    })
    assert sig is None


def test_sentiment_validate_rejects_invalid_z_threshold():
    """``z_score_threshold <= 0`` must fail validate()."""
    strat = SentimentAggregator()
    strat.z_score_threshold = 0.0  # invalid
    is_valid, msg = strat.validate()
    assert is_valid is False
    assert "z_score_threshold" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Section 7 — CrossMarket (arb_cluster_dislocation) behavioral tests.
# ═══════════════════════════════════════════════════════════════════════════

def test_cross_market_buy_when_member_underpriced():
    """When the most-dislocated cluster member is BELOW the cluster
    mean (under-priced, z < 0), the strategy must emit BUY."""
    strat = CrossMarket()
    # 3-member cluster: mids 0.50, 0.50, 0.30. mean=0.433, std=0.094.
    # member 3 z = (0.30 - 0.433) / 0.094 ≈ -1.42 (< 1.5 threshold).
    # Make the dislocation bigger: mids 0.50, 0.50, 0.20.
    # mean=0.40, std=0.1414, member 3 z = (0.20 - 0.40) / 0.1414 ≈ -1.41.
    # Still under 1.5 — use mids 0.55, 0.55, 0.20:
    # mean=0.4333, std=0.1610, member 3 z = (0.20 - 0.4333) / 0.1610 ≈ -1.45.
    # Need |z| ≥ 1.5. Use mids 0.60, 0.60, 0.20:
    # mean=0.4667, std=0.1886, member 3 z = (0.20 - 0.4667) / 0.1886 ≈ -1.41.
    # Hmm. Let me try mids 0.80, 0.80, 0.20:
    # mean=0.60, std=0.2828, member 3 z = (0.20 - 0.60) / 0.2828 ≈ -1.41.
    # The |z| of the most-dislocated member of a 3-element set with
    # 2 high + 1 low is bounded at ~1.41σ (since one outlier in n=3
    # has max |z| = sqrt(n/(n-1)) = sqrt(3/2) ≈ 1.41). Need a 4th member.
    sig = strat.generate_signal({
        "cluster_members": [
            {"token_id": "0xcm_1", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_2", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_3", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_4", "mid": 0.20, "spread": 0.01},
        ],
        "cluster_correlation": 0.80,
    })
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.token_id == "0xcm_4"  # most-dislocated member
    assert sig.metadata["target_z_score"] < 0  # under-priced (negative z)
    assert sig.metadata["cluster_size"] == 4


def test_cross_market_sell_when_member_overpriced():
    """Symmetric case — the dislocated member is ABOVE the cluster
    mean (over-priced, z > 0) ⇒ SELL."""
    strat = CrossMarket()
    sig = strat.generate_signal({
        "cluster_members": [
            {"token_id": "0xcm_1", "mid": 0.20, "spread": 0.01},
            {"token_id": "0xcm_2", "mid": 0.20, "spread": 0.01},
            {"token_id": "0xcm_3", "mid": 0.20, "spread": 0.01},
            {"token_id": "0xcm_4", "mid": 0.80, "spread": 0.01},
        ],
        "cluster_correlation": 0.80,
    })
    assert sig is not None
    assert sig.action == "SELL"
    assert sig.token_id == "0xcm_4"
    assert sig.metadata["target_z_score"] > 0


def test_cross_market_returns_none_with_too_few_members():
    """``len(cluster_members) < min_cluster_size`` (3) ⇒ skip."""
    strat = CrossMarket()
    sig = strat.generate_signal({
        "cluster_members": [
            {"token_id": "0xcm_1", "mid": 0.80, "spread": 0.01},
            {"token_id": "0xcm_2", "mid": 0.20, "spread": 0.01},
        ],
        "cluster_correlation": 0.80,
    })
    assert sig is None


def test_cross_market_returns_none_when_correlation_below_floor():
    """``cluster_correlation < min_cluster_correlation`` (0.55) ⇒ skip
    (cluster isn't tightly coupled enough to trade the dislocation)."""
    strat = CrossMarket()
    sig = strat.generate_signal({
        "cluster_members": [
            {"token_id": "0xcm_1", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_2", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_3", "mid": 0.70, "spread": 0.01},
            {"token_id": "0xcm_4", "mid": 0.20, "spread": 0.01},
        ],
        "cluster_correlation": 0.30,  # < 0.55 floor
    })
    assert sig is None


def test_cross_market_returns_none_when_cluster_aligned():
    """When all cluster members have the same mid, σ=0 ⇒ skip (the
    cluster is perfectly aligned, no dislocation to trade)."""
    strat = CrossMarket()
    sig = strat.generate_signal({
        "cluster_members": [
            {"token_id": "0xcm_1", "mid": 0.50, "spread": 0.01},
            {"token_id": "0xcm_2", "mid": 0.50, "spread": 0.01},
            {"token_id": "0xcm_3", "mid": 0.50, "spread": 0.01},
            {"token_id": "0xcm_4", "mid": 0.50, "spread": 0.01},
        ],
        "cluster_correlation": 0.80,
    })
    assert sig is None


def test_cross_market_validate_rejects_invalid_cluster_size():
    """``min_cluster_size < 3`` must fail validate() — a pair is a
    stat-arb, not a cluster."""
    strat = CrossMarket()
    strat.min_cluster_size = 2  # invalid
    is_valid, msg = strat.validate()
    assert is_valid is False
    assert "min_cluster_size" in msg
