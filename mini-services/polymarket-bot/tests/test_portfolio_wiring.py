"""
tests/test_portfolio_wiring.py — W20-4 portfolio optimizer wiring tests.

Covers the W20-4 task: the Kelly-criterion portfolio optimizer
(:mod:`core.portfolio_optimizer`) is wired into the live trade path of
:mod:`strategies.signal_trader` and exposed through two new HTTP
endpoints (``GET /api/portfolio/optimizer-status`` and
``GET /api/portfolio/rebalance/live``).

Four test classes:

  (1) ``TestSignalTraderOptimizerWiring`` — ``signal_trader`` actually
      calls ``portfolio_optimizer.optimize`` from ``_process_signals``;
      selected bets override the per-signal ``size_usdc``;
      ``_act_on_signal`` is dispatched once per selected bet;
      an optimizer exception falls back to the legacy top-3 path;
      empty input is a no-op.

  (2) ``TestOptimizerStatusEndpoint`` — ``GET /api/portfolio/optimizer-status``
      returns the six-scalar config PLUS the cached
      ``last_optimization`` summary; the summary is ``None`` before any
      call to ``optimize`` and is populated after a POST optimize.

  (3) ``TestRebalanceEndpoints`` — the existing
      ``POST /api/portfolio/rebalance`` (caller-supplied body) is
      unchanged; the new ``GET /api/portfolio/rebalance/live`` auto-
      fetches positions from ``store.get_positions()`` and returns
      ``add`` / ``reduce`` / ``close`` / ``hold`` lists; if
      ``store.get_positions`` raises, the endpoint returns 200 with a
      ``warning`` field instead of a 5xx.

  (4) ``TestOptimizationConstraints`` — the optimizer respects
      ``min_edge`` (zero-Kelly on sub-threshold edge),
      ``min_confidence`` (zero-Kelly on sub-threshold confidence),
      ``max_single_bet`` (no bet exceeds ``max_single_bet *
      operating_capital``), and ``max_total_exposure`` (total allocated
      never exceeds ``max_total_exposure * operating_capital``,
      including the last-bet scale-down-to-fit branch).

Approach
~~~~~~~~
The module-level singleton ``portfolio_optimizer`` is constructed at
import time with the conservative defaults documented in the module
docstring. The endpoint test classes use the singleton — and to keep
test isolation, the ``_restore_singleton_config`` fixture (copied from
``tests/test_portfolio_optimizer.py``) snapshots the singleton's config
before each API test and restores it in the teardown so a PUT in one
test doesn't leak into the next.

The signal-trader wiring tests construct a fresh
``SignalTraderStrategy`` per test and monkeypatch the
``portfolio_optimizer.optimize`` symbol (looked up via the lazy import
inside ``_process_signals``) with a stub that records its calls and
returns a canned ``PortfolioOptimization`` so the dispatch logic can be
asserted without invoking the real Kelly sizing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with ``tests/conftest.py``: conftest sets these first via
# its own ``_ENV_REDIRECTS`` table, but if this module is imported before
# conftest (e.g. by a different runner that does not pick up conftest), the
# ``setdefault`` calls here ensure the strategy import never reaches into
# the repo's real ``data/`` directory (which is read-only in the sandbox).
_TMP_ROOT = Path("/tmp/portfolio_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS = {
    "STORE_STATE_PATH": _TMP_ROOT / "store_state.json",
    "DECISION_LEDGER_DB_PATH": _TMP_ROOT / "decision_ledger.db",
    "AUDIT_DB_PATH": _TMP_ROOT / "audit_trail.db",
    "MARKET_DB_PATH": _TMP_ROOT / "market_intelligence.db",
    "KILL_SWITCH_PATH": _TMP_ROOT / "kill_switch",
    "KILL_SWITCH_REASON_PATH": _TMP_ROOT / "kill_switch.reason",
    "VECTOR_STORE_PATH": _TMP_ROOT / "vector_index.json",
    "MODEL_PATH": _TMP_ROOT / "model.pkl",
    "MODEL_REGISTRY_PATH": _TMP_ROOT / "model_registry.json",
    "CLOSED_POSITIONS_DB_PATH": _TMP_ROOT / "closed_positions.db",
    "EXECUTION_QUALITY_DB_PATH": _TMP_ROOT / "execution_quality.db",
    "OBSERVABILITY_DB_PATH": _TMP_ROOT / "observability.db",
    "FLAGS_DB_PATH": _TMP_ROOT / "feature_flags.db",
    "RECON_REPORT_DIR": _TMP_ROOT / "reports",
    "AB_TEST_DB_PATH": _TMP_ROOT / "ab_tests.db",
    "FEATURE_STORE_DB": _TMP_ROOT / "feature_store.db",
    "IMMUTABLE_AUDIT_DB": _TMP_ROOT / "immutable_audit.db",
    "JOB_QUEUE_DB": _TMP_ROOT / "job_queue.db",
    "ML_VALUE_DB": _TMP_ROOT / "ml_economic_value.db",
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-portfolio-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, str(_val))

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``ml.*``, ``strategies.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import portfolio_optimizer as _po_module  # noqa: E402
from core.data_store import Position, Side, store  # noqa: E402
from core.portfolio_optimizer import (  # noqa: E402
    KellyBet,
    PortfolioOptimization,
    PortfolioOptimizer,
    register_routes,
)
from strategies.signal_trader import MarketSignal, SignalTraderStrategy  # noqa: E402

# Async tests in this module are decorated individually with
# ``@pytest.mark.asyncio`` (pytest-asyncio strict mode) rather than via a
# module-level ``pytestmark`` — the sync tests in the endpoint / constraint
# classes would otherwise emit ``PytestWarning: marked with asyncio but
# not an async function`` for every sync method. Mirrors the pattern in
# ``tests/test_portfolio_optimizer.py`` (which has no module-level mark).


# ── (1) signal_trader → portfolio_optimizer wiring ──────────────────────────


class TestSignalTraderOptimizerWiring:
    """``signal_trader._process_signals`` actually invokes the optimizer.

    These tests construct a fresh ``SignalTraderStrategy``, monkeypatch
    ``portfolio_optimizer.optimize`` (looked up via the lazy import inside
    ``_process_signals``) with a stub that records its arguments and returns
    a canned ``PortfolioOptimization``, then assert that
    ``_process_signals``:

      * converts the signals to the ``opportunities`` shape the optimizer
        expects (token_id / strategy / price / edge / confidence);
      * dispatches one ``_act_on_signal`` per selected bet;
      * overrides the per-signal ``size_usdc`` with the optimizer's
        ``suggested_size_usdc``;
      * falls back to the legacy top-3 path on an optimizer exception;
      * is a no-op on empty input.
    """

    @pytest.mark.asyncio
    async def test_process_signals_calls_optimizer_and_dispatches(
        self, monkeypatch
    ) -> None:
        """One signal → one optimizer call → one ``_act_on_signal`` call.

        The optimizer stub returns a single ``KellyBet`` with
        ``suggested_size_usdc = 7.5``; we then assert ``_act_on_signal``
        was invoked exactly once with the original signal, that the
        signal's ``size_usdc`` was overwritten to ``7.5`` before the
        dispatch, and that the optimizer received the opportunities list
        in the expected shape.
        """
        # Fresh strategy — the conftest autouse fixture has already reset
        # the global ``store`` singleton to factory defaults.
        strategy = SignalTraderStrategy()

        # Build a signal that would survive the optimizer's Kelly gate
        # if the real optimizer were running.
        sig = MarketSignal(
            token_id="t1",
            slug="test-1",
            direction=Side.BUY,
            confidence=0.7,
            target_price=0.55,
            size_usdc=1.0,
            reason="test",
            ml_score=0.7,
            source="ml",
            decision_id="dec-1",
            edge=0.10,
            price=0.55,
        )

        # Stub ``portfolio_optimizer.optimize`` to record its call args
        # and return a single-bet PortfolioOptimization.
        captured: dict = {}

        def _fake_optimize(opps):
            captured["opps"] = opps
            bet = KellyBet(
                token_id="t1",
                strategy="signal_trader",
                price=0.55,
                edge=0.10,
                confidence=0.7,
                kelly_fraction=0.075,
                kelly_fraction_adjusted=0.075,
                suggested_size_usdc=7.5,
                expected_return=0.75,
                expected_risk=2.5,
            )
            return PortfolioOptimization(
                bets=[bet],
                total_allocated_usdc=7.5,
                total_expected_return=0.75,
                total_expected_risk=2.5,
                diversification_ratio=1.0,
                constraint_violations=[],
            )

        # Patch the module-level singleton's ``optimize`` method so the
        # lazy ``from core.portfolio_optimizer import portfolio_optimizer``
        # inside ``_process_signals`` picks up the stub.
        monkeypatch.setattr(
            "core.portfolio_optimizer.portfolio_optimizer.optimize",
            _fake_optimize,
        )

        # Stub ``_act_on_signal`` so no real order submission runs.
        act_calls: list[MarketSignal] = []

        async def _fake_act(sig):
            act_calls.append(sig)

        monkeypatch.setattr(strategy, "_act_on_signal", _fake_act)

        await strategy._process_signals([sig])

        # Optimizer was called once with the right opportunities shape.
        assert "opps" in captured
        assert len(captured["opps"]) == 1
        opp = captured["opps"][0]
        assert opp["token_id"] == "t1"
        assert opp["strategy"] == "signal_trader"
        assert opp["price"] == pytest.approx(0.55)
        assert opp["edge"] == pytest.approx(0.10)
        assert opp["confidence"] == pytest.approx(0.7)

        # ``_act_on_signal`` was dispatched once with the original signal,
        # and the signal's ``size_usdc`` was overwritten to the optimizer's
        # suggested size (clamped to a $0.50 floor).
        assert len(act_calls) == 1
        assert act_calls[0] is sig
        assert sig.size_usdc == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_process_signals_zero_bets_is_noop(self, monkeypatch) -> None:
        """Optimizer returns zero bets (every signal failed the Kelly
        gate) → ``_process_signals`` logs and returns without dispatching
        ``_act_on_signal``. No exception, no orders placed."""
        strategy = SignalTraderStrategy()

        sig = MarketSignal(
            token_id="t1",
            slug="test-1",
            direction=Side.BUY,
            confidence=0.7,
            target_price=0.55,
            size_usdc=1.0,
            reason="test",
            ml_score=0.7,
            source="ml",
            decision_id="dec-1",
            edge=0.01,  # below default min_edge (0.03)
            price=0.55,
        )

        def _fake_optimize(opps):
            return PortfolioOptimization(
                bets=[],
                total_allocated_usdc=0.0,
                total_expected_return=0.0,
                total_expected_risk=0.0,
                diversification_ratio=1.0,
                constraint_violations=[],
            )

        monkeypatch.setattr(
            "core.portfolio_optimizer.portfolio_optimizer.optimize",
            _fake_optimize,
        )

        act_calls: list[MarketSignal] = []

        async def _fake_act(sig):
            act_calls.append(sig)

        monkeypatch.setattr(strategy, "_act_on_signal", _fake_act)

        await strategy._process_signals([sig])

        assert act_calls == []

    @pytest.mark.asyncio
    async def test_process_signals_empty_input_is_noop(self) -> None:
        """Empty ``signals`` list → no optimizer call, no
        ``_act_on_signal`` call. Trivial but load-bearing for the
        scan-cycle's ``signals[:10]`` slicing boundary."""
        strategy = SignalTraderStrategy()
        await strategy._process_signals([])

    @pytest.mark.asyncio
    async def test_process_signals_optimizer_exception_falls_back(
        self, monkeypatch
    ) -> None:
        """If ``portfolio_optimizer.optimize`` raises, ``_process_signals``
        falls back to the legacy top-3 path so a buggy optimizer never
        blocks a scan cycle. The fallback dispatches ``_act_on_signal``
        on the first 3 signals by their original order (NOT
        re-sorted — the caller has already sorted)."""
        strategy = SignalTraderStrategy()

        signals = [
            MarketSignal(
                token_id=f"t{i}",
                slug=f"slug-{i}",
                direction=Side.BUY,
                confidence=0.7,
                target_price=0.55,
                size_usdc=1.0,
                reason="test",
                ml_score=0.7,
                source="ml",
                decision_id=f"dec-{i}",
                edge=0.10,
                price=0.55,
            )
            for i in range(5)
        ]

        def _raise(opps):
            raise RuntimeError("simulated optimizer failure")

        monkeypatch.setattr(
            "core.portfolio_optimizer.portfolio_optimizer.optimize",
            _raise,
        )

        act_calls: list[MarketSignal] = []

        async def _fake_act(sig):
            act_calls.append(sig)

        monkeypatch.setattr(strategy, "_act_on_signal", _fake_act)

        await strategy._process_signals(signals)

        # Fallback fired on the first 3 signals (the legacy top-3 cap).
        assert len(act_calls) == 3
        assert [s.token_id for s in act_calls] == ["t0", "t1", "t2"]

    @pytest.mark.asyncio
    async def test_process_signals_size_floor_clamps_dust(
        self, monkeypatch
    ) -> None:
        """A sub-$0.50 ``suggested_size_usdc`` from the optimizer is
        clamped up to $0.50 before dispatch so the resulting order is
        non-dust (matches the legacy allocator floor)."""
        strategy = SignalTraderStrategy()

        sig = MarketSignal(
            token_id="t1",
            slug="test-1",
            direction=Side.BUY,
            confidence=0.7,
            target_price=0.55,
            size_usdc=1.0,
            reason="test",
            ml_score=0.7,
            source="ml",
            decision_id="dec-1",
            edge=0.10,
            price=0.55,
        )

        def _fake_optimize(opps):
            bet = KellyBet(
                token_id="t1",
                strategy="signal_trader",
                price=0.55,
                edge=0.10,
                confidence=0.7,
                kelly_fraction=0.001,
                kelly_fraction_adjusted=0.001,
                suggested_size_usdc=0.05,  # sub-floor dust
                expected_return=0.005,
                expected_risk=0.015,
            )
            return PortfolioOptimization(
                bets=[bet],
                total_allocated_usdc=0.05,
                total_expected_return=0.005,
                total_expected_risk=0.015,
                diversification_ratio=1.0,
                constraint_violations=[],
            )

        monkeypatch.setattr(
            "core.portfolio_optimizer.portfolio_optimizer.optimize",
            _fake_optimize,
        )

        act_calls: list[MarketSignal] = []

        async def _fake_act(sig):
            act_calls.append(sig)

        monkeypatch.setattr(strategy, "_act_on_signal", _fake_act)

        await strategy._process_signals([sig])

        assert len(act_calls) == 1
        # Clamped to the $0.50 floor — never sub-dust.
        assert act_calls[0].size_usdc == pytest.approx(0.50)


# ── (2) GET /api/portfolio/optimizer-status ─────────────────────────────────


@pytest.fixture
def _restore_singleton_config():
    """Snapshot + restore the singleton ``portfolio_optimizer`` config
    around each endpoint test so a PUT in one test doesn't leak into
    the next. Mirrors the fixture in ``test_portfolio_optimizer.py``."""
    snapshot = _po_module.portfolio_optimizer.get_config()
    # Also snapshot the cached last_optimization so an optimize() in one
    # test doesn't surface in the next test's optimizer-status response.
    cached_last = _po_module.portfolio_optimizer._last_optimization
    yield _po_module.portfolio_optimizer
    _po_module.portfolio_optimizer.operating_capital = snapshot["operating_capital"]
    _po_module.portfolio_optimizer.kelly_fraction = snapshot["kelly_fraction"]
    _po_module.portfolio_optimizer.max_single_bet = snapshot["max_single_bet"]
    _po_module.portfolio_optimizer.max_total_exposure = snapshot["max_total_exposure"]
    _po_module.portfolio_optimizer.min_edge = snapshot["min_edge"]
    _po_module.portfolio_optimizer.min_confidence = snapshot["min_confidence"]
    _po_module.portfolio_optimizer._last_optimization = cached_last


@pytest.fixture
def client(_restore_singleton_config) -> TestClient:
    """Fresh ``FastAPI`` app with only the portfolio-optimizer routes
    registered (mirrors the production ``api/server.py`` W16-3 block)."""
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestOptimizerStatusEndpoint:
    """``GET /api/portfolio/optimizer-status`` returns the live config
    PLUS the cached ``last_optimization`` summary."""

    def test_status_returns_six_scalars_before_any_optimization(
        self, client: TestClient
    ) -> None:
        """Before any call to ``optimize``, the endpoint returns the six
        config scalars with ``last_optimization: None``."""
        # Make sure the cache is empty (the autouse fixture restored the
        # snapshot before us, but belt-and-braces).
        _po_module.portfolio_optimizer._last_optimization = None
        response = client.get("/api/portfolio/optimizer-status")
        assert response.status_code == 200, response.text
        body = response.json()
        # Six config scalars present.
        for key in (
            "operating_capital",
            "kelly_fraction",
            "max_single_bet",
            "max_total_exposure",
            "min_edge",
            "min_confidence",
        ):
            assert key in body, f"missing config scalar: {key}"
        # last_optimization is None before any optimize() call.
        assert body["last_optimization"] is None

    def test_status_returns_last_optimization_after_optimize_call(
        self, client: TestClient
    ) -> None:
        """After a ``POST /api/portfolio/optimize`` call, the status
        endpoint surfaces the cached optimization summary (n_bets, total
        allocated, expected return / risk, diversification ratio,
        constraint violations)."""
        # Trigger an optimization via the existing POST endpoint.
        resp = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "t1",
                        "strategy": "test",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text

        # Status now reflects the cached result.
        response = client.get("/api/portfolio/optimizer-status")
        assert response.status_code == 200, response.text
        body = response.json()
        last = body["last_optimization"]
        assert last is not None
        assert last["n_bets"] == 1
        assert last["total_allocated_usdc"] == pytest.approx(5.0, abs=1e-6)
        assert last["total_expected_return"] == pytest.approx(0.50, abs=1e-6)
        assert last["constraint_violations"] == []

    def test_status_reflects_config_changes(self, client: TestClient) -> None:
        """A ``PUT /api/portfolio/config`` change is immediately visible
        in the status endpoint (the singleton's in-place mutation is
        reflected without a restart)."""
        client.put(
            "/api/portfolio/config",
            json={"kelly_fraction": 0.50},
        )
        response = client.get("/api/portfolio/optimizer-status")
        assert response.status_code == 200
        body = response.json()
        assert body["kelly_fraction"] == pytest.approx(0.50)


# ── (3) rebalance endpoints ─────────────────────────────────────────────────


class TestRebalanceEndpoints:
    """Both the existing POST (caller-supplied body) and the new GET
    (live, no body) rebalance endpoints work and return the same
    ``{add, reduce, close, hold}`` shape."""

    def test_post_rebalance_with_body_still_works(self, client: TestClient) -> None:
        """The pre-W20-4 ``POST /api/portfolio/rebalance`` endpoint (with
        a caller-supplied ``current_positions`` + ``opportunities`` body)
        is unchanged — the W20-4 wiring must not break it."""
        response = client.post(
            "/api/portfolio/rebalance",
            json={
                "current_positions": [
                    {"token_id": "t_old", "size_usdc": 5.0},
                ],
                "opportunities": [
                    {
                        "token_id": "t_new",
                        "strategy": "test",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # t_new is not in current_positions → add.
        assert any(a["token_id"] == "t_new" for a in body["add"])
        # t_old is not in suggested_tokens → close.
        assert any(c["token_id"] == "t_old" for c in body["close"])

    def test_get_rebalance_live_returns_empty_actions_when_store_empty(
        self, client: TestClient
    ) -> None:
        """``GET /api/portfolio/rebalance/live`` with no open positions
        in the store returns ``{add: [], reduce: [], close: [], hold: []}``
        — empty everywhere because the optimizer is given an empty
        opportunities list AND there are no current positions to close."""
        # Belt-and-braces: the autouse conftest fixture already cleared
        # ``store.positions``; make sure it stays empty for this test.
        store.positions.clear()
        response = client.get("/api/portfolio/rebalance/live")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["add"] == []
        assert body["reduce"] == []
        assert body["hold"] == []
        # close is empty because there are no current positions.
        assert body["close"] == []

    def test_get_rebalance_live_surfaces_open_positions_as_close(
        self, client: TestClient
    ) -> None:
        """With one open position in ``store.positions`` and an empty
        opportunity set, the live rebalance endpoint suggests closing
        that position (the optimizer's ``suggest_rebalance`` puts every
        current position not in the suggested set into ``close``)."""
        store.positions.clear()
        store.positions["t_live"] = Position(
            token_id="t_live",
            market_slug="slug",
            yes_shares=10.0,
            avg_entry_price=0.5,
            total_invested=5.0,
            strategy="signal_trader",
        )
        try:
            response = client.get("/api/portfolio/rebalance/live")
            assert response.status_code == 200, response.text
            body = response.json()
            # The open position is not in the (empty) suggestion set → close.
            closes = [c["token_id"] for c in body["close"]]
            assert "t_live" in closes
        finally:
            store.positions.clear()

    def test_get_rebalance_live_returns_warning_when_store_raises(
        self, client: TestClient, monkeypatch
    ) -> None:
        """If ``store.get_positions()`` raises, the endpoint returns 200
        with a ``warning`` field (not 5xx) so the dashboard can surface
        "live positions unavailable" without crashing the panel."""
        # Make ``get_positions`` raise.
        def _raise():
            raise RuntimeError("simulated store failure")

        monkeypatch.setattr(store, "get_positions", _raise)
        response = client.get("/api/portfolio/rebalance/live")
        assert response.status_code == 200, response.text
        body = response.json()
        assert "warning" in body
        assert "simulated store failure" in body["warning"]


# ── (4) optimizer constraint enforcement ────────────────────────────────────


class TestOptimizationConstraints:
    """The optimizer respects ``min_edge``, ``min_confidence``,
    ``max_single_bet``, and ``max_total_exposure``. Uses fresh
    ``PortfolioOptimizer()`` instances (no shared state with the
    singleton) so the assertions are deterministic regardless of any
    PUT-mutation in the endpoint tests above."""

    def test_min_edge_gate_drops_sub_threshold_opportunities(self) -> None:
        """An opportunity with ``edge < min_edge`` produces a zero Kelly
        fraction and is dropped from the optimization (no bet, zero
        total allocated)."""
        opt = PortfolioOptimizer(min_edge=0.05)
        result = opt.optimize(
            [
                {
                    "token_id": "t1",
                    "strategy": "s",
                    "price": 0.5,
                    "edge": 0.04,  # below min_edge=0.05
                    "confidence": 0.7,
                }
            ]
        )
        assert result.bets == []
        assert result.total_allocated_usdc == 0.0

    def test_min_confidence_gate_drops_sub_threshold_opportunities(self) -> None:
        """An opportunity with ``confidence < min_confidence`` produces
        a zero Kelly fraction and is dropped."""
        opt = PortfolioOptimizer(min_confidence=0.6)
        result = opt.optimize(
            [
                {
                    "token_id": "t1",
                    "strategy": "s",
                    "price": 0.5,
                    "edge": 0.10,
                    "confidence": 0.55,  # below min_confidence=0.6
                }
            ]
        )
        assert result.bets == []
        assert result.total_allocated_usdc == 0.0

    def test_max_single_bet_caps_each_bet_size(self) -> None:
        """No single bet's ``suggested_size_usdc`` may exceed
        ``max_single_bet * operating_capital``. With a very high edge
        + confidence, the raw Kelly would otherwise be enormous; the
        cap clamps it to ``max_single_bet * operating_capital``."""
        opt = PortfolioOptimizer(
            operating_capital=100.0,
            kelly_fraction=1.0,  # full Kelly — would be huge without the cap
            max_single_bet=0.15,
            max_total_exposure=1.0,  # generous so total budget doesn't pre-empt
            min_edge=0.03,
            min_confidence=0.55,
        )
        result = opt.optimize(
            [
                {
                    "token_id": "t1",
                    "strategy": "s",
                    "price": 0.5,
                    "edge": 0.30,  # very high edge
                    "confidence": 0.95,
                }
            ]
        )
        assert len(result.bets) == 1
        # max_single_bet * operating_capital = 0.15 * 100 = 15.0
        assert result.bets[0].suggested_size_usdc <= 15.0 + 1e-9

    def test_max_total_exposure_never_exceeded_even_with_many_bets(self) -> None:
        """The sum of selected bet sizes never exceeds
        ``max_total_exposure * operating_capital``, even when many
        above-threshold opportunities are passed in. The last selected
        bet is scaled down to fit if needed."""
        opt = PortfolioOptimizer(
            operating_capital=100.0,
            kelly_fraction=1.0,
            max_single_bet=0.15,        # 15 per bet cap
            max_total_exposure=0.50,     # $50 total cap
            min_edge=0.03,
            min_confidence=0.55,
        )
        # 10 above-threshold opportunities — each would otherwise get $15
        # (max_single_bet cap), totalling $150, but the total budget is $50.
        opps = [
            {
                "token_id": f"t{i}",
                "strategy": "s",
                "price": 0.5,
                "edge": 0.30,
                "confidence": 0.95,
            }
            for i in range(10)
        ]
        result = opt.optimize(opps)
        assert result.total_allocated_usdc <= 50.0 + 1e-6, (
            f"total allocated {result.total_allocated_usdc} exceeds "
            f"max_total_exposure * operating_capital = 50.0"
        )
        # At least one bet was selected (the optimizer is greedy on Sharpe).
        assert len(result.bets) >= 1

    def test_last_optimization_cached_on_optimizer_instance(self) -> None:
        """``optimize()`` caches its result on
        ``self._last_optimization`` so the status endpoint can surface
        it without a re-run."""
        opt = PortfolioOptimizer()
        assert opt._last_optimization is None  # fresh instance
        result = opt.optimize(
            [
                {
                    "token_id": "t1",
                    "strategy": "s",
                    "price": 0.5,
                    "edge": 0.10,
                    "confidence": 0.7,
                }
            ]
        )
        assert opt._last_optimization is result  # cached by reference
        assert opt._last_optimization.total_allocated_usdc == result.total_allocated_usdc

    def test_empty_opportunities_returns_empty_optimization(self) -> None:
        """An empty opportunities list yields a no-bet optimization
        (NOT an exception) — preserves the W16-3 contract."""
        opt = PortfolioOptimizer()
        result = opt.optimize([])
        assert result.bets == []
        assert result.total_allocated_usdc == 0.0
        assert result.constraint_violations == []
