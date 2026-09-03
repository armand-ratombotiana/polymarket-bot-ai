"""Circuit breaker pattern for external API calls.

Prevents cascading failures by tripping (opening) when failure rate exceeds
a threshold, then half-opening to test if the service has recovered.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Requests fail fast (no call to external service)
- HALF_OPEN: Limited test requests allowed to probe recovery

Usage:
    breaker = CircuitBreaker(name="clob_api", failure_threshold=5, recovery_timeout=30)

    @breaker
    def call_clob():
        return clob_client.get_order_book(token_id)

    # Or manual:
    if breaker.can_execute():
        try:
            result = external_call()
            breaker.record_success()
        except Exception:
            breaker.record_failure()

The decorator transparently handles both synchronous and ``async`` callables
— when applied to a coroutine function (``async def``), the wrapper is
itself an ``async def`` and ``await``s the wrapped call before recording
the outcome. This is necessary because the polymarket-bot API clients
(``core.clob_client.ClobClient``, ``core.gamma_client.GammaClient``) are
async; wrapping an async function with the spec's original sync wrapper
would call ``record_success`` BEFORE the coroutine actually resolved (the
sync wrapper would simply return the un-awaited coroutine), masking every
failure from the breaker. The dual sync/async implementation below keeps
the documented decorator usage working for both call styles.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5       # Trip after N consecutive failures
    recovery_timeout: float = 30.0   # Seconds before trying half-open
    half_open_max_calls: int = 3    # Max test calls in half-open
    success_threshold: int = 2      # Successes needed to close from half-open
    timeout: float = 10.0           # Request timeout (s)


class CircuitBreaker:
    """Thread-safe circuit breaker."""

    def __init__(self, name: str, config: CircuitBreakerConfig = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change: float = time.time()
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed
                if self._last_failure_time and \
                   time.time() - self._last_failure_time > self.config.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
            return self._state

    def can_execute(self) -> bool:
        """Check if a request can be made."""
        state = self.state  # Triggers open→half_open check
        with self._lock:
            if state == CircuitState.CLOSED:
                return True
            elif state == CircuitState.OPEN:
                return False
            elif state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

    def record_success(self):
        """Record a successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.OPEN:
                self._transition_to(CircuitState.HALF_OPEN)
                self._success_count = 1
            else:
                self._failure_count = 0

    def record_failure(self, exception: Optional[Exception] = None):
        """Record a failed call."""
        with self._lock:
            self._last_failure_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                self._failure_count += 1
                self._transition_to(CircuitState.OPEN)
                logger.warning("Circuit '%s' OPENED (half-open failure)", self.name)
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                    logger.warning(
                        "Circuit '%s' OPENED (threshold %d reached)",
                        self.name, self.config.failure_threshold,
                    )

    def _transition_to(self, new_state: CircuitState):
        old = self._state
        self._state = new_state
        self._last_state_change = time.time()
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._success_count = 0
        logger.info("Circuit '%s': %s -> %s", self.name, old.value, new_state.value)

    def reset(self):
        """Force-close the circuit."""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)

    def status(self) -> dict:
        """Get current status for monitoring."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.config.failure_threshold,
            "recovery_timeout": self.config.recovery_timeout,
            "last_failure_time": self._last_failure_time,
            "last_state_change": self._last_state_change,
        }

    def __call__(self, func: Callable) -> Callable:
        """Decorator that wraps a function with circuit breaker logic.

        Transparently supports both ``def`` and ``async def`` callables —
        when ``func`` is a coroutine function, the returned wrapper is
        itself a coroutine function that ``await``s ``func`` so
        ``record_success`` / ``record_failure`` reflect the actual outcome
        of the awaited call (not the construction of the coroutine).
        """
        if asyncio.iscoroutinefunction(func):
            async def async_wrapper(*args, **kwargs) -> Any:
                if not self.can_execute():
                    raise CircuitBreakerOpenError(
                        f"Circuit '{self.name}' is OPEN"
                    )
                try:
                    result = await func(*args, **kwargs)
                    self.record_success()
                    return result
                except Exception as e:
                    self.record_failure(e)
                    raise
            async_wrapper.__wrapped__ = func  # type: ignore[attr-defined]
            async_wrapper.__name__ = getattr(func, "__name__", "async_wrapper")
            return async_wrapper

        def wrapper(*args, **kwargs) -> Any:
            if not self.can_execute():
                raise CircuitBreakerOpenError(f"Circuit '{self.name}' is OPEN")
            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure(e)
                raise
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(func, "__name__", "wrapper")
        return wrapper


class CircuitBreakerOpenError(Exception):
    """Raised when the circuit breaker is open."""
    pass


# Pre-configured breakers for external services
clob_breaker = CircuitBreaker("clob_api", CircuitBreakerConfig(
    failure_threshold=5, recovery_timeout=30, timeout=10
))

gamma_breaker = CircuitBreaker("gamma_api", CircuitBreakerConfig(
    failure_threshold=3, recovery_timeout=60, timeout=15
))

websocket_breaker = CircuitBreaker("polymarket_ws", CircuitBreakerConfig(
    failure_threshold=5, recovery_timeout=15, timeout=5
))

# Registry
_all_breakers = {
    "clob_api": clob_breaker,
    "gamma_api": gamma_breaker,
    "polymarket_ws": websocket_breaker,
}


def get_all_breaker_status() -> list[dict]:
    return [b.status() for b in _all_breakers.values()]


def get_breaker(name: str) -> Optional[CircuitBreaker]:
    return _all_breakers.get(name)
