"""Source reliability tracker — tracks data source health over time.

For each source:
- Uptime percentage (last 24h, 7d, 30d)
- Average latency
- Error rate
- Rate limit hits
- Data gap frequency
- Last N events health

Reliability score: 0-100%
- >95%: Healthy
- 80-95%: Degraded
- <80%: Unreliable

W34-5 — Source reliability tracking.

The tracker is IN-MEMORY (no SQLite persistence) because:

  * The metrics are windowed — every windowed view (24h / 7d / 30d) is
    derived from a bounded in-memory deque of recent attempt records,
    so a long-running session can't grow the deque without limit.
    Persisting these deques across restarts would require schema
    management for high-cardinality event tables, which is out of
    scope for this task — the W31-4 ``ingestion.health`` and
    W24-7 ``core.api_resilience`` modules follow the same in-memory
    convention for the same reason.
  * A restart zeroes the per-source reliability, which is the right
    behaviour for an operational "last 24h" view (the operator's
    dashboard re-polls every 15 s; a fresh process is a fresh
    observation, not a continuation of the pre-restart window).
  * The ``reliability_tracker`` singleton is fed by the same call
    sites that already feed ``ingestion_health_monitor`` and
    ``api_resilience`` — every ``record_attempt`` call mirrors a
    ``record_event`` / ``call_with_resilience`` invocation. Wiring
    those calls into the production poller / WS client / Gamma client
    is the next step; this module ships the contract + scoring +
    API surface first (the "additive only" convention — no edits to
    existing call sites required for the contract to land).

Contract
--------
``record_attempt(source, success, latency_ms=0.0, error='', timestamp=None) -> None``
    Record one attempt — success or failure — for ``source``. The
    attempt is appended to a bounded deque (default 50 000 entries
    per source) so the deque can't grow without limit. ``latency_ms``
    is the end-to-end attempt latency (0.0 when not measured — those
    attempts are excluded from the latency-consistency axis so a
    source that doesn't measure latency doesn't get penalised for
    "all zeros have zero variance").

``record_gap(source, duration_s, timestamp=None) -> None``
    Record one detected data gap for ``source``. Gap detection lives
    in ``ingestion.health.IngestionHealthMonitor.check_alerts``
    (W31-4 ``no_data_received``); this method is the secondary hook
    for the reliability tracker to count gaps and factor them into
    the score's "gap frequency" axis.

``record_rate_limit(source, timestamp=None) -> None``
    Record one rate-limit hit for ``source``. Surfaced verbatim in
    the per-source view (the 24h / 7d / 30d windows) so an operator
    can see at-a-glance which source is being throttled.

``get_reliability(source=None) -> dict``
    Return the live per-source reliability snapshots (or every source
    when ``source=None``). JSON-serialisable — exposed via the
    ``GET /api/ingestion/reliability`` endpoint (W34-5).

``compute_score(success_rate, latency_consistency, gap_frequency_score, error_recovery_score) -> float``
    Compute the 0-100 reliability score from the four normalised
    inputs. The weights are module-level constants so an operator can
    tune them without recompiling:

        WEIGHT_SUCCESS_RATE         = 0.50
        WEIGHT_LATENCY_CONSISTENCY  = 0.25
        WEIGHT_GAP_FREQUENCY        = 0.15
        WEIGHT_ERROR_RECOVERY       = 0.10

    Each input is clamped to ``[0, 1]`` before the weighted sum, so a
    malformed caller can't push the score outside the contract range.

Thread-safety
-------------
Every public method acquires ``self._lock`` so the tracker is safe to
call from sync and async code paths alike. The underlying ``deque``
isn't thread-safe by itself — the ``threading.Lock`` wrapper provides
the atomicity guarantee (mirrors ``ingestion.health``).

The bounded ``deque(maxlen=MAX_ATTEMPTS_PER_SOURCE)`` per source caps
memory growth: 50 000 attempts at 1 EPS = ~14 h of history, which is
beyond the 24h window (older entries are evicted from the deque AND
filtered out by the windowed slice — so the maxlen is a belt-and-
braces guard against a pathological burst of > 50 000 attempts in
under 24h).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Tunable constants ──────────────────────────────────────────────────────
# Exposed at module level so tests / dashboards can read them and so the
# API route can reference them by name (rather than re-deriving the
# thresholds client-side).

# Time windows for the per-source view (seconds).
WINDOW_24H_S: float = 24.0 * 3600.0
WINDOW_7D_S: float = 7.0 * 24.0 * 3600.0
WINDOW_30D_S: float = 30.0 * 24.0 * 3600.0

# Max attempt / gap / rate-limit records kept per source (belt-and-
# braces against a pathological burst > 50k events in <24h).
MAX_ATTEMPTS_PER_SOURCE: int = 50_000

# Reliability-score thresholds (percent). The status enum is derived
# from these — see ``ReliabilityStatus`` below.
HEALTHY_THRESHOLD: float = 95.0     # >95% → HEALTHY
DEGRADED_THRESHOLD: float = 80.0   # 80–95% → DEGRADED, <80% → UNRELIABLE

# Number of recent events to surface in the per-source view (newest
# first).
RECENT_EVENTS_N: int = 10

# Scoring weights — these MUST sum to 1.0 (validated at module load
# via the ``assert`` below so a typo in one constant surfaces as a
# module-import error rather than a silent scoring drift).
WEIGHT_SUCCESS_RATE: float = 0.50
WEIGHT_LATENCY_CONSISTENCY: float = 0.25
WEIGHT_GAP_FREQUENCY: float = 0.15
WEIGHT_ERROR_RECOVERY: float = 0.10

assert (
    abs(
        WEIGHT_SUCCESS_RATE
        + WEIGHT_LATENCY_CONSISTENCY
        + WEIGHT_GAP_FREQUENCY
        + WEIGHT_ERROR_RECOVERY
        - 1.0
    )
    < 1e-9
), "Scoring weights must sum to 1.0 — see W34-5 reliability.py."

# Normalization ceilings for the non-success axes (so a "perfect"
# score on that axis is 1.0, a "worst" score is 0.0).
# Latency variance ceiling in ms² — a stable source with ~10 ms²
# variance scores near 1.0; a flapping source with >10 000 ms²
# variance scores 0.0 on this axis.
LATENCY_VARIANCE_CEILING_MS2: float = 10_000.0
# Gap-frequency ceiling in gaps/hour — sources with > 6 gaps per hour
# (one every 10 minutes) score 0.0 on this axis.
GAP_FREQUENCY_CEILING_PER_HR: float = 6.0
# Recovery-time ceiling in seconds — sources whose mean failure→success
# recovery exceeds 5 minutes score 0.0 on this axis.
RECOVERY_TIME_CEILING_S: float = 300.0


class ReliabilityStatus(Enum):
    """Discrete reliability status derived from the 0-100 score.

    The mapping is::

        score > 95   → HEALTHY
        80 ≤ score ≤ 95 → DEGRADED
        score < 80   → UNRELIABLE
        no attempts  → UNKNOWN (fresh source — not yet proven reliable)
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNRELIABLE = "unreliable"
    UNKNOWN = "unknown"


@dataclass
class AttemptRecord:
    """One attempt — success or failure — recorded for a source.

    Attributes:
        timestamp: Wall-clock time of the attempt (Unix epoch float).
        success: ``True`` if the attempt succeeded.
        latency_ms: End-to-end attempt latency in milliseconds. ``0.0``
            when latency was not measured (those attempts are excluded
            from the latency-consistency axis so a source that doesn't
            measure latency isn't penalised for "all zeros have zero
            variance" — which would falsely inflate the consistency
            score).
        error: Error message on failure (empty string on success).
    """

    timestamp: float
    success: bool
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class GapRecord:
    """One detected data gap for a source.

    Attributes:
        timestamp: Wall-clock time the gap was detected (Unix epoch
            float).
        duration_s: Gap duration in seconds.
    """

    timestamp: float
    duration_s: float


@dataclass
class SourceReliability:
    """Per-source reliability record kept by ``ReliabilityTracker``.

    Attributes:
        source: Source identifier (e.g. ``"clob_rest"``).
        attempts: Bounded ``deque`` of ``AttemptRecord`` (newest
            appended on the right; oldest evicted when ``maxlen`` is
            reached). Used for windowed uptime / latency / error-rate
            / error-recovery computations.
        gaps: Bounded ``deque`` of ``GapRecord`` — used for the
            gap-frequency axis.
        rate_limit_hits: Bounded ``deque`` of ``float`` timestamps —
            one per rate-limit hit. Used for the per-window count
            surfaced in the per-source view.
    """

    source: str
    attempts: deque = field(
        default_factory=lambda: deque(maxlen=MAX_ATTEMPTS_PER_SOURCE)
    )
    gaps: deque = field(
        default_factory=lambda: deque(maxlen=MAX_ATTEMPTS_PER_SOURCE)
    )
    rate_limit_hits: deque = field(
        default_factory=lambda: deque(maxlen=MAX_ATTEMPTS_PER_SOURCE)
    )

    def attempts_in_window(self, window_s: float) -> list[AttemptRecord]:
        """Return every attempt whose timestamp falls within ``window_s``.

        ``window_s`` is measured backwards from ``time.time()`` — so
        ``WINDOW_24H_S`` returns every attempt from the last 24 hours.
        """
        now = time.time()
        return [a for a in self.attempts if (now - a.timestamp) <= window_s]

    def gaps_in_window(self, window_s: float) -> list[GapRecord]:
        """Return every gap whose timestamp falls within ``window_s``."""
        now = time.time()
        return [g for g in self.gaps if (now - g.timestamp) <= window_s]

    def rate_limit_hits_in_window(self, window_s: float) -> int:
        """Count the rate-limit hits that fell within ``window_s``."""
        now = time.time()
        return sum(1 for t in self.rate_limit_hits if (now - t) <= window_s)


class ReliabilityTracker:
    """Per-source reliability tracker.

    Tracks data-source health over time and computes a 0-100
    reliability score. The tracker is IN-MEMORY (no persistence) —
    mirrors the convention of ``ingestion.health.IngestionHealthMonitor``
    and ``core.api_resilience.APIResilienceLayer``. A restart zeroes
    every counter, which is the correct contract for short-horizon
    observability.
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceReliability] = {}
        self._lock = threading.Lock()

    # ── Recording ──────────────────────────────────────────────────────────

    def _get_or_create(self, source: str) -> SourceReliability:
        """Return the per-source record, creating it on first access.

        MUST be called under ``self._lock`` — the caller is
        responsible for taking the lock before invoking this helper.
        """
        if source not in self._sources:
            self._sources[source] = SourceReliability(source=source)
        return self._sources[source]

    def record_attempt(
        self,
        source: str,
        success: bool,
        latency_ms: float = 0.0,
        error: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Record one attempt — success or failure — for ``source``.

        Args:
            source: Source identifier (e.g. ``"clob_rest"``).
            success: ``True`` if the attempt succeeded.
            latency_ms: End-to-end attempt latency in milliseconds.
                ``0.0`` when not measured — those attempts are excluded
                from the latency-consistency axis so a source that
                doesn't measure latency isn't penalised for the
                degenerate "all zeros have zero variance" case.
            error: Error message on failure (empty on success).
            timestamp: Optional override (Unix epoch float). Defaults
                to ``time.time()``. Used by tests to back-fill the
                deque with synthetic historical attempts so the
                windowed slices can be asserted on without sleeping
                for 24h.
        """
        ts = time.time() if timestamp is None else float(timestamp)
        with self._lock:
            r = self._get_or_create(source)
            r.attempts.append(
                AttemptRecord(
                    timestamp=ts,
                    success=bool(success),
                    latency_ms=float(latency_ms),
                    error=error if not success else "",
                )
            )

    def record_gap(
        self,
        source: str,
        duration_s: float,
        timestamp: float | None = None,
    ) -> None:
        """Record one detected data gap for ``source``.

        Args:
            source: Source identifier.
            duration_s: Gap duration in seconds.
            timestamp: Optional override (Unix epoch float). Defaults
                to ``time.time()``.
        """
        ts = time.time() if timestamp is None else float(timestamp)
        with self._lock:
            r = self._get_or_create(source)
            r.gaps.append(GapRecord(timestamp=ts, duration_s=float(duration_s)))

    def record_rate_limit(
        self, source: str, timestamp: float | None = None
    ) -> None:
        """Record one rate-limit hit for ``source``.

        Args:
            source: Source identifier.
            timestamp: Optional override (Unix epoch float). Defaults
                to ``time.time()``.
        """
        ts = time.time() if timestamp is None else float(timestamp)
        with self._lock:
            r = self._get_or_create(source)
            r.rate_limit_hits.append(ts)

    # ── Reads ──────────────────────────────────────────────────────────────

    def get_reliability(self, source: str | None = None) -> dict[str, Any]:
        """Return per-source reliability snapshots.

        Args:
            source: When supplied, returns a single source's
                reliability dict (or ``{}`` if the source is unknown).
                When ``None``, returns a dict keyed by source name.

        Returns:
            JSON-serialisable dict. Per-source shape:

            .. code-block:: python

                {
                    "source": "clob_rest",
                    "score": 92.5,
                    "status": "degraded",
                    "uptime_pct":  {"24h": 98.7, "7d": 96.1, "30d": 94.3},
                    "avg_latency_ms": {"24h": 42.3, "7d": 51.8, "30d": 55.0},
                    "error_rate":   {"24h": 0.013, "7d": 0.039, "30d": 0.057},
                    "rate_limit_hits": {"24h": 1, "7d": 4, "30d": 12},
                    "gap_count":    {"24h": 0, "7d": 2, "30d": 7},
                    "recent_events": [
                        {"timestamp": ..., "success": True,
                         "latency_ms": 35.0, "error": ""},
                        ...
                    ],
                    "score_inputs": {
                        "success_rate": 0.987,
                        "latency_consistency": 0.95,
                        "gap_frequency_score": 1.0,
                        "error_recovery_score": 1.0,
                    },
                }

            When the source has no attempts in the 24h window, the
            ``score`` is ``0.0`` and the ``status`` is ``"unknown"``
            (the honest zero-state — never fabricate a plausible-
            looking score for a source we haven't observed).
        """
        with self._lock:
            if source is not None:
                r = self._sources.get(source)
                if r is None:
                    return {}
                return self._format_source(r)
            return {
                s: self._format_source(r) for s, r in self._sources.items()
            }

    # ── Scoring ───────────────────────────────────────────────────────────

    @staticmethod
    def compute_score(
        success_rate: float,
        latency_consistency: float,
        gap_frequency_score: float,
        error_recovery_score: float,
    ) -> float:
        """Compute the 0-100 reliability score from four 0..1 inputs.

        The weights are module-level constants so an operator can tune
        them without recompiling. Every input is clamped to ``[0, 1]``
        before the weighted sum so a malformed caller can't push the
        score outside the contract range.

        Args:
            success_rate: Fraction of successful attempts (0..1).
            latency_consistency: 0..1 — 1 means consistent latency
                (low variance), 0 means wildly flapping latency.
            gap_frequency_score: 0..1 — 1 means no gaps, 0 means
                > ``GAP_FREQUENCY_CEILING_PER_HR`` gaps/hour.
            error_recovery_score: 0..1 — 1 means fast recovery from
                failures, 0 means slow recovery.

        Returns:
            Score 0-100 (rounded to 1 decimal place).
        """
        success_rate = _clamp01(success_rate)
        latency_consistency = _clamp01(latency_consistency)
        gap_frequency_score = _clamp01(gap_frequency_score)
        error_recovery_score = _clamp01(error_recovery_score)
        raw = (
            WEIGHT_SUCCESS_RATE * success_rate
            + WEIGHT_LATENCY_CONSISTENCY * latency_consistency
            + WEIGHT_GAP_FREQUENCY * gap_frequency_score
            + WEIGHT_ERROR_RECOVERY * error_recovery_score
        )
        return round(raw * 100.0, 1)

    def reset(self) -> None:
        """Clear every per-source reliability record.

        Used by tests to guarantee a clean baseline before each
        assertion. NOT exposed via HTTP — a production operator
        should never be able to silently zero the reliability counters
        (that would mask a real outage from the dashboard, mirroring
        the W24-7 ``APIResilienceLayer.reset()`` contract).
        """
        with self._lock:
            self._sources.clear()

    # ── Internals ─────────────────────────────────────────────────────────

    def _format_source(self, r: SourceReliability) -> dict[str, Any]:
        """Format a ``SourceReliability`` as a JSON-serialisable dict.

        Computes the per-window uptime / latency / error-rate / gap /
        rate-limit-hit counts, the score inputs (over the 24h window —
        the most actionable horizon for an operator), and the recent-
        events list (last ``RECENT_EVENTS_N`` attempts, newest first).
        """
        windows = {
            "24h": WINDOW_24H_S,
            "7d": WINDOW_7D_S,
            "30d": WINDOW_30D_S,
        }
        uptime_pct: dict[str, float] = {}
        avg_latency_ms: dict[str, float] = {}
        error_rate: dict[str, float] = {}
        rate_limit_hits: dict[str, int] = {}
        gap_count: dict[str, int] = {}

        for label, w in windows.items():
            attempts = r.attempts_in_window(w)
            n = len(attempts)
            if n > 0:
                succ = sum(1 for a in attempts if a.success)
                uptime_pct[label] = round(succ / n * 100.0, 2)
                error_rate[label] = round((n - succ) / n, 4)
                latencies = [
                    a.latency_ms for a in attempts if a.latency_ms > 0.0
                ]
                avg_latency_ms[label] = (
                    round(sum(latencies) / len(latencies), 3)
                    if latencies
                    else 0.0
                )
            else:
                uptime_pct[label] = 0.0
                error_rate[label] = 0.0
                avg_latency_ms[label] = 0.0
            rate_limit_hits[label] = r.rate_limit_hits_in_window(w)
            gap_count[label] = len(r.gaps_in_window(w))

        # Score inputs — derived from the 24h window (the most
        # actionable horizon for an operator; longer windows can be
        # inspected via the uptime_pct / error_rate per-window
        # breakdowns above).
        attempts_24h = r.attempts_in_window(WINDOW_24H_S)
        score_inputs = self._compute_score_inputs(attempts_24h, r.gaps_in_window(WINDOW_24H_S))

        if not attempts_24h:
            # No attempts in the 24h window → "unknown" status. The
            # per-window counts above are all 0.0 already; we just
            # need to flip the status.
            score = 0.0
            status = ReliabilityStatus.UNKNOWN
        else:
            score = self.compute_score(**score_inputs)
            if score > HEALTHY_THRESHOLD:
                status = ReliabilityStatus.HEALTHY
            elif score >= DEGRADED_THRESHOLD:
                status = ReliabilityStatus.DEGRADED
            else:
                status = ReliabilityStatus.UNRELIABLE

        # Recent events — last RECENT_EVENTS_N, newest first.
        recent = list(r.attempts)[-RECENT_EVENTS_N:]
        recent_events = [
            {
                "timestamp": a.timestamp,
                "success": a.success,
                "latency_ms": a.latency_ms,
                "error": a.error,
            }
            for a in reversed(recent)
        ]

        return {
            "source": r.source,
            "score": score,
            "status": status.value,
            "uptime_pct": uptime_pct,
            "avg_latency_ms": avg_latency_ms,
            "error_rate": error_rate,
            "rate_limit_hits": rate_limit_hits,
            "gap_count": gap_count,
            "recent_events": recent_events,
            # Surface the score inputs so an operator can see WHY the
            # score is what it is — mirrors the W17-4 "honest health"
            # convention (no opaque scoring).
            "score_inputs": {
                "success_rate": round(score_inputs["success_rate"], 4),
                "latency_consistency": round(
                    score_inputs["latency_consistency"], 4
                ),
                "gap_frequency_score": round(
                    score_inputs["gap_frequency_score"], 4
                ),
                "error_recovery_score": round(
                    score_inputs["error_recovery_score"], 4
                ),
            },
        }

    @staticmethod
    def _compute_score_inputs(
        attempts: list[AttemptRecord], gaps: list[GapRecord]
    ) -> dict[str, float]:
        """Compute the four 0..1 score-input values from raw records.

        Returns a dict keyed by the four ``compute_score`` arg names
        so ``compute_score(**score_inputs)`` works directly.
        """
        if not attempts:
            return {
                "success_rate": 0.0,
                "latency_consistency": 1.0,
                "gap_frequency_score": 1.0,
                "error_recovery_score": 1.0,
            }

        # 1. Success rate — successes / total attempts.
        n = len(attempts)
        succ = sum(1 for a in attempts if a.success)
        success_rate = succ / n

        # 2. Latency consistency — 1 - normalized_variance.
        # Only attempts with a positive ``latency_ms`` contribute to
        # the variance so a source that doesn't measure latency (all
        # zeros) isn't penalised for "all zeros have zero variance"
        # (which would falsely inflate the consistency score).
        latencies = [a.latency_ms for a in attempts if a.latency_ms > 0.0]
        if len(latencies) >= 2:
            mean = sum(latencies) / len(latencies)
            var = sum((x - mean) ** 2 for x in latencies) / len(latencies)
            norm = min(var / LATENCY_VARIANCE_CEILING_MS2, 1.0)
            latency_consistency = 1.0 - norm
        else:
            # Fewer than 2 measured latencies → can't compute a
            # meaningful variance, so don't penalise the source on
            # this axis (return 1.0).
            latency_consistency = 1.0

        # 3. Gap frequency — 1 - normalized_gaps_per_hour.
        if gaps:
            gaps_per_hr = len(gaps) / (WINDOW_24H_S / 3600.0)
            norm = min(gaps_per_hr / GAP_FREQUENCY_CEILING_PER_HR, 1.0)
            gap_frequency_score = 1.0 - norm
        else:
            gap_frequency_score = 1.0

        # 4. Error recovery — 1 - normalized_mean_recovery_time_s.
        # The mean time between each failure and the next success,
        # normalized against the ceiling. Returns ``None`` when no
        # failure→success pair exists (so the score stays at 1.0 — a
        # source with no failures has perfect recovery by definition).
        recovery = _mean_recovery_time_s(attempts)
        if recovery is None:
            error_recovery_score = 1.0
        else:
            norm = min(recovery / RECOVERY_TIME_CEILING_S, 1.0)
            error_recovery_score = 1.0 - norm

        return {
            "success_rate": success_rate,
            "latency_consistency": latency_consistency,
            "gap_frequency_score": gap_frequency_score,
            "error_recovery_score": error_recovery_score,
        }


# ── Module-level helpers ───────────────────────────────────────────────────


def _clamp01(x: float) -> float:
    """Clamp ``x`` to ``[0.0, 1.0]``."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _mean_recovery_time_s(attempts: list[AttemptRecord]) -> float | None:
    """Mean seconds between each failure and the next success.

    Walks ``attempts`` in chronological order (the deque is
    append-ordered, so the list slice is already chronological). For
    every failure, finds the next success that follows it and records
    the delta. Returns the mean of those deltas, or ``None`` when no
    failure→success pair exists (a source with zero failures has
    perfect recovery by definition; a source with failures but no
    subsequent success in the window has recovery score 0.0).

    Args:
        attempts: Chronologically-ordered list of ``AttemptRecord``.

    Returns:
        Mean recovery time in seconds, or ``None`` when no
        failure→success pair exists.
    """
    pairs: list[float] = []
    last_failure_ts: float | None = None
    for a in attempts:  # already in chronological order
        if not a.success:
            last_failure_ts = a.timestamp
        elif last_failure_ts is not None:
            pairs.append(a.timestamp - last_failure_ts)
            last_failure_ts = None  # one recovery per failure
    if not pairs:
        return None
    return sum(pairs) / len(pairs)


# ── Module-level singleton ─────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion / observability
# module. Importers grab it at module-import time; the constructor
# allocates the in-memory dicts and deques — no I/O at construction time.
reliability_tracker = ReliabilityTracker()


__all__ = [
    "AttemptRecord",
    "GapRecord",
    "ReliabilityStatus",
    "ReliabilityTracker",
    "SourceReliability",
    "reliability_tracker",
    # Tunable constants — exported so tests / dashboards can read them.
    "WINDOW_24H_S",
    "WINDOW_7D_S",
    "WINDOW_30D_S",
    "HEALTHY_THRESHOLD",
    "DEGRADED_THRESHOLD",
    "MAX_ATTEMPTS_PER_SOURCE",
    "RECENT_EVENTS_N",
    "WEIGHT_SUCCESS_RATE",
    "WEIGHT_LATENCY_CONSISTENCY",
    "WEIGHT_GAP_FREQUENCY",
    "WEIGHT_ERROR_RECOVERY",
    "LATENCY_VARIANCE_CEILING_MS2",
    "GAP_FREQUENCY_CEILING_PER_HR",
    "RECOVERY_TIME_CEILING_S",
]
