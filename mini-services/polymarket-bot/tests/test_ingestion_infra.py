"""tests/test_ingestion_infra.py — unit tests for the W31-4 ingestion
infrastructure (dead-letter queue + checkpoint + health monitor).

Covers the three new ``ingestion.*`` modules' public contracts:

  * ``ingestion.dead_letter.DeadLetterQueue`` — add / get / get_pending /
    mark_retried / clear / depth / get_stats + alert firing.
  * ``ingestion.checkpoint.CheckpointManager`` — save / load / resume /
    list_checkpoints / clear + partial-update merging + cross-instance
    persistence.
  * ``ingestion.health.IngestionHealthMonitor`` — record_event /
    record_failure / record_dlq_depth / mark_available /
    mark_unavailable / get_metrics / check_alerts + alert firing for
    every threshold (no_data / high_error_rate / dlq_depth_high /
    high_latency / source_unavailable) + debounce behaviour.

Isolation strategy
-------------------
Each test constructs a fresh ``DeadLetterQueue(db_path=tmp_path / ...)
`` / ``CheckpointManager(db_path=...)`` instance per test so the
SQLite stores are empty at the start of every test — no cross-test
pollution. The module-level singletons (``dead_letter_queue`` /
``checkpoint_manager`` / ``ingestion_health_monitor``) are NOT
exercised here (they're constructed against the default
``/app/data/*.db`` paths which are unwritable in the sandbox, and
the test scope is per-instance behaviour, not the singleton wiring).

The ``alert_engine.record_alert`` call (which every DLQ add and
every health alert makes) is patched to a ``Mock`` via monkeypatch so
the tests don't depend on the alerting SQLite store and can assert
on the alert invocation (name / category / severity / message /
metadata).

Time mocking
------------
Several health-monitor tests need to advance the wall clock past
``NO_DATA_THRESHOLD`` (60s) / ``ALERT_DEBOUNCE`` (60s). They use
``monkeypatch.setattr('time.time', fake_now_fn)`` which patches the
global ``time.time`` function for the duration of the test. This
affects every module in the process — acceptable in tests since
the assertions are scoped to the health monitor's behaviour and
no other module's behaviour is asserted on within the same test.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). Mirrors the pattern in
# ``tests/test_dedup.py`` and ``tests/test_data_validator.py``.
_TMP_ROOT = Path("/tmp/ingestion_infra_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    # The sibling ``core.alerting`` singleton (used by DLQ + health
    # via lazy import) defaults to ``/app/data/alerts.db`` which is
    # not writable in the sandbox. Redirect it to /tmp so the
    # alert-engine singleton's _init_db doesn't emit a noisy ERROR
    # log line on every test run.
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    # Belt-and-braces with conftest's MARKET_DB_PATH redirect so the
    # ``core.timescale_db`` singleton doesn't raise PermissionError
    # at import time (which would propagate through the defensive
    # try/except in ``ingestion/__init__.py`` and pollute the test
    # log output).
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-ingestion-infra",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*`` / ``core.*``). Mirrors the bootstrap pattern in
# every existing ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import
# mode inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — see the note below.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# ── Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package. ──────────────────────────────────────────
# A sibling wave (W31-7) created ``tests/ingestion/`` with an ``__init__.py``,
# turning it into a Python package also named ``ingestion``. Pytest's default
# ``prepend`` import mode inserts ``tests/`` at ``sys.path[0]`` during test
# collection, which means Python finds ``tests/ingestion/`` BEFORE our
# top-level ``polymarket-bot/ingestion/`` package. The stale module cache
# entry then causes ``from ingestion.checkpoint import ...`` to fail with
# ``ModuleNotFoundError: No module named 'ingestion.checkpoint'`` (because
# ``tests/ingestion/`` has no ``checkpoint.py``).
#
# Fix: (1) move ``_PROJECT_ROOT`` to ``sys.path[0]`` (done above) so it
# wins the package-resolution race against ``tests/``, and (2) clear any
# cached ``ingestion`` / ``ingestion.*`` module that points at the
# ``tests/ingestion/`` directory so the next import resolves against the
# freshly-prepended ``_PROJECT_ROOT`` and finds our top-level package.
# W31-7's tests don't import from ``ingestion.*`` (they import from
# ``core.*`` directly — verified by grepping their source), so clearing
# this cache doesn't break them.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

from ingestion.checkpoint import (  # noqa: E402
    Checkpoint,
    CheckpointManager,
)
from ingestion.dead_letter import (  # noqa: E402
    DeadLetterQueue,
    DeadLetterRecord,
)
from ingestion.health import (  # noqa: E402
    ALERT_DEBOUNCE,
    DLQ_DEPTH_THRESHOLD,
    ERROR_RATE_THRESHOLD,
    LATENCY_THRESHOLD,
    NO_DATA_THRESHOLD,
    IngestionHealthMonitor,
)


# ── Shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def dlq(tmp_path: Path) -> DeadLetterQueue:
    """Fresh ``DeadLetterQueue`` instance backed by a tmp_path DB.

    Alert firing is ENABLED (the test patches ``alert_engine.record_alert``
    via monkeypatch to a Mock so the alert is observable but doesn't
    actually persist to SQLite).
    """
    return DeadLetterQueue(db_path=tmp_path / "dlq.db", alert_enabled=True)


@pytest.fixture
def dlq_no_alert(tmp_path: Path) -> DeadLetterQueue:
    """DLQ instance with alert firing DISABLED.

    For tests that exercise the queue's data-path without caring about
    the alert side-effect.
    """
    return DeadLetterQueue(db_path=tmp_path / "dlq_no_alert.db", alert_enabled=False)


@pytest.fixture
def ckpt(tmp_path: Path) -> CheckpointManager:
    """Fresh ``CheckpointManager`` instance backed by a tmp_path DB."""
    return CheckpointManager(db_path=tmp_path / "ckpt.db")


@pytest.fixture
def health() -> IngestionHealthMonitor:
    """Fresh ``IngestionHealthMonitor`` instance (in-memory)."""
    return IngestionHealthMonitor()


@pytest.fixture(autouse=True)
def _patch_alert_engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``core.alerting.alert_engine.record_alert`` with a Mock.

    Autouse so every test gets a fresh Mock — the DLQ's ``add()`` and
    the health monitor's ``_fire()`` both call ``record_alert`` lazily,
    so patching the function on the ``alert_engine`` singleton (which
    is constructed at module-import time) is the cleanest interception
    point. The Mock captures every call so tests can assert on
    ``call_args`` / ``call_count``.

    The patch is applied to the singleton's bound method (NOT the
    class method) so the production ``AlertEngine.record_alert`` is
    untouched — only the singleton's call site is intercepted.
    """
    try:
        from core.alerting import alert_engine
    except Exception:  # pragma: no cover — defensive
        alert_engine = MagicMock()  # type: ignore[assignment]
    mock = MagicMock()
    # ``alert_engine.record_alert`` is a bound method — assigning a
    # plain MagicMock to the attribute replaces it for the duration
    # of the test. ``monkeypatch.setattr`` undoes the assignment on
    # teardown.
    monkeypatch.setattr(alert_engine, "record_alert", mock)
    return mock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dead-letter queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeadLetterQueueAddGet:
    """``add`` / ``get`` / ``get_pending`` contract."""

    def test_add_returns_record_id(self, dlq: DeadLetterQueue) -> None:
        rid = dlq.add(
            source="clob_rest",
            record_type="trade",
            payload={"token_id": "T1", "price": 0.5, "size": 10, "side": "BUY"},
            reason="validation_failed",
            error="Invalid price: -1.0",
        )
        assert isinstance(rid, str)
        assert len(rid) == 32  # UUID4 hex length

    def test_add_and_get_roundtrip(self, dlq: DeadLetterQueue) -> None:
        payload = {"token_id": "T1", "price": 0.5, "size": 10, "side": "BUY"}
        rid = dlq.add(
            source="clob_rest",
            record_type="trade",
            payload=payload,
            reason="validation_failed",
            error="Invalid price: -1.0",
            metadata={"warnings": ["Crossed market"]},
        )
        record = dlq.get(rid)
        assert record is not None
        assert record.record_id == rid
        assert record.source == "clob_rest"
        assert record.record_type == "trade"
        assert record.payload == payload
        assert record.reason == "validation_failed"
        assert record.error == "Invalid price: -1.0"
        assert record.status == "pending"
        assert record.retry_count == 0
        assert record.first_seen > 0
        assert record.last_attempt == 0.0
        assert record.metadata == {"warnings": ["Crossed market"]}

    def test_add_with_stack_trace_preserved(self, dlq: DeadLetterQueue) -> None:
        rid = dlq.add(
            source="ws_book",
            record_type="snapshot",
            payload={"token_id": "T1", "best_bid": 0.4, "best_ask": 0.6},
            reason="storage_error",
            error="DB write failed",
            stack_trace="Traceback (most recent call last):\n  File ...",
        )
        record = dlq.get(rid)
        assert record is not None
        assert record.stack_trace.startswith("Traceback")

    def test_get_unknown_returns_none(self, dlq: DeadLetterQueue) -> None:
        assert dlq.get("nonexistent-id") is None

    def test_get_pending_returns_oldest_first(
        self, dlq: DeadLetterQueue
    ) -> None:
        # Add three records with distinct first_seen timestamps (sleep
        # briefly between adds so they're not all in the same ms).
        r1 = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        time.sleep(0.01)
        r2 = dlq.add("clob_rest", "trade", {"i": 2}, "validation_failed", "e2")
        time.sleep(0.01)
        r3 = dlq.add("clob_rest", "trade", {"i": 3}, "validation_failed", "e3")
        pending = dlq.get_pending(limit=10)
        assert [r.record_id for r in pending] == [r1, r2, r3]

    def test_get_pending_source_filter(self, dlq: DeadLetterQueue) -> None:
        dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.add("gamma_api", "event", {"i": 2}, "validation_failed", "e2")
        dlq.add("clob_rest", "trade", {"i": 3}, "validation_failed", "e3")
        clob_pending = dlq.get_pending(limit=10, source="clob_rest")
        assert len(clob_pending) == 2
        assert all(r.source == "clob_rest" for r in clob_pending)
        gamma_pending = dlq.get_pending(limit=10, source="gamma_api")
        assert len(gamma_pending) == 1
        assert gamma_pending[0].source == "gamma_api"

    def test_get_pending_respects_limit(self, dlq: DeadLetterQueue) -> None:
        for i in range(5):
            dlq.add("clob_rest", "trade", {"i": i}, "validation_failed", f"e{i}")
        pending = dlq.get_pending(limit=2)
        assert len(pending) == 2


class TestDeadLetterQueueRetry:
    """``mark_retried`` contract."""

    def test_retry_success_marks_retried(self, dlq: DeadLetterQueue) -> None:
        rid = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        ok = dlq.mark_retried(rid, success=True)
        assert ok is True
        record = dlq.get(rid)
        assert record is not None
        assert record.status == "retried"
        assert record.retry_count == 1
        assert record.last_attempt > 0

    def test_retry_failure_increments_count(
        self, dlq: DeadLetterQueue
    ) -> None:
        rid = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        ok = dlq.mark_retried(rid, success=False)
        assert ok is True
        record = dlq.get(rid)
        assert record is not None
        assert record.status == "pending"
        assert record.retry_count == 1

    def test_retry_abandoned_after_max_retries(
        self, dlq: DeadLetterQueue
    ) -> None:
        # Default MAX_RETRIES = 3 — three failed retries → abandoned.
        rid = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        assert dlq.mark_retried(rid, success=False)
        assert dlq.mark_retried(rid, success=False)
        assert dlq.mark_retried(rid, success=False)
        record = dlq.get(rid)
        assert record is not None
        assert record.status == "abandoned"
        assert record.retry_count == 3

    def test_retry_unknown_returns_false(self, dlq: DeadLetterQueue) -> None:
        assert dlq.mark_retried("nonexistent-id", success=True) is False

    def test_retry_success_after_failure_resets_status(
        self, dlq: DeadLetterQueue
    ) -> None:
        # One failed retry, then a successful one → status = retried.
        rid = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.mark_retried(rid, success=False)
        dlq.mark_retried(rid, success=True)
        record = dlq.get(rid)
        assert record is not None
        assert record.status == "retried"
        assert record.retry_count == 2

    def test_custom_max_retries(self, tmp_path: Path) -> None:
        """A DLQ with ``max_retries=1`` abandons after ONE failed retry."""
        q = DeadLetterQueue(
            db_path=tmp_path / "dlq_custom.db",
            alert_enabled=False,
            max_retries=1,
        )
        rid = q.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        q.mark_retried(rid, success=False)
        record = q.get(rid)
        assert record is not None
        assert record.status == "abandoned"


class TestDeadLetterQueueClearAndStats:
    """``clear`` / ``depth`` / ``get_stats`` contract."""

    def test_clear_all(self, dlq: DeadLetterQueue) -> None:
        dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.add("gamma_api", "event", {"i": 2}, "validation_failed", "e2")
        n = dlq.clear()
        assert n == 2
        assert dlq.depth() == 0

    def test_clear_by_source(self, dlq: DeadLetterQueue) -> None:
        dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.add("gamma_api", "event", {"i": 2}, "validation_failed", "e2")
        dlq.add("clob_rest", "trade", {"i": 3}, "validation_failed", "e3")
        n = dlq.clear(source="clob_rest")
        assert n == 2
        assert dlq.depth() == 1

    def test_clear_by_status(self, dlq: DeadLetterQueue) -> None:
        r1 = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.add("clob_rest", "trade", {"i": 2}, "validation_failed", "e2")
        dlq.mark_retried(r1, success=True)
        # r1 is now 'retried', r2 is 'pending'.
        n = dlq.clear(status="retried")
        assert n == 1
        assert dlq.depth() == 1
        assert dlq.depth(status="pending") == 1

    def test_clear_by_source_and_status(self, dlq: DeadLetterQueue) -> None:
        r1 = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        r2 = dlq.add("gamma_api", "event", {"i": 2}, "validation_failed", "e2")
        dlq.mark_retried(r1, success=True)
        dlq.mark_retried(r2, success=True)
        n = dlq.clear(source="clob_rest", status="retried")
        assert n == 1
        assert dlq.depth() == 1

    def test_depth_total_and_by_status(self, dlq: DeadLetterQueue) -> None:
        r1 = dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        r2 = dlq.add("clob_rest", "trade", {"i": 2}, "validation_failed", "e2")
        r3 = dlq.add("clob_rest", "trade", {"i": 3}, "validation_failed", "e3")
        dlq.mark_retried(r1, success=True)
        dlq.mark_retried(r2, success=False)
        dlq.mark_retried(r2, success=False)
        dlq.mark_retried(r2, success=False)  # → abandoned
        dlq.mark_retried(r3, success=False)
        assert dlq.depth() == 3
        assert dlq.depth(status="retried") == 1
        assert dlq.depth(status="abandoned") == 1
        assert dlq.depth(status="pending") == 1

    def test_get_stats_shape(self, dlq: DeadLetterQueue) -> None:
        dlq.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        dlq.add("gamma_api", "event", {"i": 2}, "validation_failed", "e2")
        dlq.add("clob_rest", "trade", {"i": 3}, "validation_failed", "e3")
        stats = dlq.get_stats()
        assert stats["total"] == 3
        assert stats["pending"] == 3
        assert stats["retried"] == 0
        assert stats["abandoned"] == 0
        assert stats["by_source"] == {"clob_rest": 2, "gamma_api": 1}

    def test_get_stats_empty(self, dlq: DeadLetterQueue) -> None:
        stats = dlq.get_stats()
        assert stats == {
            "total": 0,
            "pending": 0,
            "retried": 0,
            "abandoned": 0,
            "by_source": {},
        }

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        """A second DLQ instance pointing at the same DB file sees the records."""
        q1 = DeadLetterQueue(
            db_path=tmp_path / "shared.db", alert_enabled=False
        )
        rid = q1.add("clob_rest", "trade", {"i": 1}, "validation_failed", "e1")
        q2 = DeadLetterQueue(
            db_path=tmp_path / "shared.db", alert_enabled=False
        )
        record = q2.get(rid)
        assert record is not None
        assert record.source == "clob_rest"
        assert q2.depth() == 1


class TestDeadLetterAlerts:
    """Alert-firing side-effect of ``add()``."""

    def test_add_fires_warning_alert(
        self,
        dlq: DeadLetterQueue,
        _patch_alert_engine: MagicMock,
    ) -> None:
        dlq.add(
            source="clob_rest",
            record_type="trade",
            payload={"token_id": "T1"},
            reason="validation_failed",
            error="Invalid price: -1.0",
        )
        _patch_alert_engine.assert_called_once()
        kwargs = _patch_alert_engine.call_args.kwargs
        assert kwargs["name"] == "dead_letter_record_added"
        assert kwargs["category"] == "data"
        assert kwargs["severity"] == "warning"
        assert "Dead-letter record added" in kwargs["message"]
        assert kwargs["metadata"]["source"] == "clob_rest"
        assert kwargs["metadata"]["reason"] == "validation_failed"
        assert "record_id" in kwargs["metadata"]

    def test_alert_disabled_does_not_fire(
        self,
        dlq_no_alert: DeadLetterQueue,
        _patch_alert_engine: MagicMock,
    ) -> None:
        dlq_no_alert.add(
            source="clob_rest",
            record_type="trade",
            payload={},
            reason="validation_failed",
            error="e",
        )
        _patch_alert_engine.assert_not_called()

    def test_add_failure_does_not_raise(
        self,
        tmp_path: Path,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """An ``add`` call against an unwritable DB returns empty string, no raise."""
        # Construct a DLQ against a directory-as-file path (mkdir
        # succeeds but opening as a SQLite DB file fails).
        bad_path = tmp_path / "a_dir"
        bad_path.mkdir()
        q = DeadLetterQueue(db_path=bad_path, alert_enabled=False)
        # No exception — the storage error is swallowed.
        rid = q.add(
            source="clob_rest",
            record_type="trade",
            payload={},
            reason="validation_failed",
            error="e",
        )
        assert rid == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Checkpoint manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCheckpointSaveLoad:
    """``save`` / ``load`` / ``resume`` contract."""

    def test_save_and_load_roundtrip(self, ckpt: CheckpointManager) -> None:
        ok = ckpt.save(
            source="clob_rest",
            last_processed=1717283400.5,
            last_processed_type="timestamp",
            offset=0,
            metadata={"cursor": "abc"},
        )
        assert ok is True
        cp = ckpt.load("clob_rest")
        assert cp is not None
        assert cp.source == "clob_rest"
        assert cp.last_processed == 1717283400.5
        assert cp.last_processed_type == "timestamp"
        assert cp.metadata == {"cursor": "abc"}
        assert cp.created_at > 0
        assert cp.updated_at >= cp.created_at

    def test_load_unknown_returns_none(self, ckpt: CheckpointManager) -> None:
        assert ckpt.load("nonexistent_source") is None

    def test_resume_is_alias_for_load(self, ckpt: CheckpointManager) -> None:
        ckpt.save("clob_rest", last_processed=100.0)
        assert ckpt.resume("clob_rest") == ckpt.load("clob_rest")

    def test_save_with_defaults(self, ckpt: CheckpointManager) -> None:
        """A bare ``save(source)`` creates a row with sensible defaults."""
        ok = ckpt.save("clob_rest")
        assert ok is True
        cp = ckpt.load("clob_rest")
        assert cp is not None
        assert cp.last_processed == 0.0
        assert cp.last_processed_type == "timestamp"
        assert cp.offset == 0
        assert cp.metadata == {}

    def test_save_sequence_based(self, ckpt: CheckpointManager) -> None:
        ckpt.save(
            source="clob_rest",
            last_processed=12345,
            last_processed_type="sequence",
        )
        cp = ckpt.load("clob_rest")
        assert cp is not None
        assert cp.last_processed == 12345.0
        assert cp.last_processed_type == "sequence"

    def test_save_offset_based(self, ckpt: CheckpointManager) -> None:
        ckpt.save(
            source="gamma_api",
            offset=500,
            last_processed_type="offset",
            metadata={"page_size": 100},
        )
        cp = ckpt.load("gamma_api")
        assert cp is not None
        assert cp.offset == 500
        assert cp.last_processed_type == "offset"
        assert cp.metadata == {"page_size": 100}

    def test_save_partial_update_preserves_other_fields(
        self, ckpt: CheckpointManager
    ) -> None:
        """A ``save`` with only ``offset`` keeps the existing ``last_processed``."""
        ckpt.save(
            source="clob_rest",
            last_processed=100.0,
            last_processed_type="timestamp",
            offset=0,
        )
        ckpt.save(source="clob_rest", offset=50)  # only offset
        cp = ckpt.load("clob_rest")
        assert cp is not None
        # last_processed preserved.
        assert cp.last_processed == 100.0
        assert cp.last_processed_type == "timestamp"
        # offset updated.
        assert cp.offset == 50

    def test_save_merges_metadata(self, ckpt: CheckpointManager) -> None:
        ckpt.save(
            source="clob_rest",
            metadata={"cursor": "abc", "batch_id": 1},
        )
        ckpt.save(source="clob_rest", metadata={"cursor": "xyz"})
        cp = ckpt.load("clob_rest")
        assert cp is not None
        # cursor overwritten, batch_id preserved.
        assert cp.metadata == {"cursor": "xyz", "batch_id": 1}

    def test_save_updates_updated_at(self, ckpt: CheckpointManager) -> None:
        ckpt.save("clob_rest", last_processed=100.0)
        cp1 = ckpt.load("clob_rest")
        assert cp1 is not None
        time.sleep(0.01)
        ckpt.save("clob_rest", last_processed=200.0)
        cp2 = ckpt.load("clob_rest")
        assert cp2 is not None
        assert cp2.updated_at > cp1.updated_at
        # created_at preserved across updates.
        assert cp2.created_at == cp1.created_at


class TestCheckpointListClear:
    """``list_checkpoints`` / ``clear`` contract."""

    def test_list_checkpoints_alphabetical(
        self, ckpt: CheckpointManager
    ) -> None:
        ckpt.save("zeta_api", last_processed=300.0)
        ckpt.save("alpha_api", last_processed=100.0)
        ckpt.save("mid_api", last_processed=200.0)
        cps = ckpt.list_checkpoints()
        sources = [cp.source for cp in cps]
        assert sources == ["alpha_api", "mid_api", "zeta_api"]

    def test_list_empty(self, ckpt: CheckpointManager) -> None:
        assert ckpt.list_checkpoints() == []

    def test_clear_one(self, ckpt: CheckpointManager) -> None:
        ckpt.save("clob_rest", last_processed=100.0)
        ckpt.save("gamma_api", last_processed=200.0)
        n = ckpt.clear("clob_rest")
        assert n == 1
        assert ckpt.load("clob_rest") is None
        assert ckpt.load("gamma_api") is not None

    def test_clear_all(self, ckpt: CheckpointManager) -> None:
        ckpt.save("clob_rest", last_processed=100.0)
        ckpt.save("gamma_api", last_processed=200.0)
        n = ckpt.clear()
        assert n == 2
        assert ckpt.list_checkpoints() == []

    def test_clear_unknown_source_returns_zero(
        self, ckpt: CheckpointManager
    ) -> None:
        assert ckpt.clear("nonexistent_source") == 0


class TestCheckpointPersistence:
    """Cross-instance persistence (the resume-after-restart guarantee)."""

    def test_new_instance_sees_prior_checkpoints(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "shared_ckpt.db"
        m1 = CheckpointManager(db_path=db)
        m1.save("clob_rest", last_processed=12345.5)
        m2 = CheckpointManager(db_path=db)
        cp = m2.load("clob_rest")
        assert cp is not None
        assert cp.last_processed == 12345.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Health monitor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHealthRecordMetrics:
    """``record_event`` / ``record_failure`` / ``record_dlq_depth`` /
    ``mark_available`` / ``mark_unavailable``."""

    def test_record_event_increments_counters(
        self, health: IngestionHealthMonitor
    ) -> None:
        health.record_event("clob_rest")
        health.record_event("clob_rest")
        m = health.get_metrics("clob_rest")
        assert m["events_received"] == 2
        assert m["events_failed"] == 0
        assert m["error_rate"] == 0.0
        assert m["last_event_at"] > 0
        assert m["available"] is True

    def test_record_event_failure_increments_failed(
        self, health: IngestionHealthMonitor
    ) -> None:
        health.record_event("clob_rest", success=False, error="boom")
        m = health.get_metrics("clob_rest")
        assert m["events_received"] == 1
        assert m["events_failed"] == 1
        assert m["error_rate"] == 1.0
        assert m["last_error"] == "boom"
        # last_event_at IS updated on a failed record_event (the event
        # was processed, just unsuccessfully).
        assert m["last_event_at"] > 0

    def test_record_failure_does_not_update_last_event_at(
        self, health: IngestionHealthMonitor
    ) -> None:
        # ``record_failure`` is for failures BEFORE an event was
        # received (e.g. API 5xx). It does NOT update last_event_at
        # so the ``no_data_received`` alert fires on the absence of
        # successful events.
        health.record_failure("clob_rest", error="api_5xx")
        m = health.get_metrics("clob_rest")
        assert m["events_received"] == 1
        assert m["events_failed"] == 1
        assert m["last_event_at"] == 0.0
        assert m["last_error"] == "api_5xx"

    def test_record_event_with_event_time(self, health: IngestionHealthMonitor) -> None:
        event_time = time.time() - 1.5  # 1.5s old
        health.record_event("clob_rest", event_time=event_time)
        m = health.get_metrics("clob_rest")
        assert m["last_event_time"] == event_time
        assert m["last_latency"] >= 1.4  # ~1.5s ± clock granularity

    def test_record_event_no_event_time_uses_now(
        self, health: IngestionHealthMonitor
    ) -> None:
        # event_time=None → defaults to processing time → latency ~0.
        health.record_event("clob_rest")
        m = health.get_metrics("clob_rest")
        assert m["last_latency"] < 0.1

    def test_record_dlq_depth(self, health: IngestionHealthMonitor) -> None:
        health.record_dlq_depth("clob_rest", 42)
        m = health.get_metrics("clob_rest")
        assert m["dlq_depth"] == 42

    def test_mark_unavailable_available(self, health: IngestionHealthMonitor) -> None:
        health.mark_unavailable("clob_rest")
        assert health.get_metrics("clob_rest")["available"] is False
        health.mark_available("clob_rest")
        assert health.get_metrics("clob_rest")["available"] is True

    def test_get_metrics_unknown_source(self, health: IngestionHealthMonitor) -> None:
        assert health.get_metrics("nonexistent_source") == {}

    def test_get_metrics_all_sources(self, health: IngestionHealthMonitor) -> None:
        health.record_event("clob_rest")
        health.record_event("gamma_api")
        m = health.get_metrics()
        assert set(m.keys()) == {"clob_rest", "gamma_api"}
        assert m["clob_rest"]["source"] == "clob_rest"
        assert m["gamma_api"]["source"] == "gamma_api"


class TestHealthThroughput:
    """``throughput_eps`` rolling-window computation."""

    def test_throughput_zero_with_no_events(
        self, health: IngestionHealthMonitor
    ) -> None:
        m = health.get_metrics("clob_rest")
        # Unknown source → empty metrics dict (no throughput key).
        assert m == {}

    def test_throughput_positive_with_events(
        self, health: IngestionHealthMonitor
    ) -> None:
        # Record 10 events; throughput should be > 0 within the 60s window.
        for _ in range(10):
            health.record_event("clob_rest")
        m = health.get_metrics("clob_rest")
        assert m["throughput_eps"] > 0

    def test_throughput_decays_after_window(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Events older than the window don't count toward throughput."""
        # Patch time.time to a fixed baseline so we can advance it past
        # the 60s throughput window.
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        # Record 5 events at t0.
        for _ in range(5):
            health.record_event("clob_rest")
        # Advance the clock 120s (past the 60s window).
        fake_now[0] = t0 + 120.0
        m = health.get_metrics("clob_rest")
        # All latencies are now outside the window → throughput is 0.
        assert m["throughput_eps"] == 0.0


# ── Alert evaluation ──────────────────────────────────────────────────────


class TestHealthAlerts:
    """``check_alerts`` contract — fires + debounces per-source alerts."""

    def test_no_alerts_when_healthy(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # No events recorded at all — no alert should fire.
        fired = health.check_alerts()
        assert fired == []
        _patch_alert_engine.assert_not_called()

    def test_no_data_alert_fires(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # Record one event at t0, then advance the clock 61s.
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        health.record_event("clob_rest")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        fired = health.check_alerts()
        assert len(fired) == 1
        assert fired[0]["alert"] == "no_data_received"
        assert fired[0]["source"] == "clob_rest"
        assert fired[0]["threshold"] == NO_DATA_THRESHOLD
        _patch_alert_engine.assert_called_once()
        kwargs = _patch_alert_engine.call_args.kwargs
        assert kwargs["name"] == "no_data_received"
        assert kwargs["severity"] == "warning"

    def test_no_data_alert_skipped_when_never_received(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """A source with no successful events ever recorded doesn't fire."""
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        # Only a failure — no successful event.
        health.record_failure("clob_rest", error="api_5xx")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        fired = health.check_alerts()
        # No no_data_received alert — last_event_at is still 0.
        alerts_fired = {f["alert"] for f in fired}
        assert "no_data_received" not in alerts_fired

    def test_high_error_rate_alert(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # 10 events, 6 failures → 60% error rate > 5% threshold.
        for _ in range(4):
            health.record_event("clob_rest")
        for _ in range(6):
            health.record_event("clob_rest", success=False, error="boom")
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "high_error_rate" in alerts_fired
        # Severity is warning.
        kwargs = next(
            c.kwargs
            for c in _patch_alert_engine.call_args_list
            if c.kwargs.get("name") == "high_error_rate"
        )
        assert kwargs["severity"] == "warning"

    def test_high_error_rate_skipped_below_min_traffic(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # 1 event, 1 failure → 100% error rate, but < 10 events so no alert.
        health.record_event("clob_rest", success=False, error="boom")
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "high_error_rate" not in alerts_fired

    def test_dlq_depth_alert(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        health.record_dlq_depth("clob_rest", DLQ_DEPTH_THRESHOLD + 1)
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "dlq_depth_high" in alerts_fired
        kwargs = next(
            c.kwargs
            for c in _patch_alert_engine.call_args_list
            if c.kwargs.get("name") == "dlq_depth_high"
        )
        assert kwargs["severity"] == "critical"

    def test_dlq_depth_at_threshold_does_not_fire(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # depth == threshold (100) — strict > so no alert.
        health.record_dlq_depth("clob_rest", DLQ_DEPTH_THRESHOLD)
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "dlq_depth_high" not in alerts_fired

    def test_high_latency_alert(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        # Record an event whose event_time is 10s in the past → latency 10s.
        event_time = time.time() - 10.0
        health.record_event("clob_rest", event_time=event_time)
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "high_latency" in alerts_fired

    def test_source_unavailable_alert(
        self,
        health: IngestionHealthMonitor,
        _patch_alert_engine: MagicMock,
    ) -> None:
        health.mark_unavailable("clob_rest")
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "source_unavailable" in alerts_fired

    def test_multiple_alerts_fire_for_one_source(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """A source that's both stalled and unavailable fires both alerts."""
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        health.record_event("clob_rest")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        health.mark_unavailable("clob_rest")
        fired = health.check_alerts()
        alerts_fired = {f["alert"] for f in fired}
        assert "no_data_received" in alerts_fired
        assert "source_unavailable" in alerts_fired

    def test_alert_debounce(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """A second ``check_alerts`` within ALERT_DEBOUNCE doesn't re-fire."""
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        health.record_event("clob_rest")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        fired1 = health.check_alerts()
        assert len(fired1) == 1
        # Advance just 1s — within debounce window.
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 2.0
        fired2 = health.check_alerts()
        assert fired2 == []
        # record_alert should have been called exactly once.
        assert _patch_alert_engine.call_count == 1

    def test_alert_refires_after_debounce_window(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """After ALERT_DEBOUNCE elapses, the same alert fires again."""
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        health.record_event("clob_rest")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        fired1 = health.check_alerts()
        assert len(fired1) == 1
        # Advance past the debounce window.
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0 + ALERT_DEBOUNCE + 1.0
        fired2 = health.check_alerts()
        assert len(fired2) == 1
        assert fired2[0]["alert"] == "no_data_received"
        assert _patch_alert_engine.call_count == 2

    def test_reset_alert_debounce_allows_refire(
        self,
        health: IngestionHealthMonitor,
        monkeypatch: pytest.MonkeyPatch,
        _patch_alert_engine: MagicMock,
    ) -> None:
        """``reset_alert_debounce`` clears the debounce so the next check_alerts fires immediately."""
        t0 = 1_000_000.0
        fake_now = [t0]
        monkeypatch.setattr("time.time", lambda: fake_now[0])
        health.record_event("clob_rest")
        fake_now[0] = t0 + NO_DATA_THRESHOLD + 1.0
        fired1 = health.check_alerts()
        assert len(fired1) == 1
        health.reset_alert_debounce("clob_rest")
        fired2 = health.check_alerts()
        assert len(fired2) == 1


class TestHealthModuleConstants:
    """The threshold constants are exposed at module level."""

    def test_thresholds_match_spec(self) -> None:
        # W31-4 task spec values.
        assert NO_DATA_THRESHOLD == 60.0
        assert ERROR_RATE_THRESHOLD == 0.05
        assert DLQ_DEPTH_THRESHOLD == 100
        assert LATENCY_THRESHOLD == 5.0
        assert ALERT_DEBOUNCE == 60.0
