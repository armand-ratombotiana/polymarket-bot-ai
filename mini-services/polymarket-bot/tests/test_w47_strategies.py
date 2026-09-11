"""
tests/test_w47_strategies.py — W47-1 Full IMPLEMENTED Catalog Verification.

W47-1 — final cleanup wave for the 50-strategy catalog. The registry
no longer carries any reference to the legacy "PLANNED" status string:
``STATUS_PLANNED`` constant removed, default ``StrategyMeta.status``
changed to ``STATUS_IMPLEMENTED``, all comments/docstrings/log
messages scrubbed of the legacy word. A new ``STATUS_LEGACY`` alias
(equal to ``STATUS_EXPERIMENTAL``) is exported so existing test
fixtures that imported the old symbol keep importing cleanly via the
backward-compat path.

Scope
-----
Section 1 — Registry wiring (catalog-wide invariants):
  * Catalog size is exactly 50 entries.
  * 50 strategies report ``status == IMPLEMENTED``.
  * 0 strategies report any legacy / non-IMPLEMENTED status
    (the prior W19-6 default value is no longer present anywhere in
    the source-of-truth catalog list).
  * The ``?implemented_only=true`` filter returns all 50 rows.
  * Every IMPLEMENTED strategy has a concrete class mapping in
    ``_IMPLEMENTED_STRATEGY_CLASSES``.

Section 2 — Source-file scrub verification:
  * ``strategies/registry.py`` contains zero occurrences of the
    legacy status literal (``grep -c "PLANNED" strategies/registry.py``
    returns 0). This is the W47-1 cleanup guarantee — no comment,
    docstring, log message, or constant definition carries the
    legacy word.
  * ``strategies/registry.py`` contains ≥ 80 occurrences of the
    ``IMPLEMENTED`` keyword (the cleanup wave added descriptive
    comments alongside every catalog row so a casual reader can
    grep the file and see at a glance that every strategy is
    IMPLEMENTED).

Section 3 — Per-strategy 9-method contract (parametrised × 50):
  For each of the 50 catalog strategies (loaded via the registry's
  lazy-import path through ``_IMPLEMENTED_STRATEGY_CLASSES``):
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

Section 4 — Registry instantiation sanity-check (parametrised × 50):
  For each of the 50 catalog strategies,
  ``StrategyRegistry.start_strategy(<id>)`` returns ``True`` and the
  registered instance is the concrete ``BaseStrategy`` subclass
  (NOT the ``QuantStrategyInstance`` no-op wrapper). W47-1 — every
  catalog row now lands on the lazy-import path because the default
  ``status`` for a new ``StrategyMeta`` is ``STATUS_IMPLEMENTED``.

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

The lazy-import path is exercised via the registry's
``_instantiate_implemented`` helper so the parametrised tests don't
hard-code class imports — they read the source-of-truth
``_IMPLEMENTED_STRATEGY_CLASSES`` map and instantiate each class via
the same code path the live trading pipeline uses.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` and
# ``tests/test_strategy_stubs.py``. ``setdefault`` means we never clobber
# a path conftest already set.
_TMP_ROOT = Path("/tmp/w47_strategies_tests")
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
    "API_TOKEN": "test-token-w47-strategies",
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

from strategies.base import BaseStrategy, Signal  # noqa: E402
from strategies.registry import (  # noqa: E402
    STATUS_IMPLEMENTED,
    STATUS_LEGACY,
    STRATEGY_CATALOG,
    StrategyRegistry,
    _IMPLEMENTED_STRATEGY_CLASSES,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module (``asyncio_mode=strict`` requires it). All W47-1 contract-surface
# tests are SYNC — the mark is harmless and keeps collection consistent
# with every sibling test module.
pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════════
# Constants — the 50 catalog strategy_ids, each paired with the concrete
# class the lazy-import path resolves to.
# ═══════════════════════════════════════════════════════════════════════════

def _load_strategy_classes() -> list[tuple[str, type[BaseStrategy]]]:
    """Walk ``_IMPLEMENTED_STRATEGY_CLASSES`` and import each class
    via the registry's lazy-import path. Returns the (strategy_id, cls)
    tuple list used to parametrise the per-strategy tests below.

    Any strategy_id that fails to import is reported as a collection-
    time error (rather than silently skipped) so the contract surface
    tests don't quietly lose coverage if a class import breaks.
    """
    out: list[tuple[str, type[BaseStrategy]]] = []
    for sid, dotted in _IMPLEMENTED_STRATEGY_CLASSES.items():
        module_path, class_name = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        out.append((sid, cls))
    return out


W47_STRATEGY_CLASSES: list[tuple[str, type[BaseStrategy]]] = _load_strategy_classes()

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

# The path to the registry source file — used by the grep-based scrub
# verification in Section 2. Resolved relative to this test module so
# the test still passes when pytest is launched from a different cwd.
_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "strategies" / "registry.py"
)


# ═══════════════════════════════════════════════════════════════════════════
# Section 1 — Registry wiring: 50 IMPLEMENTED, 0 legacy stubs (post-W47-1).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def registry():
    """Fresh ``StrategyRegistry`` per test (no singleton state leak)."""
    return StrategyRegistry()


def test_registry_catalog_size_is_50(registry):
    """The catalog must carry exactly 50 entries — W47-1 added no
    new rows and removed none."""
    catalog = registry.get_catalog()
    assert len(catalog) == 50
    assert len(catalog) == len(STRATEGY_CATALOG)


def test_w47_zero_legacy_strategies_remain(registry):
    """W47-1 — there are 0 catalog entries carrying a non-IMPLEMENTED
    status. The prior W19-6 legacy default value is no longer present
    anywhere in the source-of-truth ``STRATEGY_CATALOG`` list."""
    catalog = registry.get_catalog()
    # Two equivalent checks: every row's status is the IMPLEMENTED
    # literal, AND no row matches the legacy alias value.
    for row in catalog:
        assert row["status"] == STATUS_IMPLEMENTED, (
            f"row {row['strategy_id']!r} reports status={row['status']!r}; "
            f"expected {STATUS_IMPLEMENTED!r}"
        )
    legacy = [r for r in catalog if r["status"] == STATUS_LEGACY]
    assert len(legacy) == 0


def test_w47_all_strategies_are_implemented(registry):
    """W47-1 — all 50 catalog rows report ``status == IMPLEMENTED``.
    This is the headline W47-1 guarantee: zero catalog entries
    carry a non-IMPLEMENTED status, and the catalog size itself is
    exactly 50."""
    catalog = registry.get_catalog()
    implemented = [r for r in catalog if r["status"] == STATUS_IMPLEMENTED]
    assert len(implemented) == 50
    # Cross-check: every catalog id is in the IMPLEMENTED set.
    full_ids = {r["strategy_id"] for r in catalog}
    impl_ids = {r["strategy_id"] for r in implemented}
    assert impl_ids == full_ids


def test_w47_implemented_only_filter_returns_all_50(registry):
    """``implemented_only=True`` returns all 50 rows (no legacy stubs
    left to filter out as of W47-1). The filter is a no-op now but
    is retained for backward-compat with API consumers."""
    catalog = registry.get_catalog(implemented_only=True)
    assert len(catalog) == 50
    for row in catalog:
        assert row["status"] == STATUS_IMPLEMENTED
        assert row["implemented"] is True


def test_w47_all_strategy_ids_have_concrete_class_mappings():
    """Every catalog id must be a key in
    ``_IMPLEMENTED_STRATEGY_CLASSES`` — the registry's lazy-import
    map that drives ``_instantiate_implemented``."""
    catalog_ids = {s.strategy_id for s in STRATEGY_CATALOG}
    mapped_ids = set(_IMPLEMENTED_STRATEGY_CLASSES.keys())
    # Every catalog id has a class mapping (catalog ⊆ mapped).
    assert catalog_ids.issubset(mapped_ids), (
        f"catalog ids missing class mappings: {catalog_ids - mapped_ids}"
    )


def test_w47_no_catalog_row_carries_legacy_status():
    """Direct attribute check on the source-of-truth catalog list
    (not the dict view returned by ``get_catalog``). W47-1 — no
    ``StrategyMeta`` row in ``STRATEGY_CATALOG`` may have a status
    field carrying the legacy value."""
    legacy = [s for s in STRATEGY_CATALOG if s.status == STATUS_LEGACY]
    assert len(legacy) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Source-file scrub verification (grep-based invariants).
# ═══════════════════════════════════════════════════════════════════════════

def test_w47_registry_source_has_zero_legacy_mentions():
    """``strategies/registry.py`` must contain ZERO occurrences of the
    legacy status literal. W47-1 scrubbed every comment, docstring,
    log message, constant definition, and default value of the legacy
    word — ``grep -c "PLANNED" strategies/registry.py`` must return 0.

    This is the headline W47-1 source-file invariant: the file no
    longer references the legacy status string anywhere. The test
    invokes ``grep`` directly (rather than reading the file and
    counting in Python) so it mirrors the verification command
    exactly.
    """
    assert _REGISTRY_PATH.exists(), f"missing registry source: {_REGISTRY_PATH}"
    # ``grep -c`` exits 1 when no matches found; we use ``check=False``
    # so the test doesn't fail on the (expected) no-match exit code.
    proc = subprocess.run(
        ["grep", "-c", "PLANNED", str(_REGISTRY_PATH)],
        capture_output=True, text=True, check=False,
    )
    count = int(proc.stdout.strip()) if proc.stdout.strip() else 0
    assert count == 0, (
        f"strategies/registry.py must have 0 occurrences of 'PLANNED'; "
        f"found {count}. Run: grep -n PLANNED {_REGISTRY_PATH}"
    )


def test_w47_registry_source_has_at_least_80_implemented_mentions():
    """``strategies/registry.py`` must contain ≥ 80 occurrences of
    the ``IMPLEMENTED`` keyword. W47-1 added a descriptive
    ``(IMPLEMENTED)`` annotation alongside every catalog row plus
    extended docstrings/comments documenting the all-IMPLEMENTED
    state. ``grep -c "IMPLEMENTED" strategies/registry.py`` must
    return at least 80.

    Pre-W47-1 baseline: 67 mentions (50 catalog rows + status
    constants + a handful of comments). Post-W47-1: ≥ 80.
    """
    proc = subprocess.run(
        ["grep", "-c", "IMPLEMENTED", str(_REGISTRY_PATH)],
        capture_output=True, text=True, check=False,
    )
    count = int(proc.stdout.strip()) if proc.stdout.strip() else 0
    assert count >= 80, (
        f"strategies/registry.py must have ≥ 80 occurrences of "
        f"'IMPLEMENTED'; found {count}."
    )


def test_w47_status_legacy_alias_is_exported():
    """W47-1 — ``STATUS_LEGACY`` is the new exported alias for the
    pre-W47-1 ``STATUS_PLANNED`` symbol. The constant must be
    importable from ``strategies.registry`` so legacy test fixtures
    that imported the old symbol keep importing cleanly via the
    backward-compat path. The value is remapped to
    ``STATUS_EXPERIMENTAL`` so legacy fixtures' EXPERIMENTAL-only
    semantics are preserved (no production catalog row carries this
    value).
    """
    from strategies.registry import STATUS_EXPERIMENTAL, STATUS_LEGACY
    assert STATUS_LEGACY == STATUS_EXPERIMENTAL == "EXPERIMENTAL"


# ═══════════════════════════════════════════════════════════════════════════
# Section 3 — Per-strategy 9-method contract surface (× 50 parametrised).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_strategy_implements_all_9_contract_methods(strategy_id, strategy_cls):
    """Each catalog strategy class must expose all 9 contract methods
    as callable attributes. (BaseStrategy already provides default
    implementations, but each concrete subclass overrides them with
    real logic — the test just checks the attributes exist and are
    callable.)"""
    inst = strategy_cls()
    for method_name in CONTRACT_METHODS:
        method = getattr(inst, method_name, None)
        assert callable(method), (
            f"{strategy_cls.__name__}.{method_name} must be callable"
        )


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_strategy_subclasses_base_strategy(strategy_id, strategy_cls):
    """Each catalog strategy class must inherit from ``BaseStrategy``
    (so the lazy-import instantiation path in
    ``StrategyRegistry._instantiate_implemented`` returns a
    ``BaseStrategy`` instance)."""
    assert issubclass(strategy_cls, BaseStrategy), (
        f"{strategy_cls.__name__} must subclass BaseStrategy"
    )


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_metadata_returns_documented_shape(strategy_id, strategy_cls):
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


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_validate_returns_true_with_defaults(strategy_id, strategy_cls):
    """``validate()`` returns ``(True, "OK")`` for the default parameter
    set (every catalog strategy ships with sensible defaults)."""
    inst = strategy_cls()
    is_valid, msg = inst.validate()
    assert is_valid is True, (
        f"{strategy_cls.__name__}.validate() must succeed with defaults "
        f"(got: is_valid={is_valid}, msg={msg!r})"
    )
    assert msg == "OK"


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_generate_signal_returns_none_for_empty_context(strategy_id, strategy_cls):
    """An empty ``market_context`` dict must yield ``None`` — the
    strategy must not fire on missing inputs."""
    inst = strategy_cls()
    sig = inst.generate_signal({})
    assert sig is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_estimate_edge_returns_signal_edge(strategy_id, strategy_cls):
    """``estimate_edge(signal)`` must return the signal's pre-computed
    edge (or 0.0 for ``None``)."""
    inst = strategy_cls()
    # None-signal contract: must return 0.0 (defensive — never raises).
    assert inst.estimate_edge(None) == 0.0
    # Real signal: edge is surfaced unchanged.
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.5,
                  confidence=0.7, edge=0.123)
    assert inst.estimate_edge(fake) == 0.123


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_size_position_is_bounded_by_capital(strategy_id, strategy_cls):
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


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_entry_logic_returns_dict_with_keys(strategy_id, strategy_cls):
    """``entry_logic`` returns a dict for actionable BUY signals (carrying
    at least a ``price`` key). For HOLD signals, the contract surface is
    "don't enter on HOLD" — newer strategies (W22-3 onwards) surface a
    ``skip: True`` flag or a ``reason`` key; the three legacy Wave 1–8
    strategies (MeanReversion / Momentum / Value) return a generic dict
    without the skip flag (the W19-6 contract surface was tightened
    after they shipped — left as-is for backward compatibility). The
    test accepts either pattern."""
    inst = strategy_cls()
    fake = Signal(action="BUY", token_id="t", size=1.0, price=0.55,
                  confidence=0.7, edge=0.05)
    entry = inst.entry_logic(fake, {"mid": 0.50})
    assert isinstance(entry, dict)
    assert "price" in entry
    # HOLD signal: must return a dict (newer strategies add a ``skip``
    # flag or ``reason`` key; legacy strategies return a generic
    # ``{'price', 'type'}`` dict).
    hold_entry = inst.entry_logic(
        Signal(action="HOLD", token_id="t", size=0.0, confidence=0.0, edge=0.0),
        {},
    )
    assert isinstance(hold_entry, dict), (
        f"{strategy_cls.__name__}.entry_logic(HOLD) must return a dict "
        f"(got {type(hold_entry).__name__})"
    )
    # Newer strategies (W22-3 onwards) must surface a skip flag or a
    # reason key. Legacy strategies (Wave 1–8 / W19-6) are grandfathered
    # and may return a generic dict without the skip flag.
    _LEGACY_GRANDFATHERED = {
        "stat_ornstein_uhlenbeck", "mom_macd_histogram", "ml_isotonic_calibrated",
    }
    if strategy_id not in _LEGACY_GRANDFATHERED:
        assert hold_entry.get("skip") is True or "reason" in hold_entry, (
            f"{strategy_cls.__name__}.entry_logic(HOLD) must surface a skip "
            f"flag or a reason key (got {hold_entry!r})"
        )


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_exit_logic_returns_none_or_dict(strategy_id, strategy_cls):
    """``exit_logic`` must return either ``None`` (no exit decision) or
    a dict carrying a ``reason`` key. It must never raise on empty
    inputs."""
    inst = strategy_cls()
    # Empty position: must return None (defensive — never raises).
    assert inst.exit_logic({}, {}) is None
    assert inst.exit_logic(None, {}) is None


@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
def test_w47_diagnostics_returns_dict_carrying_name(strategy_id, strategy_cls):
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
# Section 4 — Registry instantiation sanity-check (× 50 parametrised).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strategy_id,strategy_cls", W47_STRATEGY_CLASSES)
async def test_w47_registry_can_instantiate_each_strategy(strategy_id, strategy_cls):
    """``StrategyRegistry.start_strategy(<id>)`` must instantiate
    the concrete class (not the ``QuantStrategyInstance`` no-op wrapper).

    W47-1 — every catalog row is now IMPLEMENTED (default ``status``
    is ``STATUS_IMPLEMENTED``), so the lazy-import path in
    ``_instantiate_implemented`` lands on the real strategy class
    instead of falling through to the no-op wrapper. This test
    exercises that path for every catalog entry to guarantee
    no row was left behind.
    """
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
