"""
tests/test_latency_wiring.py — W23-2 LatencyTracker contract + wiring tests.

Covers the four behavioural surfaces the W23-2 task spec asks for:

  1. **Signal generation records signal_time** — verified both as a
     direct ``LatencyTracker.record_signal`` contract test AND as an
     end-to-end wire-up test through ``SignalTraderStrategy._ml_signal``
     (the canonical call site).
  2. **Order submission records order_time** — verified both as a direct
     ``LatencyTracker.record_order`` contract test AND as an end-to-end
     wire-up test through ``BaseStrategy.submit_order`` (the canonical
     call site, mocked risk gate + paper sim).
  3. **Fill records fill_time and computes latencies** — verified both
     as a direct ``LatencyTracker.record_fill`` contract test AND as an
     end-to-end wire-up test through ``PaperSimulator._execute_fill``
     (the paper-mode canonical call site).
  4. **API routes** — ``GET /api/latency/stats`` /
     ``GET /api/latency/recent`` wired into ``api.server.app``,
     auth-protected, return the expected JSON shape.

The direct contract tests intentionally do NOT spin up a FastAPI app
(the ``LatencyTracker`` is a pure in-memory data structure with no I/O
dependencies). The API-route tests use ``fastapi.testclient.TestClient``
against the production ``api.server.app`` (mirrors the pattern in
``tests/test_profiling.py``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). ``setdefault`` means we never clobber a
# path the conftest already set; the duplicate redirect here exists
# purely so this test module remains self-contained when imported outside
# the pytest runner (e.g. by an IDE that doesn't load conftest first).
_TMP_ROOT = Path("/tmp/latency_wiring_tests")
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
    # Force the canonical trading mode to paper + live disabled so the
    # paper-mode branch of ``submit_order`` (``self._paper is True``) is
    # the path exercised end-to-end without the live-trading gate
    # short-circuiting anything.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-latency-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)
os.makedirs(_TMP_ROOT / "dao_data", exist_ok=True)
os.makedirs(_TMP_ROOT / "reports", exist_ok=True)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``strategies.*``, ``paper.*``) when pytest is invoked from
# a different cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402  (env must be set first)

from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import (  # noqa: E402
    Order,
    OrderBook,
    PriceLevel,
    Side,
    store,
)
from core.latency_tracker import (  # noqa: E402
    LatencyRecord,
    LatencyTracker,
    latency_tracker,
)
from strategies import base as base_module  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Note: this module intentionally does NOT declare a module-level
# ``pytestmark = pytest.mark.asyncio``. The direct-contract tests above
# are synchronous (the ``LatencyTracker`` is a pure in-memory data
# structure with no I/O dependencies), and the wire-up tests that drive
# the strategy / paper-sim / live-fill-monitor paths are async. Marking
# every test in the module as ``asyncio`` would trigger a
# ``PytestWarning`` for the sync tests; instead the async tests below
# carry ``@pytest.mark.asyncio`` individually.


# ── Per-test singleton reset ─────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_latency_singleton():
    """Reset the module-level singleton before AND after every test so a
    prior test's recorded latencies don't leak into the next test's
    assertions. Mirrors the autouse-reset convention in
    ``tests/test_rate_limit_tracker.py`` / ``tests/test_profiling.py``."""
    latency_tracker.reset()
    yield
    latency_tracker.reset()


# ═══════════════════════════════════════════════════════════════════════════
# 1. LatencyTracker.record_signal — direct contract
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordSignal:
    """Behavioural contract for ``LatencyTracker.record_signal``."""

    def test_record_signal_stores_metadata(self):
        t = LatencyTracker()
        before = time.time()
        t.record_signal(
            correlation_id="dec-1",
            token_id="tok-1",
            strategy="signal_trader",
        )
        after = time.time()
        recent = t.get_recent(10)
        assert len(recent) == 1
        rec = recent[0]
        assert rec["correlation_id"] == "dec-1"
        assert rec["token_id"] == "tok-1"
        assert rec["strategy"] == "signal_trader"
        assert rec["signal_time"] is not None
        assert before <= rec["signal_time"] <= after
        # No order/fill yet — both must be None and complete must be False.
        assert rec["order_time"] is None
        assert rec["fill_time"] is None
        assert rec["complete"] is False

    def test_record_signal_empty_correlation_id_no_op(self):
        """An empty correlation_id is silently dropped (no record created).

        Guards against a degenerate caller (e.g. a strategy that forgot
        to mint a decision_id) silently populating the tracker with
        empty-keyed records that would all collapse onto each other in
        the index."""
        t = LatencyTracker()
        t.record_signal(correlation_id="", token_id="tok", strategy="s")
        assert t.get_recent(10) == []
        assert t.get_stats()["total_records"] == 0

    def test_record_signal_idempotent_preserves_first_anchor(self):
        """A retried signal for the same correlation_id does NOT overwrite
        the original signal_time — the first-signal timestamp is the
        canonical anchor for downstream latency math."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        first_signal_time = t.get_recent(1)[0]["signal_time"]
        # Sleep so the second call's time.time() is strictly greater.
        time.sleep(0.002)
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        recent = t.get_recent(10)
        # Still exactly one record (idempotent — no duplicate appended).
        assert len(recent) == 1
        # signal_time unchanged.
        assert recent[0]["signal_time"] == first_signal_time

    def test_record_signal_fills_missing_fields_on_revisit(self):
        """If the first record_signal call omitted the token_id / strategy
        (e.g. a caller that only had the correlation_id), a subsequent
        call must populate them without overwriting signal_time."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["token_id"] == ""
        assert rec["strategy"] == ""
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        rec = t.get_recent(1)[0]
        assert rec["token_id"] == "tok"
        assert rec["strategy"] == "s"


# ═══════════════════════════════════════════════════════════════════════════
# 2. LatencyTracker.record_order — direct contract
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordOrder:
    """Behavioural contract for ``LatencyTracker.record_order``."""

    def test_record_order_computes_signal_to_order_ms(self):
        """record_order computes signal_to_order_ms when signal_time is set."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        time.sleep(0.005)  # 5 ms so the latency is non-trivial
        t.record_order(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["order_time"] is not None
        assert rec["signal_to_order_ms"] is not None
        # signal_to_order_ms must be ≥ the slept duration (5 ms) —
        # allow a generous upper bound (1 s) so a slow CI runner
        # doesn't flake.
        assert 5.0 <= rec["signal_to_order_ms"] <= 1000.0
        # No fill yet → order_to_fill / signal_to_fill still None.
        assert rec["order_to_fill_ms"] is None
        assert rec["signal_to_fill_ms"] is None
        assert rec["complete"] is False

    def test_record_order_without_prior_signal_creates_stub(self):
        """An order submitted without a prior record_signal (e.g. a manual
        order) creates a stub record anchored at order_time so the FILL
        stage can still anchor to it for the order→fill segment."""
        t = LatencyTracker()
        t.record_order(correlation_id="dec-manual")
        rec = t.get_recent(1)[0]
        assert rec["correlation_id"] == "dec-manual"
        assert rec["signal_time"] is None
        assert rec["order_time"] is not None
        # signal_to_order_ms must be None (no signal anchor to measure from).
        assert rec["signal_to_order_ms"] is None

    def test_record_order_empty_correlation_id_no_op(self):
        t = LatencyTracker()
        t.record_order(correlation_id="")
        assert t.get_recent(10) == []

    def test_record_order_idempotent_preserves_first_anchor(self):
        """A retried record_order for the same correlation_id does NOT
        overwrite the original order_time."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        t.record_order(correlation_id="dec-1")
        first_order_time = t.get_recent(1)[0]["order_time"]
        time.sleep(0.002)
        t.record_order(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["order_time"] == first_order_time


# ═══════════════════════════════════════════════════════════════════════════
# 3. LatencyTracker.record_fill — direct contract (computes latencies)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordFill:
    """Behavioural contract for ``LatencyTracker.record_fill``."""

    def test_record_fill_computes_all_three_latencies(self):
        """After signal → order → fill, all three latency segments must be
        populated and the record must be marked complete."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        time.sleep(0.005)
        t.record_order(correlation_id="dec-1")
        time.sleep(0.005)
        t.record_fill(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["complete"] is True
        assert rec["fill_time"] is not None
        assert rec["signal_to_order_ms"] is not None
        assert rec["order_to_fill_ms"] is not None
        assert rec["signal_to_fill_ms"] is not None
        # Latency ordering: signal_to_fill ≈ signal_to_order + order_to_fill.
        # Allow a generous epsilon for clock granularity + scheduler jitter.
        assert (
            rec["signal_to_fill_ms"]
            >= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] - 1.0
        )
        assert rec["signal_to_fill_ms"] <= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] + 1.0

    def test_record_fill_without_prior_stages_creates_stub(self):
        """A fill arriving for an un-tracked correlation_id creates a stub
        record anchored at fill_time (so the dashboard still surfaces the
        fill event even when the upstream record_signal / record_order
        calls failed)."""
        t = LatencyTracker()
        t.record_fill(correlation_id="dec-orphan")
        rec = t.get_recent(1)[0]
        assert rec["fill_time"] is not None
        assert rec["signal_time"] is None
        assert rec["order_time"] is None
        assert rec["signal_to_order_ms"] is None
        assert rec["order_to_fill_ms"] is None
        assert rec["signal_to_fill_ms"] is None

    def test_record_fill_computes_only_available_segments(self):
        """If only order_time is set (no signal_time), record_fill must
        compute order_to_fill_ms but leave signal_to_fill_ms None."""
        t = LatencyTracker()
        t.record_order(correlation_id="dec-1")
        time.sleep(0.005)
        t.record_fill(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["signal_time"] is None
        assert rec["order_to_fill_ms"] is not None
        assert rec["signal_to_fill_ms"] is None  # no signal anchor
        assert rec["complete"] is True

    def test_record_fill_idempotent(self):
        """A retried record_fill for the same correlation_id does NOT
        overwrite the original fill_time (mirrors the record_signal /
        record_order idempotency contract)."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="tok", strategy="s")
        t.record_order(correlation_id="dec-1")
        t.record_fill(correlation_id="dec-1")
        first_fill = t.get_recent(1)[0]["fill_time"]
        time.sleep(0.002)
        t.record_fill(correlation_id="dec-1")
        rec = t.get_recent(1)[0]
        assert rec["fill_time"] == first_fill


# ═══════════════════════════════════════════════════════════════════════════
# 4. LatencyTracker.get_stats — shape + percentile bucketing
# ═══════════════════════════════════════════════════════════════════════════


class TestGetStats:
    """Behavioural contract for ``LatencyTracker.get_stats``."""

    def test_get_stats_empty_returns_zeroes(self):
        t = LatencyTracker()
        stats = t.get_stats()
        assert stats["window_hours"] == 24.0
        assert stats["total_records"] == 0
        assert stats["complete_records"] == 0
        assert stats["in_flight_records"] == 0
        assert stats["orphaned_records"] == 0
        assert stats["signal_only_records"] == 0
        for seg in ("signal_to_order", "order_to_fill", "signal_to_fill"):
            seg_stats = stats["latencies_ms"][seg]
            assert seg_stats["count"] == 0
            assert seg_stats["avg"] == 0.0
            assert seg_stats["p50"] == 0.0
            assert seg_stats["p95"] == 0.0
            assert seg_stats["p99"] == 0.0
            assert seg_stats["max"] == 0.0
        assert stats["by_strategy"] == {}

    def test_get_stats_counts_complete_in_flight_orphaned_signal_only(self):
        """The four count buckets partition the trailing window:

          * ``complete_records``     — signal + order + fill all set
          * ``in_flight_records``    — signal + order set, fill pending
          * ``signal_only_records``   — signal set, no order/fill
          * ``orphaned_records``     — alias for in_flight (signal+order,
                                       no fill); preserved for the
                                       dashboard's "orphaned order"
                                       investigation view.
        """
        t = LatencyTracker()
        # 1 complete record (signal + order + fill).
        t.record_signal(correlation_id="dec-complete", token_id="t1", strategy="s")
        t.record_order(correlation_id="dec-complete")
        t.record_fill(correlation_id="dec-complete")
        # 1 in-flight (signal + order, no fill).
        t.record_signal(correlation_id="dec-inflight", token_id="t2", strategy="s")
        t.record_order(correlation_id="dec-inflight")
        # 1 signal-only.
        t.record_signal(correlation_id="dec-sigonly", token_id="t3", strategy="s")
        stats = t.get_stats()
        assert stats["total_records"] == 3
        assert stats["complete_records"] == 1
        assert stats["in_flight_records"] == 1
        assert stats["orphaned_records"] == 1
        assert stats["signal_only_records"] == 1

    def test_get_stats_by_strategy_breakdown(self):
        """``by_strategy`` groups complete records by strategy and
        surfaces a per-strategy p95 for each segment."""
        t = LatencyTracker()
        # Two strategies, each with one complete record.
        for strat in ("signal_trader", "momentum"):
            cid = f"dec-{strat}"
            t.record_signal(correlation_id=cid, token_id=f"tok-{strat}", strategy=strat)
            t.record_order(correlation_id=cid)
            t.record_fill(correlation_id=cid)
        stats = t.get_stats()
        assert set(stats["by_strategy"].keys()) == {"signal_trader", "momentum"}
        for strat, row in stats["by_strategy"].items():
            assert row["count"] == 1
            assert row["signal_to_order_p95_ms"] >= 0.0
            assert row["order_to_fill_p95_ms"] >= 0.0
            assert row["signal_to_fill_p95_ms"] >= 0.0

    def test_get_stats_window_filter(self):
        """``hours=0`` (or a tiny window) filters out records older than
        the cutoff so a dashboard can render a tight recent window."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-old", token_id="tok", strategy="s")
        # Backdate the signal_time so it falls outside a 0.001h window.
        with t._lock:
            rec = t._index["dec-old"]
            rec.signal_time = time.time() - 3600  # 1 hour ago
        t.record_signal(correlation_id="dec-new", token_id="tok", strategy="s")
        # hours=0 → cutoff = now → only the "dec-new" record (signal_time
        # within the same second) is included.
        stats = t.get_stats(hours=0.0)
        # When hours=0, the cutoff is exactly now. The "dec-new" record
        # was just recorded so its signal_time >= cutoff (within clock
        # granularity) — but the test must be defensive against the
        # boundary. Accept 0 or 1 (both are valid outcomes if the cutoff
        # falls on the exact microsecond of the record's signal_time).
        assert stats["total_records"] in (0, 1)
        # The backdated record is definitely excluded.
        if stats["total_records"] == 1:
            assert "dec-new" in {r["correlation_id"] for r in t.get_recent(10)}

    def test_get_stats_percentile_buckets(self):
        """With enough samples, p50 < p95 < p99 < max (the nearest-rank
        method produces monotonic percentiles for any sorted list)."""
        t = LatencyTracker()
        # Record 10 complete decisions with strictly increasing
        # signal→order latencies. We achieve this by sleeping a growing
        # amount between record_signal and record_order for each.
        for i in range(10):
            cid = f"dec-{i}"
            t.record_signal(correlation_id=cid, token_id="tok", strategy="s")
            time.sleep(0.001 * (i + 1))  # 1ms, 2ms, …, 10ms
            t.record_order(correlation_id=cid)
            t.record_fill(correlation_id=cid)
        stats = t.get_stats()
        seg = stats["latencies_ms"]["signal_to_order"]
        assert seg["count"] == 10
        # Monotonic: p50 <= p95 <= p99 <= max.
        assert seg["p50"] <= seg["p95"]
        assert seg["p95"] <= seg["p99"]
        assert seg["p99"] <= seg["max"]


# ═══════════════════════════════════════════════════════════════════════════
# 5. LatencyTracker.get_recent — newest-first + limit clamp
# ═══════════════════════════════════════════════════════════════════════════


class TestGetRecent:
    """Behavioural contract for ``LatencyTracker.get_recent``."""

    def test_get_recent_returns_newest_first(self):
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="t1", strategy="s")
        time.sleep(0.002)
        t.record_signal(correlation_id="dec-2", token_id="t2", strategy="s")
        time.sleep(0.002)
        t.record_signal(correlation_id="dec-3", token_id="t3", strategy="s")
        recent = t.get_recent(10)
        # Newest first.
        assert [r["correlation_id"] for r in recent] == ["dec-3", "dec-2", "dec-1"]

    def test_get_recent_clamps_limit(self):
        """``limit`` is clamped to [1, 500]."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="t", strategy="s")
        # limit=0 → clamped to 1.
        assert len(t.get_recent(0)) == 1
        # limit=-5 → clamped to 1.
        assert len(t.get_recent(-5)) == 1
        # limit=10000 → clamped to 500 (only 1 record exists, so 1 returned).
        assert len(t.get_recent(10000)) == 1

    def test_get_recent_returns_dicts(self):
        """Each record is a JSON-friendly dict with the expected keys."""
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="t1", strategy="s")
        rec = t.get_recent(1)[0]
        expected_keys = {
            "correlation_id",
            "token_id",
            "strategy",
            "signal_time",
            "order_time",
            "fill_time",
            "signal_to_order_ms",
            "order_to_fill_ms",
            "signal_to_fill_ms",
            "complete",
        }
        assert set(rec.keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════════════════
# 6. LatencyTracker — maxlen eviction + thread safety + reset + singleton
# ═══════════════════════════════════════════════════════════════════════════


class TestEvictionAndReset:
    """Maxlen eviction prunes the index; reset clears everything."""

    def test_maxlen_eviction_prunes_index(self):
        """When the deque's maxlen cap evicts the oldest record, its
        index entry must also be pruned so the index doesn't grow
        without bound."""
        t = LatencyTracker(max_records=3)
        for i in range(5):
            t.record_signal(correlation_id=f"dec-{i}", token_id="t", strategy="s")
        # Deque should hold the last 3 records (dec-2, dec-3, dec-4).
        recent = t.get_recent(10)
        assert {r["correlation_id"] for r in recent} == {"dec-2", "dec-3", "dec-4"}
        # Index should also hold exactly those 3 entries (no leaks).
        assert set(t._index.keys()) == {"dec-2", "dec-3", "dec-4"}
        # Calling record_order on an evicted correlation_id must NOT
        # resurrect the old record (it should create a fresh stub).
        t.record_order(correlation_id="dec-0")
        rec = next(r for r in t.get_recent(10) if r["correlation_id"] == "dec-0")
        assert rec["signal_time"] is None
        assert rec["order_time"] is not None

    def test_reset_clears_state(self):
        t = LatencyTracker()
        t.record_signal(correlation_id="dec-1", token_id="t", strategy="s")
        assert t.get_stats()["total_records"] == 1
        t.reset()
        assert t.get_stats()["total_records"] == 0
        assert t.get_recent(10) == []
        assert len(t._index) == 0

    def test_concurrent_record_calls_no_loss(self):
        """Spawn N threads, each recording M signals; the final count
        should be N*M (no lost records / no race on the deque append)."""
        t = LatencyTracker(max_records=10_000)
        n_threads = 8
        n_per_thread = 100
        barrier = threading.Barrier(n_threads + 1)

        def worker():
            barrier.wait()
            for i in range(n_per_thread):
                t.record_signal(
                    correlation_id=f"dec-{threading.get_ident()}-{i}",
                    token_id="t",
                    strategy="s",
                )

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for th in threads:
            th.start()
        barrier.wait()
        for th in threads:
            th.join()
        expected = n_threads * n_per_thread
        assert t.get_stats()["total_records"] == expected


class TestSingleton:
    """The module-level singleton is the instance imported by every call
    site (strategies/signal_trader.py, strategies/base.py,
    paper/simulator.py, core/live_fill_monitor.py, api/server.py)."""

    def test_singleton_is_latency_tracker_instance(self):
        assert isinstance(latency_tracker, LatencyTracker)

    def test_singleton_reset_returns_empty_stats(self):
        latency_tracker.reset()
        stats = latency_tracker.get_stats()
        assert stats["total_records"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. Wire-up: signal generation records signal_time via _ml_signal
# ═══════════════════════════════════════════════════════════════════════════


_TOKEN_ID = "0xlatency_test_token_id_deadbeefcafe"


def _book(
    bid_price: float = 0.59,
    bid_size: float = 10.0,
    ask_price: float = 0.61,
    ask_size: float = 10.0,
) -> OrderBook:
    """Build a minimal two-sided OrderBook for ``_ml_signal`` tests.

    Default spread (0.61 − 0.59 = 0.02) is comfortably below the 0.04
    regime-filter threshold so the spread gate never trips.
    """
    return OrderBook(
        token_id=_TOKEN_ID,
        bids=[PriceLevel(price=bid_price, size=bid_size)],
        asks=[PriceLevel(price=ask_price, size=ask_size)],
    )


def _features() -> np.ndarray:
    """Dummy 38-dim feature vector — content is irrelevant because
    ``ml_model.predict`` is mocked."""
    return np.zeros(38, dtype=np.float32)


@pytest.fixture
def mock_ml_model(monkeypatch):
    """Replace the module-level ``ml_model`` reference in
    ``strategies.signal_trader`` with a ``MagicMock`` so no real sklearn
    inference runs (and no 18-second cold-start training happens on
    first call)."""
    mock = MagicMock()
    mock.predict.return_value = (0.5, 0.5)
    monkeypatch.setattr("strategies.signal_trader.ml_model", mock)
    return mock


@pytest.fixture
def mock_allocate_capital(monkeypatch):
    """Stub ``allocate_capital`` to always return $2.50 so the V2
    allocator's safety gates never short-circuit a passing
    ``_ml_signal`` to ``None``."""
    monkeypatch.setattr(
        "strategies.signal_trader.allocate_capital",
        lambda **kwargs: 2.5,
    )
    return None


@pytest.fixture
def signal_trader_strategy(monkeypatch):
    """Fresh ``SignalTraderStrategy`` per test with the decision-ledger
    emit plumbing neutralised so no async scheduling happens.

    Mirrors the ``strategy`` fixture in ``tests/test_signal_trader.py`` —
    the strategy's ``_ml_signal`` path calls into ``core.decision_ledger``
    to mint a ``decision_id`` and fire-and-forgets ``PREDICTION`` /
    ``SIGNAL`` stage records via ``_emit_ledger``. Under pytest-asyncio
    the running event loop would otherwise schedule those
    never-awaited coroutines, producing ``RuntimeWarning: coroutine
    'DecisionLedger.record' was never awaited`` noise.
    """
    from strategies.signal_trader import SignalTraderStrategy

    s = SignalTraderStrategy()
    s._min_confidence = 0.65

    # Neutralise the fire-and-forget emit plumbing at the instance level.
    monkeypatch.setattr(s, "_emit_ledger", MagicMock(return_value=None))
    monkeypatch.setattr(s, "_emit_rejection", MagicMock(return_value=None))

    # Belt-and-braces: the PREDICTION/SIGNAL stage emits in ``_ml_signal``
    # itself do call ``decision_ledger.record`` to build their coro.
    # Patch the singleton's ``record`` / ``record_rejection`` to return
    # ``None`` (not a coroutine) so the MagicMock ``_emit_ledger`` doesn't
    # receive a stray coroutine object.
    from core.decision_ledger import decision_ledger as real_ledger
    monkeypatch.setattr(real_ledger, "record", MagicMock(return_value=None))
    monkeypatch.setattr(
        real_ledger, "record_rejection", MagicMock(return_value=None)
    )
    return s


@pytest.mark.asyncio
async def test_signal_generation_records_signal_time(
    signal_trader_strategy,
    mock_ml_model,
    mock_allocate_capital,
):
    """WIRE-UP: ``_ml_signal`` calls ``latency_tracker.record_signal`` with
    the minted decision_id, the token_id, and the strategy name — so the
    signal timestamp is anchored for downstream latency math.

    Pins the call-site added in ``strategies/signal_trader.py::_ml_signal``
    (after the SIGNAL decision-ledger stage is recorded).
    """
    # p_yes=0.75 (BUY zone), confidence=0.85 (above floor).
    mock_ml_model.predict.return_value = (0.75, 0.85)
    book = _book()
    mkt = {"slug": "test-market"}

    sig = signal_trader_strategy._ml_signal(_TOKEN_ID, "test-market", mkt, book, _features())

    # The signal must have been generated (sanity).
    assert sig is not None
    assert sig.decision_id  # non-empty
    # The latency tracker must have a record for that decision_id with
    # signal_time set, order_time/fill_time still None.
    recent = latency_tracker.get_recent(50)
    matching = [r for r in recent if r["correlation_id"] == sig.decision_id]
    assert len(matching) == 1, (
        f"latency_tracker must have exactly one record for decision_id "
        f"{sig.decision_id!r}; got {matching}"
    )
    rec = matching[0]
    assert rec["signal_time"] is not None
    assert rec["order_time"] is None
    assert rec["fill_time"] is None
    assert rec["token_id"] == _TOKEN_ID
    assert rec["strategy"] == signal_trader_strategy.name


# ═══════════════════════════════════════════════════════════════════════════
# 8. Wire-up: order submission records order_time via submit_order
# ═══════════════════════════════════════════════════════════════════════════


class _StubStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for submit_order tests.

    Mirrors the stub in ``tests/test_strategy_base.py`` — ``_run`` blocks
    on an ``asyncio.Event`` so the task stays alive until ``stop()``
    cancels it (we don't actually start the strategy here, but the
    subclass must implement ``_run`` to satisfy the ABC).
    """

    name: str = "stub"

    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    async def _run(self) -> None:
        await self._gate.wait()


def _order_args(
    *,
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 2.0,
    token_id: str = _TOKEN_ID,
) -> OrderArgs:
    return OrderArgs(token_id=token_id, price=price, side=side, size=size)


@pytest.mark.asyncio
async def test_order_submission_records_order_time(monkeypatch):
    """WIRE-UP: ``BaseStrategy.submit_order`` calls
    ``latency_tracker.record_order`` after risk approval and before the
    paper / live submit call — so the order timestamp is anchored for
    downstream latency math.

    Pins the call-site added in ``strategies/base.py::submit_order``
    (after the OSM VALIDATED transition, before the paper/live branch).
    """
    strat = _StubStrategy()
    assert strat._paper is True  # paper-mode branch is the path under test

    sentinel_order = Order(
        order_id="paper-sentinel",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-submit-1",
    )
    mock_risk = SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))
    mock_paper = SimpleNamespace(create_order=AsyncMock(return_value=sentinel_order))
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-submit-1")

    # (a) Order was submitted (sanity).
    assert result is sentinel_order
    # (b) The latency tracker must have a record for "dec-submit-1" with
    # order_time set, signal_time still None (no record_signal call was
    # made — the test exercised submit_order in isolation).
    recent = latency_tracker.get_recent(50)
    matching = [r for r in recent if r["correlation_id"] == "dec-submit-1"]
    assert len(matching) == 1, (
        f"latency_tracker must have exactly one record for dec-submit-1; "
        f"got {matching}"
    )
    rec = matching[0]
    assert rec["order_time"] is not None
    assert rec["signal_time"] is None
    # signal_to_order_ms must be None (no signal anchor).
    assert rec["signal_to_order_ms"] is None


@pytest.mark.asyncio
async def test_order_submission_after_signal_computes_signal_to_order(monkeypatch):
    """WIRE-UP (combined): when record_signal fires (in _ml_signal) and
    then record_order fires (in submit_order) for the same correlation_id,
    the tracker computes signal_to_order_ms automatically.

    This pins the end-to-end signal→order latency measurement contract:
    the two call sites thread the same ``decision_id`` / ``correlation_id``
    through so the tracker can join them."""
    # Pre-record the signal (simulating what _ml_signal would have done).
    latency_tracker.record_signal(
        correlation_id="dec-combined-1",
        token_id=_TOKEN_ID,
        strategy="stub",
    )
    time.sleep(0.005)  # 5 ms so the latency is measurable.

    # Now run submit_order with the same decision_id.
    strat = _StubStrategy()
    sentinel_order = Order(
        order_id="paper-sentinel",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=2.0,
        strategy=strat.name,
        paper=True,
        decision_id="dec-combined-1",
    )
    mock_risk = SimpleNamespace(check_order=AsyncMock(return_value=(True, "OK")))
    mock_paper = SimpleNamespace(create_order=AsyncMock(return_value=sentinel_order))
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-combined-1")
    assert result is sentinel_order

    rec = latency_tracker.get_recent(1)[0]
    assert rec["correlation_id"] == "dec-combined-1"
    assert rec["signal_time"] is not None
    assert rec["order_time"] is not None
    assert rec["signal_to_order_ms"] is not None
    assert 5.0 <= rec["signal_to_order_ms"] <= 1000.0


@pytest.mark.asyncio
async def test_order_rejected_by_risk_does_not_record_order(monkeypatch):
    """WIRE-UP (negative): when risk rejects the order, the latency tracker
    must NOT have an order_time recorded — the call site is placed AFTER
    risk approval, so a rejected order never reaches record_order."""
    strat = _StubStrategy()
    mock_risk = SimpleNamespace(
        check_order=AsyncMock(return_value=(False, "Mock rejection")),
    )
    mock_paper = SimpleNamespace(create_order=AsyncMock())
    monkeypatch.setattr(base_module, "risk_manager", mock_risk)
    monkeypatch.setattr(base_module, "paper_sim", mock_paper)

    args = _order_args()
    result = await strat.submit_order(args, decision_id="dec-rej-1")
    assert result is None

    # No record_order call → no record for "dec-rej-1" in the tracker.
    recent = latency_tracker.get_recent(50)
    matching = [r for r in recent if r["correlation_id"] == "dec-rej-1"]
    assert matching == [], (
        "latency_tracker must NOT have a record for a risk-rejected order"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 9. Wire-up: fill records fill_time via paper_sim._execute_fill
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_paper_fill_records_fill_time(monkeypatch):
    """WIRE-UP: ``PaperSimulator._execute_fill`` calls
    ``latency_tracker.record_fill`` when the order has a decision_id —
    so the fill timestamp is anchored for downstream latency math.

    Pins the call-site added in ``paper/simulator.py::_execute_fill``
    (inside the ``if order.decision_id:`` block, after the
    decision-ledger FILL stage).
    """
    from paper.simulator import paper_sim

    # Build an Order with a decision_id (so the ``if order.decision_id:``
    # block fires) and a non-trivial size so the fill is non-dust.
    order = Order(
        order_id="paper-fill-test-1",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=5.0,
        strategy="stub",
        paper=True,
        decision_id="dec-fill-1",
    )
    # Pre-populate the latency tracker with signal + order for the same
    # decision_id so the fill triggers full latency computation.
    latency_tracker.record_signal(
        correlation_id="dec-fill-1",
        token_id=_TOKEN_ID,
        strategy="stub",
    )
    time.sleep(0.005)
    latency_tracker.record_order(correlation_id="dec-fill-1")
    time.sleep(0.005)

    # Neutralise the closed_positions / ml_value_tracker / risk_manager /
    # execution_quality fire-and-forget calls — they're additive and
    # wrapped in try/except, but mocking them keeps the test focused on
    # the latency_tracker wiring.
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.record_closed_position",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "ml.economic_value.ml_value_tracker.record_trade",
        MagicMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "risk.manager.risk_manager.report_trade_pnl",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "core.execution_quality.record_execution",
        MagicMock(return_value=None),
        raising=False,
    )

    # Pre-populate store.open_orders so update_order can find the order.
    await store.add_order(order)

    fill_price = 0.51
    await paper_sim._execute_fill(order, fill_price)

    # The latency tracker must now have a complete record for "dec-fill-1".
    recent = latency_tracker.get_recent(50)
    matching = [r for r in recent if r["correlation_id"] == "dec-fill-1"]
    assert len(matching) == 1, (
        f"latency_tracker must have exactly one record for dec-fill-1; "
        f"got {matching}"
    )
    rec = matching[0]
    assert rec["complete"] is True
    assert rec["fill_time"] is not None
    assert rec["signal_to_order_ms"] is not None
    assert rec["order_to_fill_ms"] is not None
    assert rec["signal_to_fill_ms"] is not None
    # All three segments are positive (we slept 5 ms between each stage).
    assert rec["signal_to_order_ms"] > 0
    assert rec["order_to_fill_ms"] > 0
    assert rec["signal_to_fill_ms"] > 0


@pytest.mark.asyncio
async def test_paper_fill_without_decision_id_does_not_record(monkeypatch):
    """WIRE-UP (negative): an order without a decision_id (legacy / manual
    order) does NOT trigger record_fill — the call site is inside
    ``if order.decision_id:``, so the tracker only records fills that
    also have a decision-ledger chain."""
    from paper.simulator import paper_sim

    order = Order(
        order_id="paper-fill-no-decid",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.50,
        size=5.0,
        strategy="stub",
        paper=True,
        decision_id="",  # ← no decision_id
    )
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.record_closed_position",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "ml.economic_value.ml_value_tracker.record_trade",
        MagicMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "risk.manager.risk_manager.report_trade_pnl",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        "core.execution_quality.record_execution",
        MagicMock(return_value=None),
        raising=False,
    )
    await store.add_order(order)

    await paper_sim._execute_fill(order, 0.51)

    # No record_fill call → no records at all in the tracker.
    assert latency_tracker.get_recent(50) == []


# ═══════════════════════════════════════════════════════════════════════════
# 10. API routes — TestClient against api.server.app
# ═══════════════════════════════════════════════════════════════════════════


# Import once at module load so the ``client`` fixture can use it without
# re-importing per-test (the import is heavy — full lifespan of the
# polymarket-bot app).
try:
    from api.server import app as _app
except ImportError:  # pragma: no cover — defensive: api.server may be heavy
    _app = None  # type: ignore[assignment]

# Defensive: disable the rate-limit middleware so a fast test sequence
# against a per-minute-limited route doesn't 429 mid-suite (mirrors the
# pattern in ``tests/test_profiling.py``).
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, but we
# override it here to the value this module set in ``_ENV_REDIRECTS`` so
# the bearer token below matches what the ``enforce_api_auth`` middleware
# accepts.
VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-latency-wiring")


@pytest.fixture
def client() -> "TestClient":
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests. Mirrors the pattern
    in ``tests/test_profiling.py``.
    """
    from fastapi.testclient import TestClient
    return TestClient(_app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.mark.skipif(_app is None, reason="api.server.app not importable")
class TestLatencyAPIRoutes:
    """The two latency routes wired into ``api.server.app``."""

    def test_stats_returns_200_with_expected_shape(self, client, auth_headers):
        """``GET /api/latency/stats`` returns 200 with the full stats
        shape (window_hours / total_records / complete_records /
        in_flight_records / orphaned_records / signal_only_records /
        latencies_ms / by_strategy)."""
        # Pre-populate the singleton so the response has at least one row.
        latency_tracker.record_signal(
            correlation_id="dec-api-1",
            token_id="tok-api",
            strategy="signal_trader",
        )
        latency_tracker.record_order(correlation_id="dec-api-1")
        latency_tracker.record_fill(correlation_id="dec-api-1")

        response = client.get("/api/latency/stats", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/latency/stats must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        # Every key the dashboard renders must be present.
        expected_keys = {
            "window_hours",
            "total_records",
            "complete_records",
            "in_flight_records",
            "orphaned_records",
            "signal_only_records",
            "latencies_ms",
            "by_strategy",
        }
        assert set(data.keys()) >= expected_keys
        # The pre-populated record must show up.
        assert data["total_records"] >= 1
        assert data["complete_records"] >= 1
        # latencies_ms has the three segments.
        assert set(data["latencies_ms"].keys()) == {
            "signal_to_order",
            "order_to_fill",
            "signal_to_fill",
        }
        # by_strategy has the strategy we recorded against.
        assert "signal_trader" in data["by_strategy"]

    def test_stats_supports_hours_query_param(self, client, auth_headers):
        """``?hours=1`` is honoured (no 422 / 500) and reflected in the
        response's ``window_hours`` field."""
        latency_tracker.record_signal(
            correlation_id="dec-hours-1",
            token_id="t",
            strategy="s",
        )
        response = client.get(
            "/api/latency/stats?hours=1", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["window_hours"] == 1.0

    def test_stats_hours_validation(self, client, auth_headers):
        """``hours`` outside [0, 720] returns 422 (FastAPI's ``Query(ge=0,
        le=720)`` rejects)."""
        response = client.get(
            "/api/latency/stats?hours=-1", headers=auth_headers
        )
        assert response.status_code == 422
        response = client.get(
            "/api/latency/stats?hours=1000", headers=auth_headers
        )
        assert response.status_code == 422

    def test_stats_requires_auth(self, client):
        """``GET /api/latency/stats`` without a bearer token returns
        401 (``enforce_api_auth`` middleware rejects)."""
        response = client.get("/api/latency/stats")
        assert response.status_code == 401

    def test_recent_returns_200_with_list(self, client, auth_headers):
        """``GET /api/latency/recent`` returns 200 with a list of dicts
        (newest first), length ≤ ``limit``."""
        for i in range(5):
            latency_tracker.record_signal(
                correlation_id=f"dec-recent-{i}",
                token_id=f"t-{i}",
                strategy="s",
            )
            time.sleep(0.001)
        response = client.get(
            "/api/latency/recent?limit=3", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) <= 3
        # Newest first → the last-recorded correlation_id is first.
        assert data[0]["correlation_id"] == "dec-recent-4"

    def test_recent_limit_validation(self, client, auth_headers):
        """``limit`` outside [1, 500] returns 422."""
        response = client.get(
            "/api/latency/recent?limit=0", headers=auth_headers
        )
        assert response.status_code == 422
        response = client.get(
            "/api/latency/recent?limit=1000", headers=auth_headers
        )
        assert response.status_code == 422

    def test_recent_requires_auth(self, client):
        """``GET /api/latency/recent`` without a bearer token returns
        401."""
        response = client.get("/api/latency/recent")
        assert response.status_code == 401

    def test_recent_returns_empty_list_when_no_data(self, client, auth_headers):
        """``GET /api/latency/recent`` returns ``[]`` (not 404 / 500)
        when the tracker has no records yet."""
        response = client.get("/api/latency/recent", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []
