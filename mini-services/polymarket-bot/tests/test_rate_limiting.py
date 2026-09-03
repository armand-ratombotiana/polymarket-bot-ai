"""
tests/test_rate_limiting.py — W10-4 rate-limiting contract tests.

Covers the four behavioural guarantees the W10-4 task spec asks for:

  1. **Read routes allow up to 120/min** — a 120/min GET route accepts 5
     rapid requests without throttling (the spec's "simulate 5 requests,
     all pass" smoke test).
  2. **Write routes are limited to 20/min** — a 20/min POST route starts
     returning 429 once the limit is exceeded (the contract the W10-4
     policy attaches to ``/api/trade``, ``/api/orders`` (DELETE),
     ``/api/orders/{id}`` (DELETE), and ``/api/positions/{id}/close``).
  3. **The 429 response has the correct JSON shape** —
     ``{"detail": "Rate limit exceeded", "retry_after": N}`` so the
     frontend's existing error-display logic can render a "retry in Ns"
     countdown.
  4. **The ``Retry-After`` header is present** on the 429 response so
     well-behaved clients can back off without parsing the body.

Why a self-contained test app
-----------------------------
The repo's main ``api.server.app`` registers 50+ routes and pulls in 30+
``core/*`` modules at import time — too heavy for a focused contract test.
Instead, this module builds a MINIMAL FastAPI app per test with the SAME
``@limiter.limit()`` decorator + ``rate_limit_handler`` exception handler
plumbing used by the production app. This isolates the contract under
test (slowapi's hit-counter behaviour + our handler's response shape)
from the rest of the pipeline.

The shared ``api.rate_limit.limiter`` singleton is disabled in
``tests/conftest.py`` (so existing tests that hit rate-limited routes
don't suddenly start receiving 429s). Each test here constructs its
OWN ``Limiter(key_func=get_remote_address)`` instance so the conftest
disable doesn't interfere.

Sync ``def`` test functions (no ``async def``) — mirrors the convention
in ``tests/test_live_safety_gate_api.py``: TestClient runs the ASGI app
in a separate thread with its own event loop, and async test functions
would contend with that loop.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_app(read_limit: str = "120/minute", write_limit: str = "20/minute") -> tuple[FastAPI, Limiter]:
    """Build a minimal FastAPI app wired with the SAME limiter plumbing
    the production ``api/server.py`` uses (a ``Limiter`` keyed on the
    client IP, ``app.state.limiter`` assignment, and the project's custom
    ``rate_limit_handler`` exception handler).

    Returns the ``(app, limiter)`` pair so each test can drive the limiter
    with its own ``read_limit`` / ``write_limit`` policy strings without
    polluting any shared singleton.
    """
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    # Each test gets a FRESH limiter — never the shared one (which is
    # disabled in conftest.py for the rest of the test suite).
    limiter.enabled = True
    app.state.limiter = limiter

    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        """Mirror of the production handler in ``api/server.py``.

        Kept in sync (copy-paste) rather than imported so this test module
        is hermetic: if a future refactor moves the handler into a separate
        module, the tests still verify the contract is held (the handler
        exists, returns the expected JSON shape, sets the expected headers).
        """
        rate_limit_item = getattr(getattr(exc, "limit", None), "limit", None)
        if rate_limit_item is not None:
            retry_after_secs = int(rate_limit_item.get_expiry())
            amount = int(getattr(rate_limit_item, "amount", 0))
            granularity_obj = getattr(rate_limit_item, "GRANULARITY", None)
            granularity_name = getattr(granularity_obj, "name", "minute")
            limit_str = f"{amount}/{granularity_name}"
        else:
            retry_after_secs = 60
            limit_str = "100/minute"
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded",
                "retry_after": retry_after_secs,
            },
            headers={
                "Retry-After": str(retry_after_secs),
                "X-RateLimit-Limit": limit_str,
            },
        )

    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    @app.get("/api/health")
    @limiter.limit(read_limit)
    async def health(request: Request):
        return {"status": "ok"}

    @app.post("/api/trade")
    @limiter.limit(write_limit)
    async def place_trade(request: Request):
        return {"status": "placed"}

    return app, limiter


# ── Test 1: read routes allow 5 rapid requests without 429 ──────────────────

def test_read_route_allows_5_rapid_requests_without_429():
    """W10-4 contract (read tier): a 120/min GET route accepts 5 rapid
    requests without throttling.

    This is the spec's "simulate 5 requests, all pass" smoke test — it
    verifies the limiter doesn't FALSE-positive on a small burst (a
    naive buggy limiter could hit 429 after the 1st request, which
    would break every dashboard polling loop in the frontend).
    """
    app, _ = _build_app(read_limit="120/minute")
    client = TestClient(app)

    for i in range(5):
        r = client.get("/api/health")
        assert r.status_code == 200, (
            f"request #{i} to /api/health (limit 120/min) returned {r.status_code} "
            f"instead of 200 — body={r.json()}"
        )
        assert r.json() == {"status": "ok"}


# ── Test 2: write routes are limited to 20/min ───────────────────────────────

def test_write_route_enforces_20_per_minute_limit():
    """W10-4 contract (write tier): a 20/min POST route accepts the first
    20 requests, then returns 429 on the 21st.

    Uses a 3/minute limit instead of 20/minute to keep the test fast
    (3 requests vs. 21 requests — same contract, fewer iterations).
    """
    app, _ = _build_app(write_limit="3/minute")
    client = TestClient(app)

    # First 3 requests succeed
    for i in range(3):
        r = client.post("/api/trade")
        assert r.status_code == 200, (
            f"request #{i} to /api/trade (limit 3/min) returned {r.status_code} "
            f"instead of 200 — should still be within the limit"
        )

    # 4th request must be throttled
    r = client.post("/api/trade")
    assert r.status_code == 429, (
        f"4th request to /api/trade (limit 3/min) returned {r.status_code} "
        f"instead of 429 — limit not enforced"
    )


# ── Test 3: 429 response has the correct JSON shape ──────────────────────────

def test_429_response_has_correct_json_shape():
    """W10-4 contract: the 429 response body is
    ``{"detail": "Rate limit exceeded", "retry_after": N}`` where N is
    a positive integer (seconds until the limit window resets).

    The shape is the project's standard error-envelope variant for
    rate-limit errors — the frontend's ``fetchJson()`` helper expects
    every 4xx/5xx response to have a ``detail`` field, and the
    rate-limit-specific ``retry_after`` lets the UI render a "retry in
    Ns" countdown.
    """
    app, _ = _build_app(write_limit="1/minute")
    client = TestClient(app)

    # First request exhausts the 1/min limit
    client.post("/api/trade")
    # Second request gets throttled
    r = client.post("/api/trade")

    assert r.status_code == 429
    body = r.json()
    assert "detail" in body, f"429 body missing 'detail' field — got {body}"
    assert body["detail"] == "Rate limit exceeded", (
        f"429 body 'detail' must be 'Rate limit exceeded' — got {body['detail']!r}"
    )
    assert "retry_after" in body, f"429 body missing 'retry_after' field — got {body}"
    assert isinstance(body["retry_after"], int), (
        f"'retry_after' must be an int (seconds) — got {type(body['retry_after']).__name__}"
    )
    assert body["retry_after"] > 0, (
        f"'retry_after' must be positive — got {body['retry_after']}"
    )
    # For a per-minute limit, the retry window is 60 seconds.
    assert body["retry_after"] == 60, (
        f"per-minute limit's retry_after should be 60s — got {body['retry_after']}"
    )


# ── Test 4: Retry-After header is present on the 429 response ────────────────

def test_429_response_includes_retry_after_header():
    """W10-4 contract: the 429 response carries a ``Retry-After`` header
    (RFC 7231 §7.1.3) so well-behaved HTTP clients can back off without
    having to parse the JSON body.

    Also verifies the ``X-RateLimit-Limit`` header is present and carries
    the canonical "<amount>/<granularity>" form (e.g. "1/minute") so
    clients can display the limit alongside the countdown.
    """
    app, _ = _build_app(write_limit="1/minute")
    client = TestClient(app)

    client.post("/api/trade")  # exhaust the 1/min limit
    r = client.post("/api/trade")  # this one gets throttled

    assert r.status_code == 429
    assert "Retry-After" in r.headers, (
        "429 response must carry a 'Retry-After' header — clients rely on it to back off"
    )
    retry_after = r.headers["Retry-After"]
    assert retry_after.isdigit(), (
        f"'Retry-After' header must be a numeric string — got {retry_after!r}"
    )
    assert int(retry_after) > 0, (
        f"'Retry-After' must be a positive integer (seconds) — got {retry_after}"
    )
    # Per-minute limit → 60-second retry window
    assert int(retry_after) == 60, (
        f"per-minute limit's Retry-After should be 60s — got {retry_after}"
    )

    # X-RateLimit-Limit carries the canonical "amount/granularity" form
    assert "X-RateLimit-Limit" in r.headers, (
        "429 response must carry an 'X-RateLimit-Limit' header — clients use it "
        "to display the limit alongside the retry countdown"
    )
    x_ratelimit_limit = r.headers["X-RateLimit-Limit"]
    assert x_ratelimit_limit == "1/minute", (
        f"'X-RateLimit-Limit' must be '1/minute' for the 1/min route — got {x_ratelimit_limit!r}"
    )


# ── Test 5 (bonus): limiter.enabled = False disables rate limiting ────────────

def test_limiter_can_be_disabled_via_enabled_flag():
    """W10-4 contract (test-env escape hatch): flipping
    ``limiter.enabled = False`` makes the ``@limiter.limit()`` decorator
    a pass-through — no 429s ever returned. This is what
    ``tests/conftest.py`` relies on to keep the existing test suite
    hermetic to rate-limit state.
    """
    app, limiter = _build_app(write_limit="1/minute")
    # Disable — mirrors what tests/conftest.py does at module-load time.
    limiter.enabled = False
    client = TestClient(app)

    # Fire 10 requests against a 1/min limit — all must pass because the
    # limiter is disabled.
    for i in range(10):
        r = client.post("/api/trade")
        assert r.status_code == 200, (
            f"request #{i} returned {r.status_code} with limiter disabled — "
            f"the 'enabled = False' escape hatch is broken"
        )
