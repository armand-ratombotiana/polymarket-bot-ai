"""
Unit tests for ``core/live_safety_gate.py``.

U4 — Live Safety Gate unit tests.

Covers the seven public-surface guarantees of the God Mode §82 Live Trading
Safety Gate requested by the U4 task spec:

  1. ``check_live_readiness()`` returns exactly 10 staged checks (one per
     ``CHECK_ORDER`` slot — paper soak → performance → ML governance → safety
     posture → credentials).
  2. The gate fails when the continuous paper-mode session is < 24h old
     (check ``paper_mode_24h`` records ``passed=False`` and surfaces in
     ``blocking_checks``).
  3. The gate fails when expectancy is negative
     (check ``positive_expectancy`` records ``passed=False``).
  4. The gate fails when the ML drift detector's status is not ``HEALTHY``
     (check ``drift_healthy`` records ``passed=False``).
  5. The gate fails when there are < 20 closed trades
     (check ``min_20_closed_trades`` records ``passed=False``).
  6. The ``POST /api/live/enable`` endpoint returns HTTP 409 when any check
     fails (rather than flipping live mode on).
  7. Every one of the 10 check dicts carries the contract fields
     ``name``, ``passed``, and ``detail`` — the dashboard / operator UI
     relies on these three keys being present on every check, regardless of
     whether the check passed or failed (failed checks may additionally carry
     an exception string in ``detail``).

Testing strategy
-----------------
The gate's check functions import their dependencies lazily *inside* each
check body (``from core.closed_positions import closed_positions``, etc.).
This is by design — the gate's contract is to *always* return a verdict
(never raise), so a broken dependency records itself as a failed check via
the ``_failed()`` helper rather than crashing the gate.

For deterministic unit coverage, this module patches the dependency
singletons *at the module-global level* (e.g.
``monkeypatch.setattr("core.closed_positions.closed_positions.get_closed_stats", AsyncMock(...))``)
so the lazy ``from X import Y`` import inside each check picks up the patched
object. The ``happy_baseline`` fixture patches all 10 dependencies to a
passing state; each failing test then overrides exactly ONE dependency to
flip a single check to ``passed=False`` and asserts that:
  - the gate's top-level ``passed`` field is ``False``;
  - the overridden check's id is in ``blocking_checks``;
  - ``blocking_checks`` contains ONLY that one id (proving the failure is
    isolated to the intended check, not a side-effect on a sibling check
    that shares the dependency — e.g. checks #2 / #4 / #5 all read from
    ``closed_positions.get_closed_stats``).

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (pytest-asyncio is already
a project dependency; the repo's ``pytest.ini`` declares ``testpaths =
tests`` and is intentionally left untouched per the U4 "no existing file
edits" constraint).
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.live_safety_gate import (
    CHECK_CLOSED_TRADES,
    CHECK_DRIFT_HEALTHY,
    CHECK_ORDER,
    CHECK_PAPER_MODE,
    CHECK_POSITIVE_EXPECTANCY,
    DRIFT_HEALTHY_STATUS,
    MAX_LIVE_DRAWDOWN_USD,
    MIN_CLOSED_TRADES,
    MIN_WIN_RATE,
    PAPER_MODE_MIN_SECONDS,
    check_live_readiness,
    register_routes,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the U4 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors the convention in ``tests/test_decision_ledger.py`` and the
# other sibling test files).
pytestmark = pytest.mark.asyncio


# ── Fixture: happy baseline (all 10 checks pass) ────────────────────────────
@pytest.fixture
def happy_baseline(monkeypatch):
    """
    Patch every dependency of the 10 §82 staged checks to a *passing* state.

    This fixture is the deterministic foundation for the per-check failure
    tests below: each failing test requests ``happy_baseline`` and then
    overrides exactly ONE dependency (via its own ``monkeypatch.setattr``
    call) to flip a single check to ``passed=False``. The fixture's patches
    are applied in ``CHECK_ORDER`` so a reader can walk top-to-bottom and
    see which patch corresponds to which check.

    Why mock all 10 (not just the one under test)?
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Without a happy baseline, the default sandbox state (no closed trades,
    ml_model trained on synthetic-only, audit trail empty, kill-switch
    marker absent, settings.has_credentials=False, …) would fail 7+ checks
    simultaneously. A test that only asserts ``passed == False`` would
    pass trivially without proving that the *specific* check under test is
    the one that failed. By starting from an all-pass baseline and flipping
    exactly one check, each test proves the failure is *isolated* to that
    check — the ``blocking_checks == [<single_id>]`` assertion is the
    load-bearing guarantee.

    Restored on teardown
    ~~~~~~~~~~~~~~~~~~~~
    ``monkeypatch`` auto-reverts every ``setattr`` after the test, so the
    global singletons (``store``, ``settings``, ``ml_model``,
    ``drift_detector``, ``closed_positions``, ``audit_logger``,
    ``risk_manager``) return to their pre-test state for the next test.
    """
    # ── Check #1: paper_mode_24h — paper mode + 25h-old session ───────────
    # 25h comfortably exceeds PAPER_MODE_MIN_SECONDS (24h); using exactly
    # 24h would risk a flaky boundary failure if pytest startup latency
    # pushed the measured age below 86400.0s between fixture setup and the
    # check's ``time.time()`` read.
    monkeypatch.setattr("config.settings.trading_mode", "paper")
    monkeypatch.setattr(
        "core.data_store.store.session_start",
        time.time() - (PAPER_MODE_MIN_SECONDS + 3600.0),  # 25h ago
    )

    # ── Checks #2, #4, #5: closed_positions — 25 trades, +expectancy,
    #    60% win rate (above MIN_WIN_RATE=0.50 and at/above MIN_CLOSED_TRADES=20).
    #    A single mock return value satisfies all three checks because they
    #    each read different fields from the same stats dict.
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.get_closed_stats",
        AsyncMock(return_value={
            "count": MIN_CLOSED_TRADES + 5,        # 25 ≥ 20  → check #5 passes
            "avg_pnl": 0.50,                       # > 0      → check #2 passes
            "win_rate": MIN_WIN_RATE + 0.10,       # 0.60 > 0.50 → check #4 passes
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
    # training_source="real_and_synthetic" contains "real"; n_real_samples=500
    # > 0 → has_real=True → passed=True.
    #
    # ``is_fitted`` is a read-only ``@property`` on the ``MarketMLModel``
    # class (returns ``self.rf is not None``) — it has no setter, so
    # ``monkeypatch.setattr`` on the *instance* would fail at teardown with
    # ``AttributeError: property 'is_fitted' of 'MarketMLModel' object has
    # no setter``. Patching at the CLASS level replaces the property
    # descriptor with a plain ``True`` class attribute for the duration of
    # the test; on teardown monkeypatch restores the original property
    # descriptor (``setattr(cls, name, <property_obj>)`` reinstalls it as a
    # descriptor). ``training_source`` / ``n_real_samples`` /
    # ``n_synthetic_samples`` are plain instance attributes set in
    # ``__init__`` and patch normally on the singleton.
    monkeypatch.setattr("ml.model.MarketMLModel.is_fitted", True)
    monkeypatch.setattr("ml.model.ml_model.training_source", "real_and_synthetic")
    monkeypatch.setattr("ml.model.ml_model.n_real_samples", 500)
    monkeypatch.setattr("ml.model.ml_model.n_synthetic_samples", 1000)

    # ── Check #7: drift_detector — HEALTHY (the default, but made explicit
    #    so the drift failure test can override it without surprise).
    monkeypatch.setattr(
        "ml.drift_detector.drift_detector.drift_status",
        DRIFT_HEALTHY_STATUS,
    )

    # ── Check #8: kill_switch_tested — audit trail carries one activate AND
    #    one deactivate event (deactivation AFTER activation, so the
    #    ordered=True branch in the detail string fires). The marker file
    #    path resolves to /app/data/... which does not exist in the sandbox,
    #    so marker_present stays False and the audit evidence is the sole
    #    passing signal — exactly the canonical path the gate documents.
    now = time.time()
    monkeypatch.setattr(
        "core.audit_logger.audit_logger.get_recent_events",
        AsyncMock(return_value=[
            {"event_type": "kill_switch_activated", "timestamp": now - 200.0},
            {"event_type": "kill_switch_deactivated", "timestamp": now - 100.0},
        ]),
    )

    # ── Check #10: api_credentials_configured — both wallet key + CLOB API
    #    key/secret/passphrase present. ``has_credentials`` / ``has_api_keys``
    #    are read-only ``@property`` methods on the ``Settings`` class
    #    (derived from ``poly_private_key`` and
    #    ``poly_api_key`` / ``poly_api_secret`` / ``poly_api_passphrase``
    #    respectively), so we patch the *underlying* plain pydantic str
    #    fields — the properties then re-derive ``True`` from the non-empty
    #    underlying values. (Patching the property directly fails at
    #    teardown with ``AttributeError: property 'has_credentials' of
    #    'Settings' object has no setter`` because pydantic v2's
    #    ``__setattr__`` routes through the property's non-existent
    #    ``__set__``.)
    monkeypatch.setattr("config.settings.poly_private_key", "0x" + "a" * 64)
    monkeypatch.setattr("config.settings.poly_api_key", "test-api-key")
    monkeypatch.setattr("config.settings.poly_api_secret", "test-api-secret")
    monkeypatch.setattr("config.settings.poly_api_passphrase", "test-passphrase")


# ── 1. check_live_readiness returns exactly 10 checks ───────────────────────
async def test_check_live_readiness_returns_10_checks(happy_baseline):
    """``check_live_readiness()`` must return a verdict with ``total_count``
    == 10 and a ``checks`` list of length 10, in the exact staged order
    documented in ``CHECK_ORDER``.

    The 10-check count is the §82 gate's headline contract — an operator
    reading the dashboard trusts that exactly 10 staged checks exist, no
    more, no less. Drift in this count (e.g. a new check added without
    updating the dashboard) would silently break the dashboard's
    pass-count-to-total-count ratio. This test pins the count to 10 and
    the order to ``CHECK_ORDER``.
    """
    verdict = await check_live_readiness()

    # Top-level verdict contract: ``passed`` reflects whether ALL checks
    # passed; ``total_count`` / ``passed_count`` are integer counters.
    assert isinstance(verdict, dict)
    assert "passed" in verdict
    assert "checks" in verdict
    assert "total_count" in verdict
    assert "passed_count" in verdict
    assert "blocking_checks" in verdict
    assert "checked_at" in verdict

    # The headline contract: exactly 10 checks.
    assert verdict["total_count"] == 10
    assert len(verdict["checks"]) == 10

    # The checks are returned in the staged CHECK_ORDER (paper soak →
    # performance → ML governance → safety → credentials). Dashboard
    # rendering depends on this exact sequence.
    actual_ids = [c["id"] for c in verdict["checks"]]
    expected_ids = list(CHECK_ORDER)
    assert actual_ids == expected_ids, (
        f"check order mismatch: expected {expected_ids}, got {actual_ids}"
    )

    # With the happy baseline, every check passes — proving the baseline
    # fixture is correctly configured (if this fails, every downstream
    # "flip exactly one check" test would be unreliable because the
    # baseline itself was already failing).
    assert verdict["passed"] is True
    assert verdict["passed_count"] == 10
    assert verdict["blocking_checks"] == []


# ── 2. gate fails when paper mode < 24h ─────────────────────────────────────
async def test_gate_fails_when_paper_mode_under_24h(happy_baseline, monkeypatch):
    """When the continuous paper-mode session is < 24h old, the
    ``paper_mode_24h`` check must record ``passed=False`` and the gate's
    top-level ``passed`` must be ``False`` with ``CHECK_PAPER_MODE`` in
    ``blocking_checks``.

    Overrides ``store.session_start`` to ``time.time()`` (session just
    started → age = 0s, well under the 24h threshold) while leaving every
    other dependency at the happy-baseline passing state. Asserts that
    ``blocking_checks`` contains ONLY ``CHECK_PAPER_MODE`` — proving the
    failure is isolated to the paper-mode check, not a side-effect on a
    sibling check.
    """
    # Override: session_start = now → age = 0s < 24h threshold.
    monkeypatch.setattr("core.data_store.store.session_start", time.time())

    verdict = await check_live_readiness()

    # Gate as a whole fails.
    assert verdict["passed"] is False
    assert verdict["passed_count"] == 9  # 9 of 10 still pass
    assert verdict["total_count"] == 10

    # The paper_mode_24h check specifically failed.
    assert CHECK_PAPER_MODE in verdict["blocking_checks"]

    # The failure is isolated to paper_mode_24h — no other check was
    # perturbed by overriding session_start (which only check #1 reads).
    assert verdict["blocking_checks"] == [CHECK_PAPER_MODE]

    # The specific check's payload records the failure with a human-readable
    # detail string an operator can act on.
    paper_check = next(c for c in verdict["checks"] if c["id"] == CHECK_PAPER_MODE)
    assert paper_check["passed"] is False
    assert isinstance(paper_check["detail"], str)
    assert len(paper_check["detail"]) > 0
    # Detail should mention the session age / 24h threshold so the operator
    # knows what to fix.
    assert "24h" in paper_check["detail"] or "paper" in paper_check["detail"].lower()


# ── 3. gate fails when expectancy < 0 ───────────────────────────────────────
async def test_gate_fails_when_expectancy_negative(happy_baseline, monkeypatch):
    """When the average PnL per closed trade is negative, the
    ``positive_expectancy`` check must record ``passed=False``.

    Overrides ``closed_positions.get_closed_stats()`` to return 25 closed
    trades with ``avg_pnl = -0.50`` (negative expectancy) while keeping
    ``win_rate`` above the 50% threshold and ``count`` at/above 20 — so
    checks #4 (win_rate) and #5 (closed_trades) still pass, isolating the
    failure to check #2.
    """
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.get_closed_stats",
        AsyncMock(return_value={
            "count": MIN_CLOSED_TRADES + 5,          # 25 ≥ 20  → check #5 passes
            "avg_pnl": -0.50,                        # < 0      → check #2 FAILS
            "win_rate": MIN_WIN_RATE + 0.10,         # 0.60 > 0.50 → check #4 passes
        }),
    )

    verdict = await check_live_readiness()

    # Gate fails.
    assert verdict["passed"] is False
    assert CHECK_POSITIVE_EXPECTANCY in verdict["blocking_checks"]

    # Failure isolated to the expectancy check — sibling checks #4 (win
    # rate) and #5 (closed trade count) share the same stats dict but
    # read different fields, so they must still pass.
    assert verdict["blocking_checks"] == [CHECK_POSITIVE_EXPECTANCY]

    # The specific check's payload records the failure.
    exp_check = next(c for c in verdict["checks"] if c["id"] == CHECK_POSITIVE_EXPECTANCY)
    assert exp_check["passed"] is False
    assert isinstance(exp_check["detail"], str)
    assert len(exp_check["detail"]) > 0
    # Detail should reference the negative expectancy so the operator
    # knows the gate failed on expectancy specifically.
    assert "expectancy" in exp_check["detail"].lower() or "avg_pnl" in exp_check["detail"].lower()


# ── 4. gate fails when drift != HEALTHY ─────────────────────────────────────
async def test_gate_fails_when_drift_not_healthy(happy_baseline, monkeypatch):
    """When the ML drift detector reports a status other than ``HEALTHY``,
    the ``drift_healthy`` check must record ``passed=False``.

    Overrides ``drift_detector.drift_status`` to ``"DRIFT_DETECTED"`` (a
    plausible non-HEALTHY status the detector emits when PSI/KS statistics
    exceed their drift threshold). Every other dependency stays at the
    happy baseline, so the failure isolates to check #7.
    """
    monkeypatch.setattr(
        "ml.drift_detector.drift_detector.drift_status",
        "DRIFT_DETECTED",
    )

    verdict = await check_live_readiness()

    # Gate fails.
    assert verdict["passed"] is False
    assert CHECK_DRIFT_HEALTHY in verdict["blocking_checks"]

    # Failure isolated to the drift check.
    assert verdict["blocking_checks"] == [CHECK_DRIFT_HEALTHY]

    # The specific check's payload records the failure with the offending
    # status visible in the detail string.
    drift_check = next(c for c in verdict["checks"] if c["id"] == CHECK_DRIFT_HEALTHY)
    assert drift_check["passed"] is False
    assert isinstance(drift_check["detail"], str)
    assert "DRIFT_DETECTED" in drift_check["detail"]
    # The value block should expose the offending status for the dashboard.
    assert drift_check["value"]["drift_status"] == "DRIFT_DETECTED"


# ── 5. gate fails when < 20 closed trades ───────────────────────────────────
async def test_gate_fails_when_under_20_closed_trades(happy_baseline, monkeypatch):
    """When fewer than 20 closed trades exist, the ``min_20_closed_trades``
    check must record ``passed=False``.

    Overrides ``closed_positions.get_closed_stats()`` to return 5 closed
    trades with positive expectancy and >50% win rate — so checks #2
    (expectancy) and #4 (win_rate) still pass, isolating the failure to
    check #5 (the statistical-significance floor).
    """
    monkeypatch.setattr(
        "core.closed_positions.closed_positions.get_closed_stats",
        AsyncMock(return_value={
            "count": 5,                              # < 20     → check #5 FAILS
            "avg_pnl": 0.50,                         # > 0      → check #2 passes
            "win_rate": MIN_WIN_RATE + 0.10,         # 0.60 > 0.50 → check #4 passes
        }),
    )

    verdict = await check_live_readiness()

    # Gate fails.
    assert verdict["passed"] is False
    assert CHECK_CLOSED_TRADES in verdict["blocking_checks"]

    # Failure isolated to the closed-trades count check — sibling checks
    # #2 (expectancy) and #4 (win_rate) share the same stats dict but
    # read different fields, so they must still pass.
    assert verdict["blocking_checks"] == [CHECK_CLOSED_TRADES]

    # The specific check's payload records the failure.
    trades_check = next(c for c in verdict["checks"] if c["id"] == CHECK_CLOSED_TRADES)
    assert trades_check["passed"] is False
    assert isinstance(trades_check["detail"], str)
    assert len(trades_check["detail"]) > 0
    # Detail should reference the count gap so the operator knows how many
    # more closed trades are needed before the §82 statistical-significance
    # floor is met.
    assert "20" in trades_check["detail"]


# ── 6. POST /api/live/enable returns 409 when checks fail ──────────────────
async def test_enable_endpoint_returns_409_when_checks_fail(monkeypatch):
    """``POST /api/live/enable`` must refuse with HTTP 409 (Conflict) when
    any §82 check fails — never flipping live mode on.

    The route handler runs ``check_live_readiness()`` first; if the verdict
    is ``passed=False``, it raises ``HTTPException(status_code=409, ...)``
    with a structured ``detail`` payload containing the blocking-check list
    and the full readiness verdict. This test mocks
    ``check_live_readiness`` to return a failed verdict (so the test is
    independent of the live dependency state) and verifies the endpoint:
      - returns status 409;
      - the response body's ``detail`` carries the blocking-check list;
      - the response body's ``detail`` carries the full ``checks`` array
        (so the operator dashboard can render every check's status without
        a follow-up GET /api/live/readiness).

    Uses ``httpx.AsyncClient`` + ``ASGITransport`` rather than the sync
    ``fastapi.testclient.TestClient`` because every test in this module is
    async (the module-level ``pytestmark = pytest.mark.asyncio`` applies
    to all of them); ``TestClient`` would work but mixes sync portal
    semantics with the async test loop, which is needlessly fragile.
    """
    # Mock check_live_readiness to return a deterministically-failed
    # verdict. Patching the module global works because the route handler
    # ``_enable_live`` (defined inside ``register_routes``) resolves
    # ``check_live_readiness`` via the ``core.live_safety_gate`` module's
    # global namespace at call time — not via a closure-captured binding.
    failed_verdict: dict[str, Any] = {
        "passed": False,
        "checks": [
            {
                "id": CHECK_PAPER_MODE,
                "name": "Paper mode soak ≥ 24h",
                "passed": False,
                "severity": "BLOCKING",
                "threshold": "trading_mode=='paper' AND session_age_s >= 86400",
                "value": {"trading_mode": "paper", "session_age_seconds": 12.0},
                "detail": "paper session only 0.00h old (need ≥24h)",
            },
        ],
        "passed_count": 9,
        "total_count": 10,
        "blocking_checks": [CHECK_PAPER_MODE],
        "checked_at": time.time(),
    }
    monkeypatch.setattr(
        "core.live_safety_gate.check_live_readiness",
        AsyncMock(return_value=failed_verdict),
    )

    # Build a minimal FastAPI app and register only the live-safety-gate
    # routes (no auth middleware, no other endpoints — keeps the test
    # focused on the 409 contract).
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    register_routes(app)

    # POST with confirm=true (the defence-against-accidental-clicks guard).
    # If confirm were false, the endpoint would return 400 before even
    # running the gate — but the test's concern is the 409 path, which only
    # fires after the confirm check passes.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        response = await ac.post(
            "/api/live/enable",
            json={"confirm": True, "reason": "U4 unit test"},
        )

    # ── The headline contract: HTTP 409, not 200. ─────────────────────────
    assert response.status_code == 409

    # The 409 body carries the structured detail payload so an operator
    # dashboard can render every blocking check without a follow-up GET.
    body = response.json()
    assert "detail" in body
    detail = body["detail"]
    assert isinstance(detail, dict)

    # The blocking-check list is surfaced for the operator to act on.
    assert detail["blocking_checks"] == [CHECK_PAPER_MODE]
    assert detail["passed_count"] == 9
    assert detail["total_count"] == 10

    # The full checks array is included so the dashboard can render every
    # check's status (passing AND failing) in one round-trip.
    assert "checks" in detail
    assert isinstance(detail["checks"], list)
    assert len(detail["checks"]) == 1
    assert detail["checks"][0]["id"] == CHECK_PAPER_MODE
    assert detail["checks"][0]["passed"] is False

    # The guidance string tells the operator what to do next.
    assert "guidance" in detail
    assert isinstance(detail["guidance"], str)
    assert len(detail["guidance"]) > 0


# ── 7. all checks have name / passed / detail fields ────────────────────────
async def test_all_checks_have_name_passed_detail_fields(happy_baseline):
    """Every one of the 10 check dicts must carry the three contract fields
    ``name``, ``passed``, and ``detail`` — regardless of whether the check
    passed or failed.

    The dashboard / operator UI iterates ``checks`` and renders each row's
    name, pass/fail state, and detail string. A missing field would crash
    the dashboard mid-render. This test pins the schema: for every check,
    all three fields must be present, with the right types (``name`` is a
    non-empty string, ``passed`` is a bool, ``detail`` is a string).

    Run against the happy baseline so all checks PASS — verifying the
    schema holds on the passing path. (The failing path is exercised by
    tests 2-5 above, where each failing check's payload is also asserted
    to carry ``detail`` — the ``_failed()`` helper guarantees the same
    schema on the exception path.)
    """
    verdict = await check_live_readiness()

    assert verdict["total_count"] == 10
    checks = verdict["checks"]
    assert len(checks) == 10

    required_fields = ("name", "passed", "detail")

    for idx, check in enumerate(checks):
        # Every required field is present on every check.
        for field in required_fields:
            assert field in check, (
                f"check #{idx} (id={check.get('id', '<missing>')!r}) "
                f"is missing required field {field!r}: {check!r}"
            )

        # Type contracts the dashboard relies on:
        #   - name: non-empty human-readable string (rendered as the row label)
        #   - passed: bool (drives the pass/fail badge colour)
        #   - detail: string (may be empty in theory, but in practice the
        #     gate always populates it — empty is allowed by the schema,
        #     non-empty is the norm)
        assert isinstance(check["name"], str), (
            f"check #{idx} name must be str, got {type(check['name']).__name__}: "
            f"{check['name']!r}"
        )
        assert len(check["name"]) > 0, (
            f"check #{idx} (id={check.get('id')!r}) name is empty"
        )

        assert isinstance(check["passed"], bool), (
            f"check #{idx} (id={check.get('id')!r}) passed must be bool, "
            f"got {type(check['passed']).__name__}: {check['passed']!r}"
        )

        assert isinstance(check["detail"], str), (
            f"check #{idx} (id={check.get('id')!r}) detail must be str, "
            f"got {type(check['detail']).__name__}: {check['detail']!r}"
        )

    # With the happy baseline, every check passes — so the schema holds on
    # the passing path. (A companion assertion on the failing path is
    # implicit in tests 2-5: each failing check's ``detail`` is asserted
    # to be a non-empty string there.)
    assert verdict["passed"] is True
    assert all(c["passed"] for c in checks), (
        "happy_baseline should make every check pass — if this fails, the "
        "baseline fixture is misconfigured and tests 2-5's "
        "'blocking_checks == [<single_id>]' isolation assertions are "
        "unreliable"
    )
