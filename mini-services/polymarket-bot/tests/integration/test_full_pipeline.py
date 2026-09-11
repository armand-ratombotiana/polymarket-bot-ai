"""Full signal-to-fill pipeline integration test.

W28-4 — End-to-end verification of the complete signal→order→fill chain.

Verifies the production pipeline wired in W18-1 (OSM) / W23-2 (latency
tracker) / W24-3 (pre-submission gate) / W24-6 (dedup registry) /
W19-3 (decision-ledger POSITION stage) actually traverses every stage
end-to-end:

  1. Signal generated → ``latency_tracker.record_signal`` records ``signal_time``
     AND the decision-ledger PREDICTION + SIGNAL stages land under one
     ``decision_id``.
  2. Pre-submission gate runs the 14-check suite (kill_switch / balance /
     exposure / single-position / open-orders / daily-loss / drawdown /
     data-freshness / spread / liquidity / min-edge / min-confidence /
     idempotency / circuit-breaker) — every check recorded in the audit
     trail.
  3. Idempotency check (the gate's check #13) blocks duplicate signals
     within the 5-minute TTL window.
  4. ``BaseStrategy.submit_order`` (paper mode) drives the OSM through
     CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN before
     returning the Order.
  5. Fill occurs → ``paper_sim._execute_fill`` books the trade,
     ``latency_tracker.record_fill`` records ``fill_time``, the decision
     ledger records FILL + POSITION stages, ``execution_quality`` records
     slippage / latency, and the OSM transitions to FILLED.
  6. Decision-ledger chain reconstructs as PREDICTION → SIGNAL →
     RISK_APPROVED → ORDER → FILL → POSITION under one ``decision_id``.
  7. Position updated on the store.
  8. Dedup registry prevents duplicate fill processing.

The four test methods below split this chain across four scenarios:

  * ``test_signal_to_fill_complete_chain`` — happy path, full chain.
  * ``test_duplicate_signal_blocked`` — idempotency short-circuits the
    duplicate signal at the pre-submission gate.
  * ``test_risk_gate_rejects_bad_order`` — pre-submission gate rejects
    an insufficient-balance order at the ``balance`` check.
  * ``test_latency_tracking`` — latency tracker records signal_time /
    order_time / fill_time and computes the three latency segments.

Hermeticity
-----------
The autouse ``_reset_store_factory_defaults`` fixture in
``tests/conftest.py`` wipes the global ``store`` / ``risk_manager`` /
``paper_sim`` singletons AND the ``dedup_registry`` /
``idempotency_manager`` / ``data_validator`` caches BEFORE every test.
This module adds belt-and-braces resets for the ``latency_tracker`` /
``clob_breaker`` / ``pre_submission_gate`` thresholds + a ``patched_osm``
fixture that swaps the OSM singleton for a ``tmp_path``-scoped instance
so the audit trail is writable and hermetic (mirrors
``tests/test_osm_integration.py``).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# ── Redirect ORDER_STATE_MACHINE_DB_PATH to /tmp BEFORE the project import.
# Belt-and-braces with ``tests/conftest.py`` (which does NOT redirect this
# env var, so the OSM singleton constructed at import time against
# ``/app/data/order_state_machine.db`` would silently fail every save()).
# Setting it here doesn't help the already-constructed singleton (the
# conftest's `from paper.simulator import paper_sim` line runs first and
# triggers the singleton's ctor), but the ``patched_osm`` fixture below
# swaps the singleton for a fresh test-scoped instance — the env redirect
# here is for completeness so a future refactor that constructs a second
# OSM against ``DB_PATH`` resolves to a writable path.
_TMP_ROOT = Path("/tmp/pmbot_full_pipeline_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault(
    "ORDER_STATE_MACHINE_DB_PATH", str(_TMP_ROOT / "order_state_machine.db")
)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``strategies.*``, ``api.*``)
# regardless of the cwd pytest was launched from. Mirrors the bootstrap
# pattern in every sibling ``tests/test_*.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from core.circuit_breaker import clob_breaker  # noqa: E402
from core.clob_client import OrderArgs  # noqa: E402
from core.data_store import (  # noqa: E402
    OrderBook,
    PriceLevel,
    Side,
    store,
)
from core.decision_ledger import (  # noqa: E402
    STAGE_FILL,
    STAGE_ORDER,
    STAGE_POSITION,
    STAGE_PREDICTION,
    STAGE_RISK_APPROVED,
    STAGE_SIGNAL,
    decision_ledger,
)
from core.dedup import dedup_registry  # noqa: E402
from core.execution_quality import DB_PATH as EXEC_DB_PATH  # noqa: E402
from core.execution_quality import get_execution_stats  # noqa: E402
from core.idempotency import idempotency_manager  # noqa: E402
from core.latency_tracker import latency_tracker  # noqa: E402
from core.order_state_machine import (  # noqa: E402
    OrderState,
    OrderStateMachine,
)
from core.pre_submission_gate import pre_submission_gate  # noqa: E402
from ml.model import ml_model  # noqa: E402
from paper.simulator import paper_sim  # noqa: E402
from strategies.base import BaseStrategy  # noqa: E402

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module (mirrors the convention in ``tests/integration/test_decision_chain.py``).
pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pipeline_singletons():
    """Clear every process-global singleton the pipeline test exercises.

    Belt-and-braces with the autouse ``_reset_store_factory_defaults``
    fixture in ``tests/conftest.py`` (which already resets the ``store`` /
    ``risk_manager`` / ``paper_sim`` / ``dedup_registry`` /
    ``idempotency_manager`` / ``data_validator`` singletons). This fixture
    adds the additional singletons conftest doesn't know about:

      * ``latency_tracker``  — W23-2 in-memory signal→order→fill tracker.
      * ``clob_breaker``     — CLOB circuit breaker (gate's check #14).

    And resets the pre-submission gate's thresholds to factory defaults
    so a prior test's ``configure`` call doesn't leak (mirrors the
    ``_reset_gate_thresholds`` fixture in
    ``tests/test_pre_submission_wiring.py``).
    """
    latency_tracker.reset()
    clob_breaker.reset()
    pre_submission_gate._min_edge = 0.03
    pre_submission_gate._min_confidence = 0.55
    pre_submission_gate._max_spread = 0.10
    pre_submission_gate._min_liquidity = 50.0
    pre_submission_gate._max_staleness_seconds = 60.0
    yield
    # Post-test teardown: clear again so a leak doesn't bleed into the
    # next test (the autouse conftest fixture runs before the next test
    # too, but explicit is safer for these particular singletons).
    latency_tracker.reset()
    clob_breaker.reset()


@pytest.fixture(autouse=True)
def _kill_switch_off(monkeypatch):
    """Patch the durable kill switch OFF for every test.

    The pre-submission gate's check #1 (``_check_kill_switch``) queries
    ``core.safety.kill_switch_file_exists()``. Belt-and-braces with the
    autouse ``_reset_store_factory_defaults`` fixture (which removes the
    marker file before every test): this fixture additionally guards
    against the file being re-created mid-test.
    """
    monkeypatch.setattr("core.safety.kill_switch_file_exists", lambda: False)


@pytest.fixture
def patched_osm(tmp_path, monkeypatch):
    """Swap the module-level ``osm`` singleton for a tmp_path-scoped instance.

    The autouse ``tests/conftest.py`` imports ``paper.simulator`` at
    module-load time, which transitively imports ``core.order_state_machine``.
    That means the OSM singleton is constructed against whatever path the
    env var pointed to AT IMPORT time — and ``tests/conftest.py`` doesn't
    set ``ORDER_STATE_MACHINE_DB_PATH`` before importing ``paper.simulator``,
    so the singleton falls back to ``/app/data/order_state_machine.db``
    (read-only in the sandbox). Every ``osm.save()`` call from production
    code paths would silently fail with a logged ``OperationalError``.

    This fixture mirrors the ``patched_osm`` fixture in
    ``tests/test_osm_integration.py``: swap BOTH ``core.order_state_machine
    .osm`` AND ``core.order_state_machine.order_state_machine`` for a fresh
    ``OrderStateMachine(tmp_path / "osm_full_pipeline.db")`` so the lazy
    ``from core.order_state_machine import osm; osm.transition(...)``
    imports inside ``BaseStrategy.submit_order`` /
    ``paper_sim.create_order`` / ``_execute_fill`` resolve to the
    test-scoped DB.
    """
    fresh = OrderStateMachine(tmp_path / "osm_full_pipeline.db")
    monkeypatch.setattr("core.order_state_machine.osm", fresh)
    monkeypatch.setattr("core.order_state_machine.order_state_machine", fresh)
    return fresh


@pytest.fixture
def deterministic_predict(monkeypatch):
    """Patch ``ml_model.predict`` to a deterministic BUY-leaning return.

    p_yes=0.85 clears the strategy's p_yes >= 0.55 gate; confidence=0.70
    clears the strategy's confidence >= 0.45 floor. Mirrors the
    ``deterministic_predict`` fixture in
    ``tests/integration/test_decision_chain.py``.
    """

    def fake_predict(features, token_id: str = "") -> tuple[float, float]:
        return 0.85, 0.70

    monkeypatch.setattr(ml_model, "predict", fake_predict)
    return fake_predict


@pytest.fixture(scope="module")
def client():
    """Module-scoped ``TestClient`` bound to the production FastAPI app.

    Used by the latency-tracking + full-pipeline tests to verify the
    ``GET /api/latency/stats`` + ``GET /api/latency/recent`` + ``POST
    /api/risk/pre-submission-check`` HTTP routes are wired into the
    production app and return the expected JSON shape. Mirrors the
    ``client`` fixture in ``tests/contract/conftest.py``.

    ``raise_server_exceptions=False`` so a 500-error response surfaces as
    a sanitized JSON body instead of a re-raised exception in the test
    process (mirrors ``tests/test_security.py``).
    """
    # Late import so the env-var redirects above are in effect when
    # ``api.server`` is imported (it reads env vars at module-import time).
    from api.server import app
    from fastapi.testclient import TestClient

    # Disable rate limiting (mirrors the conftest pattern — every TestClient
    # request presents the same source IP so the per-IP 120/min read cap
    # would 429 the suite halfway through).
    try:
        from api.server import limiter
        limiter.enabled = False
    except ImportError:  # pragma: no cover — defensive
        pass
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """``{Authorization: Bearer <token>}`` header for authenticated routes.

    Resolved dynamically from ``settings.api_token`` so the suite is
    robust to env-var overrides (mirrors the ``auth_headers`` fixture in
    ``tests/contract/conftest.py``). Defaults to the ``test-token-conftest``
    value the sibling ``tests/conftest.py`` sets via ``API_TOKEN``.
    """
    try:
        from config import settings
        token = (
            settings.api_token
            or os.environ.get("API_TOKEN", "test-token-conftest")
        )
    except Exception:  # noqa: BLE001 — defensive: never break test collection
        token = os.environ.get("API_TOKEN", "test-token-conftest")
    return {"Authorization": f"Bearer {token}"}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_mock_book(token_id: str, mid: float = 0.5) -> OrderBook:
    """Build a 2¢-spread order book with comfortable depth both sides.

    Mirrors the helper in ``tests/integration/test_decision_chain.py``.
    The 500-share top-of-book depth on each side guarantees a small
    (2-share) order pays only the flat 1-tick crossing penalty (no
    size-impact slippage), keeping the fill price deterministic enough
    for the latency / execution-quality assertions below.
    """
    return OrderBook(
        token_id=token_id,
        bids=[
            PriceLevel(price=round(mid - 0.01, 4), size=500.0),
            PriceLevel(price=round(mid - 0.02, 4), size=500.0),
        ],
        asks=[
            PriceLevel(price=round(mid + 0.01, 4), size=500.0),
            PriceLevel(price=round(mid + 0.02, 4), size=500.0),
        ],
    )


class _ConcreteStrategy(BaseStrategy):
    """Minimal concrete ``BaseStrategy`` subclass for the pipeline test.

    The async ``_run`` is a no-op await so the strategy can be constructed
    without booting its async loop. Paper mode is the default
    (``BaseStrategy.__init__`` reads ``settings.paper_trade`` which the
    conftest's ``TRADING_MODE=paper`` redirect sets to True). Mirrors the
    ``_StubStrategy`` pattern in ``tests/test_pre_submission_wiring.py``
    and the ``_ConcreteStrategy`` in ``tests/test_osm_integration.py``.
    """

    name: str = "full_pipeline_test"

    async def _run(self) -> None:  # pragma: no cover — not exercised here
        await asyncio.Event().wait()


# ── Test class ───────────────────────────────────────────────────────────────


class TestFullPipeline:
    """End-to-end pipeline test class.

    Each test method exercises one slice of the signal→order→fill chain
    against the REAL production code path (no mocked OSM / paper_sim /
    decision_ledger / latency_tracker — the only stub is ``ml_model.predict``,
    patched via the ``deterministic_predict`` fixture to a deterministic
    BUY-leaning return so the test is fast and reproducible).
    """

    async def test_signal_to_fill_complete_chain(
        self,
        client,
        auth_headers,
        deterministic_predict,
        patched_osm,
        monkeypatch,
    ):
        """Verify the complete signal→order→fill chain end-to-end.

        Drives the production code path:
          (1) PREDICTION — ``ml_model.predict()`` + ``decision_ledger.record``
          (2) SIGNAL     — ``decision_ledger.record(SIGNAL)`` +
                           ``latency_tracker.record_signal``
          (3) RISK       — ``pre_submission_gate.check`` approves (14 checks)
                           + ``decision_ledger.record(RISK_APPROVED)``
          (4) ORDER      — ``BaseStrategy.submit_order`` creates the OSM
                           entry, walks CREATED → VALIDATED → SUBMITTED →
                           ACKNOWLEDGED → OPEN, and ``paper_sim.create_order``
                           records the ORDER stage in the ledger.
          (5) FILL       — ``paper_sim._try_fill_orders`` fills the order;
                           ``paper_sim._execute_fill`` records FILL +
                           POSITION stages, ``latency_tracker.record_fill``,
                           and ``execution_quality.record_execution``.

        Then verifies:
          - Decision ledger has the 6-stage chain (PREDICTION → SIGNAL →
            RISK_APPROVED → ORDER → FILL → POSITION) under one ``decision_id``.
          - OSM history has the canonical 5-snapshot chain
            (CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN) and
            after the fill, the latest snapshot is FILLED with the
            ``fill_price`` stamped on metadata.
          - ``execution_quality`` row exists for the ``decision_id`` with
            ``slippage`` / ``slippage_bps`` / ``latency_ms`` populated.
          - ``latency_tracker`` has signal_time, order_time, fill_time
            all set and the record is marked complete.
          - Position is updated on the store.
          - Dedup registry blocks duplicate fill processing.
          - ``GET /api/latency/stats`` HTTP route returns 200 with at
            least one complete record.
        """
        # Bypass the risk-engine gate (the W18-1 / W24-3 wiring is the
        # thing under test; the existing 22-gate risk engine is exercised
        # by ``tests/integration/test_risk_pipeline.py``).
        async def _approve(_order):
            return True, "OK"

        monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

        token_id = f"TEST_PIPELINE_{uuid.uuid4().hex[:8]}"
        await store.update_order_book(_build_mock_book(token_id))
        book = store.order_books[token_id]
        mid = book.mid or 0.5
        spread = book.spread or 0.02
        best_ask = book.best_ask
        assert best_ask is not None

        correlation_id = decision_ledger.new_decision_id()
        assert correlation_id.startswith("dec-"), (
            f"unexpected decision_id prefix: {correlation_id!r}"
        )

        # ── (1) PREDICTION ──────────────────────────────────────────────
        features = np.zeros(38, dtype=np.float32)
        features[0] = mid
        p_yes, confidence = ml_model.predict(features, token_id=token_id)
        assert p_yes == pytest.approx(0.85)
        predicted_edge = p_yes - mid

        await decision_ledger.record(
            decision_id=correlation_id,
            stage=STAGE_PREDICTION,
            token_id=token_id,
            strategy=_ConcreteStrategy.name,
            pnl=0.0,
            p_yes=p_yes,
            confidence=confidence,
            market_mid=mid,
            spread=spread,
            predicted_edge=predicted_edge,
        )

        # ── (2) SIGNAL ──────────────────────────────────────────────────
        target_price = round(min(best_ask + 0.001, 0.98), 4)
        size_usdc = 1.50
        size_shares = max(1.0, size_usdc / target_price)
        reason_str = f"ML Prob={p_yes:.1%} (edge={predicted_edge*100:.1f}%)"

        await decision_ledger.record(
            decision_id=correlation_id,
            stage=STAGE_SIGNAL,
            token_id=token_id,
            strategy=_ConcreteStrategy.name,
            pnl=0.0,
            direction=Side.BUY.value,
            target_price=target_price,
            size_usdc=size_usdc,
            p_yes=p_yes,
            confidence=confidence,
            market_mid=mid,
            reason=reason_str,
        )
        # W23-2 — record the signal timestamp against the latency tracker.
        latency_tracker.record_signal(
            correlation_id=correlation_id,
            token_id=token_id,
            strategy=_ConcreteStrategy.name,
        )

        # ── (3) RISK — pre-submission gate (14 checks), called INSIDE
        # ``submit_order``. We spy on ``pre_submission_gate.check`` so
        # the test can assert on the gate's 14-check audit trail WITHOUT
        # calling the gate directly (a direct call would record the
        # ``(strategy, token_id, side, price, size)`` 5-tuple in the
        # idempotency cache, then submit_order's internal call would
        # see it as a duplicate and reject — the spy invokes the real
        # gate logic and captures the result for the test's assertions
        # without the duplicate-cache-poisoning side effect).
        captured_gate: dict = {}
        real_check = pre_submission_gate.check

        def _spy_check(order_request, market_data=None, account_state=None):
            result = real_check(order_request, market_data, account_state)
            captured_gate["result"] = result
            captured_gate["order_request"] = order_request
            captured_gate["market_data"] = market_data
            captured_gate["account_state"] = account_state
            return result

        monkeypatch.setattr(pre_submission_gate, "check", _spy_check)

        # ── (4) ORDER — BaseStrategy.submit_order (paper mode) ──────────
        # ``submit_order`` internally:
        #   (a) consults the W24-6 dedup_registry (passes — autouse
        #       fixture cleared it before this test),
        #   (b) creates the OSM entry (CREATED snapshot),
        #   (c) calls ``pre_submission_gate.check`` via the spy above,
        #   (d) calls ``risk_manager.check_order`` (mocked to approve),
        #   (e) records the RISK_APPROVED decision-ledger stage,
        #   (f) transitions OSM CREATED → VALIDATED,
        #   (g) records order_time on the latency tracker,
        #   (h) calls ``paper_sim.create_order`` which records the
        #       ORDER stage and transitions OSM SUBMITTED → ACKNOWLEDGED
        #       → OPEN.
        strat = _ConcreteStrategy()
        args = OrderArgs(
            token_id=token_id, price=target_price, side=Side.BUY, size=size_shares
        )
        # Sleep briefly so signal→order latency is non-trivial (>0ms).
        await asyncio.sleep(0.005)
        paper_order = await strat.submit_order(args, decision_id=correlation_id)

        assert paper_order is not None, (
            "submit_order should return the paper Order, not None "
            "(the gate + risk both approve in this happy path)"
        )

        # The gate was called (via the spy).
        assert "result" in captured_gate, (
            "pre_submission_gate.check was not called by submit_order"
        )
        gate_result = captured_gate["result"]
        assert gate_result.approved, (
            f"pre-submission gate should approve a small paper BUY; "
            f"got rejection_category={gate_result.rejection_category!r} "
            f"reason={gate_result.rejection_reason!r}"
        )
        # 14 checks are recorded (kill_switch + 6 account-state + 3 market
        # data + min_edge + min_confidence + idempotency + circuit_breaker).
        # NOTE: the gate was called WITHOUT market_data / account_state
        # (submit_order's default ``gate_context=None`` path), so the
        # account-state and market-data checks are recorded as PASSED
        # with the explicit "skipped — no input data" message. The
        # kill_switch / idempotency / circuit_breaker checks are always
        # enforced.
        assert len(gate_result.checks) == 14, (
            f"expected 14 checks; got {len(gate_result.checks)}"
        )
        assert paper_order.paper is True
        assert paper_order.token_id == token_id
        assert paper_order.decision_id == correlation_id
        # Order is now in store.open_orders (OPEN status).
        assert paper_order.order_id in store.open_orders

        # OSM audit trail: CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED
        # → OPEN (5 hops) before the fill.
        history = patched_osm.get_history(paper_order.order_id)
        states = [h.state for h in history]
        assert states == [
            OrderState.CREATED,
            OrderState.VALIDATED,
            OrderState.SUBMITTED,
            OrderState.ACKNOWLEDGED,
            OrderState.OPEN,
        ], (
            f"expected canonical 5-hop OSM chain (CREATED → VALIDATED → "
            f"SUBMITTED → ACKNOWLEDGED → OPEN); got {states}"
        )

        # ── (5) FILL — paper_sim._try_fill_orders ───────────────────────
        # Sleep briefly so order→fill latency is non-trivial (>0ms).
        await asyncio.sleep(0.005)
        await paper_sim._try_fill_orders()

        # OSM transitioned to FILLED with fill metadata.
        post = patched_osm.get_order(paper_order.order_id)
        assert post is not None, (
            "OSM entry should still exist after the fill"
        )
        assert post.state == OrderState.FILLED, (
            f"OSM should be FILLED after the fill loop; got {post.state}"
        )
        assert post.filled_size == pytest.approx(size_shares, rel=1e-3), (
            f"OSM filled_size should match order size; got {post.filled_size}"
        )
        assert "fill_price" in post.metadata, (
            f"OSM FILLED snapshot should stamp fill_price on metadata; "
            f"got {post.metadata!r}"
        )

        # ── (6) Decision ledger chain (6 stages) ────────────────────────
        chain = await decision_ledger.get_chain(correlation_id)
        stages = [row["stage"] for row in chain]
        # W19-3 — 6-stage chain: PREDICTION → SIGNAL → RISK_APPROVED →
        # ORDER → FILL → POSITION.
        assert stages == [
            STAGE_PREDICTION,
            STAGE_SIGNAL,
            STAGE_RISK_APPROVED,
            STAGE_ORDER,
            STAGE_FILL,
            STAGE_POSITION,
        ], f"unexpected chain stage order: {stages}"
        # Every row carries the same decision_id (the correlation id that
        # threads the chain).
        assert all(row["decision_id"] == correlation_id for row in chain), (
            "chain rows do not share a single decision_id (correlation id)"
        )
        assert all(row["token_id"] == token_id for row in chain), (
            "chain rows do not share a single token_id"
        )
        # Timestamps monotonically non-decreasing.
        timestamps = [row["timestamp"] for row in chain]
        assert timestamps == sorted(timestamps), (
            f"chain timestamps not in chronological order: {timestamps}"
        )

        # ── (7) Execution quality row recorded ──────────────────────────
        rows: list[dict] = []
        with sqlite3.connect(EXEC_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM execution_quality WHERE decision_id = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (correlation_id,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        assert len(rows) == 1, (
            f"expected exactly 1 execution_quality row for {correlation_id}; "
            f"got {len(rows)}"
        )
        eq = rows[0]
        assert eq["decision_id"] == correlation_id
        assert eq["token_id"] == token_id
        assert eq["strategy"] == _ConcreteStrategy.name
        assert eq["order_id"] == paper_order.order_id
        assert eq["paper"] == 1
        assert eq["side"] == "BUY"
        # Slippage / latency are populated (not NULL).
        assert eq["actual_fill"] is not None and eq["actual_fill"] > 0
        assert eq["slippage"] is not None
        assert eq["slippage_bps"] is not None
        assert eq["latency_ms"] is not None
        assert eq["latency_ms"] >= 0
        # Aggregate stats now reflect at least one fill.
        stats = get_execution_stats()
        assert stats["count"] >= 1
        assert stats["by_side"]["BUY"] >= 1

        # ── (8) Latency tracker record ─────────────────────────────────
        recent = latency_tracker.get_recent(10)
        matching = [
            r for r in recent if r["correlation_id"] == correlation_id
        ]
        assert len(matching) == 1, (
            f"latency_tracker should have 1 record for {correlation_id}; "
            f"got {len(matching)}"
        )
        rec = matching[0]
        assert rec["signal_time"] is not None
        assert rec["order_time"] is not None
        assert rec["fill_time"] is not None
        assert rec["complete"] is True
        assert rec["signal_to_order_ms"] is not None
        assert rec["order_to_fill_ms"] is not None
        assert rec["signal_to_fill_ms"] is not None
        # All three segments non-negative.
        assert rec["signal_to_order_ms"] >= 0
        assert rec["order_to_fill_ms"] >= 0
        assert rec["signal_to_fill_ms"] >= 0
        # Latency ordering invariant: signal_to_fill ≈ signal_to_order +
        # order_to_fill (within clock-jitter epsilon).
        assert (
            rec["signal_to_fill_ms"]
            >= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] - 1.0
        ), (
            f"signal_to_fill ({rec['signal_to_fill_ms']}) should be "
            f"≥ signal_to_order + order_to_fill - 1ms"
        )
        assert (
            rec["signal_to_fill_ms"]
            <= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] + 1.0
        ), (
            f"signal_to_fill ({rec['signal_to_fill_ms']}) should be "
            f"≤ signal_to_order + order_to_fill + 1ms"
        )

        # ── (9) Position updated on the store ──────────────────────────
        assert token_id in store.positions, (
            "Position should exist on the store after a BUY fill"
        )
        pos = store.positions[token_id]
        assert pos.yes_shares > 0, "yes_shares should be > 0 after a BUY fill"
        assert pos.avg_entry_price > 0, (
            "avg_entry_price should be > 0 after a BUY fill"
        )

        # ── (10) Dedup registry blocks duplicate fill ───────────────────
        # The fill loop's dedup key is ``paper:{order_id}`` — a second
        # attempt to add it must return False (blocked).
        fill_key = f"paper:{paper_order.order_id}"
        blocked = dedup_registry.check_and_add(
            "fill", fill_key, ttl_seconds=3600
        )
        assert blocked is False, (
            "dedup_registry should block a duplicate fill key for an "
            "order that was already filled (the W24-6 dedup gate is the "
            "guard against a re-entered _execute_fill double-booking a fill)"
        )

        # ── (11) HTTP sanity: GET /api/latency/stats returns 200 ────────
        response = client.get("/api/latency/stats", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/latency/stats should return 200; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        # At least one complete record (ours).
        assert body["total_records"] >= 1, (
            f"latency stats should reflect >=1 record; got {body}"
        )
        assert body["complete_records"] >= 1, (
            f"latency stats should reflect >=1 complete record; got {body}"
        )
        # Per-strategy breakdown includes our strategy.
        assert _ConcreteStrategy.name in body["by_strategy"], (
            f"by_strategy should include {_ConcreteStrategy.name!r}; "
            f"got {body['by_strategy']}"
        )

    async def test_duplicate_signal_blocked(
        self,
        client,
        auth_headers,
        deterministic_predict,
        patched_osm,
        monkeypatch,
    ):
        """Verify duplicate signals are blocked by idempotency.

        The pre-submission gate's check #13 (``_check_idempotency``) uses
        a deterministic SHA-256 over the ``(strategy, token_id, side,
        price, size)`` 5-tuple so a strategy that fires the same signal
        twice in quick succession is caught before the second order
        reaches the exchange. The TTL window is 5 minutes
        (``_DEFAULT_TTL_SECONDS = 300.0``).

        This test calls ``pre_submission_gate.check`` twice with the
        SAME 5-tuple (different ``order_id``). The first call records
        the ``(key, order_id, now)`` triple in the idempotency cache;
        the second call returns ``approved=False, rejection_category=
        "idempotency"`` because the key was already recorded within the
        TTL window.

        Also exercises the production ``POST /api/risk/pre-submission-check``
        HTTP route so the wiring invariant (gate reachable via the API)
        is verified end-to-end.
        """
        # Bypass risk gate (not under test here).
        async def _approve(_order):
            return True, "OK"

        monkeypatch.setattr("risk.manager.risk_manager.check_order", _approve)

        token_id = f"TEST_DUP_{uuid.uuid4().hex[:8]}"
        order_request = {
            "token_id": token_id,
            "side": "BUY",
            "size": 2.0,
            "price": 0.50,
            "strategy": "dup_test_strategy",
            "order_id": "dup-ord-1",
            "edge": 0.05,
            "confidence": 0.65,
        }
        market_data = {
            "best_bid": 0.48,
            "best_ask": 0.52,
            "spread": 0.04,
            "liquidity": 250.0,
            "last_update": time.time(),
            "mid": 0.50,
        }
        account_state = {
            "balance": 100.0,
            "total_exposure": 0.0,
            "open_orders": 0,
            "daily_pnl": 0.0,
            "drawdown": 0.0,
            "max_total_exposure": 25.0,
            "max_single_position": 3.0,
            "max_open_orders": 8,
            "daily_loss_limit": -2.0,
            "max_drawdown_limit": 0.15,
        }

        # ── First call — approves (no prior key in the idempotency cache).
        result1 = pre_submission_gate.check(
            order_request=order_request,
            market_data=market_data,
            account_state=account_state,
        )
        assert result1.approved, (
            f"first call should approve; got rejection_category="
            f"{result1.rejection_category!r} reason={result1.rejection_reason!r}"
        )
        # The idempotency check passed (is_dup=False).
        idem_check_1 = next(
            c for c in result1.checks if c.check_name == "idempotency"
        )
        assert idem_check_1.passed is True
        assert idem_check_1.value is False, (  # is_dup=False
            "first call's idempotency value should be False (no dup)"
        )

        # ── Second call — same 5-tuple, different order_id. The
        # idempotency key is the SAME (deterministic SHA-256 over the
        # 5-tuple, ignoring order_id), so the second call is flagged as
        # a duplicate.
        order_request_2 = dict(order_request)
        order_request_2["order_id"] = "dup-ord-2"
        result2 = pre_submission_gate.check(
            order_request=order_request_2,
            market_data=market_data,
            account_state=account_state,
        )
        assert result2.approved is False, (
            "second call with same 5-tuple should be blocked by idempotency"
        )
        assert result2.rejection_category == "idempotency", (
            f"rejection_category should be 'idempotency'; got "
            f"{result2.rejection_category!r}"
        )
        # The idempotency check failed (is_dup=True).
        idem_check_2 = next(
            c for c in result2.checks if c.check_name == "idempotency"
        )
        assert idem_check_2.passed is False
        assert idem_check_2.value is True, (  # is_dup=True
            "second call's idempotency value should be True (dup detected)"
        )
        # The rejection_reason references the original order_id.
        assert "dup-ord-1" in (idem_check_2.message or ""), (
            f"rejection message should mention the original order_id "
            f"'dup-ord-1'; got {idem_check_2.message!r}"
        )

        # ── HTTP wiring: POST /api/risk/pre-submission-check mirrors
        # the direct gate call. The first request approves; the second
        # (with a different order_id but same 5-tuple) is blocked with
        # rejection_category="idempotency".
        api_payload_1 = {
            "order_request": order_request,
            "market_data": market_data,
            "account_state": account_state,
        }
        # NOTE: the second HTTP call needs a DIFFERENT order_id (the
        # first HTTP call already recorded the key in the singleton's
        # cache — the in-process gate singleton is shared between the
        # direct calls above AND the HTTP route).
        api_payload_2 = {
            "order_request": order_request_2,
            "market_data": market_data,
            "account_state": account_state,
        }

        # The first HTTP call would be a duplicate of the second direct
        # call (same 5-tuple) — it'd be blocked by idempotency. So we
        # only assert the route is reachable + returns the right shape
        # on the BLOCKED payload (which is what the test is really
        # verifying: the HTTP route surfaces gate rejections).
        response = client.post(
            "/api/risk/pre-submission-check",
            json=api_payload_2,
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/risk/pre-submission-check should return 200 "
            f"(rejection in body, not HTTP status); got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        assert body["approved"] is False, (
            f"HTTP route should surface the idempotency rejection; "
            f"got body={body}"
        )
        assert body["rejection_category"] == "idempotency", (
            f"HTTP route's rejection_category should be 'idempotency'; "
            f"got {body.get('rejection_category')!r}"
        )

    async def test_risk_gate_rejects_bad_order(
        self,
        client,
        auth_headers,
        deterministic_predict,
        patched_osm,
    ):
        """Verify the pre-submission gate rejects orders that fail risk checks.

        Constructs an account_state whose ``balance`` ($0.50) is below
        the order cost (``size=2.0 * price=0.50 = $1.00``). The
        ``balance`` check (check #2) must fail, surfacing
        ``rejection_category="balance"`` and a rejection_reason that
        mentions the balance and the cost.

        Verifies:
          - ``approved=False`` and ``rejection_category="balance"``.
          - The ``balance`` check entry exists in ``checks`` with
            ``passed=False``, value=$0.50, threshold=$1.00.
          - The ``kill_switch`` check (check #1) still passed (kill
            switch is OFF via the autouse fixture).
          - All 14 checks are recorded (every check runs even when one
            fails — the audit trail records every check's outcome).
          - The HTTP route ``POST /api/risk/pre-submission-check``
            surfaces the same rejection.
        """
        token_id = f"TEST_BAD_{uuid.uuid4().hex[:8]}"
        order_request = {
            "token_id": token_id,
            "side": "BUY",
            "size": 2.0,   # cost = 2.0 * 0.50 = $1.00
            "price": 0.50,
            "strategy": "bad_order_test",
            "order_id": "bad-ord-1",
            "edge": 0.05,
            "confidence": 0.65,
        }
        account_state = {
            "balance": 0.50,            # $0.50 < $1.00 cost → balance fails
            "total_exposure": 0.0,
            "open_orders": 0,
            "daily_pnl": 0.0,
            "drawdown": 0.0,
            "max_total_exposure": 25.0,
            "max_single_position": 3.0,
            "max_open_orders": 8,
            "daily_loss_limit": -2.0,
            "max_drawdown_limit": 0.15,
        }
        market_data = {
            "best_bid": 0.48,
            "best_ask": 0.52,
            "spread": 0.04,
            "liquidity": 250.0,
            "last_update": time.time(),
            "mid": 0.50,
        }

        result = pre_submission_gate.check(
            order_request=order_request,
            market_data=market_data,
            account_state=account_state,
        )
        assert result.approved is False, (
            "insufficient-balance order must be rejected"
        )
        assert result.rejection_category == "balance", (
            f"rejection_category should be 'balance'; got "
            f"{result.rejection_category!r}"
        )
        # The balance check is in the checks list and is the first
        # failing check.
        balance_check = next(
            c for c in result.checks if c.check_name == "balance"
        )
        assert balance_check.passed is False
        assert balance_check.value == pytest.approx(0.50), (
            f"balance check value should be $0.50; got {balance_check.value}"
        )
        assert balance_check.threshold == pytest.approx(1.00), (
            f"balance check threshold should be $1.00 (the order cost); "
            f"got {balance_check.threshold}"
        )
        assert "balance" in balance_check.message.lower(), (
            f"balance check message should mention 'balance'; got "
            f"{balance_check.message!r}"
        )

        # The kill_switch check (check #1) still passed — it runs first
        # and the kill switch is OFF (autouse fixture).
        kill_switch_check = next(
            c for c in result.checks if c.check_name == "kill_switch"
        )
        assert kill_switch_check.passed is True, (
            f"kill_switch check should pass (autouse fixture is OFF); "
            f"got value={kill_switch_check.value!r} "
            f"message={kill_switch_check.message!r}"
        )

        # 14 checks are recorded (every check runs even when one fails —
        # the audit trail records every check's outcome so operators can
        # see at a glance which checks failed and which passed).
        assert len(result.checks) == 14, (
            f"expected 14 checks (every check runs even on rejection); "
            f"got {len(result.checks)}"
        )

        # ── HTTP wiring: POST /api/risk/pre-submission-check surfaces
        # the same rejection.
        response = client.post(
            "/api/risk/pre-submission-check",
            json={
                "order_request": order_request,
                "market_data": market_data,
                "account_state": account_state,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"POST /api/risk/pre-submission-check should return 200 "
            f"(rejection in body, not HTTP status); got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        assert body["approved"] is False, (
            f"HTTP route should surface the balance rejection; got body={body}"
        )
        assert body["rejection_category"] == "balance", (
            f"HTTP route's rejection_category should be 'balance'; "
            f"got {body.get('rejection_category')!r}"
        )
        assert len(body["checks"]) == 14, (
            f"HTTP route should return 14 checks; got {len(body['checks'])}"
        )

    async def test_latency_tracking(
        self,
        client,
        auth_headers,
        deterministic_predict,
    ):
        """Verify signal-to-fill latency is tracked.

        Drives the latency tracker through the three-stage pipeline
        (signal → order → fill) with realistic sleeps between stages so
        the latency segments are non-trivial, then verifies:

          - The record is complete (fill_time is set).
          - All three timestamps (signal_time, order_time, fill_time)
            are set.
          - All three latency segments (signal_to_order_ms,
            order_to_fill_ms, signal_to_fill_ms) are populated and
            non-negative.
          - The latency ordering invariant holds:
            signal_to_fill ≈ signal_to_order + order_to_fill.
          - ``get_stats`` returns at least 1 complete record.
          - The ``by_strategy`` breakdown includes the test strategy.
          - The HTTP routes ``GET /api/latency/stats`` + ``GET
            /api/latency/recent`` surface the record.
        """
        correlation_id = f"dec-latency-{uuid.uuid4().hex[:8]}"
        token_id = f"TEST_LAT_{uuid.uuid4().hex[:8]}"
        strategy = "latency_test_strategy"

        # Stage 1: signal.
        latency_tracker.record_signal(
            correlation_id=correlation_id,
            token_id=token_id,
            strategy=strategy,
        )
        # Sleep 5ms so the signal→order segment is non-trivial.
        await asyncio.sleep(0.005)

        # Stage 2: order.
        latency_tracker.record_order(correlation_id=correlation_id)
        # Sleep 5ms so the order→fill segment is non-trivial.
        await asyncio.sleep(0.005)

        # Stage 3: fill.
        latency_tracker.record_fill(correlation_id=correlation_id)

        # ── Verify the record ──────────────────────────────────────────
        recent = latency_tracker.get_recent(10)
        matching = [
            r for r in recent if r["correlation_id"] == correlation_id
        ]
        assert len(matching) == 1, (
            f"latency_tracker should have 1 record for {correlation_id}; "
            f"got {len(matching)}"
        )
        rec = matching[0]
        # All three timestamps populated.
        assert rec["signal_time"] is not None, "signal_time must be set"
        assert rec["order_time"] is not None, "order_time must be set"
        assert rec["fill_time"] is not None, "fill_time must be set"
        # Record marked complete.
        assert rec["complete"] is True, (
            "record must be marked complete after record_fill"
        )
        # All three latency segments populated.
        assert rec["signal_to_order_ms"] is not None, (
            "signal_to_order_ms must be computed after record_order"
        )
        assert rec["order_to_fill_ms"] is not None, (
            "order_to_fill_ms must be computed after record_fill"
        )
        assert rec["signal_to_fill_ms"] is not None, (
            "signal_to_fill_ms must be computed after record_fill"
        )
        # Each segment is non-negative.
        assert rec["signal_to_order_ms"] >= 0
        assert rec["order_to_fill_ms"] >= 0
        assert rec["signal_to_fill_ms"] >= 0
        # The signal→order and order→fill segments should be at least
        # ~4ms each (we slept 5ms between stages). Allow a generous
        # upper bound for CI runners (5s).
        assert 4.0 <= rec["signal_to_order_ms"] <= 5_000, (
            f"signal_to_order_ms should be ~5ms; got "
            f"{rec['signal_to_order_ms']}"
        )
        assert 4.0 <= rec["order_to_fill_ms"] <= 5_000, (
            f"order_to_fill_ms should be ~5ms; got "
            f"{rec['order_to_fill_ms']}"
        )
        # Latency ordering invariant: signal_to_fill ≈ signal_to_order +
        # order_to_fill (within clock-jitter epsilon of ±1ms).
        assert (
            rec["signal_to_fill_ms"]
            >= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] - 1.0
        ), (
            f"signal_to_fill ({rec['signal_to_fill_ms']}) should be "
            f"≥ signal_to_order + order_to_fill - 1ms"
        )
        assert (
            rec["signal_to_fill_ms"]
            <= rec["signal_to_order_ms"] + rec["order_to_fill_ms"] + 1.0
        ), (
            f"signal_to_fill ({rec['signal_to_fill_ms']}) should be "
            f"≤ signal_to_order + order_to_fill + 1ms"
        )

        # ── Verify get_stats ──────────────────────────────────────────
        stats = latency_tracker.get_stats(hours=24.0)
        assert stats["total_records"] >= 1, (
            f"get_stats should reflect >=1 record; got {stats}"
        )
        assert stats["complete_records"] >= 1, (
            f"get_stats should reflect >=1 complete record; got {stats}"
        )
        # By-strategy breakdown includes our strategy.
        assert strategy in stats["by_strategy"], (
            f"by_strategy should include {strategy!r}; "
            f"got {stats['by_strategy']}"
        )
        strat_row = stats["by_strategy"][strategy]
        assert strat_row["count"] >= 1
        assert strat_row["signal_to_order_p95_ms"] >= 0
        assert strat_row["order_to_fill_p95_ms"] >= 0
        assert strat_row["signal_to_fill_p95_ms"] >= 0

        # ── HTTP sanity: GET /api/latency/stats ────────────────────────
        response = client.get(
            "/api/latency/stats",
            params={"hours": 24.0},
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/latency/stats should return 200; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        assert body["total_records"] >= 1
        assert body["complete_records"] >= 1
        assert strategy in body["by_strategy"], (
            f"HTTP /api/latency/stats by_strategy should include "
            f"{strategy!r}; got {body['by_strategy']}"
        )

        # ── HTTP sanity: GET /api/latency/recent ──────────────────────
        response = client.get(
            "/api/latency/recent",
            params={"limit": 50},
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/latency/recent should return 200; got "
            f"{response.status_code}. Body: {response.text!r}"
        )
        body = response.json()
        assert isinstance(body, list), (
            f"GET /api/latency/recent should return a list; got {type(body)}"
        )
        # Our record is in the recent list.
        matching_http = [
            r for r in body if r.get("correlation_id") == correlation_id
        ]
        assert len(matching_http) == 1, (
            f"GET /api/latency/recent should surface our record; "
            f"got {len(matching_http)} matches"
        )
        http_rec = matching_http[0]
        assert http_rec["complete"] is True
        assert http_rec["fill_time"] is not None
        assert http_rec["signal_to_fill_ms"] is not None
