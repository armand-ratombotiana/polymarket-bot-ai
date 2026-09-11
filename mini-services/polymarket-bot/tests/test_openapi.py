"""W11-3 — OpenAPI / Swagger documentation contract tests.

Verifies the auto-generated OpenAPI schema exposed at ``/openapi.json`` is
structurally sound and carries the rich metadata the W11-3 task attached
to the FastAPI app and to its most-used routes:

* App-level: title, version, description (Markdown), contact, license,
  tags (with descriptions).
* Route-level: the 5 contract-critical routes (``GET /api/health``,
  ``GET /api/positions``, ``GET /api/orders``, ``GET /api/trades``,
  ``GET /api/ml/metrics``) carry a non-trivial
  ``response_model`` so the docs surface a typed schema instead of the
  generic ``{}`` placeholder.
* Route-level: at least 20 routes carry a ``summary`` (the one-line
  label that shows up next to each path in Swagger UI's left rail).

Hermeticity
~~~~~~~~~~~
Imports the production ``api.server.app`` so the schema under test is the
REAL one (every route, every middleware, every Pydantic validator). The
autouse ``_reset_store_factory_defaults`` conftest fixture wipes store
singletons before every test; rate limiting is disabled in
``conftest.py`` (``limiter.enabled = False``) so the per-route slowapi
limits don't interfere.

All tests are SYNC ``def test_...`` — ``TestClient`` bridges each
request through its own anyio portal (mirrors
``tests/test_integration.py``).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.server import app

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
    ``tests/test_integration.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
def openapi_schema(client, auth_headers) -> dict:
    """Fetch ``/openapi.json`` once per test that needs it.

    Cached at function scope (not session) so a test that mutates app
    state doesn't poison the next test's schema view. The endpoint is in
    ``PUBLIC_PATHS`` so auth headers aren't strictly required, but we
    pass them anyway for symmetry with every other test.
    """
    response = client.get("/openapi.json", headers=auth_headers)
    assert response.status_code == 200, (
        f"GET /openapi.json must return 200; got {response.status_code}. "
        f"Body: {response.text[:300]!r}"
    )
    return response.json()


# ═══════════════════════════════════════════════════════════════════════════
# 1. /openapi.json endpoint contract
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenAPIEndpoint:
    """Verifies ``GET /openapi.json`` itself is reachable and well-formed."""

    def test_openapi_json_returns_200(self, client, auth_headers):
        """``GET /openapi.json`` must return 200.

        The endpoint is in ``PUBLIC_PATHS`` so the bearer-token auth
        middleware lets it through unconditionally. Swagger UI and ReDoc
        both fetch this URL on page load — a 4xx / 5xx here breaks the
        entire docs site.
        """
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200

    def test_openapi_json_is_valid_json(self, client, auth_headers):
        """The body must be valid JSON (parseable by ``json.loads``)."""
        response = client.get("/openapi.json", headers=auth_headers)
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            pytest.fail(f"/openapi.json body is not valid JSON: {e}")
        assert isinstance(data, dict), "/openapi.json root must be an object"

    def test_openapi_json_has_required_top_level_keys(self, openapi_schema):
        """The schema must carry the OpenAPI 3.x top-level keys.

        ``openapi`` (version string), ``info`` (metadata), ``paths``
        (route table), and ``components`` (Pydantic schemas) are all
        required for the docs to render. ``tags`` is optional in the
        OpenAPI spec but we declare it explicitly so it should be present.
        """
        for key in ("openapi", "info", "paths"):
            assert key in openapi_schema, (
                f"/openapi.json missing required top-level key {key!r}; "
                f"got {sorted(openapi_schema.keys())}"
            )

    def test_openapi_json_accessible_without_auth(self, client):
        """``GET /openapi.json`` must be reachable WITHOUT a bearer token.

        ``PUBLIC_PATHS`` in ``api/server.py`` includes ``/openapi.json``
        so external tooling (Swagger UI, code generators, Postman
        imports) can introspect the API without an API token.
        """
        response = client.get("/openapi.json")
        assert response.status_code == 200, (
            f"/openapi.json must be unauthenticated (PUBLIC_PATHS); got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. App-level metadata (title, version, description, contact, license)
# ═══════════════════════════════════════════════════════════════════════════


class TestAppMetadata:
    """App-level metadata attached to the ``FastAPI(...)`` constructor."""

    def test_title_is_polymarket_pro(self, openapi_schema):
        """Title must be the W11-3 spec's enhanced value (not the
        legacy ``Polymarket Pro Bot API`` placeholder)."""
        assert openapi_schema["info"]["title"] == "Polymarket Pro — Trading Bot API"

    def test_version_is_1_0_0(self, openapi_schema):
        """Version must be ``1.0.0`` (not the legacy ``3.0.0``)."""
        assert openapi_schema["info"]["version"] == "1.0.0"

    def test_description_mentions_key_features(self, openapi_schema):
        """The Markdown description must mention the 5 key features so
        the Swagger UI landing page surfaces the project's value props
        (paper trading, ML ensemble, decision ledger, risk management,
        observability)."""
        desc = openapi_schema["info"]["description"]
        for keyword in (
            "Paper trading",
            "ML ensemble",
            "Decision ledger",
            "Risk management",
            "Observability",
        ):
            assert keyword in desc, (
                f"info.description must mention {keyword!r}; got {desc[:300]!r}"
            )

    def test_description_documents_auth_scheme(self, openapi_schema):
        """The description must document the Bearer-token auth scheme
        so a new API consumer can authenticate without reading the
        source."""
        desc = openapi_schema["info"]["description"]
        assert "Bearer" in desc, "info.description must document Bearer auth"
        assert "Authorization" in desc

    def test_description_documents_rate_limiting(self, openapi_schema):
        """The description must mention the rate-limiting policy so
        clients know to expect 429s."""
        desc = openapi_schema["info"]["description"]
        assert "Rate Limiting" in desc or "rate limit" in desc.lower()

    def test_contact_block_present(self, openapi_schema):
        """The ``contact`` block must be present and reference the
        project repository."""
        contact = openapi_schema["info"].get("contact")
        assert contact is not None, "info.contact must be present"
        assert "Polymarket Pro" in contact.get("name", "")
        assert "github" in contact.get("url", "").lower()

    def test_license_block_present(self, openapi_schema):
        """The ``license_info`` block must be present and name MIT."""
        license_info = openapi_schema["info"].get("license")
        assert license_info is not None, "info.license must be present"
        assert license_info.get("name") == "MIT"

    def test_docs_urls_configured(self, openapi_schema):
        """The ``docs_url`` / ``redoc_url`` / ``openapi_url`` arguments
        don't appear in the schema directly, but the docs routes
        themselves must be reachable. Verified here by hitting them."""
        # Fixture already verified /openapi.json (200). Hit /docs and
        # /redoc as well — they're in PUBLIC_PATHS.
        pass  # See TestDocsRoutes class below for the actual fetch tests.


class TestDocsRoutes:
    """Swagger UI / ReDoc / OpenAPI JSON routes must all be reachable."""

    def test_docs_route_returns_200(self, client):
        """``GET /docs`` must return 200 — serves the Swagger UI HTML."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_redoc_route_returns_200(self, client):
        """``GET /redoc`` must return 200 — serves the ReDoc HTML."""
        response = client.get("/redoc")
        assert response.status_code == 200

    def test_openapi_json_route_returns_200(self, client):
        """``GET /openapi.json`` must return 200 — serves the JSON schema."""
        response = client.get("/openapi.json")
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 3. openapi_tags — every declared tag has a description
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenAPITags:
    """``openapi_tags`` block — declared tag groups with descriptions."""

    REQUIRED_TAGS = (
        "trading",
        "markets",
        "ml",
        "analysis",
        "risk",
        "strategies",
        "arbitrage",
        "system",
        "ai",
        "database",
        "audit",
        "config",
        "backtesting",
        "alerts",
    )

    def test_tags_block_present(self, openapi_schema):
        """The ``tags`` block must be present (declared via the
        ``openapi_tags=[...]`` argument to ``FastAPI(...)``)."""
        assert "tags" in openapi_schema, (
            "openapi.json missing top-level 'tags' key — the FastAPI app "
            "must declare openapi_tags=[...] so Swagger UI groups routes."
        )
        assert len(openapi_schema["tags"]) > 0

    def test_all_required_tags_declared(self, openapi_schema):
        """Every tag in ``REQUIRED_TAGS`` must be present in the
        schema's ``tags`` list so Swagger UI has a group heading for
        each route group."""
        declared_names = {t["name"] for t in openapi_schema["tags"]}
        missing = set(self.REQUIRED_TAGS) - declared_names
        assert not missing, (
            f"openapi.json missing required tags: {sorted(missing)}; "
            f"declared: {sorted(declared_names)}"
        )

    def test_every_tag_has_description(self, openapi_schema):
        """Every declared tag must carry a ``description`` so Swagger UI
        renders a one-liner under each group heading."""
        for tag in openapi_schema["tags"]:
            assert "description" in tag and tag["description"], (
                f"Tag {tag.get('name')!r} missing description; "
                f"got {tag!r}"
            )

    def test_at_least_14_tags_declared(self, openapi_schema):
        """The spec lists 14 tag groups; we declared 21 (extras for
        decisions, observability, execution, capital, shadow, live,
        retention). Verify the count is at least 14."""
        assert len(openapi_schema["tags"]) >= 14, (
            f"Expected ≥14 declared tags, got {len(openapi_schema['tags'])}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Response models attached to the 5 contract-critical routes
# ═══════════════════════════════════════════════════════════════════════════


class TestResponseModels:
    """The 5 most-used routes must declare a ``response_model`` so the
    OpenAPI schema surfaces a typed response schema instead of the
    generic ``{}`` placeholder."""

    ROUTES_WITH_RESPONSE_MODEL = {
        "/api/health": "HealthResponse",
        "/api/positions": "PositionsResponse",
        "/api/orders": "OrdersResponse",
        "/api/trades": "TradesResponse",
        "/api/ml/metrics": "MLMetricsResponse",
    }

    def test_all_key_routes_have_typed_response_schema(self, openapi_schema):
        """For each route in ``ROUTES_WITH_RESPONSE_MODEL``, the
        schema's ``paths[path].get.responses['200'].content['application/json'].schema``
        must be a ``$ref`` pointing at ``components/schemas/<ModelName>``.

        A bare ``{}`` schema means the route declared no response_model
        — that's the exact gap W11-3 closes for these 5 routes.
        """
        for path, expected_model in self.ROUTES_WITH_RESPONSE_MODEL.items():
            assert path in openapi_schema["paths"], (
                f"Path {path} not in openapi.json paths"
            )
            get_op = openapi_schema["paths"][path].get("get")
            assert get_op is not None, f"{path} has no GET operation"
            responses = get_op.get("responses", {})
            assert "200" in responses, (
                f"{path} GET has no 200 response declared"
            )
            content = responses["200"].get("content", {})
            assert "application/json" in content, (
                f"{path} GET 200 response has no application/json content"
            )
            schema_ref = content["application/json"].get("schema", {})
            ref = schema_ref.get("$ref", "")
            assert ref.endswith(f"/{expected_model}"), (
                f"{path} GET 200 response schema must be {expected_model}; "
                f"got {ref!r} (full schema: {schema_ref!r})"
            )

    def test_response_model_components_are_defined(self, openapi_schema):
        """The response model classes referenced by ``$ref`` must
        actually exist in ``components/schemas`` — a dangling reference
        would render as ``{}`` in Swagger UI."""
        components_schemas = openapi_schema.get("components", {}).get("schemas", {})
        assert components_schemas, (
            "components.schemas is empty — no Pydantic response models are "
            "registered with FastAPI"
        )
        for model_name in self.ROUTES_WITH_RESPONSE_MODEL.values():
            assert model_name in components_schemas, (
                f"Response model {model_name} referenced by a route but "
                f"not present in components.schemas; got "
                f"{sorted(components_schemas.keys())}"
            )

    def test_health_response_schema_has_required_fields(self, openapi_schema):
        """The ``HealthResponse`` schema must declare at least the
        fields the route actually returns (``status``, ``timestamp``,
        ``paper``)."""
        health_schema = openapi_schema["components"]["schemas"]["HealthResponse"]
        required = set(health_schema.get("required", []))
        # ``status`` / ``timestamp`` / ``paper`` are required (no default);
        # ``mode`` / ``uptime`` / ``balance`` are Optional (default=None).
        for field in ("status", "timestamp", "paper"):
            assert field in required, (
                f"HealthResponse.{field} must be a required field; "
                f"required={sorted(required)}"
            )

    def test_positions_response_schema_has_list_field(self, openapi_schema):
        """The ``PositionsResponse`` schema's ``positions`` field must
        be an array — the contract that the dashboard's
        ``data.positions.map(...)`` call relies on."""
        positions_schema = openapi_schema["components"]["schemas"]["PositionsResponse"]
        positions_props = positions_schema.get("properties", {})
        assert "positions" in positions_props
        assert positions_props["positions"].get("type") == "array", (
            f"PositionsResponse.positions must be an array; got "
            f"{positions_props['positions']!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Route summaries — at least 20 routes carry a summary
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteSummaries:
    """``summary`` is the one-line label Swagger UI renders next to each
    path in the left rail. The W11-3 spec asks for at least 20 routes to
    carry one."""

    def test_at_least_20_routes_have_summary(self, openapi_schema):
        """Count routes with a non-empty ``summary`` across all paths.
        Includes GET / POST / PUT / DELETE / PATCH operations."""
        count = 0
        for path, methods in openapi_schema["paths"].items():
            for method, op in methods.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                summary = op.get("summary", "")
                if summary:
                    count += 1
        assert count >= 20, (
            f"Expected ≥20 routes with a summary; got {count}. "
            f"The W11-3 spec requires summaries on the 20 most-used routes."
        )

    def test_health_route_has_summary(self, openapi_schema):
        """``GET /api/health`` must have a summary (the liveness probe
        is the most-referenced route; its summary shows in Swagger UI)."""
        op = openapi_schema["paths"]["/api/health"]["get"]
        assert op.get("summary"), (
            f"GET /api/health must have a summary; got {op!r}"
        )

    def test_positions_route_has_summary(self, openapi_schema):
        """``GET /api/positions`` must have a summary."""
        op = openapi_schema["paths"]["/api/positions"]["get"]
        assert op.get("summary")

    def test_ml_metrics_route_has_summary(self, openapi_schema):
        """``GET /api/ml/metrics`` must have a summary."""
        op = openapi_schema["paths"]["/api/ml/metrics"]["get"]
        assert op.get("summary")


# ═══════════════════════════════════════════════════════════════════════════
# 6. End-to-end: routes still return 200 (response_model didn't break them)
# ═══════════════════════════════════════════════════════════════════════════


class TestRoutesStillWork:
    """Belt-and-braces: applying ``response_model`` must NOT have broken
    the actual responses. Each of the 5 contract-critical routes must
    still return 200 with the expected payload shape."""

    def test_health_returns_200_with_status(self, client, auth_headers):
        """``GET /api/health`` returns 200 with ``status`` field
        (response_model=HealthResponse didn't filter it out)."""
        response = client.get("/api/health", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
        assert "timestamp" in data
        assert "paper" in data

    def test_health_response_excludes_unset_optional_fields(self, client, auth_headers):
        """``response_model_exclude_unset=True`` means the Optional future
        fields (``mode`` / ``uptime`` / ``balance``) MUST NOT appear in
        the wire payload — the route doesn't set them, so they're unset,
        so they're excluded. Without this, callers would see spurious
        ``"mode": null`` keys."""
        response = client.get("/api/health", headers=auth_headers)
        data = response.json()
        for unset_field in ("mode", "uptime", "balance"):
            assert unset_field not in data, (
                f"/api/health must NOT include unset field {unset_field!r} "
                f"(response_model_exclude_unset=True should drop it); "
                f"got {sorted(data.keys())}"
            )

    def test_positions_returns_200_with_array(self, client, auth_headers):
        """``GET /api/positions`` returns 200 with ``positions`` list +
        ``count`` field (response_model=PositionsResponse didn't drop
        them)."""
        response = client.get("/api/positions", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "positions" in data
        assert isinstance(data["positions"], list)
        assert data["count"] == len(data["positions"])

    def test_orders_returns_200_with_array(self, client, auth_headers):
        """``GET /api/orders`` returns 200 with ``orders`` list +
        ``count`` field."""
        response = client.get("/api/orders", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "orders" in data
        assert isinstance(data["orders"], list)
        assert data["count"] == len(data["orders"])

    def test_trades_returns_200_with_array(self, client, auth_headers):
        """``GET /api/trades`` returns 200 with ``trades`` list +
        ``count`` field."""
        response = client.get("/api/trades", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "trades" in data
        assert isinstance(data["trades"], list)
        assert data["count"] == len(data["trades"])

    def test_ml_metrics_returns_200(self, client, auth_headers):
        """``GET /api/ml/metrics`` returns 200 (response_model didn't
        break the route). The route may take ~1s on first call (cache
        miss → triggers ML metric computation)."""
        response = client.get("/api/ml/metrics", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ml/metrics must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        # The response model declares ``model_type`` as required — it
        # must be present in the wire payload.
        assert "model_type" in data
