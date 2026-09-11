"""
tests/test_training_orchestrator.py — Unit tests for
``ml/training_orchestrator.py``.

X4 — Continuous drift-triggered re-training orchestrator unit tests.

Covers the five behaviours required by the X4 task spec:

  (1) ``start()`` schedules the retraining task (an ``asyncio.Task``
      wrapping ``_orchestrator_loop`` is created and assigned to
      ``self._task``; ``_running`` flips to ``True``).
  (2) ``stop()`` cancels the scheduled task (``self._task.cancel()``
      is invoked and the task transitions to the ``CANCELLED`` state;
      ``_running`` flips back to ``False``).
  (3) ``stats`` returns a dict with the expected orchestration keys
      (``retrain_count`` / ``last_champion_brier`` /
      ``seconds_since_retrain`` / ``drift_threshold_psi`` /
      ``brier_drift_threshold`` / ``min_improvement_ratio`` /
      ``schedule_hours``).
  (4) ``evaluate_and_retrain_if_needed()`` returns a ``bool`` (the
      method's documented return contract — ``False`` when no trigger
      fires, ``True`` when a trigger fires and the challenger is
      promoted).
  (5) A drift-triggered retrain fires when ``PSI > threshold``
      (``drift_detector.last_psi >= DRIFT_RETRAIN_THRESHOLD`` (0.10)
      AND the challenger beats the champion by the
      ``MIN_IMPROVEMENT_RATIO`` margin — promotion path:
      ``_retrain_count`` increments, ``drift_detector.reset()`` is
      called, ``model_registry.register_version`` is invoked, and the
      method returns ``True``).

Test isolation strategy
-----------------------
* The orchestrator module imports four symbols at module-load time:

      from ml.drift_detector import BRIER_DRIFT_THRESHOLD, drift_detector
      from ml.model import MarketMLModel, ml_model
      from ml.model_registry import model_registry

  ``drift_detector`` / ``ml_model`` / ``model_registry`` are
  **singletons** constructed at import time (the drift detector is a
  pure-Python object with no I/O; ``ml_model``'s ctor is a bare
  ``MarketMLModel()`` that doesn't train; ``model_registry``'s ctor
  loads a JSON file but ``conftest.py``'s env-var redirect already
  points ``MODEL_REGISTRY_PATH`` at a writable ``/tmp`` sandbox so the
  import succeeds). The orchestrator's ``evaluate_and_retrain_if_needed``
  reads from these singletons directly (``drift_detector.last_psi``,
  ``ml_model.brier_score``, ``model_registry.register_version(...)``),
  so the tests patch the **module-attribute references** inside
  ``ml.training_orchestrator`` (string-targeted
  ``unittest.mock.patch("ml.training_orchestrator.drift_detector", fake)``
  form) to swap in fakes for the duration of each test. The patches
  are scoped to the ``with`` block so they revert automatically —
  no module-global pollution leaks across tests.

* ``MarketMLModel`` is the **class** used to construct the challenger
  inside ``_train_challenger`` (called via ``asyncio.to_thread``). A
  real ``MarketMLModel().fit_initial(...)`` would train a 4-member
  ensemble on 3,000 synthetic samples (~25 s wall-time — unacceptable
  for a unit test). The tests substitute a fake class whose
  ``__init__`` pre-populates ``brier_score`` / ``roc_auc`` / ``ece``
  / ``n_real_samples`` / ``n_synthetic_samples`` and whose
  ``fit_initial(**hp)`` / ``save()`` are no-ops recording the call.
  This isolates the orchestrator's trigger + promotion logic from the
  (slow, non-deterministic) model training path.

* The orchestrator's ``_orchestrator_loop`` does
  ``await asyncio.sleep(60)`` (initial warm-up) before its first
  ``evaluate_and_retrain_if_needed()`` call. Tests (1) and (2) call
  ``start()`` then immediately assert on ``_task`` and call ``stop()``
  — the 60 s sleep guarantees the loop never reaches the evaluation
  branch during the test, so no drift_detector / ml_model mock is
  needed for the loop's own execution (only ``start()``'s
  ``ml_model.brier_score`` read needs the fake).

* Test (3) (``stats``) is a synchronous ``def`` — no event loop, no
  async work. The orchestrator is instantiated directly and the
  ``stats`` property is read; no singleton state is touched.

* Tests (4) and (5) call ``evaluate_and_retrain_if_needed()``
  directly (NOT via ``start()``), so no background task is created
  and no teardown is needed — the function is a one-shot coroutine
  that returns ``True`` / ``False``.

* ``conftest.py`` (auto-loaded by pytest BEFORE this module) redirects
  every persisted-state path (``MODEL_PATH``,
  ``MODEL_REGISTRY_PATH``, ``MARKET_DB_PATH``, …) into a writable
  ``/tmp/pmbot_conftest_isolation`` sandbox and inserts the project
  root into ``sys.path``. The inline ``sys.path`` bootstrap below is
  a belt-and-braces measure for direct
  ``pytest tests/test_training_orchestrator.py`` runs from a
  non-project cwd.

Async mode
~~~~~~~~~~
pytest-asyncio 1.3.0 is installed. The repo's ``pytest.ini`` declares
``testpaths = tests`` but does NOT set ``asyncio_mode`` (the X4 task
forbids editing existing files), so the project default
(``asyncio_mode=strict``) applies. Every ``async def test_*`` is
decorated via the module-level ``pytestmark = pytest.mark.asyncio``
idiom — same convention used by every sibling async test module in
the repo (``test_observability.py``, ``test_decision_ledger.py``,
``test_risk_manager.py``, …). The single sync test (test 3) does not
need the marker and is correctly ignored by pytest-asyncio.

The X4 task spec forbids editing existing files; this module is
strictly additive. All accesses to ``orch._task`` /
``orch._running`` / ``orch._retrain_count`` / ``orch._last_retrain_time``
are private-member touches that the repo's ``pyproject.toml`` permits
for ``tests/*`` (``[tool.ruff.lint.per-file-ignores] "tests/*" =
["SLF001"]``).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Inline sys.path bootstrap — mirrors the pattern in test_features.py /
# test_drift_detector.py / tests/conftest.py so
# ``from ml.training_orchestrator import ...`` resolves regardless of the
# cwd pytest was launched from (monorepo root, CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)

from ml.training_orchestrator import (  # noqa: E402
    BRIER_DRIFT_THRESHOLD,
    DRIFT_RETRAIN_THRESHOLD,
    MIN_IMPROVEMENT_RATIO,
    ContinuousTrainingOrchestrator,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` cannot be edited per the X4 task
# constraint ("Do NOT edit existing files"), so we use the module-level
# ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``. Same
# convention as every sibling async test module.
pytestmark = pytest.mark.asyncio


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeDriftDetector:
    """Minimal fake of ``ml.drift_detector.ModelDriftDetector``.

    Exposes ONLY the subset of the public surface accessed by
    ``ContinuousTrainingOrchestrator.evaluate_and_retrain_if_needed``:
      * ``last_psi: float``           — PSI trigger signal
      * ``rolling_brier: float|None`` — Brier trigger signal
      * ``recent_actuals: list``       — used to compute ``n_outcomes``
                                         (the Brier trigger's ≥20 guard)
      * ``reset()``                    — called on successful promotion

    All four attributes are caller-configurable via the constructor so
    each test can drive a specific trigger branch (PSI / Brier /
    schedule) deterministically.
    """

    def __init__(
        self,
        last_psi: float = 0.0,
        rolling_brier: float | None = None,
        recent_actuals: list | None = None,
    ) -> None:
        self.last_psi = last_psi
        self.rolling_brier = rolling_brier
        self.recent_actuals = list(recent_actuals) if recent_actuals else []
        self.reset_calls = 0

    def reset(self) -> None:
        """Mirror the real ``ModelDriftDetector.reset`` contract — clears
        rolling state on promotion. The test only tracks the call count
        so we can assert ``drift_detector.reset()`` was invoked on the
        promotion path."""
        self.reset_calls += 1


def _make_fake_ml_model(brier_score: float = 0.20) -> SimpleNamespace:
    """Build a fake ``ml_model`` champion singleton.

    ``evaluate_and_retrain_if_needed`` reads ``ml_model.brier_score`` as
    the champion's current Brier (the gate for
    ``challenger_brier < current_brier * MIN_IMPROVEMENT_RATIO``) and
    accesses the SGD state + Brier rolling windows for the
    promotion-path transplant (``copy.deepcopy(ml_model.sgd)``,
    ``copy.copy(ml_model._rf_brier_window)``, …). The fake exposes all
    of them as no-op / empty values so the promotion code path doesn't
    raise on attribute lookup.

    ``ml_model.__dict__.update(challenger.__dict__)`` (the atomic
    hot-swap) requires the fake to have a real ``__dict__`` —
    ``SimpleNamespace`` satisfies that contract (unlike a bare
    ``MagicMock`` whose ``__dict__`` is the mock's own attribute store
    and would coerce the update target in surprising ways).
    """
    return SimpleNamespace(
        brier_score=brier_score,
        sgd=None,
        _sgd_trained=False,
        _n_updates=0,
        _rf_brier_window=[],
        _gb_brier_window=[],
        _sgd_brier_window=[],
        _lgbm_brier_window=[],
    )


def _make_fake_market_model_class(
    challenger_brier: float = 0.05,
) -> tuple[type, list]:
    """Build a fake ``MarketMLModel`` class whose instances are
    pre-configured with the supplied ``challenger_brier``.

    The orchestrator's ``_train_challenger`` does
    ``challenger = MarketMLModel(); challenger.fit_initial(**hp)``.
    Substituting this fake class for the real ``MarketMLModel`` lets the
    orchestrator exercise its full trigger + champion/challenger
    comparison + promotion path WITHOUT running the slow
    (multi-second, non-deterministic) sklearn ensemble fit.

    Returns a 2-tuple ``(fake_class, instances_list)`` where
    ``instances_list`` is a closure-scoped list that every constructed
    instance appends itself to — the test reads it to assert exactly
    one challenger was built and that ``save()`` was invoked on it.

    The closure-list-return pattern (rather than a class-level
    ``instances`` attribute) sidesteps the well-known Python
    class-body scoping gotcha: a class body cannot reference an
    enclosing function's local via ``instances = instances`` because
    the LHS assignment makes ``instances`` a class-body local, so the
    RHS read is treated as a not-yet-defined local (``NameError``).
    Exposing the list via the factory's return value keeps the
    book-keeping in the enclosing function scope where the append
    inside ``__init__`` resolves cleanly via the normal closure
    lookup.
    """
    instances: list = []

    class _FakeMarketMLModel:
        """Drop-in replacement for ``MarketMLModel`` used as the
        challenger factory inside ``_train_challenger``."""

        def __init__(self) -> None:
            self.brier_score = challenger_brier
            self.roc_auc = 0.95
            self.ece = 0.01
            self.n_real_samples = 100
            self.n_synthetic_samples = 2000
            # SGD + Brier-window attributes accessed on the promotion
            # transplant path (``copy.deepcopy(ml_model.sgd)`` etc.) —
            # the fake makes them simple picklable values so ``copy``
            # / ``deepcopy`` succeed without raising.
            self.sgd = None
            self._sgd_trained = False
            self._n_updates = 0
            self._rf_brier_window: list[float] = []
            self._gb_brier_window: list[float] = []
            self._sgd_brier_window: list[float] = []
            self._lgbm_brier_window: list[float] = []
            # Call-tracking records (consumed by the test assertions).
            self.fit_initial_calls: list[dict] = []
            self.save_calls = 0
            instances.append(self)

        def fit_initial(self, **kwargs) -> None:
            """No-op challenger training. Records the hyperparameters
            the orchestrator sampled so the test can assert the
            hyperparameter search space was exercised."""
            self.fit_initial_calls.append(dict(kwargs))

        def save(self) -> None:
            """No-op model persistence. Increments the call counter so
            the test can assert ``challenger.save()`` was invoked on
            the promotion path."""
            self.save_calls += 1

    return _FakeMarketMLModel, instances


class _FakeModelRegistry:
    """Minimal fake of ``ml.model_registry.ModelRegistry``.

    Exposes only ``register_version(**kwargs)`` — the method the
    orchestrator calls on the promotion path. Records every call so
    the test can assert the canonical ``"vN.champion"`` version tag
    and the promoted challenger's benchmark metrics were registered.
    """

    def __init__(self) -> None:
        self.register_calls: list[dict] = []

    def register_version(self, **kwargs) -> bool:
        """Mirror the real ``ModelRegistry.register_version`` signature
        (returns ``bool``). The real method enforces a Brier ≤ 0.22 and
        ROC-AUC ≥ 0.70 safety gate; the fake skips that gate so the
        test can drive the promotion path with a hand-picked
        challenger_brier without depending on the gate's outcome."""
        self.register_calls.append(dict(kwargs))
        return True


# ── (1) start() schedules the retraining task ───────────────────────────────


async def test_start_schedules_retraining_task():
    """``start()`` must schedule the ``_orchestrator_loop`` coroutine
    as an ``asyncio.Task`` and assign it to ``self._task``.

    The orchestrator's ``start()`` body::

        self._running = True
        self._last_champion_brier = ml_model.brier_score
        self._task = asyncio.create_task(
            self._orchestrator_loop(),
            name="ml-training-orchestrator",
        )

    So after ``await start()`` returns:
      * ``self._running`` is ``True``
      * ``self._task`` is a non-None ``asyncio.Task``
      * the task is named ``"ml-training-orchestrator"`` (the canonical
        name used by ``asyncio.all_tasks()`` filters in observability)
      * the task is NOT yet done — the loop's first action is
        ``await asyncio.sleep(60)`` (initial warm-up), so the task is
        parked on the sleep at the moment ``start()`` returns.

    ``ml_model.brier_score`` is read inside ``start()`` (to seed
    ``_last_champion_brier``), so the test patches
    ``ml.training_orchestrator.ml_model`` with a fake exposing a finite
    ``brier_score``. No other singleton needs mocking — the loop's
    60-second warm-up guarantees it never reaches
    ``evaluate_and_retrain_if_needed()`` during the test.

    Teardown: the test calls ``await stop()`` and then ``await _task``
    (catching ``CancelledError``) so the cancelled task is fully
    reaped — avoids pytest-asyncio's "Task was destroyed but it is
    pending" warning.
    """
    fake_ml_model = _make_fake_ml_model(brier_score=0.20)

    with patch("ml.training_orchestrator.ml_model", fake_ml_model):
        orch = ContinuousTrainingOrchestrator()

        # Pre-condition: fresh orchestrator has no task and is not running.
        assert orch._task is None, (
            "precondition: fresh orchestrator must have _task=None"
        )
        assert orch._running is False, (
            "precondition: fresh orchestrator must have _running=False"
        )

        await orch.start()

        try:
            # Post-condition (1a): _task is populated.
            assert orch._task is not None, (
                "start() must populate self._task with the scheduled "
                "_orchestrator_loop task"
            )
            # Post-condition (1b): _task is a real asyncio.Task (not a
            # coroutine, not a Future, not a Mock).
            assert isinstance(orch._task, asyncio.Task), (
                f"_task must be an asyncio.Task, got "
                f"{type(orch._task).__name__}"
            )
            # Post-condition (1c): _running flag flipped to True.
            assert orch._running is True, (
                "start() must set self._running=True"
            )
            # Post-condition (1d): task is named "ml-training-orchestrator"
            # — the canonical name the orchestrator explicitly passes to
            # asyncio.create_task (used by observability filters /
            # asyncio.all_tasks lookups).
            assert orch._task.get_name() == "ml-training-orchestrator", (
                f"task must be named 'ml-training-orchestrator', got "
                f"{orch._task.get_name()!r}"
            )
            # Post-condition (1e): task is still pending — the loop's
            # 60s initial warm-up sleep guarantees the task body hasn't
            # run yet at the moment start() returns.
            assert not orch._task.done(), (
                "freshly-scheduled _orchestrator_loop task must not be "
                "done (60s initial warm-up sleep parks it immediately)"
            )
            # Post-condition (1f): _last_champion_brier was seeded from
            # ml_model.brier_score inside start().
            assert orch._last_champion_brier == 0.20, (
                "start() must seed _last_champion_brier from "
                "ml_model.brier_score"
            )
        finally:
            # Cleanup: stop() cancels the task; await it so the
            # cancelled coroutine is fully reaped (avoids the
            # "Task was destroyed but it is pending" pytest-asyncio
            # warning).
            await orch.stop()
            if orch._task is not None:
                try:
                    await orch._task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Best-effort cleanup — never let teardown noise
                    # mask the real test result.
                    pass


# ── (2) stop() cancels the task ─────────────────────────────────────────────


async def test_stop_cancels_task():
    """``stop()`` must cancel the scheduled retraining task.

    The orchestrator's ``stop()`` body::

        self._running = False
        if self._task:
            self._task.cancel()

    So after ``await stop()``:
      * ``self._running`` is ``False``
      * ``self._task`` is still the same task object (stop does NOT
        clear the reference — it only calls ``.cancel()``)
      * the task transitions to the ``CANCELLED`` state once the
        cancellation propagates (the loop's
        ``await asyncio.sleep(60)`` raises ``CancelledError``, which
        propagates up uncaught — the loop's ``try/except`` only
        wraps the ``evaluate_and_retrain_if_needed()`` call, not the
        sleep).

    Verified by capturing the task reference BEFORE ``stop()``,
    awaiting it AFTER ``stop()`` (so the cancellation propagates and
    the task reaches a terminal state), then asserting:
      (a) ``_running`` is ``False``
      (b) the captured task is ``done()``
      (c) the captured task is ``cancelled()`` (the canonical
          terminal state for a ``CancelledError``-raised coroutine)

    Belt-and-braces: no ``"ml-training-orchestrator"`` task lingers
    on the event loop after stop (verified via ``asyncio.all_tasks()``
    — excludes the current coroutine).
    """
    fake_ml_model = _make_fake_ml_model(brier_score=0.20)

    with patch("ml.training_orchestrator.ml_model", fake_ml_model):
        orch = ContinuousTrainingOrchestrator()
        await orch.start()
        task = orch._task
        # Pre-condition: task was scheduled and is still pending.
        assert task is not None, (
            "precondition: start() must populate _task before stop() "
            "is called"
        )
        assert not task.cancelled(), (
            "precondition: freshly-started task must not be cancelled"
        )

        await orch.stop()

        # Post-condition (a): _running flag flipped back to False.
        assert orch._running is False, (
            "stop() must set self._running=False"
        )

        # The task has been cancelled but the cancellation hasn't
        # propagated yet (stop() calls .cancel() but doesn't await).
        # Await the task so the CancelledError propagates and the task
        # reaches its terminal CANCELLED state.
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Best-effort cleanup — never let teardown noise mask the
            # real test result.
            pass

        # Post-condition (b): the cancelled task is done.
        assert task.done(), (
            "stopped task must be done (cancel propagated + awaited)"
        )
        # Post-condition (c): the task ended in the CANCELLED state.
        # _orchestrator_loop's first action is `await asyncio.sleep(60)`,
        # which raises CancelledError when .cancel() is called; the
        # loop's try/except only wraps evaluate_and_retrain_if_needed
        # (not the sleep), so CancelledError propagates uncaught and
        # the task ends in CANCELLED.
        assert task.cancelled(), (
            "stopped task must be in CANCELLED state — _orchestrator_loop "
            "doesn't swallow the CancelledError raised by its initial "
            "asyncio.sleep(60) warm-up"
        )

        # Belt-and-braces: no "ml-training-orchestrator" task lingers
        # on the loop after stop. asyncio.all_tasks() excludes the
        # current coroutine so the running test itself isn't counted.
        lingering = [
            t for t in asyncio.all_tasks()
            if t.get_name() == "ml-training-orchestrator"
        ]
        assert lingering == [], (
            f"no 'ml-training-orchestrator' task should remain after "
            f"stop(); found {len(lingering)}"
        )


# ── (3) stats returns dict with expected keys ───────────────────────────────


def test_stats_returns_dict_with_expected_keys():
    """``stats`` must return a dict carrying the seven canonical
    orchestration keys, with values reflecting the fresh-orchestrator
    baseline.

    The implementation's ``stats`` property returns::

        {
            "retrain_count": self._retrain_count,
            "last_champion_brier": self._last_champion_brier,
            "seconds_since_retrain": round(time.time() - self._last_retrain_time),
            "drift_threshold_psi": DRIFT_RETRAIN_THRESHOLD,   # 0.10
            "brier_drift_threshold": BRIER_DRIFT_THRESHOLD,    # 0.22
            "min_improvement_ratio": MIN_IMPROVEMENT_RATIO,   # 0.98
            "schedule_hours": 6,
        }

    This test is synchronous (no event loop, no async work) — the
    orchestrator is instantiated directly and the ``stats`` property
    is read. No singleton state is touched.

    Belt-and-braces: the three threshold constants
    (``drift_threshold_psi`` / ``brier_drift_threshold`` /
    ``min_improvement_ratio``) are imported into the orchestrator
    module from ``ml.drift_detector`` / the orchestrator's own module
    globals — pinning them to the imported values (rather than
    hard-coded literals) keeps the test in lock-step with the
    implementation if the thresholds are ever re-tuned.
    """
    orch = ContinuousTrainingOrchestrator()
    stats = orch.stats

    # Return type is dict.
    assert isinstance(stats, dict), (
        f"stats must return a dict, got {type(stats).__name__}"
    )

    # The seven canonical keys required by the X4 task spec.
    expected_keys = {
        "retrain_count",
        "last_champion_brier",
        "seconds_since_retrain",
        "drift_threshold_psi",
        "brier_drift_threshold",
        "min_improvement_ratio",
        "schedule_hours",
    }
    assert set(stats.keys()) == expected_keys, (
        f"stats keys must be exactly {expected_keys!r}, got "
        f"{set(stats.keys())!r}"
    )

    # Fresh-orchestrator baseline values.
    # retrain_count starts at 0 (set in __init__).
    assert stats["retrain_count"] == 0, (
        f"fresh orchestrator retrain_count must be 0, got "
        f"{stats['retrain_count']!r}"
    )
    # last_champion_brier starts at 1.0 (the __init__ default — a
    # pessimistic ceiling so the first challenger always passes the
    # MIN_IMPROVEMENT_RATIO gate on its first promotion).
    assert stats["last_champion_brier"] == 1.0, (
        f"fresh orchestrator last_champion_brier must be 1.0 (the "
        f"__init__ default), got {stats['last_champion_brier']!r}"
    )

    # seconds_since_retrain is a non-negative int (rounded from the
    # time delta since __init__ set _last_retrain_time = time.time()).
    assert isinstance(stats["seconds_since_retrain"], int), (
        f"seconds_since_retrain must be an int (rounded), got "
        f"{type(stats['seconds_since_retrain']).__name__}"
    )
    assert stats["seconds_since_retrain"] >= 0, (
        f"seconds_since_retrain must be non-negative on a fresh "
        f"orchestrator, got {stats['seconds_since_retrain']!r}"
    )

    # The three threshold constants pinned to the module-level imports
    # (DRIFT_RETRAIN_THRESHOLD = 0.10, BRIER_DRIFT_THRESHOLD = 0.22,
    # MIN_IMPROVEMENT_RATIO = 0.98 — see ml/training_orchestrator.py).
    assert stats["drift_threshold_psi"] == DRIFT_RETRAIN_THRESHOLD, (
        f"drift_threshold_psi must equal DRIFT_RETRAIN_THRESHOLD "
        f"({DRIFT_RETRAIN_THRESHOLD}), got "
        f"{stats['drift_threshold_psi']!r}"
    )
    assert stats["brier_drift_threshold"] == BRIER_DRIFT_THRESHOLD, (
        f"brier_drift_threshold must equal BRIER_DRIFT_THRESHOLD "
        f"({BRIER_DRIFT_THRESHOLD}), got "
        f"{stats['brier_drift_threshold']!r}"
    )
    assert stats["min_improvement_ratio"] == MIN_IMPROVEMENT_RATIO, (
        f"min_improvement_ratio must equal MIN_IMPROVEMENT_RATIO "
        f"({MIN_IMPROVEMENT_RATIO}), got "
        f"{stats['min_improvement_ratio']!r}"
    )
    # schedule_hours is the documented 6-hour routine refresh interval.
    assert stats["schedule_hours"] == 6, (
        f"schedule_hours must be 6 (6-hour routine refresh), got "
        f"{stats['schedule_hours']!r}"
    )


# ── (4) evaluate_and_retrain_if_needed returns bool ─────────────────────────


async def test_evaluate_and_retrain_if_needed_returns_bool():
    """``evaluate_and_retrain_if_needed()`` must return a ``bool``
    (the method's documented return contract — ``False`` when no
    trigger fires, ``True`` when a trigger fires and the challenger
    is promoted).

    The X4 task spec asks for the return-type contract specifically
    (``returns bool``), so this test drives the no-trigger path (the
    cleanest way to verify the ``False`` return is a genuine ``bool``
    and not a truthy value of another type). The True-return contract
    is covered by test (5) below — both are ``bool`` by construction
    because the implementation's two return statements are
    ``return False`` and ``return True`` (no implicit coercion).

    No-trigger setup:
      * ``drift_detector.last_psi = 0.0``  → PSI trigger gate
        (``psi >= DRIFT_RETRAIN_THRESHOLD``) is False.
      * ``drift_detector.rolling_brier = None`` → Brier trigger gate
        (``rolling_brier > BRIER_DRIFT_THRESHOLD``) short-circuits on
        the ``None`` branch (``None or 0.0 = 0.0`` →
        ``0.0 > 0.22`` is False).
      * ``drift_detector.recent_actuals = []`` → ``n_outcomes = 0``
        (the Brier trigger's ``n_outcomes >= 20`` guard fails regardless).
      * ``time_since_retrain`` ≈ 0 (orchestrator just constructed →
        ``_last_retrain_time = time.time()`` → delta ≈ 0 →
        ``schedule_trigger = delta >= 21600`` is False).

    With all three triggers False, ``should_retrain = False`` and the
    method hits the early ``return False`` at the head of the body —
    no challenger is built, no ml_model mutation, no registry write.
    """
    fake_drift = _FakeDriftDetector(
        last_psi=0.0,           # PSI trigger: 0.0 < 0.10 → False
        rolling_brier=None,     # Brier trigger: None → 0.0; 0.0 > 0.22 → False
        recent_actuals=[],      # n_outcomes: 0 < 20 → Brier guard fails
    )
    fake_ml_model = _make_fake_ml_model(brier_score=0.20)
    fake_model_cls, fake_instances = _make_fake_market_model_class(
        challenger_brier=0.05,
    )
    fake_registry = _FakeModelRegistry()

    with (
        patch("ml.training_orchestrator.drift_detector", fake_drift),
        patch("ml.training_orchestrator.ml_model", fake_ml_model),
        patch("ml.training_orchestrator.MarketMLModel", fake_model_cls),
        patch("ml.training_orchestrator.model_registry", fake_registry),
    ):
        orch = ContinuousTrainingOrchestrator()
        # Pre-condition: orchestrator hasn't retrained yet.
        assert orch._retrain_count == 0

        result = await orch.evaluate_and_retrain_if_needed()

        # The X4 contract: return type is exactly bool (not int, not
        # numpy bool_, not a truthy object — Python's ``bool`` is a
        # subclass of ``int`` so we use ``is`` to distinguish).
        assert isinstance(result, bool), (
            f"evaluate_and_retrain_if_needed must return a bool, got "
            f"{type(result).__name__}"
        )
        # No-trigger path returns False.
        assert result is False, (
            "evaluate_and_retrain_if_needed must return False when no "
            "trigger (PSI / Brier / schedule) fires"
        )

        # Belt-and-braces: no challenger was built, no promotion
        # happened (the no-trigger early-return short-circuits before
        # _train_challenger).
        assert fake_instances == [], (
            "no challenger must be built on the no-trigger path"
        )
        assert fake_drift.reset_calls == 0, (
            "drift_detector.reset() must NOT be called on the no-trigger path"
        )
        assert fake_registry.register_calls == [], (
            "model_registry.register_version must NOT be called on the "
            "no-trigger path"
        )
        assert orch._retrain_count == 0, (
            "retrain_count must remain 0 on the no-trigger path"
        )


# ── (5) drift-triggered retrain fires when PSI > threshold ──────────────────


async def test_drift_triggered_retrain_fires_when_psi_above_threshold():
    """When ``drift_detector.last_psi >= DRIFT_RETRAIN_THRESHOLD`` (0.10)
    AND the challenger beats the champion by the ``MIN_IMPROVEMENT_RATIO``
    margin, ``evaluate_and_retrain_if_needed()`` must fire the full
    drift-triggered retrain + champion promotion path and return ``True``.

    Trigger setup:
      * ``drift_detector.last_psi = 0.15``  → PSI trigger fires
        (``0.15 >= 0.10``).
      * ``drift_detector.rolling_brier = None`` → Brier trigger suppressed.
      * ``drift_detector.recent_actuals = []`` → schedule/Brier guards
        satisfied (no competing trigger).
      * Champion ``ml_model.brier_score = 0.20``; challenger
        ``brier_score = 0.05``. The promotion gate is
        ``challenger_brier < current_brier * MIN_IMPROVEMENT_RATIO``
        = ``0.05 < 0.20 * 0.98 = 0.196`` → True → promotion happens.

    Verified end-to-end on the promotion path:
      (a) return value is ``True`` (the promotion branch's only return).
      (b) ``_retrain_count`` was incremented by exactly 1.
      (c) ``drift_detector.reset()`` was called exactly once
          (the post-promotion rolling-window reset).
      (d) exactly one challenger was constructed (``MarketMLModel()``)
          and ``challenger.save()`` was invoked on it (the post-promotion
          atomic hot-swap persistence).
      (e) ``model_registry.register_version(...)`` was called once with
          the canonical ``"v1.champion"`` version tag and the
          challenger's benchmark metrics.
      (f) ``_last_champion_brier`` was updated to the promoted
          challenger's Brier (0.05).

    Belt-and-braces: the ``trigger_reason`` logged inside the method
    names the PSI branch (``f"PSI={psi:.4f}"``) — captured indirectly
    via the ``register_version`` call's ``parameters["retrain_trigger"]``
    field, which is asserted to start with ``"PSI="``.
    """
    # PSI above the 0.10 threshold — drives the psi_trigger branch.
    fake_drift = _FakeDriftDetector(
        last_psi=0.15,          # > DRIFT_RETRAIN_THRESHOLD (0.10) → fires
        rolling_brier=None,     # Brier trigger suppressed
        recent_actuals=[],      # n_outcomes=0 → Brier guard fails
    )
    # Champion Brier 0.20 — the gate target.
    fake_ml_model = _make_fake_ml_model(brier_score=0.20)
    # Challenger Brier 0.05 — beats champion by a wide margin
    # (0.05 < 0.20 * 0.98 = 0.196 → promotion branch fires).
    fake_model_cls, fake_instances = _make_fake_market_model_class(
        challenger_brier=0.05,
    )
    fake_registry = _FakeModelRegistry()

    with (
        patch("ml.training_orchestrator.drift_detector", fake_drift),
        patch("ml.training_orchestrator.ml_model", fake_ml_model),
        patch("ml.training_orchestrator.MarketMLModel", fake_model_cls),
        patch("ml.training_orchestrator.model_registry", fake_registry),
    ):
        orch = ContinuousTrainingOrchestrator()
        pre_retrain_count = orch._retrain_count
        assert pre_retrain_count == 0, (
            "precondition: fresh orchestrator must start at retrain_count=0"
        )

        result = await orch.evaluate_and_retrain_if_needed()

        # (a) Return value is True (promotion branch's only return).
        assert isinstance(result, bool), (
            f"evaluate_and_retrain_if_needed must return a bool, got "
            f"{type(result).__name__}"
        )
        assert result is True, (
            "evaluate_and_retrain_if_needed must return True when PSI > "
            "threshold AND challenger beats champion by MIN_IMPROVEMENT_RATIO"
        )

        # (b) _retrain_count was incremented by exactly 1.
        assert orch._retrain_count == pre_retrain_count + 1, (
            f"_retrain_count must increment by 1 on promotion "
            f"(was {pre_retrain_count}, now {orch._retrain_count})"
        )
        assert orch._retrain_count == 1

        # (c) drift_detector.reset() called exactly once on the
        # promotion path.
        assert fake_drift.reset_calls == 1, (
            f"drift_detector.reset() must be called exactly once on "
            f"promotion, got {fake_drift.reset_calls} calls"
        )

        # (d) Exactly one challenger was built and save() was invoked
        # on it (the post-promotion atomic hot-swap persistence).
        assert len(fake_instances) == 1, (
            f"exactly one challenger must be built via MarketMLModel(), "
            f"got {len(fake_instances)}"
        )
        challenger = fake_instances[0]
        assert challenger.save_calls == 1, (
            f"challenger.save() must be called exactly once on the "
            f"promotion path, got {challenger.save_calls}"
        )
        # fit_initial was called with the sampled hyperparameters
        # (rf_max_depth / gb_learning_rate / n_estimators_rf /
        # n_estimators_gb — the orchestrator's challenger-diversity
        # search space).
        assert len(challenger.fit_initial_calls) == 1, (
            "challenger.fit_initial must be called exactly once with "
            "the sampled hyperparameters"
        )
        hp = challenger.fit_initial_calls[0]
        for hp_key in (
            "rf_max_depth",
            "gb_learning_rate",
            "n_estimators_rf",
            "n_estimators_gb",
        ):
            assert hp_key in hp, (
                f"challenger hyperparameter {hp_key!r} missing from "
                f"fit_initial call: {hp!r}"
            )

        # (e) model_registry.register_version was called once with the
        # canonical "v1.champion" version tag and the challenger's
        # benchmark metrics.
        assert len(fake_registry.register_calls) == 1, (
            f"model_registry.register_version must be called exactly "
            f"once on promotion, got {len(fake_registry.register_calls)}"
        )
        reg_call = fake_registry.register_calls[0]
        assert reg_call["version"] == "v1.champion", (
            f"register_version version tag must be 'v1.champion' on the "
            f"first promotion, got {reg_call['version']!r}"
        )
        assert reg_call["brier_score"] == 0.05, (
            f"register_version brier_score must be the promoted "
            f"challenger's Brier (0.05), got {reg_call['brier_score']!r}"
        )
        assert reg_call["roc_auc"] == 0.95
        assert reg_call["ece"] == 0.01
        # The retrain_trigger parameter records WHICH trigger fired —
        # for the PSI branch it starts with "PSI=".
        params = reg_call.get("parameters", {})
        assert "retrain_trigger" in params, (
            f"register_version parameters must include 'retrain_trigger', "
            f"got {params!r}"
        )
        assert params["retrain_trigger"].startswith("PSI="), (
            f"retrain_trigger must start with 'PSI=' on the PSI-trigger "
            f"branch, got {params['retrain_trigger']!r}"
        )

        # (f) _last_champion_brier was updated to the promoted
        # challenger's Brier.
        assert orch._last_champion_brier == 0.05, (
            f"_last_champion_brier must be updated to the promoted "
            f"challenger's Brier (0.05), got "
            f"{orch._last_champion_brier!r}"
        )

        # Belt-and-braces: the champion singleton's __dict__ was
        # hot-swapped (ml_model.__dict__.update(challenger.__dict__))
        # so ml_model.brier_score now reflects the promoted challenger.
        # This is the atomic hot-swap contract — the live singleton's
        # state is replaced in-place so production code paths reading
        # ml_model.brier_score immediately see the new champion.
        assert fake_ml_model.brier_score == 0.05, (
            f"ml_model.brier_score must be hot-swapped to the promoted "
            f"challenger's Brier (0.05) via __dict__.update, got "
            f"{fake_ml_model.brier_score!r}"
        )
