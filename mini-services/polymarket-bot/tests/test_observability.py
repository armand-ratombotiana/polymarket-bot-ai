"""
Unit tests for ``core/observability.py``.

T10 — System Observability unit tests.

Covers the six behaviours required by the task spec:

  (1) ``record_metric(category, name, value, **metadata)`` persists a metric
      row carrying the supplied ``category`` / ``name`` / ``value``
      (verifiable via ``get_metric_history``).
  (2) ``get_metric_history(name, limit=N)`` returns the most-recent-N
      samples for ``name``, newest-first.
  (3) ``value=True`` / ``value=False`` are coerced to ``1.0`` / ``0.0`` via
      the ``float(value)`` cast inside ``record_metric``.
  (4) Non-numeric ``value`` (e.g. a string, ``None``) is coerced to ``0.0``
      — the recorder logs at ``debug`` and persists 0.0 so an observability
      hiccup never breaks the trading pipeline.
  (5) ``get_health_report()`` returns a structured report whose ``categories``
      dict has all six canonical categories (``data_source`` / ``bot`` /
      ``strategy`` / ``execution`` / ``ml`` / ``system``) as keys, even when
      no metric has been recorded for some of them.
  (6) Status derivation (HEALTHY / DEGRADED / UNHEALTHY): the current
      implementation does NOT derive an overall status — there is no
      top-level ``status`` field in the report. The report exposes the
      *inputs* a future derivation rule would consume
      (``newest_sample_age_seconds`` / ``oldest_sample_age_seconds``,
      per-metric ``value`` + ``age_seconds``). This test PINS the current
      contract and documents the gap so a future enhancement that adds
      the field can flip the assertion.

Each test constructs a fresh ``Observability`` instance against a
``tmp_path``-scoped SQLite file (mirrors the per-test temp-DB convention
used in ``tests/test_decision_ledger.py``). The module-level singleton
``observability`` (instantiated at import time) is left untouched — we
never record or read from it.

``OBSERVABILITY_DB_PATH`` is redirected to ``/tmp`` *before* the first
import of any project module so the import-time ``Observability()`` ctor
does NOT attempt to write to ``/app/data`` in the sandbox. The ctor's
``_init_db`` swallows init errors, but redirecting keeps the test
hermetic and avoids polluting the repo's ``data/`` directory if the
sandbox ever mounts ``/app/data`` writable. ``setdefault`` lets an
outer runner override if needed.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (pytest-asyncio is already a project
dependency — the repo's ``pytest.ini`` / ``pyproject.toml`` cannot be
edited per the T10 task constraint "Do NOT edit existing files", so we
use the ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ── Redirect OBSERVABILITY_DB_PATH to /tmp BEFORE importing the module. ──
# The observability singleton is constructed at import time and reads its
# DB path from this env var (falling back to ``/app/data/observability.db``).
# Redirecting keeps the import-time ``_init_db`` call hermetic — it never
# touches the production path, even if the sandbox mounts ``/app/data``
# writable. ``setdefault`` lets an outer runner override if needed.
_TMP_ROOT = Path("/tmp/observability_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("OBSERVABILITY_DB_PATH", str(_TMP_ROOT / "observability.db"))

# Make the polymarket-bot package root importable as top-level modules
# (``core.observability``) regardless of the cwd pytest was launched from.
# Mirrors the bootstrap pattern in tests/test_features.py and
# tests/test_risk_manager.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.observability import (  # noqa: E402
    CATEGORIES,
    CAT_BOT,
    CAT_DATA_SOURCE,
    CAT_EXECUTION,
    CAT_ML,
    CAT_STRATEGY,
    CAT_SYSTEM,
    Observability,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` cannot be edited per the T10 task
# constraint ("Do NOT edit existing files"), so we use the module-level
# ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh temp-DB-backed Observability per test ────────────────────
@pytest.fixture
def obs(tmp_path):
    """
    Return an ``Observability`` instance whose SQLite file lives under
    ``tmp_path``.

    Passing ``db_path`` explicitly exercises the same code path production
    uses (``__init__`` resolves ``db_path`` → ``DB_PATH`` module global →
    ``OBSERVABILITY_DB_PATH`` env var) while keeping each test fully
    hermetic. The module-level singleton ``observability`` (instantiated at
    import time against the env-var-redirected /tmp path) is left untouched
    — we never record or read from it.
    """
    return Observability(db_path=tmp_path / "test_observability.db")


# ── 1. record_metric stores category / name / value ───────────────────────
async def test_record_metric_stores_category_name_value(obs):
    """``record_metric(category, name, value, **metadata)`` must persist a
    metric row carrying the supplied ``category`` / ``name`` / ``value``
    exactly as the caller supplied them."""
    await obs.record_metric(CAT_BOT, "cycles", 42, scan_id="scan-001")

    history = await obs.get_metric_history("cycles")
    assert len(history) == 1

    row = history[0]
    # Identity columns persisted verbatim.
    assert row["category"] == CAT_BOT
    assert row["name"] == "cycles"
    # Value coerced to float (int 42 → 42.0).
    assert row["value"] == pytest.approx(42.0)
    # Metadata round-tripped through JSON.
    assert row["metadata"] == {"scan_id": "scan-001"}
    # Timestamp is a recent epoch second.
    assert isinstance(row["timestamp"], float)
    assert row["timestamp"] > 0
    assert time.time() - row["timestamp"] < 5.0


# ── 2. get_metric_history returns recent samples, newest-first ───────────
async def test_get_metric_history_returns_recent_samples(obs):
    """``get_metric_history(name, limit=N)`` must return the most-recent-N
    samples for ``name``, ordered newest-first."""
    # Record 5 samples for "latency" with tiny sleeps so each lands at a
    # strictly greater ``time.time()`` value. SQLite stores REAL with
    # ~µs precision; 5 ms is a comfortable margin even on a loaded CI box.
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    for v in values:
        await obs.record_metric(CAT_DATA_SOURCE, "latency", v)
        await asyncio.sleep(0.005)

    # Default limit returns all 5 (newest-first).
    history = await obs.get_metric_history("latency")
    assert len(history) == 5
    assert [r["value"] for r in history] == [50.0, 40.0, 30.0, 20.0, 10.0]

    # Explicit limit caps the result count.
    recent = await obs.get_metric_history("latency", limit=2)
    assert len(recent) == 2
    assert [r["value"] for r in recent] == [50.0, 40.0]

    # Unknown metric name → empty list (the API's 404 path depends on this).
    assert await obs.get_metric_history("does_not_exist") == []

    # Empty name → empty list (the silent-skip contract).
    assert await obs.get_metric_history("") == []


# ── 3. boolean value is coerced to 0.0 / 1.0 ──────────────────────────────
async def test_record_metric_coerces_boolean_to_zero_or_one(obs):
    """``value=True`` / ``value=False`` must be coerced to ``1.0`` / ``0.0``
    via the ``float(value)`` cast inside ``record_metric``."""
    await obs.record_metric(CAT_BOT, "errors", False)  # oldest
    await asyncio.sleep(0.005)
    await obs.record_metric(CAT_BOT, "errors", True)   # newest

    history = await obs.get_metric_history("errors")
    assert len(history) == 2

    # Newest-first ordering → True (1.0) is the most recent, False (0.0) older.
    assert history[0]["value"] == pytest.approx(1.0)
    assert history[1]["value"] == pytest.approx(0.0)


# ── 4. non-numeric value is coerced to 0.0 ────────────────────────────────
async def test_record_metric_coerces_non_numeric_to_zero(obs):
    """A non-numeric ``value`` (e.g. a string or ``None``) must be coerced
    to ``0.0`` — the recorder logs at ``debug`` and persists 0.0 so an
    observability hiccup never breaks the trading pipeline."""
    # A string that cannot be parsed as a float.
    await obs.record_metric(CAT_ML, "drift", "not-a-number")

    history = await obs.get_metric_history("drift")
    assert len(history) == 1
    assert history[0]["value"] == pytest.approx(0.0)
    assert history[0]["category"] == CAT_ML
    assert history[0]["name"] == "drift"

    # ``None`` also falls back to 0.0 (TypeError caught by the except).
    await asyncio.sleep(0.005)
    await obs.record_metric(CAT_ML, "drift", None)
    history = await obs.get_metric_history("drift")
    assert len(history) == 2
    # Newest-first → None-coerced 0.0 is the most recent.
    assert history[0]["value"] == pytest.approx(0.0)


# ── 5. get_health_report returns the six canonical categories ────────────
async def test_get_health_report_returns_six_categories(obs):
    """``get_health_report()`` must return a structured report whose
    ``categories`` dict has all six canonical categories as keys, even
    when no metric has been recorded for some of them."""
    # Empty report (fresh DB) — all six buckets present, all empty.
    empty = await obs.get_health_report()
    assert isinstance(empty, dict)
    assert empty["category_count"] == 6
    assert empty["metric_count"] == 0
    assert set(empty["categories"].keys()) == set(CATEGORIES)
    assert tuple(empty["categories"].keys()) == CATEGORIES
    for cat in CATEGORIES:
        assert empty["categories"][cat] == {}
    # No samples → both age fields are None.
    assert empty["oldest_sample_age_seconds"] is None
    assert empty["newest_sample_age_seconds"] is None

    # Record one metric in three different categories.
    await obs.record_metric(CAT_DATA_SOURCE, "updates", 7)
    await obs.record_metric(CAT_BOT, "cycles", 3)
    await obs.record_metric(CAT_SYSTEM, "cpu_percent", 55.5)

    report = await obs.get_health_report()
    assert report["category_count"] == 6
    assert report["metric_count"] == 3
    assert set(report["categories"].keys()) == set(CATEGORIES)

    # Populated categories carry the latest value under the metric name.
    assert "updates" in report["categories"][CAT_DATA_SOURCE]
    assert (
        report["categories"][CAT_DATA_SOURCE]["updates"]["value"]
        == pytest.approx(7.0)
    )
    assert "cycles" in report["categories"][CAT_BOT]
    assert report["categories"][CAT_BOT]["cycles"]["value"] == pytest.approx(3.0)
    assert "cpu_percent" in report["categories"][CAT_SYSTEM]
    assert (
        report["categories"][CAT_SYSTEM]["cpu_percent"]["value"]
        == pytest.approx(55.5)
    )

    # Each entry carries its timestamp + age_seconds (the inputs a future
    # status derivation would consume).
    for cat in (CAT_DATA_SOURCE, CAT_BOT, CAT_SYSTEM):
        for _, entry in report["categories"][cat].items():
            assert isinstance(entry["timestamp"], float)
            assert entry["timestamp"] > 0
            assert isinstance(entry["age_seconds"], float)
            assert entry["age_seconds"] >= 0.0

    # Empty categories are still present (empty dict).
    assert report["categories"][CAT_STRATEGY] == {}
    assert report["categories"][CAT_EXECUTION] == {}
    assert report["categories"][CAT_ML] == {}

    # With samples, age fields are populated floats (non-negative).
    assert isinstance(report["newest_sample_age_seconds"], float)
    assert isinstance(report["oldest_sample_age_seconds"], float)
    assert report["newest_sample_age_seconds"] >= 0.0
    # Oldest >= Newest (oldest sample is at least as old as the newest one).
    assert (
        report["oldest_sample_age_seconds"]
        >= report["newest_sample_age_seconds"]
    )


# ── 6. Status derivation (HEALTHY / DEGRADED / UNHEALTHY) ─────────────────
async def test_status_derivation_field_absent_and_inputs_present(obs):
    """Status derivation (HEALTHY / DEGRADED / UNHEALTHY) is NOT currently
    implemented in ``core/observability.py``.

    The current ``get_health_report()`` exposes the *inputs* a future
    derivation rule would consume:

      * ``newest_sample_age_seconds`` — a fresh value (< threshold) would
        be HEALTHY; a stale value (> threshold) would be DEGRADED/UNHEALTHY.
      * ``oldest_sample_age_seconds`` — for tail-staleness checks.
      * per-metric ``value`` + ``age_seconds`` — for value-domain checks
        (e.g. ``bot.errors > 0`` ⇒ DEGRADED; ``system.memory_percent > 95``
        ⇒ UNHEALTHY).

    This test PINS the current contract: the report does NOT include a
    top-level ``status`` field. A future enhancement that adds status
    derivation should flip the assertion to check the new field's value
    for each canonical case (empty → HEALTHY; recent-only metrics →
    HEALTHY; stale metrics → DEGRADED; metrics-old + value-domain breach
    → UNHEALTHY).

    The test also verifies the *inputs* to a future status derivation are
    correct: an empty report has null age fields, and a fresh sample
    yields a small, recent ``newest_sample_age_seconds``.
    """
    # Empty report — no metrics have been recorded.
    empty = await obs.get_health_report()
    # The current contract: NO top-level ``status`` field.
    assert "status" not in empty
    # The age inputs are present but null (no samples → can't derive age).
    assert empty["newest_sample_age_seconds"] is None
    assert empty["oldest_sample_age_seconds"] is None

    # Record a fresh metric.
    await obs.record_metric(CAT_BOT, "cycles", 1)
    fresh = await obs.get_health_report()
    # Still no top-level ``status`` field — the gap is pinned.
    assert "status" not in fresh
    # The newest-sample age is a small, fresh value — the input a future
    # HEALTHY derivation would consume.
    assert isinstance(fresh["newest_sample_age_seconds"], float)
    assert fresh["newest_sample_age_seconds"] < 5.0  # fresh
    assert (
        fresh["oldest_sample_age_seconds"]
        == fresh["newest_sample_age_seconds"]  # only one sample
    )

    # Sanity: the canonical HEALTHY/DEGRADED/UNHEALTHY strings are NOT
    # present anywhere in the report's top-level keys. This is a guard
    # against a future partial implementation that adds a ``status``
    # field under a different name (e.g. ``health_status``) — the test
    # would catch it and force an explicit decision to either rename it
    # to ``status`` or update this assertion.
    for forbidden_key in ("status", "health_status", "overall_status"):
        assert forbidden_key not in fresh
