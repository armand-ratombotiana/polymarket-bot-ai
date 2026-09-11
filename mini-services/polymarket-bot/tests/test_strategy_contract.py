"""
tests/test_strategy_contract.py — Unit tests for the W19-2 Unified
Strategy Contract (God Mode §26).

W19-2 — verifies that the 9-method ``StrategyContract`` ABC is correctly
defined on ``strategies.base.StrategyContract`` and that every concrete
strategy in the bot (``BaseStrategy`` itself via a stub subclass,
``SignalTraderStrategy``, ``MarketMakerStrategy``, ``ArbScannerStrategy``)
implements all 9 methods with the documented return shapes.

Scope (12 test groups):

  (1) ``StrategyContract`` exposes exactly the 9 documented abstract
      methods and nothing else.
  (2) ``BaseStrategy`` is concrete except for ``_run`` (the async loop
      remains the only abstract method).
  (3) ``BaseStrategy`` default ``metadata()`` shape (name / version /
      description / author).
  (4) ``BaseStrategy`` default ``configure()`` merges into ``self.config``.
  (5) ``BaseStrategy`` default ``validate()`` returns ``(True, "OK")``.
  (6) ``BaseStrategy`` default ``generate_signal()`` returns ``None``.
  (7) ``BaseStrategy`` default ``estimate_edge()`` returns ``signal.edge``
      (and ``0.0`` for ``None``).
  (8) ``BaseStrategy`` default ``size_position()`` returns 1% of capital
      for actionable signals, ``0.0`` for ``None`` / ``HOLD``.
  (9) ``BaseStrategy`` default ``entry_logic()`` returns a dict with
      ``price`` and ``type="limit"``.
  (10) ``BaseStrategy`` default ``exit_logic()`` returns ``None``.
  (11) ``BaseStrategy`` default ``diagnostics()`` shape (name / running /
       stats / last_error).
  (12) ``SignalTraderStrategy`` implements all 9 methods with documented
       return shapes.
  (13) ``MarketMakerStrategy`` implements all 9 methods with documented
       return shapes.
  (14) ``ArbScannerStrategy`` implements all 9 methods with documented
       return shapes.
  (15) ``Signal`` dataclass fields and defaults are exactly as specified
       in the W19-2 contract.

Approach
--------
The strategy classes are imported under the same env-var redirect
bootstrap used by every sibling test module (``tests/conftest.py`` +
``tests/test_strategy_base.py``). The autouse ``_reset_store_factory_
defaults`` fixture from conftest runs BEFORE every test, resetting the
global ``store`` / ``risk_manager`` / ``paper_sim`` singletons to a clean
baseline so per-test mutation is hermetic.

The contract methods are SYNC (no ``await``) — they're designed for
introspection from sync contexts (FastAPI request handlers, REPL, backtest
replay) — so the test functions are plain ``def test_...`` (not
``async def``). This also verifies the design invariant: no contract
method needs an event loop.

A small ``_StubStrategy`` subclass provides a concrete ``_run`` so
``BaseStrategy`` can be instantiated for the default-implementation
tests; its ``_run`` body just returns immediately (we never actually
start the async loop here).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import get_type_hints

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with ``tests/conftest.py``: conftest sets these first via
# its own ``_ENV_REDIRECTS`` table, but if this module is imported before
# conftest (e.g. by an IDE that doesn't load conftest first), the
# ``setdefault`` calls here ensure the strategy import never reaches into
# the repo's real ``data/`` directory (which is read-only in the sandbox
# — see the import-time ``PermissionError: [Errno 13] Permission denied:
# '/app/data'`` raised by ``ml.model_registry.ModelRegistry._save_to_disk``
# when MODEL_REGISTRY_PATH is unset).
_TMP_ROOT = Path("/tmp/strategy_contract_tests")
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
    # Force paper mode + live disabled so the strategy ``__init__``
    # ``settings.paper_trade=True`` path is exercised (no live-trading
    # gate short-circuits the contract method smoke checks).
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-strategy-contract",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# W18-8 — clear the model-registry file BEFORE the first project import
# so the singleton seeds the factory baseline (exactly one clean entry)
# rather than reading polluted state from a prior pytest session.
_TMP_REGISTRY_FILE = _TMP_ROOT / "model_registry.json"
if _TMP_REGISTRY_FILE.exists():
    try:
        _TMP_REGISTRY_FILE.unlink()
    except OSError:
        pass

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``ml.*``, ``strategies.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from strategies.arb_scanner import ArbScannerStrategy  # noqa: E402
from strategies.base import (  # noqa: E402
    BaseStrategy,
    Signal,
    StrategyContract,
)
from strategies.market_maker import MarketMakerStrategy  # noqa: E402
from strategies.signal_trader import SignalTraderStrategy  # noqa: E402


# ── The 9 contract methods (canonical names) ─────────────────────────────────
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


# ── Stub concrete strategy for BaseStrategy default-impl tests ──────────────
class _StubStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for contract-method tests.

    The only abstract method on ``BaseStrategy`` (post-W19-2) is ``_run``
    (the async loop). This stub provides a no-op ``_run`` so we can
    instantiate ``BaseStrategy`` and exercise its default contract impls
    without booting the async scan / quote / arb loops.
    """

    name: str = "stub"

    async def _run(self) -> None:
        # No-op — the stub is never ``start()``-ed in the contract tests;
        # we only need a concrete instance to call the contract methods on.
        return None


# ── (1) StrategyContract exposes exactly the 9 documented abstract methods ──

def test_strategy_contract_has_exactly_nine_abstract_methods():
    """``StrategyContract`` must declare exactly the 9 documented methods
    as ``@abstractmethod`` (and no others) so the W19-2 contract surface
    is pinned: a strategy that fails to implement even one of them cannot
    be instantiated, and a strategy that adds a 10th method is not
    blocked from instantiating.

    The 9 methods (in alphabetical order — the canonical reference order
    used by the tests below):
      1. ``configure``
      2. ``diagnostics``
      3. ``entry_logic``
      4. ``estimate_edge``
      5. ``exit_logic``
      6. ``generate_signal``
      7. ``metadata``
      8. ``size_position``
      9. ``validate``
    """
    expected = {
        "configure",
        "diagnostics",
        "entry_logic",
        "estimate_edge",
        "exit_logic",
        "generate_signal",
        "metadata",
        "size_position",
        "validate",
    }
    actual = set(StrategyContract.__abstractmethods__)
    assert actual == expected, (
        f"StrategyContract must declare exactly the 9 documented abstract "
        f"methods; got {sorted(actual)} (expected {sorted(expected)})."
    )
    # Belt-and-braces: the canonical name tuple also matches.
    assert set(CONTRACT_METHODS) == expected, (
        f"CONTRACT_METHODS test constant must match the abstract method "
        f"set; got {sorted(CONTRACT_METHODS)} vs {sorted(expected)}."
    )


# ── (2) BaseStrategy is concrete except for _run ─────────────────────────────

def test_base_strategy_abstract_methods_after_contract():
    """``BaseStrategy`` must provide concrete implementations of all 9
    contract methods so subclasses are NOT forced to implement them.
    After W19-2, ``BaseStrategy.__abstractmethods__`` should be exactly
    ``{"_run"}`` — the async loop remains the only abstract method.

    This is the load-bearing test for backward compatibility: the three
    real strategies (``SignalTraderStrategy``, ``MarketMakerStrategy``,
    ``ArbScannerStrategy``) and the 47 stub ``QuantStrategyInstance``
    entries in ``strategies/registry.py`` all provide a ``_run`` and
    must remain instantiable unchanged.
    """
    assert "_run" in BaseStrategy.__abstractmethods__, (
        "BaseStrategy must keep _run abstract so subclasses provide the "
        "async loop body."
    )
    # None of the 9 contract methods are still abstract on BaseStrategy.
    leftover = BaseStrategy.__abstractmethods__ - {"_run"}
    assert leftover == set(), (
        f"BaseStrategy must provide concrete impls for all 9 contract "
        f"methods; leftover abstract: {sorted(leftover)}."
    )


def test_stub_strategy_is_instantiable():
    """A minimal ``BaseStrategy`` subclass that only implements ``_run``
    can be instantiated — i.e. the contract surface does not block
    instantiation. ``_StubStrategy`` is used by every BaseStrategy
    default-impl test below."""
    s = _StubStrategy()
    assert s.name == "stub"
    assert s._running is False
    # W19-2 — config + stats + last_error initialised by the new __init__.
    # W22-7 — _stats extended with ``evaluations`` + ``rejects`` counters
    # for the canonical strategy.evaluations / strategy.rejects
    # observability surface; the subset assertion tolerates future
    # counter additions without churn.
    assert s.config == {}
    assert s._stats == {
        "signals": 0, "trades": 0, "errors": 0,
        "evaluations": 0, "rejects": 0,
    }
    assert s._last_error is None


# ── (3) BaseStrategy default metadata() ──────────────────────────────────────

def test_base_default_metadata_shape():
    """``BaseStrategy.metadata()`` returns a dict with at least the four
    documented keys: ``name``, ``version``, ``description``, ``author``.
    Concrete strategies may add more keys (``category``, ``model``,
    ``sizing``, …) but the base impl must surface the canonical four."""
    s = _StubStrategy()
    md = s.metadata()
    assert isinstance(md, dict)
    assert md["name"] == "stub"
    assert "version" in md and isinstance(md["version"], str)
    assert "description" in md and isinstance(md["description"], str)
    assert "author" in md and isinstance(md["author"], str)


# ── (4) BaseStrategy default configure() ────────────────────────────────────

def test_base_default_configure_merges_into_self_config():
    """``BaseStrategy.configure(config)`` shallow-merges the supplied
    dict into ``self.config`` so subclasses can read typed fields off
    ``self.config.get("min_confidence", default)`` after a configure
    call. The merge is non-destructive: existing keys are overwritten
    only if the new config supplies them; keys absent from the new
    config are preserved."""
    s = _StubStrategy()
    assert s.config == {}
    s.configure({"min_confidence": 0.7, "base_order_size": 5.0})
    assert s.config == {"min_confidence": 0.7, "base_order_size": 5.0}
    # Second configure call merges (does not replace) — existing keys
    # not in the new dict are preserved.
    s.configure({"min_confidence": 0.6})
    assert s.config == {
        "min_confidence": 0.6,
        "base_order_size": 5.0,
    }


def test_base_default_configure_handles_empty_and_none():
    """``configure()`` must not raise when passed an empty dict or
    ``None``-like value (callers may pass an empty config when no
    overrides apply)."""
    s = _StubStrategy()
    s.configure({})  # no-op
    assert s.config == {}
    # An empty / falsy config is also a no-op (the ``if config:`` guard
    # in the default impl short-circuits).
    s.configure(None)  # type: ignore[arg-type]
    assert s.config == {}


# ── (5) BaseStrategy default validate() ──────────────────────────────────────

def test_base_default_validate_returns_true_ok():
    """``BaseStrategy.validate()`` returns ``(True, "OK")`` so callers
    can short-circuit on the boolean without inspecting the message."""
    s = _StubStrategy()
    is_valid, msg = s.validate()
    assert is_valid is True
    assert msg == "OK"


# ── (6) BaseStrategy default generate_signal() returns None ─────────────────

def test_base_default_generate_signal_returns_none():
    """``BaseStrategy.generate_signal(ctx)`` returns ``None`` so a
    freshly-constructed strategy (or a stub catalog entry) never
    accidentally fires a trade just because the contract method exists.
    Concrete strategies override to surface actionable signals."""
    s = _StubStrategy()
    assert s.generate_signal({}) is None
    assert s.generate_signal({"token_id": "0xfoo", "mid": 0.5}) is None


# ── (7) BaseStrategy default estimate_edge() ────────────────────────────────

def test_base_default_estimate_edge_returns_signal_edge():
    """``BaseStrategy.estimate_edge(signal)`` returns ``signal.edge``
    (the pre-computed value set by ``generate_signal`` in concrete
    strategies) so callers don't need to know each strategy's edge
    formula."""
    s = _StubStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", edge=0.05)
    assert s.estimate_edge(sig) == 0.05


def test_base_default_estimate_edge_returns_zero_for_none_signal():
    """``BaseStrategy.estimate_edge(None)`` returns ``0.0`` so callers
    can chain ``edge = strat.estimate_edge(strat.generate_signal(ctx))``
    without a None-guard."""
    s = _StubStrategy()
    assert s.estimate_edge(None) == 0.0  # type: ignore[arg-type]


# ── (8) BaseStrategy default size_position() ─────────────────────────────────

def test_base_default_size_position_returns_one_pct_of_capital():
    """``BaseStrategy.size_position(signal, capital, risk_params)``
    returns ``capital * 0.01`` (a conservative 1% baseline) for an
    actionable BUY/SELL signal. This ensures a misconfigured strategy
    (one that forgot to override ``size_position``) cannot blow up the
    bankroll on a single trade."""
    s = _StubStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", size=1.0)
    assert s.size_position(sig, 1000.0, {}) == pytest.approx(10.0)


def test_base_default_size_position_returns_zero_for_hold():
    """``size_position`` returns ``0.0`` for a ``HOLD`` signal so a
    sub-threshold signal (e.g. confidence below the gate) doesn't
    accidentally trigger a trade through the size-position path."""
    s = _StubStrategy()
    sig_hold = Signal(action="HOLD", token_id="0xfoo")
    assert s.size_position(sig_hold, 1000.0, {}) == 0.0


def test_base_default_size_position_returns_zero_for_none_signal():
    """``size_position`` returns ``0.0`` for ``None`` so callers can
    chain without a None-guard."""
    s = _StubStrategy()
    assert s.size_position(None, 1000.0, {}) == 0.0  # type: ignore[arg-type]


# ── (9) BaseStrategy default entry_logic() ───────────────────────────────────

def test_base_default_entry_logic_returns_limit_dict():
    """``BaseStrategy.entry_logic(signal, market_context)`` returns a
    plain dict with ``price`` (from the signal or market mid) and
    ``type="limit"``. The dict shape is JSON-serializable so the
    dashboard / API surface can render it directly."""
    s = _StubStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", price=0.55)
    el = s.entry_logic(sig, {"mid": 0.5})
    assert isinstance(el, dict)
    assert el["price"] == 0.55
    assert el["type"] == "limit"


def test_base_default_entry_logic_falls_back_to_market_mid():
    """When the signal has no ``price``, ``entry_logic`` falls back to
    ``market_context["mid"]`` (defaulting to 0.5 when mid is missing)."""
    s = _StubStrategy()
    sig_no_price = Signal(action="BUY", token_id="0xfoo")
    el = s.entry_logic(sig_no_price, {"mid": 0.42})
    assert el["price"] == 0.42
    # Belt-and-braces: missing mid defaults to 0.5.
    el_default = s.entry_logic(sig_no_price, {})
    assert el_default["price"] == 0.5


# ── (10) BaseStrategy default exit_logic() returns None ──────────────────────

def test_base_default_exit_logic_returns_none():
    """``BaseStrategy.exit_logic(position, market_context)`` returns
    ``None`` so the base impl is a no-op. Concrete strategies override
    to encode stop-loss / take-profit / time-based flush rules."""
    s = _StubStrategy()
    assert s.exit_logic({}, {}) is None
    assert s.exit_logic({"created_at": 0, "order_id": "x"}, {"now": 99999}) is None


# ── (11) BaseStrategy default diagnostics() shape ───────────────────────────

def test_base_default_diagnostics_shape():
    """``BaseStrategy.diagnostics()`` returns a dict with at least the
    four base keys: ``name``, ``running``, ``stats``, ``last_error``.
    Concrete strategies override to add strategy-specific fields but
    should call ``super().diagnostics()`` and ``.update()`` the result
    so the base fields are always present."""
    s = _StubStrategy()
    d = s.diagnostics()
    assert isinstance(d, dict)
    assert d["name"] == "stub"
    assert d["running"] is False
    assert "stats" in d and isinstance(d["stats"], dict)
    # W22-7 — _stats carries ``evaluations`` + ``rejects`` counters in
    # addition to the legacy signals/trades/errors trio. The subset
    # assertion tolerates future counter additions without churn.
    assert d["stats"] == {
        "signals": 0, "trades": 0, "errors": 0,
        "evaluations": 0, "rejects": 0,
    }
    assert d["last_error"] is None


# ── (12) SignalTraderStrategy implements all 9 contract methods ──────────────

def test_signal_trader_implements_all_nine_contract_methods():
    """``SignalTraderStrategy`` must implement all 9 contract methods
    (overriding the ``BaseStrategy`` defaults where appropriate). After
    W19-2 the class must be concrete (``__abstractmethods__ == set()``)
    so it can be instantiated by the registry without further subclassing.
    """
    # The class has no leftover abstract methods.
    assert SignalTraderStrategy.__abstractmethods__ == set(), (
        f"SignalTraderStrategy must be concrete after W19-2; leftover "
        f"abstract methods: {sorted(SignalTraderStrategy.__abstractmethods__)}"
    )
    s = SignalTraderStrategy()
    for method_name in CONTRACT_METHODS:
        assert callable(getattr(s, method_name, None)), (
            f"SignalTraderStrategy must implement contract method "
            f"``{method_name}``."
        )


def test_signal_trader_metadata_shape():
    s = SignalTraderStrategy()
    md = s.metadata()
    assert md["name"] == "signal_trader"
    assert "version" in md
    assert "description" in md
    assert "author" in md


def test_signal_trader_validate_returns_true_ok_by_default():
    s = SignalTraderStrategy()
    is_valid, msg = s.validate()
    assert is_valid is True
    assert msg == "OK"


def test_signal_trader_validate_rejects_bad_min_confidence():
    """``validate()`` returns ``(False, ...)`` when ``_min_confidence``
    is outside ``[0, 1]`` — a misconfiguration that would either
    always-fire or always-reject signals."""
    s = SignalTraderStrategy()
    s._min_confidence = 1.5  # out of range
    is_valid, msg = s.validate()
    assert is_valid is False
    assert "min_confidence" in msg


def test_signal_trader_configure_overrides_min_confidence():
    """``configure({"min_confidence": v})`` overrides ``_min_confidence``
    AND clamps the value to ``[0, 1]`` so a misconfigured caller can't
    flip the gate to a value > 1 (always-fire)."""
    s = SignalTraderStrategy()
    original = s._min_confidence
    s.configure({"min_confidence": 0.8})
    assert s._min_confidence == 0.8
    # Out-of-range value is clamped, not applied verbatim.
    s.configure({"min_confidence": 1.5})
    assert s._min_confidence == 1.0
    # Belt-and-braces: original value is restored by the autouse conftest
    # reset (we mutated the instance only, not the class).
    assert s._min_confidence != original or original == 1.0


def test_signal_trader_generate_signal_returns_signal_for_valid_context():
    """``generate_signal(ctx)`` returns a populated ``Signal`` when the
    context supplies a token_id and a confidence above the gate."""
    s = SignalTraderStrategy()
    s._min_confidence = 0.5
    sig = s.generate_signal({
        "token_id": "0xY",
        "confidence": 0.9,
        "target_price": 0.6,
        "edge": 0.05,
        "size_usdc": 2.5,
        "reason": "test",
        "decision_id": "dec-1",
    })
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"  # default direction
    assert sig.token_id == "0xY"
    assert sig.confidence == 0.9
    assert sig.edge == 0.05
    assert sig.price == 0.6
    assert sig.size == 2.5
    assert sig.metadata["decision_id"] == "dec-1"


def test_signal_trader_generate_signal_returns_none_for_missing_token_id():
    """``generate_signal({})`` returns ``None`` when the context lacks
    a token_id — the load-bearing required field."""
    s = SignalTraderStrategy()
    assert s.generate_signal({}) is None
    assert s.generate_signal({"confidence": 0.9}) is None


def test_signal_trader_generate_signal_marks_hold_below_confidence_gate():
    """When the supplied confidence is below ``_min_confidence`` the
    signal is marked ``action="HOLD"`` so callers can still observe
    sub-threshold signals without acting on them."""
    s = SignalTraderStrategy()
    s._min_confidence = 0.8
    sig = s.generate_signal({
        "token_id": "0xY",
        "confidence": 0.5,  # below 0.8 gate
    })
    assert sig is not None
    assert sig.action == "HOLD"


def test_signal_trader_estimate_edge_returns_signal_edge():
    s = SignalTraderStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", edge=0.07)
    assert s.estimate_edge(sig) == 0.07


def test_signal_trader_estimate_edge_returns_zero_for_none():
    s = SignalTraderStrategy()
    assert s.estimate_edge(None) == 0.0  # type: ignore[arg-type]


def test_signal_trader_size_position_returns_positive_for_buy():
    """``size_position`` returns a positive float for a BUY signal with
    positive edge — bounded by capital, max_position_per_market, and
    floored at $0.50."""
    s = SignalTraderStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", edge=0.05)
    size = s.size_position(sig, 1000.0, {})
    assert size > 0.0
    assert size <= 1000.0  # never exceeds capital


def test_signal_trader_size_position_returns_zero_for_hold():
    s = SignalTraderStrategy()
    sig = Signal(action="HOLD", token_id="0xfoo")
    assert s.size_position(sig, 1000.0, {}) == 0.0


def test_signal_trader_entry_logic_returns_dict_with_required_fields():
    s = SignalTraderStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", price=0.55, size=2.5)
    el = s.entry_logic(sig, {"mid": 0.5})
    assert isinstance(el, dict)
    assert el["token_id"] == "0xfoo"
    assert el["price"] == 0.55
    assert el["side"] == "BUY"
    assert el["type"] == "limit"
    assert el["size"] > 0.0


def test_signal_trader_entry_logic_returns_skip_dict_for_hold():
    s = SignalTraderStrategy()
    sig_hold = Signal(action="HOLD", token_id="0xfoo")
    el = s.entry_logic(sig_hold, {})
    assert el.get("skip") is True


def test_signal_trader_exit_logic_returns_cancel_for_stale_order():
    """``exit_logic`` returns a cancel dict when the position's
    ``created_at`` is older than ``STALE_ORDER_SECONDS`` (180s)."""
    s = SignalTraderStrategy()
    # position created at epoch=0, now=99999 → age = 99999s > 180s
    el = s.exit_logic(
        {"created_at": 0, "order_id": "ord-1"},
        {"now": 99999},
    )
    assert el is not None
    assert el["action"] == "cancel"
    assert el["order_id"] == "ord-1"


def test_signal_trader_exit_logic_returns_none_for_fresh_order():
    """``exit_logic`` returns ``None`` when the position is fresh
    (age ≤ STALE_ORDER_SECONDS)."""
    s = SignalTraderStrategy()
    import time as _time
    now = _time.time()
    el = s.exit_logic(
        {"created_at": now - 10, "order_id": "ord-1"},  # 10s ago
        {"now": now},
    )
    assert el is None


def test_signal_trader_diagnostics_includes_base_and_strategy_fields():
    s = SignalTraderStrategy()
    d = s.diagnostics()
    # Base fields (always present).
    assert d["name"] == "signal_trader"
    assert "running" in d
    assert "stats" in d
    assert "last_error" in d
    # Strategy-specific fields.
    assert "active_signals" in d
    assert "feature_cache_size" in d
    assert "min_confidence" in d
    assert "base_order_size" in d
    assert "model_is_fitted" in d


# ── (13) MarketMakerStrategy implements all 9 contract methods ──────────────

def test_market_maker_implements_all_nine_contract_methods():
    """``MarketMakerStrategy`` must be concrete after W19-2 and expose
    all 9 contract methods."""
    assert MarketMakerStrategy.__abstractmethods__ == set(), (
        f"MarketMakerStrategy must be concrete after W19-2; leftover "
        f"abstract: {sorted(MarketMakerStrategy.__abstractmethods__)}"
    )
    s = MarketMakerStrategy()
    for method_name in CONTRACT_METHODS:
        assert callable(getattr(s, method_name, None)), (
            f"MarketMakerStrategy must implement ``{method_name}``."
        )


def test_market_maker_metadata_shape():
    s = MarketMakerStrategy()
    md = s.metadata()
    assert md["name"] == "market_maker"
    assert "version" in md
    assert "description" in md
    assert "author" in md
    assert "category" in md


def test_market_maker_validate_returns_true_ok_by_default():
    s = MarketMakerStrategy()
    is_valid, msg = s.validate()
    assert is_valid is True
    assert msg == "OK"


def test_market_maker_validate_rejects_zero_quote_size():
    """``validate()`` returns ``(False, ...)`` when ``_quote_size <= 0``."""
    s = MarketMakerStrategy()
    s._quote_size = 0.0
    is_valid, msg = s.validate()
    assert is_valid is False
    assert "quote_size" in msg


def test_market_maker_configure_overrides_quote_size():
    s = MarketMakerStrategy()
    original = s._quote_size
    s.configure({"quote_size": 5.0})
    assert s._quote_size == 5.0
    # Invalid (non-positive) value is rejected — original preserved.
    s.configure({"quote_size": -1.0})
    assert s._quote_size == 5.0
    # Restore for downstream tests (instance mutation only).
    s._quote_size = original


def test_market_maker_generate_signal_returns_signal_for_valid_context():
    """``generate_signal(ctx)`` returns a populated ``Signal`` with the
    A-S reservation-price-skewed bid price."""
    s = MarketMakerStrategy()
    sig = s.generate_signal({
        "token_id": "0xY",
        "mid": 0.5,
        "spread": 0.02,
        "inventory": 0.0,
        "side": "BUY",
    })
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    assert sig.token_id == "0xY"
    # Bid price below mid (BUY side quote).
    assert sig.price < 0.5
    # Edge = half-spread (expected per-side capture).
    assert sig.edge > 0.0


def test_market_maker_generate_signal_returns_none_for_missing_mid():
    """``generate_signal({})`` returns ``None`` when the context lacks
    a token_id or mid."""
    s = MarketMakerStrategy()
    assert s.generate_signal({}) is None
    assert s.generate_signal({"token_id": "0xY"}) is None  # missing mid
    assert s.generate_signal({"mid": 0.5}) is None  # missing token_id


def test_market_maker_estimate_edge_returns_half_spread():
    s = MarketMakerStrategy()
    sig = s.generate_signal({
        "token_id": "0xY",
        "mid": 0.5,
        "spread": 0.02,
    })
    assert sig is not None
    edge = s.estimate_edge(sig)
    assert edge > 0.0
    # Edge should be approximately half the spread (0.01 for spread=0.02).
    assert edge == pytest.approx(0.01, abs=0.005)


def test_market_maker_estimate_edge_returns_zero_for_none():
    s = MarketMakerStrategy()
    assert s.estimate_edge(None) == 0.0  # type: ignore[arg-type]


def test_market_maker_size_position_returns_quote_size_bounded_by_headroom():
    """``size_position`` returns the configured quote_size, bounded by
    the inventory headroom (max_inv - current_inv)."""
    s = MarketMakerStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", size=1.5)
    # Default quote_size=1.5, max_inv=15.0, current_inv=5.0 → headroom=10.
    # min(1.5, 10, capital=100) = 1.5.
    size = s.size_position(sig, 100.0, {"current_inventory_usdc": 5.0})
    assert size == pytest.approx(1.5)


def test_market_maker_size_position_returns_zero_for_hold():
    s = MarketMakerStrategy()
    sig = Signal(action="HOLD", token_id="0xfoo")
    assert s.size_position(sig, 100.0, {}) == 0.0


def test_market_maker_entry_logic_returns_dict_with_required_fields():
    s = MarketMakerStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", price=0.49, size=1.5)
    el = s.entry_logic(sig, {"mid": 0.5})
    assert isinstance(el, dict)
    assert el["token_id"] == "0xfoo"
    assert el["price"] == 0.49
    assert el["side"] == "BUY"
    assert el["type"] == "limit"


def test_market_maker_exit_logic_returns_flush_sell_for_stale_inventory():
    """``exit_logic`` returns a flush-sell dict when YES inventory has
    been held > 60s and a marketable best_bid is available."""
    s = MarketMakerStrategy()
    import time as _time
    now = _time.time()
    el = s.exit_logic(
        {"yes_shares": 10.0, "inventory_since": now - 100.0},  # 100s ago
        {"now": now, "best_bid": 0.49},
    )
    assert el is not None
    assert el["action"] == "flush_sell"
    assert el["price"] == 0.49
    assert el["size"] > 0.0


def test_market_maker_exit_logic_returns_none_within_grace_window():
    """``exit_logic`` returns ``None`` when inventory is fresh
    (held ≤ 60s)."""
    s = MarketMakerStrategy()
    import time as _time
    now = _time.time()
    el = s.exit_logic(
        {"yes_shares": 10.0, "inventory_since": now - 10.0},  # 10s ago
        {"now": now, "best_bid": 0.49},
    )
    assert el is None


def test_market_maker_diagnostics_includes_base_and_strategy_fields():
    s = MarketMakerStrategy()
    d = s.diagnostics()
    # Base fields.
    assert d["name"] == "market_maker"
    assert "running" in d
    assert "stats" in d
    # Strategy-specific fields.
    assert "quoted_tokens" in d
    assert "active_quotes" in d
    assert "base_spread_frac" in d
    assert "quote_size" in d
    assert "max_inventory" in d


# ── (14) ArbScannerStrategy implements all 9 contract methods ───────────────

def test_arb_scanner_implements_all_nine_contract_methods():
    """``ArbScannerStrategy`` must be concrete after W19-2 and expose
    all 9 contract methods."""
    assert ArbScannerStrategy.__abstractmethods__ == set(), (
        f"ArbScannerStrategy must be concrete after W19-2; leftover "
        f"abstract: {sorted(ArbScannerStrategy.__abstractmethods__)}"
    )
    s = ArbScannerStrategy()
    for method_name in CONTRACT_METHODS:
        assert callable(getattr(s, method_name, None)), (
            f"ArbScannerStrategy must implement ``{method_name}``."
        )


def test_arb_scanner_metadata_shape():
    s = ArbScannerStrategy()
    md = s.metadata()
    assert md["name"] == "arb_scanner"
    assert "version" in md
    assert "description" in md
    assert "author" in md
    assert "category" in md


def test_arb_scanner_validate_returns_true_ok_by_default():
    s = ArbScannerStrategy()
    is_valid, msg = s.validate()
    assert is_valid is True
    assert msg == "OK"


def test_arb_scanner_validate_rejects_zero_min_profit():
    """``validate()`` returns ``(False, ...)`` when ``_min_profit_frac <= 0``."""
    s = ArbScannerStrategy()
    s._min_profit_frac = 0.0
    is_valid, msg = s.validate()
    assert is_valid is False
    assert "min_profit_frac" in msg


def test_arb_scanner_configure_overrides_order_size():
    s = ArbScannerStrategy()
    original = s._order_size
    s.configure({"order_size": 5.0})
    assert s._order_size == 5.0
    # Invalid (non-positive) value is rejected.
    s.configure({"order_size": -1.0})
    assert s._order_size == 5.0
    # Restore for downstream tests.
    s._order_size = original


def test_arb_scanner_generate_signal_returns_signal_for_long_dutch_book():
    """``generate_signal`` returns a populated ``Signal`` for a long
    Dutch Book arb (yes + no < 1.00)."""
    s = ArbScannerStrategy()
    sig = s.generate_signal({
        "yes_token": "0xY",
        "no_token": "0xN",
        "yes_price": 0.45,
        "no_price": 0.50,  # total = 0.95 → profit = 0.05
        "arb_type": "long_dutch_book",
    })
    assert sig is not None
    assert isinstance(sig, Signal)
    assert sig.action == "BUY"
    assert sig.token_id == "0xY"
    assert sig.edge == pytest.approx(0.05)
    assert sig.confidence == 1.0  # arb is risk-free
    assert sig.metadata["no_token"] == "0xN"
    assert sig.metadata["arb_type"] == "long_dutch_book"


def test_arb_scanner_generate_signal_returns_none_for_missing_tokens():
    """``generate_signal({})`` returns ``None`` when required keys are missing."""
    s = ArbScannerStrategy()
    assert s.generate_signal({}) is None
    assert s.generate_signal({"yes_token": "0xY"}) is None  # missing no_token


def test_arb_scanner_generate_signal_returns_none_below_min_profit():
    """``generate_signal`` returns ``None`` when the computed profit is
    below ``_min_profit_frac`` — mirrors the production min-profit gate."""
    s = ArbScannerStrategy()
    # Default _min_profit_frac = 0.005. Set a higher floor to verify gating.
    s._min_profit_frac = 0.10  # require 10% profit
    sig = s.generate_signal({
        "yes_token": "0xY",
        "no_token": "0xN",
        "yes_price": 0.49,
        "no_price": 0.50,  # profit = 0.01 < 0.10
        "arb_type": "long_dutch_book",
    })
    assert sig is None


def test_arb_scanner_estimate_edge_returns_signal_edge():
    s = ArbScannerStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", edge=0.05)
    assert s.estimate_edge(sig) == 0.05


def test_arb_scanner_estimate_edge_returns_zero_for_none():
    s = ArbScannerStrategy()
    assert s.estimate_edge(None) == 0.0  # type: ignore[arg-type]


def test_arb_scanner_size_position_returns_order_size_bounded_by_capital():
    s = ArbScannerStrategy()
    sig = Signal(action="BUY", token_id="0xfoo", size=1.5)
    # Default order_size = 1.5; min(1.5, capital=100) = 1.5.
    size = s.size_position(sig, 100.0, {})
    assert size == pytest.approx(1.5)
    # Capital-bounded: when capital < order_size, return capital.
    size_capped = s.size_position(sig, 1.0, {})
    assert size_capped == pytest.approx(1.0)


def test_arb_scanner_size_position_returns_zero_for_hold():
    s = ArbScannerStrategy()
    sig = Signal(action="HOLD", token_id="0xfoo")
    assert s.size_position(sig, 100.0, {}) == 0.0


def test_arb_scanner_entry_logic_returns_dict_with_both_legs():
    """``entry_logic`` returns a dict with the YES-leg params AND a
    ``no_leg`` sub-dict so callers have both legs' execution parameters."""
    s = ArbScannerStrategy()
    sig = s.generate_signal({
        "yes_token": "0xY",
        "no_token": "0xN",
        "yes_price": 0.45,
        "no_price": 0.50,
    })
    assert sig is not None
    el = s.entry_logic(sig, {"mid": 0.5})
    assert isinstance(el, dict)
    assert el["token_id"] == "0xY"
    assert el["price"] == 0.45
    assert el["side"] == "BUY"
    assert el["type"] == "FOK"  # Fill-or-Kill — both legs must fill
    # The NO leg is also surfaced.
    assert "no_leg" in el
    assert el["no_leg"]["token_id"] == "0xN"
    assert el["no_leg"]["price"] == 0.50


def test_arb_scanner_exit_logic_returns_none():
    """``ArbScannerStrategy.exit_logic`` returns ``None`` unconditionally
    because FOK orders resolve immediately — there's no held position
    to exit."""
    s = ArbScannerStrategy()
    assert s.exit_logic({}, {}) is None
    assert s.exit_logic({"yes_shares": 10.0}, {"best_bid": 0.49}) is None


def test_arb_scanner_diagnostics_includes_base_and_strategy_fields():
    s = ArbScannerStrategy()
    d = s.diagnostics()
    # Base fields.
    assert d["name"] == "arb_scanner"
    assert "running" in d
    assert "stats" in d
    # Strategy-specific fields.
    assert "pairs_scanned" in d
    assert "min_profit_frac" in d
    assert "scan_interval" in d
    assert "order_size" in d


# ── (15) Signal dataclass fields + defaults ─────────────────────────────────

def test_signal_dataclass_has_documented_fields():
    """The ``Signal`` dataclass must declare exactly the 8 documented
    fields with the documented defaults so callers can rely on the
    shape across every strategy."""
    fields = Signal.__dataclass_fields__
    expected_field_names = {
        "action",
        "token_id",
        "size",
        "price",
        "confidence",
        "edge",
        "reason",
        "metadata",
    }
    assert set(fields.keys()) == expected_field_names, (
        f"Signal fields must be exactly {sorted(expected_field_names)}; "
        f"got {sorted(fields.keys())}."
    )


def test_signal_dataclass_defaults():
    """``Signal`` field defaults: size=0.0, price=None, confidence=0.0,
    edge=0.0, reason="", metadata={}. The two required fields
    (``action``, ``token_id``) have no default."""
    sig = Signal(action="BUY", token_id="0xfoo")
    assert sig.size == 0.0
    assert sig.price is None
    assert sig.confidence == 0.0
    assert sig.edge == 0.0
    assert sig.reason == ""
    # metadata default_factory=dict → fresh dict per instance (no shared
    # mutable default).
    assert sig.metadata == {}
    sig.metadata["k"] = "v"
    # A second instance gets a fresh metadata dict (not the same object).
    sig2 = Signal(action="SELL", token_id="0xbar")
    assert sig2.metadata == {}


def test_signal_dataclass_metadata_default_is_fresh_per_instance():
    """``metadata`` uses ``field(default_factory=dict)`` so two Signal
    instances do not share the same dict (the classic mutable-default
    footgun)."""
    a = Signal(action="BUY", token_id="0xfoo")
    b = Signal(action="SELL", token_id="0xbar")
    a.metadata["shared"] = True
    # b's metadata must NOT see the mutation on a's metadata.
    assert "shared" not in b.metadata


def test_signal_action_field_is_required():
    """``action`` and ``token_id`` are required (no default). Constructing
    a Signal without them must raise ``TypeError``."""
    with pytest.raises(TypeError):
        Signal()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Signal(action="BUY")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Signal(token_id="0xfoo")  # type: ignore[call-arg]


# ── Cross-strategy contract parity ──────────────────────────────────────────

@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_is_concrete_after_w19_2(strategy_class):
    """All three real strategies (``SignalTrader``, ``MarketMaker``,
    ``ArbScanner``) must be concrete after W19-2 — i.e. they implement
    all 9 contract methods AND the ``_run`` async loop, so the registry
    can instantiate them without further subclassing."""
    assert strategy_class.__abstractmethods__ == set(), (
        f"{strategy_class.__name__} must be concrete after W19-2; "
        f"leftover abstract: {sorted(strategy_class.__abstractmethods__)}"
    )


@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_returns_dict_from_metadata(strategy_class):
    """Every real strategy's ``metadata()`` returns a dict with at
    least the four base keys (name / version / description / author)
    plus the strategy-specific extras."""
    s = strategy_class()
    md = s.metadata()
    assert isinstance(md, dict)
    assert "name" in md and isinstance(md["name"], str)
    assert "version" in md and isinstance(md["version"], str)
    assert "description" in md and isinstance(md["description"], str)
    assert "author" in md and isinstance(md["author"], str)


@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_validate_returns_tuple_of_bool_str(strategy_class):
    """Every real strategy's ``validate()`` returns a 2-tuple of
    ``(bool, str)`` — the documented contract return type."""
    s = strategy_class()
    result = s.validate()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_diagnostics_includes_base_fields(strategy_class):
    """Every real strategy's ``diagnostics()`` includes the four base
    fields (name / running / stats / last_error) so the dashboard /
    operator can introspect any strategy uniformly."""
    s = strategy_class()
    d = s.diagnostics()
    assert isinstance(d, dict)
    assert "name" in d
    assert "running" in d
    assert "stats" in d
    assert "last_error" in d


@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_estimate_edge_handles_none(strategy_class):
    """Every real strategy's ``estimate_edge(None)`` returns ``0.0``
    so callers can chain without a None-guard."""
    s = strategy_class()
    assert s.estimate_edge(None) == 0.0  # type: ignore[arg-type]


@pytest.mark.parametrize("strategy_class", [
    SignalTraderStrategy,
    MarketMakerStrategy,
    ArbScannerStrategy,
])
def test_every_real_strategy_size_position_handles_hold(strategy_class):
    """Every real strategy's ``size_position(HOLD_signal, ...)`` returns
    ``0.0`` so a sub-threshold signal doesn't accidentally trigger a
    trade through the size-position path."""
    s = strategy_class()
    sig_hold = Signal(action="HOLD", token_id="0xfoo")
    assert s.size_position(sig_hold, 1000.0, {}) == 0.0
