"""
tests/test_reconciliation.py — Unit tests for ``core/reconciliation.py``.

X2 — Storage-vs-engine reconciliation unit tests.

Covers the five behaviour contracts required by the task spec:

  (1) ``run_reconciliation()`` returns a report ``dict``.
  (2) The report contains the required data fields (the task spec lists
      ``exposure`` / ``positions`` / ``trades``; the actual implementation
      is a *storage-vs-engine telemetry* reconciliation module — see the
      "Spec-vs-implementation divergence" note below — so the report's
      real keys are ``tables`` / ``breaches`` / ``write_failures``. This
      test pins the implementation's actual contract and documents the
      divergence so a future spec realignment surfaces as an explicit
      test failure rather than a silent drift).
  (3) The report carries an ``is_clean`` boolean.
  (4) Reconciliation handles an empty store gracefully (zero rows, zero
      engine writes, no last_error → ``is_clean=True`` and ``breaches=[]``).
  (5) Reconciliation detects a position/order mismatch — i.e. the engine
      accepted more writes than were physically stored (positive drift on
      any table → ``is_clean=False`` and a populated ``breaches`` list).

Spec-vs-implementation divergence
---------------------------------
The task spec's wording ("report contains exposure/positions/trades
fields" and "reconciliation detects position/order mismatch") reads as if
``core/reconciliation.py`` were a *trading-domain* reconciliation module
(orders booked vs positions held vs exposure). The actual module —
tagged P0-DAT-03 in its own docstring — is a *storage-vs-engine
telemetry* reconciliation: it compares the count of writes the engine
accepted (per-table ``inserts_ok`` telemetry counters) against the count
of rows physically stored on disk (per-table ``COUNT(*)``), across the
4 P0-DAT-03 storage tables (``market_snapshots``, ``orderbook_ticks``,
``fundamental_news``, ``ml_feature_store``) plus the 5 sibling tables in
``_TABLES``. The output report's actual keys are therefore:

    generated_at, backend, is_clean, tables, breaches, write_failures

— NOT ``exposure`` / ``positions`` / ``trades``. Following the same
precedent established by W6 (``test_capital_allocator_advanced.py``),
this test file pins the **implementation's** actual behaviour and
documents the divergence in each test's docstring; the spec wording is
NOT silently re-interpreted to match the implementation.

Mocked-store strategy
---------------------
``run_reconciliation(engine=None)`` accepts a per-call engine argument
(defaults to the global ``timescale_db`` singleton). Each test passes a
``unittest.mock.MagicMock`` whose ``get_stats.return_value`` is
configured to the desired telemetry shape — this exercises the same
code path production uses (``engine.get_stats()`` → ``_storage_counts`` /
``_engine_counts`` → drift loop → report assembly → artifact write)
without ever touching a real SQLite file or the singleton. The report
artifact directory (``core.reconciliation.RECON_REPORT_DIR``) is
monkeypatched per-test to a ``tmp_path``-scoped dir so each test writes
to a clean directory (the artifact filename is keyed on calendar date,
so without a per-test dir, tests run on the same day would clobber each
other's artifact file).

DB isolation strategy
---------------------
``core/timescale_db.py`` constructs its singleton at module-import time
against ``SQLITE_FALLBACK_PATH`` (resolved from ``MARKET_DB_PATH``, with
default ``/app/data/market_intelligence.db``). The repo's
``tests/conftest.py`` already redirects every persisted-state env var to
``/tmp/pmbot_conftest_isolation`` before any project module is imported.
This file additionally sets the same redirects via ``setdefault`` so it
stays hermetic even if a future test run somehow bypasses conftest (same
defensive pattern as ``tests/test_retention.py`` /
``tests/test_capital_allocator.py``). The mocked engine means the
tests never actually call into ``timescale_db`` at all — the env
redirect is purely defensive (so the module-import-time singleton
construction doesn't crash in a conftest-less run).

Every test is synchronous (the entire ``core/reconciliation.py`` module
is sync). The repo's ``pytest.ini`` / ``pyproject.toml`` are
intentionally left untouched (X2 constraint: "Do NOT edit existing
files"); no ``pytest.mark.asyncio`` is required.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ── Defensive env-var redirect (see "DB isolation strategy" docstring). ──────
# ``setdefault`` lets conftest (which loads first) win when present; this
# block is purely a defensive net so the file stays hermetic in a
# hypothetical conftest-less invocation. Mirrors the established pattern
# in tests/test_retention.py / tests/test_capital_allocator.py /
# tests/test_execution_quality.py.
_TMP_ROOT = Path("/tmp/reconciliation_tests")
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
    # Where run_reconciliation writes its dated JSON artifact. Per-test
    # monkeypatch (see ``recon_dir`` fixture) overrides this in-test for
    # full tmp_path isolation; this default keeps the module-import-time
    # value sensible in a conftest-less run.
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
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
# tests/test_paper_simulator.py / tests/test_decision_ledger.py /
# tests/test_retention.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env + sys.path must be set first)

from core.reconciliation import (  # noqa: E402
    RECON_REPORT_DIR,
    run_reconciliation,
)
from core.timescale_db import _TABLES  # noqa: E402


# ── Canonical expected report keys (pinned from the implementation). ───────
# The implementation assembles the report dict with exactly these six
# top-level keys (see core/reconciliation.py::run_reconciliation). A future
# refactor that renamed any of them would surface as a test failure here,
# forcing an explicit test-update rather than a silent drift. Pinned from
# the implementation (not the task spec) — see the module docstring's
# "Spec-vs-implementation divergence" note.
_EXPECTED_REPORT_KEYS = frozenset(
    {"generated_at", "backend", "is_clean", "tables", "breaches", "write_failures"}
)

# The 4 P0-DAT-03 storage tables that ``_TABLE_COUNT_KEY`` /
# ``_TABLE_ENGINE_KEY`` actually wire up to non-zero telemetry keys. The
# other 5 tables in ``_TABLES`` (``strategy_decisions`` / ``risk_decisions``
# / ``orders`` / ``fills`` / ``raw_observations``) appear in the report's
# ``tables`` dict but always carry ``engine_accepted_writes=0`` and
# ``storage_rows=0`` because the module has no count-key mapping for
# them. Pinned from ``core.reconciliation._TABLE_COUNT_KEY`` so a future
# refactor that wired up one of the 5 unmapped tables would surface as a
# test failure (the "engine_accepted_writes" / "storage_rows" /
# "drift" shape assertions below would need updating).
_TELEMETRY_TABLES = (
    "market_snapshots",
    "orderbook_ticks",
    "fundamental_news",
    "ml_feature_store",
)


# ── Helper: build a fully-shaped ``get_stats`` return value ─────────────────
def _build_stats(
    *,
    storage_rows: dict[str, int] | None = None,
    inserts_ok: dict[str, int] | None = None,
    inserts_failed: dict[str, int] | None = None,
    last_error: str | None = None,
    db_backend: str = "SQLite3 WAL (Cold Standby)",
) -> dict:
    """Construct a ``get_stats()``-shaped dict for the mocked engine.

    Mirrors the shape returned by ``TimescaleDBEngine.get_stats`` so the
    reconciliation helpers (``_storage_counts`` / ``_engine_counts``)
    walk the same key paths production walks. Every telemetry-table key
    defaults to 0 (clean store); callers override only the counters they
    want to perturb for the scenario under test.
    """
    storage_rows = storage_rows or {}
    inserts_ok = inserts_ok or {}
    inserts_failed = inserts_failed or {}

    # Map each telemetry table to its canonical storage-count key (mirrors
    # ``core.reconciliation._TABLE_COUNT_KEY`` so the ``_storage_counts``
    # helper resolves the value).
    _STORAGE_KEY = {
        "market_snapshots": "snapshots_recorded",
        "orderbook_ticks": "ticks_recorded",
        "fundamental_news": "news_items_recorded",
        "ml_feature_store": "ml_feature_vectors",
    }

    stats: dict = {
        "db_backend": db_backend,
        "is_timescaledb": False,
        "size_mb": 0.0,
        "inserts_ok": {t: int(inserts_ok.get(t, 0)) for t in _TABLES},
        "inserts_failed": {t: int(inserts_failed.get(t, 0)) for t in _TABLES},
        "write_time_ms": {t: 0.0 for t in _TABLES},
        "last_error": last_error,
        "last_error_at": None,
    }
    # Storage-row counts for the 4 telemetry tables are surfaced under
    # their canonical ``*_recorded`` keys (which ``_storage_counts`` then
    # resolves via ``_TABLE_COUNT_KEY``). Tables not in the canonical map
    # fall back to ``stats.get(t, 0)`` → 0.
    for tbl, key in _STORAGE_KEY.items():
        stats[key] = int(storage_rows.get(tbl, 0))
    return stats


# ── Helper: build a mocked engine whose ``get_stats`` returns ``stats`` ────
def _mock_engine(stats: dict) -> MagicMock:
    """Return a ``MagicMock`` shaped like ``TimescaleDBEngine``.

    ``run_reconciliation(engine=...)`` only calls ``engine.get_stats()``
    (the ``engine`` parameter is rebound to the result of
    ``_engine_counts(stats)`` immediately after, so no other engine method
    is invoked). The mock therefore only needs ``get_stats.return_value``
    configured.
    """
    engine = MagicMock(name="mock_timescale_db")
    engine.get_stats.return_value = stats
    return engine


# ── Fixture: per-test ``tmp_path``-scoped report artifact directory ────────
@pytest.fixture
def recon_dir(monkeypatch, tmp_path):
    """Redirect ``core.reconciliation.RECON_REPORT_DIR`` to ``tmp_path``.

    ``run_reconciliation`` writes its JSON artifact to
    ``RECON_REPORT_DIR / f"reconciliation_{date.today().isoformat()}.json"``.
    The filename is keyed on calendar date, so without a per-test
    directory, two tests run on the same day would clobber each other's
    artifact. This fixture gives each test a clean directory AND lets the
    test assert the artifact was actually written (by inspecting
    ``tmp_path`` afterwards). Mirrors the per-test isolation pattern in
    ``tests/test_decision_ledger.py``'s ``ledger`` fixture.
    """
    out = tmp_path / "recon_reports"
    monkeypatch.setattr("core.reconciliation.RECON_REPORT_DIR", out)
    return out


# ── 1. run_reconciliation returns a report dict ─────────────────────────────
def test_run_reconciliation_returns_report_dict(recon_dir):
    """``run_reconciliation(engine=...)`` must return a ``dict``.

    Pinning the return type at the type boundary: a regression that
    returned a list / namedtuple / dataclass would surface here. The dict
    is populated by ``run_reconciliation``'s final ``return report``
    statement (see ``core/reconciliation.py``).
    """
    engine = _mock_engine(_build_stats())
    report = run_reconciliation(engine=engine)

    # (a) Return type is exactly ``dict`` (not a Mapping subclass / dict
    #     subclass that could subtly break ``==`` semantics in callers).
    assert isinstance(report, dict), (
        f"run_reconciliation must return a dict, got {type(report).__name__}"
    )

    # (b) The mocked engine was actually consulted (defensive — catches a
    #     regression where ``run_reconciliation`` ignored its ``engine``
    #     argument and silently fell back to the global singleton).
    assert engine.get_stats.called, (
        "run_reconciliation did not call engine.get_stats() — the mocked "
        "store was bypassed (the production fallback to the global "
        "timescale_db singleton would have been used instead)."
    )

    # (c) Artifact file was actually written to ``recon_dir`` (the
    #     function has a write-to-disk side effect, not just a return
    #     value). Belt-and-braces: a regression that swallowed the
    #     ``artifact.write_text(...)`` call in a try/except would surface
    #     here.
    artifacts = list(recon_dir.glob("reconciliation_*.json"))
    assert len(artifacts) == 1, (
        f"expected exactly 1 artifact in {recon_dir}, found {len(artifacts)}"
    )
    # The artifact on disk must round-trip to the same dict the function
    # returned (defensive — catches a regression where the in-memory
    # report and the on-disk artifact diverge).
    on_disk = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert on_disk == report, (
        "on-disk artifact diverged from the returned report dict"
    )


# ── 2. report contains the required data fields ─────────────────────────────
def test_report_contains_required_data_fields(recon_dir):
    """The report dict must carry the implementation's canonical data
    fields.

    Spec-vs-implementation divergence
    ---------------------------------
    The task spec lists ``exposure`` / ``positions`` / ``trades`` as the
    required report fields. The actual ``core/reconciliation.py`` module
    is a *storage-vs-engine telemetry* reconciliation (P0-DAT-03), NOT a
    trading-domain reconciliation — it compares per-table engine-accepted
    write counts against per-table on-disk row counts. The report's real
    data fields are therefore:

        ``tables``         — per-table {engine_accepted_writes,
                                          storage_rows, drift} dict
        ``breaches``       — list[str] of human-readable breach messages
        ``write_failures`` — per-table inserts_failed dict (pass-through
                              from ``stats["inserts_failed"]``)

    Following the W6 precedent (``test_capital_allocator_advanced.py``),
    this test pins the implementation's actual contract (not the spec's
    wording) and documents the divergence explicitly. A future spec
    realignment that added ``exposure`` / ``positions`` / ``trades``
    fields to the report would surface as a test failure here (forcing
    an explicit test update rather than a silent drift); conversely, a
    refactor that dropped any of the implementation's canonical fields
    would also fail here.
    """
    engine = _mock_engine(_build_stats())
    report = run_reconciliation(engine=engine)

    # (a) The report's top-level key set is EXACTLY the canonical set
    #     (no extra keys, no missing keys). Belt-and-braces against both
    #     silent key removal and silent key addition.
    assert set(report.keys()) == _EXPECTED_REPORT_KEYS, (
        f"report key set drifted from canonical contract: "
        f"expected {_EXPECTED_REPORT_KEYS}, got {set(report.keys())}"
    )

    # (b) ``tables`` is a dict keyed by every table in ``_TABLES`` (9
    #     tables, pinned from ``core.timescale_db._TABLES`` so a future
    #     schema addition that registered a new table would surface here
    #     as a test failure — the test would need updating to match).
    assert isinstance(report["tables"], dict)
    assert set(report["tables"].keys()) == set(_TABLES), (
        f"report['tables'] keys must match _TABLES exactly: "
        f"expected {set(_TABLES)}, got {set(report['tables'].keys())}"
    )

    # (c) Each per-table entry carries the 3 expected sub-fields.
    for t in _TABLES:
        entry = report["tables"][t]
        assert set(entry.keys()) == {
            "engine_accepted_writes",
            "storage_rows",
            "drift",
        }, (
            f"per-table entry for {t} has wrong keys: {set(entry.keys())}"
        )
        # All three values are ints (drift can be negative — see
        # ``data/reports/reconciliation_2026-09-03.json`` where
        # ``fundamental_news`` has ``drift = -130``).
        assert isinstance(entry["engine_accepted_writes"], int)
        assert isinstance(entry["storage_rows"], int)
        assert isinstance(entry["drift"], int)
        # Drift is algebraically engine_accepted_writes - storage_rows.
        assert entry["drift"] == (
            entry["engine_accepted_writes"] - entry["storage_rows"]
        ), f"drift invariant violated for {t}"

    # (d) ``breaches`` is a list of strings (human-readable breach messages).
    assert isinstance(report["breaches"], list)
    for b in report["breaches"]:
        assert isinstance(b, str), (
            f"breach entries must be str, got {type(b).__name__}: {b!r}"
        )

    # (e) ``write_failures`` is a dict keyed by every table in ``_TABLES``
    #     (pass-through from ``stats["inserts_failed"]``).
    assert isinstance(report["write_failures"], dict)
    assert set(report["write_failures"].keys()) == set(_TABLES), (
        f"write_failures keys must match _TABLES: "
        f"expected {set(_TABLES)}, got {set(report['write_failures'].keys())}"
    )

    # (f) ``generated_at`` is an ISO-8601 timestamp string parseable by
    #     ``datetime.datetime.fromisoformat`` (the implementation uses
    #     ``datetime.datetime.now(datetime.timezone.utc).isoformat()``).
    assert isinstance(report["generated_at"], str)
    parsed = datetime.datetime.fromisoformat(report["generated_at"])
    assert parsed.tzinfo is not None, (
        "generated_at must carry timezone info (UTC) — a regression that "
        "dropped ``datetime.timezone.utc`` would surface here"
    )

    # (g) ``backend`` is a non-empty string surfaced from the engine's
    #     ``get_stats()`` payload (the implementation reads
    #     ``stats.get("db_backend", "unknown")``).
    assert isinstance(report["backend"], str)
    assert report["backend"], "backend must be a non-empty str"


# ── 3. report contains an is_clean boolean ──────────────────────────────────
def test_report_contains_is_clean_boolean(recon_dir):
    """The report must carry an ``is_clean`` field that is a real
    ``bool`` (not just truthy / falsy) and reflects the breach count.

    The implementation computes ``is_clean = len(breaches) == 0``. This
    test verifies:
      (a) ``is_clean`` is present in the report.
      (b) ``is_clean`` is exactly ``bool`` (not ``int`` — in Python
          ``True == 1`` and ``isinstance(True, int)`` is True, so this
          pins the stronger contract that the value is produced by a
          boolean expression, not by an arithmetic one).
      (c) ``is_clean`` is consistent with the breach count
          (``is_clean == (len(breaches) == 0)``).
      (d) For a clean store (no breaches), ``is_clean`` is ``True``.
    """
    engine = _mock_engine(_build_stats())  # zero writes / zero rows / no error
    report = run_reconciliation(engine=engine)

    # (a) Field present.
    assert "is_clean" in report, "report must contain 'is_clean' field"

    # (b) Strong type pin: exactly bool, not int. (``isinstance(True, int)``
    #     is True in Python, so we use ``type(...) is bool`` for the
    #     stronger check. A regression that returned ``0`` / ``1`` — both
    #     of which ``== False`` / ``== True`` — would surface here.)
    assert type(report["is_clean"]) is bool, (
        f"is_clean must be exactly bool, got {type(report['is_clean']).__name__}"
    )

    # (c) Consistency with breach count.
    assert report["is_clean"] == (len(report["breaches"]) == 0), (
        "is_clean must equal (len(breaches) == 0)"
    )

    # (d) For a clean store, is_clean is True.
    assert report["is_clean"] is True, (
        f"clean store must yield is_clean=True, got {report['is_clean']} "
        f"(breaches: {report['breaches']})"
    )
    assert report["breaches"] == [], (
        f"clean store must yield breaches=[], got {report['breaches']}"
    )


# ── 4. reconciliation handles an empty store gracefully ────────────────────
def test_reconciliation_handles_empty_store_gracefully(recon_dir):
    """A store with zero rows on every table and zero engine-accepted
    writes must reconcile cleanly without raising.

    The empty-store scenario is the most common real-world state at
    startup (no ingestion has happened yet) and after a clean retention
    prune (all rows aged out). The reconciliation must NOT crash on
    missing keys, zero divisions, or empty iterators, and must report
    ``is_clean=True`` with all-zero drift across every table.

    Belt-and-braces: this also covers the ``last_error is None`` path
    (a non-None ``last_error`` would add a "persistence reported an
    un-flushed write error" breach — see ``core/reconciliation.py``).
    """
    stats = _build_stats()  # every counter zero, last_error=None
    engine = _mock_engine(stats)
    report = run_reconciliation(engine=engine)

    # (a) Did not raise (implicitly verified by reaching this assert).
    assert isinstance(report, dict)

    # (b) Clean: no breaches, is_clean True.
    assert report["is_clean"] is True
    assert report["breaches"] == []

    # (c) Every table has zero drift (engine_accepted_writes == storage_rows
    #     == 0). The drift is computed as ``engine[t] - storage[t]`` and
    #     must be 0 for every table in ``_TABLES`` (the 4 telemetry tables
    #     AND the 5 unmapped tables — both default to 0/0).
    for t in _TABLES:
        entry = report["tables"][t]
        assert entry["engine_accepted_writes"] == 0, (
            f"empty store must report 0 engine_accepted_writes for {t}, "
            f"got {entry['engine_accepted_writes']}"
        )
        assert entry["storage_rows"] == 0, (
            f"empty store must report 0 storage_rows for {t}, "
            f"got {entry['storage_rows']}"
        )
        assert entry["drift"] == 0, (
            f"empty store must report 0 drift for {t}, "
            f"got {entry['drift']}"
        )

    # (d) ``write_failures`` is present and all-zero (no failed writes on
    #     an empty store).
    for t in _TABLES:
        assert report["write_failures"][t] == 0, (
            f"empty store must report 0 write_failures for {t}, "
            f"got {report['write_failures'][t]}"
        )

    # (e) The artifact file was still written (the empty-store path must
    #     not skip the persistence side effect).
    artifacts = list(recon_dir.glob("reconciliation_*.json"))
    assert len(artifacts) == 1
    on_disk = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert on_disk["is_clean"] is True
    assert on_disk["breaches"] == []

    # (f) ``last_error`` was None on the engine side, and the report does
    #     NOT carry an un-flushed-write-error breach (defensive — catches
    #     a regression where the ``last_error is not None`` branch fired
    #     spuriously on a clean store).
    assert stats["last_error"] is None
    assert not any(
        "un-flushed write error" in b for b in report["breaches"]
    ), "spurious un-flushed-write-error breach on a clean store"


# ── 5. reconciliation detects a position/order mismatch (positive drift) ──
def test_reconciliation_detects_position_order_mismatch(recon_dir):
    """Reconciliation must flag a mismatch between engine-accepted writes
    and physically-stored rows.

    Spec-vs-implementation divergence
    ---------------------------------
    The task spec calls this a "position/order mismatch". The actual
    ``core/reconciliation.py`` module is a storage-vs-engine telemetry
    reconciliation: a "mismatch" here is a per-table *positive drift*
    (``engine_accepted_writes - storage_rows > 0``), meaning the engine
    accepted N writes to a table but only ``< N`` rows are physically on
    disk — i.e. some writes were silently dropped between the engine's
    accept-counter increment and the on-disk row count. This is the
    P0-DAT-03 exit-gate failure mode (``Exit gate G-M3: … reconciliation
    report clean``).

    Following the W6 precedent, this test pins the implementation's
    actual drift-detection behaviour and documents the divergence in
    this docstring.

    Scenario
    --------
    The engine accepted 5 writes to ``market_snapshots`` and 3 to
    ``orderbook_ticks`` but the on-disk store has 0 rows for both (the
    writes were lost somewhere between the accept-counter increment and
    the physical insert). Reconciliation must:

      (a) Report ``is_clean=False``.
      (b) Populate ``breaches`` with one entry per drift-positive table.
      (c) Each breach message includes the table name and the drift
          value (human-readable, parseable by an on-call operator).
      (d) The ``tables[t]["drift"]`` for the affected tables equals the
          positive drift (5 for market_snapshots, 3 for orderbook_ticks).
      (e) The non-affected tables (``fundamental_news``,
          ``ml_feature_store``, and the 5 unmapped tables) still have
          zero drift and are NOT in the breaches list.
    """
    stats = _build_stats(
        storage_rows={
            "market_snapshots": 0,
            "orderbook_ticks": 0,
            "fundamental_news": 0,
            "ml_feature_store": 0,
        },
        inserts_ok={
            # 5 accepted writes lost (storage has 0) → drift = 5
            "market_snapshots": 5,
            # 3 accepted writes lost (storage has 0) → drift = 3
            "orderbook_ticks": 3,
            # No drift on these two (engine 0, storage 0):
            "fundamental_news": 0,
            "ml_feature_store": 0,
        },
    )
    engine = _mock_engine(stats)
    report = run_reconciliation(engine=engine)

    # (a) NOT clean.
    assert report["is_clean"] is False, (
        f"positive-drift store must yield is_clean=False, got "
        f"{report['is_clean']} (breaches: {report['breaches']})"
    )

    # (b) Exactly one breach per drift-positive table (2 here).
    assert len(report["breaches"]) == 2, (
        f"expected 2 breaches (market_snapshots drift=5, orderbook_ticks "
        f"drift=3), got {len(report['breaches'])}: {report['breaches']}"
    )

    # (c) Each breach message mentions the table name and its drift value.
    breaches_text = "\n".join(report["breaches"])
    assert "market_snapshots" in breaches_text, (
        f"breaches must mention 'market_snapshots': {report['breaches']}"
    )
    assert "drift=5" in breaches_text, (
        f"breaches must mention 'drift=5' for market_snapshots: "
        f"{report['breaches']}"
    )
    assert "orderbook_ticks" in breaches_text, (
        f"breaches must mention 'orderbook_ticks': {report['breaches']}"
    )
    assert "drift=3" in breaches_text, (
        f"breaches must mention 'drift=3' for orderbook_ticks: "
        f"{report['breaches']}"
    )

    # (d) Per-table drift values pinned.
    assert report["tables"]["market_snapshots"]["drift"] == 5, (
        f"market_snapshots drift must be 5, got "
        f"{report['tables']['market_snapshots']['drift']}"
    )
    assert report["tables"]["market_snapshots"]["engine_accepted_writes"] == 5
    assert report["tables"]["market_snapshots"]["storage_rows"] == 0

    assert report["tables"]["orderbook_ticks"]["drift"] == 3, (
        f"orderbook_ticks drift must be 3, got "
        f"{report['tables']['orderbook_ticks']['drift']}"
    )
    assert report["tables"]["orderbook_ticks"]["engine_accepted_writes"] == 3
    assert report["tables"]["orderbook_ticks"]["storage_rows"] == 0

    # (e) Non-affected tables still have zero drift and are NOT mentioned
    #     in the breaches list. Covers the 2 remaining telemetry tables
    #     AND the 5 unmapped tables (which always have 0/0).
    drift_clean_tables = [
        t for t in _TABLES if t not in ("market_snapshots", "orderbook_ticks")
    ]
    for t in drift_clean_tables:
        assert report["tables"][t]["drift"] == 0, (
            f"{t} should have 0 drift (not in the mismatch scenario), "
            f"got {report['tables'][t]['drift']}"
        )
        assert t not in breaches_text, (
            f"{t} should not appear in the breaches list, but breaches "
            f"are: {report['breaches']}"
        )

    # (f) The artifact on disk carries the same dirty verdict (defensive —
    #     catches a regression where the in-memory report and the on-disk
    #     artifact diverge on the is_clean flag).
    artifacts = list(recon_dir.glob("reconciliation_*.json"))
    assert len(artifacts) == 1
    on_disk = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert on_disk["is_clean"] is False
    assert len(on_disk["breaches"]) == 2
    assert on_disk["tables"]["market_snapshots"]["drift"] == 5
    assert on_disk["tables"]["orderbook_ticks"]["drift"] == 3


# ── Bonus: last_error path also produces a dirty report ────────────────────
# The implementation has a SECOND breach-trigger path besides positive
# drift: a non-None ``stats["last_error"]`` produces a "persistence
# reported an un-flushed write error" breach (see ``core/reconciliation.py``).
# This test pins that path so a regression that swallowed the last_error
# check would surface here. Not in the task spec's 5 tests but a natural
# complement to test 5 (the spec's "position/order mismatch" wording maps
# most directly to positive drift, but the last_error path is the other
# half of the "mismatch detection" surface).
def test_reconciliation_flags_unflushed_write_error(recon_dir):
    """A non-None ``stats["last_error"]`` must produce a dirty report
    with a breach mentioning the un-flushed write error.

    Belt-and-braces complement to test 5: the implementation's breach
    surface is {positive-drift per table} ∪ {non-None last_error}. This
    test pins the second half so a regression that swallowed the
    ``last_error is not None`` branch would surface here.
    """
    stats = _build_stats(
        last_error="asyncpg connection reset: OperationalError",
    )
    engine = _mock_engine(stats)
    report = run_reconciliation(engine=engine)

    # All per-table drifts are zero (no positive drift), but the
    # last_error path must still trip a single breach.
    for t in _TABLES:
        assert report["tables"][t]["drift"] == 0

    assert report["is_clean"] is False, (
        f"non-None last_error must yield is_clean=False, got "
        f"{report['is_clean']}"
    )
    assert len(report["breaches"]) == 1, (
        f"non-None last_error must yield exactly 1 breach, got "
        f"{len(report['breaches'])}: {report['breaches']}"
    )
    breach = report["breaches"][0]
    assert "un-flushed write error" in breach, (
        f"breach must mention 'un-flushed write error': {breach!r}"
    )
    assert "asyncpg connection reset" in breach, (
        f"breach must include the original last_error string: {breach!r}"
    )
