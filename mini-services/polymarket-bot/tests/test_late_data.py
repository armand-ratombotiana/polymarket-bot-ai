"""tests/test_late_data.py — unit + HTTP tests for the W35-4 late-data
handler + correction log.

Covers the W35-4 ``ingestion.late_data.LateDataHandler`` public contract:

  * ``detect_late_arrival`` — threshold comparison + rolling-rate tracker.
  * ``record_late_arrival`` / ``get_late_arrivals`` — SQLite-backed log.
  * ``record_correction`` / ``get_corrections`` — SQLite-backed correction
    log (what / when / old / new / why).
  * ``is_safe_for_pit`` / ``filter_pit_safe`` — point-in-time safety so
    ML features don't leak late-arriving data into past predictions.

Also covers the two new HTTP routes added to ``api/server.py``:

  * ``GET /api/ingestion/corrections``
  * ``GET /api/ingestion/late-arrivals``

Strategy
--------
Mirrors the isolation discipline in ``tests/test_ingestion_infra.py`` +
``tests/test_ingestion_api.py``:

  * Each handler-level test constructs a fresh ``LateDataHandler(db_path=
    tmp_path / ...)`` instance so the SQLite stores are empty at the
    start of every test — no cross-test pollution.
  * Each API-level test imports the production ``api.server.app`` via
    the shared ``client`` fixture and seeds the module-level singleton
    DIRECTLY via its module's public API so the route's response can be
    asserted against a known state — no mocking of the singleton itself,
    only direct seeding (mirrors ``tests/test_ingestion_api.py``).
  * The autouse ``_reset_late_data_singleton`` fixture clears the
    singleton's in-memory counters + late-rate deque before every test
    AND truncates the on-disk SQLite tables so a prior test's seeded
    rows don't leak into the next test's HTTP response.

Auth enforcement is verified for both routes (401 when the
``Authorization`` header is missing). The ``ingestion`` OpenAPI tag is
verified to cover the new paths.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). Mirrors the pattern in
# ``tests/test_ingestion_infra.py``.
_TMP_ROOT = Path("/tmp/late_data_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# NOTE: ``API_TOKEN`` is intentionally NOT set here. ``tests/conftest.py``
# already sets ``API_TOKEN=test-token-conftest`` via ``setdefault`` BEFORE
# this module is imported, so setting it again here would have no effect
# (the conftest's value wins). The ``_VALID_TOKEN`` constant below matches
# the conftest's token so the auth middleware accepts the request. Mirrors
# the convention in ``tests/test_ingestion_api.py``.
_ENV_REDIRECTS: dict[str, str] = {
    "LATE_DATA_DB_PATH": str(_TMP_ROOT / "late_data.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "MARKET_EVENTS_DB_PATH": str(_TMP_ROOT / "market_events.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*`` / ``core.*`` / ``api.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import
# mode inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — same fix as
# ``tests/test_ingestion_infra.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package — same defensive cache-clear as
# ``tests/test_ingestion_infra.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)
from unittest.mock import MagicMock  # noqa: E402

from ingestion.late_data import (  # noqa: E402
    CORRECTION_REASONS,
    LATE_RATE_ALERT_THRESHOLD,
    LATE_THRESHOLD_S,
    Correction,
    LateArrival,
    LateDataHandler,
)


# ── Shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def handler(tmp_path: Path) -> LateDataHandler:
    """Fresh ``LateDataHandler`` instance backed by a tmp_path DB.

    Alert firing is ENABLED by default; the autouse
    ``_patch_alert_engine`` fixture intercepts ``alert_engine.record_alert``
    so the alert is observable (via the Mock's call_args) but doesn't
    actually persist to SQLite.
    """
    return LateDataHandler(db_path=tmp_path / "late_data.db")


@pytest.fixture(autouse=True)
def _patch_alert_engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``core.alerting.alert_engine.record_alert`` with a Mock.

    Autouse so every test gets a fresh Mock — the handler's
    ``_maybe_fire_alert`` calls ``record_alert`` lazily, so patching
    the function on the ``alert_engine`` singleton (which is constructed
    at module-import time) is the cleanest interception point. Mirrors
    the autouse fixture in ``tests/test_ingestion_infra.py``.
    """
    try:
        from core.alerting import alert_engine
    except Exception:  # pragma: no cover — defensive
        alert_engine = MagicMock()  # type: ignore[assignment]
    mock = MagicMock()
    monkeypatch.setattr(alert_engine, "record_alert", mock)
    return mock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Late-arrival detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectLateArrival:
    """``detect_late_arrival`` threshold comparison + rate tracker."""

    def test_returns_true_when_late(self, handler: LateDataHandler) -> None:
        """A record 60 s late (> 30 s default threshold) returns True."""
        now = time.time()
        assert handler.detect_late_arrival(
            event_time=now - 60.0, ingestion_time=now,
        ) is True

    def test_returns_false_when_on_time(self, handler: LateDataHandler) -> None:
        """A record 5 s late (< 30 s default threshold) returns False."""
        now = time.time()
        assert handler.detect_late_arrival(
            event_time=now - 5.0, ingestion_time=now,
        ) is False

    def test_returns_false_at_exact_threshold(self, handler: LateDataHandler) -> None:
        """``ingestion_time - event_time == threshold`` is NOT late
        (the comparison is strict ``>``, not ``>=``). Mirrors the
        W31-1 ``STALE_REJECT_THRESHOLD_S`` strict-``>`` convention."""
        now = time.time()
        assert handler.detect_late_arrival(
            event_time=now - LATE_THRESHOLD_S, ingestion_time=now,
        ) is False

    def test_returns_false_for_clock_skew(self, handler: LateDataHandler) -> None:
        """A negative lateness (event_time in the future relative to
        ingestion_time — clock skew) is NOT late (a record can't be
        'late' if it arrived before it happened)."""
        now = time.time()
        assert handler.detect_late_arrival(
            event_time=now + 60.0, ingestion_time=now,
        ) is False

    def test_custom_threshold_overrides_default(self, handler: LateDataHandler) -> None:
        """A per-call ``threshold`` overrides the handler's default."""
        now = time.time()
        # 5 s late — below default (30 s) but above custom (1 s).
        assert handler.detect_late_arrival(
            event_time=now - 5.0,
            ingestion_time=now,
            threshold=1.0,
        ) is True

    def test_defaults_ingestion_time_to_now(self, handler: LateDataHandler) -> None:
        """When ``ingestion_time`` is omitted, the handler uses
        ``time.time()`` at call entry — a record with an old
        ``event_time`` is therefore detected as late."""
        # ``event_time`` 60 s in the past, ``ingestion_time`` omitted.
        assert handler.detect_late_arrival(event_time=time.time() - 60.0) is True

    def test_updates_rolling_rate_tracker(self, handler: LateDataHandler) -> None:
        """Each call appends a 1.0 / 0.0 flag to the rolling window so
        ``get_stats()['late_rate']`` reflects the rolling late-rate."""
        now = time.time()
        # 2 late + 2 on-time → 50 % late-rate.
        handler.detect_late_arrival(event_time=now - 60.0, ingestion_time=now)
        handler.detect_late_arrival(event_time=now - 5.0, ingestion_time=now)
        handler.detect_late_arrival(event_time=now - 90.0, ingestion_time=now)
        handler.detect_late_arrival(event_time=now - 2.0, ingestion_time=now)
        stats = handler.get_stats()
        assert stats["late_rate_samples"] == 4
        assert stats["late_rate"] == 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Late-arrival recording
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRecordLateArrival:
    """``record_late_arrival`` / ``get_late_arrivals`` SQLite-backed log."""

    def test_returns_late_id(self, handler: LateDataHandler) -> None:
        """A late record returns a UUID4 hex ``late_id``."""
        now = time.time()
        late_id = handler.record_late_arrival(
            source="clob",
            source_id="trade-1",
            event_type="trade",
            event_time=now - 60.0,
            ingestion_time=now,
            observation_id="obs-1",
        )
        assert isinstance(late_id, str)
        assert len(late_id) == 32  # UUID4 hex (no dashes)
        assert int(late_id, 16) is not None  # valid hex

    def test_returns_empty_string_for_on_time(self, handler: LateDataHandler) -> None:
        """A record below the threshold is NOT logged — returns empty
        string and increments no counter."""
        now = time.time()
        late_id = handler.record_late_arrival(
            source="clob",
            source_id="trade-2",
            event_type="trade",
            event_time=now - 5.0,  # < 30 s threshold
            ingestion_time=now,
        )
        assert late_id == ""
        stats = handler.get_stats()
        assert stats["late_count"] == 0

    def test_persists_to_sqlite_and_round_trips(self, handler: LateDataHandler) -> None:
        """The recorded late arrival is fetchable via ``get_late_arrivals``
        and round-trips every field (including JSON ``metadata``)."""
        now = time.time()
        handler.record_late_arrival(
            source="clob",
            source_id="trade-3",
            event_type="trade",
            event_time=now - 45.0,
            ingestion_time=now,
            observation_id="obs-3",
            token_id="tok-3",
            metadata={"reason": "late fill", "trade_id": "T3"},
        )
        lates = handler.get_late_arrivals(limit=10)
        assert len(lates) == 1
        la = lates[0]
        assert isinstance(la, LateArrival)
        assert la.source == "clob"
        assert la.source_id == "trade-3"
        assert la.event_type == "trade"
        assert la.event_time == pytest.approx(now - 45.0, abs=0.01)
        assert la.ingestion_time == pytest.approx(now, abs=0.01)
        assert la.lateness_seconds == pytest.approx(45.0, abs=0.01)
        assert la.observation_id == "obs-3"
        assert la.token_id == "tok-3"
        assert la.metadata == {"reason": "late fill", "trade_id": "T3"}

    def test_lateness_clamped_at_zero(self, handler: LateDataHandler) -> None:
        """A negative lateness (clock skew) is clamped at 0.0 in the
        stored row — the handler logs the row anyway (the caller
        explicitly asked to record it), but the ``lateness_seconds``
        column is never negative."""
        now = time.time()
        # Force-record with an event_time in the future. Use
        # ``record_late_arrival`` directly with a threshold so the
        # > 0 lateness check passes (we can't get here via the normal
        # path — see ``test_returns_empty_string_for_on_time``).
        # Instead, override the threshold to a NEGATIVE value so the
        # ``lateness > threshold`` check passes for clock-skew input.
        late_id = handler.record_late_arrival(
            source="clob",
            source_id="trade-skew",
            event_type="trade",
            event_time=now + 60.0,  # 60 s in the future (clock skew)
            ingestion_time=now,
            threshold=-100.0,  # negative threshold so any skew is "late"
        )
        assert late_id, "expected a late_id despite clock skew"
        la = handler.get_late_arrivals(limit=1)[0]
        assert la.lateness_seconds == 0.0, (
            f"lateness_seconds must be clamped at 0.0 for clock skew; "
            f"got {la.lateness_seconds}"
        )

    def test_source_filter_narrows_results(self, handler: LateDataHandler) -> None:
        """The ``source`` filter on ``get_late_arrivals`` narrows the
        result set to records from that source only."""
        now = time.time()
        for sid, source in [("t1", "clob"), ("t2", "gamma"), ("t3", "clob")]:
            handler.record_late_arrival(
                source=source,
                source_id=sid,
                event_type="trade",
                event_time=now - 60.0,
                ingestion_time=now,
            )
        clob_lates = handler.get_late_arrivals(limit=10, source="clob")
        assert len(clob_lates) == 2
        assert all(la.source == "clob" for la in clob_lates)

    def test_token_filter_narrows_results(self, handler: LateDataHandler) -> None:
        """The ``token_id`` filter on ``get_late_arrivals`` narrows the
        result set to records for that market only."""
        now = time.time()
        for i, tok in enumerate(["tok-A", "tok-B", "tok-A"]):
            handler.record_late_arrival(
                source="clob",
                source_id=f"t{i}",
                event_type="trade",
                event_time=now - 60.0,
                ingestion_time=now,
                token_id=tok,
            )
        a_lates = handler.get_late_arrivals(limit=10, token_id="tok-A")
        assert len(a_lates) == 2
        assert all(la.token_id == "tok-A" for la in a_lates)

    def test_limit_caps_response_size(self, handler: LateDataHandler) -> None:
        """The ``limit`` arg caps the number of records returned."""
        now = time.time()
        for i in range(5):
            handler.record_late_arrival(
                source="clob",
                source_id=f"t-cap-{i}",
                event_type="trade",
                event_time=now - 60.0,
                ingestion_time=now,
            )
        lates = handler.get_late_arrivals(limit=2)
        assert len(lates) <= 2

    def test_most_recent_first(self, handler: LateDataHandler) -> None:
        """``get_late_arrivals`` orders by ``recorded_at`` DESC."""
        now = time.time()
        first = handler.record_late_arrival(
            source="clob", source_id="first", event_type="trade",
            event_time=now - 60.0, ingestion_time=now,
        )
        time.sleep(0.01)  # ensure recorded_at differs
        second = handler.record_late_arrival(
            source="clob", source_id="second", event_type="trade",
            event_time=now - 60.0, ingestion_time=now,
        )
        lates = handler.get_late_arrivals(limit=10)
        # Most-recent first.
        assert lates[0].late_id == second
        assert lates[1].late_id == first

    def test_counter_increments(self, handler: LateDataHandler) -> None:
        """``late_count`` in ``get_stats`` reflects the cumulative count."""
        now = time.time()
        for i in range(3):
            handler.record_late_arrival(
                source="clob",
                source_id=f"t-ct-{i}",
                event_type="trade",
                event_time=now - 60.0,
                ingestion_time=now,
            )
        assert handler.get_stats()["late_count"] == 3

    def test_storage_failure_returns_empty_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A SQLite write failure returns an empty string (best-effort
        contract — the handler must NEVER raise)."""
        handler = LateDataHandler(db_path=tmp_path / "ok.db")
        # Corrupt the db path AFTER init — replace ``_connect`` with one
        # that raises ``sqlite3.OperationalError``.
        import sqlite3 as _sql

        def _broken_connect(self):
            raise _sql.OperationalError("simulated disk failure")

        monkeypatch.setattr(LateDataHandler, "_connect", _broken_connect)
        now = time.time()
        late_id = handler.record_late_arrival(
            source="clob", source_id="t-broken", event_type="trade",
            event_time=now - 60.0, ingestion_time=now,
        )
        assert late_id == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Correction logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRecordCorrection:
    """``record_correction`` / ``get_corrections`` SQLite-backed log."""

    def test_returns_correction_id(self, handler: LateDataHandler) -> None:
        """A successful correction returns a UUID4 hex ``correction_id``."""
        cid = handler.record_correction(
            observation_id="obs-1",
            field_path="best_bid",
            old_value=0.50,
            new_value=0.51,
            reason="exchange_correction",
        )
        assert isinstance(cid, str) and len(cid) == 32

    def test_persists_and_round_trips(self, handler: LateDataHandler) -> None:
        """The recorded correction is fetchable via ``get_corrections``
        and round-trips every field (old/new values JSON-parsed back)."""
        handler.record_correction(
            observation_id="obs-2",
            field_path="payload.trades[0].price",
            old_value=0.42,
            new_value=0.43,
            reason="exchange_correction",
            source="clob",
            source_id="trade-2",
            actor="system",
            metadata={"exchange_msg_id": "M1"},
        )
        corrs = handler.get_corrections(limit=10)
        assert len(corrs) == 1
        c = corrs[0]
        assert isinstance(c, Correction)
        assert c.observation_id == "obs-2"
        assert c.field_path == "payload.trades[0].price"
        assert c.old_value == 0.42
        assert c.new_value == 0.43
        assert c.reason == "exchange_correction"
        assert c.source == "clob"
        assert c.source_id == "trade-2"
        assert c.actor == "system"
        assert c.metadata == {"exchange_msg_id": "M1"}
        assert c.corrected_at > 0.0

    def test_none_values_round_trip(self, handler: LateDataHandler) -> None:
        """``old_value=None`` is serialised as the JSON token ``null``
        so it round-trips cleanly through SQLite TEXT."""
        handler.record_correction(
            observation_id="obs-none",
            field_path="new_field",
            old_value=None,
            new_value="hello",
            reason="schema_migration",
        )
        c = handler.get_corrections(limit=1)[0]
        assert c.old_value is None
        assert c.new_value == "hello"

    def test_complex_values_round_trip(self, handler: LateDataHandler) -> None:
        """Dict / list values round-trip via JSON."""
        handler.record_correction(
            observation_id="obs-complex",
            field_path="payload.bids",
            old_value=[{"price": 0.5, "size": 10}],
            new_value=[{"price": 0.51, "size": 20}],
            reason="exchange_correction",
        )
        c = handler.get_corrections(limit=1)[0]
        assert c.old_value == [{"price": 0.5, "size": 10}]
        assert c.new_value == [{"price": 0.51, "size": 20}]

    def test_non_canonical_reason_stored_anyway(self, handler: LateDataHandler) -> None:
        """A non-canonical reason is stored anyway (the DB layer accepts
        free-form TEXT) — logged at WARNING, not rejected. The
        correction audit trail never loses a row to a vocabulary drift."""
        handler.record_correction(
            observation_id="obs-nc",
            field_path="best_bid",
            old_value=1,
            new_value=2,
            reason="some_future_reason_not_in_vocab",
        )
        c = handler.get_corrections(limit=1)[0]
        assert c.reason == "some_future_reason_not_in_vocab"

    def test_observation_id_filter_narrows_results(
        self, handler: LateDataHandler,
    ) -> None:
        """The ``observation_id`` filter on ``get_corrections`` narrows
        to corrections for that record only."""
        for i, obs in enumerate(["obs-A", "obs-B", "obs-A"]):
            handler.record_correction(
                observation_id=obs,
                field_path="f",
                old_value=i,
                new_value=i + 10,
                reason="reconciliation",
            )
        a_corrs = handler.get_corrections(limit=10, observation_id="obs-A")
        assert len(a_corrs) == 2
        assert all(c.observation_id == "obs-A" for c in a_corrs)

    def test_reason_filter_narrows_results(self, handler: LateDataHandler) -> None:
        """The ``reason`` filter on ``get_corrections`` narrows to
        corrections with that reason only."""
        for reason in ["exchange_correction", "reconciliation", "manual_override"]:
            handler.record_correction(
                observation_id="obs-r",
                field_path="f",
                old_value=0,
                new_value=1,
                reason=reason,
            )
        exc_corrs = handler.get_corrections(limit=10, reason="exchange_correction")
        assert len(exc_corrs) == 1
        assert exc_corrs[0].reason == "exchange_correction"

    def test_most_recent_first(self, handler: LateDataHandler) -> None:
        """``get_corrections`` orders by ``corrected_at`` DESC."""
        first = handler.record_correction(
            observation_id="obs-mf1", field_path="f",
            old_value=0, new_value=1, reason="reconciliation",
        )
        time.sleep(0.01)
        second = handler.record_correction(
            observation_id="obs-mf2", field_path="f",
            old_value=0, new_value=1, reason="reconciliation",
        )
        corrs = handler.get_corrections(limit=10)
        assert corrs[0].correction_id == second
        assert corrs[1].correction_id == first

    def test_counter_increments(self, handler: LateDataHandler) -> None:
        """``correction_count`` in ``get_stats`` reflects the count."""
        for i in range(3):
            handler.record_correction(
                observation_id=f"obs-ct-{i}", field_path="f",
                old_value=0, new_value=1, reason="reconciliation",
            )
        assert handler.get_stats()["correction_count"] == 3

    def test_all_canonical_reasons_accepted(self, handler: LateDataHandler) -> None:
        """Every entry in ``CORRECTION_REASONS`` is accepted by the
        precondition check (no spurious rejections)."""
        for reason in CORRECTION_REASONS:
            cid = handler.record_correction(
                observation_id=f"obs-{reason}", field_path="f",
                old_value=0, new_value=1, reason=reason,
            )
            assert cid, f"correction for reason={reason!r} returned empty id"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Point-in-time safety
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPointInTimeSafety:
    """``is_safe_for_pit`` + ``filter_pit_safe`` — ML feature
    leak-prevention."""

    def test_is_safe_returns_true_when_ingested_before_cutoff(
        self, handler: LateDataHandler,
    ) -> None:
        """A record ingested BEFORE the cutoff is PIT-safe (``True``)."""
        now = time.time()
        handler.record_late_arrival(
            source="clob", source_id="t-pit-1", event_type="trade",
            event_time=now - 100.0, ingestion_time=now - 10.0,
            observation_id="obs-pit-1",
        )
        # Cutoff 5 s in the future → the record (ingested 10 s ago)
        # is PIT-safe.
        assert handler.is_safe_for_pit("obs-pit-1", now + 5.0) is True

    def test_is_safe_returns_false_when_ingested_after_cutoff(
        self, handler: LateDataHandler,
    ) -> None:
        """A record ingested AFTER the cutoff is NOT PIT-safe (``False``)
        — would leak future info into a past prediction."""
        now = time.time()
        handler.record_late_arrival(
            source="clob", source_id="t-pit-2", event_type="trade",
            event_time=now - 100.0, ingestion_time=now,
            observation_id="obs-pit-2",
        )
        # Cutoff 30 s in the PAST → the record (ingested now) is
        # NOT PIT-safe.
        assert handler.is_safe_for_pit("obs-pit-2", now - 30.0) is False

    def test_is_safe_returns_none_for_unknown_observation(
        self, handler: LateDataHandler,
    ) -> None:
        """An observation_id not in the late-arrivals log returns
        ``None`` (the caller should fall back to the raw vault's
        ``ingestion_timestamp``)."""
        assert handler.is_safe_for_pit("does-not-exist", time.time()) is None

    def test_filter_pit_safe_excludes_future_records(
        self, handler: LateDataHandler,
    ) -> None:
        """``filter_pit_safe`` drops records whose ingestion_time is
        after the cutoff — the canonical "no future leakage" check."""
        now = time.time()
        records = [
            {"observation_id": "a", "ingestion_time": now - 100.0},
            {"observation_id": "b", "ingestion_time": now - 50.0},
            {"observation_id": "c", "ingestion_time": now + 100.0},  # future
        ]
        safe = handler.filter_pit_safe(records, as_of=now - 10.0)
        ids = {r["observation_id"] for r in safe}
        assert ids == {"a", "b"}

    def test_filter_pit_safe_drops_records_without_ingestion_time(
        self, handler: LateDataHandler,
    ) -> None:
        """Records without a numeric ``ingestion_time`` are DROPPED
        (a record without an ingestion timestamp can't be PIT-validated
        — the safest interpretation is "exclude")."""
        now = time.time()
        records = [
            {"observation_id": "ok", "ingestion_time": now - 100.0},
            {"observation_id": "missing_key"},
            {"observation_id": "none_val", "ingestion_time": None},
            {"observation_id": "str_val", "ingestion_time": "not-a-number"},
            "not-a-dict",  # non-dict iterable element
        ]
        safe = handler.filter_pit_safe(records, as_of=now)
        assert [r["observation_id"] for r in safe] == ["ok"]

    def test_filter_pit_safe_custom_key(self, handler: LateDataHandler) -> None:
        """The ``ingestion_time_key`` arg lets the caller use a custom
        dict key (e.g. the raw vault's ``ingestion_timestamp`` column
        name)."""
        now = time.time()
        records = [
            {"observation_id": "x", "ingestion_timestamp": now - 100.0},
            {"observation_id": "y", "ingestion_timestamp": now + 100.0},
        ]
        safe = handler.filter_pit_safe(
            records, as_of=now, ingestion_time_key="ingestion_timestamp",
        )
        assert [r["observation_id"] for r in safe] == ["x"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Alerting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLateRateAlert:
    """Late-rate alert heuristic — fires when the rolling late-rate
    exceeds ``LATE_RATE_ALERT_THRESHOLD``."""

    def test_alert_does_not_fire_below_threshold(
        self, handler: LateDataHandler, _patch_alert_engine: MagicMock,
    ) -> None:
        """A late-rate below the threshold does NOT fire the alert."""
        now = time.time()
        # 1 late out of 10 → 10 % (below 20 % threshold).
        for i in range(9):
            handler.detect_late_arrival(event_time=now - 1.0, ingestion_time=now)
        handler.detect_late_arrival(event_time=now - 60.0, ingestion_time=now)
        # Trigger an alert check by recording a late arrival.
        handler.record_late_arrival(
            source="clob", source_id="t-alert-1", event_type="trade",
            event_time=now - 60.0, ingestion_time=now,
        )
        assert handler.get_stats()["alert_fired_count"] == 0
        _patch_alert_engine.assert_not_called()

    def test_alert_fires_above_threshold(
        self, handler: LateDataHandler, _patch_alert_engine: MagicMock,
    ) -> None:
        """A late-rate above the threshold fires the
        ``late_data_rate_high`` alert (best-effort)."""
        now = time.time()
        # 9 late out of 10 → 90 % (above 20 % threshold).
        for _ in range(9):
            handler.detect_late_arrival(event_time=now - 60.0, ingestion_time=now)
        handler.detect_late_arrival(event_time=now - 1.0, ingestion_time=now)
        # Trigger an alert check by recording a late arrival.
        handler.record_late_arrival(
            source="clob", source_id="t-alert-2", event_type="trade",
            event_time=now - 60.0, ingestion_time=now,
        )
        assert handler.get_stats()["alert_fired_count"] >= 1
        _patch_alert_engine.assert_called()
        # The alert name + category are positional / keyword args.
        kwargs = _patch_alert_engine.call_args.kwargs
        assert kwargs.get("name") == "late_data_rate_high"
        assert kwargs.get("category") == "data"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Module-level singleton + constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModuleSingleton:
    """Module-level ``late_data_handler`` singleton + constants."""

    def test_singleton_constructed(self) -> None:
        """``late_data_handler`` is a ``LateDataHandler`` instance (the
        defensive ``try/except`` in ``ingestion.late_data`` didn't
        fire in the test env — ``LATE_DATA_DB_PATH`` is redirected to
        ``/tmp``)."""
        from ingestion.late_data import late_data_handler
        assert isinstance(late_data_handler, LateDataHandler)

    def test_constants_exposed(self) -> None:
        """The W35-4 constants are importable from the module."""
        assert LATE_THRESHOLD_S == 30.0
        assert LATE_RATE_ALERT_THRESHOLD == 0.20
        assert "exchange_correction" in CORRECTION_REASONS
        assert "reconciliation" in CORRECTION_REASONS
        assert "late_fill" in CORRECTION_REASONS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP API routes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Bearer token the env var ``API_TOKEN`` sets up (matches the conftest's
# ``API_TOKEN=test-token-conftest`` — see the env-redirect block above).
_VALID_TOKEN = "test-token-conftest"


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_ingestion_api.py``.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_late_data_singleton():
    """Reset the W35-4 ``late_data_handler`` singleton before every test.

    The API routes under ``/api/ingestion/late-arrivals`` /
    ``/api/ingestion/corrections`` read from the module-level singleton —
    it persists across tests within a pytest session. Without a reset,
    a prior test's seeded record would leak into the next test's HTTP
    response and break the count / membership assertions.

    Belt-and-braces with the autouse fixture in ``tests/test_ingestion_api.py``.
    The reset is best-effort — a transient SQLite hiccup is swallowed so
    the test session can still proceed.
    """
    try:
        from ingestion.late_data import late_data_handler
        # Truncate the on-disk SQLite tables directly so the next test
        # starts from a known-empty state. ``reset_stats`` only clears
        # the in-memory counters / deque; it does NOT truncate the
        # tables (mirrors the ``raw_vault`` convention).
        try:
            import sqlite3 as _sql
            with _sql.connect(str(late_data_handler._db_path)) as conn:
                conn.execute("DELETE FROM late_arrivals")
                conn.execute("DELETE FROM corrections")
                conn.commit()
        except Exception:  # pragma: no cover — defensive
            pass
        late_data_handler.reset_stats()
    except Exception:  # pragma: no cover — defensive
        pass

    yield  # ── test runs ──

    # No post-test teardown — the pre-test reset of the NEXT test
    # cleans up whatever the prior test seeded.


def _seed_one_late_arrival() -> str:
    """Seed the singleton with one late arrival. Returns the ``late_id``."""
    from ingestion.late_data import late_data_handler

    now = time.time()
    late_id = late_data_handler.record_late_arrival(
        source="clob",
        source_id=f"trade-api-{int(now * 1000)}",
        event_type="trade",
        event_time=now - 60.0,
        ingestion_time=now,
        observation_id="obs-api-1",
        token_id="tok-api-1",
        metadata={"reason": "late fill", "_seed": True},
    )
    assert late_id, (
        "late_data_handler.record_late_arrival returned empty string — "
        "the singleton's db may be unwritable; check LATE_DATA_DB_PATH"
    )
    return late_id


def _seed_one_correction() -> str:
    """Seed the singleton with one correction. Returns the
    ``correction_id``."""
    from ingestion.late_data import late_data_handler

    cid = late_data_handler.record_correction(
        observation_id="obs-api-1",
        field_path="best_bid",
        old_value=0.50,
        new_value=0.51,
        reason="exchange_correction",
        source="clob",
        source_id="trade-api-1",
        actor="system",
        metadata={"_seed": True},
    )
    assert cid, (
        "late_data_handler.record_correction returned empty string — "
        "the singleton's db may be unwritable; check LATE_DATA_DB_PATH"
    )
    return cid


# ── GET /api/ingestion/corrections ─────────────────────────────────────────


class TestCorrectionsRoute:
    """``GET /api/ingestion/corrections``."""

    def test_returns_200_with_empty_handler(self, client, auth_headers):
        """``GET /api/ingestion/corrections`` must return 200 with the
        zero-state (empty ``corrections`` list, ``count=0``) when no
        corrections have been logged yet — no fabrication, no 500."""
        response = client.get("/api/ingestion/corrections", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ingestion/corrections must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "corrections" in data
        assert isinstance(data["corrections"], list)
        assert data["count"] == len(data["corrections"])
        assert data["count"] == 0
        assert "handler_stats" in data
        assert "generated_at" in data

    def test_returns_seeded_correction(self, client, auth_headers):
        """When the handler has been seeded with a correction, the route
        surfaces it in the ``corrections`` list with every field."""
        cid = _seed_one_correction()
        response = client.get("/api/ingestion/corrections", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1, (
            f"expected ≥1 correction after seeding; got count={data['count']}"
        )
        ids = [c.get("correction_id") for c in data["corrections"]]
        assert cid in ids, (
            f"seeded correction_id {cid!r} not in returned ids {ids}"
        )
        # Verify the seeded correction's fields round-trip.
        c = next(corr for corr in data["corrections"] if corr["correction_id"] == cid)
        assert c["observation_id"] == "obs-api-1"
        assert c["field_path"] == "best_bid"
        assert c["old_value"] == 0.50
        assert c["new_value"] == 0.51
        assert c["reason"] == "exchange_correction"
        assert c["source"] == "clob"
        assert c["actor"] == "system"

    def test_observation_id_filter_narrows_results(self, client, auth_headers):
        """The ``observation_id`` query param narrows the result set."""
        _seed_one_correction()
        response = client.get(
            "/api/ingestion/corrections?observation_id=obs-api-1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["observation_id_filter"] == "obs-api-1"
        for c in data["corrections"]:
            assert c["observation_id"] == "obs-api-1"

    def test_observation_id_filter_no_match(self, client, auth_headers):
        """A filter that matches nothing returns an empty list (NOT a
        404 — the route treats "no matching corrections" as the honest
        zero-state, not an error)."""
        _seed_one_correction()
        response = client.get(
            "/api/ingestion/corrections?observation_id=does-not-exist",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["corrections"] == []

    def test_reason_filter_narrows_results(self, client, auth_headers):
        """The ``reason`` query param narrows the result set."""
        _seed_one_correction()
        response = client.get(
            "/api/ingestion/corrections?reason=exchange_correction",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["reason_filter"] == "exchange_correction"
        for c in data["corrections"]:
            assert c["reason"] == "exchange_correction"

    def test_limit_param_caps_response_size(self, client, auth_headers):
        """The ``limit`` query param caps the number of records returned."""
        response = client.get(
            "/api/ingestion/corrections?limit=1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["corrections"]) <= 1

    def test_limit_over_max_rejected(self, client, auth_headers):
        """``limit=1001`` exceeds the ``le=1000`` ceiling — the route
        must 422 (FastAPI's standard validation error)."""
        response = client.get(
            "/api/ingestion/corrections?limit=1001",
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"limit=1001 must 422; got {response.status_code}"
        )

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/corrections`` without an Authorization
        header must 401 (fail-closed auth middleware)."""
        response = client.get("/api/ingestion/corrections")
        assert response.status_code == 401

    def test_handler_stats_block_present(self, client, auth_headers):
        """The ``handler_stats`` block mirrors ``get_stats`` so the
        operator can see the running counters alongside the recent
        corrections."""
        _seed_one_correction()
        response = client.get("/api/ingestion/corrections", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        stats = data["handler_stats"]
        assert "late_count" in stats
        assert "correction_count" in stats
        assert "late_threshold_s" in stats
        assert "late_rate" in stats
        assert "alert_threshold" in stats
        assert stats["correction_count"] >= 1


# ── GET /api/ingestion/late-arrivals ────────────────────────────────────────


class TestLateArrivalsRoute:
    """``GET /api/ingestion/late-arrivals``."""

    def test_returns_200_with_empty_handler(self, client, auth_headers):
        """``GET /api/ingestion/late-arrivals`` must return 200 with the
        zero-state (empty ``late_arrivals`` list, ``count=0``) when no
        late arrivals have been logged yet — no fabrication, no 500."""
        response = client.get("/api/ingestion/late-arrivals", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/ingestion/late-arrivals must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "late_arrivals" in data
        assert isinstance(data["late_arrivals"], list)
        assert data["count"] == len(data["late_arrivals"])
        assert data["count"] == 0
        assert "handler_stats" in data
        assert "generated_at" in data

    def test_returns_seeded_late_arrival(self, client, auth_headers):
        """When the handler has been seeded with a late arrival, the
        route surfaces it in the ``late_arrivals`` list with every field."""
        late_id = _seed_one_late_arrival()
        response = client.get("/api/ingestion/late-arrivals", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1, (
            f"expected ≥1 late arrival after seeding; got count={data['count']}"
        )
        ids = [la.get("late_id") for la in data["late_arrivals"]]
        assert late_id in ids, (
            f"seeded late_id {late_id!r} not in returned ids {ids}"
        )
        la = next(l for l in data["late_arrivals"] if l["late_id"] == late_id)
        assert la["source"] == "clob"
        assert la["event_type"] == "trade"
        assert la["observation_id"] == "obs-api-1"
        assert la["token_id"] == "tok-api-1"
        assert la["lateness_seconds"] == pytest.approx(60.0, abs=0.5)
        assert la["metadata"] == {"reason": "late fill", "_seed": True}

    def test_source_filter_narrows_results(self, client, auth_headers):
        """The ``source`` query param narrows the result set."""
        _seed_one_late_arrival()
        response = client.get(
            "/api/ingestion/late-arrivals?source=clob",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["source_filter"] == "clob"
        for la in data["late_arrivals"]:
            assert la["source"] == "clob"

    def test_token_filter_narrows_results(self, client, auth_headers):
        """The ``token_id`` query param narrows the result set."""
        _seed_one_late_arrival()
        response = client.get(
            "/api/ingestion/late-arrivals?token_id=tok-api-1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["token_id_filter"] == "tok-api-1"
        for la in data["late_arrivals"]:
            assert la["token_id"] == "tok-api-1"

    def test_limit_param_caps_response_size(self, client, auth_headers):
        """The ``limit`` query param caps the number of records returned."""
        response = client.get(
            "/api/ingestion/late-arrivals?limit=1",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["late_arrivals"]) <= 1

    def test_limit_over_max_rejected(self, client, auth_headers):
        """``limit=1001`` exceeds the ``le=1000`` ceiling — 422."""
        response = client.get(
            "/api/ingestion/late-arrivals?limit=1001",
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_no_auth_returns_401(self, client):
        """``GET /api/ingestion/late-arrivals`` without an Authorization
        header must 401."""
        response = client.get("/api/ingestion/late-arrivals")
        assert response.status_code == 401

    def test_handler_stats_block_present(self, client, auth_headers):
        """The ``handler_stats`` block mirrors ``get_stats``."""
        _seed_one_late_arrival()
        response = client.get("/api/ingestion/late-arrivals", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        stats = data["handler_stats"]
        assert "late_count" in stats
        assert "correction_count" in stats
        assert "late_threshold_s" in stats
        assert "late_rate" in stats
        assert stats["late_count"] >= 1


# ── OpenAPI tag + path declaration ──────────────────────────────────────────


class TestOpenAPIDeclaration:
    """The new routes must be declared in the OpenAPI schema under the
    ``ingestion`` tag."""

    def test_paths_declared(self, client, auth_headers):
        """Both new paths must appear in ``openapi.json``."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        for path in (
            "/api/ingestion/corrections",
            "/api/ingestion/late-arrivals",
        ):
            assert path in paths, (
                f"OpenAPI schema must declare path {path!r}; got "
                f"{sorted(paths.keys())[:30]}..."
            )

    def test_routes_carry_ingestion_tag(self, client, auth_headers):
        """Both new routes carry ``tags=['ingestion']``."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        for path in (
            "/api/ingestion/corrections",
            "/api/ingestion/late-arrivals",
        ):
            for method, op in schema["paths"][path].items():
                if method not in ("get", "post", "delete", "put", "patch"):
                    continue
                tags = op.get("tags", [])
                assert "ingestion" in tags, (
                    f"{method.upper()} {path} must carry tags=['ingestion']; "
                    f"got {tags}"
                )
