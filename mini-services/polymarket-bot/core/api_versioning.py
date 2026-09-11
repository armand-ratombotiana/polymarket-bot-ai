"""API versioning support.

Supports two strategies:
1. URL prefix versioning: ``/api/v1/...`` (recognised, sets
   ``request.state.api_version`` from the path segment).
2. Header versioning: ``Accept-Version: v1``

Current version: ``v1`` (overridable via the ``API_VERSION`` env var).

Design notes
------------
The middleware is **read-mostly**: it never rewrites the request path or
breaks existing routes. Existing routes live at ``/api/...`` (no version
prefix); the version is tracked purely on ``request.state.api_version``
and echoed back on the response via ``X-API-Version`` so the client /
operator can confirm which version actually served the request. A future
``/api/v1/...`` router can layer on top of this without changing the
middleware contract.

URL-prefix detection
~~~~~~~~~~~~~~~~~~~~
A path like ``/api/version`` is NOT a versioned URL — the segment after
``/api/`` is ``"version"``, which starts with ``"v"`` but is NOT a
valid version identifier (``v1`` / ``v2`` / ``v3`` ...). The detector
therefore requires the segment to match ``^v\\d+$`` (the literal letter
``v`` followed by one or more digits). Without this guard, every
``/api/v*`` route (e.g. ``/api/version``, ``/api/volume``, a hypothetical
``/api/vault``) would be misparsed as versioned URLs and rejected with
400 because the parsed "version" string isn't in ``SUPPORTED_VERSIONS``.

Error behaviour
~~~~~~~~~~~~~~~
Raising ``fastapi.HTTPException`` from inside a Starlette
``BaseHTTPMiddleware`` (the class that powers ``@app.middleware("http")``)
does **not** route through FastAPI's ``ExceptionMiddleware`` — the
exception propagates all the way up to ``ServerErrorMiddleware`` and
surfaces as a 500. This is a long-standing Starlette quirk (the
``BaseHTTPMiddleware`` runs the dispatch func inside an ``anyio``
task group whose exception group is collapsed *after* the
``ExceptionMiddleware`` would have had a chance to handle it).

The fix is to return a ``JSONResponse(status_code=400, ...)`` directly
from the middleware — this preserves the ``{"detail": "..."}`` body
shape clients already expect from FastAPI's default ``HTTPException``
handler while actually returning a 400 (verified against
``starlette`` 0.37+).
"""
from __future__ import annotations

import os
import re
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

# ── Version policy ──────────────────────────────────────────────────────────
# ``API_VERSION`` is read once at import time so a hot-reload of the
# module (or a fresh ``import`` in a sibling test module) picks up an
# updated env var. ``SUPPORTED_VERSIONS`` / ``DEPRECATED_VERSIONS`` are
# plain module-level lists (not tuples) so operators can monkey-patch
# them in tests (e.g. ``api_versioning.SUPPORTED_VERSIONS.append("v2")``)
# without hitting "tuple assignment index out of range" issues.
API_VERSION = os.environ.get("API_VERSION", "v1")
SUPPORTED_VERSIONS: list[str] = ["v1"]
DEPRECATED_VERSIONS: list[str] = []

# Regex for a valid version URL segment: ``v`` followed by one or more
# digits. Compiled once at import. Used by the URL-prefix detector so a
# path like ``/api/version`` (segment ``"version"`` — starts with ``v``
# but isn't a version) isn't misparsed as versioned.
_VERSION_SEGMENT_RE = re.compile(r"^v\d+$")

# Standard headers we attach to every versioned response.
# Defined once here so the middleware and the ``/api/version`` endpoint
# use the exact same header names.
HDR_API_VERSION = "X-API-Version"
HDR_SUPPORTED_VERSIONS = "X-API-Supported-Versions"
HDR_DEPRECATION = "Deprecation"
HDR_SUNSET = "Sunset"

# Sunset date for any deprecated version (RFC 7231 §7.1.3 HTTP-date).
# Picked far enough out that operators have a deprecation runway, but
# fixed so the value is deterministic in tests.
DEPRECATED_SUNSET_DATE = "Sat, 31 Dec 2025 23:59:59 GMT"


async def versioning_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable],
):
    """Middleware that handles API versioning.

    - Detects the requested version (URL prefix ``/api/vN/...`` OR the
      ``Accept-Version`` request header).
    - Falls back to ``API_VERSION`` (the module-level default) when the
      client doesn't specify one.
    - Validates the version against ``SUPPORTED_VERSIONS``; rejects with
      ``400 Bad Request`` when unsupported.
    - Stores the resolved version on ``request.state.api_version`` so
      route handlers can branch on it if they need to.
    - Adds ``X-API-Version`` + ``X-API-Supported-Versions`` response
      headers on EVERY response (200, 4xx, 5xx).
    - Adds ``Deprecation`` + ``Sunset`` headers when the version is in
      ``DEPRECATED_VERSIONS``.
    """
    path = request.url.path

    # ── 1. URL-prefix versioning: /api/v1/... ──────────────────────────────
    # Parse the leading version segment after ``/api/`` WITHOUT rewriting
    # the path — we only set ``request.state.api_version`` from it. This
    # keeps the middleware a no-op for routing: existing ``/api/...``
    # routes continue to work, and ``/api/v1/foo`` falls through to the
    # router (which will 404 unless a v1-prefixed router is mounted — the
    # spec marks that as optional / future work).
    #
    # The segment must match ``^v\d+$`` so a route like ``/api/version``
    # (segment ``"version"``) isn't misparsed as a versioned URL — see
    # the module docstring "URL-prefix detection" section.
    version_from_url: str | None = None
    if path.startswith("/api/v"):
        # path = "/api/v1/foo/bar" → ["", "api", "v1", "foo", "bar"]
        parts = path.split("/")
        if len(parts) >= 3 and _VERSION_SEGMENT_RE.match(parts[2]):
            version_from_url = parts[2]

    # ── 2. Header versioning ───────────────────────────────────────────────
    version_from_header = request.headers.get("Accept-Version")

    # ── 3. Effective version (URL > header > default) ──────────────────────
    version = version_from_url or version_from_header or API_VERSION

    # ── 4. Validation ─────────────────────────────────────────────────────
    is_deprecated = version in DEPRECATED_VERSIONS
    if version not in SUPPORTED_VERSIONS and not is_deprecated:
        # See module docstring: must return a JSONResponse (raising
        # ``HTTPException`` from inside ``@app.middleware("http")``
        # surfaces as a 500 under Starlette's BaseHTTPMiddleware).
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    f"Unsupported API version '{version}'. "
                    f"Supported: {SUPPORTED_VERSIONS}"
                ),
                "code": "UNSUPPORTED_API_VERSION",
                "supported_versions": SUPPORTED_VERSIONS,
            },
            headers={
                HDR_API_VERSION: version,
                HDR_SUPPORTED_VERSIONS: ", ".join(SUPPORTED_VERSIONS),
            },
        )

    # ── 5. Store on request state for downstream handlers ─────────────────
    # ``request.state`` is the canonical Starlette place for per-request
    # extras; downstream handlers reach it via ``request.state.api_version``.
    request.state.api_version = version

    # ── 6. Forward the request ─────────────────────────────────────────────
    response = await call_next(request)

    # ── 7. Annotate the response with version info ────────────────────────
    response.headers[HDR_API_VERSION] = version
    response.headers[HDR_SUPPORTED_VERSIONS] = ", ".join(SUPPORTED_VERSIONS)

    # Deprecation runway (RFC 8594 §3 + RFC 7231 §7.1.3).
    if is_deprecated:
        response.headers[HDR_DEPRECATION] = "true"
        response.headers[HDR_SUNSET] = DEPRECATED_SUNSET_DATE

    return response


def get_version_info() -> dict:
    """Static snapshot of the active version policy.

    Returned by the ``GET /api/version`` route. Kept as a function (not
    a module-level constant) so a test that monkey-patches
    ``SUPPORTED_VERSIONS`` / ``DEPRECATED_VERSIONS`` sees the new values
    on the next call rather than a stale snapshot from import time.
    """
    return {
        "current": API_VERSION,
        "supported": list(SUPPORTED_VERSIONS),
        "deprecated": list(DEPRECATED_VERSIONS),
    }

