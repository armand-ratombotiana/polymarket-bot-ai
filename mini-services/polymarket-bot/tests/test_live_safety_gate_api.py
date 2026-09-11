"""
Integration tests for the live safety gate API endpoints.

W9 — Live Safety Gate API integration tests.

Covers the 5 contract guarantees the W9 task spec asks for, all driven
through ``fastapi.testclient.TestClient`` (sync) hitting the actual
``register_routes(app)`` endpoints defined in ``core/live_safety_gate.py``:

  1. ``GET /api/live/readiness`` returns HTTP 200 with exactly 10 checks.
  2. ``POST /api/live/enable`` with ``confirm=false`` returns HTTP 400
     (defence-against-accidental-clicks guard fires BEFORE the gate
     runs).
  3. ``POST /api/live/enable`` with ``confirm=true`` returns HTTP 409
     when any §82 check fails (rather than flipping live mode on).
  4. The ``GET /api/live/readiness`` response carries ``passed_count``
     and ``total_count`` (the operator dashboard relies on these for its
     pass/total ratio display).
  5. Every check dict in the ``checks`` array carries the three contract
     fields ``name``, ``passed``, ``detail`` (the dashboard iterates
     the checks array and renders each row's name, pass/fail badge, and
     detail string — a missing field would crash mid-render).

Integration vs unit scope
--------------------------
The sibling ``tests/test_live_safety_gate.py`` (U4) covers the gate
function ``check_live_readiness()`` directly (unit-level) and the
``POST /api/live/enable`` 409 path via a mocked ``check_live_readiness``
return value (unit-level on the endpoint). W9 here is the **integration**
complement: it stands up the full FastAPI app via ``register_routes(app)``
and drives real HTTP requests through ``TestClient`` so the route
handler invokes the actual ``check_live_readiness`` coroutine, which in
turn runs the actual 10 staged checks against patched dependencies.

Testing strategy
-----------------
A self-contained ``happy_baseline`` fixture patches all 10 of the gate's
dependencies to a passing state (mirrors the pattern in the sibling U4
``tests/test_live_safety_gate.py`` module, duplicated here so this file
is fully self-contained — cross-test-file fixture imports are an
anti-pattern pytest doesn't recommend). Each test then either:

  * uses ``happy_baseline`` to assert deterministic passing-state
    structure (tests #1, #4, #5); or
  * overrides exactly ONE dependency on top of ``happy_baseline`` to
    flip a single check to ``passed=False`` (test #3), then asserts the
    HTTP 409 contract; or
  * needs no fixture because the contract under test fires BEFORE the
    gate runs (test #2's ``confirm=false`` → 400 path).

Tests are SYNC ``def`` (not ``async def``) so they run cleanly under
``TestClient``'s sync portal — ``TestClient`` runs the ASGI app in a
separate thread with its own event loop, and async test functions
would contend with that loop. Sync test functions let ``TestClient``
manage the event-loop plumbing itself. The repo's ``pytest.ini``
declares ``testpaths = tests``; per the W9 "Do NOT edit existing files"
constraint, no ``asyncio_mode = auto`` config is added — and since
these tests are sync, no ``pytestmark = pytest.mark.asyncio`` is
needed either (mirrors the convention in ``tests/test_settlement.py``,
``tests/test_decision_ledger.py`` for their sync tests).

Monkeypatch gotchas (re-surfaced from U4, applied here too)
------------------------------------------------------------
- ``ml.model.MarketMLModel.is_fitted`` is a read-only ``@property``
  (returns ``self.rf is not None``). ``monkeypatch.setattr`` on the
  *instance* fails at teardown (no setter). Fix: patch at the CLASS
  level (``ml.model.MarketMLModel.is_fitted``). Monkeypatch captures
  the property descriptor and reinstalls it on teardown.
- ``config.Settings.has_credentials`` / ``has_api_keys`` are also
  read-only ``@property`` methods. Fix: patch the *underlying* plain
  pydantic str fields (``poly_private_key``, ``poly_api_key``,
  ``poly_api_secret``, ``poly_api_passphrase``) — the properties then
  re-derive ``True`` from the non-empty underlying values.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.live_safety_gate import (
    CHECK_ORDER,
    CHECK_PAPER_MODE,
    DRIFT_HEALTHY_STATUS,
    MIN_CLOSED_TRADES,
    MIN_WIN_RATE,
    PAPER_MODE_MIN_SECONDS,
    register_routes,
)


# ── Fixture: happy baseline (all 10 checks pass) ────────────────────────────
# Duplicated locally from ``tests/test_live_safety_gate.py`` (U4) so this
# module is fully self-contained — no cross-test-file fixture import. The
# fixture patches all 10 of the gate's dependencies to a deterministic
# passing state via ``monkeypatch.setattr`` (auto-reverted on teardown).
@pytest.fixture
def happy_baseline(monkeypatch):
    """Patch every dependency of the 10 §82 staged checks to a *passing*
    state.

    This is the deterministic foundation for tests #1, #4, #5: under this
    baseline, the GET /api/live/readiness endpoint returns 200 with
    ``passed_count == 10`` and ``passed == True``. Test #3 then requests
    ``happy_baseline`` and overrides exactly ONE dependency
    (``store.session_start = time.time()``) to flip the paper-mode check
    to ``passed=False``, which triggers the 409 path on the enable
    endpoint.

    Why mock all 10 (not just the one under test)?
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The default sandbox state (no closed trades, ml_model trained on
    synthetic-only, audit trail empty, settings.has_credentials=False,
    kill-switch marker absent, store.session_start=now via the autouse
    conftest reset) would fail 7+ checks simultaneously. A test that
    only asserts ``status_code == 409`` would pass trivially — but it
    wouldn't prove the 409 fired *because of the specific check under
    test*. By starting from an all-pass baseline and flipping exactly
    one check, test #3 can assert the 409's ``detail.blocking_checks``
    contains ONLY ``CHECK_PAPER_MODE`` — the load-bearing isolation
    guarantee that proves the gate's failure isolation.
    """
    # ── Check #1: paper_mode_24h — paper mode + 25h-old session.
    # 25h comfortably exceeds PAPER_MODE_MIN_SECONDS (24h); using exactly
    # 24h would risk a flaky boundary failure if pytest startup latency
    # pushed the measured age below 86400.0s between fixture setup and
    # the check's ``time.time()`` read inside the route handler.
    monkeypatch.setattr("config.settings.trading_mode", "paper")
    monkeypatch.setattr(
        "core.data_store.store.session_start",
        time.time() - (PAPER_MODE_MIN_SECONDS + 3600.0),  # 25h ago
    )

    # ── Checks #2, #4, #5: closed_positions — 25 trades, +expectancy,
    #    60% win rate (above MIN_WIN_RATE=0.50 and at/above
    #    MIN_CLOSED_TRADES=20). A single mock return value satisfies all
    #    three checks because they each read different fields from the
    #    same stats dict.
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.get_closed_stats",
        AsyncMock(return_value={
            "count": MIN_CLOSED_TRADES + 5,          # 25 ≥ 20  → check #5 passes
            "avg_pnl": 0.50,                         # > 0      → check #2 passes
            "win_rate": MIN_WIN_RATE + 0.10,         # 0.60 > 0.50 → check #4 passes
        }),
    )

    # ── Checks #3, #9: risk_manager.status_report — healthy risk posture.
    #    The report dict must carry every key the two checks read
    #    (drawdown_dollars, max_drawdown_limit, daily_pnl, daily_loss_limit,
    #    weekly_pnl, weekly_loss_limit, total_exposure, max_total_exposure,
    #    pending_order_capital, max_pending_order_capital, open_orders,
    #    max_open_orders, kill_switch, kill_switch_durable,
    #    observation_only, exposure_reconciled). Values chosen so every
    #    sub-check in check #9's 11-item sub_checks list evaluates to ok.
    monkeypatch.setattr(
        "risk.manager.risk_manager.status_report",
        AsyncMock(return_value={
            "kill_switch": False,
            "kill_switch_durable": False,
            "observation_only": False,
            "observation_reason": "",
            "exposure_reconciled": True,
            "drawdown_dollars": 0.50,                    # < $2.00 live gate AND < $8 hard limit
            "max_drawdown_limit": 8.00,
            "daily_pnl": 0.50,                          # > -$5.00 daily loss stop
            "daily_loss_limit": -5.00,
            "weekly_pnl": 1.00,                         # > -$10.00 weekly loss stop
            "weekly_loss_limit": -10.00,
            "total_exposure": 10.00,                     # ≤ $100 cap
            "max_total_exposure": 100.00,
            "pending_order_capital": 5.00,               # ≤ $50 cap
            "max_pending_order_capital": 50.00,
            "open_orders": 1,                            # ≤ 10 cap
            "max_open_orders": 10,
        }),
    )

    # ── Check #6: ml_model — fitted on real data ─────────────────────────
    # ``is_fitted`` is a read-only ``@property`` on ``MarketMLModel`` —
    # patch at the CLASS level so the route handler's ``getattr(ml_model,
    # "is_fitted")`` returns the patched ``True`` (class-attribute lookup
    # short-circuits the property descriptor). Monkeypatch captures the
    # original property descriptor and reinstalls it on teardown.
    monkeypatch.setattr("ml.model.MarketMLModel.is_fitted", True)
    monkeypatch.setattr("ml.model.ml_model.training_source", "real_and_synthetic")
    monkeypatch.setattr("ml.model.ml_model.n_real_samples", 500)
    monkeypatch.setattr("ml.model.ml_model.n_synthetic_samples", 1000)

    # ── Check #7: drift_detector — HEALTHY (explicit; the default is also
    #    HEALTHY but this pins it so a future default change doesn't
    #    silently flip the baseline).
    monkeypatch.setattr(
        "ml.drift_detector.drift_detector.drift_status",
        DRIFT_HEALTHY_STATUS,
    )

    # ── Check #8: kill_switch_tested — audit trail carries one activate
    #    AND one deactivate event (deactivation AFTER activation, so the
    #    ordered=True branch in the detail string fires). The marker file
    #    path resolves to /app/data/... which does not exist in the
    #    sandbox, so marker_present stays False and the audit evidence is
    #    the sole passing signal — exactly the canonical path the gate
    #    documents.
    now = time.time()
    monkeypatch.setattr(
        "core.audit_logger.audit_logger.get_recent_events",
        AsyncMock(return_value=[
            {"event_type": "kill_switch_activated", "timestamp": now - 200.0},
            {"event_type": "kill_switch_deactivated", "timestamp": now - 100.0},
        ]),
    )

    # ── Check #10: api_credentials_configured — both wallet key + CLOB
    #    API key/secret/passphrase present. ``has_credentials`` /
    #    ``has_api_keys`` are read-only ``@property`` methods on
    #    ``Settings`` (derived from the underlying pydantic str fields),
    #    so patch the underlying fields — the properties re-derive True
    #    from the non-empty underlying values.
    monkeypatch.setattr("config.settings.poly_private_key", "0x" + "a" * 64)
    monkeypatch.setattr("config.settings.poly_api_key", "test-api-key")
    monkeypatch.setattr("config.settings.poly_api_secret", "test-api-secret")
    monkeypatch.setattr("config.settings.poly_api_passphrase", "test-passphrase")


# ── Helper: build a fresh FastAPI app with only the live-safety-gate routes ─
# Each test builds its own app instance via this factory so there's zero
# state leakage between tests (no shared route registry, no shared
# middleware). The app registers ONLY the live-safety-gate endpoints — no
# auth middleware, no other endpoints — keeping the tests focused on the
# W9 contract under test.
def _build_client() -> TestClient:
    """Build a ``TestClient`` against a minimal FastAPI app with only the
    live-safety-gate routes registered. Returns a ``TestClient`` ready to
    issue HTTP requests against ``/api/live/*``.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


# ── 1. GET /api/live/readiness returns 200 with 10 checks ──────────────────
def test_get_readiness_returns_200_with_10_checks(happy_baseline):
    """W9 spec item (1): ``GET /api/live/readiness`` must return HTTP 200
    with a body carrying exactly 10 checks (one per ``CHECK_ORDER`` slot —
    paper soak → performance evidence → ML governance → safety posture →
    credentials).

    The 10-check count is the §82 gate's headline contract — an operator
    reading the dashboard trusts that exactly 10 staged checks exist, no
    more, no less. Drift in this count (e.g. a new check added without
    updating the dashboard) would silently break the dashboard's
    pass-count-to-total-count ratio. This test pins the count to 10 and
    the order to ``CHECK_ORDER``.

    Uses ``happy_baseline`` so all 10 checks deterministically pass —
    proving the baseline fixture is correctly configured (if this fails,
    every downstream test's isolation assertions become unreliable).
    """
    client = _build_client()
    response = client.get("/api/live/readiness")

    # ── The headline contract: HTTP 200 OK. ───────────────────────────────
    # The readiness endpoint never 500s — even if a check throws, the
    # gate records the failure as a failed check rather than raising.
    assert response.status_code == 200, (
        f"GET /api/live/readiness must return 200; got {response.status_code}. "
        f"Body: {response.text!r}"
    )

    body = response.json()
    assert isinstance(body, dict)

    # ── The 10-check contract: exactly 10 checks in the array. ───────────
    assert "checks" in body, "response body must carry a 'checks' array"
    checks = body["checks"]
    assert isinstance(checks, list)
    assert len(checks) == 10, (
        f"readiness must carry exactly 10 checks; got {len(checks)}. "
        f"The §82 gate's dashboard pass-count-to-total-count ratio depends "
        f"on this count being pinned to 10."
    )

    # ── The checks are returned in the staged CHECK_ORDER (paper soak →
    #    performance → ML governance → safety → credentials). Dashboard
    #    rendering depends on this exact sequence.
    actual_ids = [c["id"] for c in checks]
    expected_ids = list(CHECK_ORDER)
    assert actual_ids == expected_ids, (
        f"check order mismatch: expected {expected_ids}, got {actual_ids}"
    )

    # ── Baseline-fitness guard: under happy_baseline, every check passes.
    #    If this fails, the happy_baseline fixture is misconfigured and the
    #    test #3 isolation assertion (blocking_checks == [CHECK_PAPER_MODE])
    #    becomes unreliable.
    assert body.get("passed") is True, (
        "happy_baseline should make every check pass — if this fails, the "
        "baseline fixture is misconfigured and downstream test #3's "
        "'blocking_checks == [CHECK_PAPER_MODE]' isolation assertion is "
        f"unreliable. Body: {body!r}"
    )


# ── 2. POST /api/live/enable with confirm=false returns 400 ────────────────
def test_post_enable_with_confirm_false_returns_400():
    """W9 spec item (2): ``POST /api/live/enable`` with ``confirm=false``
    must return HTTP 400 — the defence-against-accidental-clicks guard
    fires BEFORE the gate runs.

    The route handler's first check is ``if not req.confirm: raise
    HTTPException(status_code=400, ...)``. This test verifies that guard
    fires regardless of the underlying gate state — no fixture is needed
    because the guard short-circuits before ``check_live_readiness()`` is
    ever called. The 400 path is the safety net against an operator
    double-clicking the "Enable Live Trading" button without confirming.
    """
    client = _build_client()
    response = client.post(
        "/api/live/enable",
        json={"confirm": False, "reason": "W9 — confirm=false guard test"},
    )

    # ── The headline contract: HTTP 400 Bad Request. ─────────────────────
    assert response.status_code == 400, (
        f"POST /api/live/enable with confirm=false must return 400 "
        f"(defence-against-accidental-clicks guard); got "
        f"{response.status_code}. Body: {response.text!r}"
    )

    # The 400 body carries FastAPI's standard error envelope: a single
    # ``detail`` string (the guard's human-readable explanation). The
    # operator UI surfaces this string as a tooltip / inline warning.
    body = response.json()
    assert "detail" in body
    detail = body["detail"]
    # FastAPI serialises a plain-str HTTPException.detail as a JSON string
    # (not a dict). The guard's message references the confirm=true
    # requirement so the operator knows exactly what to fix.
    assert isinstance(detail, str)
    assert "confirm" in detail.lower(), (
        f"400 detail should reference the confirm=true requirement so the "
        f"operator knows what to fix; got {detail!r}"
    )


# ── 3. POST /api/live/enable with confirm=true returns 409 when checks fail
def test_post_enable_with_confirm_true_returns_409_when_checks_fail(
    happy_baseline, monkeypatch
):
    """W9 spec item (3): ``POST /api/live/enable`` with ``confirm=true``
    must return HTTP 409 (Conflict) when any §82 check fails — never
    flipping live mode on.

    The route handler runs ``check_live_readiness()`` after the confirm
    guard; if the verdict is ``passed=False``, it raises
    ``HTTPException(status_code=409, detail={message, passed_count,
    total_count, blocking_checks, checks, guidance})``. This test:

      * uses ``happy_baseline`` to make ALL 10 checks deterministically
        pass;
      * then overrides exactly ONE dependency
        (``store.session_start = time.time()``) to flip the
        ``paper_mode_24h`` check to ``passed=False`` (session age = 0s,
        well under the 24h threshold);
      * POSTs ``/api/live/enable`` with ``confirm=true``;
      * asserts the response is HTTP 409 (NOT 200 — live mode must NOT
        flip on when a check fails);
      * asserts the 409's ``detail.blocking_checks`` contains ONLY
        ``CHECK_PAPER_MODE`` — proving the failure is isolated to the
        overridden check, not a side-effect on a sibling check.

    This is the integration complement to U4 test #6: where U4 #6 mocks
    ``check_live_readiness`` to return a failed verdict (unit-level on
    the endpoint), this W9 test drives the FULL HTTP → gate → checks
    path against patched dependencies — proving the integration
    contract: a real failed check surfaces as a real 409.
    """
    # Override: session_start = now → age = 0s < 24h threshold. This flips
    # check #1 (paper_mode_24h) to passed=False. Every other dependency
    # stays at the happy-baseline passing state, so the failure isolates
    # to check #1.
    monkeypatch.setattr("core.data_store.store.session_start", time.time())

    client = _build_client()
    response = client.post(
        "/api/live/enable",
        json={"confirm": True, "reason": "W9 — 409-on-failed-check integration test"},
    )

    # ── The headline contract: HTTP 409 Conflict, NOT 200 OK. ───────────
    # 409 (Conflict) is the canonical "request conflicts with current
    # server state" status — here, the operator's "go live" request
    # conflicts with the server's "safety gate not yet satisfied" state.
    assert response.status_code == 409, (
        f"POST /api/live/enable with confirm=true AND a failing check must "
        f"return 409 (NOT flip live mode on / return 200); got "
        f"{response.status_code}. Body: {response.text!r}"
    )

    body = response.json()
    assert "detail" in body, "409 response must carry a 'detail' payload"

    # The 409 ``detail`` is a structured DICT (not a plain string) so an
    # operator dashboard can render every blocking check without a
    # follow-up GET /api/live/readiness.
    detail = body["detail"]
    assert isinstance(detail, dict), (
        f"409 detail must be a structured dict for dashboard rendering; "
        f"got {type(detail).__name__}: {detail!r}"
    )

    # ── The blocking-check list is surfaced for the operator to act on. ─
    assert "blocking_checks" in detail
    blocking = detail["blocking_checks"]
    assert isinstance(blocking, list)
    assert CHECK_PAPER_MODE in blocking, (
        f"the overridden check ({CHECK_PAPER_MODE!r}) must appear in "
        f"blocking_checks; got {blocking!r}"
    )
    # Failure is ISOLATED to the paper-mode check — no other check was
    # perturbed by overriding session_start (which only check #1 reads).
    # This is the load-bearing isolation guarantee: it proves the 409
    # fired because of the SPECIFIC check we flipped, not because some
    # other check happened to fail.
    assert blocking == [CHECK_PAPER_MODE], (
        f"failure must be isolated to the overridden check; expected "
        f"blocking_checks == [{CHECK_PAPER_MODE!r}], got {blocking!r}"
    )

    # ── The 409 detail also carries passed_count / total_count so the
    #    dashboard can show "9 of 10 checks passing" alongside the 409.
    assert "passed_count" in detail
    assert "total_count" in detail
    assert detail["passed_count"] == 9, (
        f"with exactly one of 10 checks failing, passed_count must be 9; "
        f"got {detail['passed_count']!r}"
    )
    assert detail["total_count"] == 10, (
        f"total_count must always be 10 (the §82 gate's headline contract); "
        f"got {detail['total_count']!r}"
    )

    # ── The full checks array is included so the dashboard can render
    #    every check's status (passing AND failing) in one round-trip
    #    without a follow-up GET /api/live/readiness.
    assert "checks" in detail
    assert isinstance(detail["checks"], list)
    assert len(detail["checks"]) == 10, (
        f"409 detail.checks must carry all 10 checks (so the dashboard can "
        f"render every check in one round-trip); got {len(detail['checks'])}"
    )
    # The paper-mode check specifically is the failed one.
    paper_check = next(
        c for c in detail["checks"] if c["id"] == CHECK_PAPER_MODE
    )
    assert paper_check["passed"] is False


# ── 4. response contains passed_count and total_count ───────────────────────
def test_readiness_response_contains_passed_count_and_total_count(happy_baseline):
    """W9 spec item (4): the ``GET /api/live/readiness`` response must
    carry ``passed_count`` and ``total_count`` fields at the top level.

    The operator dashboard polls ``GET /api/live/readiness`` and renders
    a pass-count-to-total-count ratio ("9 / 10 checks passing") in its
    header. A missing field would crash the dashboard's header render
    mid-poll. This test pins the two fields' presence and types (both
    ints), and verifies they're consistent with the ``checks`` array
    (``passed_count`` equals the count of checks with ``passed == True``,
    ``total_count`` equals ``len(checks)``).

    Uses ``happy_baseline`` so the values are deterministic:
    ``passed_count == 10`` and ``total_count == 10`` — if this fails,
    the baseline fixture is misconfigured.
    """
    client = _build_client()
    response = client.get("/api/live/readiness")
    assert response.status_code == 200

    body = response.json()

    # ── Both count fields must be present at the top level. ─────────────
    assert "passed_count" in body, (
        "readiness response must carry top-level 'passed_count' for the "
        "dashboard's pass-count-to-total-count ratio display"
    )
    assert "total_count" in body, (
        "readiness response must carry top-level 'total_count' for the "
        "dashboard's pass-count-to-total-count ratio display"
    )

    passed_count = body["passed_count"]
    total_count = body["total_count"]

    # ── Both must be integers (the dashboard renders them numerically). ─
    assert isinstance(passed_count, int), (
        f"passed_count must be an int; got {type(passed_count).__name__}"
    )
    assert isinstance(total_count, int), (
        f"total_count must be an int; got {type(total_count).__name__}"
    )

    # ── The headline §82 contract: total_count is always 10. ───────────
    assert total_count == 10, (
        f"total_count must always be 10 (the §82 gate's headline contract "
        f"— 10 staged checks, no more, no less); got {total_count}"
    )

    # ── passed_count must equal the count of checks with passed == True
    #    in the checks array (consistency between the count fields and the
    #    array the dashboard iterates for row rendering).
    checks = body["checks"]
    actual_passed = sum(1 for c in checks if c.get("passed") is True)
    assert passed_count == actual_passed, (
        f"passed_count ({passed_count}) must equal the number of checks "
        f"with passed==True in the checks array ({actual_passed})"
    )

    # ── Under happy_baseline, every check passes → passed_count == 10.
    #    This is a baseline-fitness guard: if it fails, the happy_baseline
    #    fixture is misconfigured and downstream test #3's isolation
    #    assertion (passed_count == 9 with one override) becomes
    #    unreliable.
    assert passed_count == 10, (
        f"under happy_baseline, every check should pass so passed_count "
        f"must be 10; got {passed_count}. If this fails, the baseline "
        f"fixture is misconfigured."
    )

    # ── Cross-field consistency: top-level ``passed`` is True iff
    #    passed_count == total_count (the gate's pass semantics — the gate
    #    passes only when EVERY check passes).
    assert body["passed"] is (passed_count == total_count), (
        f"top-level 'passed' ({body['passed']!r}) must equal "
        f"(passed_count == total_count) ({passed_count == total_count!r})"
    )


# ── 5. each check has name / passed / detail fields ─────────────────────────
def test_each_check_has_name_passed_detail_fields(happy_baseline):
    """W9 spec item (5): every check dict in the ``checks`` array must
    carry the three contract fields ``name``, ``passed``, and ``detail``
    — regardless of whether the check passed or failed.

    The dashboard / operator UI iterates ``checks`` and renders each row's
    name (row label), passed (pass/fail badge colour), and detail (string
    an operator can act on). A missing field would crash the dashboard
    mid-render. This test pins the schema: for every check, all three
    fields must be present, with the right types (``name`` is a non-empty
    string, ``passed`` is a bool, ``detail`` is a string).

    Run against the happy baseline so all checks PASS — verifying the
    schema holds on the passing path. (The failing-path schema is
    exercised by test #3 above, where the failing paper-mode check's
    payload is asserted to carry ``passed == False`` and a non-empty
    ``detail`` string; the gate's ``_failed()`` helper guarantees the
    same schema on the exception path too.)
    """
    client = _build_client()
    response = client.get("/api/live/readiness")
    assert response.status_code == 200

    body = response.json()
    checks = body["checks"]
    assert len(checks) == 10

    required_fields = ("name", "passed", "detail")

    for idx, check in enumerate(checks):
        # ── Every required field is present on every check. ─────────────
        for field in required_fields:
            assert field in check, (
                f"check #{idx} (id={check.get('id', '<missing>')!r}) "
                f"is missing required field {field!r}: {check!r}"
            )

        # ── Type contracts the dashboard relies on:
        #    - name: non-empty human-readable string (row label)
        name = check["name"]
        assert isinstance(name, str), (
            f"check #{idx} (id={check.get('id')!r}) name must be str, "
            f"got {type(name).__name__}: {name!r}"
        )
        assert len(name) > 0, (
            f"check #{idx} (id={check.get('id')!r}) name is empty"
        )

        #    - passed: bool (drives the pass/fail badge colour)
        passed = check["passed"]
        assert isinstance(passed, bool), (
            f"check #{idx} (id={check.get('id')!r}) passed must be bool, "
            f"got {type(passed).__name__}: {passed!r}"
        )

        #    - detail: string (operator-actionable context — may be empty
        #      in theory, but the gate always populates it in practice)
        detail = check["detail"]
        assert isinstance(detail, str), (
            f"check #{idx} (id={check.get('id')!r}) detail must be str, "
            f"got {type(detail).__name__}: {detail!r}"
        )

    # ── Sanity: under happy_baseline every check passes, so the schema
    #    holds on the passing path. (The failing-path schema is implicit
    #    in test #3's assertion that the failing paper-mode check carries
    #    ``passed == False``.)
    assert all(c["passed"] for c in checks), (
        "happy_baseline should make every check pass — if this fails, the "
        "baseline fixture is misconfigured and test #3's isolation "
        "assertion (blocking_checks == [CHECK_PAPER_MODE]) is unreliable"
    )
