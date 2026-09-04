"""
Project-local pytest configuration for the polymarket-bot test suite.

Anchors the test root (so ``from core.<module> import ...`` resolves
without sys.path gymnastics) and applies ``@pytest.mark.asyncio`` to every
``async def test_...`` function in the package via the module-level
``pytestmark`` declaration in each test module.

The repo's ``pytest.ini`` declares ``testpaths = tests`` — this file makes
that discovery work even though the project's ``pyproject.toml`` /
``pytest.ini`` are intentionally left untouched (the S9 task spec
constrains us to *new* files only).

T15 — Shared fixtures.
~~~~~~~~~~~~~~~~~~~~~~~

This file is imported by pytest BEFORE any ``tests/test_*.py`` sibling
module, which makes it the natural place to (a) redirect every
on-disk persisted-state path into a writable ``/tmp`` sandbox BEFORE the
first import of a project module that reads ``os.environ`` at
module-import time (``core.data_store``, ``core.decision_ledger``,
``core.audit_logger``, ``core.safety``, ``ml.model_registry`` …) and
(b) expose shared isolation fixtures that the sibling test modules can
opt into without re-defining them.

Five explicit fixtures + one autouse fixture:

  (1) ``isolated_store``               — fresh ``DataStore`` with
                                         ``load_from_disk`` neutralized.
  (2) ``isolated_risk_manager``        — fresh ``InstitutionalRiskEngine``.
  (3) ``isolated_decision_ledger``     — ``DecisionLedger`` with
                                         ``DB_PATH`` patched to ``tmp_path``.
  (4) ``isolated_paper_sim``           — fresh ``PaperSimulator``.
  (5) ``no_kill_switch``              — patches
                                         ``core.safety.kill_switch_file_exists``
                                         to ``False``.
  (autouse) ``_reset_store_factory_defaults``
                                      — resets the global ``store`` /
                                        ``risk_manager`` / ``paper_sim``
                                        singletons to a clean baseline
                                        before every test. Directly fixes
                                        the pre-existing flaky
                                        ``test_insufficient_balance_paper_zero``
                                        behaviour (the
                                        ``test_07_insufficient_balance_does_not_crash``
                                        test in
                                        ``tests/test_failure_injection.py``)
                                        where a prior test could leave
                                        ``store.paper_balance`` at a
                                        non-baseline value, perturbing the
                                        zero-balance assertion path.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads os.environ at module-import time. ────────────────
# ``setdefault`` lets an outer runner (CI / pytest invocation / a sibling test
# file imported later in the session) override these if it needs to; otherwise
# the tests run fully hermetic to ``/tmp`` and cannot clobber any real
# persisted state in the repo's ``data/`` directory. This also fixes the
# pre-existing env-var ``setdefault`` race between sibling test files (each
# one used to compete to set ``DECISION_LEDGER_DB_PATH`` first; now conftest
# sets it before any sibling is imported, so the global ``decision_ledger``
# singleton is constructed against a writable path every time).
_TMP_ROOT = Path(
    os.environ.get("PMBOT_TEST_TMP_ROOT", "/tmp/pmbot_conftest_isolation")
)
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    # W12-1 — feature flags SQLite store. Module-level singleton
    # ``core.feature_flags.flag_manager`` is constructed at import time
    # and would otherwise try to mkdir ``/app/data`` (read-only in the
    # sandbox) and crash the import.
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    # W14-5 — A/B testing SQLite store. Module-level singleton
    # ``ml.ab_testing.ab_test`` is constructed at import time and would
    # otherwise try to mkdir ``/app/data`` (read-only in the sandbox) and
    # crash the import — same defensive pattern as FLAGS_DB_PATH above.
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    # W16-2 — ML feature store SQLite db. Module-level singleton
    # ``ml.feature_store.feature_store`` is constructed at import time and
    # would otherwise try to mkdir ``/app/data`` (read-only in the
    # sandbox) and crash the import — same defensive pattern as
    # FLAGS_DB_PATH / AB_TEST_DB_PATH above.
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    # W17-5 — Immutable hash-chained audit trail. Module-level singleton
    # ``core.immutable_audit.immutable_audit`` is constructed at import
    # time and would otherwise try to mkdir ``/app/data`` (read-only in
    # the sandbox) and emit warnings on every import — same defensive
    # redirect pattern as FLAGS_DB_PATH / AB_TEST_DB_PATH above.
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    # W17-8 — async job queue SQLite db. Module-level singleton
    # ``core.job_queue.job_queue`` is constructed at import time and
    # would otherwise try to mkdir ``/app/data`` (read-only in the
    # sandbox) and crash the import — same defensive pattern as the
    # other *_DB env redirects above.
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    # W19-4 — ML economic value tracker SQLite db. Module-level singleton
    # ``ml.economic_value.ml_value_tracker`` is constructed at import
    # time and would otherwise try to mkdir ``/app/data`` (read-only in
    # the sandbox) and crash the import — same defensive pattern as the
    # other *_DB env redirects above.
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    # W20-3 — backtest experiment registry SQLite db. Module-level
    # singleton ``backtesting.experiment_store.experiment_store`` is
    # constructed at import time and would otherwise try to mkdir
    # ``/app/data`` (read-only in the sandbox) — same defensive pattern
    # as the other *_DB env redirects above.
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    # W21-1 — unified DatabaseManager SQLite paths. Module-level
    # singleton ``core.database_manager.db_manager`` is constructed at
    # import time and would otherwise try to mkdir ``/app/data``
    # (read-only in the sandbox) — same defensive pattern as the other
    # *_DB env redirects above. The DAO uses SEPARATE SQLite files from
    # the legacy ``core/market_db.py`` / ``core/decision_ledger.py``
    # modules so the DAO's market_snapshots table can carry the extra
    # bid_size / ask_size / bids_json / asks_json / bid_depth_10 /
    # ask_depth_10 columns and the decision_events table can carry
    # correlation_id / model_version columns (the legacy schemas lack
    # these — see ``core/database_manager.py`` docstring for the full
    # rationale).
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    # W21-4 — Directory holding every DAO-owned SQLite DB file the
    # ``DatabaseManager`` resolves via ``get_sqlite_path(name)``. The
    # manager's ``SQLITE_PATHS`` dict is populated eagerly at
    # ``__init__`` time so the DAO singletons (constructed at module-
    # import time) can resolve their target path BEFORE
    # ``initialize()`` runs. Without this redirect, the DAO would
    # default to ``/app/data`` (read-only in the sandbox).
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    # Force the canonical trading mode to paper + live disabled so risk-gate
    # tests don't short-circuit at the shadow / live-trading gates inside
    # ``InstitutionalRiskEngine.check_order``.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# ── W18-8 — Clear the conftest-redirected model registry BEFORE any project
# module that imports ``ml.model_registry`` is loaded. ──────────────────────
# Without this guard, test pollution from a prior pytest session accumulates
# in ``_TMP_ROOT / "model_registry.json"`` — every ``MarketMLModel.fit_initial``
# call inside a unit test registers a version (typically ``n=100, brier=0.1786,
# ece=0.2617`` — the shrunk-synthetic fixture signature), and the next test
# session boots the singleton against that polluted file. The polluted
# versions then appear in ``list_versions()`` / ``GET /api/ml/versions`` for
# the entire session, breaking tests that assert on the lineage shape.
#
# Deleting the file BEFORE the singleton is constructed forces ``ModelRegistry
# .__init__`` → ``_load_from_disk`` to seed the factory baseline
# (``v1.0.0, n=3000, brier=0.1838, ece=0.038``) — exactly one clean entry.
# This is the same path the registry takes on a fresh deployment, so the
# test session starts from a known-clean baseline every time.
#
# The unlink is best-effort: a missing file is fine (the registry will be
# seeded by ``_load_from_disk``); an unwritable file (rare in /tmp) is
# swallowed so the test session can still proceed (the singleton will load
# whatever's there).
_TMP_REGISTRY_FILE = _TMP_ROOT / "model_registry.json"
if _TMP_REGISTRY_FILE.exists():
    try:
        _TMP_REGISTRY_FILE.unlink()
    except OSError:
        # Defensive: a transient permission / lock issue must NOT block
        # the test session from starting. ``_load_from_disk`` will log a
        # warning and seed the baseline if the file is unreadable.
        pass

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``, ``ml.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_store import (  # noqa: E402
    BANKROLL_BASELINE,
    DataStore,
    store,
)
from core.decision_ledger import DecisionLedger  # noqa: E402
from core.safety import (  # noqa: E402
    ACTIVATION_REASON_FILE,
    KILL_SWITCH_PATH,
    clear_kill_switch,
)
from paper.simulator import PaperSimulator, paper_sim  # noqa: E402
from risk.manager import InstitutionalRiskEngine, risk_manager  # noqa: E402

# ── Disable rate limiter in tests (W10-4) ─────────────────────────────────────
# The shared ``limiter`` singleton in ``api/rate_limit.py`` is disabled here
# at conftest module-load time (before any test runs) so:
#   * existing tests that hit rate-limited routes via TestClient don't
#     suddenly start receiving 429s after the limit is hit (TestClient uses
#     the same source IP — ``127.0.0.1`` — for every request, so the
#     4th request to a 3/min-limited route would otherwise fail);
#   * the in-memory hit counter doesn't leak between tests;
#   * the ``slowapi`` decorator's wrapper is essentially a pass-through
#     when ``limiter.enabled = False`` (verified against slowapi 0.1.10).
# The dedicated ``tests/test_rate_limiting.py`` module builds its OWN
# ``Limiter`` instances for the limit-is-actually-enforced tests, so the
# global ``enabled = False`` flag doesn't affect them.
try:
    from api.rate_limit import limiter as _shared_limiter  # noqa: E402

    _shared_limiter.enabled = False
except ImportError:  # pragma: no cover — defensive: if api.rate_limit
    # ever becomes importable only inside the server package, the test
    # suite should still run (limiter just isn't installed in this env).
    pass


# ── Clear caches before every test (W11-2) ──────────────────────────────────
# The TTLCache singletons in ``core.cache`` (analytics_cache /
# attribution_cache / ml_metrics_cache / markets_cache /
# observability_cache / general_cache) persist across tests because they're
# module-level singletons. Without a clear, a test that hit
# ``GET /api/analytics`` (caching the result for 30s) would leak that cached
# dict into the next test — even after the autouse ``store`` reset zeroes
# ``store.trades`` / ``store.positions``. The next test's
# ``GET /api/analytics`` would return the PRIOR test's cached snapshot,
# breaking value-level assertions (and silently masking regressions
# because the 200 status + headline-key checks would still pass).
#
# The clear runs BEFORE the test (not after) so the post-test teardown is
# unnecessary — the pre-test clear of the NEXT test cleans up whatever the
# prior test cached, mirroring the ``_reset_store_factory_defaults`` pattern.
try:
    from core.cache import (  # noqa: E402
        analytics_cache,
        attribution_cache,
        general_cache,
        markets_cache,
        ml_metrics_cache,
        observability_cache,
    )

    def _clear_all_caches() -> None:
        """Drop every entry + reset hit/miss counters in every TTLCache singleton."""
        analytics_cache.clear()
        attribution_cache.clear()
        ml_metrics_cache.clear()
        markets_cache.clear()
        observability_cache.clear()
        general_cache.clear()
except ImportError:  # pragma: no cover — defensive: if core.cache is ever
    # renamed / removed, the test suite should still run (caching just
    # isn't active in this env).
    def _clear_all_caches() -> None:  # type: ignore[no-redef]
        pass


# ── Autouse: reset store singletons before every test ──────────────────────
@pytest.fixture(autouse=True)
def _reset_store_factory_defaults():
    """Reset the global ``store`` / ``risk_manager`` / ``paper_sim`` singletons
    to a clean factory baseline BEFORE every test (autouse).

    Why this exists
    ---------------
    The pipeline's risk engine, data store, and paper simulator are
    process-global singletons (``risk.manager.risk_manager``,
    ``core.data_store.store``, ``paper.simulator.paper_sim``). Without a
    reset, state from one test (an activated kill switch, a paused
    strategy, a zeroed ``paper_balance``, an altered ``peak_equity``,
    a stale ``paper_sim._virtual_balance_usdc`` …) would leak into the
    next and mask regressions.

    Direct fix for the pre-existing flaky
    ``test_insufficient_balance_paper_zero`` (a.k.a.
    ``test_07_insufficient_balance_does_not_crash`` in
    ``tests/test_failure_injection.py``): that test saves and restores
    ``store.paper_balance`` in a ``finally`` block, but if a PRIOR test
    left ``store.paper_balance`` at a non-baseline value (or left the
    durable kill-switch marker file in place), the saved "original" value
    would be wrong, the test's ``store.paper_balance = 0.0`` would still
    execute, but the surrounding state (kill switch, peak equity,
    positions, paper-sim virtual balance) could perturb the assertion
    path. Resetting everything to factory defaults before every test
    removes that race entirely.

    Idempotency
    -----------
    Safe to stack with the per-module autouse reset fixtures already
    present in ``tests/test_risk_manager.py`` (``reset_risk_and_store_state``)
    and ``tests/test_failure_injection.py`` (``_reset_global_state``):
    running the same reset twice is a harmless re-clear of already-empty
    containers / re-zero of already-zeroed scalars. The pre-test reset is
    the load-bearing half for the flaky-test fix; the post-test clear is
    skipped here (the per-module fixtures already handle teardown for
    their own tests, and the pre-test reset of the NEXT test cleans up
    anything the prior test left behind regardless).

    Restored baseline
    -----------------
      * kill switch off (in-memory flag AND the durable marker file removed)
      * PnL zeroed (daily + weekly), weekly window started "now"
      * peak equity at ``BANKROLL_BASELINE`` ($100.00)
      * paper_balance at ``BANKROLL_BASELINE``
      * positions / open_orders / trades / market_slugs / order_books /
        events all cleared; equity_history reset to the single initial point
      * observation-only mode off
      * per-strategy cooldowns cleared
      * ``paper_sim._virtual_balance_usdc`` reset to ``BANKROLL_BASELINE``
    """
    _clear_durable_kill_switch()
    _reset_store_state()
    _reset_risk_engine_state()
    _reset_paper_simulator_state()

    yield  # ── test runs ──

    # No post-test teardown: the pre-test reset above is what fixes the
    # flaky ``test_insufficient_balance_paper_zero`` race, and the
    # per-module autouse fixtures in sibling test files already run their
    # own teardown for their own tests.


def _clear_durable_kill_switch() -> None:
    """Remove the durable kill-switch marker file (and its reason sidecar).

    Belt-and-braces: ``clear_kill_switch()`` from ``core.safety`` is the
    canonical helper, but it can raise ``OSError`` in CI sandboxes with a
    read-only ``/tmp``; in that case we fall back to direct
    ``Path.unlink(missing_ok=True)`` and swallow the error.
    """
    try:
        clear_kill_switch()
    except OSError:
        for p in (KILL_SWITCH_PATH, ACTIVATION_REASON_FILE):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _reset_store_state() -> None:
    """Restore the global ``store`` to a freshly-bootstrapped baseline."""
    store.kill_switch_active = False
    store.daily_pnl = 0.0
    store.weekly_pnl = 0.0
    store.week_window_started_at = time.time()
    store.paper_balance = BANKROLL_BASELINE
    store.peak_equity = BANKROLL_BASELINE
    store.session_start = time.time()
    store.open_orders.clear()
    store.order_history.clear()
    store.positions.clear()
    store.trades.clear()
    store.market_slugs.clear()
    store.order_books.clear()
    store.event_log.clear()
    store.equity_history = [
        {"timestamp": time.time(), "equity": BANKROLL_BASELINE, "pnl": 0.0}
    ]


def _reset_risk_engine_state() -> None:
    """Restore the global ``risk_manager`` to its post-ctor state."""
    risk_manager.observation_only = False
    risk_manager.observation_reason = ""
    risk_manager._strategy_cooldowns.clear()


def _reset_paper_simulator_state() -> None:
    """Restore ``paper_sim``'s virtual balance to the baseline.

    ``PaperSimulator.__init__`` snapshots ``store.paper_balance`` at
    construction time and then caches the result in
    ``_virtual_balance_usdc``; subsequent fills re-sync the cache via
    ``self._virtual_balance_usdc = store.paper_balance``. A prior test
    that filled an order would have left the cache at the post-fill
    balance (not $100); without this reset, the next test would see a
    stale virtual balance even after ``_reset_store_state`` zeroed
    ``store.paper_balance`` back to the baseline.
    """
    paper_sim._virtual_balance_usdc = BANKROLL_BASELINE


# ── (1) isolated_store ──────────────────────────────────────────────────────
@pytest.fixture
def isolated_store(monkeypatch):
    """Fresh ``DataStore`` with ``load_from_disk`` neutralized.

    Returns a brand-new ``DataStore`` instance whose in-memory containers
    are empty and whose ``paper_balance`` / ``peak_equity`` /
    ``equity_history`` are at the post-ctor factory defaults
    (``BANKROLL_BASELINE`` = $100.00).

    ``DataStore.load_from_disk`` is monkeypatched to a no-op for the
    duration of the test so neither this instance NOR the global
    singleton (already constructed at module-import time) can read
    persisted on-disk state mid-test. The global ``store`` singleton is
    NOT replaced — production code paths that import ``store`` directly
    (e.g. ``paper/simulator.py``, ``risk/manager.py``,
    ``strategies/base.py``) still see the singleton; for an end-to-end
    hermetic reset of that singleton, rely on the autouse
    ``_reset_store_factory_defaults`` fixture instead.

    Use this fixture when a test needs a hermetic ``DataStore`` for pure
    unit-testing of ``DataStore`` methods (e.g. ``record_fill``,
    ``total_exposure``, ``roll_weekly_window``) without perturbing the
    global singleton that the rest of the pipeline references.
    """
    # Neutralize load_from_disk so even if the test (or a downstream
    # caller) explicitly invokes ``fresh_store.load_from_disk()`` no
    # on-disk state is read. The lambda accepts the ``self`` positional
    # argument so the bound-method replacement signature still matches.
    monkeypatch.setattr(DataStore, "load_from_disk", lambda self: None)
    return DataStore()


# ── (2) isolated_risk_manager ───────────────────────────────────────────────
@pytest.fixture
def isolated_risk_manager():
    """Fresh ``InstitutionalRiskEngine`` (no shared state).

    Returns a brand-new ``InstitutionalRiskEngine`` instance with empty
    per-strategy cooldowns (``_strategy_cooldowns``) and
    observation-only mode off — i.e. exactly the state the global
    ``risk_manager`` singleton is in immediately after its constructor
    runs, before any order / pnl / kill-switch method has been called.

    The global ``risk_manager`` singleton is NOT replaced — production
    code paths that import ``risk_manager`` directly (e.g.
    ``paper/simulator.py::_execute_fill`` calls
    ``risk_manager.report_trade_pnl``) still see the singleton. Use this
    fixture for unit-testing the risk engine in isolation; for
    end-to-end tests that exercise the singleton, rely on the autouse
    ``_reset_store_factory_defaults`` fixture to bring the singleton
    back to a clean state between tests.
    """
    return InstitutionalRiskEngine()


# ── (3) isolated_decision_ledger ─────────────────────────────────────────────
@pytest.fixture
def isolated_decision_ledger(monkeypatch, tmp_path):
    """``DecisionLedger`` whose SQLite file lives under ``tmp_path``.

    ``core.decision_ledger.DB_PATH`` is monkeypatched so the no-arg
    ``DecisionLedger()`` constructor picks up the test path — the same
    global-lookup code path production uses (``DECISION_LEDGER_DB_PATH``
    env var override → ``DB_PATH`` module global → ``__init__``). The
    module-level singleton ``decision_ledger`` (constructed at import
    time) is left untouched; this fixture returns a fresh instance
    scoped to the test's own ``tmp_path``.

    Mirrors the ``ledger`` fixture already inlined in
    ``tests/test_decision_ledger.py`` — promoted here so any sibling
    test module can opt into the same isolation without redefining it.
    """
    db_path = tmp_path / "isolated_decision_ledger.db"
    monkeypatch.setattr("core.decision_ledger.DB_PATH", db_path)
    return DecisionLedger()


# ── (4) isolated_paper_sim ──────────────────────────────────────────────────
@pytest.fixture
def isolated_paper_sim():
    """Fresh ``PaperSimulator`` (no shared state).

    Returns a brand-new ``PaperSimulator`` instance. The new sim's
    ``_virtual_balance_usdc`` is initialized from the global ``store``'s
    ``paper_balance`` at construction time; because the autouse
    ``_reset_store_factory_defaults`` fixture runs FIRST and resets
    ``store.paper_balance`` to ``BANKROLL_BASELINE`` ($100), the new sim
    starts with a clean $100 virtual balance.

    The global ``paper_sim`` singleton is NOT replaced — production code
    paths that import ``paper_sim`` directly (e.g.
    ``strategies/base.submit_order``) still see the singleton. Use this
    fixture for unit-testing ``PaperSimulator`` methods (e.g.
    ``_can_fill``, ``_apply_slippage``, ``create_order``) in isolation;
    for end-to-end tests that exercise the singleton, rely on the
    autouse ``_reset_store_factory_defaults`` fixture instead.
    """
    return PaperSimulator()


# ── (5) no_kill_switch ──────────────────────────────────────────────────────
@pytest.fixture
def no_kill_switch(monkeypatch):
    """Patch ``core.safety.kill_switch_file_exists`` to return ``False``.

    The risk gate ``InstitutionalRiskEngine.check_order`` consults the
    durable kill-switch marker file via ``kill_switch_file_exists()`` at
    every order path (alongside the in-memory ``store.kill_switch_active``
    flag). This fixture neutralizes the file-based check for the duration
    of the test so the order path can be exercised end-to-end without a
    leftover marker file (from a prior test that triggered the breaker)
    short-circuiting the gate with "Kill switch is active — all trading
    halted".

    Belt-and-braces with the autouse ``_reset_store_factory_defaults``
    fixture (which removes the marker file before every test): this
    fixture additionally guards against the file being re-created
    mid-test (e.g. by a daily-loss-stop trigger inside the test itself),
    which is useful for tests that want to verify a downstream risk gate
    WITHOUT the kill switch firing first.
    """
    monkeypatch.setattr("core.safety.kill_switch_file_exists", lambda: False)
    return None
