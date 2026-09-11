"""
Unit tests for ``core/observability_collector.py``.

W8 — Observability collector unit tests.

Five behaviours required by the W8 spec
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  (1) ``start_collector`` is idempotent — calling twice doesn't start
      two loops.
  (2) ``collect_once`` records metrics across categories.
  (3) ``stop_collector`` cancels the loop.
  (4) ``is_running`` returns True after start, False after stop.
  (5) system metrics include ``cpu_percent`` and ``memory_percent``.

Spec ↔ module surface reconciliation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two of the spec's named entrypoints don't exist verbatim on the
module's public API:

  * **``collect_once``** — the module does NOT expose a public
    ``collect_once`` function. The equivalent single-collection-pass
    entrypoint is the private ``_collect_cycle()`` coroutine, which
    fans out to the per-subsystem ``_collect_*`` collectors and emits
    the bot-level ``cycles`` heartbeat at the end. Test (2) invokes
    ``_collect_cycle()`` directly (the spec's ``collect_once`` concept)
    and asserts the recorded metrics span multiple canonical categories.

  * **``is_running``** — the module does NOT expose a public
    ``is_running`` function. Collector liveness is encoded in the
    module-level ``_collector_task`` global (``None`` ⇒ not running;
    non-None & not done ⇒ running). The spec's ``is_running`` concept
    is realised here by a test-local helper ``_is_running()`` that
    reads the module global — equivalent to what a public
    ``is_running`` would expose if it existed. Test (4) asserts the
    underlying state machine satisfies the spec contract via that
    helper.

Both gaps are documented inline so a future task that adds the public
``collect_once`` / ``is_running`` symbols can simply replace the
private-function / helper references with the real public calls and
delete the explanatory note.

Environment
~~~~~~~~~~~

``tests/conftest.py`` (auto-loaded by pytest BEFORE any sibling test
module) redirects every persisted-state path to a writable ``/tmp``
sandbox via ``os.environ.setdefault`` BEFORE the first project import.
This is load-bearing for the collector tests because:

  * ``OBSERVABILITY_DB_PATH`` → ``/tmp/pmbot_conftest_isolation/observability.db``
    so the module-level ``observability`` singleton's SQLite file is
    writable (not ``/app/data/observability.db``, which is read-only in
    the sandbox). The collector's ``record_metric`` calls therefore
    persist for real and can be read back via
    ``observability.get_metric_history()``.
  * ``MODEL_PATH`` / ``MODEL_REGISTRY_PATH`` → ``/tmp`` paths so
    ``ml.model.ml_model`` (constructed at module import) doesn't
    crash trying to write to ``/app/data``. Without this redirect,
    the ``_collect_ml_metrics`` collector's ``from ml.model import
    ml_model`` line raises ``PermissionError``, the whole ``ml``
    bucket is skipped, and test (2)'s "metrics across categories"
    assertion would still pass (4 of 5 categories) but mask a
    real-environment regression.

State isolation
~~~~~~~~~~~~~~~

The collector's module-level ``_collector_task`` global persists across
tests (the module is imported once per pytest session). An autouse
``_reset_collector_state`` async fixture stops any leftover task and
clears the global before AND after every test so:

  * Test (1)'s idempotency assertion starts from a known-clean baseline
    (a leftover non-None ``_collector_task`` from a prior test would
    make the FIRST ``start_collector()`` call a no-op and the
    idempotency check would pass for the wrong reason).
  * No background task is left dangling when a test ends (avoids
    pytest-asyncio's "Task was destroyed but it is pending" warning
    if a test crashes before its own ``await stop_collector()`` runs).

Belt-and-braces with the autouse ``_reset_store_factory_defaults`` in
``conftest.py`` (which resets the ``store`` / ``risk_manager`` /
``paper_sim`` singletons the collector reads from).

Async mode
~~~~~~~~~~

pytest-asyncio 1.3.0 is installed. The repo's ``pytest.ini`` declares
``testpaths = tests`` but does NOT set ``asyncio_mode`` (the W8 task
forbids editing existing files), so the project default
(``asyncio_mode=strict``) applies. Every ``async def test_*`` is
decorated via the module-level ``pytestmark = pytest.mark.asyncio``
idiom — same convention used by every sibling test module in the repo
(``test_observability.py``, ``test_decision_ledger.py``,
``test_paper_simulator.py``, …). The autouse ``_reset_collector_state``
fixture uses ``@pytest_asyncio.fixture(autouse=True)`` (the
async-fixture decorator) so it can ``await stop_collector()`` for
proper task teardown.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules
# (``core.observability_collector``) regardless of the cwd pytest was
# launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling. conftest.py also inserts the project root
# into sys.path, but the inline guard makes this module self-sufficient
# if it's ever run standalone (``pytest tests/test_observability_collector.py``
# from a non-project cwd).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)
import pytest_asyncio  # noqa: E402

import core.observability_collector as observability_collector  # noqa: E402
from core.observability import (  # noqa: E402
    CAT_BOT,
    CAT_DATA_SOURCE,
    CAT_EXECUTION,
    CAT_ML,
    CAT_SYSTEM,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` cannot be edited per the W8 task
# constraint ("Do NOT edit existing files"), so we use the module-level
# ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``. Same
# convention as every sibling test module.
pytestmark = pytest.mark.asyncio


# ── Test-local helper: ``is_running`` concept ───────────────────────────────
# The collector module exposes liveness via the module-level
# ``_collector_task`` global rather than a public ``is_running`` function.
# This helper encapsulates the state-machine lookup so the test reads as
# the spec writes it. If a future task adds a public ``is_running`` to
# the module, this helper can be replaced with the real call (and the
# explanatory note above deleted).
def _is_running() -> bool:
    """Return True iff the collector has a non-None, not-done task handle.

    Mirrors the semantics a public ``is_running()`` would expose:
      * ``_collector_task is None``           → False (never started / stopped)
      * ``_collector_task`` non-None & ``.done()`` → False (task finished)
      * ``_collector_task`` non-None & not done   → True  (loop running)
    """
    task = observability_collector._collector_task
    return task is not None and not task.done()


# ── Autouse: reset collector state before & after every test ───────────────
@pytest_asyncio.fixture(autouse=True)
async def _reset_collector_state():
    """Stop any leftover collector task and clear the module global.

    Pre-test: clear any non-None ``_collector_task`` left behind by a
    prior test (e.g. the prior test crashed before its
    ``await stop_collector()`` ran). Without this, the next test's
    ``start_collector()`` call would silently no-op (idempotency
    short-circuit) and the assertions would pass for the wrong reason.

    Post-test: call ``await stop_collector()`` so any task this test
    started is properly cancelled and awaited — avoids pytest-asyncio's
    "Task was destroyed but it is pending" warning. The await is
    best-effort: if ``stop_collector`` itself raises (it shouldn't —
    it has a broad except), the exception is swallowed so the test
    result is determined by the test body, not by teardown noise.

    Belt-and-braces with conftest's autouse ``_reset_store_factory_defaults``
    (which resets the ``store`` / ``risk_manager`` / ``paper_sim``
    singletons the collector reads from).
    """
    # Pre-test: clear the module global so this test starts clean.
    # (If a prior test left a task bound to a now-closed event loop,
    # that task is an orphan — clearing the global is the load-bearing
    # part for idempotency; the orphan itself can't run on the new
    # test's fresh event loop and will be GC'd.)
    observability_collector._collector_task = None

    yield  # ── test runs ──

    # Post-test: stop whatever task this test started.
    if observability_collector._collector_task is not None:
        try:
            await observability_collector.stop_collector()
        except Exception:
            # Best-effort cleanup — never let teardown noise mask the
            # real test result.
            pass
        observability_collector._collector_task = None


# ── (1) start_collector is idempotent ───────────────────────────────────────
async def test_start_collector_is_idempotent():
    """Calling ``start_collector()`` twice must NOT create two background
    loops.

    The module's idempotency contract (from the ``start_collector``
    docstring): if ``_collector_task`` is already non-None and not done,
    the second call is a no-op (logs at debug) — the existing task is
    returned unchanged.

    Verified by two complementary assertions:
      (a) **Identity check** — the module-level ``_collector_task``
          reference is identical before and after the second call. If
          the second call had created a new task, the reference would
          differ.
      (b) **Loop-wide task count** — exactly one asyncio task named
          ``observability-collector`` exists on the running event loop
          after the second call (counted via ``asyncio.all_tasks()``,
          which excludes the current coroutine). Two concurrent loops
          would surface as count == 2.
    """
    # First start — schedules the background loop.
    await observability_collector.start_collector()
    task_after_first = observability_collector._collector_task
    assert task_after_first is not None, "start_collector should populate _collector_task"
    assert not task_after_first.done(), "freshly-started task must not be done"
    assert task_after_first.get_name() == "observability-collector"

    # Second start — must be a no-op (idempotency contract).
    await observability_collector.start_collector()
    task_after_second = observability_collector._collector_task

    # (a) Identity check — same single task handle, not a replacement.
    assert task_after_second is task_after_first, (
        "start_collector() called twice must NOT replace the existing task — "
        "second call should be a no-op"
    )

    # (b) Loop-wide count — exactly one "observability-collector" task.
    # ``asyncio.all_tasks()`` excludes the current coroutine (the test's
    # own async def), so the count reflects only background work.
    collector_tasks_on_loop = [
        t for t in asyncio.all_tasks()
        if t.get_name() == "observability-collector"
    ]
    assert len(collector_tasks_on_loop) == 1, (
        f"expected exactly 1 'observability-collector' task after two "
        f"start_collector() calls, got {len(collector_tasks_on_loop)}"
    )

    # Teardown handled by the autouse ``_reset_collector_state`` fixture.


# ── (2) collect_once records metrics across categories ─────────────────────
async def test_collect_once_records_metrics_across_categories(monkeypatch):
    """A single ``_collect_cycle()`` pass must record metrics spanning
    multiple canonical categories (``data_source`` / ``execution`` /
    ``ml`` / ``system`` / ``bot``).

    NOTE: the module does NOT expose a public ``collect_once`` function.
    The equivalent single-collection-pass entrypoint is the private
    ``_collect_cycle()`` coroutine — the spec's ``collect_once`` concept.
    This test invokes ``_collect_cycle()`` directly.

    The cycle's design contract: each ``_collect_*`` call is
    independently fault-tolerant (catches its own exceptions and logs at
    debug), but in aggregate the cycle is expected to emit metrics
    across multiple canonical categories. Under the conftest env
    redirects (``MODEL_PATH`` / ``STORE_STATE_PATH`` /
    ``OBSERVABILITY_DB_PATH`` all pointed at writable ``/tmp`` paths),
    all four subsystem collectors succeed and the ``bot/cycles``
    heartbeat is appended at the end.

    Strategy: capture every ``record_metric`` call by monkeypatching the
    module-level binding inside ``core.observability_collector``. The
    ``_collect_*`` functions resolve the bare ``record_metric`` name
    through the module's globals at call time, so monkeypatching the
    binding on the module object is sufficient to redirect every
    internal call. The fake still forwards to the real backend so
    production-like behaviour is exercised end-to-end.

    Assertions:
      * The set of categories touched includes the four always-present
        ones (``data_source``, ``execution``, ``system``, ``bot``).
        ``ml`` is environment-sensitive (depends on ``ml_model``
        importing cleanly) so it's a bonus, not a hard requirement.
      * The ``bot/cycles`` heartbeat is always recorded (unconditional
        final step of ``_collect_cycle`` — the collector's own liveness
        signal).
      * The cycle emits a healthy number of metrics total (loose lower
        bound guards against a regression where one of the
        ``_collect_*`` collectors silently no-ops).
    """
    captured: list[tuple[str, str, float, dict]] = []
    original_record_metric = observability_collector.record_metric

    async def capturing_record_metric(category, name, value, **metadata):
        captured.append((category, name, value, metadata))
        # Forward to the real backend so behaviour under test is
        # production-like (and so a sibling test that reads back via
        # ``observability.get_metric_history`` would see the rows too).
        await original_record_metric(category, name, value, **metadata)

    monkeypatch.setattr(
        observability_collector, "record_metric", capturing_record_metric
    )

    # The spec's "collect_once" entrypoint — a single collection pass
    # across all four subsystems + the bot heartbeat.
    await observability_collector._collect_cycle()

    categories_touched = {cat for cat, _, _, _ in captured}
    names_touched = {(cat, name) for cat, name, _, _ in captured}

    # The four always-present categories must each have at least one
    # metric recorded. (``ml`` is environment-sensitive — included as a
    # bonus when ``ml_model`` imports cleanly under conftest env, but
    # not a hard requirement.)
    always_present = {CAT_DATA_SOURCE, CAT_EXECUTION, CAT_SYSTEM, CAT_BOT}
    missing = always_present - categories_touched
    assert not missing, (
        f"collect_once should record metrics in {sorted(always_present)}; "
        f"missing categories: {sorted(missing)}; observed: {sorted(categories_touched)}"
    )

    # At least 4 distinct categories touched (5 when ml succeeds).
    assert len(categories_touched) >= 4, (
        f"expected >=4 distinct categories touched by _collect_cycle, "
        f"got {len(categories_touched)}: {sorted(categories_touched)}"
    )

    # The bot/cycles heartbeat is unconditional — must always be present
    # regardless of subsystem import success (it's the collector's own
    # liveness signal; if the dashboard sees ``bot/cycles`` age growing,
    # the collector itself is stuck).
    assert (CAT_BOT, "cycles") in names_touched, (
        "the bot/cycles heartbeat must be recorded on every _collect_cycle pass"
    )

    # Loose lower bound on total metric count — guards against a
    # regression where one of the ``_collect_*`` collectors silently
    # no-ops (e.g. a stray ``return`` added before the record_metric
    # calls). The cycle emits ~14-23 metrics in practice (4 data_source
    # + 7 execution + 7-9 ml + 3 system + 1 bot); 10 is a conservative
    # floor that still catches a wholesale skip of any one collector.
    assert len(captured) >= 10, (
        f"expected >=10 record_metric calls in one _collect_cycle pass, "
        f"got {len(captured)}"
    )


# ── (3) stop_collector cancels the loop ─────────────────────────────────────
async def test_stop_collector_cancels_loop():
    """``stop_collector()`` must cancel the running background loop and
    clear the module-level ``_collector_task`` global.

    The loop's first pass runs IMMEDIATELY (no initial sleep) so the
    dashboard has data on boot, then it sleeps
    ``COLLECTION_INTERVAL_SECONDS`` (30 s) before the next pass. So
    after ``start_collector()`` returns, the task is either mid-first-pass
    or blocked on ``asyncio.sleep(30)``. ``stop_collector`` must:

      * Clear the module-level ``_collector_task`` global (so a
        subsequent ``start_collector`` call creates a fresh task —
        verified end-to-end in test (4)).
      * Cancel the task and await its completion — the task ends in
        CANCELLED state (the loop's ``except asyncio.CancelledError``
        re-raises so the cancel propagates cleanly).
      * Complete without raising (the ``except asyncio.CancelledError``
        inside ``stop_collector`` swallows the propagated cancel).

    Verified by:
      (a) The captured task reference (saved before stop) is done AND
          cancelled after stop returns.
      (b) The module global is None after stop returns.
      (c) No ``observability-collector`` task remains on the loop.
    """
    await observability_collector.start_collector()
    task = observability_collector._collector_task
    assert task is not None, "precondition: start_collector must populate the task"
    assert not task.done(), "precondition: freshly-started task must not be done"

    # stop_collector must not raise (the internal
    # ``except asyncio.CancelledError: pass`` swallows the propagated
    # cancel from the awaited task).
    await observability_collector.stop_collector()

    # (b) Global cleared — so a subsequent start creates a fresh task.
    assert observability_collector._collector_task is None, (
        "stop_collector must clear the _collector_task module global"
    )

    # (a) The task we captured is now done (cancel propagated + awaited).
    assert task.done(), "stopped task must be done"
    assert task.cancelled(), (
        "stopped task must be in CANCELLED state — _collector_loop "
        "re-raises CancelledError so the cancel propagates cleanly"
    )

    # (c) No "observability-collector" task remains on the loop.
    # ``asyncio.all_tasks()`` excludes the current coroutine.
    lingering = [
        t for t in asyncio.all_tasks()
        if t.get_name() == "observability-collector"
    ]
    assert lingering == [], (
        f"no 'observability-collector' task should remain after stop_collector; "
        f"found {len(lingering)}"
    )


# ── (4) is_running returns True after start, False after stop ──────────────
async def test_is_running_reflects_lifecycle():
    """The W8 spec's ``is_running`` contract: True after
    ``start_collector()``, False after ``stop_collector()``.

    NOTE: the module does NOT expose a public ``is_running`` function.
    Collector liveness is encoded in the module-level ``_collector_task``
    global (``None`` ⇒ not running; non-None & not done ⇒ running). This
    test verifies the state machine via the test-local ``_is_running()``
    helper — equivalent to what a public ``is_running`` would expose.

    The full contract exercised here:
      * Before start:        ``is_running()`` is False
      * After start:         ``is_running()`` is True
      * After stop:          ``is_running()`` is False
      * After stop + start:  ``is_running()`` is True again (restart
                             works — stop clears the global so a
                             subsequent start creates a fresh task)
      * After second stop:   ``is_running()`` is False
    """
    # Pre-condition: autouse fixture has cleared the global, so the
    # collector is not running. Asserted explicitly so the test is
    # self-documenting if the fixture is ever changed.
    assert _is_running() is False, "precondition: collector must not be running before start"

    # Start → running.
    await observability_collector.start_collector()
    assert _is_running() is True, "is_running() must be True after start_collector"

    # Stop → not running.
    await observability_collector.stop_collector()
    assert _is_running() is False, "is_running() must be False after stop_collector"

    # Restart works — stop_collector clears the global so a subsequent
    # start_collector creates a fresh task (not a no-op).
    await observability_collector.start_collector()
    assert _is_running() is True, (
        "is_running() must be True after a second start_collector (restart "
        "must work — stop_collector clears the module global so the "
        "idempotency guard doesn't short-circuit)"
    )

    # Second stop → not running.
    await observability_collector.stop_collector()
    assert _is_running() is False, "is_running() must be False after the second stop_collector"


# ── (5) system metrics include cpu_percent and memory_percent ──────────────
async def test_system_metrics_include_cpu_and_memory(monkeypatch):
    """``_collect_system_metrics()`` must emit ``cpu_percent`` and
    ``memory_percent`` under the ``system`` category, sourced from
    ``psutil``.

    The collector's system metrics are documented (module docstring
    table) to include ``cpu_percent``, ``memory_percent``, and
    ``memory_used_mb``. This test pins the two required by the W8 spec
    (``cpu_percent`` + ``memory_percent``) by capturing
    ``record_metric`` calls during ``_collect_system_metrics()`` and
    asserting both names appear under ``CAT_SYSTEM``. ``psutil`` is a
    runtime dependency (already required by
    ``core.observability.Observability.record_system_snapshot``); the
    test will ``xfail`` gracefully if ``psutil`` is not installed
    (mirrors the ``_collect_system_metrics`` early-return path).

    To make the value-assertion deterministic and not depend on the
    host's instantaneous CPU load, ``psutil.cpu_percent`` and
    ``psutil.virtual_memory`` are monkeypatched to fixed returns before
    the call. The monkeypatch targets the ``psutil`` module object
    itself (which is cached in ``sys.modules``), so the
    ``import psutil`` line inside ``_collect_system_metrics`` retrieves
    the same already-patched module — the patched values flow through
    end-to-end.

    Assertions:
      * ``cpu_percent``, ``memory_percent``, and ``memory_used_mb`` all
        appear under ``CAT_SYSTEM`` in the captured calls.
      * The recorded ``cpu_percent`` value matches the monkeypatched
        psutil read (float-coerced) — confirms the value flowed through
        end-to-end, not just the name.
      * The recorded ``memory_percent`` value matches the monkeypatched
        psutil read.
    """
    try:
        import psutil  # noqa: WPS433  (local import — mirrors the module's own pattern)
    except ImportError:
        pytest.xfail(
            "psutil not installed — _collect_system_metrics early-returns; "
            "system metrics cannot be asserted in this environment"
        )

    # Pin psutil reads to deterministic values so the value-assertion is
    # independent of host load. ``_FakeVMem`` exposes the two attributes
    # ``_collect_system_metrics`` actually reads (``.percent`` and ``.used``).
    class _FakeVMem:
        percent = 73.5
        used = 123 * 1024 * 1024  # 123 MiB → memory_used_mb should be 123.0

    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 42.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _FakeVMem())

    # Capture record_metric calls by monkeypatching the module-level
    # binding inside ``core.observability_collector``. The
    # ``_collect_system_metrics`` function resolves the bare
    # ``record_metric`` name through the module's globals at call time,
    # so this redirect catches every internal call.
    captured: list[tuple[str, str, float, dict]] = []
    original_record_metric = observability_collector.record_metric

    async def capturing_record_metric(category, name, value, **metadata):
        captured.append((category, name, value, metadata))
        await original_record_metric(category, name, value, **metadata)

    monkeypatch.setattr(
        observability_collector, "record_metric", capturing_record_metric
    )

    await observability_collector._collect_system_metrics()

    # Names recorded under the ``system`` category.
    system_names = {
        name for cat, name, _, _ in captured if cat == CAT_SYSTEM
    }

    # The two W8-required system metrics.
    assert "cpu_percent" in system_names, (
        "_collect_system_metrics must emit 'cpu_percent' under CAT_SYSTEM"
    )
    assert "memory_percent" in system_names, (
        "_collect_system_metrics must emit 'memory_percent' under CAT_SYSTEM"
    )

    # Bonus: ``memory_used_mb`` is also emitted (documented in the
    # module docstring table). Pinned so a future refactor that drops
    # it surfaces here rather than silently regressing the dashboard.
    assert "memory_used_mb" in system_names, (
        "_collect_system_metrics must emit 'memory_used_mb' under CAT_SYSTEM "
        "(documented in the module docstring table)"
    )

    # Value round-trip: the recorded cpu_percent matches the
    # monkeypatched psutil read (float-coerced). Confirms the value
    # flowed through end-to-end, not just the name.
    cpu_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "cpu_percent"
    ]
    assert len(cpu_calls) == 1, (
        f"expected exactly 1 cpu_percent record_metric call, got {len(cpu_calls)}"
    )
    assert cpu_calls[0][2] == pytest.approx(42.0), (
        "recorded cpu_percent value must match the monkeypatched psutil read"
    )

    # Same for memory_percent.
    mem_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "memory_percent"
    ]
    assert len(mem_calls) == 1, (
        f"expected exactly 1 memory_percent record_metric call, got {len(mem_calls)}"
    )
    assert mem_calls[0][2] == pytest.approx(73.5), (
        "recorded memory_percent value must match the monkeypatched psutil read"
    )

    # And memory_used_mb is derived correctly from the monkeypatched
    # ``mem.used`` (123 MiB → 123.0 MB after the round() / conversion).
    used_calls = [
        c for c in captured if c[0] == CAT_SYSTEM and c[1] == "memory_used_mb"
    ]
    assert len(used_calls) == 1
    assert used_calls[0][2] == pytest.approx(123.0), (
        "memory_used_mb must be derived from psutil.virtual_memory().used "
        "via round(used / (1024*1024), 2)"
    )
