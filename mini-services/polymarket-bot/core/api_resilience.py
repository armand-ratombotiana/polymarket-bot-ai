"""API resilience layer — handles external API failures gracefully.

Features
~~~~~~~~

1. **Retry with exponential backoff** — 3 attempts at 100 ms / 500 ms /
   2 000 ms. Backed by ``asyncio.sleep`` so a saturated event loop
   cooperatively yields between attempts.
2. **Circuit breaker integration** — the layer tracks per-API
   ``consecutive_failures`` and trips its own internal breaker after
   ``_failure_threshold`` (default 5) consecutive logical-call
   failures. While tripped, every subsequent call returns the
   ``fallback_data`` immediately without burning a network round-trip
   — same fail-fast contract as ``core.circuit_breaker.CircuitBreaker``
   but layered on top of it so a logical call (which may itself do
   internal retries via ``ClobClient._get``) only counts once toward
   the threshold.
3. **Timeout enforcement** — every attempt is wrapped in
   ``asyncio.wait_for`` (default 5 s). A hung TCP connection can no
   longer block the poller indefinitely.
4. **Fallback to cached data** — when every retry fails (or the
   breaker is open), the layer returns the caller-supplied
   ``fallback_data`` instead of raising. This is the load-bearing
   primitive for graceful degradation: callers pass their stale
   cache as ``fallback_data`` and the system continues serving stale
   data instead of crashing.
5. **Health status tracking per API** — every call records
   ``last_success`` / ``last_failure`` / ``consecutive_failures`` /
   ``total_calls`` / ``total_failures`` / ``total_timeouts`` /
   ``avg_latency_ms`` / ``last_error`` for the named API. Surfaced
   via ``get_health()`` and the ``GET /api/api-health`` endpoint so
   an operator can see at a glance which external API is degraded.
6. **Graceful degradation** — the layer never raises for calls that
   supply ``fallback_data``; the only failure mode that propagates
   is when the caller explicitly opts out of the fallback by passing
   ``fallback_data=None`` (the default), in which case a
   ``ConnectionError`` is raised after the final retry fails. This
   makes the contract explicit: callers that want graceful
   degradation pass a cached value; callers that want hard failure
   leave it at ``None``.

Layered with ``core.circuit_breaker``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The W13-2 ``CircuitBreaker`` instances (``clob_breaker``,
``gamma_breaker``, ``websocket_breaker``) wrap individual HTTP
transports inside ``ClobClient._get`` / ``GammaClient._get`` — they
fail-fast when the underlying transport is in a sustained failure
run. This module wraps the OUTER logical call (e.g.
``ClobClient.get_order_book(token_id)``) so:

* three retries with backoff happen before the layer gives up;
* after the layer gives up, the cached fallback is returned (so the
  poller keeps running on stale data instead of crashing);
* per-API health is aggregated at the logical-call level (one entry
  per logical call, not one per HTTP request).

The two layers are complementary: the inner breaker short-circuits
the HTTP transport (no socket opened), the outer layer short-circuits
the logical call (no retries burnt) and supplies the cached fallback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class APIStatus(Enum):
    """Per-API health status, derived from the rolling failure history."""

    HEALTHY = "healthy"        # All recent calls succeeded
    DEGRADED = "degraded"      # Some failures, still operational
    UNHEALTHY = "unhealthy"    # Circuit breaker open — fail-fast path
    UNKNOWN = "unknown"        # No calls observed yet (fresh boot)


@dataclass
class APIHealth:
    """Mutable per-API health record kept by ``APIResilienceLayer``."""

    name: str
    status: APIStatus = APIStatus.UNKNOWN
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_timeouts: int = 0
    avg_latency_ms: float = 0.0
    last_error: str = ""


class APIResilienceLayer:
    """Wraps external API calls with retry, timeout, and circuit breaker.

    The layer is intentionally stateless across process restarts — every
    counter is in-memory. A restart zeroes the per-API health, which is
    the correct contract for a "last N minutes" operational view (the
    dashboard can derive "API has been unhealthy since reboot" from the
    ``last_success`` / ``last_failure`` timestamps).
    """

    def __init__(self) -> None:
        self._health: dict[str, APIHealth] = {}
        self._max_retries = 3
        self._timeout = 5.0
        # Exponential backoff schedule — indexed by attempt number
        # (``self._backoff[attempt]`` is the sleep BEFORE the next
        # attempt, so ``self._backoff[0]`` = 100 ms is the sleep between
        # attempt 0 and attempt 1).
        self._backoff: list[float] = [0.1, 0.5, 2.0]
        self._failure_threshold = 5  # Circuit breaker threshold

    async def call_with_resilience(
        self,
        api_name: str,
        call_fn: Callable[[], Any],
        fallback_data: Any = None,
    ) -> Any:
        """Call an external API with full resilience.

        Args:
            api_name: Name of the API (e.g., ``"clob"``, ``"gamma"``).
                Used to scope the per-API health record and the
                circuit-breaker counter. The first call for a given
                ``api_name`` lazily creates the health record.
            call_fn: Async callable that makes the API request. Invoked
                with no arguments; the callable is responsible for
                closing over its own request params (path, query, body,
                auth headers). The callable MUST be a coroutine function
                — a plain ``def`` returning a coroutine is accepted too
                because ``await`` works on any awaitable, but a plain
                sync callable will hang the event loop indefinitely
                (``asyncio.wait_for`` only times out async work).
            fallback_data: Data to return if every retry fails (or the
                circuit breaker is open). When ``None`` (the default),
                the layer raises ``ConnectionError`` after the final
                retry fails — opt out of graceful degradation by
                leaving the default. When set (a cached value, an
                empty list, a sentinel object — anything truthy or
                even ``0`` / ``""`` / ``[]``), the layer returns it
                instead of raising.

        Returns:
            The API response on success, or ``fallback_data`` if every
            retry failed (and ``fallback_data`` was supplied). When
            ``fallback_data is None`` AND every retry failed, raises
            ``ConnectionError``.
        """
        health = self._get_or_create_health(api_name)

        # Circuit-breaker short-circuit. Trip after
        # ``_failure_threshold`` consecutive logical-call failures.
        # While tripped, return ``fallback_data`` immediately and skip
        # the retry / backoff / timeout overhead — the API is already
        # known to be down, burning another 3 attempts × 5 s timeout
        # would only delay the fallback.
        if health.consecutive_failures >= self._failure_threshold:
            health.status = APIStatus.UNHEALTHY
            logger.warning(
                "%s circuit breaker open — using fallback", api_name,
            )
            return fallback_data

        # Attempt with retries. The last attempt does NOT sleep
        # afterwards — ``range(self._max_retries)`` runs 0, 1, 2 and
        # the sleep is gated by ``attempt < self._max_retries - 1``.
        last_error: str | None = None
        for attempt in range(self._max_retries):
            try:
                start = time.time()

                # ``asyncio.wait_for`` raises ``asyncio.TimeoutError``
                # when the inner awaitable doesn't resolve within
                # ``self._timeout``. The inner awaitable's cancellation
                # is propagated (so an ``httpx.AsyncClient.get`` whose
                # socket is hung gets ``CancelledError`` raised inside
                # its read loop, freeing the FD).
                result = await asyncio.wait_for(
                    call_fn(), timeout=self._timeout,
                )

                # Success — record and return. Latency is measured
                # across the full ``call_fn`` invocation (including any
                # auth / retry / circuit-breaker logic inside the
                # caller's closure).
                latency = (time.time() - start) * 1000
                self._record_success(api_name, latency)
                return result

            except asyncio.TimeoutError:
                # TimeoutError is a subclass of Exception in Python
                # 3.11+; the dedicated branch keeps the
                # ``total_timeouts`` counter accurate (a generic
                # ``except Exception`` would lump timeouts in with
                # HTTP errors, hiding the dominant failure mode from
                # the operator dashboard).
                health.total_timeouts += 1
                last_error = f"Timeout after {self._timeout}s"
                logger.warning(
                    "%s timeout (attempt %d/%d)",
                    api_name, attempt + 1, self._max_retries,
                )

            except Exception as e:  # noqa: BLE001 — broad on purpose
                # Any non-timeout exception (HTTP status error,
                # connection refused, JSON decode error, the inner
                # ``CircuitBreakerOpenError`` raised by
                # ``ClobClient._get`` …). Recorded as ``last_error``
                # verbatim so the dashboard can surface the upstream
                # message.
                last_error = str(e) or e.__class__.__name__
                logger.warning(
                    "%s error (attempt %d/%d): %s",
                    api_name, attempt + 1, self._max_retries, e,
                )

            # Backoff before the next retry. The final attempt does
            # NOT sleep — ``self._backoff`` is indexed by attempt
            # number, and we only sleep when there's another attempt
            # queued. ``self._backoff`` has 3 entries (100 ms / 500 ms
            # / 2 000 ms) and ``self._max_retries`` is 3, so the last
            # sleep we'd reach for would be at index 2 (2 000 ms)
            # AFTER attempt 2 — which never happens because attempt 2
            # is the last.
            if attempt < self._max_retries - 1:
                await asyncio.sleep(self._backoff[attempt])

        # All retries failed. Record ONE logical-call failure against
        # the per-API health (not ``self._max_retries`` failures — the
        # internal retries of one logical call should not burn the
        # breaker threshold 3x as fast).
        self._record_failure(api_name, last_error or "unknown error")

        if fallback_data is not None:
            logger.warning(
                "%s all retries failed — using fallback data", api_name,
            )
            return fallback_data

        raise ConnectionError(
            f"{api_name} failed after {self._max_retries} attempts: {last_error}"
        )

    def _get_or_create_health(self, name: str) -> APIHealth:
        """Return the per-API health record, creating it on first access."""
        if name not in self._health:
            self._health[name] = APIHealth(name=name)
        return self._health[name]

    def _record_success(self, name: str, latency_ms: float) -> None:
        """Record a successful logical call and reset the failure counter."""
        health = self._get_or_create_health(name)
        health.last_success = time.time()
        health.consecutive_failures = 0
        health.total_calls += 1
        health.status = APIStatus.HEALTHY

        # Exponential moving average latency. ``alpha=0.1`` smooths
        # the metric so a single slow request doesn't dominate the
        # rolling average; the first measurement seeds the EMA
        # rather than being averaged against zero (which would
        # understate the true latency for the first ~10 calls).
        if health.avg_latency_ms == 0.0:
            health.avg_latency_ms = latency_ms
        else:
            health.avg_latency_ms = (
                0.9 * health.avg_latency_ms + 0.1 * latency_ms
            )

    def _record_failure(self, name: str, error: str) -> None:
        """Record a failed logical call and update the per-API status."""
        health = self._get_or_create_health(name)
        health.last_failure = time.time()
        health.consecutive_failures += 1
        health.total_calls += 1
        health.total_failures += 1
        health.last_error = error

        # Status derivation mirrors the breaker threshold: at
        # ``_failure_threshold`` consecutive failures the status
        # flips to UNHEALTHY (the next ``call_with_resilience`` will
        # short-circuit to fallback). Between 2 and threshold-1
        # failures the status is DEGRADED — the API is intermittently
        # failing but still operational.
        if health.consecutive_failures >= self._failure_threshold:
            health.status = APIStatus.UNHEALTHY
            logger.error("%s circuit breaker tripped: %s", name, error)
        elif health.consecutive_failures >= 2:
            health.status = APIStatus.DEGRADED

    def get_health(self) -> dict:
        """Get health status of all tracked APIs.

        Returns a dict keyed by API name; each value is a plain dict
        carrying every documented ``APIHealth`` field (with the
        ``status`` enum coerced to its string value so the JSON
        response from ``GET /api/api-health`` is directly
        serialisable).
        """
        return {
            name: {
                "status": h.status.value,
                "last_success": h.last_success,
                "last_failure": h.last_failure,
                "consecutive_failures": h.consecutive_failures,
                "total_calls": h.total_calls,
                "total_failures": h.total_failures,
                "total_timeouts": h.total_timeouts,
                "avg_latency_ms": h.avg_latency_ms,
                "last_error": h.last_error,
            }
            for name, h in self._health.items()
        }

    def is_healthy(self, api_name: str) -> bool:
        """Return ``True`` only when the named API's status is ``HEALTHY``.

        ``UNKNOWN`` (no calls observed yet) returns ``False`` — a
        fresh-boot API is not "healthy" until proven so by a
        successful call. This matches the operator-dashboard contract
        ("green only when confirmed up") and avoids the false-positive
        where a never-called API would show as healthy on first load.
        """
        health = self._health.get(api_name)
        return health is not None and health.status == APIStatus.HEALTHY

    def reset(self) -> None:
        """Clear every per-API health record.

        Used by tests to guarantee a clean baseline before each
        assertion. NOT exposed via HTTP — a production operator
        should never be able to silently zero the failure counters
        (that would mask a real outage from the dashboard).
        """
        self._health.clear()


# Module-level singleton. Imported by ``core.clob_client``,
# ``core.gamma_client`` and ``api.server`` so every call site shares
# the same per-API health counters and the same circuit-breaker state.
api_resilience = APIResilienceLayer()
