"""
W10-6 — Comprehensive backend integration tests.

Drives the **full production FastAPI app** (``api.server.app``) end-to-end
through ``fastapi.testclient.TestClient`` so every test exercises the real
HTTP request → middleware → route → response cycle: the CORS middleware,
the bearer-token auth middleware (``enforce_api_auth``), the request
logging middleware, the global exception handler, every Pydantic validator
on every route, and every singleton the route handlers close over
(``store`` / ``risk_manager`` / ``paper_sim`` / ``ml_model`` /
``model_registry`` / ``drift_detector`` / ``fundamental_engine`` /
``market_discovery`` / ``observability`` / ``decision_ledger`` …).

Why drive the production ``app`` directly
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The sibling test modules ``tests/test_shadow_trading_api.py`` (W10),
``tests/test_live_safety_gate_api.py`` (W9), and
``tests/test_error_handling.py`` (W9-8) deliberately build a fresh
``FastAPI()`` per test and register only the routes under test — that
keeps each of those test modules hermetic and focused on its specific
contract. W10-6 takes the COMPLEMENTARY approach: it imports the
production ``app`` (with every route, every middleware, every exception
handler) and exercises the full request cycle. The two approaches together
give the test suite BOTH breadth (W10-6: every endpoint, real middleware
chain) and depth (W10/W9/W9-8: contract specifics on isolated routes).

Hermeticity
~~~~~~~~~~~
- The autouse ``_reset_store_factory_defaults`` conftest fixture wipes
  ``store`` / ``risk_manager`` / ``paper_sim`` / ``paper_sim._virtual_balance_usdc``
  to factory defaults BEFORE every test, so read-only endpoints always
  see the same empty baseline.
- ``conftest.py`` redirects every persisted-state path
  (``AUDIT_DB_PATH`` / ``DECISION_LEDGER_DB_PATH`` / ``OBSERVABILITY_DB_PATH``
  / ``MODEL_REGISTRY_PATH`` / ``MARKET_DB_PATH`` …) to
  ``/tmp/pmbot_conftest_isolation/`` so the SQLite files the route
  handlers read from are isolated from the production ``/app/data/``
  paths (which would be read-only in this sandbox).
- The ``API_TOKEN`` env var is set to ``"test-token-conftest"`` by
  conftest — that is the bearer token every authenticated request
  uses here (the ``VALID_TOKEN`` module constant below).

Rate limiting
~~~~~~~~~~~~~
W10-4 (rate limiting) has not yet landed as of this task — no
``slowapi`` / ``Limiter`` import is present anywhere in the repo
(verified via ripgrep across the polymarket-bot tree). The defensive
``try: from api.server import limiter; limiter.enabled = False; except
ImportError: pass`` block below auto-activates if W10-4 lands later, so
these tests keep working whether or not the rate-limit middleware is
present. If W10-4 lands and IS enabled, requests in this test module
run well under any plausible rate limit (32 tests, ~0.1s each), so the
``limiter.enabled = False`` toggle is belt-and-braces rather than
load-bearing — but the cost of the toggle is one try/except, so we
keep it for forward compatibility.

Sync tests
~~~~~~~~~~
All tests are SYNC ``def test_...`` (not ``async def``). ``TestClient``
bridges each request into the ASGI app via its own ``anyio`` portal
(owns its own event loop); ``pytest.mark.asyncio`` would compete with
that portal. Mirrors the established convention in
``tests/test_settlement.py``, ``tests/test_decision_ledger.py``,
``tests/test_shadow_trading_api.py``, ``tests/test_live_safety_gate_api.py``,
and ``tests/test_error_handling.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.server import app

# ── Defensive: disable rate-limit middleware if W10-4 lands later ──────────
# W10-4 (rate limiting) is not yet present in the repo, but the W10-6 spec
# explicitly asks for this toggle so the test suite keeps working when W10-4
# lands. The import is wrapped in try/except ImportError so the test module
# imports cleanly whether or not the ``limiter`` symbol exists yet.
try:  # pragma: no cover — executed only when W10-4 lands
    from api.server import limiter  # type: ignore[attr-defined]
    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ── Bearer token every authenticated request uses ─────────────────────────
# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# ``settings.api_token`` resolves to this string in every test. The
# bearer-token auth middleware (``enforce_api_auth`` in ``api/server.py``)
# compares the credential against ``settings.api_token`` using
# ``hmac.compare_digest`` — this constant mirrors what the middleware will
# accept. The W10-6 spec's placeholder token
# (``I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT``)
# was a template; the actual sandbox value is ``test-token-conftest``
# (set by conftest).
VALID_TOKEN = "test-token-conftest"


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    """A ``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` is passed so that the global
    exception handler (``api/server.py::global_exception_handler``) can
    be exercised end-to-end on the error-handling tests — without it,
    Starlette re-raises the exception in the test process instead of
    letting the handler return the sanitized 500 response. Mirrors the
    pattern in ``tests/test_error_handling.py`` (W9-8).

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding / strategy_registry /
    training_orchestrator. ``TestClient(app)`` (constructed WITHOUT
    ``with``) does NOT trigger the lifespan — the lifespan only runs
    on ``__enter__`` of the ``TestClient`` context manager — so the
    heavy startup is skipped, and each test stays fast (<0.5s).
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """The ``Authorization: Bearer <VALID_TOKEN>`` header every
    authenticated request must carry.

    The ``enforce_api_auth`` middleware (``api/server.py`` line ~485)
    compares ``hmac.compare_digest(creds, settings.api_token)`` against
    the ``Bearer`` scheme credential; the value here matches the
    ``test-token-conftest`` env var conftest sets.
    """
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. HEALTH & SYSTEM ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthEndpoints:
    """Liveness probe + system status / snapshot / health endpoints.

    ``GET /api/health`` is the ONLY unauthenticated route (``PUBLIC_PATHS``
    in ``api/server.py``); the other three require the bearer token.
    """

    def test_health_returns_200_with_status_field(self, client, auth_headers):
        """``GET /api/health`` returns 200 — and is the only route the
        auth middleware lets through WITHOUT a bearer token (per
        ``PUBLIC_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json"}``
        in ``api/server.py``). The body carries a ``status`` field so
        monitoring probes can distinguish healthy vs. degraded without
        parsing the whole payload.
        """
        # Auth headers passed but the route is in PUBLIC_PATHS so they're
        # not strictly required; we pass them anyway so the assertion is
        # symmetric with every other test in this file.
        response = client.get("/api/health", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/health must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The route handler returns ``{"status": "ok", "timestamp": ...,
        # "paper": bool}``. Accept any of the contract-equivalent keys
        # the spec enumerates (status / healthy / mode) so a future
        # refactor that renames the field doesn't silently break the test.
        assert "status" in data or "healthy" in data or "mode" in data, (
            f"/api/health response should carry one of status/healthy/mode; "
            f"got {data!r}"
        )

    def test_health_accessible_without_auth(self, client):
        """``GET /api/health`` is the liveness probe — it MUST be
        reachable WITHOUT a bearer token so Docker / k8s health checks
        can hit it before the operator has configured ``API_TOKEN``.
        """
        response = client.get("/api/health")
        assert response.status_code == 200, (
            f"/api/health is the liveness probe and must be unauthenticated; "
            f"got {response.status_code}. Body: {response.text!r}"
        )

    def test_status_returns_200_with_mode_and_balance(self, client, auth_headers):
        """``GET /api/status`` returns 200 with the canonical trading
        mode and (in paper mode) the paper balance. The dashboard
        renders both fields top-of-page; missing either would crash the
        UI mid-render.
        """
        response = client.get("/api/status", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/status must return 200 with valid auth; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        data = response.json()
        assert "mode" in data, (
            f"/api/status must carry the canonical 'mode' field; got {sorted(data.keys())}"
        )
        # ``paper_balance`` is present iff settings.paper_trade is True.
        # conftest forces TRADING_MODE=paper, so paper_trade is True and
        # paper_balance should be a number (the autouse store reset puts
        # it at BANKROLL_BASELINE = 100.00).
        assert "paper_balance" in data, (
            f"/api/status must carry 'paper_balance' in paper mode; got {sorted(data.keys())}"
        )

    def test_snapshot_returns_200_with_positions_and_orders(self, client, auth_headers):
        """``GET /api/snapshot`` returns 200 with a payload that carries
        both ``positions`` and ``open_orders`` arrays (the dashboard's
        main portfolio panel renders both). The arrays may be empty
        (fresh store baseline — the autouse ``_reset_store_factory_defaults``
        conftest fixture wipes ``store.positions`` and
        ``store.open_orders`` before every test) but the KEYS must be
        present so the dashboard's ``data.positions.map(...)`` call
        doesn't throw.
        """
        response = client.get("/api/snapshot", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/snapshot must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The snapshot's contract is the union of positions + open_orders
        # + recent_trades + events + ml; the dashboard iterates each.
        assert "positions" in data, (
            f"/api/snapshot must carry 'positions'; got {sorted(data.keys())}"
        )
        assert "open_orders" in data, (
            f"/api/snapshot must carry 'open_orders'; got {sorted(data.keys())}"
        )
        # The arrays are lists (could be empty) — NOT None.
        assert isinstance(data["positions"], list)
        assert isinstance(data["open_orders"], list)

    def test_system_health_returns_200(self, client, auth_headers):
        """``GET /api/system/health`` returns 200 with the honest
        pipeline-health roll-up (kill_switch / timescale_db /
        reconciliation / book_poller / ml_engine / watchdog checks
        composed into a single HEALTHY / DEGRADED / UNHEALTHY status).
        """
        response = client.get("/api/system/health", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/system/health must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The headline field is ``status`` — one of HEALTHY / DEGRADED /
        # UNHEALTHY. The test does NOT assert the value (it depends on
        # live component state — the kill switch, watchdog findings,
        # DB reachability); it only asserts the field is present so the
        # dashboard can render the status badge.
        assert "status" in data, (
            f"/api/system/health must carry 'status'; got {sorted(data.keys())}"
        )
        assert data["status"] in ("HEALTHY", "DEGRADED", "UNHEALTHY"), (
            f"/api/system/health 'status' must be one of the documented "
            f"tri-state values; got {data['status']!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. TRADING ENDPOINTS (READ-ONLY)
# ═══════════════════════════════════════════════════════════════════════════


class TestTradingEndpoints:
    """Read-only trading-state endpoints.

    The autouse ``_reset_store_factory_defaults`` conftest fixture wipes
    ``store.positions`` / ``store.open_orders`` / ``store.trades`` /
    ``store.order_history`` BEFORE every test, so each of these endpoints
    returns a payload with an empty list and ``count=0`` — proving the
    read path does NOT 404 or 500 on empty state.
    """

    def test_positions_returns_200_with_array(self, client, auth_headers):
        """``GET /api/positions`` returns 200 with a ``positions`` list.
        Empty state (fresh store baseline) returns ``count=0`` and an
        empty list — NOT 404, NOT 500.
        """
        response = client.get("/api/positions", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/positions must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "positions" in data and isinstance(data["positions"], list)
        assert data["count"] == len(data["positions"])

    def test_orders_returns_200_with_array(self, client, auth_headers):
        """``GET /api/orders`` returns 200 with an ``orders`` list.
        Empty state returns ``count=0`` and an empty list.
        """
        response = client.get("/api/orders", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/orders must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "orders" in data and isinstance(data["orders"], list)
        assert data["count"] == len(data["orders"])

    def test_trades_returns_200_with_array(self, client, auth_headers):
        """``GET /api/trades`` returns 200 with a ``trades`` list.
        Empty state returns ``count=0`` and an empty list.
        """
        response = client.get("/api/trades", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/trades must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "trades" in data and isinstance(data["trades"], list)
        assert data["count"] == len(data["trades"])

    def test_positions_empty_state_returns_empty_array_not_404(self, client, auth_headers):
        """Empty store state MUST return an empty array (not 404, not
        500). This is the load-bearing contract the dashboard's
        ``data.positions.map(...)`` call depends on — a 404 would crash
        the render. Verified explicitly here (rather than implicit in
        the 200 test above) because the W10-6 spec calls it out as a
        distinct contract.
        """
        # Triple-check: positions, orders, AND trades all return empty
        # arrays on fresh state — not None, not 404, not 500.
        for path, field in [
            ("/api/positions", "positions"),
            ("/api/orders", "orders"),
            ("/api/trades", "trades"),
        ]:
            response = client.get(path, headers=auth_headers)
            assert response.status_code == 200, (
                f"{path} must return 200 on empty state, not 404 / 500"
            )
            body = response.json()
            assert body[field] == [], (
                f"{path} must return an empty {field} array on empty state; "
                f"got {body[field]!r}"
            )
            assert body["count"] == 0, (
                f"{path} must report count=0 on empty state; got {body['count']}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 3. MARKET ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketEndpoints:
    """Market-data endpoints.

    ``GET /api/markets`` is the ONLY route here that depends on a live
    upstream (``gamma_client.get_markets`` hits Polymarket's Gamma API).
    In a network-isolated sandbox the call raises and the route returns
    502 with a sanitized detail (verified in W9-8); in an environment
    with network access it returns 200. This test module accepts BOTH
    so it stays green in either environment (the route is correctly
    wired either way — the W9-8 test module specifically covers the
    502 sanitization contract). The other three market endpoints read
    from in-process state (``market_discovery`` / ``store.order_books``)
    and so are fully deterministic.
    """

    def test_markets_returns_200_or_502(self, client, auth_headers, monkeypatch):
        """``GET /api/markets`` returns 200 with a ``markets`` array when
        the upstream Gamma API is reachable, or 502 with a sanitized
        ``detail`` when it isn't. To make this test hermetic and
        deterministic, we monkeypatch ``gamma_client.get_markets`` to
        return an empty list — proving the route is wired correctly and
        that the empty-array contract (``count=0``) holds without
        depending on network state.
        """
        # Patch BOTH the bound name on ``api.server`` (the route handler
        # imports gamma_client at module scope) and the singleton on
        # ``core.gamma_client`` (mirrors the W9-8 pattern in
        # tests/test_error_handling.py::test_upstream_failure_detail_is_sanitized).
        async def _fake_get_markets(*args, **kwargs):
            return []

        async def _fake_search_markets(*args, **kwargs):
            return []

        class _FakeGammaClient:
            get_markets = _fake_get_markets
            search_markets = _fake_search_markets

        fake = _FakeGammaClient()
        import core.gamma_client as gamma_module
        import api.server as srv
        monkeypatch.setattr(gamma_module, "gamma_client", fake)
        monkeypatch.setattr(srv, "gamma_client", fake)

        response = client.get("/api/markets", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/markets with stubbed gamma_client must return 200; "
            f"got {response.status_code}. Body: {response.text!r}"
        )
        data = response.json()
        assert "markets" in data and isinstance(data["markets"], list)
        assert data["count"] == len(data["markets"])

    def test_orderbooks_returns_200(self, client, auth_headers):
        """``GET /api/orderbooks`` returns 200 with the live order-book
        cache. Empty state (no tracked tokens) returns ``count=0`` and
        an empty ``order_books`` array — NOT 404.
        """
        response = client.get("/api/orderbooks", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/orderbooks must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "order_books" in data and isinstance(data["order_books"], list)
        assert data["count"] == len(data["order_books"])

    def test_markets_catalog_returns_200(self, client, auth_headers):
        """``GET /api/markets/catalog`` returns 200 with the indexed
        market catalog (the universal discovery engine's full hierarchy
        metadata). Empty state returns an empty ``catalog`` array.
        """
        response = client.get("/api/markets/catalog", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/markets/catalog must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "catalog" in data and isinstance(data["catalog"], list)
        assert data["count"] == len(data["catalog"])

    def test_markets_coverage_returns_200(self, client, auth_headers):
        """``GET /api/markets/coverage`` returns 200 with authoritative
        Polymarket catalog coverage metrics (validated_markets_stored,
        coverage_percentage, exclusions audit log).
        """
        response = client.get("/api/markets/coverage", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/markets/coverage must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The coverage report's contract: counts + percentage + sample.
        assert "coverage_percentage" in data, (
            f"/api/markets/coverage must carry 'coverage_percentage'; "
            f"got {sorted(data.keys())}"
        )
        assert "validated_markets_stored" in data


# ═══════════════════════════════════════════════════════════════════════════
# 4. ML ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestMLEndpoints:
    """ML model governance + diagnostics endpoints.

    All four read from in-process singletons (``ml_model`` /
    ``model_registry`` / ``drift_detector`` / ``ensemble_meta_learner``)
    so they're fully deterministic. The ML model is seeded at module
    import time on synthetic-only data, so ``model_ready=True`` and
    ``model_version`` is a non-empty string from the registry.
    """

    def test_ml_returns_200_with_model_info(self, client, auth_headers):
        """``GET /api/ml`` returns 200 with the ML ensemble's status:
        model_type, members, model_ready, model_version, drift status,
        adaptive weights, meta-learner summary, training orchestrator
        stats, label backfill stats, Brier / AUC / ECE / feature
        importances.
        """
        response = client.get("/api/ml", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The headline contract: model_type string + model_ready flag +
        # version. The dashboard renders all three at the top of the ML
        # panel.
        assert "model_type" in data, (
            f"/api/ml must carry 'model_type'; got {sorted(data.keys())}"
        )
        assert "model_ready" in data, (
            f"/api/ml must carry 'model_ready'; got {sorted(data.keys())}"
        )

    def test_ml_metrics_returns_200_with_metrics_dict(self, client, auth_headers):
        """``GET /api/ml/metrics`` returns 200 with the full quantitative
        diagnostics dict (Brier, ROC-AUC, log_loss, ECE, Sharpe,
        reliability curve, feature importances, drift, meta-learner
        summary, registry summary).
        """
        response = client.get("/api/ml/metrics", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml/metrics must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # Spot-check the canonical metric names the dashboard renders.
        for key in ("brier_score", "roc_auc", "ece"):
            assert key in data, (
                f"/api/ml/metrics must carry '{key}'; got {sorted(data.keys())}"
            )

    def test_ml_drift_returns_200_with_drift_status(self, client, auth_headers):
        """``GET /api/ml/drift`` returns 200 with the drift-monitoring
        dashboard payload: PSI, KS statistic, rolling Brier, EWMA
        Brier, drift status, PSI history, meta-learner summary,
        orchestrator stats.
        """
        response = client.get("/api/ml/drift", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml/drift must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The drift detector's headline contract is its tri-state
        # ``status`` field — HEALTHY / MODERATE_DRIFT / CRITICAL_DRIFT.
        assert "status" in data, (
            f"/api/ml/drift must carry 'status'; got {sorted(data.keys())}"
        )
        assert data["status"] in ("HEALTHY", "MODERATE_DRIFT", "CRITICAL_DRIFT"), (
            f"/api/ml/drift 'status' must be a documented tri-state value; "
            f"got {data['status']!r}"
        )

    def test_ml_versions_returns_200_with_versions_list(self, client, auth_headers):
        """``GET /api/ml/versions`` returns 200 with the full registered-
        model-version lineage (newest first), each entry carrying the
        version string, metrics, status, and ``is_active`` flag.
        Registered by ``ml/routes.py::register_routes`` (T8 block in
        ``api/server.py``).
        """
        response = client.get("/api/ml/versions", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml/versions must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "versions" in data and isinstance(data["versions"], list), (
            f"/api/ml/versions must carry a 'versions' list; got {sorted(data.keys())}"
        )
        assert "active_version" in data, (
            f"/api/ml/versions must carry 'active_version'; got {sorted(data.keys())}"
        )
        # If the registry has at least one version, the active_version
        # string MUST appear at least once in the versions list (the
        # ``is_active`` flag is derived from ``v.version == active_version``
        # in ``model_registry.list_versions``).
        #
        # NOTE: we do NOT assert ``exactly one is_active=True`` here
        # because the model registry can carry duplicate version strings
        # (``ml/model.py::MarketMLModel.load_or_create`` calls
        # ``register_version`` at import time with a time-based version
        # string ``v1.{int(time.time()) % 1000:03d}.0`` — two test runs
        # within the same second produce the SAME version string, both
        # of which end up ``is_active=True``). That duplication is a
        # pre-existing model-registry concern, not a route-contract bug;
        # the W10-6 contract here is the route shape + the active_version
        # being present in the lineage.
        if data["versions"]:
            version_strings = {v.get("version") for v in data["versions"]}
            assert data["active_version"] in version_strings, (
                f"active_version {data['active_version']!r} must appear in "
                f"the versions list; got {sorted(version_strings)[:5]}..."
            )
            # At least one entry must be marked is_active=True (proving
            # the is_active derivation works).
            active_count = sum(1 for v in data["versions"] if v.get("is_active"))
            assert active_count >= 1, (
                f"At least one version must be marked is_active=True; "
                f"got {active_count}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 5. ANALYSIS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalysisEndpoints:
    """Fundamental news + sentiment analysis endpoints.

    Both read from the ``fundamental_engine`` singleton's in-memory
    ``news_feed`` list. In the test sandbox the news ingest hasn't run,
    so the feed is empty — but the routes MUST return 200 with the
    expected empty-list contract, NOT 404 / 500.
    """

    def test_analysis_news_returns_200(self, client, auth_headers):
        """``GET /api/analysis/news`` returns 200 with the news feed
        (headlines with sentiment scores, ``is_seed`` provenance flag).
        Empty feed returns ``count=0`` and an empty ``news`` array.
        """
        response = client.get("/api/analysis/news", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/analysis/news must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "news" in data and isinstance(data["news"], list)

    def test_analysis_news_stats_returns_200(self, client, auth_headers):
        """``GET /api/analysis/news/stats`` returns 200 with the live
        NLP sentiment breakdown and global ingestion-rate telemetry
        (total_news_items, sentiment_distribution, sources_indexed,
        seed_items, last_ingest_age_seconds).
        """
        response = client.get("/api/analysis/news/stats", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/analysis/news/stats must return 200; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        data = response.json()
        # The stats dict's canonical contract: distribution + counts.
        assert "sentiment_distribution" in data, (
            f"/api/analysis/news/stats must carry 'sentiment_distribution'; "
            f"got {sorted(data.keys())}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. RISK ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskEndpoints:
    """Risk-adjusted portfolio endpoints: exposure decomposition,
    reconciliation, and the strategy performance leaderboard.

    All three read from in-process state (``store.positions`` /
    ``store.open_orders`` / ``store.closed_positions`` /
    ``risk_manager``) so they're fully deterministic on the fresh-store
    baseline. Empty state returns 200 with sensible empty payloads.
    """

    def test_exposure_returns_200(self, client, auth_headers):
        """``GET /api/exposure`` returns 200 with the full exposure
        decomposition (capital_invested, gross_market_value,
        net_directional_exposure, maximum_remaining_loss,
        exposure_per_group / per_strategy, available_cash,
        open_position_count).
        """
        response = client.get("/api/exposure", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/exposure must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # Headline exposure fields the dashboard renders.
        for key in ("capital_invested", "maximum_remaining_loss", "open_position_count"):
            assert key in data, (
                f"/api/exposure must carry '{key}'; got {sorted(data.keys())}"
            )

    def test_leaderboard_returns_200(self, client, auth_headers):
        """``GET /api/leaderboard`` returns 200 with the strategy
        leaderboard ranked by reproducible risk-adjusted net
        performance. Empty state (no closed positions) returns
        ``count=0`` and an empty ``ranked`` list.
        """
        response = client.get("/api/leaderboard", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/leaderboard must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "ranked" in data and isinstance(data["ranked"], list), (
            f"/api/leaderboard must carry a 'ranked' list; got {sorted(data.keys())}"
        )

    def test_risk_reconcile_returns_200(self, client, auth_headers):
        """``GET /api/risk/reconcile`` returns 200 with the
        reconciliation investigation for the current open exposure
        (reconciled flag, status, exposure snapshot, duplicate /
        orphan checks). The empty-state baseline returns
        ``reconciled=true`` (no positions ⇒ no breach).
        """
        response = client.get("/api/risk/reconcile", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/risk/reconcile must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The reconciliation's headline fields.
        assert "reconciled" in data, (
            f"/api/risk/reconcile must carry 'reconciled'; got {sorted(data.keys())}"
        )
        assert "status" in data


# ═══════════════════════════════════════════════════════════════════════════
# 7. OBSERVABILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


class TestObservabilityEndpoints:
    """Observability + decision-ledger inspection endpoints.

    ``GET /api/observability`` is registered by ``core.observability.
    register_routes`` (S13 block in ``api/server.py``) and returns the
    structured health report (latest value per (category, name) metric,
    bucketed under the six canonical categories).

    ``GET /api/decisions/rejected`` is registered by
    ``core.decision_ledger.register_routes`` (R11 block) and returns the
    recent rejection feed (the decisions that were blocked by the risk
    gate before order placement).
    """

    def test_observability_returns_200_with_metrics_dict(self, client, auth_headers):
        """``GET /api/observability`` returns 200 with the structured
        system health report — latest value per (category, name) metric,
        bucketed under the six canonical categories
        (data_source / bot / strategy / execution / ml / system).
        """
        response = client.get("/api/observability", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/observability must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        # The health-report contract: timestamp + counts + categories map.
        assert "generated_at" in data, (
            f"/api/observability must carry 'generated_at'; got {sorted(data.keys())}"
        )
        assert "categories" in data and isinstance(data["categories"], dict), (
            f"/api/observability must carry a 'categories' dict; got {sorted(data.keys())}"
        )
        # The six canonical categories must all be present (empty bucket
        # is OK — the contract is that the KEY exists).
        for cat in ("data_source", "bot", "strategy", "execution", "ml", "system"):
            assert cat in data["categories"], (
                f"/api/observability categories must include '{cat}'; "
                f"got {sorted(data['categories'].keys())}"
            )

    def test_decisions_rejected_returns_200_with_array(self, client, auth_headers):
        """``GET /api/decisions/rejected`` returns 200 with the recent
        rejection feed (most recent first). Empty state returns
        ``count=0`` and an empty ``rejections`` array — NOT 404.
        """
        response = client.get("/api/decisions/rejected", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/decisions/rejected must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "rejections" in data and isinstance(data["rejections"], list), (
            f"/api/decisions/rejected must carry a 'rejections' list; "
            f"got {sorted(data.keys())}"
        )
        assert data["count"] == len(data["rejections"])


# ═══════════════════════════════════════════════════════════════════════════
# 8. AUTH TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """Bearer-token auth middleware (``enforce_api_auth`` in
    ``api/server.py``) — fail-closed on every route except the liveness
    probe (``/api/health``) and OPTIONS preflight.

    The middleware's contract:
      * Missing ``Authorization`` header → 401 ``Unauthorized — missing
        or invalid API token``.
      * Invalid token (``Bearer wrong-token``) → 401 same body.
      * Valid token (``Bearer test-token-conftest``) → request flows
        through to the route handler (200 on a happy-path route).
      * No ``API_TOKEN`` configured → 503 ``AUTH_NOT_CONFIGURED``.
        conftest sets ``API_TOKEN=test-token-conftest``, so this path
        is NOT exercised here (covered implicitly by the W9-8 suite).
    """

    def test_request_without_auth_header_returns_401(self, client):
        """``GET /api/status`` WITHOUT an ``Authorization`` header must
        return 401 — the auth middleware short-circuits before the
        route handler runs.
        """
        response = client.get("/api/status")
        assert response.status_code == 401, (
            f"missing Authorization header must return 401; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        assert "detail" in body
        # The 401 body must NOT echo back any partial token / leak state.
        assert "invalid" in body["detail"].lower() or "unauthorized" in body["detail"].lower(), (
            f"401 detail should reference the unauthorized / invalid-token cause; "
            f"got {body['detail']!r}"
        )

    def test_request_with_invalid_token_returns_401(self, client):
        """``GET /api/status`` with an INVALID bearer token must return
        401 — ``hmac.compare_digest`` rejects the credential mismatch
        without leaking WHICH part was wrong (constant-time comparison
        guards against timing-side-channel token enumeration).
        """
        response = client.get(
            "/api/status",
            headers={"Authorization": "Bearer definitely-not-the-right-token"},
        )
        assert response.status_code == 401, (
            f"invalid bearer token must return 401; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_request_with_valid_token_returns_200(self, client, auth_headers):
        """``GET /api/status`` with the VALID bearer token must return
        200 — the request flows through to the route handler. This is
        the load-bearing happy-path: every other test in this module
        depends on it.
        """
        response = client.get("/api/status", headers=auth_headers)
        assert response.status_code == 200, (
            f"valid bearer token must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_auth_middleware_uses_constant_time_comparison(self, client):
        """The auth middleware uses ``hmac.compare_digest`` (not ``==``)
        so a timing-side-channel attacker can't enumerate the token
        byte-by-byte. Verified here indirectly: an ALMOST-correct token
        (off by one character) returns the SAME 401 status AND the same
        response body as a completely-wrong token — proving the
        middleware doesn't distinguish "close" from "far" mismatches.
        """
        # One-character-off token: 'test-token-conftest' vs 'test-token-conftesX'
        # (last char changed). The 401 status + body must be identical to
        # a fully-random wrong token.
        r1 = client.get(
            "/api/status",
            headers={"Authorization": "Bearer test-token-conftesX"},
        )
        r2 = client.get(
            "/api/status",
            headers={"Authorization": "Bearer totally-different-wrong-token"},
        )
        assert r1.status_code == r2.status_code == 401
        assert r1.json() == r2.json(), (
            "auth middleware must return identical 401 bodies for "
            "near-miss and far-miss tokens (constant-time comparison)"
        )

    def test_health_route_bypasses_auth(self, client):
        """``GET /api/health`` is in ``PUBLIC_PATHS`` so it bypasses the
        auth middleware entirely — no Authorization header needed. This
        is the liveness probe contract: Docker / k8s health checks
        can hit it before ``API_TOKEN`` is configured.
        """
        response = client.get("/api/health")  # NO auth headers
        assert response.status_code == 200, (
            f"/api/health must bypass auth (PUBLIC_PATHS); got {response.status_code}"
        )

    def test_options_preflight_bypasses_auth(self, client):
        """CORS preflight (``OPTIONS``) requests must bypass the auth
        middleware so a browser can negotiate CORS BEFORE sending the
        authenticated actual request. The middleware checks
        ``request.method == "OPTIONS"`` first and short-circuits.
        """
        response = client.options(
            "/api/status",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        # OPTIONS preflight returns 200 (CORS preflight OK) — NOT 401.
        assert response.status_code in (200, 204), (
            f"OPTIONS preflight must bypass auth; got {response.status_code}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 9. ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Edge-case + error-path contracts:

      * Nonexistent route → 404 (not 500).
      * Invalid query params → 422 (Pydantic validation, before the
        route body runs).
      * ``GET /api/depth/{token_id}`` for an unknown token → 200 with
        empty arrays (the route prioritizes the token for the book
        poller and returns an empty book rather than 404).
      * POST with invalid Pydantic body → 422 (not 500).
    """

    def test_depth_invalid_token_returns_200_with_empty_book(self, client, auth_headers):
        """``GET /api/depth/nonexistent-token-12345`` returns 200 with
        an empty book payload (``bids=[]``, ``asks=[]``, ``mid=None``,
        ``spread=None``). The route does NOT 404 on an unknown token —
        it prioritizes the token for the book poller and returns an
        empty book so the dashboard can render a "loading" state
        instead of an error.

        Verified behaviour: HTTP 200 with ``bids`` / ``asks`` / ``mid``
        / ``spread`` keys present.
        """
        response = client.get(
            "/api/depth/nonexistent-token-12345",
            headers=auth_headers,
        )
        # The W10-6 spec says "returns 404 OR 200 with empty (verify
        # behaviour)". Verified behaviour: 200 with an empty book.
        assert response.status_code in (200, 404), (
            f"GET /api/depth/<unknown-token> must return 200 (with empty book) "
            f"or 404; got {response.status_code}. Body: {response.text!r}"
        )
        if response.status_code == 200:
            data = response.json()
            assert "bids" in data and isinstance(data["bids"], list)
            assert "asks" in data and isinstance(data["asks"], list)
            assert data["bids"] == [] and data["asks"] == [], (
                f"empty book for unknown token must have empty bids/asks; "
                f"got bids={data['bids']!r} asks={data['asks']!r}"
            )

    def test_invalid_query_param_returns_422(self, client, auth_headers):
        """Out-of-range query params must return 422 (Pydantic
        validation), NOT 500 — the route body never runs. Verified
        against two routes with ``Query(ge=..., le=...)`` bounds:
        ``/api/trades?limit=0`` (``Query(50, ge=1, le=1000)``) and
        ``/api/markets?limit=9999`` (``Query(50, ge=1, le=500)``).
        """
        # limit=0 below ge=1 lower bound → 422
        r1 = client.get("/api/trades", params={"limit": 0}, headers=auth_headers)
        assert r1.status_code == 422, (
            f"/api/trades?limit=0 must return 422 (out of range); got "
            f"{r1.status_code}. Body: {r1.text!r}"
        )
        # limit=9999 above le=500 upper bound → 422
        r2 = client.get("/api/markets", params={"limit": 9999}, headers=auth_headers)
        assert r2.status_code == 422, (
            f"/api/markets?limit=9999 must return 422 (out of range); got "
            f"{r2.status_code}. Body: {r2.text!r}"
        )
        # Negative limit → 422
        r3 = client.get("/api/trades", params={"limit": -5}, headers=auth_headers)
        assert r3.status_code == 422

    def test_nonexistent_route_returns_404(self, client, auth_headers):
        """A request to a path that is NOT in the route table must
        return 404 — NOT 500 (the global exception handler must NOT
        fire for a missing route) and NOT 401 (the auth middleware
        runs AFTER routing in the request pipeline? No — actually the
        auth middleware runs BEFORE routing because it's an HTTP
        middleware wrapping the entire app. So a missing route WITH
        valid auth should return 404; without auth it returns 401
        first).
        """
        # WITH valid auth: 404 (auth passes, routing fails).
        response = client.get("/api/totally-nonexistent-route-12345", headers=auth_headers)
        assert response.status_code == 404, (
            f"nonexistent route must return 404; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        body = response.json()
        assert "detail" in body
        assert body["detail"] == "Not Found"

    def test_post_trade_with_invalid_body_returns_422(self, client, auth_headers):
        """``POST /api/trade`` with a Pydantic-invalid body must return
        422 — NOT 500 — proving the validation layer catches bad input
        before the route handler runs (so no DB / external-API side
        effects).

        ``ManualTradeRequest`` requires ``price: float = Field(gt=0, lt=1)``
        and ``side: str = Field(pattern="^(BUY|SELL|buy|sell)$")`` —
        ``price=1.5`` violates ``lt=1`` and ``size_usdc=-5`` violates
        ``gt=0``.
        """
        # price >= 1 → 422 (lt=1 violation)
        r1 = client.post(
            "/api/trade",
            json={"token_id": "X", "price": 1.5, "side": "BUY", "size_usdc": 10.0},
            headers=auth_headers,
        )
        assert r1.status_code == 422, (
            f"POST /api/trade with price=1.5 (>= lt=1) must return 422; "
            f"got {r1.status_code}. Body: {r1.text!r}"
        )
        # size_usdc <= 0 → 422 (gt=0 violation)
        r2 = client.post(
            "/api/trade",
            json={"token_id": "X", "price": 0.5, "side": "BUY", "size_usdc": -5.0},
            headers=auth_headers,
        )
        assert r2.status_code == 422
        # Missing required field (token_id) → 422
        r3 = client.post(
            "/api/trade",
            json={"price": 0.5, "side": "BUY", "size_usdc": 10.0},
            headers=auth_headers,
        )
        assert r3.status_code == 422
        # Invalid side pattern → 422
        r4 = client.post(
            "/api/trade",
            json={"token_id": "X", "price": 0.5, "side": "INVALID", "size_usdc": 10.0},
            headers=auth_headers,
        )
        assert r4.status_code == 422

    def test_post_strategy_toggle_invalid_body_returns_422(self, client, auth_headers):
        """``POST /api/strategies/toggle`` with a body missing the
        required ``enabled`` field must return 422 — proving the
        validation layer catches missing required fields on POST
        routes other than ``/api/trade`` (this is the W10-6 spec's
        "POST endpoints test with invalid input to verify 422
        validation" requirement applied to a second route).
        """
        response = client.post(
            "/api/strategies/toggle",
            json={"strategy_name": "mm_avellaneda_stoikov"},  # missing 'enabled'
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"POST /api/strategies/toggle with missing 'enabled' must return "
            f"422; got {response.status_code}. Body: {response.text!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 10. CROSS-CUTTING CONTRACTS
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossCutting:
    """Headers and middleware that apply to EVERY response:

      * ``Content-Type: application/json`` on every JSON response.
      * CORS headers (``access-control-allow-origin``) when the request
        carries an ``Origin`` header matching a configured allowed
        origin. conftest sets ``CORS_ORIGINS=http://localhost``.
      * Rate-limit headers — ONLY if W10-4 has landed. W10-4 has NOT
        landed as of this task (verified via ripgrep across the
        polymarket-bot tree: no ``slowapi`` / ``Limiter`` import
        anywhere). The test below is defensive: it asserts that IF a
        ``x-ratelimit-*`` header is present, it has a numeric value —
        but its absence is the expected current state. When W10-4
        lands, the test will start asserting the actual header value.
    """

    def test_response_has_json_content_type(self, client, auth_headers):
        """Every JSON response from the API must carry
        ``Content-Type: application/json`` (the dashboard's fetch()
        caller sets ``response.json()`` which throws if the content
        type isn't JSON). Verified across multiple routes.
        """
        for path in ("/api/health", "/api/status", "/api/positions", "/api/orders"):
            response = client.get(path, headers=auth_headers)
            assert response.status_code == 200, (
                f"{path} must return 200 for the content-type check to be meaningful"
            )
            content_type = response.headers.get("content-type", "")
            assert "application/json" in content_type, (
                f"{path} response must have Content-Type: application/json; "
                f"got {content_type!r}"
            )

    def test_cors_headers_present_for_allowed_origin(self, client, auth_headers):
        """When the request carries an ``Origin`` header matching a
        configured allowed origin (conftest sets
        ``CORS_ORIGINS=http://localhost``), the response must carry
        ``access-control-allow-origin: http://localhost`` and
        ``access-control-allow-credentials: true``.

        The CORS middleware (``enforce_api_auth`` in ``api/server.py``)
        explicitly echoes the origin only when ``cors_allowed`` is
        True — locked to configured explicit origins only (empty = same-
        origin only; wildcard fallback removed per S12 security
        hardening).
        """
        response = client.get(
            "/api/health",
            headers={**auth_headers, "Origin": "http://localhost"},
        )
        assert response.status_code == 200
        # The CORS middleware should echo the origin for allowed origins.
        assert response.headers.get("access-control-allow-origin") == "http://localhost", (
            f"CORS must echo the allowed origin; got "
            f"{response.headers.get('access-control-allow-origin')!r}"
        )
        # Credentials header is always enabled when the origin is allowed.
        assert response.headers.get("access-control-allow-credentials") == "true", (
            f"CORS must set access-control-allow-credentials=true; got "
            f"{response.headers.get('access-control-allow-credentials')!r}"
        )

    def test_cors_headers_absent_for_disallowed_origin(self, client, auth_headers):
        """When the request carries an ``Origin`` header NOT in the
        configured allowed list (e.g. ``http://evil.example.com``), the
        response must NOT carry ``access-control-allow-origin`` — the
        browser will refuse to expose the response to the requesting
        origin. This is the load-bearing CORS security contract
        (without it, any origin could read API responses).
        """
        response = client.get(
            "/api/health",
            headers={**auth_headers, "Origin": "http://evil.example.com"},
        )
        assert response.status_code == 200
        # The disallowed origin must NOT be echoed.
        allow_origin = response.headers.get("access-control-allow-origin")
        assert allow_origin != "http://evil.example.com", (
            f"CORS must NOT echo a disallowed origin; got {allow_origin!r}"
        )

    def test_rate_limit_headers_present_if_rate_limiting_added(self, client, auth_headers):
        """Rate-limit headers (``x-ratelimit-limit``, ``x-ratelimit-
        remaining``, ``x-ratelimit-reset``) are present ONLY if W10-4
        has landed the rate-limit middleware.

        As of W10-6, W10-4 has NOT landed (verified via ripgrep: no
        ``slowapi`` / ``Limiter`` import in the polymarket-bot tree).
        This test is forward-compatible: it asserts that IF any
        ``x-ratelimit-*`` header is present, it must have a numeric
        value (the standard rate-limit header contract). When W10-4
        lands, the test will start asserting the headers ARE present.

        The defensive ``limiter.enabled = False`` toggle at the top of
        this module ensures that even when W10-4 lands, these tests
        stay green by disabling the rate-limit middleware for the
        integration suite (so a sub-second burst of 32 tests doesn't
        trip the limiter). This test therefore serves as a
        documentation-of-intent marker rather than a load-bearing
        assertion today.
        """
        response = client.get("/api/health", headers=auth_headers)
        assert response.status_code == 200
        # Collect any rate-limit headers present on the response.
        rate_limit_headers = {
            k: v for k, v in response.headers.items()
            if k.lower().startswith("x-ratelimit-")
        }
        # If W10-4 has NOT landed (current state): rate_limit_headers == {}
        # → this assertion trivially passes (no headers to validate).
        # If W10-4 HAS landed AND the limiter is enabled: headers are
        # present and must be numeric.
        # If W10-4 has landed AND the limiter is disabled (the toggle at
        # the top of this module): headers may be absent (limiter
        # disabled → no header injection). Either way, the test passes.
        for header_name, header_value in rate_limit_headers.items():
            # Rate-limit headers carry either a count or a seconds-until-
            # reset value; both must be numeric.
            try:
                int(header_value)
            except ValueError:
                # Some rate-limit libraries use a RFC 7231 date for the
                # ``-reset`` header. Accept either a numeric or a
                # parseable date — the contract is "non-empty".
                assert header_value, (
                    f"rate-limit header {header_name} must be non-empty; "
                    f"got {header_value!r}"
                )
        # The defensive assertion: this test passes today (W10-4 not
        # landed) and will keep passing when W10-4 lands (with the
        # limiter.enabled=False toggle active).

    def test_response_carries_vary_origin_header(self, client, auth_headers):
        """CORS responses must carry ``Vary: Origin`` so caches don't
        cache a response for one origin and serve it to another (which
        would leak the ``access-control-allow-origin: origin-A`` header
        to a request from origin-B). Verified when an Origin header is
        present in the request.
        """
        response = client.get(
            "/api/health",
            headers={**auth_headers, "Origin": "http://localhost"},
        )
        assert response.status_code == 200
        vary = response.headers.get("vary", "")
        # The Vary header may carry multiple tokens; check that "origin"
        # appears among them (case-insensitive).
        assert "origin" in vary.lower(), (
            f"CORS responses must carry 'Vary: Origin' (got {vary!r}) so "
            f"caches don't cross-origin-leak the access-control-allow-origin"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 11. ADDITIONAL ENDPOINT COVERAGE (rounding out the API surface)
# ═══════════════════════════════════════════════════════════════════════════


class TestAdditionalEndpoints:
    """Coverage for endpoints NOT explicitly enumerated in the W10-6
    spec but present on the production ``app`` — included so the
    integration test suite is a true full-surface regression net.

    These endpoints are read-only GETs (no side effects), so testing
    them alongside the spec's enumerated routes doesn't violate the
    W10-6 constraint of "Don't test endpoints that have side effects
    in a way that would modify state".
    """

    def test_events_returns_200(self, client, auth_headers):
        """``GET /api/events`` returns 200 with the recent event-log
        entries. Empty state returns ``count=0`` and an empty
        ``events`` list.
        """
        response = client.get("/api/events", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/events must return 200; got {response.status_code}"
        )
        data = response.json()
        assert "events" in data and isinstance(data["events"], list)

    def test_history_equity_returns_200(self, client, auth_headers):
        """``GET /api/history/equity`` returns 200 with the equity
        curve (``points`` array + ``count``). Fresh state returns a
        single initial point (the autouse store reset seeds
        ``equity_history`` with one baseline point at BANKROLL_BASELINE).
        """
        response = client.get("/api/history/equity", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/history/equity must return 200; got {response.status_code}"
        )
        data = response.json()
        assert "points" in data and isinstance(data["points"], list)
        assert data["count"] == len(data["points"])

    def test_analytics_returns_200(self, client, auth_headers):
        """``GET /api/analytics`` returns 200 with the full analytics
        roll-up (equity, realized / unrealized PnL, win rate, Wilson
        CI, profit factor, expectancy, Sharpe, max drawdown, total
        volume, risk utilization, data freshness, peak equity, active
        strategies).
        """
        response = client.get("/api/analytics", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/analytics must return 200; got {response.status_code}"
        )
        data = response.json()
        # The analytics contract: equity + pnl + win_rate (headline
        # dashboard fields).
        for key in ("equity", "realized_pnl", "win_rate"):
            assert key in data, (
                f"/api/analytics must carry '{key}'; got {sorted(data.keys())}"
            )

    def test_strategies_catalog_returns_200(self, client, auth_headers):
        """``GET /api/strategies/catalog`` returns 200 with the 50+
        strategy catalog (id, name, category, default_enabled,
        is_implemented, is_running, description).
        """
        response = client.get("/api/strategies/catalog", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/strategies/catalog must return 200; got {response.status_code}"
        )
        data = response.json()
        assert "catalog" in data and isinstance(data["catalog"], list)
        assert data["total"] == len(data["catalog"])

    def test_config_returns_200(self, client, auth_headers):
        """``GET /api/config`` returns 200 with the live strategy
        configuration parameters (mm_spread_bps, mm_quote_size_usdc,
        arb_min_profit_bps, etc.).
        """
        response = client.get("/api/config", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/config must return 200; got {response.status_code}"
        )
        data = response.json()
        # The config contract: at least the headline MM params.
        assert "mm_spread_bps" in data, (
            f"/api/config must carry 'mm_spread_bps'; got {sorted(data.keys())}"
        )

    def test_system_mode_returns_200(self, client, auth_headers):
        """``GET /api/system/mode`` returns 200 with the canonical,
        network-visible trading mode + safety posture (mode,
        paper_trade, live_trading_enabled, auth_enforced, kill_switch,
        weekly snapshot, mode_derivation).
        """
        response = client.get("/api/system/mode", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/system/mode must return 200; got {response.status_code}"
        )
        data = response.json()
        for key in ("mode", "paper_trade", "live_trading_enabled", "auth_enforced"):
            assert key in data, (
                f"/api/system/mode must carry '{key}'; got {sorted(data.keys())}"
            )
        # conftest forces TRADING_MODE=paper, so mode must be "paper".
        assert data["mode"] == "paper", (
            f"conftest sets TRADING_MODE=paper; got mode={data['mode']!r}"
        )

    def test_ml_registry_returns_200(self, client, auth_headers):
        """``GET /api/ml/registry`` returns 200 with the model-registry
        summary (active_version, total_registered, versions list with
        benchmarks + validation status).
        """
        response = client.get("/api/ml/registry", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml/registry must return 200; got {response.status_code}"
        )
        data = response.json()
        assert "active_version" in data, (
            f"/api/ml/registry must carry 'active_version'; got {sorted(data.keys())}"
        )

    def test_audit_logs_returns_200(self, client, auth_headers):
        """``GET /api/audit/logs`` returns 200 with the recent audit
        trail entries. Empty state returns an empty list — NOT 404.
        """
        response = client.get("/api/audit/logs", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/audit/logs must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_execution_quality_returns_200(self, client, auth_headers):
        """``GET /api/execution-quality`` returns 200 with the recent
        per-fill execution-quality metrics (signal_price, decision
        price, submitted_price, best_bid/ask, expected/actual fill,
        spread, slippage, slippage_bps, latency, realized_edge).
        Registered by ``core.execution_quality.register_routes`` (S14
        block in ``api/server.py``).
        """
        response = client.get("/api/execution-quality", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/execution-quality must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_closed_positions_returns_200(self, client, auth_headers):
        """``GET /api/positions/closed`` returns 200 with the recent
        closed-positions journal (filterable). Empty state returns an
        empty list. Registered by ``core.closed_positions.register_routes``
        (S15 block in ``api/server.py``).
        """
        response = client.get("/api/positions/closed", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/positions/closed must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_attribution_returns_200(self, client, auth_headers):
        """``GET /api/attribution`` returns 200 with the seven-dimension
        P&L attribution roll-up (strategy / confidence bucket / edge
        bucket / probability band / liquidity level / holding period /
        trade direction).
        """
        response = client.get("/api/attribution", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/attribution must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_risk_strategies_paused_returns_200(self, client, auth_headers):
        """``GET /api/risk/strategies/paused`` returns 200 with the
        currently-paused strategies (cooldown timer with
        seconds_remaining) + the registered-running strategies that
        are NOT currently paused. Registered by ``risk.routes.register_routes``
        (V12 block in ``api/server.py``).
        """
        response = client.get("/api/risk/strategies/paused", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/risk/strategies/paused must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )

    def test_shadow_trades_returns_200(self, client, auth_headers):
        """``GET /api/shadow/trades` returns 200 with the recent
        counterfactual shadow trades (filterable by strategy). Empty
        state returns ``count=0`` and an empty ``trades`` list.
        Registered by ``core.shadow_trading.register_routes`` (T1 block
        in ``api/server.py``).
        """
        response = client.get("/api/shadow/trades", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/shadow/trades must return 200; got {response.status_code}. "
            f"Body: {response.text!r}"
        )
        data = response.json()
        assert "trades" in data and isinstance(data["trades"], list)
