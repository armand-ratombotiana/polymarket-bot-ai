"""
Contract-test fixtures for the polymarket-bot API.

W15-3 — Frontend ↔ Backend response-shape contract tests.

Why this file exists
~~~~~~~~~~~~~~~~~~~~~
The frontend (``src/lib/api-client.ts`` + ``src/lib/schemas.ts``) declares an
expected wire-shape for every backend route. Until W15-3 there was no test
on the BACKEND side asserting the response actually conforms to that shape —
the Zod schemas in ``src/lib/schemas.ts`` only fire at runtime in the
browser, so a backend change (renamed field, dropped optional, type drift
from ``int`` → ``str``) silently produced ``undefined`` in the UI.

This module wires the FastAPI ``TestClient`` against the production
``api.server.app`` and exposes:

  * ``client``         — module-scoped ``TestClient(app, raise_server_exceptions=False)``.
                         ``raise_server_exceptions=False`` so the 500-error
                         contract test (``test_error_contracts.py``) gets the
                         sanitized JSON response instead of a re-raised
                         exception in the test process — mirrors
                         ``tests/test_security.py``.
  * ``auth_headers``   — ``{"Authorization": "Bearer <VALID_TOKEN>"}`` for
                         every authenticated request.

Token resolution
~~~~~~~~~~~~~~~
The task spec hard-codes ``VALID_TOKEN = "I76FCamSbBw0e1r_V0RRX81uG-..."``,
but the sibling ``tests/conftest.py`` redirects ``API_TOKEN`` to
``test-token-conftest`` via ``os.environ.setdefault`` BEFORE any project
module is imported. Whichever value is set, ``settings.api_token`` is the
value ``enforce_api_auth`` compares against — so we resolve it dynamically
at fixture-load time instead of baking in a string. This makes the contract
suite robust against:
  * the sandbox (token = ``test-token-conftest``);
  * a dev box that exports a real token;
  * the spec's hard-coded value if an operator exports it via ``API_TOKEN``
    before pytest starts.

Rate-limiting is disabled (mirrors ``tests/test_security.py`` /
``tests/test_cli.py``) so a high-volume contract suite (60+ requests) does
not hit the 120/min read cap and start emitting spurious 429s. The
dedicated ``tests/test_rate_limiting.py`` module builds its own ``Limiter``
instances for the limit-is-actually-enforced assertions, so this global
``enabled = False`` flag does NOT affect them.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.server import app

# Disable rate limiting — every contract test fires ≥1 request per
# assertion, and TestClient always presents the same source IP (127.0.0.1)
# so the per-IP 120/min read cap would 429 the suite ~halfway through.
try:
    from api.server import limiter
    limiter.enabled = False
except ImportError:  # pragma: no cover — defensive
    pass

# Resolve the bearer token dynamically. ``settings.api_token`` is whatever
# the sibling ``tests/conftest.py`` (or an outer CI runner) set via the
# ``API_TOKEN`` env var before any project module was imported — typically
# ``test-token-conftest``. The spec's hard-coded value is kept as a
# fallback so the suite still works if a future env exports it.
try:
    from config import settings
    VALID_TOKEN = settings.api_token or "I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT"
except Exception:  # noqa: BLE001 — defensive: never break test collection
    VALID_TOKEN = "I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT"


@pytest.fixture(scope="module")
def client():
    """Module-scoped ``TestClient`` bound to the production FastAPI app.

    ``raise_server_exceptions=False`` so the 500-error contract test gets
    the sanitized JSON response (``{"detail": "Internal server error",
    "path": "..."}``) instead of a re-raised exception in the test
    process — mirrors the pattern in ``tests/test_security.py``.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """The ``Authorization: Bearer <VALID_TOKEN>`` header every
    authenticated route requires. Resolved dynamically from
    ``settings.api_token`` so the suite is robust to env-var overrides."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}
