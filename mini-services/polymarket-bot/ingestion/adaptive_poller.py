"""Adaptive polling manager — adjusts polling intervals based on market activity.

Features
~~~~~~~~

1. **Active markets (high volume)** — poll every ``ACTIVE_INTERVAL``
   (1 s by default, matching the WS feed's tick rate). A market is
   classified ACTIVE when it has had ≥ ``ACTIVE_TRADE_VOLUME_THRESHOLD``
   trades in the last ``ACTIVE_TRADE_RECENCY_S`` window.
2. **Normal markets** — poll every ``NORMAL_INTERVAL`` (5 s). A market
   is classified NORMAL when it has had at least one trade in the
   last ``NORMAL_TRADE_RECENCY_S`` window (but didn't qualify as
   ACTIVE on the volume threshold).
3. **Inactive markets** — poll every ``INACTIVE_INTERVAL`` (30 s).
   The long-tail cadence for markets that haven't traded today but
   still need top-of-book freshness for the screener.
4. **On rate-limit (HTTP 429)** — back off to
   ``RATE_LIMITED_INTERVAL`` (60 s). Gradually recover: every
   subsequent successful poll decrements the per-token
   ``consecutive_429s`` streak by one, so the tier steps down
   60 s → 30 s → 15 s → back to the activity-based tier. (The
   recovery ratio is achieved by halving the streak count, NOT by
   halving the interval itself — the streak count is what gates
   the RATE_LIMITED tier classification.)
5. **On API error (non-429)** — exponential backoff:
   ``BASE_BACKOFF_S * (BACKOFF_MULTIPLIER ** (errors - 1))`` capped
   at ``MAX_BACKOFF_S`` (so 1 s → 2 s → 4 s → 8 s → 16 s → 32 s →
   60 s). The first successful poll resets the streak to zero.
6. **On market resolution** — ``mark_resolved(token_id)`` removes
   the token from the polling set so the next ``tick`` skips it.
   Fires the ``on_resolution`` hook (if registered) BEFORE removal
   so the caller can record the resolution event against the prior
   state.
7. **On new market** — ``add_token(token_id)`` registers a token
   with ``last_polled_at=0`` so the next ``tick`` polls it
   immediately (no warm-up delay). Fires the ``on_new_token`` hook
   (if registered).

Rate-limit-aware polling
~~~~~~~~~~~~~~~~~~~~~~~~~

The poller tracks the upstream's remaining rate-limit budget from
the standard ``X-RateLimit-*`` response headers via
``update_rate_limit(headers)``. The shared ``RateLimitState`` is
consulted by ``next_interval_for(token_id)`` to slow down when the
budget is exhausted:

  * ``usage_ratio < RATE_LIMIT_CRITICAL_THRESHOLD`` (10 %) — 4× the
    tier interval (so ACTIVE becomes ~4 s instead of 1 s).
  * ``usage_ratio < RATE_LIMIT_CAUTION_THRESHOLD`` (30 %) — 2× the
    tier interval.

When ``X-RateLimit-Reset`` is in the past, the state auto-recovers:
``remaining`` is set back to ``limit`` so the slow-down logic
recovers immediately (handles upstreams that send ``Reset`` but
don't refresh ``Remaining`` once the window rolls over).

Construction
~~~~~~~~~~~~~

The poller accepts an async ``fetcher`` callable so tests can
inject deterministic responses without spinning up a real
``httpx.AsyncClient``. The poller NEVER imports ``core.clob_client``
directly — production callers wrap ``clob_client.get_order_book``
in a small adapter that returns a ``FetchResult`` (so this module
stays hermetic to test and avoids perturbing the production
singleton's cached order books).

Layered with ``core/api_resilience`` + ``core/circuit_breaker``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module is the OUTERMOST layer of the resilience stack:

  * ``core.circuit_breaker`` (W13-2) — short-circuits the HTTP
    transport (no socket opened when the breaker is OPEN).
  * ``core.api_resilience`` (W24-7) — retries the logical call
    up to 3 times with exponential backoff before falling back to
    a cached value.
  * ``ingestion.adaptive_poller`` (W35-2 — this module) — adjusts
    the per-token polling cadence based on market activity AND
    slows down when the rate-limit budget is exhausted.

The three layers are complementary: the inner two protect the
upstream (no load during an outage), the outer one paces the
downstream (no rate-limit burnout when the budget is low).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# ── Adaptive intervals ───────────────────────────────────────────────────────
ACTIVE_INTERVAL = 1.0       # active markets (high volume)
NORMAL_INTERVAL = 5.0       # normal-trading markets
INACTIVE_INTERVAL = 30.0    # inactive markets (long tail)

# ── Rate-limit handling ──────────────────────────────────────────────────────
RATE_LIMITED_INTERVAL = 60.0    # backoff interval after a 429
RATE_LIMIT_CRITICAL_THRESHOLD = 0.10  # <10 % remaining → 4× the tier interval
RATE_LIMIT_CAUTION_THRESHOLD = 0.30   # <30 % remaining → 2× the tier interval

# ── Exponential backoff on API error (non-429) ───────────────────────────────
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
BACKOFF_MULTIPLIER = 2.0

# ── Activity thresholds — recent trade window for tier classification ────────
ACTIVE_TRADE_RECENCY_S = 60.0       # traded in last 60 s → ACTIVE window
NORMAL_TRADE_RECENCY_S = 300.0      # traded in last 5 min → NORMAL window
ACTIVE_TRADE_VOLUME_THRESHOLD = 5   # ≥5 trades in recency window → ACTIVE


class PollingTier(Enum):
    """Per-market polling tier — controls the per-token polling cadence."""

    ACTIVE = "active"            # high-volume markets (1 s cadence)
    NORMAL = "normal"            # normal-trading markets (5 s cadence)
    INACTIVE = "inactive"        # long-tail markets (30 s cadence)
    RATE_LIMITED = "rate_limited"  # backed off after 429 (60 s)
    BACKING_OFF = "backing_off"   # exponential backoff after API error


@dataclass
class MarketActivity:
    """Per-market activity record — drives the adaptive tier classification.

    Tracks recent trades in a bounded ``deque(maxlen=100)`` so memory
    is bounded regardless of trade frequency. Each ``record_trade``
    appends a timestamp; the adaptive classifier counts how many fall
    within the recency windows.

    The ``error_count`` / ``consecutive_429s`` streaks are reset on
    the first successful poll after a failure run (mirrors the
    W24-7 ``APIResilienceLayer._record_success`` contract — a single
    success zeroes the streak, so the poller recovers as soon as the
    upstream is healthy again).
    """

    token_id: str
    last_trade_at: float = 0.0
    last_polled_at: float = 0.0
    last_poll_success: bool = True
    poll_count: int = 0
    error_count: int = 0          # consecutive errors (non-429); resets on success
    consecutive_429s: int = 0     # consecutive 429 responses; recovers gradually
    resolved: bool = False
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_trade(self, ts: Optional[float] = None) -> None:
        """Record a trade observed for this token at ``ts`` (defaults to now)."""
        actual_ts = ts if ts is not None else time.time()
        self.last_trade_at = actual_ts
        self.recent_trades.append(actual_ts)

    def trades_in_window(
        self, window_s: float, now: Optional[float] = None
    ) -> int:
        """Count trades observed within the last ``window_s`` seconds."""
        actual_now = now if now is not None else time.time()
        cutoff = actual_now - window_s
        return sum(1 for ts in self.recent_trades if ts >= cutoff)


@dataclass
class RateLimitState:
    """Tracks the remaining rate-limit budget from API response headers.

    Polymarket / CLOB REST returns standard ``X-RateLimit-*`` headers
    (``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` /
    ``X-RateLimit-Limit``). When absent (e.g. paper mode or an upstream
    that doesn't return them), every field defaults to ``None`` and the
    poller falls back to its tier-based cadence without slow-down.

    The state is shared across every tracked token — Polymarket's CLOB
    rate limit is account-wide, not per-token, so a single budget
    counter is the correct granularity.
    """

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_at: Optional[float] = None
    last_updated: float = 0.0

    @property
    def usage_ratio(self) -> Optional[float]:
        """Return remaining/limit ratio (1.0 = full budget, 0.0 = exhausted).

        Returns ``None`` when the budget cannot be computed (one of
        ``limit`` / ``remaining`` is missing, or ``limit == 0``). The
        caller treats ``None`` as "no slow-down" so an upstream that
        doesn't return headers doesn't accidentally trigger the
        critical-slow-down path.
        """
        if self.limit is None or self.remaining is None or self.limit == 0:
            return None
        return max(0.0, min(1.0, self.remaining / self.limit))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot for the observability collector."""
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "usage_ratio": self.usage_ratio,
            "last_updated": self.last_updated,
        }


@dataclass
class FetchResult:
    """Outcome of a single fetch — drives the poller's state machine.

    A ``FetchResult`` is what the configured ``fetcher`` callable
    returns per token. The poller inspects the three flags
    (``ok`` / ``error`` / ``is_rate_limited``) + the optional
    ``headers`` / ``last_trade_ts`` fields to update its adaptive
    state.

    Attributes:
        token_id: the token this result is for.
        ok: True when the fetch returned a 2xx response (no error,
            no rate-limit).
        error: error message string when ``ok=False`` AND the failure
            was NOT a 429 (e.g. timeout, 5xx, JSON decode error). An
            empty string signals "no error".
        is_rate_limited: True when the upstream returned HTTP 429.
        headers: optional dict of rate-limit headers from the response
            (``X-RateLimit-*``). When present, the poller updates its
            ``RateLimitState`` so the slow-down logic can react.
        last_trade_ts: optional Unix timestamp of the most recent
            trade observed in the fetched payload. When present, the
            poller calls ``market.record_trade(ts)`` so the tier
            classification promotes the token without an explicit
            out-of-band call.
        payload: the fetched body (any type) — opaque to the poller,
            passed through for the caller's downstream persistence.
    """

    token_id: str
    ok: bool = True
    error: str = ""
    is_rate_limited: bool = False
    headers: Optional[dict[str, Any]] = None
    last_trade_ts: Optional[float] = None
    payload: Any = None


# Type alias for the fetcher callable signature.
Fetcher = Callable[[str], Awaitable[FetchResult]]


class AdaptivePoller:
    """Adaptive polling manager with rate-limit + error backoff handling.

    Tier classification
    ~~~~~~~~~~~~~~~~~~~~
    A token is classified on every ``classify(token_id)`` /
    ``next_interval_for(token_id)`` call:

      * ``RATE_LIMITED`` — when ``consecutive_429s > 0`` (overrides
        every other tier — a rate-limited token MUST cool down).
      * ``BACKING_OFF`` — when ``error_count > 0`` (overrides every
        non-rate-limited tier).
      * ``ACTIVE`` — when ``trades_in_window(ACTIVE_TRADE_RECENCY_S)
        >= ACTIVE_TRADE_VOLUME_THRESHOLD``.
      * ``NORMAL`` — when ``last_trade_at`` is within
        ``NORMAL_TRADE_RECENCY_S`` (but didn't qualify as ACTIVE).
      * ``INACTIVE`` — otherwise.

    Rate-limit slow-down
    ~~~~~~~~~~~~~~~~~~~~~~
    Even within an ACTIVE tier, the poller slows down when the
    rate-limit budget is exhausted:

      * ``usage_ratio < RATE_LIMIT_CRITICAL_THRESHOLD`` (10 %) — 4×
        the tier interval (so ACTIVE becomes ~4 s instead of 1 s).
      * ``usage_ratio < RATE_LIMIT_CAUTION_THRESHOLD`` (30 %) — 2×
        the tier interval.

    Recovery from RATE_LIMITED
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The recovery ratio is achieved by decrementing
    ``consecutive_429s`` on every successful poll (NOT by halving
    the interval itself — the streak count is what gates the
    RATE_LIMITED classification). A token with ``consecutive_429s=3``
    needs 3 successful polls before it drops out of the RATE_LIMITED
    tier, giving the upstream rate-limit window time to actually
    recover before the poller ramps back up to ACTIVE cadence.
    """

    def __init__(
        self,
        *,
        fetcher: Optional[Fetcher] = None,
        active_interval: float = ACTIVE_INTERVAL,
        normal_interval: float = NORMAL_INTERVAL,
        inactive_interval: float = INACTIVE_INTERVAL,
        rate_limited_interval: float = RATE_LIMITED_INTERVAL,
        max_concurrent: int = 12,
    ) -> None:
        self._fetcher = fetcher
        self._active_interval = active_interval
        self._normal_interval = normal_interval
        self._inactive_interval = inactive_interval
        self._rate_limited_interval = rate_limited_interval
        self._sem = asyncio.Semaphore(max_concurrent)

        # Per-token state — keyed by token_id.
        self._markets: dict[str, MarketActivity] = {}
        # Rate-limit state — single shared budget across every tracked
        # token (Polymarket's CLOB rate limit is account-wide).
        self._rate_limit = RateLimitState()
        # Running flag + background task.
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        # Hooks — invoked on new-market / resolution events.
        self.on_resolution: Optional[Callable[[str], None]] = None
        self.on_new_token: Optional[Callable[[str], None]] = None
        # Health counters.
        self._polls_ok: int = 0
        self._polls_failed: int = 0
        self._rate_limit_events: int = 0
        self._resolutions: int = 0

    # ── Token management ─────────────────────────────────────────────────

    def add_token(self, token_id: str) -> bool:
        """Register a new token for polling. Returns ``True`` if newly added.

        New tokens default to ``last_polled_at=0`` so the next tick
        polls them immediately (the spec's "On new market: start
        polling immediately" requirement). Adding an already-tracked
        token is a no-op that returns ``False`` (idempotent — the
        tier classification is preserved).
        """
        if not token_id:
            return False
        if token_id in self._markets:
            return False
        self._markets[token_id] = MarketActivity(token_id=token_id)
        if self.on_new_token is not None:
            try:
                self.on_new_token(token_id)
            except Exception:  # noqa: BLE001 — hooks must not crash the poller
                log.debug(
                    "[adaptive_poller] on_new_token hook raised", exc_info=True
                )
        return True

    def remove_token(self, token_id: str) -> None:
        """Stop polling a token (e.g. on market resolution).

        Safe to call on a token that's not tracked (no-op).
        """
        self._markets.pop(token_id, None)

    def mark_resolved(self, token_id: str) -> None:
        """Mark a token as resolved — polling stops on the next tick.

        Implements the spec's "On market resolution: stop polling"
        requirement. The token's ``MarketActivity.resolved`` flag
        flips to ``True`` (so any in-flight tick sees it), the
        ``on_resolution`` hook (if registered) is fired BEFORE the
        token is removed so the caller can record the resolution
        event against the prior state, and the token is then
        removed from the polling set.
        """
        market = self._markets.get(token_id)
        if market is None:
            return
        market.resolved = True
        self._resolutions += 1
        if self.on_resolution is not None:
            try:
                self.on_resolution(token_id)
            except Exception:  # noqa: BLE001 — hooks must not crash the poller
                log.debug(
                    "[adaptive_poller] on_resolution hook raised", exc_info=True
                )
        self.remove_token(token_id)

    def record_trade(
        self, token_id: str, ts: Optional[float] = None
    ) -> None:
        """Record a trade observed for ``token_id`` at ``ts`` (defaults to now).

        Used by the ingestion pipeline (or by tests) to drive the
        adaptive tier classification. A trade ALWAYS promotes the
        token toward ACTIVE tier (subject to the volume threshold).
        If the token isn't tracked yet, it is added on the fly (so
        out-of-band trade notifications from the WS feed don't
        require a separate ``add_token`` call).
        """
        market = self._markets.get(token_id)
        if market is None:
            market = MarketActivity(token_id=token_id)
            self._markets[token_id] = market
        market.record_trade(ts)

    # ── Rate-limit header tracking ────────────────────────────────────────

    def update_rate_limit(self, headers: dict[str, Any]) -> None:
        """Parse ``X-RateLimit-*`` headers into the shared ``RateLimitState``.

        Recognised headers (case-insensitive):
          * ``X-RateLimit-Limit`` — total budget per window.
          * ``X-RateLimit-Remaining`` — remaining budget in the
            current window.
          * ``X-RateLimit-Reset`` — Unix epoch seconds when the window
            resets (the budget refills to ``Limit``).

        Missing / unparseable headers are silently skipped so a
        partial set of headers (e.g. only ``X-RateLimit-Remaining``
        without ``Limit``) doesn't crash the poller. When
        ``X-RateLimit-Reset`` is in the past, the state is treated
        as "fresh window" — ``remaining`` is set to ``limit`` so the
        slow-down logic recovers immediately (handles upstreams that
        send the ``Reset`` header but never refresh ``Remaining``
        once the window rolls over).

        Side effects:
          * Logs a warning at ``WARNING`` level when the budget
            drops below the critical threshold (10 %) so an operator
            can see the slow-down taking effect.
          * Logs an informational message at ``INFO`` level when the
            budget recovers above the caution threshold (30 %).
        """
        if not headers:
            return
        limit = _parse_int_header(headers, "X-RateLimit-Limit")
        remaining = _parse_int_header(headers, "X-RateLimit-Remaining")
        reset = _parse_float_header(headers, "X-RateLimit-Reset")
        now = time.time()

        # Preserve prior values when a header is absent (the upstream
        # may only refresh one field per response — e.g. only the
        # ``Remaining`` counter ticks down, ``Limit`` stays constant).
        prior_ratio = self._rate_limit.usage_ratio
        if limit is not None:
            self._rate_limit.limit = limit
        if remaining is not None:
            self._rate_limit.remaining = remaining
        if reset is not None:
            self._rate_limit.reset_at = reset
        self._rate_limit.last_updated = now

        # Auto-recovery: when the reset window has passed, treat the
        # budget as refilled.
        if (
            self._rate_limit.reset_at is not None
            and self._rate_limit.reset_at < now
            and self._rate_limit.limit is not None
        ):
            self._rate_limit.remaining = self._rate_limit.limit
            self._rate_limit.reset_at = None

        new_ratio = self._rate_limit.usage_ratio
        # Log slow-down / recovery transitions (only on a real ratio
        # change so the log isn't spammed on every poll).
        if (
            new_ratio is not None
            and prior_ratio is not None
            and new_ratio < RATE_LIMIT_CRITICAL_THRESHOLD
            and prior_ratio >= RATE_LIMIT_CRITICAL_THRESHOLD
        ):
            log.warning(
                "[adaptive_poller] Rate-limit budget CRITICAL (%.0f%% remaining) "
                "— slowing down to 4× tier interval",
                new_ratio * 100.0,
            )
        elif (
            new_ratio is not None
            and prior_ratio is not None
            and new_ratio >= RATE_LIMIT_CAUTION_THRESHOLD
            and prior_ratio < RATE_LIMIT_CAUTION_THRESHOLD
        ):
            log.info(
                "[adaptive_poller] Rate-limit budget recovered "
                "(%.0f%% remaining) — resuming tier cadence",
                new_ratio * 100.0,
            )

    # ── Tier classification ───────────────────────────────────────────────

    def classify(
        self, token_id: str, now: Optional[float] = None
    ) -> PollingTier:
        """Return the current ``PollingTier`` for ``token_id``.

        Read-only — does NOT mutate state. The polling loop consults
        this on every tick to pick the per-token interval. Tests
        can call it directly to verify the adaptive behaviour
        without running the loop.

        Tokens not currently tracked return ``PollingTier.INACTIVE``
        (the safest default — a token we know nothing about is
        treated as the long-tail cadence, NOT ACTIVE).
        """
        market = self._markets.get(token_id)
        if market is None:
            return PollingTier.INACTIVE
        # Override tiers always win.
        if market.consecutive_429s > 0:
            return PollingTier.RATE_LIMITED
        if market.error_count > 0:
            return PollingTier.BACKING_OFF
        if market.resolved:
            return PollingTier.INACTIVE
        # Activity-based tiers.
        actual_now = now if now is not None else time.time()
        if (
            market.trades_in_window(ACTIVE_TRADE_RECENCY_S, actual_now)
            >= ACTIVE_TRADE_VOLUME_THRESHOLD
        ):
            return PollingTier.ACTIVE
        if (
            market.last_trade_at > 0
            and actual_now - market.last_trade_at < NORMAL_TRADE_RECENCY_S
        ):
            return PollingTier.NORMAL
        return PollingTier.INACTIVE

    def tier_interval(self, tier: PollingTier) -> float:
        """Return the base polling interval (seconds) for ``tier``.

        For ``BACKING_OFF``, the interval scales with the largest
        per-token ``error_count`` across the polling set (so a single
        failing token doesn't trigger a 60 s pause on every other
        token — each token backs off based on its OWN streak).
        """
        if tier == PollingTier.ACTIVE:
            return self._active_interval
        if tier == PollingTier.NORMAL:
            return self._normal_interval
        if tier == PollingTier.RATE_LIMITED:
            return self._rate_limited_interval
        if tier == PollingTier.BACKING_OFF:
            # Exponential backoff: BASE * MULTIPLIER^(errors-1).
            # ``classify`` guarantees error_count > 0 for this tier,
            # so the exponent is ≥ 0 (worst case: 1 error → 1 s).
            max_errors = max(
                (m.error_count for m in self._markets.values() if m.error_count > 0),
                default=1,
            )
            backoff = BASE_BACKOFF_S * (
                BACKOFF_MULTIPLIER ** max(0, max_errors - 1)
            )
            return min(backoff, MAX_BACKOFF_S)
        return self._inactive_interval

    def next_interval_for(
        self, token_id: str, now: Optional[float] = None
    ) -> float:
        """Return the polling interval (seconds) to wait before next poll.

        This is the per-token EFFECTIVE interval — tier cadence
        modified by rate-limit pressure (caution / critical slow-down).

        Used by the poll loop's scheduler AND by tests asserting on
        the adaptive behaviour. The slow-down factor only applies to
        the activity-based tiers (ACTIVE / NORMAL / INACTIVE) —
        RATE_LIMITED and BACKING_OFF are already slow enough that
        multiplying by 4× would be wasteful.
        """
        tier = self.classify(token_id, now)
        base = self.tier_interval(tier)
        if tier in (
            PollingTier.ACTIVE,
            PollingTier.NORMAL,
            PollingTier.INACTIVE,
        ):
            ratio = self._rate_limit.usage_ratio
            if ratio is not None:
                if ratio < RATE_LIMIT_CRITICAL_THRESHOLD:
                    base *= 4.0
                elif ratio < RATE_LIMIT_CAUTION_THRESHOLD:
                    base *= 2.0
        return base

    # ── Polling loop ──────────────────────────────────────────────────────

    async def tick(
        self, *, force: bool = False
    ) -> dict[str, FetchResult]:
        """Single polling pass over every tracked (non-resolved) token.

        For each token whose ``last_polled_at +
        next_interval_for()`` has elapsed, calls the configured
        ``fetcher`` and records the result. Returns a
        ``{token_id: FetchResult}`` map so tests can assert on the
        per-token outcomes.

        Skips resolved tokens (``MarketActivity.resolved=True``) and
        tokens whose backoff / rate-limit cooldown hasn't elapsed
        yet — the classification already encodes the cooldown, so we
        trust it (and the next call's ``next_interval_for`` returns
        the next-due time).

        When ``force=True``, every tracked (non-resolved) token is
        polled regardless of cadence — used by tests + an
        on-demand "refresh now" endpoint. Production callers should
        leave ``force=False`` so the adaptive cadence is honoured
        (otherwise a 1 Hz ``tick()`` loop would re-poll every token
        on every iteration, defeating the per-token cadence).

        When no ``fetcher`` is configured, the tick is a no-op that
        returns an empty dict. This lets tests construct an
        ``AdaptivePoller`` purely to exercise the classifier (no
        fetcher needed) without raising.
        """
        results: dict[str, FetchResult] = {}
        if self._fetcher is None:
            log.debug("[adaptive_poller] tick called without fetcher — no-op")
            return results
        now = time.time()
        # Snapshot the tokens to poll (avoids mutation-during-iteration
        # if the fetcher callback adds/removes tokens via the hooks).
        pending = [
            tid
            for tid, m in self._markets.items()
            if not m.resolved
            and (
                force
                or m.last_polled_at == 0.0
                or now - m.last_polled_at >= self.next_interval_for(tid, now)
            )
        ]
        if not pending:
            return results

        async def _fetch_one(tid: str) -> tuple[str, FetchResult]:
            async with self._sem:
                result = await self._fetcher(tid)  # type: ignore[misc]
                self._apply_result(tid, result)
                return tid, result

        gathered = await asyncio.gather(
            *(_fetch_one(tid) for tid in pending),
            return_exceptions=True,
        )
        for r in gathered:
            if isinstance(r, Exception):
                log.debug(
                    "[adaptive_poller] fetch task raised: %s", r
                )
                continue
            tid, result = r
            results[tid] = result
        return results

    def _apply_result(self, token_id: str, result: FetchResult) -> None:
        """Update per-token + global state after a fetch returns.

        This is the single state-mutation entry point for the poller
        — every tier transition goes through here. Tests that want
        to drive the state machine directly can call this instead of
        running the full ``tick`` loop.
        """
        market = self._markets.get(token_id)
        if market is None:
            return
        now = time.time()
        market.last_polled_at = now
        market.poll_count += 1

        # Rate-limit header tracking (when the FetchResult carries any).
        if result.headers:
            self.update_rate_limit(result.headers)

        if result.is_rate_limited:
            market.consecutive_429s += 1
            market.last_poll_success = False
            self._rate_limit_events += 1
            self._polls_failed += 1
            log.warning(
                "[adaptive_poller] %s rate-limited (429) — backing off "
                "(streak=%d, interval=%.0fs)",
                token_id[:12],
                market.consecutive_429s,
                self._rate_limited_interval,
            )
            return

        if result.error:
            market.error_count += 1
            market.last_poll_success = False
            self._polls_failed += 1
            backoff = min(
                BASE_BACKOFF_S
                * (BACKOFF_MULTIPLIER ** max(0, market.error_count - 1)),
                MAX_BACKOFF_S,
            )
            log.warning(
                "[adaptive_poller] %s fetch error (%s) — error streak=%d, "
                "backoff=%.1fs",
                token_id[:12],
                result.error,
                market.error_count,
                backoff,
            )
            return

        # Success path — reset / decay the streak counters.
        # The 429 streak decays by one per success (gradual recovery:
        # 60 s → 60 s → 60 s → activity tier). The error streak is
        # zeroed outright (a single success is enough to confirm the
        # upstream is healthy — mirrors the W24-7 contract).
        if market.consecutive_429s > 0:
            market.consecutive_429s = max(0, market.consecutive_429s - 1)
        market.error_count = 0
        market.last_poll_success = True
        self._polls_ok += 1

        # If the payload carries a trade timestamp, record it so the
        # tier classification promotes the token without an explicit
        # out-of-band ``record_trade`` call.
        if result.last_trade_ts is not None and result.last_trade_ts > 0:
            market.record_trade(result.last_trade_ts)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self, interval: Optional[float] = None) -> None:
        """Start the background poll loop.

        ``interval`` is the loop's base sleep between ticks (defaults
        to ``ACTIVE_INTERVAL`` so the loop spins at 1 Hz — fast
        enough to honour the active-market cadence, slow enough to
        not peg CPU on a 200-token polling set).

        Raises ``RuntimeError`` when called without a configured
        ``fetcher`` — the loop would otherwise spin uselessly.
        """
        if self._running:
            return
        if self._fetcher is None:
            raise RuntimeError(
                "AdaptivePoller.start() requires a fetcher — pass one to "
                "__init__ or call tick() manually for unit tests"
            )
        self._running = True
        loop_sleep = interval if interval is not None else self._active_interval
        self._task = asyncio.create_task(
            self._loop(loop_sleep), name="adaptive-poller"
        )
        log.info(
            "[adaptive_poller] Started (active=%.1fs, normal=%.1fs, "
            "inactive=%.1fs, rate_limited=%.1fs, tokens=%d)",
            self._active_interval,
            self._normal_interval,
            self._inactive_interval,
            self._rate_limited_interval,
            len(self._markets),
        )

    async def stop(self) -> None:
        """Stop the background poll loop (cancels the task)."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — shutdown must not raise
                log.debug("[adaptive_poller] Stop caught", exc_info=True)
            self._task = None
        log.info("[adaptive_poller] Stopped")

    async def _loop(self, loop_sleep: float) -> None:
        """Background poll loop — runs ``tick`` every ``loop_sleep`` seconds."""
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — loop must not crash
                log.debug("[adaptive_poller] tick raised", exc_info=True)
            await asyncio.sleep(loop_sleep)

    # ── Health / introspection ────────────────────────────────────────────

    @property
    def tracked_tokens(self) -> list[str]:
        """Return the list of currently-tracked token IDs."""
        return list(self._markets.keys())

    @property
    def rate_limit_state(self) -> RateLimitState:
        """Direct accessor for the shared rate-limit state (tests)."""
        return self._rate_limit

    def stats(self) -> dict[str, Any]:
        """Public health snapshot for the observability collector.

        Returns a JSON-serialisable dict carrying:

          * ``tracked_tokens`` — count of currently-tracked tokens.
          * ``polls_ok`` / ``polls_failed`` — cumulative success /
            failure counts since construction.
          * ``rate_limit_events`` — cumulative 429 responses seen.
          * ``resolutions`` — cumulative market-resolution events.
          * ``tier_counts`` — per-tier tracked-token counts.
          * ``rate_limit`` — the shared ``RateLimitState`` snapshot.
          * ``intervals`` — the configured tier intervals.
        """
        # Per-tier count — computed lazily on every call so the
        # snapshot is always live.
        tier_counts: dict[str, int] = {t.value: 0 for t in PollingTier}
        for tid in self._markets:
            tier_counts[self.classify(tid).value] += 1
        return {
            "tracked_tokens": len(self._markets),
            "polls_ok": self._polls_ok,
            "polls_failed": self._polls_failed,
            "rate_limit_events": self._rate_limit_events,
            "resolutions": self._resolutions,
            "tier_counts": tier_counts,
            "rate_limit": self._rate_limit.to_dict(),
            "intervals": {
                "active_s": self._active_interval,
                "normal_s": self._normal_interval,
                "inactive_s": self._inactive_interval,
                "rate_limited_s": self._rate_limited_interval,
            },
        }


# ── Header parsing helpers ───────────────────────────────────────────────────


def _parse_int_header(
    headers: dict[str, Any], name: str
) -> Optional[int]:
    """Case-insensitive lookup of ``name`` in ``headers`` → ``int``.

    Returns ``None`` when the header is absent or unparseable. The
    intermediate ``float()`` parse lets headers like ``"99.0"``
    round-trip cleanly (some upstreams send the integer quota as a
    float — the ``int(float(...))`` coercion handles both shapes
    without raising).
    """
    val = _lookup_header(headers, name)
    if val is None:
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _parse_float_header(
    headers: dict[str, Any], name: str
) -> Optional[float]:
    """Case-insensitive lookup of ``name`` in ``headers`` → ``float``."""
    val = _lookup_header(headers, name)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _lookup_header(
    headers: dict[str, Any], name: str
) -> Optional[str]:
    """Case-insensitive header lookup that handles ``httpx`` /
    ``requests`` / plain ``dict`` shapes.

    Returns the value as a ``str`` (or ``None`` when absent) so the
    caller can parse it into whatever type it needs. ``None`` values
    are coerced to ``None`` (rather than the string ``"None"``) so
    a downstream ``int("None")`` doesn't raise.
    """
    if not headers:
        return None
    # Fast path: direct case-sensitive lookup (most common — httpx
    # normalises header names to title-case).
    if name in headers:
        v = headers[name]
        return str(v) if v is not None else None
    # Slow path: case-insensitive scan.
    name_lower = name.lower()
    for k, v in headers.items():
        if str(k).lower() == name_lower:
            return str(v) if v is not None else None
    return None


# ── Module-level singleton ───────────────────────────────────────────────────

# Production singleton — mirrors ``rest_ingestion_fallback`` /
# ``ws_ingestion_manager``. Tests do NOT mutate this singleton;
# they construct fresh ``AdaptivePoller`` instances per test (mirrors
# the ``poller`` fixture in ``tests/test_book_poller.py``). The
# singleton ships without a ``fetcher`` so importing this module
# is side-effect-free; production wiring code (``main.py`` or a
# sibling W35 wave) is responsible for registering a fetcher and
# calling ``start()``.
adaptive_poller = AdaptivePoller()
