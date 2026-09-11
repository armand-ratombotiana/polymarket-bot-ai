"""tests/test_dedup.py — unit tests for the unified deduplication registry.

W24-6 — comprehensive duplicate event prevention.

Covers the public ``DedupRegistry`` contract:

  (1) ``check_and_add`` returns ``True`` for a NEW (non-duplicate) entity.
  (2) ``check_and_add`` returns ``False`` for a DUPLICATE (same key in the
      same TTL window).
  (3) TTL expiration — the same key submitted in two DIFFERENT TTL windows
      is treated as two unique entities (the second is NOT a duplicate).
  (4) Different entity types are INDEPENDENT — recording the same key
      under two entity_types does NOT cross-dedup.
  (5) ``get_stats`` returns the right per-type counters (total_seen /
      duplicates_blocked / unique_passed / duplicate_rate).
  (6) ``get_stats(entity_type=X)`` filters to one type.
  (7) ``get_stats(entity_type=unknown)`` returns a zeroed stub so the
      API shape is stable for pre-listed types.
  (8) ``clear(entity_type=X)`` wipes ONLY that type.
  (9) ``clear()`` (no arg) wipes EVERY type.
  (10) Memory bound — the registry's ``deque(maxlen=10000)`` evicts the
       oldest entries once full (no unbounded growth).
  (11) TTL=0 / negative TTL falls back to a 1s bucket rather than
       ``ZeroDivisionError``.
  (12) The singleton ``dedup_registry`` is shared across the process so
       wiring call sites in ``strategies/base.py`` / ``paper/simulator.py``
       / ``core/decision_ledger.py`` / ``core/alerting.py`` / ``core/
       live_fill_monitor.py`` all see the same registry.

Each test constructs a fresh ``DedupRegistry()`` so there is zero state
leakage between tests; the autouse ``_reset_store_factory_defaults``
fixture in ``tests/conftest.py`` additionally clears the singleton so
production-code-path tests that go through the singleton start clean.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads os.environ at module-import time. ────────────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists purely
# so this test module remains self-contained when imported outside the
# pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/dedup_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-dedup",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from. Mirrors the
# bootstrap pattern in every existing ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.dedup import (  # noqa: E402
    DedupRegistry,
    DedupStats,
    dedup_registry,
)

# All tests in this module are SYNC ``def`` (the registry is fully sync),
# EXCEPT the fire_alert wire-up test which is ``async def`` and uses the
# per-test ``@pytest.mark.asyncio`` decorator. The repo's ``pytest.ini``
# leaves ``asyncio_mode`` at the pytest-asyncio default (``strict``); the
# per-test mark idiom opts the lone async test in without editing
# ``pytest.ini`` / ``pyproject.toml``. Running the fire_alert test inside
# an event loop avoids the ``RuntimeWarning: coroutine was never awaited``
# that would otherwise leak from ``asyncio.create_task`` inside fire_alert
# when called from a sync context (the broadcast coroutine is created
# before ``create_task`` raises ``RuntimeError``, then dropped).


# ── Fixture: fresh DedupRegistry per test ───────────────────────────────────
@pytest.fixture
def registry():
    """Fresh ``DedupRegistry`` (no shared state with the singleton).

    Each test gets its own instance so the test's calls do NOT pollute
    the singleton ``dedup_registry`` (which is shared with production
    code paths exercised by sibling test modules — the autouse
    ``_reset_store_factory_defaults`` fixture clears the singleton
    between tests; using a fresh instance here is belt-and-braces so a
    future refactor that removes the autouse clear does NOT break these
    tests' isolation guarantees).
    """
    return DedupRegistry()


# ═══════════════════════════════════════════════════════════════════════════
# 1. check_and_add returns True for a NEW entity
# ═══════════════════════════════════════════════════════════════════════════
def test_check_and_add_returns_true_for_new_entity(registry: DedupRegistry):
    """A first-time key must return ``True`` (the entity is new)."""
    assert registry.check_and_add("order", "token-1:BUY:10:0.55") is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. check_and_add returns False for a duplicate
# ═══════════════════════════════════════════════════════════════════════════
def test_check_and_add_returns_false_for_duplicate(registry: DedupRegistry):
    """A repeat call with the same key + same TTL window must return False."""
    key = "token-1:BUY:10:0.55"
    assert registry.check_and_add("order", key) is True
    assert registry.check_and_add("order", key) is False
    # A third call is STILL a duplicate (the key stays in the deque for
    # the duration of the TTL window — idempotent within the window).
    assert registry.check_and_add("order", key) is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. TTL expiration — same key in a DIFFERENT TTL window is unique
# ═══════════════════════════════════════════════════════════════════════════
def test_ttl_expiration_same_key_after_ttl_passes(registry: DedupRegistry):
    """A key submitted in two DIFFERENT TTL windows is NOT a duplicate.

    The TTL is implemented as ``int(time.time() / ttl_seconds)`` — two
    calls within the same window share a bucket and dedup; two calls in
    adjacent windows have different buckets and pass. This test uses a
    1-second TTL and ``time.sleep`` so the second call lands in a
    different bucket (the wall clock has advanced past the bucket
    boundary).
    """
    ttl = 1  # 1 second — fast enough for a unit test
    key = "decision-1:PREDICTION"
    # First call — new.
    assert registry.check_and_add("decision", key, ttl_seconds=ttl) is True
    # Second call immediately — duplicate (same bucket).
    assert registry.check_and_add("decision", key, ttl_seconds=ttl) is False
    # Sleep past the bucket boundary so the next call lands in a fresh
    # bucket. ``ttl + 0.05`` is ample margin even on a heavily-loaded CI
    # box (the bucket is whole-second aligned, so any sleep > 1 s lands
    # in a strictly-later bucket).
    time.sleep(ttl + 0.05)
    # Third call — new (different bucket).
    assert registry.check_and_add("decision", key, ttl_seconds=ttl) is True


# ═══════════════════════════════════════════════════════════════════════════
# 4. Different entity types are independent
# ═══════════════════════════════════════════════════════════════════════════
def test_different_entity_types_are_independent(registry: DedupRegistry):
    """Recording the same key under two entity_types does NOT cross-dedup.

    E.g. an ``order`` keyed ``token-1:BUY:10:0.55`` and an ``alert`` keyed
    ``token-1:BUY:10:0.55`` are different entities and must NOT collide.
    """
    key = "token-1:BUY:10:0.55"
    # Record under "order".
    assert registry.check_and_add("order", key) is True
    # Same key under "alert" — must NOT be dedup'd by the order registry.
    assert registry.check_and_add("alert", key) is True
    # Same key under "alert" again — NOW it's a duplicate (within "alert").
    assert registry.check_and_add("alert", key) is False
    # And the order registry is unchanged — same key under "order" is
    # STILL a duplicate of the order entry (not affected by the alert
    # calls above).
    assert registry.check_and_add("order", key) is False


# ═══════════════════════════════════════════════════════════════════════════
# 5. get_stats returns the right per-type counters
# ═══════════════════════════════════════════════════════════════════════════
def test_get_stats_returns_per_type_counters(registry: DedupRegistry):
    """``get_stats`` returns the canonical per-type counters."""
    # Record 3 unique + 2 duplicate calls under "order".
    assert registry.check_and_add("order", "k1") is True   # unique #1
    assert registry.check_and_add("order", "k2") is True   # unique #2
    assert registry.check_and_add("order", "k1") is False  # duplicate #1
    assert registry.check_and_add("order", "k3") is True   # unique #3
    assert registry.check_and_add("order", "k2") is False  # duplicate #2

    stats = registry.get_stats()
    assert "order" in stats
    order_stats = stats["order"]
    assert order_stats["entity_type"] == "order"
    assert order_stats["total_seen"] == 5
    assert order_stats["unique_passed"] == 3
    assert order_stats["duplicates_blocked"] == 2
    # duplicate_rate = duplicates_blocked / total_seen = 2/5 = 0.4.
    assert order_stats["duplicate_rate"] == pytest.approx(0.4)


# ═══════════════════════════════════════════════════════════════════════════
# 6. get_stats(entity_type=X) filters to one type
# ═══════════════════════════════════════════════════════════════════════════
def test_get_stats_with_entity_type_filter_returns_one_type(registry: DedupRegistry):
    """``get_stats(entity_type=X)`` returns the stats dict for ONE type."""
    registry.check_and_add("order", "k1")
    registry.check_and_add("alert", "k1")
    registry.check_and_add("alert", "k1")  # duplicate

    order_stats = registry.get_stats(entity_type="order")
    alert_stats = registry.get_stats(entity_type="alert")

    # One-type shape — NOT nested under a top-level key.
    assert isinstance(order_stats, dict)
    assert order_stats["entity_type"] == "order"
    assert order_stats["total_seen"] == 1
    assert order_stats["unique_passed"] == 1
    assert order_stats["duplicates_blocked"] == 0

    assert alert_stats["entity_type"] == "alert"
    assert alert_stats["total_seen"] == 2
    assert alert_stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 7. get_stats(entity_type=unknown) returns a zeroed stub
# ═══════════════════════════════════════════════════════════════════════════
def test_get_stats_with_unknown_entity_type_returns_zeroed_stub(registry: DedupRegistry):
    """An unknown entity_type returns a zeroed ``DedupStats`` stub so the
    API shape is stable for callers that pre-list the entity types they
    care about (e.g. the dashboard's "Dedup" panel always renders the
    same six rows: order / fill / decision / alert / audit / snapshot).
    """
    stats = registry.get_stats(entity_type="audit")
    assert stats["entity_type"] == "audit"
    assert stats["total_seen"] == 0
    assert stats["unique_passed"] == 0
    assert stats["duplicates_blocked"] == 0
    assert stats["duplicate_rate"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 8. clear(entity_type=X) wipes ONLY that type
# ═══════════════════════════════════════════════════════════════════════════
def test_clear_one_entity_type_leaves_others_intact(registry: DedupRegistry):
    """``clear(entity_type=X)`` wipes ONLY that type's registry + stats."""
    # Populate two types.
    registry.check_and_add("order", "k1")
    registry.check_and_add("alert", "k1")
    # Sanity: both types have stats.
    assert "order" in registry.get_stats()
    assert "alert" in registry.get_stats()

    # Clear ONLY "order".
    registry.clear(entity_type="order")
    # "order" is gone; "alert" is intact.
    assert "order" not in registry.get_stats()
    assert "alert" in registry.get_stats()
    # A subsequent check_and_add for "order" starts fresh — same key is
    # treated as new (not a duplicate of the pre-clear entry).
    assert registry.check_and_add("order", "k1") is True


# ═══════════════════════════════════════════════════════════════════════════
# 9. clear() (no arg) wipes EVERY type
# ═══════════════════════════════════════════════════════════════════════════
def test_clear_all_wipes_every_entity_type(registry: DedupRegistry):
    """``clear()`` with no arg clears every entity_type."""
    registry.check_and_add("order", "k1")
    registry.check_and_add("alert", "k1")
    registry.check_and_add("decision", "k1")

    registry.clear()

    stats = registry.get_stats()
    assert stats == {}


# ═══════════════════════════════════════════════════════════════════════════
# 10. Memory bound — deque(maxlen=10000) evicts oldest entries
# ═══════════════════════════════════════════════════════════════════════════
def test_memory_bound_evicts_oldest_entries(registry: DedupRegistry):
    """The per-type registry is bounded — once 10000 entries are
    recorded, the OLDEST is evicted so a re-submission of the evicted
    key is treated as NEW (not a duplicate).

    This is the load-bearing memory guarantee: a runaway caller that
    hammers ``check_and_add`` with novel keys cannot OOM the process —
    the registry is O(1) memory per entity_type.
    """
    # Fill the registry past its maxlen with unique keys.
    for i in range(10010):
        registry.check_and_add("order", f"k{i}")

    stats = registry.get_stats(entity_type="order")
    # Every call was unique (no duplicates among 10010 distinct keys).
    assert stats["unique_passed"] == 10010
    assert stats["duplicates_blocked"] == 0

    # The first 10 keys (k0..k9) have been evicted — re-submitting them
    # must return True (they're NEW again from the registry's POV).
    assert registry.check_and_add("order", "k0") is True
    assert registry.check_and_add("order", "k5") is True
    # The last 10 keys (k10000..k10009) are still in the registry —
    # re-submitting them must return False (duplicate).
    assert registry.check_and_add("order", "k10009") is False
    assert registry.check_and_add("order", "k10000") is False


# ═══════════════════════════════════════════════════════════════════════════
# 11. TTL=0 / negative TTL falls back to a 1s bucket
# ═══════════════════════════════════════════════════════════════════════════
def test_zero_or_negative_ttl_does_not_raise(registry: DedupRegistry):
    """A degenerate ``ttl_seconds=0`` (or negative) must NOT raise
    ``ZeroDivisionError`` — the registry falls back to a 1-second
    bucket so the caller's hot path is never broken by a misconfiguration.
    """
    # TTL = 0 — degenerate, must not raise.
    assert registry.check_and_add("order", "k1", ttl_seconds=0) is True
    # The same key within the same 1s bucket is a duplicate.
    assert registry.check_and_add("order", "k1", ttl_seconds=0) is False
    # Negative TTL — same degenerate handling.
    assert registry.check_and_add("alert", "k1", ttl_seconds=-5) is True
    assert registry.check_and_add("alert", "k1", ttl_seconds=-5) is False


# ═══════════════════════════════════════════════════════════════════════════
# 12. The singleton is shared across the process
# ═══════════════════════════════════════════════════════════════════════════
def test_singleton_is_shared_with_production_wiring():
    """The module-level ``dedup_registry`` singleton is the SAME instance
    every production code path imports. A call recorded under the
    singleton is visible to a subsequent ``from core.dedup import
    dedup_registry`` import (e.g. inside ``strategies/base.submit_order``
    or ``core/decision_ledger.record``).

    The autouse ``_reset_store_factory_defaults`` fixture in
    ``tests/conftest.py`` clears the singleton between tests, so this
    test starts with a clean registry.
    """
    # Import the singleton fresh (mirrors the production import pattern).
    from core.dedup import dedup_registry as singleton_a
    from core.dedup import dedup_registry as singleton_b

    # Both imports return the SAME instance.
    assert singleton_a is singleton_b
    # And it's the SAME instance re-exported at module top-level.
    assert singleton_a is dedup_registry

    # Record one entry on the singleton.
    singleton_a.check_and_add("order", "singleton-test-key")

    # A separate import sees the same registry state.
    stats = singleton_b.get_stats(entity_type="order")
    assert stats["total_seen"] == 1
    assert stats["unique_passed"] == 1

    # Cleanup so subsequent tests see a clean singleton (belt-and-braces
    # with the autouse fixture).
    singleton_a.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 13. DedupStats dataclass shape
# ═══════════════════════════════════════════════════════════════════════════
def test_dedup_stats_dataclass_has_canonical_fields():
    """``DedupStats`` must carry the five canonical fields the API
    endpoint (``GET /api/dedup/stats``) and the dashboard panel both
    render. A regression that renames / drops a field would break the
    wire contract.
    """
    s = DedupStats(
        entity_type="order",
        total_seen=10,
        duplicates_blocked=3,
        unique_passed=7,
        duplicate_rate=0.3,
    )
    # Five canonical fields.
    assert s.entity_type == "order"
    assert s.total_seen == 10
    assert s.duplicates_blocked == 3
    assert s.unique_passed == 7
    assert s.duplicate_rate == pytest.approx(0.3)
    # ``asdict``-friendly (used by ``get_stats``).
    from dataclasses import asdict
    d = asdict(s)
    assert set(d.keys()) == {
        "entity_type", "total_seen", "duplicates_blocked",
        "unique_passed", "duplicate_rate",
    }


# ═══════════════════════════════════════════════════════════════════════════
# 14. Thread-safety — concurrent check_and_add calls don't lose updates
# ═══════════════════════════════════════════════════════════════════════════
def test_concurrent_check_and_add_is_thread_safe(registry: DedupRegistry):
    """``check_and_add`` is thread-safe — concurrent calls from multiple
    threads cannot lose the registry's update (no torn writes, no double-
    True for the same key).

    Spawns N threads each calling ``check_and_add`` with the SAME key;
    exactly ONE thread must see ``True`` (the unique), every other thread
    must see ``False`` (duplicate). Without the internal ``self._lock``
    the lock-free ``if composite in registry: ... else: registry.append(...)``
    would race: two threads could both observe ``composite not in
    registry`` and both append, both returning True (a torn-write that
    breaks the duplicate-prevention guarantee).
    """
    import threading

    n_threads = 16
    key = "concurrent-test-key"
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _worker():
        # Synchronize start so every thread races the same instant.
        barrier.wait()
        ok = registry.check_and_add("order", key)
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly ONE thread saw True (the unique); the rest saw False.
    assert results.count(True) == 1
    assert results.count(False) == n_threads - 1


# ═══════════════════════════════════════════════════════════════════════════
# 15. Wire-up sanity — production code paths consult the registry
# ═══════════════════════════════════════════════════════════════════════════
def test_decision_ledger_record_dedup_wires_into_singleton():
    """``core.decision_ledger.record`` consults the singleton registry
    so a duplicate ``(decision_id, stage)`` call within the TTL window
    is silently skipped. This test exercises the wire-up end-to-end
    against the singleton (cleared by the autouse fixture).
    """
    import asyncio

    from core.decision_ledger import DecisionLedger, STAGE_PREDICTION
    from core.dedup import dedup_registry

    # Use the conftest-isolated ledger (tmp_path-scoped DB).
    ledger = DecisionLedger()

    did = "dec-wireup-test"
    asyncio.run(ledger.record(did, STAGE_PREDICTION, token_id="TOK_WIRE", strategy="s"))
    # Second call with the same (decision_id, stage) within the TTL
    # window — must be dedup'd (no second row in decision_events).
    asyncio.run(ledger.record(did, STAGE_PREDICTION, token_id="TOK_WIRE", strategy="s"))

    # The decision dedup registry recorded exactly one unique + one duplicate.
    stats = dedup_registry.get_stats(entity_type="decision")
    assert stats["total_seen"] == 2
    assert stats["unique_passed"] == 1
    assert stats["duplicates_blocked"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 16. Wire-up sanity — alerting.fire_alert dedup
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alerting_fire_alert_dedup_wires_into_singleton(tmp_path):
    """``core.alerting.AlertEngine.fire_alert`` consults the singleton
    registry so a duplicate ``alert_id`` within the TTL window is
    silently skipped (returns False) — the operator sees exactly one
    alert card per distinct alert_id.

    Async so ``fire_alert``'s ``asyncio.create_task(self._broadcast_alert)``
    lands on a running event loop (the W23-3 fire-and-forget broadcast
    contract). Without a loop, the broadcast coroutine is leaked
    (``RuntimeWarning: coroutine was never awaited``).
    """
    from core.alerting import Alert, AlertEngine, SEVERITY_CRITICAL
    from core.dedup import dedup_registry

    engine = AlertEngine(db_path=tmp_path / "alerts_dedup_test.db")
    alert = Alert(
        alert_id="w24-6-dedup-test-alert",
        timestamp=1234567890.0,
        category="risk",
        name="dedup_test",
        severity=SEVERITY_CRITICAL,
        message="Dedup wire-up test alert",
    )
    # First fire — goes through (returns True).
    ok1 = engine.fire_alert(alert)
    assert ok1 is True
    # Yield once so the scheduled broadcast coroutine lands (otherwise
    # it would be torn down with the test's event loop and pytest-asyncio
    # would surface a never-awaited warning).
    import asyncio as _asyncio
    await _asyncio.sleep(0)
    # Second fire — same alert_id within the TTL window, must be dedup'd.
    ok2 = engine.fire_alert(alert)
    assert ok2 is False
    # Yield again so the (skipped) broadcast path completes cleanly.
    await _asyncio.sleep(0)

    # The alert dedup registry recorded one unique + one duplicate.
    stats = dedup_registry.get_stats(entity_type="alert")
    assert stats["total_seen"] == 2
    assert stats["unique_passed"] == 1
    assert stats["duplicates_blocked"] == 1
