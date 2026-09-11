"""W13-3 — API versioning tests.

Verifies the versioning middleware (``core/api_versioning.versioning_middleware``)
and the ``GET /api/version`` endpoint (registered in ``api/server.py``)
behave per the W13-3 task spec:

* ``GET /api/version`` returns the version info (public, no auth).
* ``Accept-Version: v1`` header is accepted (200 + ``X-API-Version: v1``).
* ``Accept-Version: v99`` header is rejected (400 + supported-versions
  body).
* Every response carries the ``X-API-Version`` + ``X-API-Supported-Versions``
  response headers.
* ``request.state.api_version`` is set on the request object the route
  handler sees (verified by an inline debug route that reflects the
  state back through the response body — direct state introspection
  from a TestClient is impossible because ``request.state`` is
  request-scoped, not response-visible).

Hermeticity
~~~~~~~~~~~
Imports the production ``api.server.app`` (so every route, every
middleware, every Pydantic validator is exercised). The autouse
``_reset_store_factory_defaults`` conftest fixture wipes store
singletons before every test; rate limiting is disabled in
``conftest.py`` (``limiter.enabled = False``) so the per-route slowapi
limits don't interfere.

All tests are SYNC ``def test_...`` — ``TestClient`` bridges each
request through its own anyio portal (mirrors
``tests/test_openapi.py``).
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from api.server import app
from core.api_versioning import (
    API_VERSION,
    HDR_API_VERSION,
    HDR_DEPRECATION,
    HDR_SUPPORTED_VERSIONS,
    HDR_SUNSET,
    SUPPORTED_VERSIONS,
    get_version_info,
    versioning_middleware,
)

# Defensive: disable the rate-limit middleware so a fast test sequence
# against a per-minute-limited route doesn't 429 mid-suite.
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# the bearer token below matches what the ``enforce_api_auth`` middleware
# accepts.
VALID_TOKEN = "test-token-conftest"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_openapi.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. GET /api/version — version info endpoint
# ═══════════════════════════════════════════════════════════════════════════


def test_api_version_endpoint_returns_version_info(client):
    """``GET /api/version`` returns the version info dict, public (no auth)."""
    # No Authorization header — endpoint is in ``PUBLIC_PATHS``.
    response = client.get("/api/version")
    assert response.status_code == 200, (
        f"GET /api/version must return 200; got {response.status_code}. "
        f"Body: {response.text[:300]!r}"
    )
    body = response.json()
    assert "current" in body, f"missing 'current' key in version info: {body}"
    assert "supported" in body, f"missing 'supported' key in version info: {body}"
    assert "deprecated" in body, f"missing 'deprecated' key in version info: {body}"
    assert body["current"] == API_VERSION, (
        f"current version mismatch: expected {API_VERSION!r}, got {body['current']!r}"
    )
    assert body["supported"] == SUPPORTED_VERSIONS, (
        f"supported versions mismatch: expected {SUPPORTED_VERSIONS}, "
        f"got {body['supported']}"
    )
    assert isinstance(body["deprecated"], list), (
        f"'deprecated' must be a list; got {type(body['deprecated'])}"
    )


def test_api_version_endpoint_accessible_without_auth(client):
    """``GET /api/version`` is in ``PUBLIC_PATHS`` — no auth required.

    This is the contract guarantee that lets a client negotiate the API
    version BEFORE presenting credentials (W13-3 design goal: a
    misconfigured client learns the version mismatch first, not the
    auth failure).
    """
    response = client.get("/api/version")  # no Authorization header
    assert response.status_code == 200, (
        "GET /api/version must be accessible without auth — got "
        f"{response.status_code}; body: {response.text[:200]!r}"
    )


def test_get_version_info_function_returns_expected_shape():
    """``get_version_info()`` returns the expected dict shape directly.

    Pure-function test (no TestClient) so a regression in the dict
    construction can't hide behind the JSON round-trip.
    """
    info = get_version_info()
    assert set(info.keys()) == {"current", "supported", "deprecated"}, (
        f"unexpected keys in version info: {info.keys()}"
    )
    assert info["current"] == API_VERSION
    assert info["supported"] == list(SUPPORTED_VERSIONS)
    assert info["deprecated"] == list([])  # default empty


# ═══════════════════════════════════════════════════════════════════════════
# 2. Accept-Version header — accepted / rejected
# ═══════════════════════════════════════════════════════════════════════════


def test_accept_version_v1_header_accepted(client, auth_headers):
    """``Accept-Version: v1`` is accepted — request returns 200."""
    response = client.get(
        "/api/version",
        headers={**auth_headers, "Accept-Version": "v1"},
    )
    assert response.status_code == 200, (
        f"Accept-Version: v1 must return 200; got {response.status_code}. "
        f"Body: {response.text[:300]!r}"
    )
    # Response header echoes the requested version.
    assert response.headers.get(HDR_API_VERSION) == "v1", (
        f"X-API-Version header must be 'v1'; got "
        f"{response.headers.get(HDR_API_VERSION)!r}"
    )


def test_accept_version_v99_header_rejected_with_400(client, auth_headers):
    """``Accept-Version: v99`` is rejected with HTTP 400.

    Returns a ``detail`` message and ``supported_versions`` list so the
    client can self-correct.
    """
    response = client.get(
        "/api/version",
        headers={**auth_headers, "Accept-Version": "v99"},
    )
    assert response.status_code == 400, (
        f"Accept-Version: v99 must return 400; got {response.status_code}. "
        f"Body: {response.text[:300]!r}"
    )
    body = response.json()
    assert "detail" in body, f"missing 'detail' in 400 body: {body}"
    assert "v99" in body["detail"], (
        f"detail must mention the rejected version 'v99': {body['detail']!r}"
    )
    assert body.get("supported_versions") == SUPPORTED_VERSIONS, (
        f"supported_versions in body must be {SUPPORTED_VERSIONS}; "
        f"got {body.get('supported_versions')}"
    )
    # The 400 response still carries the version headers (debugging aid).
    assert response.headers.get(HDR_API_VERSION) == "v99", (
        f"X-API-Version on 400 must echo the rejected 'v99'; got "
        f"{response.headers.get(HDR_API_VERSION)!r}"
    )
    assert response.headers.get(HDR_SUPPORTED_VERSIONS) == ", ".join(
        SUPPORTED_VERSIONS
    ), (
        f"X-API-Supported-Versions on 400 must list {SUPPORTED_VERSIONS}; "
        f"got {response.headers.get(HDR_SUPPORTED_VERSIONS)!r}"
    )


def test_accept_version_v99_rejected_without_auth(client):
    """An unsupported version is rejected BEFORE the auth check fires.

    W13-3 design goal: a misconfigured client learns the version
    mismatch first (400), not the auth failure (401). This test sends
    ``Accept-Version: v99`` WITHOUT an Authorization header and asserts
    the response is 400 (version rejected) — NOT 401 (auth missing).

    Without the versioning middleware running before auth, this would be
    401 (auth middleware is fail-closed on missing tokens).
    """
    response = client.get("/api/version", headers={"Accept-Version": "v99"})
    assert response.status_code == 400, (
        "Accept-Version: v99 without auth must return 400 (version rejected "
        "before auth check), not 401; got "
        f"{response.status_code}. Body: {response.text[:200]!r}"
    )


def test_no_version_header_falls_back_to_default(client, auth_headers):
    """No ``Accept-Version`` header → middleware falls back to ``API_VERSION``.

    The default (``v1``) is in ``SUPPORTED_VERSIONS``, so the request
    succeeds and the response header echoes the default.
    """
    response = client.get("/api/version", headers=auth_headers)
    assert response.status_code == 200, (
        f"no Accept-Version header must return 200; got {response.status_code}"
    )
    assert response.headers.get(HDR_API_VERSION) == API_VERSION, (
        f"X-API-Version must be the default {API_VERSION!r}; got "
        f"{response.headers.get(HDR_API_VERSION)!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Response headers — X-API-Version / X-API-Supported-Versions
# ═══════════════════════════════════════════════════════════════════════════


def test_response_has_x_api_version_header(client, auth_headers):
    """Every response carries the ``X-API-Version`` response header.

    The header is set by the versioning middleware on EVERY response
    (200, 4xx, 5xx) so a client / operator can confirm which version
    served the request without having to inspect the request side.
    """
    response = client.get("/api/version", headers=auth_headers)
    assert HDR_API_VERSION in response.headers, (
        f"response must carry {HDR_API_VERSION} header; "
        f"headers: {dict(response.headers)}"
    )
    assert response.headers[HDR_API_VERSION] == API_VERSION


def test_response_has_x_api_supported_versions_header(client, auth_headers):
    """Every response carries ``X-API-Supported-Versions`` (CSV list)."""
    response = client.get("/api/version", headers=auth_headers)
    assert HDR_SUPPORTED_VERSIONS in response.headers, (
        f"response must carry {HDR_SUPPORTED_VERSIONS} header; "
        f"headers: {dict(response.headers)}"
    )
    expected = ", ".join(SUPPORTED_VERSIONS)
    assert response.headers[HDR_SUPPORTED_VERSIONS] == expected, (
        f"{HDR_SUPPORTED_VERSIONS} must be {expected!r}; got "
        f"{response.headers[HDR_SUPPORTED_VERSIONS]!r}"
    )


def test_x_api_version_header_reflects_requested_version(client, auth_headers):
    """The header reflects the version that actually served the request,
    not a constant. A v1 request → header says v1."""
    response = client.get(
        "/api/version",
        headers={**auth_headers, "Accept-Version": "v1"},
    )
    assert response.status_code == 200
    assert response.headers[HDR_API_VERSION] == "v1"


# ═══════════════════════════════════════════════════════════════════════════
# 4. request.state.api_version — set by middleware
# ═══════════════════════════════════════════════════════════════════════════


def test_request_state_api_version_set_via_inline_route(client, auth_headers):
    """``request.state.api_version`` is set by the middleware before the
    route handler runs.

    Verified by registering a temporary inline route that reflects the
    state back through the response body — direct ``request.state``
    introspection isn't possible from a TestClient because ``state`` is
    request-scoped (the test never sees the actual Request object the
    handler received). The reflected value must match the version
    requested via the header.

    The route is registered AFTER the app is already mounted so it
    doesn't pollute production OpenAPI / production traffic — it lives
    only for the duration of this test session and is harmless because
    ``client`` is function-scoped.
    """

    @app.get("/api/versioning-test/state-echo")
    async def _state_echo(request: Request):
        # Read the state the middleware set. If the middleware didn't
        # run (or didn't set the attr), ``getattr`` returns the default
        # ``"<unset>"`` so the assertion below fails with a clear signal.
        return {"api_version": getattr(request.state, "api_version", "<unset>")}

    try:
        response = client.get(
            "/api/versioning-test/state-echo",
            headers={**auth_headers, "Accept-Version": "v1"},
        )
        assert response.status_code == 200, (
            f"state-echo route must return 200; got {response.status_code}. "
            f"Body: {response.text[:300]!r}"
        )
        body = response.json()
        assert body["api_version"] == "v1", (
            f"request.state.api_version must be 'v1' (set by middleware); "
            f"got {body['api_version']!r}"
        )
    finally:
        # Remove the test-only route so it doesn't leak into the next
        # test or any other test module that imports ``app``.
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/versioning-test/state-echo"
        ]


def test_request_state_api_version_defaults_when_no_header(client, auth_headers):
    """``request.state.api_version`` falls back to ``API_VERSION`` when no
    ``Accept-Version`` header is sent."""
    from core.api_versioning import API_VERSION as _default

    @app.get("/api/versioning-test/state-echo-default")
    async def _state_echo_default(request: Request):
        return {"api_version": getattr(request.state, "api_version", "<unset>")}

    try:
        response = client.get(
            "/api/versioning-test/state-echo-default",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["api_version"] == _default, (
            f"request.state.api_version must be the default {_default!r} when "
            f"no Accept-Version header is sent; got {body['api_version']!r}"
        )
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/versioning-test/state-echo-default"
        ]


# ═══════════════════════════════════════════════════════════════════════════
# 5. versioning_middleware — direct invocation tests
# ═══════════════════════════════════════════════════════════════════════════


def test_versioning_middleware_rejects_unknown_version_direct():
    """Direct call to ``versioning_middleware`` with an unknown version
    returns a ``JSONResponse`` with status 400.

    This bypasses the TestClient stack to verify the middleware's
    contract in isolation — it must NOT raise (raising
    ``HTTPException`` from inside a Starlette ``BaseHTTPMiddleware``
    surfaces as a 500; see the module docstring of
    ``core/api_versioning.py`` for the rationale).
    """
    from fastapi.responses import JSONResponse

    # Build a minimal Request-shaped object: the middleware only reads
    # ``request.url.path`` and ``request.headers``. We use Starlette's
    # ``Request`` constructor with a minimal scope.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/version",
        "raw_path": b"/api/version",
        "query_string": b"",
        "headers": [(b"accept-version", b"v99")],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "extensions": {},
        "state": {},
        "app": None,
        "path_params": {},
    }
    request = Request(scope)

    async def _call_next(req):
        # Should not be reached — middleware short-circuits on the 400.
        raise AssertionError(
            "call_next must NOT be invoked when the version is unsupported"
        )

    import asyncio
    response = asyncio.run(versioning_middleware(request, _call_next))
    assert isinstance(response, JSONResponse), (
        f"versioning_middleware must return a JSONResponse for unknown "
        f"versions; got {type(response)}"
    )
    assert response.status_code == 400, (
        f"status code must be 400; got {response.status_code}"
    )


def test_versioning_middleware_sets_state_for_valid_version_direct():
    """Direct call to ``versioning_middleware`` with a valid version sets
    ``request.state.api_version`` before calling ``call_next``."""

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/version",
        "raw_path": b"/api/version",
        "query_string": b"",
        "headers": [(b"accept-version", b"v1")],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "extensions": {},
        "state": {},
        "app": None,
        "path_params": {},
    }
    request = Request(scope)

    captured = {}

    async def _call_next(req):
        captured["api_version"] = getattr(req.state, "api_version", "<unset>")
        # Return a minimal response — the middleware will annotate it.
        from starlette.responses import Response
        return Response(content=b'{"ok":true}', media_type="application/json")

    import asyncio
    response = asyncio.run(versioning_middleware(request, _call_next))
    # The middleware set state before calling _call_next.
    assert captured.get("api_version") == "v1", (
        f"request.state.api_version must be 'v1' inside call_next; "
        f"captured: {captured}"
    )
    # And the response was annotated.
    assert response.headers.get(HDR_API_VERSION) == "v1"
    assert response.headers.get(HDR_SUPPORTED_VERSIONS) == ", ".join(
        SUPPORTED_VERSIONS
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Deprecation headers — exercised by monkey-patching the policy
# ═══════════════════════════════════════════════════════════════════════════


def test_deprecated_version_carries_deprecation_headers(client, auth_headers, monkeypatch):
    """A version listed in ``DEPRECATED_VERSIONS`` is still accepted (200)
    but carries ``Deprecation`` + ``Sunset`` response headers (RFC 8594 /
    RFC 7231).

    The default policy has NO deprecated versions, so this test
    monkey-patches ``core.api_versioning.DEPRECATED_VERSIONS`` to include
    ``v1`` for the duration of the test, then asserts the deprecation
    headers are present on the response.
    """
    import core.api_versioning as _av

    monkeypatch.setattr(_av, "DEPRECATED_VERSIONS", ["v1"])
    try:
        response = client.get(
            "/api/version",
            headers={**auth_headers, "Accept-Version": "v1"},
        )
        assert response.status_code == 200, (
            f"deprecated v1 must still return 200; got {response.status_code}"
        )
        assert response.headers.get(HDR_DEPRECATION) == "true", (
            f"Deprecation header must be 'true'; got "
            f"{response.headers.get(HDR_DEPRECATION)!r}"
        )
        assert HDR_SUNSET in response.headers, (
            f"Sunset header must be present; headers: {dict(response.headers)}"
        )
    finally:
        # monkeypatch.setattr handles teardown automatically, but the
        # explicit reset documents the invariant.
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 7. URL-prefix versioning — /api/v1/...
# ═══════════════════════════════════════════════════════════════════════════


def test_url_prefix_versioning_sets_state(client, auth_headers):
    """A request to ``/api/v1/<existing-route>`` resolves the version from
    the URL prefix and sets ``request.state.api_version`` accordingly.

    The spec marks full URL-prefix routing (mounting a v1-prefixed
    router) as optional / future work — the middleware still parses the
    version from the path segment and stores it on ``request.state`` so
    a future router can layer on top without changing the middleware
    contract.

    We exercise this against an inline route mounted at
    ``/api/v1/versioning-test/url-echo`` to verify the state was set
    correctly. The inline route doesn't conflict with the existing
    ``/api/...`` routes because it lives under the ``/api/v1/`` prefix.
    """

    @app.get("/api/v1/versioning-test/url-echo")
    async def _url_echo(request: Request):
        return {"api_version": getattr(request.state, "api_version", "<unset>")}

    try:
        response = client.get(
            "/api/v1/versioning-test/url-echo",
            headers=auth_headers,  # no Accept-Version header — URL wins
        )
        assert response.status_code == 200, (
            f"inline /api/v1/... route must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        body = response.json()
        assert body["api_version"] == "v1", (
            f"request.state.api_version must be 'v1' (from URL prefix); "
            f"got {body['api_version']!r}"
        )
        # The response header should also reflect the URL prefix version.
        assert response.headers.get(HDR_API_VERSION) == "v1"
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/v1/versioning-test/url-echo"
        ]
