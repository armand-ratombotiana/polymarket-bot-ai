"""
tests/test_w46_strategies.py — W46-1 Full IMPLEMENTED Catalog Tests.

W46-1 — verifies that every prior PLANNED stub has been promoted to
IMPLEMENTED in this wave. Each of the 34 strategies promoted from
PLANNED to IMPLEMENTED in W46-1 ships a concrete ``BaseStrategy``
subclass that implements the 9-method ``StrategyContract`` ABC plus
a real ``_run`` async trading loop.

Scope
-----
Section 1 — Registry wiring (catalog-wide invariants):
  * Catalog size is exactly 50 entries.
  * 50 strategies report ``status == IMPLEMENTED``.
  * 0 strategies report ``status == PLANNED``.
  * The ``?implemented_only=true`` filter returns all 50 rows.
  * Every IMPLEMENTED strategy has a concrete class mapping in
    ``_IMPLEMENTED_STRATEGY_CLASSES``.

Section 2 — Per-strategy 9-method contract (parametrised × 34):
  For each of the 34 strategies promoted in W46-1:
    (1) The class exposes all 9 contract methods (metadata, configure,
        validate, generate_signal, estimate_edge, size_position,
        entry_logic, exit_logic, diagnostics) as callables.
    (2) ``metadata()`` returns a dict with name / version / description /
        author keys, and ``md["name"] == inst.name``.
    (3) ``validate()`` returns ``(True, "OK")`` with default parameters.
    (4) ``generate_signal({})`` returns ``None`` on empty market_context
        (defensive — every strategy must short-circuit on missing inputs).
    (5) ``estimate_edge(None)`` returns ``0.0``; ``estimate_edge(signal)``
        returns ``signal.edge``.
    (6) ``size_position(None, capital, {})`` returns ``0.0``;
        ``size_position(hold_signal, capital, {})`` returns ``0.0``;
        ``size_position(buy_signal, capital, {})`` returns a float
        in ``[0.0, capital]``.
    (7) ``entry_logic(buy_signal, ctx)`` returns a dict carrying at
        least a ``price`` key; ``entry_logic(hold_signal, {})`` returns
        a dict carrying ``{"skip": True}`` or a similar skip flag.
    (8) ``exit_logic({}, {})`` returns ``None``; ``exit_logic(None, {})``
        returns ``None`` (defensive — never raises).
    (9) ``diagnostics()`` returns a dict carrying at least the ``name``
        key (BaseStrategy default merges ``self.name``).

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
_TMP_ROOT = Path("/tmp/w46_strategies_tests")
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
    "API_TOKEN": "test-token-w46-strategies",
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

from strategies.arb_cyclic_triangle import CyclicTriangleArb  # noqa: E402
from strategies.arb_gamma_clob_parity import GammaClobParityArb  # noqa: E402
from strategies.arb_multi_negative_risk import NegativeRiskMultiArb  # noqa: E402
from strategies.arb_synthetic_straddle import SyntheticStraddleArb  # noqa: E402
from strategies.base import BaseStrategy, Signal  # noqa: E402
from strategies.event_election_momentum import ElectionMomentumTracker  # noqa: E402
from strategies.event_macro_straddle import MacroStraddleTrader  # noqa: E402
from strategies.event_oracle_dispute import OracleDisputeSniper  # noqa: E402
from strategies.event_whale_follower import WhaleFollower  # noqa: E402
from strategies.ml_bayesian_belief import BayesianBeliefUpdater  # noqa: E402
from strategies.ml_gmm_regime_switch import GmmRegimeSwitch  # noqa: E402
from strategies.ml_lightgbm_boost import LightGBMBoost  # noqa: E402
from strategies.ml_online_sgd_learner import OnlineSgdLearner  # noqa: E402
from strategies.ml_qlearning_execution import QLearningExecutionAgent  # noqa: E402
from strategies.ml_svm_hyperplane import SvmHyperplaneClassifier  # noqa: E402
from strategies.ml_xgboost_directional import XGBoostDirectional  # noqa: E402
from strategies.mm_glft_optimal import GlftOptimalQuoter  # noqa: E402
from strategies.mm_ofi_microstructure import OfiMicrostructureMM  # noqa: E402
from strategies.mm_poisson_arrival import PoissonArrivalQuoter  # noqa: E402
from strategies.mm_rebate_harvester import RebateHarvester  # noqa: E402
from strategies.mm_volatility_adaptive import VolatilityAdaptiveMM  # noqa: E402
from strategies.mom_adx_trend_strength import AdxTrendStrength  # noqa: E402
from strategies.mom_donchian_breakout import DonchianBreakoutTrader  # noqa: E402
from strategies.mom_ema_crossover import EmaCrossoverTrend  # noqa: E402
from strategies.mom_micro_price_accel import MicroPriceAcceleration  # noqa: E402
from strategies.mom_parabolic_sar import ParabolicSarFollower  # noqa: E402
from strategies.mom_volatility_expansion import VolatilityExpansionTrader  # noqa: E402
from strategies.mom_volume_surge import VolumeSurgeMomentum  # noqa: E402
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    STRATEGY_CATALOG,
    StrategyRegistry,
    _IMPLEMENTED_STRATEGY_CLASSES,
)
from strategies.stat_bollinger_reversion import BollingerBandsReversion  # noqa: E402
from strategies.stat_half_life_decay import HalfLifeDecayReverter  # noqa: E402
from strategies.stat_kalman_filter import KalmanFilterTrader  # noqa: E402
from strategies.stat_pair_cointegration import PairCointegrationTrader  # noqa: E402
from strategies.stat_rsi_divergence import RsiDivergenceTrader  # noqa: E402
from strategies.stat_vwap_reversion import VwapReversionTrader  # noqa: E402
from strategies.stat_zscore_anomaly import ZScoreAnomalyTrader  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module (``asyncio_mode=strict`` requires it). All W46-1 contract-surface
# tests are SYNC — the mark is harmless and keeps collection consistent
# with every sibling test module.
pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════════
# Constants — the 34 (strategy_id, class) tuples promoted in W46-1.
# ═══════════════════════════════════════════════════════════════════════════

W46_STRATEGY_CLASSES: list[tuple[str, type[BaseStrategy]]] = [
    # Group A — market making (5):
    ("mm_glft_optimal", GlftOptimalQuoter),
    ("mm_volatility_adaptive", VolatilityAdaptiveMM),
    ("mm_rebate_harvester", RebateHarvester),
    ("mm_ofi_microstructure", OfiMicrostructureMM),
    ("mm_poisson_arrival", PoissonArrivalQuoter),
    # Group B — arbitrage (4):
    ("arb_multi_negative_risk", NegativeRiskMultiArb),
    ("arb_gamma_clob_parity", GammaClobParityArb),
    ("arb_synthetic_straddle", SyntheticStraddleArb),
    ("arb_cyclic_triangle", CyclicTriangleArb),
    # Group C — statistical (7):
    ("stat_bollinger_reversion", BollingerBandsReversion),
    ("stat_rsi_divergence", RsiDivergenceTrader),
    ("stat_zscore_anomaly", ZScoreAnomalyTrader),
    ("stat_pair_cointegration", PairCointegrationTrader),
    ("stat_vwap_reversion", VwapReversionTrader),
    ("stat_kalman_filter", KalmanFilterTrader),
    ("stat_half_life_decay", HalfLifeDecayReverter),
    # Group D — momentum (7):
    ("mom_ema_crossover", EmaCrossoverTrend),
    ("mom_donchian_breakout", DonchianBreakoutTrader),
    ("mom_volatility_expansion", VolatilityExpansionTrader),
    ("mom_volume_surge", VolumeSurgeMomentum),
    ("mom_parabolic_sar", ParabolicSarFollower),
    ("mom_adx_trend_strength", AdxTrendStrength),
    ("mom_micro_price_accel", MicroPriceAcceleration),
    # Group E — event-driven (4):
    ("event_oracle_dispute", OracleDisputeSniper),
    ("event_election_momentum", ElectionMomentumTracker),
    ("event_macro_straddle", MacroStraddleTrader),
    ("event_whale_follower", WhaleFollower),
    # Group F — machine learning (7):
    ("ml_lightgbm_boost", LightGBMBoost),
    ("ml_xgboost_directional", XGBoostDirectional),
    ("ml_online_sgd_learner", OnlineSgdLearner),
    ("ml_gmm_regime_switch", GmmRegimeSwitch),
    ("ml_svm_hyperplane", SvmHyperplaneClassifier),
    ("ml_bayesian_belief", BayesianBeliefUpdater),
    ("ml_qlearning_execution", QLearningExecutionAgent),
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
# Section 1 — Registry wiring: 50 IMPLEMENTED, 0 PLANNED (post-W46-1).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def registry():
    """Fresh ``StrategyRegistry`` per test (no singleton state leak)."""
    return StrategyRegistry()


def test_registry_catalog_size_is_50(registry):
    """The catalog must carry exactly 50 entries — the W46-1 wave
    promoted the 34 prior PLANNED stubs to IMPLEMENTED without adding
    or removing any rows."""
    catalog = registry.get_catalog()
    assert len(catalog) == 50
    assert len(catalog) == len(STRATEGY_CATALOG)


def test_registry_catalog_has_zero_planned(registry):
    """W46-1 — there are 0 PLANNED entries left in the catalog."""
    catalog = registry.get_catalog()
    planned = [r for r in catalog if r["status"] == STATUS_PLANNED]
    assert len(planned) == 0


def test_registry_catalog_has_fifty_implemented(registry):
    """W46-1 — all 50 catalog rows report ``status == IMPLEMENTED``."""
    catalog = registry.get_catalog()
    implemented = [r for r in catalog if r["status"] == STATUS_IMPLEMENTED]
    assert len(implemented) == 50


def test_registry_catalog_implemented_only_filter_returns_all_50(registry):
    """``implemented_only=True`` returns all 50 rows (no PLANNED stubs
    left to filter out as of W46-1)."""
    catalog = registry.get_catalog(implemented_only=True)
    assert len(catalog) == 50
    for row in catalog:
        assert row["status"] == STATUS_IMPLEMENTED
        assert row["implemented"] is True


def test_w46_strategy_ids_are_implemented(registry):
    """Each of the 34 W46-1 catalog ids must report
    ``status == IMPLEMENTED`` and ``implemented is True``."""
    catalog = registry.get_catalog()
    by_id = {r["strategy_id"]: r for r in catalog}
    for sid, _cls in W46_STRATEGY_CLASSES:
        assert sid in by_id, f"missing catalog entry for {sid}"
        assert by_id[sid]["status"] == STATUS_IMPLEMENTED, (
            f"{sid} must be IMPLEMENTED after W46-1"
        )
        assert by_id[sid]["implemented"] is True


def test_w46_strategy_ids_have_concrete_class_mappings():
    """Each of the 34 W46-1 catalog ids must be a key in
    ``_IMPLEMENTED_STRATEGY_CLASSES`` — the registry's lazy-import
    map that drives ``_instantiate_implemented``."""
    for sid, _cls in W46_STRATEGY_CLASSES:
        assert sid in _IMPLEMENTED_STRATEGY_CLASSES, (
            f"{sid} missing from _IMPLEMENTED_STRATEGY_CLASSES map"
        )


def test_no_strategy_left_with_planned_status_in_strategic_catalog():
    """Direct attribute check on the catalog list (not the dict view)
    — the source-of-truth ``STRATEGY_CATALOG`` list must have zero
    entries whose ``status`` field equals ``STATUS_PLANNED``."""
    planned = [s for s in STRATEGY_CATALOG if s.status == STATUS_PLANNED]
    assert len(planned) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Per-strategy 9-method contract surface (× 34 parametrised).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_strategy_implements_all_9_contract_methods(strategy_id, strategy_cls):
    """Each W46-1 strategy class must expose all 9 contract methods as
    callable attributes. (BaseStrategy already provides default
    implementations, but each W46-1 strategy overrides them with real
    logic — the test just checks the attributes exist and are callable.)"""
    inst = strategy_cls()
    for method_name in CONTRACT_METHODS:
        method = getattr(inst, method_name, None)
        assert callable(method), (
            f"{strategy_cls.__name__}.{method_name} must be callable"
        )


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_strategy_subclasses_base_strategy(strategy_id, strategy_cls):
    """Each W46-1 strategy class must inherit from ``BaseStrategy``
    (so the lazy-import instantiation path in
    ``StrategyRegistry._instantiate_implemented`` returns a
    ``BaseStrategy`` instance)."""
    assert issubclass(strategy_cls, BaseStrategy), (
        f"{strategy_cls.__name__} must subclass BaseStrategy"
    )


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
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


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_validate_returns_true_with_defaults(strategy_id, strategy_cls):
    """``validate()`` returns ``(True, "OK")`` for the default parameter
    set (every W46-1 strategy ships with sensible defaults)."""
    inst = strategy_cls()
    is_valid, msg = inst.validate()
    assert is_valid is True, (
        f"{strategy_cls.__name__}.validate() must succeed with defaults "
        f"(got: is_valid={is_valid}, msg={msg!r})"
    )
    assert msg == "OK"


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_generate_signal_returns_none_for_empty_context(strategy_id, strategy_cls):
    """An empty ``market_context`` dict must yield ``None`` — the
    strategy must not fire on missing inputs."""
    inst = strategy_cls()
    sig = inst.generate_signal({})
    assert sig is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
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


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
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


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_entry_logic_returns_dict_with_keys(strategy_id, strategy_cls):
    """``entry_logic`` returns a dict for actionable signals (carrying
    at least a ``price`` key); a ``skip`` flag for HOLD signals."""
    inst = strategy_cls()
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.55,
                  confidence=0.7, edge=0.05)
    entry = inst.entry_logic(fake, {"mid": 0.50})
    assert isinstance(entry, dict)
    assert "price" in entry
    # HOLD signal: must include a ``skip`` flag (or some skip-equivalent
    # like ``reason`` — the contract surface is "don't enter on HOLD").
    hold_entry = inst.entry_logic(
        Signal(action="HOLD", token_id="t", size=0.0, confidence=0.0, edge=0.0),
        {},
    )
    assert isinstance(hold_entry, dict)
    assert hold_entry.get("skip") is True or "reason" in hold_entry, (
        f"{strategy_cls.__name__}.entry_logic(HOLD) must surface a skip "
        f"flag or a reason key (got {hold_entry!r})"
    )


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_exit_logic_returns_none_or_dict(strategy_id, strategy_cls):
    """``exit_logic`` must return either ``None`` (no exit decision) or
    a dict carrying a ``reason`` key. It must never raise on empty
    inputs."""
    inst = strategy_cls()
    # Empty position: must return None (defensive — never raises).
    assert inst.exit_logic({}, {}) is None
    assert inst.exit_logic(None, {}) is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
def test_diagnostics_returns_dict_carrying_name(strategy_id, strategy_cls):
    """``diagnostics()`` returns a dict carrying at least the strategy's
    ``name`` (BaseStrategy's default merges ``self.name`` into the
    returned dict; subclasses extend, they don't replace it)."""
    inst = strategy_cls()
    diag = inst.diagnostics()
    assert isinstance(diag, dict)
    assert diag.get("name") == inst.name, (
        f"{strategy_cls.__name__}.diagnostics() must carry name={inst.name!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Section 3 — Registry instantiation sanity-check.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strategy_id,strategy_cls", W46_STRATEGY_CLASSES)
async def test_registry_can_instantiate_each_w46_strategy(strategy_id, strategy_cls):
    """``StrategyRegistry.start_strategy(<w46_id>)`` must instantiate
    the concrete class (not the ``QuantStrategyInstance`` stub wrapper).

    W46-1 — every prior PLANNED stub now has a concrete-class mapping
    in ``_IMPLEMENTED_STRATEGY_CLASSES``, so the lazy-import path in
    ``_instantiate_implemented`` lands on the real strategy class
    instead of falling through to the no-op stub."""
    reg = StrategyRegistry()
    ok = await reg.start_strategy(strategy_id)
    assert ok is True, f"start_strategy({strategy_id}) returned False"
    instances = reg.get_active_instances()
    assert strategy_id in instances, f"{strategy_id} not in active_instances"
    assert isinstance(instances[strategy_id], strategy_cls), (
        f"{strategy_id} instantiated as {type(instances[strategy_id]).__name__}, "
        f"expected {strategy_cls.__name__}"
    )
    await reg.stop_strategy(strategy_id)
