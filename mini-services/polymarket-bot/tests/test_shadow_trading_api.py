"""
Integration tests for the shadow trading HTTP API.

W10 — exercises the FastAPI endpoints registered by
``core.shadow_trading.register_routes`` via
``fastapi.testclient.TestClient``.

Coverage matrix (one test per spec item):

  (1) GET /api/shadow/trades               — 200 + empty list on a fresh DB.
  (2) GET /api/shadow/trades?strategy=test — 200 (empty filter result).
  (3) GET /api/shadow/comparison           — 200 + payload carrying BOTH
                                             the ``shadow`` and ``live`` sides.
  (4) limit parameter is honoured          — seeds N rows, requests
                                             ``limit=k<N``, expects exactly
                                             ``k`` rows (most-recent-first).
  (5) invalid limit returns 422            — out-of-range / non-int values
                                             rejected at the FastAPI
                                             validation layer (``Query(50,
                                             ge=1, le=500)``).

Approach
~~~~~~~~
A fresh ``FastAPI()`` app is built per test and ``register_routes(app)`` is
called on it — exactly the registration path the production
``api/server.py`` uses (see the ``(T1)`` block at line ~2191:
``from core.shadow_trading import register_routes as _register_shadow_routes``
→ ``_register_shadow_routes(app)``). This isolates the shadow-trading
endpoints from:

  * The production server's bearer-token auth middleware
    (``enforce_api_auth`` — exercised separately by the auth tests; not
    the concern of the shadow-trading-API contract).
  * The production server's heavy ``lifespan`` startup (TimescaleDB
    pool init, watchdog registration, paper_sim.start, market seeding
    via the Gamma API, position_manager.start, strategy_registry
    start_strategy × 3, ml training_orchestrator.start …). Importing
    the production ``app`` from ``api.server`` would force those imports
    + side effects on every test collection; the fresh-app approach keeps
    collection instantaneous and the test hermetic.

The route definitions / Pydantic validation annotations exercised here
are byte-identical to what the live server exposes, because the same
``register_routes`` function decorates the same handlers onto the test
app — so a regression in the route signature (e.g. dropping the
``Query(ge=1, le=500)`` constraint) would surface as a test failure
here before it could ship.

DB isolation mirrors the ``shadow_db`` fixture in the sibling unit-test
module ``tests/test_shadow_trading.py`` (U3): ``core.shadow_trading.DB_PATH``
is monkeypatched to a fresh ``tmp_path``-scoped SQLite file and
``_init_db()`` is re-run so the ``shadow_trades`` schema + its four
indexes exist on the new path. The module-import-time singleton
(``/tmp/pmbot_conftest_isolation/shadow_trades.db`` per the conftest
``DECISION_LEDGER_DB_PATH`` redirect) is left untouched.

Seeding the DB from a sync test context: ``record_shadow_trade`` is
``async`` (uses ``asyncio.to_thread`` for the SQLite write), but
``TestClient`` is synchronous (its requests are bridged into the ASGI
app via an ``anyio`` portal that owns its own event loop). The two
contexts share the SAME SQLite FILE (writes are durable to disk before
``record_shadow_trade`` returns — the row is committed inside the
``with sqlite3.connect(DB_PATH) as conn:`` context manager), so a row
seeded via ``asyncio.run(record_shadow_trade(...))`` from the sync test
is visible to the route handler running on the TestClient's portal-side
event loop on the next ``client.get(...)``. The ``_seed`` helper below
wraps that pattern so each test reads cleanly.

All tests in this module are SYNC (``def test_...``) — TestClient calls
are sync, and we deliberately avoid ``pytestmark = pytest.mark.asyncio``
so pytest-asyncio doesn't try to drive sync tests through its own event
loop. The repo's ``pytest.ini`` / ``pyproject.toml`` are not touched
(per the W10 "Do NOT edit existing files" constraint).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import shadow_trading
from core.shadow_trading import record_shadow_trade, register_routes


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def shadow_db(monkeypatch, tmp_path):
    """Point ``core.shadow_trading.DB_PATH`` at a fresh ``tmp_path`` SQLite
    file and (re)initialise the ``shadow_trades`` schema on it.

    The module-level ``DB_PATH`` constant is monkeypatched in place — the
    same global-lookup code path every public function in
    ``core.shadow_trading`` uses (each function resolves ``DB_PATH`` from
    the module namespace at *call time*, not at import time). After
    patching, we explicitly re-run ``shadow_trading._init_db()`` so the
    ``shadow_trades`` table + its four indexes exist on the new path;
    the import-time ``_init_db()`` call only created the schema at the
    conftest-redirected ``/tmp/pmbot_conftest_isolation/shadow_trades.db``
    path, not at this test's ``tmp_path``.

    Mirrors the ``shadow_db`` fixture in ``tests/test_shadow_trading.py``
    (U3) so the two test modules share an identical isolation contract.
    """
    db_path = tmp_path / "test_shadow_trades_api.db"
    monkeypatch.setattr("core.shadow_trading.DB_PATH", db_path)
    shadow_trading._init_db()
    return db_path


@pytest.fixture
def client(shadow_db):
    """Fresh ``FastAPI`` app with only the shadow-trading routes registered.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` (T1 block, line ~2191) so the route definitions /
    validation annotations exercised here are byte-identical to what the
    live server exposes — without the bearer-token auth middleware
    (``enforce_api_auth`` — a server-level concern exercised by separate
    auth tests) or the heavy ``lifespan`` startup (TimescaleDB, paper_sim,
    market seeding, watchdog ...) which would make the suite slow and
    brittle.

    The default ``FastAPI()`` constructor adds no lifespan, so
    ``TestClient`` requests don't trigger any startup side effects.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _seed(*rows: dict) -> None:
    """Sync wrapper around ``record_shadow_trade`` so sync tests can seed
    the SQLite file before issuing ``TestClient`` requests.

    Each positional arg is a ``dict`` of the kwargs to
    ``record_shadow_trade``. Runs all inserts inside a single
    ``asyncio.run`` call (one fresh event loop); each insert commits
    inside its ``with sqlite3.connect(DB_PATH) as conn:`` context
    manager before its coroutine returns, so by the time
    ``asyncio.run`` returns the rows are durable on disk — visible to
    the TestClient's portal-side event loop on the next
    ``client.get(...)``.

    A 5 ms ``asyncio.sleep`` between inserts guarantees strictly
    increasing ``time.time()`` values so the most-recent-first ordering
    the API promises is deterministic on a loaded CI box (SQLite stores
    ``timestamp`` as REAL with ~µs precision; 5 ms is a comfortable
    margin — mirrors the ordering tactic in
    ``tests/test_shadow_trading.py::test_get_shadow_trades_returns_most_recent_first``).

    Raises ``AssertionError`` if any insert fails (returns ``None``) so
    a seed-time DB failure surfaces immediately rather than manifesting
    as a confusing downstream assertion failure on the response.
    """
    async def _seed_all() -> None:
        for r in rows:
            row_id = await record_shadow_trade(**r)
            assert row_id is not None and row_id > 0, (
                f"seed insert failed for row={r!r} — record_shadow_trade "
                f"returned {row_id!r}"
            )
            await asyncio.sleep(0.005)  # strictly-increasing timestamps

    asyncio.run(_seed_all())


# ── (1) GET /api/shadow/trades — 200 + empty list initially ─────────────────

def test_get_shadow_trades_returns_200_with_empty_list_initially(client):
    """GET /api/shadow/trades on a fresh DB must return HTTP 200 with
    ``count=0`` and an empty ``trades`` list. The ``shadow_db`` fixture's
    ``tmp_path`` SQLite file is brand-new, so the ``shadow_trades`` table
    has zero rows — the read path must NOT 500 on an empty table.
    """
    response = client.get("/api/shadow/trades")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["trades"] == []


# ── (2) GET /api/shadow/trades?strategy=test — 200 on empty filter ─────────

def test_get_shadow_trades_with_strategy_filter_returns_200(client):
    """GET /api/shadow/trades?strategy=test must return 200 — the
    strategy filter is a no-op on an empty DB (returns ``count=0``,
    ``trades=[]``) rather than erroring. This guards against a regression
    where the filter SQL would fail on an empty table or where the
    endpoint would 404/500 on an unknown strategy.
    """
    response = client.get("/api/shadow/trades", params={"strategy": "test"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["trades"] == []


# ── (3) GET /api/shadow/comparison — 200 with shadow + live sides ─────────

def test_get_shadow_comparison_returns_200_with_shadow_and_live_sides(client):
    """GET /api/shadow/comparison must return 200 with a payload carrying
    BOTH the ``shadow`` side and the ``live`` side (plus the per-strategy
    merge list under ``strategies``).

    On a fresh DB the shadow side is zeroed-out (count=0). The live side
    is sourced via a lazy ``from core.closed_positions import
    closed_positions`` import inside ``_live_summary`` — in the test
    sandbox that store is empty too, so the live side is zeroed-out.
    The endpoint must NOT 500 on either an empty shadow store OR an
    empty / unavailable closed-positions store (the latter is the
    "fresh deployment" case the module docstring calls out explicitly).
    """
    response = client.get("/api/shadow/comparison")

    assert response.status_code == 200
    body = response.json()

    # Top-level shape: both sides + the per-strategy merge list.
    assert "shadow" in body
    assert "live" in body
    assert "strategies" in body

    # Shadow side on a fresh DB: count=0, by_side zeroed.
    shadow = body["shadow"]
    assert shadow["count"] == 0
    assert set(shadow.keys()) >= {
        "count", "total_size", "avg_predicted_edge", "avg_confidence",
        "by_side", "by_strategy",
    }
    assert shadow["by_side"] == {"BUY": 0, "SELL": 0}
    assert shadow["by_strategy"] == {}

    # Live side present and well-formed (the closed_positions store is
    # empty in the sandbox, so this exercises the "fresh deployment"
    # fallback path where _live_summary returns its zeroed-out default).
    live = body["live"]
    assert set(live.keys()) >= {
        "count", "total_pnl", "avg_pnl", "win_rate",
        "total_volume_shares", "by_strategy",
    }
    assert isinstance(live["count"], int)
    assert live["count"] >= 0
    assert isinstance(live["by_strategy"], dict)

    # The merge list is a (possibly empty) list of per-strategy rows.
    assert isinstance(body["strategies"], list)


# ── (4) limit parameter is honoured ────────────────────────────────────────

def test_limit_parameter_is_honored(client):
    """The ``limit`` query param (declared ``Query(50, ge=1, le=500)`` on
    the route signature) must cap the number of rows returned.

    Seeds 5 rows for the same strategy with strictly-increasing
    timestamps (5 ms apart), then requests ``limit=2`` — the response
    must contain exactly 2 rows, and they must be the two MOST RECENT
    (the API promises most-recent-first ordering per the
    ``register_routes`` docstring: "Recent counterfactual trades (most
    recent first)").
    """
    _seed(*[
        dict(
            decision_id=f"dec-limit-{i}",
            token_id=f"TOK_LIMIT_{i}",
            strategy="alpha",
            side="BUY",
            price=0.50 + 0.01 * i,
            size=10.0,
            predicted_edge=0.02,
            confidence=0.5,
        )
        for i in range(5)
    ])

    # Sanity: an unfiltered request returns all 5 rows (proves the seed
    # actually landed — guards against a false-pass on test 4 if the
    # seed silently failed and the DB were empty).
    sanity = client.get("/api/shadow/trades", params={"limit": 50})
    assert sanity.status_code == 200
    assert sanity.json()["count"] == 5, (
        "seed sanity check failed — expected 5 rows in the DB but got "
        f"{sanity.json()['count']}; the limit test below is meaningless "
        "if the seed didn't land"
    )

    # The actual limit test: request limit=2, expect exactly 2 rows,
    # most-recent-first.
    response = client.get("/api/shadow/trades", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert len(body["trades"]) == 2

    # Most-recent-first: the LAST-seeded row (TOK_LIMIT_4, highest
    # timestamp) must be at index 0; TOK_LIMIT_3 at index 1.
    assert body["trades"][0]["token_id"] == "TOK_LIMIT_4"
    assert body["trades"][1]["token_id"] == "TOK_LIMIT_3"

    # Timestamps strictly decreasing within the slice.
    ts = [r["timestamp"] for r in body["trades"]]
    assert ts[0] > ts[1]


# ── (5) invalid limit returns 422 ──────────────────────────────────────────

@pytest.mark.parametrize(
    "bad_limit, reason",
    [
        (0, "ge=1 violation (zero)"),
        (-1, "ge=1 violation (negative)"),
        (501, "le=500 violation"),
        ("abc", "non-int-coercible string"),
        ("1.5", "float-string not coercible to int"),
    ],
)
def test_invalid_limit_returns_422(client, bad_limit, reason):
    """An out-of-range or non-integer ``limit`` must trigger FastAPI's
    422 Unprocessable Entity response.

    The route signature ``limit: int = Query(50, ge=1, le=500)`` enforces
    three independent constraints at the framework layer (before the
    handler runs):

      * ``int`` type           — non-int-coercible values (``"abc"``,
                                  ``"1.5"``) are rejected.
      * ``ge=1`` (min)          — ``0`` and ``-1`` are rejected.
      * ``le=500`` (max)        — ``501`` is rejected.

    Each violation must surface as HTTP 422 (FastAPI's default response
    code for request-validation failures via ``RequestValidationError``).
    Parametrised so a regression in any one of the three constraints
    surfaces as a single, named failure rather than a single boolean
    pass/fail.
    """
    response = client.get("/api/shadow/trades", params={"limit": bad_limit})

    assert response.status_code == 422, (
        f"expected 422 for bad_limit={bad_limit!r} ({reason}), got "
        f"{response.status_code}: {response.text}"
    )
    # FastAPI's 422 body carries the validation error detail so a caller
    # can programmatically diagnose which constraint fired.
    body = response.json()
    assert "detail" in body
    assert isinstance(body["detail"], list) and len(body["detail"]) >= 1
