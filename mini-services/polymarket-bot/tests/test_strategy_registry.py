"""
W9-5 — Unit tests for ``strategies/registry.py``.

Covers the strategy catalog surface:

  1. ``StrategyRegistry.get_catalog()`` returns one entry per catalog row
     (50 strategies).
  2. ``get_catalog()`` flags each row with an ``implemented`` boolean that
     matches the documented implemented set (mm_avellaneda_stoikov,
     arb_binary_dutch_book, ml_random_forest_quant).
  3. ``get_catalog()`` flags ``is_running=False`` for all rows when no
     strategy has been started.
  4. ``get_catalog()`` row schema carries ``strategy_id``, ``name``,
     ``category``, ``description``, ``risk_level``, ``implemented``,
     ``is_running`` — the documented contract.
  5. The catalog covers all 6 categories: market_making, arbitrage,
     statistical, momentum, event_driven, machine_learning.
  6. ``get_active_instances()`` is empty initially.
  7. ``start_strategy(unknown_strategy_id)`` returns ``False`` and does
     not add an instance.
  8. ``stop_strategy(unknown_strategy_id)`` returns ``False`` and does
     not raise.
  9. ``start_strategy(QuantStrategyInstance meta)`` returns True and
     registers the instance under its strategy_id (stub-path: any
     non-implemented catalog entry instantiates a QuantStrategyInstance).
 10. ``start_strategy`` is idempotent — calling it twice returns True
     both times but only ONE instance is registered.
 11. ``stop_strategy`` on a running stub instance returns True and removes
     it from the active-instances dict.
 12. ``StrategyMeta`` dataclass exposes all six documented fields
     (strategy_id, name, category, description, risk_level,
     default_enabled).

Isolation
----------
Each test constructs a FRESH ``StrategyRegistry()`` instance — no
module-level singleton is touched.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
even though these tests are sync, the mark is harmless and keeps
collection consistent).
"""
from __future__ import annotations

import pytest

from strategies.registry import (
    STRATEGY_CATALOG,
    QuantStrategyInstance,
    StrategyMeta,
    StrategyRegistry,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def registry():
    """Fresh ``StrategyRegistry`` per test (no singleton state leak)."""
    return StrategyRegistry()


# ── 1. get_catalog returns one entry per catalog row (50 strategies) ─────────
def test_get_catalog_returns_one_entry_per_strategy(registry):
    """``get_catalog`` must return exactly 50 entries — one per row in the
    STRATEGY_CATALOG constant."""
    catalog = registry.get_catalog()
    assert len(catalog) == 50
    assert len(catalog) == len(STRATEGY_CATALOG)


# ── 2. get_catalog flags implemented set correctly ───────────────────────────
def test_get_catalog_flags_implemented_set_correctly(registry):
    """``implemented=True`` only for the eleven concrete-strategy catalog
    entries — the three original concrete strategies
    (mm_avellaneda_stoikov, arb_binary_dutch_book, ml_random_forest_quant)
    plus the three W19-6 additions (stat_ornstein_uhlenbeck,
    mom_macd_histogram, ml_isotonic_calibrated) plus the five W22-3
    additions (arb_cross_correlation, event_news_sentiment,
    event_resolution_sniper, mm_asymmetric_spread, mm_grid_liquidity)."""
    catalog = registry.get_catalog()
    implemented = [row for row in catalog if row["implemented"]]
    implemented_ids = {row["strategy_id"] for row in implemented}

    assert implemented_ids == {
        "mm_avellaneda_stoikov",
        "arb_binary_dutch_book",
        "ml_random_forest_quant",
        # W19-6 additions.
        "stat_ornstein_uhlenbeck",
        "mom_macd_histogram",
        "ml_isotonic_calibrated",
        # W22-3 additions — promoted from PLANNED to IMPLEMENTED.
        "arb_cross_correlation",
        "event_news_sentiment",
        "event_resolution_sniper",
        "mm_asymmetric_spread",
        "mm_grid_liquidity",
    }
    assert len(implemented) == 11


# ── 2b. get_catalog flags implemented_only filter ───────────────────────────
def test_get_catalog_implemented_only_filter_returns_eleven(registry):
    """``implemented_only=True`` returns only the eleven IMPLEMENTED rows."""
    catalog = registry.get_catalog(implemented_only=True)
    assert len(catalog) == 11
    for row in catalog:
        assert row["status"] == "IMPLEMENTED"
        assert row["implemented"] is True


# ── 2c. get_catalog status field matches implemented flag ────────────────────
def test_get_catalog_status_field_matches_implemented_flag(registry):
    """The new ``status`` field is consistent with the legacy ``implemented``
    boolean: ``implemented`` is True iff ``status == "IMPLEMENTED"``."""
    catalog = registry.get_catalog()
    for row in catalog:
        if row["status"] == "IMPLEMENTED":
            assert row["implemented"] is True
        else:
            assert row["implemented"] is False
        # Status is one of the three documented values.
        assert row["status"] in {"IMPLEMENTED", "PLANNED", "EXPERIMENTAL"}


# ── 3. get_catalog flags is_running=False when no strategy started ──────────
def test_get_catalog_flags_is_running_false_when_no_strategy_started(registry):
    """``is_running`` must be False for ALL rows when no strategy has been
    started — the fresh-registry contract."""
    catalog = registry.get_catalog()
    for row in catalog:
        assert row["is_running"] is False


# ── 4. get_catalog row schema carries all documented fields ─────────────────
def test_get_catalog_row_schema_carries_all_documented_fields(registry):
    """Each catalog row must carry exactly the ten documented fields —
    no missing, no extras. W19-6 added ``status`` and ``default_enabled``
    to the row schema; W24-8 added ``is_disabled`` so the UI can surface
    the strategy-health-monitor auto-disable state; the legacy
    ``implemented`` boolean is retained for backward compatibility."""
    catalog = registry.get_catalog()
    expected_keys = {
        "strategy_id", "name", "category", "description",
        "risk_level", "status", "default_enabled",
        "implemented", "is_running", "is_disabled",
    }
    for row in catalog:
        assert set(row.keys()) == expected_keys


# ── 5. Catalog covers all 6 categories ──────────────────────────────────────
def test_catalog_covers_all_six_categories(registry):
    """The catalog must contain at least one strategy in each of the six
    documented categories: market_making, arbitrage, statistical, momentum,
    event_driven, machine_learning."""
    catalog = registry.get_catalog()
    categories = {row["category"] for row in catalog}
    assert categories == {
        "market_making", "arbitrage", "statistical",
        "momentum", "event_driven", "machine_learning",
    }


# ── 6. get_active_instances is empty initially ───────────────────────────────
def test_get_active_instances_is_empty_initially(registry):
    """A fresh ``StrategyRegistry`` has zero active instances."""
    assert registry.get_active_instances() == {}
    assert len(registry.get_active_instances()) == 0


# ── 7. start_strategy returns False for unknown id ─────────────────────────
async def test_start_strategy_returns_false_for_unknown_id(registry):
    """``start_strategy(unknown_strategy_id)`` returns ``False`` and does
    NOT add an instance to the registry."""
    ok = await registry.start_strategy("nonexistent_strategy_id")
    assert ok is False
    assert registry.get_active_instances() == {}


# ── 8. stop_strategy returns False for unknown id ──────────────────────────
async def test_stop_strategy_returns_false_for_unknown_id(registry):
    """``stop_strategy(unknown_strategy_id)`` returns ``False`` and does
    NOT raise."""
    ok = await registry.stop_strategy("nonexistent_strategy_id")
    assert ok is False
    assert registry.get_active_instances() == {}


# ── 9. start_strategy on stub (non-implemented) id returns True ─────────────
async def test_start_strategy_on_stub_returns_true_and_registers(registry):
    """A catalog entry that's NOT in the documented implemented set must
    still instantiate a ``QuantStrategyInstance`` (the stub fallback) when
    ``start_strategy`` is called — returning True and registering the
    instance under its strategy_id."""
    # Pick a stub entry: stat_bollinger_reversion (statistical, not in
    # the implemented set).
    stub_id = "stat_bollinger_reversion"
    assert stub_id in {s.strategy_id for s in STRATEGY_CATALOG}

    ok = await registry.start_strategy(stub_id)

    assert ok is True
    instances = registry.get_active_instances()
    assert stub_id in instances
    assert isinstance(instances[stub_id], QuantStrategyInstance)


# ── 10. start_strategy is idempotent ────────────────────────────────────────
async def test_start_strategy_is_idempotent(registry):
    """Calling ``start_strategy`` twice for the same id returns ``True`` both
    times but registers exactly ONE instance."""
    stub_id = "stat_rsi_divergence"
    ok1 = await registry.start_strategy(stub_id)
    ok2 = await registry.start_strategy(stub_id)

    assert ok1 is True
    assert ok2 is True

    instances = registry.get_active_instances()
    assert len(instances) == 1
    assert stub_id in instances


# ── 11. stop_strategy on a running stub returns True ────────────────────────
async def test_stop_strategy_on_running_stub_returns_true_and_removes(registry):
    """``stop_strategy`` on a running instance returns True and removes the
    instance from the active-instances dict."""
    stub_id = "stat_zscore_anomaly"
    await registry.start_strategy(stub_id)
    assert stub_id in registry.get_active_instances()

    ok = await registry.stop_strategy(stub_id)
    assert ok is True
    assert stub_id not in registry.get_active_instances()


# ── 12. StrategyMeta dataclass exposes all documented fields ────────────────
def test_strategy_meta_dataclass_carries_all_documented_fields():
    """``StrategyMeta`` must expose all documented fields: strategy_id,
    name, category, description, risk_level, default_enabled, status
    (W19-6 addition — defaults to ``PLANNED``)."""
    meta = StrategyMeta(
        strategy_id="test_id",
        name="Test Strategy",
        category="statistical",
        description="A test strategy for unit testing",
        risk_level="Medium",
        default_enabled=False,
        status="IMPLEMENTED",
    )
    assert meta.strategy_id == "test_id"
    assert meta.name == "Test Strategy"
    assert meta.category == "statistical"
    assert meta.description == "A test strategy for unit testing"
    assert meta.risk_level == "Medium"
    assert meta.default_enabled is False
    assert meta.status == "IMPLEMENTED"

    # Defaults: ``default_enabled`` is False and ``status`` is PLANNED
    # (W19-6 — honest default; a new entry is assumed to be a stub
    # until explicitly marked IMPLEMENTED).
    meta2 = StrategyMeta("x", "y", "z", "w", "Low")
    assert meta2.default_enabled is False
    assert meta2.status == "PLANNED"
