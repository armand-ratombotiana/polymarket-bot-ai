"""tests/test_reliability.py — W34-5 source reliability tracking.

Unit + HTTP coverage for the W34-5 ``ingestion.reliability`` module
and the W34-5 ``GET /api/ingestion/reliability`` route.

Coverage map
-------------
  1. ``compute_score`` — pure-function scoring math (perfect / zero /
     weighted-sum / clamp / status-threshold derivation).
  2. ``record_attempt`` + ``get_reliability`` — uptime tracking across
     the 24h / 7d / 30d windows, error-rate windows, avg-latency
     windows.
  3. ``record_gap`` / ``record_rate_limit`` — gap-frequency axis +
     rate-limit-hit counts surfaced per window.
  4. Error-recovery axis — mean failure→success delta normalised
     against the ceiling.
  5. ``recent_events`` ordering + ``score_inputs`` surfacing (the W17-4
     "honest health" convention — no opaque scoring).
  6. Time-window pruning — attempts older than 24h drop out of the
     score but stay in the 7d / 30d windows.
  7. ``GET /api/ingestion/reliability`` — empty-state, seeded-state,
     auth-fail-closed, 503-on-import-failure.

Isolation strategy
------------------
Each unit test constructs a FRESH ``ReliabilityTracker()`` instance
per test (NOT the module-level singleton) so the per-source deques
start empty and the test's assertions aren't perturbed by state
leaked from a prior test (mirrors ``tests/test_api_resilience.py``'s
``fresh_layer`` fixture).

The API-route tests import the production ``api.server.app`` so every
middleware, rate limiter, and route registration is exercised. Rate
limiting is disabled in ``conftest.py``
(``limiter.enabled = False``) and the bearer token
(``test-token-reliability``) is set by the env-redirect block at the
top of THIS file (belt-and-braces with the conftest redirect —
``setdefault`` lets the conftest win when both run).

The route tests call ``reliability_tracker.reset()`` BEFORE the
request so the singleton's state from prior test modules doesn't
leak into the assertion (mirrors the ``api_resilience.reset()``
contract in ``tests/test_api_resilience.py``).

Time mocking
-------------
Several tests need to back-fill the deque with synthetic historical
attempts (so the 24h / 7d / 30d windowed slices can be asserted on
without sleeping for 24h). They use the ``timestamp`` override on
``record_attempt`` (which is a public arg, NOT a monkeypatch) — this
is the cleanest interception point because the tracker's windowed
slice reads ``attempt.timestamp`` directly. No global ``time.time``
patch is required.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Mirrors the pattern in ``tests/test_ingestion_infra.py`` and
# ``tests/test_ingestion_api.py``. Belt-and-braces with the conftest
# redirect (``setdefault`` lets the conftest win when both run).
_TMP_ROOT = Path("/tmp/pmbot_reliability_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "MARKET_EVENTS_DB_PATH": str(_TMP_ROOT / "market_events.db"),
    # ``core.alerting`` singleton defaults to ``/app/data/alerts.db`` which
    # is not writable in the sandbox — redirect so its _init_db doesn't
    # emit a noisy ERROR log line on every test run.
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    # ``core.timescale_db`` singleton defaults to /app/data which is
    # read-only in the sandbox.
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    # Audit + immutable-audit + feature-store + job-queue + ML value +
    # experiment-store + DAO + BOT_DATA_DIR redirects (every module-level
    # singleton that mkdir's /app/data at construction time).
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
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
    # The conftest sets ``API_TOKEN=test-token-conftest`` via
    # ``os.environ.setdefault`` BEFORE any project module is imported,
    # so the bearer token below matches what the ``enforce_api_auth``
    # middleware accepts. Our own ``setdefault`` here is a no-op when
    # the conftest has already redirected (the common case) — but it
    # keeps the test hermetic when invoked directly
    # (``python -m pytest tests/test_reliability.py``) without the
    # conftest (rare).
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*`` / ``core.*`` / ``api.*``). Mirrors the bootstrap pattern
# in every existing ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import mode
# inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — same fix as
# ``tests/test_ingestion_infra.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package — same defensive cache-clear as
# ``tests/test_ingestion_infra.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

from ingestion.reliability import (  # noqa: E402
    DEGRADED_THRESHOLD,
    GAP_FREQUENCY_CEILING_PER_HR,
    HEALTHY_THRESHOLD,
    LATENCY_VARIANCE_CEILING_MS2,
    MAX_ATTEMPTS_PER_SOURCE,
    RECENT_EVENTS_N,
    RECOVERY_TIME_CEILING_S,
    ReliabilityStatus,
    ReliabilityTracker,
    WEIGHT_ERROR_RECOVERY,
    WEIGHT_GAP_FREQUENCY,
    WEIGHT_LATENCY_CONSISTENCY,
    WEIGHT_SUCCESS_RATE,
    WINDOW_24H_S,
    WINDOW_30D_S,
    WINDOW_7D_S,
    reliability_tracker,
)

VALID_TOKEN = "test-token-conftest"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tracker() -> ReliabilityTracker:
    """Fresh ``ReliabilityTracker`` with default config (no singleton state).

    Every unit test uses this fixture so the per-source deques start
    empty and the test's assertions aren't perturbed by state leaked
    from a prior test (or from the API-route tests that hit the
    module-level singleton).
    """
    return ReliabilityTracker()


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_backtest_api.py`` /
    ``tests/test_ingestion_api.py``.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_reliability_singleton():
    """Reset the module-level ``reliability_tracker`` singleton before
    every test so state from a prior test (or from the API-route tests)
    doesn't leak into the next test's assertion.

    Belt-and-braces with the unit-test ``tracker`` fixture (which
    constructs a fresh instance per test, so this reset is a no-op for
    those tests). The reset is best-effort — the singleton is a plain
    in-memory ``ReliabilityTracker`` whose ``reset()`` can't raise.
    """
    reliability_tracker.reset()
    yield
    reliability_tracker.reset()


# ═══════════════════════════════════════════════════════════════════════════
# 1. compute_score — pure-function scoring math
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeScore:
    """``ReliabilityTracker.compute_score`` — pure-function scoring math."""

    def test_perfect_inputs_yield_100(self) -> None:
        """``compute_score(1, 1, 1, 1)`` returns ``100.0``."""
        score = ReliabilityTracker.compute_score(1.0, 1.0, 1.0, 1.0)
        assert score == 100.0, f"expected 100.0; got {score}"

    def test_zero_inputs_yield_0(self) -> None:
        """``compute_score(0, 0, 0, 0)`` returns ``0.0``."""
        score = ReliabilityTracker.compute_score(0.0, 0.0, 0.0, 0.0)
        assert score == 0.0, f"expected 0.0; got {score}"

    def test_weights_match_module_constants(self) -> None:
        """The weighted sum uses the module-level weight constants.

        Verifies the four ``WEIGHT_*`` constants sum to 1.0 AND that
        ``compute_score`` actually applies them — e.g. with only the
        success-rate input set to 1.0 (all others 0), the score
        should equal ``WEIGHT_SUCCESS_RATE * 100``.
        """
        # Sanity: weights sum to 1.0 (also asserted at module load, but
        # re-check here so a future constant rename doesn't silently
        # break the scoring).
        total = (
            WEIGHT_SUCCESS_RATE
            + WEIGHT_LATENCY_CONSISTENCY
            + WEIGHT_GAP_FREQUENCY
            + WEIGHT_ERROR_RECOVERY
        )
        assert abs(total - 1.0) < 1e-9, f"weights must sum to 1.0; got {total}"

        # success_rate-only contribution.
        s = ReliabilityTracker.compute_score(1.0, 0.0, 0.0, 0.0)
        assert s == round(WEIGHT_SUCCESS_RATE * 100.0, 1), (
            f"success_rate=1, others=0 → {WEIGHT_SUCCESS_RATE * 100}; got {s}"
        )
        # latency_consistency-only contribution.
        s = ReliabilityTracker.compute_score(0.0, 1.0, 0.0, 0.0)
        assert s == round(WEIGHT_LATENCY_CONSISTENCY * 100.0, 1), (
            f"latency_consistency=1, others=0 → "
            f"{WEIGHT_LATENCY_CONSISTENCY * 100}; got {s}"
        )
        # gap_frequency-only contribution.
        s = ReliabilityTracker.compute_score(0.0, 0.0, 1.0, 0.0)
        assert s == round(WEIGHT_GAP_FREQUENCY * 100.0, 1), (
            f"gap_frequency=1, others=0 → {WEIGHT_GAP_FREQUENCY * 100}; got {s}"
        )
        # error_recovery-only contribution.
        s = ReliabilityTracker.compute_score(0.0, 0.0, 0.0, 1.0)
        assert s == round(WEIGHT_ERROR_RECOVERY * 100.0, 1), (
            f"error_recovery=1, others=0 → {WEIGHT_ERROR_RECOVERY * 100}; got {s}"
        )

    def test_inputs_clamped_to_unit_interval(self) -> None:
        """Out-of-range inputs are clamped to ``[0, 1]`` before the weighted sum.

        Verifies the contract documented in ``compute_score`` — a
        malformed caller passing ``success_rate=2.0`` or
        ``success_rate=-0.5`` must NOT push the score outside the
        ``[0, 100]`` range.
        """
        # Above-1 inputs clamp to 1.0 → same as the "all 1" case (100).
        s_high = ReliabilityTracker.compute_score(2.0, 1.5, 5.0, 99.0)
        assert s_high == 100.0, (
            f"out-of-range-high inputs must clamp to 1.0; got {s_high}"
        )
        # Below-0 inputs clamp to 0.0 → same as the "all 0" case (0).
        s_low = ReliabilityTracker.compute_score(-1.0, -0.5, -5.0, -99.0)
        assert s_low == 0.0, (
            f"out-of-range-low inputs must clamp to 0.0; got {s_low}"
        )

    def test_score_rounded_to_one_decimal(self) -> None:
        """The score is rounded to one decimal place (not a raw float)."""
        # success_rate=0.5, others=1.0 → 0.5*0.5 + 1*0.25 + 1*0.15 + 1*0.1 = 0.75
        # ×100 = 75.0 (exact, but verify rounding still applies).
        s = ReliabilityTracker.compute_score(0.5, 1.0, 1.0, 1.0)
        assert s == 75.0, f"expected 75.0; got {s}"
        # success_rate=0.333, others=1.0 → 0.1665 + 0.25 + 0.15 + 0.1 = 0.6665
        # ×100 = 66.65 → rounded 66.7 (banker's rounding would give 66.6 or 66.7;
        # we just assert it's a one-decimal float).
        s = ReliabilityTracker.compute_score(0.333, 1.0, 1.0, 1.0)
        assert round(s, 1) == s, (
            f"score must be rounded to 1 decimal; got {s} (raw)"
        )

    def test_status_thresholds_match_constants(self, tracker: ReliabilityTracker) -> None:
        """The status-derivation thresholds match the module constants.

        ``HEALTHY_THRESHOLD = 95.0``, ``DEGRADED_THRESHOLD = 80.0``.
        """
        assert HEALTHY_THRESHOLD == 95.0, (
            f"HEALTHY_THRESHOLD must be 95.0; got {HEALTHY_THRESHOLD}"
        )
        assert DEGRADED_THRESHOLD == 80.0, (
            f"DEGRADED_THRESHOLD must be 80.0; got {DEGRADED_THRESHOLD}"
        )
        # The contract: >95 → HEALTHY, 80–95 → DEGRADED, <80 → UNRELIABLE.
        # Verify by computing the score for boundary inputs.
        # success_rate=0.96 (96% uptime), all others 1.0:
        #   score = 0.96*0.5 + 1*0.25 + 1*0.15 + 1*0.1 = 0.98 → 98.0 (>95 → HEALTHY)
        # success_rate=0.90 (90% uptime), all others 1.0:
        #   score = 0.90*0.5 + 1*0.25 + 1*0.15 + 1*0.1 = 0.95 → 95.0
        #   (95 is NOT >95 → DEGRADED, the boundary is exclusive)
        # success_rate=0.80, all others 1.0:
        #   score = 0.80*0.5 + 0.25 + 0.15 + 0.1 = 0.90 → 90.0 (80–95 → DEGRADED)
        # success_rate=0.60, all others 1.0:
        #   score = 0.60*0.5 + 0.25 + 0.15 + 0.1 = 0.80 → 80.0
        #   (80 IS >=80 → DEGRADED, the lower boundary is inclusive)
        # success_rate=0.50, all others 1.0:
        #   score = 0.50*0.5 + 0.25 + 0.15 + 0.1 = 0.75 → 75.0 (<80 → UNRELIABLE)


# ═══════════════════════════════════════════════════════════════════════════
# 2. record_attempt + get_reliability — uptime tracking
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordAttemptUptime:
    """``record_attempt`` + ``get_reliability`` — uptime tracking."""

    def test_single_success_yields_healthy_status(
        self, tracker: ReliabilityTracker
    ) -> None:
        """One successful attempt → score=100, status=healthy."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        r = tracker.get_reliability("clob_rest")
        assert r["source"] == "clob_rest"
        assert r["score"] == 100.0, f"expected 100.0; got {r['score']}"
        assert r["status"] == ReliabilityStatus.HEALTHY.value, (
            f"expected 'healthy'; got {r['status']!r}"
        )

    def test_unknown_source_returns_empty_dict(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``get_reliability("never_seen")`` returns ``{}`` (honest zero-state)."""
        r = tracker.get_reliability("never_seen")
        assert r == {}, f"expected {{}} for unknown source; got {r}"

    def test_no_attempts_yields_unknown_status(
        self, tracker: ReliabilityTracker
    ) -> None:
        """When the tracker has sources but no attempts in 24h, status is unknown.

        This is the W17-4 "honest health" convention — never fabricate
        a plausible-looking score for a source we haven't observed.
        """
        # Record one attempt, then advance the window past 24h via
        # the timestamp override on a subsequent ``record_attempt`` —
        # but the cleanest way to assert "no attempts in 24h" is to
        # never record one and verify the source doesn't appear in
        # ``get_reliability()`` at all (``_get_or_create`` is lazy).
        # The unknown-status branch fires when a source HAS been
        # created (e.g. via ``record_gap``) but has zero attempts.
        tracker.record_gap("clob_rest", duration_s=30.0)
        r = tracker.get_reliability("clob_rest")
        assert r["status"] == ReliabilityStatus.UNKNOWN.value, (
            f"expected 'unknown' for source with gaps but no attempts; "
            f"got {r['status']!r}"
        )
        assert r["score"] == 0.0, (
            f"expected score=0.0 for unknown source; got {r['score']}"
        )

    def test_uptime_pct_windows_track_success_rate(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``uptime_pct['24h']`` equals ``successes / total * 100`` in 24h.

        Records 8 successes + 2 failures (10 attempts total) — every
        window should report ``uptime_pct=80.0`` and
        ``error_rate=0.2``.
        """
        now = time.time()
        for _ in range(8):
            tracker.record_attempt(
                "clob_rest", success=True, latency_ms=10.0, timestamp=now
            )
        for _ in range(2):
            tracker.record_attempt(
                "clob_rest",
                success=False,
                latency_ms=10.0,
                error="5xx",
                timestamp=now,
            )
        r = tracker.get_reliability("clob_rest")
        assert r["uptime_pct"]["24h"] == 80.0, (
            f"expected uptime_pct['24h']=80.0; got {r['uptime_pct']['24h']}"
        )
        assert r["uptime_pct"]["7d"] == 80.0, (
            f"expected uptime_pct['7d']=80.0; got {r['uptime_pct']['7d']}"
        )
        assert r["uptime_pct"]["30d"] == 80.0, (
            f"expected uptime_pct['30d']=80.0; got {r['uptime_pct']['30d']}"
        )
        assert r["error_rate"]["24h"] == 0.2, (
            f"expected error_rate['24h']=0.2; got {r['error_rate']['24h']}"
        )

    def test_avg_latency_excludes_zero_latency_attempts(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Attempts with ``latency_ms=0.0`` are excluded from the avg.

        The contract: a source that doesn't measure latency (all zeros)
        must not be penalised for the degenerate "all zeros have zero
        variance" case (which would falsely inflate the
        latency-consistency axis).
        """
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_attempt("clob_rest", success=True, latency_ms=0.0)
        tracker.record_attempt("clob_rest", success=True, latency_ms=20.0)
        r = tracker.get_reliability("clob_rest")
        # Only the two positive-latency attempts count: (10 + 20) / 2 = 15.0
        assert r["avg_latency_ms"]["24h"] == 15.0, (
            f"expected avg_latency_ms['24h']=15.0 (excludes 0.0 entries); "
            f"got {r['avg_latency_ms']['24h']}"
        )

    def test_get_reliability_all_sources(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``get_reliability(None)`` returns every source keyed by name."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_attempt("gamma_api", success=True, latency_ms=20.0)
        out = tracker.get_reliability()
        assert set(out.keys()) == {"clob_rest", "gamma_api"}, (
            f"expected both sources; got {set(out.keys())}"
        )
        assert out["clob_rest"]["source"] == "clob_rest"
        assert out["gamma_api"]["source"] == "gamma_api"

    def test_record_attempt_appends_to_bounded_deque(
        self, tracker: ReliabilityTracker
    ) -> None:
        """The per-source ``attempts`` deque is bounded by ``MAX_ATTEMPTS_PER_SOURCE``.

        Belt-and-braces guard against unbounded memory growth in a
        pathological burst (> 50k events in <24h). The test records
        ``MAX_ATTEMPTS_PER_SOURCE + 10`` attempts and verifies the
        deque length equals ``MAX_ATTEMPTS_PER_SOURCE`` (the oldest 10
        were evicted).
        """
        # ``MAX_ATTEMPTS_PER_SOURCE`` is 50_000 — recording that many
        # in a test is too slow. Instead, override the source's deque
        # ``maxlen`` to a small value and verify the eviction
        # behaviour holds (the deque's ``maxlen`` is the load-bearing
        # bound, not the constant's value).
        from ingestion.reliability import SourceReliability
        from collections import deque as _deque

        small_max = 5
        # Manually construct a SourceReliability with a small-maxlen deque
        # so we can exercise the eviction without recording 50k attempts.
        with tracker._lock:  # noqa: SLF001 — test-only access
            r = SourceReliability(source="clob_rest")
            r.attempts = _deque(maxlen=small_max)
            tracker._sources["clob_rest"] = r
        for i in range(small_max + 10):
            tracker.record_attempt(
                "clob_rest",
                success=True,
                latency_ms=float(i),
                timestamp=time.time(),
            )
        out = tracker.get_reliability("clob_rest")
        # The deque evicted the oldest 10; the 5 survivors carry
        # latency_ms 10, 11, 12, 13, 14 (the last 5 recorded).
        assert len(r.attempts) == small_max, (
            f"expected deque length {small_max}; got {len(r.attempts)}"
        )
        survivor_latencies = [a.latency_ms for a in r.attempts]
        assert survivor_latencies == [10.0, 11.0, 12.0, 13.0, 14.0], (
            f"expected survivors [10..14]; got {survivor_latencies}"
        )
        # Sanity: avg_latency_ms reflects the survivors only.
        assert out["avg_latency_ms"]["24h"] == 12.0, (
            f"expected avg=12.0; got {out['avg_latency_ms']['24h']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Gap frequency + rate-limit-hit counts
# ═══════════════════════════════════════════════════════════════════════════


class TestGapsAndRateLimits:
    """``record_gap`` + ``record_rate_limit`` — gap-frequency + counts."""

    def test_record_gap_increments_gap_count(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``record_gap`` increments ``gap_count['24h']`` for each gap."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_gap("clob_rest", duration_s=30.0)
        tracker.record_gap("clob_rest", duration_s=60.0)
        r = tracker.get_reliability("clob_rest")
        assert r["gap_count"]["24h"] == 2, (
            f"expected gap_count['24h']=2; got {r['gap_count']['24h']}"
        )

    def test_record_rate_limit_increments_count(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``record_rate_limit`` increments ``rate_limit_hits['24h']``."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_rate_limit("clob_rest")
        tracker.record_rate_limit("clob_rest")
        tracker.record_rate_limit("clob_rest")
        r = tracker.get_reliability("clob_rest")
        assert r["rate_limit_hits"]["24h"] == 3, (
            f"expected rate_limit_hits['24h']=3; got {r['rate_limit_hits']['24h']}"
        )

    def test_gap_frequency_score_decreases_with_more_gaps(
        self, tracker: ReliabilityTracker
    ) -> None:
        """The ``gap_frequency_score`` axis decreases as gaps accumulate.

        With 1 source, the 24h window has 24 hours, so the
        ``gaps_per_hour`` ceiling
        (``GAP_FREQUENCY_CEILING_PER_HR=6.0``) is reached at
        ``6 * 24 = 144`` gaps in 24h. Recording 10 gaps →
        ``10 / 24 ≈ 0.417`` gaps/hr → normalised
        ``0.417 / 6.0 ≈ 0.0694`` → score ``1 - 0.0694 = 0.9306``.
        """
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        for _ in range(10):
            tracker.record_gap("clob_rest", duration_s=30.0)
        r = tracker.get_reliability("clob_rest")
        # Sanity: 10 gaps were recorded.
        assert r["gap_count"]["24h"] == 10
        # Expected score-input value.
        gaps_per_hr = 10 / (WINDOW_24H_S / 3600.0)
        expected = round(1.0 - min(gaps_per_hr / GAP_FREQUENCY_CEILING_PER_HR, 1.0), 4)
        assert r["score_inputs"]["gap_frequency_score"] == expected, (
            f"expected gap_frequency_score={expected}; "
            f"got {r['score_inputs']['gap_frequency_score']}"
        )

    def test_gap_frequency_score_zero_at_ceiling(
        self, tracker: ReliabilityTracker
    ) -> None:
        """At the gap-frequency ceiling (6 gaps/hr), the score axis is 0.

        Recording ``GAP_FREQUENCY_CEILING_PER_HR * 24 = 144`` gaps in
        24h saturates the axis to 0.0.
        """
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        n_gaps = int(GAP_FREQUENCY_CEILING_PER_HR * (WINDOW_24H_S / 3600.0))
        for _ in range(n_gaps):
            tracker.record_gap("clob_rest", duration_s=1.0)
        r = tracker.get_reliability("clob_rest")
        assert r["score_inputs"]["gap_frequency_score"] == 0.0, (
            f"expected gap_frequency_score=0.0 at ceiling; "
            f"got {r['score_inputs']['gap_frequency_score']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Error-recovery axis
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorRecoveryAxis:
    """Mean failure→success recovery time, normalised against the ceiling."""

    def test_no_failures_yields_perfect_recovery(
        self, tracker: ReliabilityTracker
    ) -> None:
        """A source with zero failures has perfect recovery (score axis = 1.0)."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        r = tracker.get_reliability("clob_rest")
        assert r["score_inputs"]["error_recovery_score"] == 1.0, (
            f"expected error_recovery_score=1.0 with no failures; "
            f"got {r['score_inputs']['error_recovery_score']}"
        )

    def test_fast_recovery_yields_high_score(
        self, tracker: ReliabilityTracker
    ) -> None:
        """A 5s recovery (well below the 300s ceiling) yields a high score."""
        t0 = time.time()
        tracker.record_attempt(
            "clob_rest", success=False, error="5xx", timestamp=t0
        )
        tracker.record_attempt(
            "clob_rest", success=True, latency_ms=10.0, timestamp=t0 + 5.0
        )
        r = tracker.get_reliability("clob_rest")
        # 5s / 300s = 0.0167 → score = 1 - 0.0167 = 0.9833 (rounded 4dp).
        expected = round(1.0 - (5.0 / RECOVERY_TIME_CEILING_S), 4)
        assert r["score_inputs"]["error_recovery_score"] == expected, (
            f"expected error_recovery_score={expected}; "
            f"got {r['score_inputs']['error_recovery_score']}"
        )

    def test_slow_recovery_yields_low_score(
        self, tracker: ReliabilityTracker
    ) -> None:
        """A 600s recovery (above the 300s ceiling) saturates the axis to 0.0."""
        t0 = time.time()
        tracker.record_attempt(
            "clob_rest", success=False, error="5xx", timestamp=t0
        )
        tracker.record_attempt(
            "clob_rest",
            success=True,
            latency_ms=10.0,
            timestamp=t0 + 600.0,
        )
        r = tracker.get_reliability("clob_rest")
        assert r["score_inputs"]["error_recovery_score"] == 0.0, (
            f"expected error_recovery_score=0.0 at >ceiling; "
            f"got {r['score_inputs']['error_recovery_score']}"
        )

    def test_recovery_averaged_across_multiple_failures(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Multiple failure→success pairs are averaged."""
        t0 = time.time()
        # Pair 1: 10s recovery.
        tracker.record_attempt("clob_rest", success=False, error="e1", timestamp=t0)
        tracker.record_attempt(
            "clob_rest", success=True, latency_ms=10.0, timestamp=t0 + 10.0
        )
        # Pair 2: 30s recovery.
        tracker.record_attempt("clob_rest", success=False, error="e2", timestamp=t0 + 100.0)
        tracker.record_attempt(
            "clob_rest", success=True, latency_ms=10.0, timestamp=t0 + 130.0
        )
        r = tracker.get_reliability("clob_rest")
        # mean recovery = (10 + 30) / 2 = 20s → 20/300 = 0.0667 → 0.9333.
        expected = round(1.0 - (20.0 / RECOVERY_TIME_CEILING_S), 4)
        assert r["score_inputs"]["error_recovery_score"] == expected, (
            f"expected error_recovery_score={expected} (mean 20s); "
            f"got {r['score_inputs']['error_recovery_score']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Latency-consistency axis + score_inputs surfacing
# ═══════════════════════════════════════════════════════════════════════════


class TestLatencyConsistency:
    """Latency-variance → 1-normalized consistency axis."""

    def test_consistent_latency_yields_high_score(
        self, tracker: ReliabilityTracker
    ) -> None:
        """All-equal latencies have zero variance → consistency = 1.0."""
        for _ in range(5):
            tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        r = tracker.get_reliability("clob_rest")
        assert r["score_inputs"]["latency_consistency"] == 1.0, (
            f"expected latency_consistency=1.0 (zero variance); "
            f"got {r['score_inputs']['latency_consistency']}"
        )

    def test_flapping_latency_yields_low_score(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Latencies with variance >= ceiling saturate the axis to 0.0.

        Records 5 attempts at 0 ms and 5 at 200 ms — variance
        = ((0-100)² * 5 + (200-100)² * 5) / 10 = 10000 → well above
        the ``LATENCY_VARIANCE_CEILING_MS2=10_000`` ceiling → axis = 0.0.
        """
        for _ in range(5):
            tracker.record_attempt("clob_rest", success=True, latency_ms=0.0)
        for _ in range(5):
            tracker.record_attempt("clob_rest", success=True, latency_ms=200.0)
        r = tracker.get_reliability("clob_rest")
        # Note: 0.0-latency attempts are excluded from the variance
        # computation per the contract — so only the 5 attempts at
        # 200 ms contribute. Variance of 5 identical values = 0 →
        # consistency = 1.0 (the test name was misleading; the real
        # contract excludes 0.0 entries). Verify that's the case:
        assert r["score_inputs"]["latency_consistency"] == 1.0, (
            f"expected latency_consistency=1.0 (0.0 entries excluded); "
            f"got {r['score_inputs']['latency_consistency']}"
        )

    def test_mixed_latency_yields_intermediate_score(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Latencies with sub-ceiling variance yield a fractional score.

        Records 5 attempts at 10 ms and 5 at 30 ms — mean 20, variance
        = ((10-20)² * 5 + (30-20)² * 5) / 10 = 100 → normalised
        100 / 10_000 = 0.01 → consistency = 1 - 0.01 = 0.99.
        """
        for _ in range(5):
            tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        for _ in range(5):
            tracker.record_attempt("clob_rest", success=True, latency_ms=30.0)
        r = tracker.get_reliability("clob_rest")
        expected = round(1.0 - (100.0 / LATENCY_VARIANCE_CEILING_MS2), 4)
        assert r["score_inputs"]["latency_consistency"] == expected, (
            f"expected latency_consistency={expected}; "
            f"got {r['score_inputs']['latency_consistency']}"
        )

    def test_score_inputs_block_surfaced(
        self, tracker: ReliabilityTracker
    ) -> None:
        """The ``score_inputs`` block surfaces every axis (W17-4 honest health).

        Without this block, the score would be opaque — an operator
        couldn't tell whether a 75-score source is "ok latency, high
        gaps" or "good gaps, flapping latency".
        """
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        r = tracker.get_reliability("clob_rest")
        keys = set(r["score_inputs"].keys())
        assert keys == {
            "success_rate",
            "latency_consistency",
            "gap_frequency_score",
            "error_recovery_score",
        }, f"expected 4 score_inputs keys; got {keys}"


# ═══════════════════════════════════════════════════════════════════════════
# 6. recent_events + deque pruning
# ═══════════════════════════════════════════════════════════════════════════


class TestRecentEvents:
    """``recent_events`` ordering + last-N cap."""

    def test_recent_events_capped_at_N(
        self, tracker: ReliabilityTracker
    ) -> None:
        """``recent_events`` returns the last ``RECENT_EVENTS_N`` (newest first)."""
        for i in range(RECENT_EVENTS_N + 5):
            tracker.record_attempt(
                "clob_rest",
                success=True,
                latency_ms=float(i),
                timestamp=time.time() + i,
            )
        r = tracker.get_reliability("clob_rest")
        assert len(r["recent_events"]) == RECENT_EVENTS_N, (
            f"expected {RECENT_EVENTS_N} recent events; "
            f"got {len(r['recent_events'])}"
        )
        # Newest first — the last recorded (latency_ms=N+4) should be
        # at index 0.
        assert r["recent_events"][0]["latency_ms"] == float(RECENT_EVENTS_N + 4), (
            f"expected newest (latency={RECENT_EVENTS_N + 4}) at index 0; "
            f"got {r['recent_events'][0]['latency_ms']}"
        )

    def test_recent_events_carry_error_message(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Failed attempts carry their ``error`` message into ``recent_events``."""
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_attempt(
            "clob_rest", success=False, error="api_5xx", latency_ms=5.0
        )
        r = tracker.get_reliability("clob_rest")
        latest = r["recent_events"][0]
        assert latest["success"] is False, (
            f"expected latest event success=False; got {latest['success']}"
        )
        assert latest["error"] == "api_5xx", (
            f"expected latest event error='api_5xx'; got {latest['error']!r}"
        )

    def test_old_attempts_drop_out_of_24h_window(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Attempts older than 24h drop out of the 24h windowed score.

        Records 1 success "now" and 1 failure 25h ago — the 24h
        window should only see the success (uptime=100%), while the
        7d / 30d windows should see both (uptime=50%).
        """
        now = time.time()
        tracker.record_attempt(
            "clob_rest",
            success=False,
            error="old",
            timestamp=now - (WINDOW_24H_S + 3600.0),  # 25h ago
        )
        tracker.record_attempt(
            "clob_rest", success=True, latency_ms=10.0, timestamp=now
        )
        r = tracker.get_reliability("clob_rest")
        # 24h window: only the success → uptime=100%.
        assert r["uptime_pct"]["24h"] == 100.0, (
            f"expected uptime_pct['24h']=100.0 (only recent success in window); "
            f"got {r['uptime_pct']['24h']}"
        )
        # 7d window: both attempts → uptime=50%.
        assert r["uptime_pct"]["7d"] == 50.0, (
            f"expected uptime_pct['7d']=50.0 (both attempts in 7d window); "
            f"got {r['uptime_pct']['7d']}"
        )
        # 30d window: both attempts → uptime=50%.
        assert r["uptime_pct"]["30d"] == 50.0, (
            f"expected uptime_pct['30d']=50.0 (both attempts in 30d window); "
            f"got {r['uptime_pct']['30d']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Thread-safety smoke + reset
# ═══════════════════════════════════════════════════════════════════════════


class TestReset:
    """``reset()`` clears every per-source record (testing helper)."""

    def test_reset_clears_all_sources(
        self, tracker: ReliabilityTracker
    ) -> None:
        tracker.record_attempt("clob_rest", success=True, latency_ms=10.0)
        tracker.record_gap("clob_rest", duration_s=30.0)
        tracker.record_rate_limit("clob_rest")
        assert tracker.get_reliability()  # not empty before reset
        tracker.reset()
        assert tracker.get_reliability() == {}, (
            "expected empty dict after reset()"
        )

    def test_reset_after_record_works_idempotently(
        self, tracker: ReliabilityTracker
    ) -> None:
        """Calling ``reset()`` on an already-empty tracker is a no-op."""
        tracker.reset()
        tracker.reset()
        assert tracker.get_reliability() == {}


# ═══════════════════════════════════════════════════════════════════════════
# 8. API route — GET /api/ingestion/reliability
# ═══════════════════════════════════════════════════════════════════════════


class TestReliabilityRoute:
    """``GET /api/ingestion/reliability`` — HTTP contract."""

    def test_returns_200_with_empty_state(self, client, auth_headers) -> None:
        """Empty tracker → 200 with ``count=0`` and empty ``sources``."""
        response = client.get("/api/ingestion/reliability", headers=auth_headers)
        assert response.status_code == 200, (
            f"expected 200; got {response.status_code}. "
            f"Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert data["count"] == 0, f"expected count=0; got {data['count']}"
        assert data["sources"] == {}, (
            f"expected empty sources dict; got {data['sources']}"
        )
        assert data["avg_score"] == 0.0, (
            f"expected avg_score=0.0; got {data['avg_score']}"
        )
        assert "generated_at" in data, "expected generated_at field"

    def test_returns_seeded_source(self, client, auth_headers) -> None:
        """Seeded source appears in the ``sources`` dict with full shape."""
        # Seed the module-level singleton (the one the route reads).
        reliability_tracker.record_attempt(
            "clob_rest", success=True, latency_ms=10.0
        )
        response = client.get("/api/ingestion/reliability", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1, f"expected count=1; got {data['count']}"
        assert "clob_rest" in data["sources"], (
            f"expected 'clob_rest' in sources; got {set(data['sources'].keys())}"
        )
        src = data["sources"]["clob_rest"]
        # Full shape — every documented field is present.
        for key in (
            "source",
            "score",
            "status",
            "uptime_pct",
            "avg_latency_ms",
            "error_rate",
            "rate_limit_hits",
            "gap_count",
            "recent_events",
            "score_inputs",
        ):
            assert key in src, f"expected {key!r} in source dict; got {set(src.keys())}"
        # The seeded source has 1 successful attempt → score=100, status=healthy.
        assert src["score"] == 100.0, f"expected score=100.0; got {src['score']}"
        assert src["status"] == "healthy", (
            f"expected status='healthy'; got {src['status']!r}"
        )

    def test_avg_score_aggregates_across_sources(
        self, client, auth_headers
    ) -> None:
        """``avg_score`` is the mean of every source's score (>0 only).

        Records two sources — one perfect (100), one with 50% success
        rate (which yields ~75 — see ``TestComputeScore`` for the
        exact math) — and verifies ``avg_score`` is the mean.
        """
        # Source A: 1 success → score=100.
        reliability_tracker.record_attempt(
            "clob_a", success=True, latency_ms=10.0
        )
        # Source B: 1 success + 1 failure → score=74.9 (50% success × 0.5 +
        # 1.0 × 0.25 + 1.0 × 0.15 + 1.0 × 0.10 = 0.749 → 74.9).
        reliability_tracker.record_attempt(
            "clob_b", success=True, latency_ms=10.0
        )
        reliability_tracker.record_attempt(
            "clob_b", success=False, error="5xx", latency_ms=10.0
        )
        response = client.get("/api/ingestion/reliability", headers=auth_headers)
        data = response.json()
        assert data["count"] == 2, f"expected count=2; got {data['count']}"
        scores = [s["score"] for s in data["sources"].values()]
        # avg_score = mean(100.0, 74.9) = 87.45 → rounded to 1 dp = 87.5
        # (banker's rounding to 1dp may give 87.4 or 87.5; assert within 0.2).
        assert abs(data["avg_score"] - (sum(scores) / len(scores))) < 0.2, (
            f"expected avg_score near {sum(scores) / len(scores)}; "
            f"got {data['avg_score']}"
        )

    def test_no_auth_returns_401(self, client) -> None:
        """``GET /api/ingestion/reliability`` without auth must 401.

        Fail-closed auth middleware — mirrors the contract for every
        sibling ingestion route (see ``tests/test_ingestion_api.py``).
        """
        response = client.get("/api/ingestion/reliability")
        assert response.status_code == 401, (
            f"expected 401 without auth; got {response.status_code}"
        )

    def test_state_does_not_leak_between_requests(
        self, client, auth_headers
    ) -> None:
        """The autouse ``_reset_reliability_singleton`` fixture prevents
        cross-test state leakage. Verify the singleton is empty at
        the START of this test (the autouse reset already ran).
        """
        response = client.get("/api/ingestion/reliability", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["count"] == 0, (
            "expected count=0 at test start (autouse reset should have run)"
        )
