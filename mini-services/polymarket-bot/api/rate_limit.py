"""
api/rate_limit.py — shared rate-limiter singleton (W10-4).

Why a dedicated module (instead of inlining ``Limiter(...)`` inside
``api/server.py``)?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``slowapi``'s per-route rate limits are applied via the ``@limiter.limit()``
decorator at function-definition time. Several routes the W10-4 spec
requires to be rate-limited (notably ``POST /api/live/enable``) are NOT
defined in ``api/server.py`` — they live in ``core/live_safety_gate.py``
under its ``register_routes(app)`` callable (it's a FastAPI sub-router
pattern used by 13 sibling ``register_routes(app)`` modules in this
codebase: ``core/shadow_trading.py``, ``core/retention.py``,
``ml/routes.py``, ``risk/routes.py``, ``core/observability.py``, …).

Both ``api/server.py`` (the top-level FastAPI app) and
``core/live_safety_gate.py`` need to apply the SAME ``limiter`` object so
the in-memory hit counter is shared — otherwise each module would have
its own counter and the 3/min limit on ``/api/live/enable`` would only
be enforced inside whichever module's limiter happened to wrap that
route.

A direct ``from api.server import limiter`` inside
``core/live_safety_gate.py`` would create a circular import
(``api/server.py`` itself imports ``core/live_safety_gate`` to call
``register_routes(app)`` at startup). Even with a late (in-function)
import, that path would force the FULL ``api/server.py`` to be imported
whenever the W9 ``tests/test_live_safety_gate_api.py`` suite builds its
own minimal ``FastAPI()`` app and calls ``register_routes(app)`` — which
pulls in every ``core/*`` module, sets up the lifespan, registers 50+
routes, and otherwise perturbs the test's hermetic isolation.

This module breaks the cycle. It is imported by BOTH call sites
(``api/server.py`` and ``core/live_safety_gate.py``) at zero cost: it
defines a single ``Limiter`` singleton (keyed on the client IP via
``slowapi.util.get_remote_address``) and a couple of policy constants
that the call sites import by name. No FastAPI ``app``, no lifespan, no
side effects — just the limiter object.

Disabling in tests
------------------
The ``tests/conftest.py`` autouse fixture flips ``limiter.enabled = False``
before every test so the rate-limit counter doesn't accumulate hits
across test boundaries (TestClient always uses the same source IP —
``127.0.0.1`` — so without disabling, the 2nd test in the suite would
hit the per-route limit and start receiving 429s instead of the
expected 200s). The ``test_rate_limiting.py`` module builds its own
Limiter instances for the limit-is-actually-enforced tests so the
global ``enabled = False`` flag doesn't affect them.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# ── Rate-limit policy (W10-4) ─────────────────────────────────────────────────
# Centralised here so the policy string is referenced from a single source of
# truth: the exception handler in ``server.py`` and the response-header
# middleware both surface this policy to clients via ``X-RateLimit-Policy``.
READ_LIMIT: str = "120/minute"      # GET routes — generous, allows polling
WRITE_LIMIT: str = "30/minute"      # POST/PUT/DELETE — stricter
HEAVY_LIMIT: str = "5/minute"       # ML retrain, backtest — very strict
TRADE_LIMIT: str = "20/minute"      # Trade / orders / position-close — auth-sensitive
ARBITRAGE_LIMIT: str = "10/minute"  # Arbitrage execute — auth-sensitive + heavy
LIVE_ENABLE_LIMIT: str = "3/minute" # Live-mode flip — strictest; one-shot escalation

# ── Shared limiter singleton ──────────────────────────────────────────────────
# Keyed on the client IP via ``get_remote_address`` so each external client is
# rate-limited independently. ``headers_enabled=False`` because slowapi's
# header-injection path requires every rate-limited route to declare a
# ``response: Response`` parameter — too invasive for the existing 50+ routes.
# The ``X-RateLimit-*`` headers are populated manually by the custom
# ``rate_limit_handler`` exception handler in ``api/server.py`` (which has
# direct access to ``exc.limit`` and can derive the values without going
# through slowapi's header-injection machinery).
limiter: Limiter = Limiter(key_func=get_remote_address, headers_enabled=False)


__all__ = [
    "limiter",
    "READ_LIMIT",
    "WRITE_LIMIT",
    "HEAVY_LIMIT",
    "TRADE_LIMIT",
    "ARBITRAGE_LIMIT",
    "LIVE_ENABLE_LIMIT",
]
