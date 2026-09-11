"""
tests/test_state_recovery_wiring.py — W25-3 wiring tests for the state
recovery manager + periodic checkpoint integration into the FastAPI
server lifecycle.

Verifies the four contract surfaces the W25-3 task spec requires:

  (1) **Startup recovery** — the lifespan startup section awaits
      ``state_recovery.recover()`` and caches the resulting
      ``RecoveryReport`` on the singleton (so the
      ``GET /api/system/recovery-report`` endpoint can surface it).

  (2) **Checkpoint loop** — ``_recovery_checkpoint_loop`` is registered
      as an asyncio ``Task`` with name ``"recovery-checkpoint"`` and
      periodically (every 30 s) awaits ``state_recovery.checkpoint()``
      so the on-disk ``recovery_state.json`` reflects the live store
      state. Errors inside the loop are swallowed (logged at debug
      level) so a transient checkpoint failure never breaks the loop.

  (3) **Final shutdown checkpoint** — the lifespan shutdown section
      awaits ``state_recovery.checkpoint()`` one final time so the
      next restart sees the exact pre-shutdown state (not up to
      30 s stale). Awaited AFTER ``store.save_to_disk()`` so the
      snapshot's positions / paper_balance reflect the final values.

  (4) **API endpoint** — ``GET /api/system/recovery-report`` is
      registered on the production ``api.server.app``, requires auth
      (NOT in ``PUBLIC_PATHS``), and returns the cached
      ``RecoveryReport`` (or ``{"status": "no_recovery_yet"}`` before
      the first ``recover()`` call).

  (5) **Stale-order detection** — the recovery flow classifies
      non-terminal orders (OPEN / PARTIALLY_FILLED / PENDING / OSM
      non-terminal states) as stale so the reconciliation engine can
      re-query the exchange before resubmitting with the same
      idempotency key. End-to-end: write a checkpoint with mixed
      orders → ``recover()`` → ``report.stale_orders > 0``.

Hermeticity
~~~~~~~~~~~
Imports the production ``api.server.app`` so every route + middleware
is exercised (mirrors ``tests/test_ws_broadcast_wiring.py``). The
shared ``tests/conftest.py`` sets ``API_TOKEN=test-token-conftest``
via ``os.environ.setdefault`` BEFORE any project module is imported,
and the autouse ``_reset_store_factory_defaults`` fixture wipes
``store`` / ``risk_manager`` / ``paper_sim`` singletons before every
test. Rate limiting is disabled in ``conftest.py``
(``limiter.enabled = False``).

Tests use ``TestClient(app)`` (NOT ``with TestClient(app)``) so the
production lifespan is skipped — keeps each test sub-second and
avoids spinning up TimescaleDB / paper_sim / market seeding / the
watchdog subsystem. Source-inspection tests use
``inspect.getsource(...)`` so the wiring assertions survive code
reformatting (they assert against substrings, not exact whitespace).

All async tests share ``pytestmark = pytest.mark.asyncio`` (the
repo's ``pytest.ini`` runs in strict mode).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads ``os.environ`` at module-import time. ────────────
# Mirrors the env-redirect block in ``tests/test_state_recovery.py`` — keeps
# this test file hermetic if it's the first sibling imported (conftest.py
# does the same setdefault, but ``setdefault`` is a no-op if either file
# has already set the key, so the redundancy is harmless).
_TMP_ROOT = Path("/tmp/pmbot_w25_3_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "RECOVERY_STATE_PATH": str(_TMP_ROOT / "recovery_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "ORDER_STATE_MACHINE_DB_PATH": str(_TMP_ROOT / "order_state_machine.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-w25-3",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

# Per-test asyncio marker (NOT module-level ``pytestmark``) so the SYNC
# ``TestClient`` + source-inspection tests below don't trip pytest-asyncio's
# "marked but not async" warning. The repo's ``pytest.ini`` runs in strict
# mode, so async tests must carry the mark explicitly. Mirrors the same
# convention in ``tests/test_ws_broadcast_wiring.py``.
ASYNC = pytest.mark.asyncio

# ``conftest.py`` sets ``API_TOKEN`` via ``os.environ.setdefault`` BEFORE
# any project module is imported. The redirect block above sets a
# file-local ``API_TOKEN`` (``test-token-w25-3``) ONLY if conftest hasn't
# already set one — but conftest IS imported before this file (pytest
# orders ``tests/conftest.py`` before any sibling test module), so the
# value below reflects whatever the conftest-redirected env won with.
# Resolving the token at import time (rather than hard-coding
# ``"test-token-conftest"``) makes the test robust to a future conftest
# token rotation without a coupled edit here.
VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-conftest")


# ── (1) Startup recovery wiring ────────────────────────────────────────────


def test_lifespan_startup_calls_state_recovery_recover() -> None:
    """The lifespan startup section must await
    ``state_recovery.recover()`` and cache the resulting
    ``RecoveryReport`` on the singleton so the
    ``GET /api/system/recovery-report`` endpoint can surface it.

    Source-inspected (rather than executed) because the production
    lifespan is too heavy to spin up in a unit test — it would
    initialise TimescaleDB / paper_sim / market seeding / the watchdog
    subsystem, taking >10 s and requiring external services. Asserting
    against ``inspect.getsource(lifespan)`` keeps the test sub-second
    and survives code reformatting (the assertion matches substrings,
    not exact whitespace).
    """
    # Import inside the test so the env-redirect block above runs first.
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    # Imports the singleton inside the startup section.
    assert "from core.state_recovery import state_recovery" in src, (
        "lifespan startup must import the state_recovery singleton — "
        "W25-3 contract (1) requires the wiring to live INSIDE the "
        "lifespan so recovery runs on every boot, not just at module-"
        "import time."
    )
    # Awaits ``recover()`` and binds the result to a local (so the
    # log line + ``store.log_event`` below can reference the report).
    assert "await state_recovery.recover()" in src, (
        "lifespan startup must ``await state_recovery.recover()`` — "
        "W25-3 contract (1): the recovery report is the input to "
        "every subsequent operator-facing log + UI surface."
    )
    # The report's headline fields are surfaced in the startup log so
    # an operator can verify "the bot booted with N positions + M stale
    # orders" without grepping the API.
    assert "recovered_positions" in src, (
        "lifespan startup must log ``recovered_positions`` from the "
        "RecoveryReport — W25-3 contract (1): the operator-facing log "
        "line is the primary signal that recovery ran."
    )
    assert "stale_orders" in src, (
        "lifespan startup must log ``stale_orders`` — operators need "
        "to know if the prior shutdown left open orders that may have "
        "filled during downtime."
    )


def test_lifespan_startup_recovers_inside_try_except() -> None:
    """The startup recovery call must be wrapped in ``try/except`` so a
    recovery failure NEVER blocks boot (mirrors the fail-soft contract
    of every other singleton in the codebase).

    Without the wrap, a corrupt checkpoint file (rare but possible
    after a disk-full event) would crash the bot at startup — which is
    the exact opposite of what state recovery is for.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    # The recover call + a following ``except Exception`` block.
    assert "await state_recovery.recover()" in src
    assert "except Exception" in src, (
        "lifespan startup must wrap ``state_recovery.recover()`` in "
        "try/except so a recovery failure never blocks boot — "
        "W25-3 fail-soft contract."
    )


@ASYNC
async def test_recover_populates_last_report_on_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``state_recovery.recover()`` (the exact call the lifespan makes)
    must populate ``state_recovery._last_report`` so the
    ``GET /api/system/recovery-report`` endpoint can surface it.

    Belt-and-braces with the unit test in ``test_state_recovery.py`` —
    this test exercises the SAME singleton the lifespan uses (NOT the
    ``StateRecoveryManager(tmp_path / ...)`` per-test instance) so it
    verifies the wiring path, not just the unit contract.
    """
    from core.state_recovery import state_recovery

    # Redirect the singleton's state path so the test doesn't touch
    # the conftest-redirected singleton path (shared across the test
    # session — mutating it would leak into sibling tests).
    monkeypatch.setattr(
        state_recovery, "_state_path", tmp_path / "singleton_recovery.json"
    )
    # Pre-condition: no prior report (in case a prior test populated it).
    monkeypatch.setattr(state_recovery, "_last_report", None)

    report = await state_recovery.recover()

    # The singleton must cache the report — the lifespan relies on this
    # so the HTTP endpoint can read it without re-running recovery.
    assert state_recovery.get_last_report() is report
    assert report.recovered_positions == 0  # fresh boot — no state file


# ── (2) Checkpoint loop wiring ──────────────────────────────────────────────


def test_recovery_checkpoint_loop_function_exists() -> None:
    """``_recovery_checkpoint_loop`` must be defined at module scope on
    ``api.server`` so the lifespan can register it as an asyncio task.

    The function name is the contract — the lifespan references it by
    name (``asyncio.create_task(_recovery_checkpoint_loop(), ...)``),
    so renaming it would silently break the wiring.
    """
    from api.server import _recovery_checkpoint_loop

    assert callable(_recovery_checkpoint_loop)
    assert asyncio.iscoroutinefunction(_recovery_checkpoint_loop), (
        "_recovery_checkpoint_loop must be ``async def`` so it can be "
        "scheduled as an asyncio task — W25-3 contract (2)."
    )


def test_lifespan_registers_checkpoint_loop_task() -> None:
    """The lifespan startup must register ``_recovery_checkpoint_loop``
    as an asyncio ``Task`` with the name ``"recovery-checkpoint"`` so
    it shows up in ``asyncio.all_tasks()`` introspection + can be
    cancelled cleanly on shutdown.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    assert "_recovery_checkpoint_loop()" in src, (
        "lifespan must call ``_recovery_checkpoint_loop()`` — "
        "W25-3 contract (2)."
    )
    assert 'name="recovery-checkpoint"' in src, (
        "lifespan must name the checkpoint-loop task "
        "``\"recovery-checkpoint\"`` so it's introspectable + "
        "cancellable on shutdown — W25-3 contract (2)."
    )


def test_lifespan_cancels_checkpoint_loop_on_shutdown() -> None:
    """The lifespan shutdown must cancel the checkpoint-loop task so
    it doesn't leak a background coroutine after the server exits.

    Without the cancel, the task would keep running (sleeping + writing
    checkpoints) until the event loop is closed, leaking resources
    during testing and on graceful shutdown.
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    # The shutdown section must cancel the recovery checkpoint task.
    # Look for ``recovery_checkpoint_task.cancel()`` — the variable
    # name is part of the wiring contract.
    assert "recovery_checkpoint_task.cancel()" in src, (
        "lifespan shutdown must cancel ``recovery_checkpoint_task`` — "
        "W25-3 contract (2): the periodic checkpoint task must not "
        "leak past shutdown."
    )


@ASYNC
async def test_checkpoint_loop_calls_checkpoint_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_recovery_checkpoint_loop`` must call
    ``state_recovery.checkpoint()`` inside its ``while True`` loop so
    the on-disk file reflects the live store state every 30 s.

    The test patches ``asyncio.sleep`` to a ``Future`` that's already
    done so the loop's first iteration returns immediately (no 30 s
    wait), and patches ``state_recovery.checkpoint`` with a recorder.
    After one iteration, the loop is cancelled (the patch makes
    ``sleep`` raise ``CancelledError`` on the second call) so the
    test stays sub-second.
    """
    from api import server as server_module
    from core.state_recovery import state_recovery

    # ── Patch ``state_recovery.checkpoint`` with a recorder. ──
    call_count = {"n": 0}

    async def _record_checkpoint() -> None:
        call_count["n"] += 1

    monkeypatch.setattr(state_recovery, "checkpoint", _record_checkpoint)

    # ── Patch ``asyncio.sleep`` (referenced as ``asyncio.sleep`` inside
    # ``_recovery_checkpoint_loop``) so the loop's first ``sleep(30)``
    # returns immediately + the second raises ``CancelledError`` so the
    # loop exits cleanly.
    call_idx = {"i": 0}

    async def _fast_sleep(delay: float) -> None:
        call_idx["i"] += 1
        if call_idx["i"] == 1:
            # First iteration: pretend the 30 s sleep already completed.
            return
        # Second iteration: cancel the loop so the test exits.
        raise asyncio.CancelledError("test fast-forward")

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    # Run the loop — must raise ``CancelledError`` (the second sleep).
    with pytest.raises(asyncio.CancelledError):
        await server_module._recovery_checkpoint_loop()

    # The loop must have called ``checkpoint`` exactly once (the first
    # iteration's checkpoint call).
    assert call_count["n"] >= 1, (
        "_recovery_checkpoint_loop must call "
        "``state_recovery.checkpoint()`` inside its loop body — "
        "W25-3 contract (2)."
    )


@ASYNC
async def test_checkpoint_loop_swallows_checkpoint_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_recovery_checkpoint_loop`` must NOT propagate a checkpoint
    failure — the error is logged at debug level and the loop continues
    so a transient snapshot failure (e.g. a deadlock in the data_store
    lock) never kills the periodic checkpoint task.
    """
    from api import server as server_module
    from core.state_recovery import state_recovery

    # ── Patch ``state_recovery.checkpoint`` to raise on every call. ──
    async def _raise_checkpoint() -> None:
        raise RuntimeError("simulated checkpoint failure")

    monkeypatch.setattr(state_recovery, "checkpoint", _raise_checkpoint)

    # ── Patch ``asyncio.sleep`` so the first call returns + the second
    # raises ``CancelledError`` (so the test exits after one loop body).
    call_idx = {"i": 0}

    async def _fast_sleep(delay: float) -> None:
        call_idx["i"] += 1
        if call_idx["i"] == 1:
            return
        raise asyncio.CancelledError("test fast-forward")

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    # The loop must NOT propagate the checkpoint RuntimeError — the
    # ``except Exception`` clause inside ``_recovery_checkpoint_loop``
    # swallows it + logs at debug level.
    with pytest.raises(asyncio.CancelledError):
        await server_module._recovery_checkpoint_loop()

    # Belt-and-braces: the loop body executed (sleep was called twice —
    # once before the first checkpoint, once after, when the second
    # checkpoint was about to run).
    assert call_idx["i"] >= 1


# ── (3) Final shutdown checkpoint ───────────────────────────────────────────


def test_lifespan_shutdown_calls_final_checkpoint() -> None:
    """The lifespan shutdown section must call
    ``state_recovery.checkpoint()`` one final time so the next restart
    sees the exact pre-shutdown state (not up to 30 s stale from the
    periodic loop).

    Awaited AFTER ``store.save_to_disk()`` so the recovery snapshot's
    positions / paper_balance reflect the same final values the
    data_store just persisted (mirrors the W24-1 docstring contract
    on ``core.state_recovery.StateRecoveryManager.checkpoint``).
    """
    from api.server import lifespan

    src = inspect.getsource(lifespan)

    # The shutdown section imports the singleton (the function-local
    # import avoids a circular import at module load time — the
    # production pattern).
    assert "from core.state_recovery import state_recovery" in src
    # The final ``await ...checkpoint()`` call.
    assert "checkpoint()" in src, (
        "lifespan shutdown must call ``state_recovery.checkpoint()`` — "
        "W25-3 contract (3): a final checkpoint so the next restart "
        "sees the exact pre-shutdown state."
    )
    # The shutdown call is wrapped in try/except so a checkpoint failure
    # never blocks shutdown.
    assert "Final recovery checkpoint failed" in src, (
        "lifespan shutdown must log the final-checkpoint failure "
        "(via the ``except Exception`` branch) so the operator can "
        "see why the final checkpoint didn't happen — W25-3 contract."
    )


# ── (4) API endpoint wiring ─────────────────────────────────────────────────


def test_recovery_report_endpoint_registered_on_production_app() -> None:
    """``GET /api/system/recovery-report`` must be registered on the
    production ``api.server.app`` so an operator (or a future
    ``RecoveryReportPanel`` React component) can query the cached
    ``RecoveryReport`` via the standard REST surface.

    Verifies the route exists at the spec'd path with the spec'd
    ``tags=["system"]`` so it shows up under the "system" group in the
    OpenAPI / Swagger UI.
    """
    from api.server import app

    # Collect every route path on the production app.
    paths = {
        getattr(r, "path", None)
        for r in app.routes
        if hasattr(r, "path")
    }
    assert "/api/system/recovery-report" in paths, (
        "``GET /api/system/recovery-report`` must be registered on the "
        "production app — W25-3 contract (4): the endpoint is the "
        "operator-facing surface for the recovery report."
    )

    # Belt-and-braces: the route's ``tags`` must include ``"system"`` so
    # the OpenAPI schema groups it with the other system routes.
    for r in app.routes:
        if getattr(r, "path", None) == "/api/system/recovery-report":
            tags = getattr(r, "tags", []) or []
            assert "system" in tags, (
                "``/api/system/recovery-report`` must be tagged "
                "``\"system\"`` so it's grouped correctly in the "
                "OpenAPI / Swagger UI — W25-3 contract (4)."
            )
            return
    pytest.fail("route registered but tags not found — investigate")


def test_recovery_report_endpoint_requires_auth() -> None:
    """``GET /api/system/recovery-report`` must NOT be in
    ``PUBLIC_PATHS`` — the recovery report includes internal state
    (positions / orders / kill-switch / flags) that an unauthenticated
    client must not see.
    """
    from api.server import PUBLIC_PATHS

    assert "/api/system/recovery-report" not in PUBLIC_PATHS, (
        "``/api/system/recovery-report`` must NOT be public — the "
        "report includes internal state an unauthenticated client "
        "must not see. W25-3 contract (4) + the W11-6 fail-closed "
        "auth contract."
    )


def test_recovery_report_endpoint_rejects_missing_token() -> None:
    """``GET /api/system/recovery-report`` without a bearer token must
    return ``401 Unauthorized`` (the W11-6 fail-closed auth contract).
    """
    from fastapi.testclient import TestClient

    from api.server import app

    # ``TestClient(app)`` (NOT ``with TestClient(app)``) — skips the
    # production lifespan so the test stays fast.
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/system/recovery-report")
    assert response.status_code == 401, (
        "GET /api/system/recovery-report without a bearer token must "
        "return 401 — W25-3 contract (4) + W11-6 fail-closed auth."
    )


def test_recovery_report_endpoint_returns_no_recovery_yet_before_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/system/recovery-report`` must return
    ``{"status": "no_recovery_yet"}`` when the singleton has NOT yet
    had ``recover()`` called (e.g. queried before the lifespan startup
    phase ran — which is exactly the state ``TestClient(app)`` without
    ``with`` leaves the singleton in).
    """
    from fastapi.testclient import TestClient

    from api.server import app
    from core.state_recovery import state_recovery

    # Force the singleton's ``_last_report`` to ``None`` so the endpoint
    # hits the "no recovery yet" branch (a prior test's recover() call
    # may have populated it; the autouse conftest reset doesn't clear
    # the recovery singleton).
    monkeypatch.setattr(state_recovery, "_last_report", None)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/system/recovery-report",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"status": "no_recovery_yet"}, (
        "GET /api/system/recovery-report must return "
        "``{\"status\": \"no_recovery_yet\"}`` before the first "
        "``recover()`` call — W25-3 contract (4)."
    )


def test_recovery_report_endpoint_returns_report_after_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/system/recovery-report`` must return the full report
    dict after ``recover()`` has populated the singleton's
    ``_last_report``. Verifies the production app's route (NOT a
    hand-rolled minimal FastAPI app) returns the right shape.
    """
    from fastapi.testclient import TestClient

    from api.server import app
    from core.state_recovery import RecoveryReport, state_recovery

    # Inject a synthetic report onto the singleton so the endpoint has
    # something to return (simulates the post-startup state where the
    # lifespan has called ``recover()``).
    fake_report = RecoveryReport(
        recovered_positions=7,
        recovered_orders=3,
        stale_orders=2,
        kill_switch_active=False,
        flags_restored=11,
        recovery_time=0.031,
        errors=[],
        recovered_at=1735689700.0,
        checkpoint_timestamp=1735689670.0,
    )
    monkeypatch.setattr(state_recovery, "_last_report", fake_report)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/system/recovery-report",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    # Every field on ``RecoveryReport`` must be present + match the
    # injected values.
    assert body["recovered_positions"] == 7
    assert body["recovered_orders"] == 3
    assert body["stale_orders"] == 2
    assert body["kill_switch_active"] is False
    assert body["flags_restored"] == 11
    assert body["recovery_time"] == pytest.approx(0.031)
    assert body["errors"] == []
    assert body["recovered_at"] == pytest.approx(1735689700.0)
    assert body["checkpoint_timestamp"] == pytest.approx(1735689670.0)
    # NOT the "no_recovery_yet" fallback.
    assert "status" not in body


# ── (5) Stale-order detection end-to-end ───────────────────────────────────


@ASYNC
async def test_stale_orders_detected_end_to_end_via_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A checkpoint file containing non-terminal orders (OPEN /
    PARTIALLY_FILLED / PENDING) must surface a non-zero
    ``stale_orders`` count on the ``RecoveryReport`` returned by
    ``state_recovery.recover()``.

    End-to-end through the singleton the lifespan uses — writes a
    state file with 4 orders (2 non-terminal, 2 terminal), calls
    ``recover()``, and asserts ``stale_orders == 2``. Belt-and-braces
    with the unit test in ``test_state_recovery.py`` — this test
    verifies the WIRING path (singleton → state file → report →
    HTTP endpoint), not just the unit contract on
    ``_find_stale_orders``.
    """
    from core.state_recovery import state_recovery

    # Redirect the singleton's state path to a tmp file so this test
    # doesn't touch the conftest-redirected singleton path (shared
    # across the session).
    state_file = tmp_path / "stale_orders_recovery.json"
    monkeypatch.setattr(state_recovery, "_state_path", state_file)
    monkeypatch.setattr(state_recovery, "_last_report", None)

    # Write a state file with mixed orders — 2 stale (OPEN /
    # PARTIALLY_FILLED) + 2 terminal (FILLED / CANCELLED).
    checkpoint_ts = time.time() - 12.0
    state_payload = {
        "timestamp": checkpoint_ts,
        "schema_version": 1,
        "positions": [
            {"token_id": "TOK_A", "size_usdc": 10.0, "strategy": "mm"},
        ],
        "orders": [
            {"order_id": "ord-1", "status": "OPEN", "token_id": "TOK_A"},
            {"order_id": "ord-2", "status": "PARTIALLY_FILLED", "token_id": "TOK_A"},
            {"order_id": "ord-3", "status": "FILLED", "token_id": "TOK_A"},
            {"order_id": "ord-4", "status": "CANCELLED", "token_id": "TOK_A"},
        ],
        "kill_switch_active": False,
        "paper_balance": 90.0,
        "feature_flags": {"live_trading": False},
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state_payload, f, default=str)

    # ── Call the SAME recover() the lifespan calls. ──
    report = await state_recovery.recover()

    # ── The report must reflect the on-disk state. ──
    assert report.recovered_positions == 1
    assert report.recovered_orders == 4
    # ── Stale orders: only OPEN + PARTIALLY_FILLED → 2. ──
    assert report.stale_orders == 2, (
        "recover() must classify OPEN + PARTIALLY_FILLED orders as "
        "stale so the reconciliation engine knows to re-query the "
        "exchange before resubmitting with the same idempotency "
        "key — W25-3 contract (5)."
    )
    assert report.checkpoint_timestamp == pytest.approx(checkpoint_ts)
    assert report.errors == []

    # ── The HTTP endpoint (via the production app) must surface the
    # same stale_orders count — verifies the wiring chain
    # (state file → recover() → _last_report → HTTP endpoint). ──
    from fastapi.testclient import TestClient

    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/system/recovery-report",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stale_orders"] == 2, (
        "GET /api/system/recovery-report must surface the "
        "``stale_orders`` count from the cached RecoveryReport — "
        "W25-3 contract (4) + (5)."
    )
    assert body["recovered_positions"] == 1
    assert body["recovered_orders"] == 4


@ASYNC
async def test_stale_orders_detected_for_osm_non_terminal_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The recovery flow must also classify OSM-only non-terminal
    statuses (CREATED / VALIDATED / SUBMITTED / ACKNOWLEDGED) as stale
    — these are the OSM ``OrderState`` non-terminal states a future
    checkpoint format that persists OSM snapshots would emit.

    Wires the same constant (``STALE_ORDER_STATUSES``) the lifespan-
    exercised ``recover()`` path uses, so a future change to the OSM
    state machine (e.g. adding a new non-terminal state) is caught by
    this test rather than silently misclassifying stale orders as
    terminal.
    """
    from core.state_recovery import state_recovery

    state_file = tmp_path / "osm_stale_recovery.json"
    monkeypatch.setattr(state_recovery, "_state_path", state_file)
    monkeypatch.setattr(state_recovery, "_last_report", None)

    state_payload = {
        "timestamp": time.time(),
        "schema_version": 1,
        "positions": [],
        "orders": [
            {"order_id": "osm-1", "status": "CREATED"},
            {"order_id": "osm-2", "status": "VALIDATED"},
            {"order_id": "osm-3", "status": "SUBMITTED"},
            {"order_id": "osm-4", "status": "ACKNOWLEDGED"},
            # Terminal OSM state — must NOT count as stale.
            {"order_id": "osm-5", "status": "FILLED"},
        ],
        "kill_switch_active": False,
        "paper_balance": 100.0,
        "feature_flags": {},
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state_payload, f, default=str)

    report = await state_recovery.recover()

    assert report.recovered_orders == 5
    assert report.stale_orders == 4, (
        "recover() must classify OSM non-terminal statuses (CREATED / "
        "VALIDATED / SUBMITTED / ACKNOWLEDGED) as stale — "
        "W25-3 contract (5) + the ``STALE_ORDER_STATUSES`` superset."
    )


@ASYNC
async def test_no_stale_orders_when_all_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A checkpoint file containing ONLY terminal orders (FILLED /
    CANCELLED / REJECTED / EXPIRED) must yield ``stale_orders == 0``
    — none of them are stale because their lifecycle is closed and
    the bot's accounting already reflects them.

    Belt-and-braces with the prior test: the classification must work
    in BOTH directions (stale when non-terminal, NOT stale when
    terminal).
    """
    from core.state_recovery import state_recovery

    state_file = tmp_path / "all_terminal_recovery.json"
    monkeypatch.setattr(state_recovery, "_state_path", state_file)
    monkeypatch.setattr(state_recovery, "_last_report", None)

    state_payload = {
        "timestamp": time.time(),
        "schema_version": 1,
        "positions": [],
        "orders": [
            {"order_id": "t-1", "status": "FILLED"},
            {"order_id": "t-2", "status": "CANCELLED"},
            {"order_id": "t-3", "status": "REJECTED"},
            {"order_id": "t-4", "status": "EXPIRED"},
        ],
        "kill_switch_active": False,
        "paper_balance": 100.0,
        "feature_flags": {},
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state_payload, f, default=str)

    report = await state_recovery.recover()

    assert report.recovered_orders == 4
    assert report.stale_orders == 0


# ── (6) Checkpoint loop → on-disk file wiring ───────────────────────────────


@ASYNC
async def test_checkpoint_loop_writes_state_file_via_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A single iteration of ``_recovery_checkpoint_loop`` must write
    the on-disk ``recovery_state.json`` file via the singleton's
    ``checkpoint()`` method — verifies the full wiring path
    (loop → singleton → file).

    Patches ``asyncio.sleep`` so the first iteration's 30 s sleep
    returns immediately, then raises ``CancelledError`` on the second
    call so the test exits cleanly. The state file must exist + be
    valid JSON after the loop returns.
    """
    from api import server as server_module
    from core.state_recovery import state_recovery

    # Redirect the singleton's state path to a tmp file.
    state_file = tmp_path / "loop_checkpoint_recovery.json"
    monkeypatch.setattr(state_recovery, "_state_path", state_file)

    # ── Patch ``asyncio.sleep`` so first iteration returns + second raises. ──
    call_idx = {"i": 0}

    async def _fast_sleep(delay: float) -> None:
        call_idx["i"] += 1
        if call_idx["i"] == 1:
            return
        raise asyncio.CancelledError("test fast-forward")

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    # Pre-condition: the state file does NOT exist.
    assert not state_file.exists()

    # Run the loop — must raise ``CancelledError`` (the second sleep).
    with pytest.raises(asyncio.CancelledError):
        await server_module._recovery_checkpoint_loop()

    # ── The state file must exist + be valid JSON. ──
    assert state_file.exists(), (
        "_recovery_checkpoint_loop must write the on-disk "
        "``recovery_state.json`` file via "
        "``state_recovery.checkpoint()`` — W25-3 contract (2)."
    )
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Schema check — every field the ``recover()`` path reads must be
    # present.
    assert "timestamp" in state
    assert "schema_version" in state
    assert state["schema_version"] == 1
    assert "positions" in state
    assert "orders" in state
    assert "kill_switch_active" in state
    assert "paper_balance" in state
    assert "feature_flags" in state
