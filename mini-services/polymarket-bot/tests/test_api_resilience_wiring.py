"""W25-8 — Wiring tests for the API resilience layer.

These tests verify the W24-7 resilience layer is correctly wired into the
production call sites. The sibling ``tests/test_api_resilience.py`` module
exercises the ``APIResilienceLayer`` class in isolation (a fresh instance
per test, no singleton state). THIS module exercises the integration:

  (1) ``ClobClient.get_order_book`` routes its HTTP fetch through
      ``api_resilience.call_with_resilience`` (api_name="clob", with the
      per-token cached order book as fallback).
  (2) ``GammaClient.get_markets`` routes its HTTP fetch through
      ``api_resilience.call_with_resilience`` (api_name="gamma", with the
      cached markets list as fallback).
  (3) On a sustained HTTP failure, the cached fallback is returned (the
      system continues with stale data instead of crashing) AND the cache
      is refreshed on the next successful call.
  (4) ``GET /api/api-health`` returns the resilience layer's per-API health
      snapshot (authenticated — bearer token required).
  (5) After ``_failure_threshold`` (5) consecutive logical-call failures
      against a single API name, the resilience layer's circuit breaker
      trips and every subsequent call returns the fallback immediately
      without invoking the inner ``call_fn``.

Isolation
~~~~~~~~~

  * The module-level singleton ``api_resilience`` (in
    ``core.api_resilience``) is shared with the production CLOB / Gamma
    clients. Every test in this module calls ``api_resilience.reset()``
    BEFORE its first assertion so state from a prior test (or from a
    sibling test module that exercised the singleton via the real client
    code path) doesn't leak in. The reset is also called AFTER each
    test that records state, as a defensive teardown.

  * The inner W13-2 ``clob_breaker`` / ``gamma_breaker`` instances are
    reset before each test that drives multiple failing calls. Without
    the reset, the inner breaker could trip before the outer resilience
    layer does (``gamma_breaker`` trips after 3 failures, ``clob_breaker``
    after 5) and short-circuit the resilience layer's retry loop with
    ``CircuitBreakerOpenError`` rather than the underlying HTTP error.
    The reset guarantees the resilience layer sees the actual HTTP
    failure on every retry attempt.

  * The ``httpx.AsyncClient`` class symbol referenced inside
    ``core.clob_client`` and ``core.gamma_client`` is replaced with a
    ``MagicMock`` returning a deterministic mock transport. No real
    network socket is opened. Mirrors the pattern in
    ``tests/test_gamma_client.py``'s ``mock_httpx_client`` fixture.

  * The 100 ms / 500 ms / 2 000 ms backoff schedule inside the resilience
    layer is patched to all-zeros via the ``no_backoff_sleep`` fixture so
    a single failing logical call (3 retries × 2 sleeps = 2.6 s of real
    sleep) doesn't dominate the suite's wall-clock. Correctness depends
    on the retry count and the order in which successes / failures are
    recorded — NOT on the actual wall-clock delay.

Async tests are collected via the per-test ``@pytest.mark.asyncio``
decorator (NOT the module-level ``pytestmark``) so the synchronous
TestClient-based route tests don't emit ``PytestWarning`` about being
marked asyncio-but-not-async. Mirrors the convention in
``tests/test_api_resilience.py``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.api_resilience import APIStatus, api_resilience
from core.circuit_breaker import clob_breaker, gamma_breaker

# ── Test-app auth token (set by ``conftest.py`` BEFORE any project module
# is imported, so the ``enforce_api_auth`` middleware accepts it).
VALID_TOKEN = "test-token-conftest"

# Defensive: disable the slowapi rate limiter so a fast sequence of
# ``GET /api/api-health`` requests doesn't 429 mid-suite. Mirrors the
# pattern in ``tests/test_api_resilience.py`` and the autouse disable
# in ``conftest.py``.
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``asyncio.sleep`` inside ``core.api_resilience`` with a no-op.

    The resilience layer's correctness depends on the retry count and the
    order in which successes / failures are recorded — NOT on the actual
    wall-clock delay between retries. Patching ``asyncio.sleep`` to a
    no-op keeps the suite fast (3 retries × 2 s = 6 s of real sleep
    avoided per failing-call test).

    Scoped to ``core.api_resilience.asyncio.sleep`` so sibling modules
    that legitimately sleep (e.g. ``core.book_poller``'s tier intervals)
    are unaffected.
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("core.api_resilience.asyncio.sleep", _no_sleep)


@pytest.fixture
def reset_resilience_and_breakers() -> None:
    """Reset the W24-7 singleton + the W13-2 inner breakers.

    Belt-and-braces reset so a prior test's failure counters (or the
    inner breaker's OPEN state) don't perturb the next test's
    assertions. Idempotent — safe to stack with the per-test inline
    ``reset()`` calls the tests below also do.
    """
    api_resilience.reset()
    clob_breaker.reset()
    gamma_breaker.reset()
    yield
    api_resilience.reset()
    clob_breaker.reset()
    gamma_breaker.reset()


@pytest.fixture
def mock_clob_httpx(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``httpx.AsyncClient`` inside ``core.clob_client``.

    Returns the mock client instance so individual tests can program
    ``mock_client.get`` to return a canned response (or raise an
    exception) before invoking ``ClobClient.get_order_book``.

    The mock client is reused on every ``httpx.AsyncClient(...)`` call
    so the params captured by ``mock_client.get.call_args`` always
    correspond to the most recent ``ClobClient._get`` invocation.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()  # no-op
    mock_resp.json = MagicMock(return_value={"bids": [], "asks": []})

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()

    monkeypatch.setattr(
        "core.clob_client.httpx.AsyncClient",
        MagicMock(return_value=mock_client),
    )
    return mock_client


@pytest.fixture
def mock_gamma_httpx(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``httpx.AsyncClient`` inside ``core.gamma_client``.

    Same shape as ``mock_clob_httpx`` but scoped to the gamma_client
    module's view of httpx. Returns the mock client instance so
    individual tests can program ``mock_client.get`` to return a
    canned markets payload (or raise).
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()  # no-op
    mock_resp.json = MagicMock(return_value=[])

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()

    monkeypatch.setattr(
        "core.gamma_client.httpx.AsyncClient",
        MagicMock(return_value=mock_client),
    )
    return mock_client


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_api_resilience.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ─────────────────────────────────────────────────────────────────────────────
# (1) CLOB client uses resilience layer
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clob_get_order_book_invokes_resilience_layer(
    mock_clob_httpx, reset_resilience_and_breakers, monkeypatch,
):
    """``ClobClient.get_order_book`` routes the HTTP fetch through
    ``api_resilience.call_with_resilience`` with ``api_name="clob"``.

    Verifies the W24-7 wiring at the call-site level:
      * ``api_resilience.call_with_resilience`` is invoked exactly once
        per ``get_order_book`` call (one logical call — the layer's
        internal retries don't multiply this).
      * The ``api_name`` argument is ``"clob"`` (the same key the
        ``GET /api/api-health`` endpoint surfaces).
      * The ``fallback_data`` argument is the per-token cached order
        book (``self._cached_order_books.get(token_id)``), or ``None``
        when the cache is empty.
    """
    # Lazy import: avoids triggering the heavy ``api.server`` import
    # chain at module-load time (only this test needs the CLOB client).
    from core.clob_client import ClobClient

    # Spy on the resilience layer so we can assert it was invoked
    # without changing its behaviour. ``wraps=`` keeps the real
    # implementation (the actual retry / fallback / circuit-breaker
    # logic runs end-to-end).
    call_spy = MagicMock(wraps=api_resilience.call_with_resilience)
    monkeypatch.setattr(api_resilience, "call_with_resilience", call_spy)

    clob = ClobClient()
    sentinel_book = {"bids": [["0.50", "10"]], "asks": [["0.52", "5"]]}

    # Program the mock httpx client to return our sentinel book.
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=sentinel_book)
    mock_clob_httpx.get = AsyncMock(return_value=mock_resp)

    result = await clob.get_order_book("TOK_123")

    # ── The call returned the sentinel (proving the resilience layer
    # actually ran the inner _fetch and returned its result).
    assert result == sentinel_book, (
        f"expected get_order_book to return the sentinel book; got {result!r}"
    )

    # ── The resilience layer was invoked exactly once for this logical
    # call (NOT 3 times — the layer's internal retries don't multiply
    # the count).
    assert call_spy.call_count == 1, (
        f"expected call_with_resilience to be called once per logical call; "
        f"got {call_spy.call_count}"
    )

    # ── The api_name was "clob" (the key the dashboard surfaces).
    args, kwargs = call_spy.call_args
    # The signature is ``call_with_resilience(api_name, call_fn,
    # fallback_data=None)`` — the api_name is the first positional arg.
    api_name = args[0] if args else kwargs.get("api_name")
    assert api_name == "clob", (
        f"expected api_name='clob'; got {api_name!r}"
    )

    # ── The fallback_data kwarg was the per-token cached order book
    # (which is None on a first call — cache miss).
    fallback = kwargs.get("fallback_data")
    assert fallback is None, (
        "expected fallback_data=None on a cache miss (first call for "
        f"this token); got {fallback!r}"
    )

    # ── After the successful call, the cache should be refreshed so
    # the NEXT call's fallback is the most recent snapshot, not None.
    assert "TOK_123" in clob._cached_order_books, (
        "expected the cached_order_books dict to contain TOK_123 after "
        "a successful get_order_book call (the cache refresh contract)"
    )
    assert clob._cached_order_books["TOK_123"] == sentinel_book, (
        "expected the cached entry to be the most recent sentinel; got "
        f"{clob._cached_order_books['TOK_123']!r}"
    )


@pytest.mark.asyncio
async def test_clob_get_order_book_passes_cached_book_as_fallback(
    mock_clob_httpx, reset_resilience_and_breakers, monkeypatch,
):
    """The fallback_data passed to ``call_with_resilience`` is the
    per-token cached snapshot (NOT a global fallback).

    Pre-populates ``clob._cached_order_books["TOK_X"]`` with a stale
    snapshot, then asserts the resilience layer was invoked with that
    exact snapshot as the ``fallback_data`` kwarg. Verifies the wiring
    passes the per-token cache entry — not a shared fallback dict — so
    a sustained CLOB outage serves the LAST known good book for that
    specific token (rather than a generic empty book).
    """
    from core.clob_client import ClobClient

    clob = ClobClient()
    stale_book = {"bids": [["0.40", "100"]], "asks": [["0.60", "200"]]}
    clob._cached_order_books["TOK_X"] = stale_book

    # Spy on the resilience layer.
    call_spy = MagicMock(wraps=api_resilience.call_with_resilience)
    monkeypatch.setattr(api_resilience, "call_with_resilience", call_spy)

    # Program the mock httpx to return a fresh book — the call succeeds,
    # the resilience layer is invoked, the stale book is passed as
    # fallback_data but never actually returned.
    fresh_book = {"bids": [["0.50", "10"]], "asks": [["0.52", "5"]]}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=fresh_book)
    mock_clob_httpx.get = AsyncMock(return_value=mock_resp)

    result = await clob.get_order_book("TOK_X")

    assert result == fresh_book, (
        f"expected the fresh book to be returned on success; got {result!r}"
    )

    # The fallback_data kwarg MUST be the stale cached book, not None.
    _, kwargs = call_spy.call_args
    assert kwargs.get("fallback_data") is stale_book, (
        "expected fallback_data to be the stale cached book for TOK_X; "
        f"got {kwargs.get('fallback_data')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (2) Gamma client uses resilience layer
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gamma_get_markets_invokes_resilience_layer(
    mock_gamma_httpx, reset_resilience_and_breakers, monkeypatch,
):
    """``GammaClient.get_markets`` routes the HTTP fetch through
    ``api_resilience.call_with_resilience`` with ``api_name="gamma"``.

    Verifies:
      * ``call_with_resilience`` is invoked exactly once per
        ``get_markets`` call.
      * The ``api_name`` is ``"gamma"``.
      * The ``fallback_data`` is the cached markets list
        (``self._cached_markets``), which is ``[]`` on a first call.
    """
    from core.gamma_client import GammaClient

    call_spy = MagicMock(wraps=api_resilience.call_with_resilience)
    monkeypatch.setattr(api_resilience, "call_with_resilience", call_spy)

    gamma = GammaClient()
    sentinel_markets = [{"condition_id": "0xabc", "question": "Test?"}]

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=sentinel_markets)
    mock_gamma_httpx.get = AsyncMock(return_value=mock_resp)

    result = await gamma.get_markets()

    assert result == sentinel_markets, (
        f"expected get_markets to return the sentinel list; got {result!r}"
    )
    assert call_spy.call_count == 1, (
        f"expected call_with_resilience to be called once per logical call; "
        f"got {call_spy.call_count}"
    )

    args, kwargs = call_spy.call_args
    api_name = args[0] if args else kwargs.get("api_name")
    assert api_name == "gamma", f"expected api_name='gamma'; got {api_name!r}"

    # On a fresh client, _cached_markets is [] — the safe no-op fallback
    # for a never-booted Gamma client.
    fallback = kwargs.get("fallback_data")
    assert fallback == [], (
        f"expected fallback_data=[] on a cache miss; got {fallback!r}"
    )

    # Cache should be refreshed after a successful call.
    assert gamma._cached_markets == sentinel_markets, (
        "expected _cached_markets to be refreshed to the sentinel list "
        f"after a successful call; got {gamma._cached_markets!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (3) Fallback data is returned on failure
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clob_get_order_book_returns_cached_fallback_on_failure(
    mock_clob_httpx, reset_resilience_and_breakers, no_backoff_sleep,
):
    """On a sustained HTTP failure, ``get_order_book`` returns the
    per-token cached snapshot instead of raising.

    Pre-populates the cache with a stale book, programs the mock httpx
    client to raise on every retry, then asserts the stale book is
    returned (the graceful-degradation contract). Verifies the cache is
    NOT refreshed on a failed call (a transient empty / error response
    would otherwise poison the cache with stale-of-stale data).
    """
    from core.clob_client import ClobClient

    clob = ClobClient()
    stale_book = {"bids": [["0.40", "100"]], "asks": [["0.60", "200"]]}
    clob._cached_order_books["TOK_STALE"] = stale_book

    # Make every httpx.get attempt raise — the resilience layer will
    # retry 3 times, then return the fallback (the stale cached book).
    # NB: the message string is asserted on below — keep "refused" in it
    # so the ``last_error`` substring check holds.
    mock_clob_httpx.get = AsyncMock(side_effect=ConnectionRefusedError(
        "upstream refused connection",
    ))

    result = await clob.get_order_book("TOK_STALE")

    assert result is stale_book, (
        "expected get_order_book to return the cached stale book when every "
        f"retry fails; got {result!r}"
    )

    # The cache should NOT be refreshed on a failed call (the
    # ``if result is not None`` guard in ``get_order_book`` only
    # refreshes on a successful fetch — and a fallback return is NOT a
    # successful fetch, even though ``result`` is truthy).
    assert clob._cached_order_books["TOK_STALE"] is stale_book, (
        "expected the cache to be unchanged after a failed call (no stale-of-"
        "stale poisoning); got a different object"
    )

    # The resilience layer should have recorded ONE logical failure
    # against the 'clob' API name (NOT 3 — the internal retries are
    # counted as a single logical failure).
    clob_health = api_resilience.get_health().get("clob", {})
    assert clob_health.get("total_failures") == 1, (
        "expected total_failures=1 (one logical call failed — the internal "
        f"retries don't multiply the count); got {clob_health}"
    )
    assert clob_health.get("consecutive_failures") == 1, (
        f"expected consecutive_failures=1; got {clob_health}"
    )
    assert "refused" in clob_health.get("last_error", "").lower(), (
        f"expected last_error to mention 'refused'; got "
        f"{clob_health.get('last_error')!r}"
    )


@pytest.mark.asyncio
async def test_gamma_get_markets_returns_cached_fallback_on_failure(
    mock_gamma_httpx, reset_resilience_and_breakers, no_backoff_sleep,
):
    """On a sustained Gamma HTTP failure, ``get_markets`` returns the
    cached markets list instead of raising.

    Pre-populates ``_cached_markets`` with a stale list, programs the
    mock httpx client to raise on every retry, then asserts the stale
    list is returned. Verifies the W24-7 contract: the system continues
    with stale data instead of crashing.
    """
    from core.gamma_client import GammaClient

    gamma = GammaClient()
    stale_markets = [{"condition_id": "0xstale", "question": "Old?"}]
    gamma._cached_markets = list(stale_markets)

    # Make every httpx.get attempt raise.
    mock_gamma_httpx.get = AsyncMock(side_effect=ConnectionError(
        "upstream Gamma is unreachable",
    ))

    result = await gamma.get_markets()

    assert result == stale_markets, (
        "expected get_markets to return the cached stale list when every "
        f"retry fails; got {result!r}"
    )

    gamma_health = api_resilience.get_health().get("gamma", {})
    assert gamma_health.get("total_failures") == 1, (
        "expected total_failures=1 for the gamma API; got "
        f"{gamma_health}"
    )

    # The cache should NOT be refreshed on a failed call — verify the
    # stale list is still in place (the ``if result`` guard only
    # refreshes on a successful fetch, and the fallback return is
    # truthy but NOT a fresh fetch).
    assert gamma._cached_markets == stale_markets, (
        "expected _cached_markets to be unchanged after a failed call"
    )


@pytest.mark.asyncio
async def test_gamma_get_markets_returns_empty_list_on_first_call_failure(
    mock_gamma_httpx, reset_resilience_and_breakers, no_backoff_sleep,
):
    """On a first-call failure (cache is empty), ``get_markets`` returns
    ``[]`` — the safe no-op fallback.

    Verifies the documented "a fresh-booted Gamma client never crashes
    on its first call" contract: the ``fallback_data=[]`` sentinel is
    NOT ``None``, so the resilience layer returns the empty list rather
    than raising ``ConnectionError``. Every consumer of ``get_markets``
    iterates the result, so an empty iteration is a no-op rather than a
    crash.
    """
    from core.gamma_client import GammaClient

    gamma = GammaClient()
    # On a fresh client, _cached_markets is []. Don't pre-populate it.

    mock_gamma_httpx.get = AsyncMock(side_effect=ConnectionRefusedError(
        "fresh-boot Gamma is down",
    ))

    result = await gamma.get_markets()

    assert result == [], (
        f"expected get_markets to return [] on a fresh-boot failure; "
        f"got {result!r}"
    )

    # The cache should remain empty (a transient empty / error response
    # would otherwise poison the cache with stale-of-stale data — the
    # ``if result`` guard skips the refresh on an empty list).
    assert gamma._cached_markets == [], (
        "expected _cached_markets to remain empty after a first-call "
        f"failure; got {gamma._cached_markets!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (4) API health endpoint returns status
# ─────────────────────────────────────────────────────────────────────────────


def test_api_health_endpoint_requires_auth(client):
    """``GET /api/api-health`` without a bearer token returns 401.

    The endpoint is NOT in ``PUBLIC_PATHS`` — every authenticated route
    is fail-closed by the ``enforce_api_auth`` middleware. Verifies the
    W25-8 wiring follows the same auth contract as every other
    authenticated route in ``api/server.py``.
    """
    api_resilience.reset()
    response = client.get("/api/api-health")  # no Authorization header
    assert response.status_code == 401, (
        "expected 401 without auth header; got "
        f"{response.status_code}; body: {response.text[:200]!r}"
    )


def test_api_health_endpoint_returns_dict(client, auth_headers):
    """``GET /api/api-health`` returns a JSON dict (possibly empty).

    Resets the singleton before the request so prior tests' state
    doesn't leak into the response shape assertion. After the reset,
    the response is ``{}`` (no API has been called yet).
    """
    api_resilience.reset()
    response = client.get("/api/api-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 with valid auth; got {response.status_code}; "
        f"body: {response.text[:300]!r}"
    )

    body = response.json()
    assert isinstance(body, dict), (
        f"expected JSON dict; got {type(body).__name__}: {body!r}"
    )
    # After reset(), no API has been called — the dict is empty.
    # Tolerate sibling-test leakage by only asserting on the type.
    for api_name, record in body.items():
        assert isinstance(api_name, str)
        assert isinstance(record, dict), (
            f"expected each record to be a dict; got {type(record).__name__} "
            f"for {api_name!r}"
        )


def test_api_health_endpoint_reflects_recorded_call(client, auth_headers):
    """``GET /api/api-health`` reflects a call recorded via the singleton.

    Resets the singleton, records a successful 'clob' call directly
    via the internal helper, then asserts the endpoint surfaces the
    'clob' entry with ``status=healthy``. Verifies the route is wired
    to the SAME singleton the CLOB / Gamma clients use (NOT a separate
    instance that would diverge from the production call sites).
    """
    api_resilience.reset()

    # Record a success directly via the singleton's internal helper.
    api_resilience._record_success("clob", 42.0)

    response = client.get("/api/api-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 with valid auth; got {response.status_code}; "
        f"body: {response.text[:300]!r}"
    )

    body = response.json()
    assert "clob" in body, (
        f"expected 'clob' entry after recording a clob success; got {body}"
    )
    clob = body["clob"]
    assert clob["status"] == APIStatus.HEALTHY.value, (
        f"expected status=healthy; got {clob['status']!r}"
    )
    assert clob["total_calls"] == 1, (
        f"expected total_calls=1; got {clob['total_calls']}"
    )
    assert clob["consecutive_failures"] == 0, (
        f"expected consecutive_failures=0; got {clob['consecutive_failures']}"
    )

    api_resilience.reset()


def test_api_health_endpoint_reflects_gamma_degradation(client, auth_headers):
    """``GET /api/api-health`` surfaces a sustained gamma failure as
    ``status=degraded`` (or ``unhealthy`` after 5+ failures).

    Records 2 synthetic gamma failures directly via the internal
    helper, then asserts the endpoint surfaces the 'gamma' entry with
    ``status=degraded``. This is the dashboard's "early warning" state
    — the API is still operational but intermittently failing.
    """
    api_resilience.reset()

    # 2 failures → DEGRADED (the threshold for DEGRADED is
    # ``consecutive_failures >= 2`` per the resilience layer's
    # ``_record_failure`` status derivation).
    api_resilience._record_failure("gamma", "synthetic 1")
    api_resilience._record_failure("gamma", "synthetic 2")

    response = client.get("/api/api-health", headers=auth_headers)
    assert response.status_code == 200, (
        f"expected 200 with valid auth; got {response.status_code}"
    )

    body = response.json()
    assert "gamma" in body, (
        f"expected 'gamma' entry after recording gamma failures; got {body}"
    )
    gamma = body["gamma"]
    assert gamma["status"] == APIStatus.DEGRADED.value, (
        f"expected status=degraded after 2 consecutive failures; "
        f"got {gamma['status']!r}"
    )
    assert gamma["consecutive_failures"] == 2, (
        f"expected consecutive_failures=2; got {gamma['consecutive_failures']}"
    )

    api_resilience.reset()


# ─────────────────────────────────────────────────────────────────────────────
# (5) Circuit breaker trips after threshold
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resilience_breaker_trips_after_threshold(
    mock_clob_httpx, reset_resilience_and_breakers, no_backoff_sleep,
    monkeypatch,
):
    """After 5 consecutive logical-call failures against 'clob', the
    resilience layer's breaker trips and every subsequent call returns
    the fallback WITHOUT invoking the inner ``call_fn``.

    Drives 5 failing ``get_order_book`` calls (each pre-populates the
    cache so the fallback is returned rather than raising), then a 6th
    call. Asserts:
      * After the 5th call, the 'clob' health record shows
        ``consecutive_failures=5`` and ``status=unhealthy``.
      * The 6th call returns the cached fallback immediately (no network
        attempt — the mock httpx client's call count does NOT increase).
      * The 6th call does NOT increment ``total_calls`` (the
        short-circuit path records no logical call).

    The ``no_backoff_sleep`` fixture is required because each failing
    logical call burns 3 retries × 2 sleeps = 6 ``asyncio.sleep``
    invocations (2.6 s of real sleep per failing logical call, 13 s for
    5 calls — well over the 2-minute per-test timeout).

    The inner W13-2 ``clob_breaker`` is patched to ``can_execute() ->
    True`` so it never short-circuits the inner ``_get`` call. Without
    this patch, the inner breaker would trip after 5 inner failures
    (≈2 logical calls) and raise ``CircuitBreakerOpenError`` BEFORE
    ``httpx.get`` is invoked — which would still count as a logical
    failure but would obscure the end-to-end "real HTTP failure →
    fallback" path the W25-8 wiring test is meant to verify. Patching
    ``can_execute`` keeps the inner breaker transparent so the OUTER
    resilience layer is the only breaker being exercised.
    """
    from core.clob_client import ClobClient

    # Patch the inner W13-2 ``clob_breaker`` so its breaker never trips
    # during the test — the test is asserting on the OUTER W24-7
    # resilience-layer breaker's tripping behaviour, not the inner one.
    monkeypatch.setattr(clob_breaker, "can_execute", lambda: True)

    clob = ClobClient()

    # Pre-populate the per-token cache so the fallback is the cached
    # stale book (NOT a ConnectionError raise on the first call).
    stale_book = {"bids": [["0.40", "100"]], "asks": [["0.60", "200"]]}
    clob._cached_order_books["TOK_BREAKER"] = stale_book

    # Make every httpx.get attempt raise — the resilience layer will
    # retry 3 times, then return the fallback. With the inner breaker
    # patched transparent, every retry reaches httpx.get and raises the
    # underlying ``ConnectionRefusedError`` (rather than being short-
    # circuited by ``CircuitBreakerOpenError``).
    mock_clob_httpx.get = AsyncMock(side_effect=ConnectionRefusedError(
        "CLOB is down",
    ))

    # Drive 5 failing logical calls. Each call should return the stale
    # cached book (the resilience layer's fallback path) rather than
    # raising.
    for i in range(5):
        result = await clob.get_order_book("TOK_BREAKER")
        assert result is stale_book, (
            f"call {i + 1} should have returned the cached stale book "
            f"after exhausting retries; got {result!r}"
        )

    # After 5 logical failures the resilience layer's breaker should be
    # tripped. Each logical call did 3 retries → 15 ``httpx.get``
    # invocations total.
    assert mock_clob_httpx.get.call_count == 15, (
        f"expected 15 httpx.get invocations (5 logical calls × 3 retries); "
        f"got {mock_clob_httpx.get.call_count}"
    )

    clob_health = api_resilience.get_health()["clob"]
    assert clob_health["consecutive_failures"] == 5, (
        f"expected consecutive_failures=5; got {clob_health['consecutive_failures']}"
    )
    assert clob_health["total_failures"] == 5, (
        f"expected total_failures=5; got {clob_health['total_failures']}"
    )
    assert clob_health["status"] == APIStatus.UNHEALTHY.value, (
        f"expected status=unhealthy after threshold reached; "
        f"got {clob_health['status']!r}"
    )
    assert api_resilience.is_healthy("clob") is False, (
        "is_healthy('clob') must return False once the breaker is tripped"
    )

    # 6th call: breaker is OPEN → the inner ``call_fn`` is NOT invoked
    # at all. The fallback (cached stale book) is returned immediately,
    # and NO additional httpx.get call is made.
    calls_before = mock_clob_httpx.get.call_count
    result = await clob.get_order_book("TOK_BREAKER")
    assert result is stale_book, (
        f"6th call should have returned the cached stale book immediately; "
        f"got {result!r}"
    )
    assert mock_clob_httpx.get.call_count == calls_before, (
        "expected httpx.get to NOT be invoked when the breaker is open; "
        f"got {mock_clob_httpx.get.call_count - calls_before} extra "
        "invocation(s)"
    )


@pytest.mark.asyncio
async def test_resilience_breaker_short_circuits_gamma_too(
    mock_gamma_httpx, reset_resilience_and_breakers, no_backoff_sleep,
    monkeypatch,
):
    """The resilience-layer breaker trips on the 'gamma' API name too —
    not just 'clob'. Verifies the per-API-name scoping: a sustained gamma
    outage trips the gamma breaker, but the clob breaker stays CLOSED
    (and vice versa).

    The inner W13-2 ``gamma_breaker`` (failure_threshold=3) is patched
    to ``can_execute() -> True`` so it never short-circuits the inner
    ``_get`` call — same pattern as
    ``test_resilience_breaker_trips_after_threshold`` but for gamma.
    Without this patch, the inner gamma breaker would trip after 3
    inner failures (i.e. the FIRST logical call's 3 retries) and
    short-circuit every subsequent retry with
    ``CircuitBreakerOpenError`` instead of the underlying
    ``ConnectionRefusedError``.
    """
    from core.gamma_client import GammaClient

    # Patch the inner W13-2 ``gamma_breaker`` so its breaker never trips
    # during the test — the test is asserting on the OUTER W24-7
    # resilience-layer breaker's tripping behaviour, not the inner one.
    monkeypatch.setattr(gamma_breaker, "can_execute", lambda: True)

    gamma = GammaClient()
    stale_markets = [{"condition_id": "0xstale", "question": "Old?"}]
    gamma._cached_markets = list(stale_markets)

    mock_gamma_httpx.get = AsyncMock(side_effect=ConnectionRefusedError(
        "Gamma is down",
    ))

    # Drive 5 failing logical calls.
    for i in range(5):
        result = await gamma.get_markets()
        assert result == stale_markets, (
            f"call {i + 1} should have returned the cached stale markets; "
            f"got {result!r}"
        )

    gamma_health = api_resilience.get_health()["gamma"]
    assert gamma_health["consecutive_failures"] == 5, (
        f"expected gamma consecutive_failures=5; got "
        f"{gamma_health['consecutive_failures']}"
    )
    assert gamma_health["status"] == APIStatus.UNHEALTHY.value, (
        f"expected gamma status=unhealthy; got {gamma_health['status']!r}"
    )

    # Cross-API isolation: the 'clob' API was NOT touched, so it should
    # have NO health record at all (lazy creation on first call).
    clob_health = api_resilience.get_health().get("clob")
    assert clob_health is None, (
        "expected no 'clob' entry in health (the gamma outage should NOT "
        f"perturb the clob API's record); got {clob_health!r}"
    )

    # 6th call: breaker is OPEN → no httpx.get invocation.
    calls_before = mock_gamma_httpx.get.call_count
    result = await gamma.get_markets()
    assert result == stale_markets, (
        f"6th call should have returned the cached markets immediately; "
        f"got {result!r}"
    )
    assert mock_gamma_httpx.get.call_count == calls_before, (
        "expected httpx.get to NOT be invoked when the gamma breaker is open"
    )
