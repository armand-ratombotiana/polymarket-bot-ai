"""
tests/test_retention.py — Unit tests for ``core/retention.py``.

U8 — Data Retention Pruning unit tests.

Covers the seven behaviour contracts required by the task spec:

  (1) ``prune_old_data`` deletes rows older than ``max_age_hours``.
  (2) ``prune_old_data`` keeps recent rows (rows whose ``timestamp`` is
      inside the ``max_age_hours`` window).
  (3) ``prune_observability`` uses the 7-day default window
      (``OBSERVABILITY_RETENTION_HOURS = 7 * 24 = 168``).
  (4) ``prune_decision_ledger`` uses the 30-day default window
      (``DECISION_LEDGER_RETENTION_HOURS = 30 * 24 = 720``). The decision
      ledger prune walks BOTH ``decision_events`` AND
      ``decision_rejections`` and returns the sum — this test verifies
      both tables are pruned under the default window.
  (5) ``prune_audit_events`` uses the 90-day default window
      (``AUDIT_EVENTS_RETENTION_HOURS = 90 * 24 = 2160``).
  (6) ``run_all_pruning()`` returns a structured summary dict
      (``timestamp`` / ``results`` / ``total_pruned`` / ``success``)
      with one entry per store.
  (7) ``prune_old_data`` rejects invalid table names — the regex
      ``^[A-Za-z_][A-Za-z0-9_]*$`` guard is the only line of defence
      against SQL injection because SQLite cannot parameterise
      identifiers. A classic ``"metrics; DROP TABLE users;--"``
      injection attempt must raise ``ValueError``.

DB isolation strategy
---------------------
The retention module resolves four DB paths at *import time* from env
vars (``OBSERVABILITY_DB_PATH``, ``DECISION_LEDGER_DB_PATH``,
``EXECUTION_QUALITY_DB_PATH``, ``AUDIT_DB_PATH``). The repo's
``tests/conftest.py`` already redirects every persisted-state env var to
``/tmp/pmbot_conftest_isolation`` before any project module is imported.
This file additionally sets the same redirects via ``setdefault`` so it
stays hermetic even if a future test run somehow bypasses conftest
(same defensive pattern as the sibling ``tests/test_capital_allocator.py``
/ ``tests/test_execution_quality.py``).

The four specialised prune functions read their DB path from the
module-level constants at *call time* (Python's global-name lookup
re-resolves the constant each call), so per-test ``monkeypatch.setattr``
on ``core.retention.<CONST>`` is sufficient to redirect each test to a
``tmp_path``-scoped SQLite file. The generic ``prune_old_data(table,
max_age_hours, db_path)`` primitive accepts an explicit ``db_path``
argument, so tests 1-2 don't even need a monkeypatch — they pass
``tmp_path`` directly.

Every test is synchronous (the entire ``core/retention.py`` module is
sync; only the HTTP route handler in ``register_routes`` wraps the
sync calls in ``asyncio.to_thread``). The repo's ``pytest.ini`` /
``pyproject.toml`` are intentionally left untouched (U8 constraint: "Do
NOT edit existing files"); no ``pytest.mark.asyncio`` is required.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Defensive env-var redirect (see "DB isolation strategy" docstring). ──────
# ``setdefault`` lets conftest (which loads first) win when present; this
# block is purely a defensive net so the file stays hermetic in a
# hypothetical conftest-less invocation. Mirrors the established pattern
# in tests/test_capital_allocator.py / test_execution_quality.py.
_TMP_ROOT = Path("/tmp/retention_tests")
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
    # Force paper mode + live disabled so any co-collected stateful test
    # doesn't trip a shadow / live-trading gate at import time.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from. Mirrors
# the bootstrap pattern in tests/test_features.py /
# tests/test_paper_simulator.py / test_decision_ledger.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

import core.retention as retention  # noqa: E402
from core.retention import (  # noqa: E402
    AUDIT_EVENTS_RETENTION_HOURS,
    AUDIT_DB_PATH,
    DECISION_LEDGER_DB_PATH,
    DECISION_LEDGER_RETENTION_HOURS,
    EXECUTION_QUALITY_DB_PATH,
    OBSERVABILITY_DB_PATH,
    OBSERVABILITY_RETENTION_HOURS,
    prune_audit_events,
    prune_decision_ledger,
    prune_old_data,
    prune_observability,
    run_all_pruning,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _create_table_with_timestamp(db_path: Path, table: str) -> None:
    """Create ``table`` (with a ``timestamp REAL`` column) in ``db_path``.

    Mirrors the minimum schema every prune-able table in the project
    shares — ``core/observability.py`` (``metrics``),
    ``core/decision_ledger.py`` (``decision_events`` +
    ``decision_rejections``), ``core/execution_quality.py``
    (``execution_quality``), and ``core/audit_logger.py``
    (``audit_events``). ``prune_old_data`` only references the
    ``timestamp`` column, so a single-column schema is sufficient to
    exercise the contract.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"CREATE TABLE {table} (timestamp REAL NOT NULL)")


def _insert_row(db_path: Path, table: str, timestamp: float) -> None:
    """Insert one row with the given epoch-seconds timestamp."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {table} (timestamp) VALUES (?)",
            (timestamp,),
        )


def _count_rows(db_path: Path, table: str) -> int:
    """Return the current row count for ``table`` in ``db_path``."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])


# ── 1. prune_old_data deletes rows older than max_age_hours ─────────────────

def test_prune_old_data_deletes_old_rows(tmp_path):
    """``prune_old_data(table, max_age_hours, db_path)`` must DELETE every
    row whose ``timestamp`` is older than ``now - max_age_hours * 3600``
    and return the deleted count.

    Setup: 3 rows at ``now``, ``now - 5h``, ``now - 25h``. With a 24h
    window the ``now - 25h`` row falls outside the cutoff and is deleted
    while the other two are kept.
    """
    db_path = tmp_path / "test_prune_delete.db"
    _create_table_with_timestamp(db_path, "metrics")

    now = time.time()
    _insert_row(db_path, "metrics", now)             # recent — keep
    _insert_row(db_path, "metrics", now - 5 * 3600)  # 5 h ago — keep
    _insert_row(db_path, "metrics", now - 25 * 3600)  # 25 h ago — DELETE

    assert _count_rows(db_path, "metrics") == 3

    deleted = prune_old_data("metrics", max_age_hours=24, db_path=db_path)

    # Exactly one row was deleted (the 25 h-old one).
    assert deleted == 1
    assert _count_rows(db_path, "metrics") == 2

    # The remaining rows are the two recent ones (sanity: fetch + check).
    with sqlite3.connect(db_path) as conn:
        rows = sorted(r[0] for r in conn.execute("SELECT timestamp FROM metrics"))
    assert rows == sorted([now, now - 5 * 3600])


# ── 2. prune_old_data keeps recent rows ─────────────────────────────────────

def test_prune_old_data_keeps_recent_rows(tmp_path):
    """``prune_old_data`` must NOT delete rows whose ``timestamp`` is inside
    the ``max_age_hours`` window — even when the window is short (e.g.
    1 hour) and the rows span the full breadth of it.

    Setup: 4 rows all inside the last hour (``now``, ``now - 5m``,
    ``now - 30m``, ``now - 59m``). With a 1-hour window NONE should be
    deleted. A 5 h-old row at the end proves the deletion path actually
    fires (so the test is not a vacuous "deleted=0 because nothing was
    deletable").
    """
    db_path = tmp_path / "test_prune_keep.db"
    _create_table_with_timestamp(db_path, "metrics")

    now = time.time()
    recent_timestamps = [
        now,                       # exactly now
        now - 5 * 60,              # 5 min ago
        now - 30 * 60,             # 30 min ago
        now - 59 * 60,             # 59 min ago — still inside 1 h window
    ]
    for ts in recent_timestamps:
        _insert_row(db_path, "metrics", ts)
    # One clearly-old row to prove the deletion path works.
    _insert_row(db_path, "metrics", now - 5 * 3600)  # 5 h ago — DELETE

    assert _count_rows(db_path, "metrics") == 5

    deleted = prune_old_data("metrics", max_age_hours=1, db_path=db_path)

    # Only the 5 h-old row was deleted; every recent row survived.
    assert deleted == 1
    assert _count_rows(db_path, "metrics") == 4

    # The four kept rows are exactly the four recent timestamps.
    with sqlite3.connect(db_path) as conn:
        kept = sorted(r[0] for r in conn.execute("SELECT timestamp FROM metrics"))
    assert kept == sorted(recent_timestamps)


# ── 3. prune_observability uses the 7-day window ────────────────────────────

def test_prune_observability_uses_seven_day_window(monkeypatch, tmp_path):
    """``prune_observability()`` (called with no args) must use the module
    default ``OBSERVABILITY_RETENTION_HOURS = 7 * 24 = 168`` hours and
    DELETE every ``metrics`` row older than 7 days.

    Boundary-test setup (anchored at ``now``):
      * row at ``now - 168 h - 1 h``  → 7 d + 1 h ago  → DELETED (outside)
      * row at ``now - 168 h + 1 h``  → 6 d 23 h ago   → KEPT   (inside)
      * row at ``now``                → fresh          → KEPT   (inside)
    """
    # Verify the constant itself — guards against an accidental re-tune.
    assert OBSERVABILITY_RETENTION_HOURS == 7 * 24

    db_path = tmp_path / "test_obs_prune.db"
    monkeypatch.setattr(retention, "OBSERVABILITY_DB_PATH", db_path)
    _create_table_with_timestamp(db_path, "metrics")

    now = time.time()
    window = OBSERVABILITY_RETENTION_HOURS * 3600.0
    ts_outside = now - window - 3600.0    # 1 h past the 7-day cutoff
    ts_inside = now - window + 3600.0     # 1 h before the 7-day cutoff
    ts_fresh = now

    _insert_row(db_path, "metrics", ts_outside)
    _insert_row(db_path, "metrics", ts_inside)
    _insert_row(db_path, "metrics", ts_fresh)

    # Default call — exercises the 7-day default.
    deleted = prune_observability()

    # Exactly the one out-of-window row was deleted.
    assert deleted == 1
    assert _count_rows(db_path, "metrics") == 2

    with sqlite3.connect(db_path) as conn:
        kept = sorted(r[0] for r in conn.execute("SELECT timestamp FROM metrics"))
    assert kept == sorted([ts_inside, ts_fresh])


# ── 4. prune_decision_ledger uses the 30-day window ────────────────────────

def test_prune_decision_ledger_uses_thirty_day_window(monkeypatch, tmp_path):
    """``prune_decision_ledger()`` (called with no args) must use the module
    default ``DECISION_LEDGER_RETENTION_HOURS = 30 * 24 = 720`` hours and
    DELETE every row older than 30 days from BOTH ``decision_events``
    AND ``decision_rejections`` (the prune walks both tables and returns
    the sum).

    Boundary-test setup per table:
      * row at ``now - 720 h - 1 h``  → 30 d + 1 h ago  → DELETED
      * row at ``now - 720 h + 1 h``  → 29 d 23 h ago   → KEPT
      * row at ``now``                → fresh          → KEPT
    """
    # Verify the constant itself — guards against an accidental re-tune.
    assert DECISION_LEDGER_RETENTION_HOURS == 30 * 24

    db_path = tmp_path / "test_dl_prune.db"
    monkeypatch.setattr(retention, "DECISION_LEDGER_DB_PATH", db_path)
    # The decision-ledger prune walks BOTH tables — create both.
    _create_table_with_timestamp(db_path, "decision_events")
    _create_table_with_timestamp(db_path, "decision_rejections")

    now = time.time()
    window = DECISION_LEDGER_RETENTION_HOURS * 3600.0
    ts_outside = now - window - 3600.0
    ts_inside = now - window + 3600.0
    ts_fresh = now

    # Three rows per table.
    for ts in (ts_outside, ts_inside, ts_fresh):
        _insert_row(db_path, "decision_events", ts)
        _insert_row(db_path, "decision_rejections", ts)

    assert _count_rows(db_path, "decision_events") == 3
    assert _count_rows(db_path, "decision_rejections") == 3

    # Default call — exercises the 30-day default on BOTH tables.
    deleted = prune_decision_ledger()

    # One out-of-window row per table → 2 total deleted.
    assert deleted == 2
    assert _count_rows(db_path, "decision_events") == 2
    assert _count_rows(db_path, "decision_rejections") == 2

    # Each table kept exactly the two in-window rows.
    for table in ("decision_events", "decision_rejections"):
        with sqlite3.connect(db_path) as conn:
            kept = sorted(r[0] for r in conn.execute(f"SELECT timestamp FROM {table}"))
        assert kept == sorted([ts_inside, ts_fresh])


# ── 5. prune_audit_events uses the 90-day window ───────────────────────────

def test_prune_audit_events_uses_ninety_day_window(monkeypatch, tmp_path):
    """``prune_audit_events()`` (called with no args) must use the module
    default ``AUDIT_EVENTS_RETENTION_HOURS = 90 * 24 = 2160`` hours and
    DELETE every ``audit_events`` row older than 90 days.

    Boundary-test setup (anchored at ``now``):
      * row at ``now - 2160 h - 1 h``  → 90 d + 1 h ago  → DELETED
      * row at ``now - 2160 h + 1 h``  → 89 d 23 h ago   → KEPT
      * row at ``now``                 → fresh           → KEPT
    """
    # Verify the constant itself — guards against an accidental re-tune.
    assert AUDIT_EVENTS_RETENTION_HOURS == 90 * 24

    db_path = tmp_path / "test_audit_prune.db"
    monkeypatch.setattr(retention, "AUDIT_DB_PATH", db_path)
    _create_table_with_timestamp(db_path, "audit_events")

    now = time.time()
    window = AUDIT_EVENTS_RETENTION_HOURS * 3600.0
    ts_outside = now - window - 3600.0
    ts_inside = now - window + 3600.0
    ts_fresh = now

    _insert_row(db_path, "audit_events", ts_outside)
    _insert_row(db_path, "audit_events", ts_inside)
    _insert_row(db_path, "audit_events", ts_fresh)

    # Default call — exercises the 90-day default.
    deleted = prune_audit_events()

    # Exactly the one out-of-window row was deleted.
    assert deleted == 1
    assert _count_rows(db_path, "audit_events") == 2

    with sqlite3.connect(db_path) as conn:
        kept = sorted(r[0] for r in conn.execute("SELECT timestamp FROM audit_events"))
    assert kept == sorted([ts_inside, ts_fresh])


# ── 6. run_all_pruning returns a structured summary ─────────────────────────

def test_run_all_pruning_returns_summary(monkeypatch, tmp_path):
    """``run_all_pruning()`` must return a structured summary dict shaped::

        {
          "timestamp": float,
          "results": {
            "observability":     {"pruned": int, "max_age_hours": float,
                                  "db_path": str, "error": str | None},
            "decision_ledger":   { ... },
            "execution_quality": { ... },
            "audit_events":      { ... },
          },
          "total_pruned": int,
          "success": bool,
        }

    Setup: redirect all four DB paths to ``tmp_path``-scoped SQLite
    files. Seed each store with one in-window row + one out-of-window
    row, then call ``run_all_pruning()`` and verify:
      * every store's ``pruned`` count is 1,
      * ``total_pruned`` is 4 (1 per store, with decision_ledger
        contributing the SUM of its two tables' deletions → 2 rows from
        that store alone),
      * ``success`` is True (no errors),
      * each ``results[<store>]["error"]`` is None,
      * each ``results[<store>]["max_age_hours"]`` matches the canonical
        retention constant,
      * each ``results[<store>]["db_path"]`` is the str of the path we
        monkeypatched in.
    """
    obs_db = tmp_path / "summary_obs.db"
    dl_db = tmp_path / "summary_dl.db"
    eq_db = tmp_path / "summary_eq.db"
    audit_db = tmp_path / "summary_audit.db"

    monkeypatch.setattr(retention, "OBSERVABILITY_DB_PATH", obs_db)
    monkeypatch.setattr(retention, "DECISION_LEDGER_DB_PATH", dl_db)
    monkeypatch.setattr(retention, "EXECUTION_QUALITY_DB_PATH", eq_db)
    monkeypatch.setattr(retention, "AUDIT_DB_PATH", audit_db)

    # Create + seed each store. Each store gets one row that's clearly
    # outside its retention window (well past the cutoff) plus one fresh
    # row. The decision_ledger prune walks BOTH its tables, so it gets
    # one outside + one inside on EACH table → 2 deletions from that
    # store alone (total = 1 + 2 + 1 + 1 = 5).
    now = time.time()

    _create_table_with_timestamp(obs_db, "metrics")
    _insert_row(obs_db, "metrics", now)
    _insert_row(obs_db, "metrics", now - (OBSERVABILITY_RETENTION_HOURS + 24) * 3600)

    _create_table_with_timestamp(dl_db, "decision_events")
    _create_table_with_timestamp(dl_db, "decision_rejections")
    for tbl in ("decision_events", "decision_rejections"):
        _insert_row(dl_db, tbl, now)
        _insert_row(
            dl_db, tbl,
            now - (DECISION_LEDGER_RETENTION_HOURS + 24) * 3600,
        )

    _create_table_with_timestamp(eq_db, "execution_quality")
    _insert_row(eq_db, "execution_quality", now)
    _insert_row(
        eq_db, "execution_quality",
        now - (retention.EXECUTION_QUALITY_RETENTION_HOURS + 24) * 3600,
    )

    _create_table_with_timestamp(audit_db, "audit_events")
    _insert_row(audit_db, "audit_events", now)
    _insert_row(
        audit_db, "audit_events",
        now - (AUDIT_EVENTS_RETENTION_HOURS + 24) * 3600,
    )

    started_before = time.time()
    summary = run_all_pruning()
    started_after = time.time()

    # ── Top-level shape ───────────────────────────────────────────────
    assert isinstance(summary, dict)
    assert set(summary.keys()) == {"timestamp", "results", "total_pruned", "success"}

    # timestamp is a fresh epoch second (bounded by the call window).
    assert isinstance(summary["timestamp"], float)
    assert started_before - 5.0 <= summary["timestamp"] <= started_after + 5.0

    # results carries exactly the four canonical stores.
    assert set(summary["results"].keys()) == {
        "observability",
        "decision_ledger",
        "execution_quality",
        "audit_events",
    }

    # ── Per-store entry shape ─────────────────────────────────────────
    expected_paths = {
        "observability": obs_db,
        "decision_ledger": dl_db,
        "execution_quality": eq_db,
        "audit_events": audit_db,
    }
    expected_ages = {
        "observability": OBSERVABILITY_RETENTION_HOURS,
        "decision_ledger": DECISION_LEDGER_RETENTION_HOURS,
        "execution_quality": retention.EXECUTION_QUALITY_RETENTION_HOURS,
        "audit_events": AUDIT_EVENTS_RETENTION_HOURS,
    }
    expected_pruned = {
        "observability": 1,       # metrics: 1 outside row
        "decision_ledger": 2,     # decision_events (1) + decision_rejections (1)
        "execution_quality": 1,   # execution_quality: 1 outside row
        "audit_events": 1,        # audit_events: 1 outside row
    }
    for store_name in expected_paths:
        entry = summary["results"][store_name]
        assert set(entry.keys()) == {
            "pruned", "max_age_hours", "db_path", "error",
        }, f"unexpected keys for {store_name}: {set(entry.keys())}"
        assert entry["pruned"] == expected_pruned[store_name], (
            f"{store_name}: expected {expected_pruned[store_name]} pruned, "
            f"got {entry['pruned']}"
        )
        assert entry["max_age_hours"] == float(expected_ages[store_name])
        assert entry["db_path"] == str(expected_paths[store_name])
        assert entry["error"] is None, (
            f"{store_name}: expected no error, got {entry['error']!r}"
        )

    # ── Aggregates ────────────────────────────────────────────────────
    # 1 (obs) + 2 (dl) + 1 (eq) + 1 (audit) = 5.
    assert summary["total_pruned"] == 5
    assert summary["success"] is True


# ── 7. invalid table name is rejected (SQL-injection guard) ────────────────

@pytest.mark.parametrize(
    "bad_table",
    [
        # ── Classic SQL-injection vector ──────────────────────────────
        "metrics; DROP TABLE users;--",
        # ── Inline comment variants ───────────────────────────────────
        "metrics --",
        "metrics/*foo*/",
        # ── Statement-terminator variants ──────────────────────────────
        "metrics;",
        "metrics\0",
        # ── Non-identifier characters ──────────────────────────────────
        "metrics(bad)",
        "metrics.bak",
        "metrics col",
        "with space",
        "has-dash",
        # ── Must start with a letter or underscore ─────────────────────
        "9starts_with_digit",
        "",
        # ── Quote-injection vector ─────────────────────────────────────
        "' OR '1'='1",
        '" OR "1"="1',
    ],
)
def test_prune_old_data_rejects_invalid_table_name(bad_table, tmp_path):
    """``prune_old_data`` must reject any table name that doesn't match
    ``^[A-Za-z_][A-Za-z0-9_]*$`` with a ``ValueError``.

    SQLite cannot parameterise identifiers — a table name passed to
    ``f"DELETE FROM {table} WHERE timestamp < ?"`` is interpolated
    verbatim into the SQL string. The only defence against SQL injection
    is the strict-identifier regex gate at the top of ``prune_old_data``.
    A programmer error (bad literal) MUST surface loudly as a
    ``ValueError`` rather than being silently swallowed into a no-op
    (which would mask the bug) or, worse, executing the injected SQL.

    Parametrised over a battery of injection / shape-rejection cases:

      * classic ``"metrics; DROP TABLE users;--"`` injection,
      * SQL comment terminators (``--``, ``/* */``),
      * statement separators (``;``, NUL byte),
      * parenthesised / dotted / spaced / dashed identifiers,
      * a table name starting with a digit,
      * an empty string,
      * single- and double-quote injection vectors.
    """
    db_path = tmp_path / "test_injection.db"
    _create_table_with_timestamp(db_path, "metrics")

    with pytest.raises(ValueError, match="invalid table name"):
        prune_old_data(bad_table, max_age_hours=24, db_path=db_path)

    # Sanity: the metrics table is untouched (the regex gate fired BEFORE
    # any SQL was executed — no destructive side-effect on the DB).
    assert _count_rows(db_path, "metrics") == 0


def test_prune_old_data_rejects_non_string_table_name(tmp_path):
    """``prune_old_data`` must reject a non-string ``table`` argument
    (e.g. ``None``, ``int``, ``list``) with a ``ValueError`` — the
    ``isinstance(table, str)`` half of the regex gate is the first
    defence against a caller that builds the table name from untrusted
    input that hasn't been coerced to a string.
    """
    db_path = tmp_path / "test_non_string.db"
    _create_table_with_timestamp(db_path, "metrics")

    for bad_table in (None, 123, ["metrics"], {"table": "metrics"}, b"metrics"):
        with pytest.raises(ValueError, match="invalid table name"):
            prune_old_data(bad_table, max_age_hours=24, db_path=db_path)

    assert _count_rows(db_path, "metrics") == 0


def test_prune_old_data_rejects_negative_max_age(tmp_path):
    """``prune_old_data`` must reject a negative ``max_age_hours`` with a
    ``ValueError`` — a negative window would invert the cutoff and delete
    every row newer than ``now - (negative) * 3600`` = ``now + |w|*3600``,
    i.e. future rows, which is meaningless. Surfaced loudly as a
    programmer error rather than silently wiping the table.
    """
    db_path = tmp_path / "test_neg_age.db"
    _create_table_with_timestamp(db_path, "metrics")
    _insert_row(db_path, "metrics", time.time())

    with pytest.raises(ValueError, match="max_age_hours must be >= 0"):
        prune_old_data("metrics", max_age_hours=-1, db_path=db_path)

    # No row was deleted (the guard fired before any SQL ran).
    assert _count_rows(db_path, "metrics") == 1
