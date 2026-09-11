"""
W17-5 — Unit + API tests for ``core/immutable_audit.py``.

Covers the cryptographic-immutability guarantees of the hash-chained
audit trail:

  (A) ``ImmutableAuditTrail.log`` builds a correct hash chain
      (genesis → entry 1 → entry 2 → …; each ``previous_hash`` equals
      the prior entry's ``entry_hash``; each ``entry_hash`` equals
      the SHA-256 of the entry's identity fields).

  (B) ``verify_chain`` returns ``valid=True`` on an intact chain.

  (C) ``verify_chain`` detects tampering: (i) modifying an entry's
      ``event_type`` / ``payload`` / ``timestamp`` / ``sequence`` breaks
      the chain at the tampered row; (ii) modifying an entry's
      ``previous_hash`` breaks the chain at the row whose link was
      rewritten; (iii) modifying an entry's ``entry_hash`` (without
      recomputing the link) breaks the chain at that row.

  (D) ``get_entries`` returns rows newest-first, respects ``limit`` /
      ``offset`` pagination, and filters by ``event_type``.

  (E) ``get_stats`` reports the correct total / per-type counts /
      latest timestamp / tail hash / sequence number.

  (F) The four ``/api/audit/immutable/*`` routes registered by
      ``register_routes`` behave correctly over HTTP: list, verify,
      stats, and manual-log. Routes are tested via a fresh
      ``FastAPI`` + ``TestClient`` app with only the immutable-audit
      routes registered (mirrors the
      ``tests/test_feature_flags.py::TestFeatureFlagRoutes`` pattern).

Isolation
~~~~~~~~~
The module-level ``immutable_audit`` singleton is constructed at import
time against the conftest-redirected ``IMMUTABLE_AUDIT_DB`` (see
``tests/conftest.py::_ENV_REDIRECTS`` →
``/tmp/pmbot_conftest_isolation/immutable_audit.db``). Each unit test
constructs a fresh ``ImmutableAuditTrail(db_path=tmp_path/...)`` so
the on-disk chain is hermetic per test. Each API test monkeypatches
``core.immutable_audit.immutable_audit`` to a fresh ``ImmutableAuditTrail``
pointed at a ``tmp_path`` SQLite file (so the route handlers — which
reference the module global at call time — see the test-local chain).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import immutable_audit as immutable_audit_module
from core.immutable_audit import (
    AuditEntry,
    GENESIS_HASH,
    IMMUTABLE_AUDIT_DB,
    ImmutableAuditTrail,
    register_routes,
)


# ── (A) log() builds a correct hash chain ────────────────────────────────────


@pytest.fixture
def trail(tmp_path: Path) -> ImmutableAuditTrail:
    """Fresh ``ImmutableAuditTrail`` against a ``tmp_path`` SQLite file.

    Independent of the module-level singleton — no shared state with
    the API-route tests or any sibling unit test.
    """
    db_path = tmp_path / "test_immutable_audit_unit.db"
    return ImmutableAuditTrail(db_path=db_path)


class TestLogBuildsCorrectHashChain:
    """Method-level coverage of :meth:`ImmutableAuditTrail.log`."""

    def test_first_entry_uses_genesis_hash(self, trail: ImmutableAuditTrail):
        """The first entry's ``previous_hash`` must be ``GENESIS_HASH``
        (the chain's genesis anchor)."""
        entry = trail.log("test_event", {"a": 1})
        assert entry is not None
        assert entry.previous_hash == GENESIS_HASH
        assert entry.sequence == 1
        assert entry.entry_id == 1

    def test_entry_hash_matches_sha256_of_identity_fields(
        self, trail: ImmutableAuditTrail
    ):
        """``entry_hash`` must equal the SHA-256 of
        ``timestamp:event_type:payload:previous_hash:sequence``
        (the canonical hash function on :class:`ImmutableAuditTrail`)."""
        import hashlib

        entry = trail.log("test_event", {"a": 1, "b": 2})
        assert entry is not None

        expected_payload = json.dumps({"a": 1, "b": 2}, sort_keys=True, default=str)
        expected_hash = hashlib.sha256(
            f"{entry.timestamp}:test_event:{expected_payload}:{GENESIS_HASH}:1".encode()
        ).hexdigest()
        assert entry.entry_hash == expected_hash

    def test_each_entry_links_to_previous(self, trail: ImmutableAuditTrail):
        """After logging N entries, each entry's ``previous_hash`` equals
        the prior entry's ``entry_hash`` (forming a contiguous chain)."""
        entries = [trail.log(f"event_{i}", {"i": i}) for i in range(5)]
        entries = [e for e in entries if e is not None]
        assert len(entries) == 5

        # Genesis link.
        assert entries[0].previous_hash == GENESIS_HASH

        # Each subsequent entry's previous_hash == prior entry's entry_hash.
        for i in range(1, len(entries)):
            assert entries[i].previous_hash == entries[i - 1].entry_hash

        # Sequence numbers are monotonic 1..N.
        assert [e.sequence for e in entries] == [1, 2, 3, 4, 5]

    def test_in_memory_tail_hash_matches_disk(self, trail: ImmutableAuditTrail):
        """After logging, the in-memory ``_last_hash`` must equal the
        most recent on-disk ``entry_hash`` so the next ``log`` call
        continues the chain correctly."""
        trail.log("a", {"x": 1})
        trail.log("b", {"x": 2})
        trail.log("c", {"x": 3})

        with sqlite3.connect(trail._db_path) as conn:
            row = conn.execute(
                "SELECT entry_hash, sequence FROM audit_chain ORDER BY entry_id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert trail._last_hash == row[0]
        assert trail._sequence == row[1]

    def test_payload_is_json_serialised_with_sorted_keys(
        self, trail: ImmutableAuditTrail
    ):
        """``payload`` must be JSON with ``sort_keys=True`` so a dict
        whose keys are passed in a different order still produces the
        same hash (the chain is order-independent)."""
        e1 = trail.log("ev", {"b": 2, "a": 1, "c": 3})
        assert e1 is not None
        assert e1.payload == '{"a": 1, "b": 2, "c": 3}'

    def test_load_last_entry_resumes_chain_after_restart(self, tmp_path: Path):
        """A fresh ``ImmutableAuditTrail`` constructed against an existing
        db file must resume the chain from the last entry — not start
        over at the genesis hash."""
        db_path = tmp_path / "resume.db"
        first = ImmutableAuditTrail(db_path=db_path)
        first.log("a", {"x": 1})
        first.log("b", {"x": 2})
        assert first._sequence == 2
        tail_hash = first._last_hash

        # Simulate a process restart: a NEW trail instance against the
        # same db file. ``_load_last_entry`` must pick up the tail.
        resumed = ImmutableAuditTrail(db_path=db_path)
        assert resumed._sequence == 2
        assert resumed._last_hash == tail_hash

        # The next log entry links to the resumed tail, not the genesis.
        new_entry = resumed.log("c", {"x": 3})
        assert new_entry is not None
        assert new_entry.previous_hash == tail_hash
        assert new_entry.sequence == 3


# ── (B) verify_chain on a valid chain ──────────────────────────────────────


class TestVerifyChainOnValidChain:
    """``verify_chain`` must return ``valid=True`` on an intact chain."""

    def test_verify_empty_chain_is_valid(self, trail: ImmutableAuditTrail):
        """An empty chain is trivially valid (no rows to tamper with)."""
        result = trail.verify_chain()
        assert result["valid"] is True
        assert result["broken_at"] is None
        assert result["checked"] == 0

    def test_verify_single_entry_chain(self, trail: ImmutableAuditTrail):
        """A one-entry chain must verify as valid with the entry's hash
        as the ``last_hash``."""
        entry = trail.log("test", {"a": 1})
        assert entry is not None
        result = trail.verify_chain()
        assert result["valid"] is True
        assert result["broken_at"] is None
        assert result["checked"] == 1
        assert result["last_hash"] == entry.entry_hash

    def test_verify_multi_entry_chain(self, trail: ImmutableAuditTrail):
        """A multi-entry chain with no tampering must verify as valid."""
        for i in range(10):
            trail.log(f"event_{i}", {"i": i, "data": "x" * i})
        result = trail.verify_chain()
        assert result["valid"] is True
        assert result["broken_at"] is None
        assert result["checked"] == 10
        assert result["last_hash"] == trail._last_hash

    def test_verify_subrange_uses_start_id(self, trail: ImmutableAuditTrail):
        """``verify_chain(start_id=N)`` skips entries before N. The
        sub-range is verified against its own boundary (the first row's
        stored ``previous_hash`` is trusted)."""
        for i in range(5):
            trail.log(f"event_{i}", {"i": i})
        # Verify entries 3..5 (entry_ids 3, 4, 5).
        result = trail.verify_chain(start_id=3)
        assert result["valid"] is True
        assert result["checked"] == 3


# ── (C) verify_chain detects tampering ────────────────────────────────────


class TestVerifyChainDetectsTampering:
    """``verify_chain`` must surface ANY tampering with a past entry."""

    def _tamper(
        self, trail: ImmutableAuditTrail, entry_id: int, column: str, new_value: object
    ) -> None:
        """Direct SQL UPDATE on a row of the audit_chain table.

        Bypasses the trail's ``log`` path so the in-memory ``_last_hash``
        is NOT updated — this is exactly the tampering scenario the
        chain is designed to detect (someone modifies a past row without
        re-computing the chain).
        """
        with sqlite3.connect(trail._db_path) as conn:
            conn.execute(
                f"UPDATE audit_chain SET {column} = ? WHERE entry_id = ?",
                (new_value, entry_id),
            )

    def test_tampering_event_type_breaks_chain(self, trail: ImmutableAuditTrail):
        """Modifying a past entry's ``event_type`` breaks the chain at
        that entry (its stored ``entry_hash`` no longer matches the
        recomputed hash)."""
        for i in range(5):
            trail.log(f"event_{i}", {"i": i})
        # Tamper with entry_id=3 — change the event_type from
        # "event_2" to "TAMPERED".
        self._tamper(trail, entry_id=3, column="event_type", new_value="TAMPERED")
        result = trail.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 3

    def test_tampering_payload_breaks_chain(self, trail: ImmutableAuditTrail):
        """Modifying a past entry's ``payload`` breaks the chain at
        that entry."""
        for i in range(3):
            trail.log("test", {"i": i})
        self._tamper(
            trail, entry_id=2, column="payload", new_value='{"tampered": true}'
        )
        result = trail.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 2

    def test_tampering_timestamp_breaks_chain(self, trail: ImmutableAuditTrail):
        """Modifying a past entry's ``timestamp`` breaks the chain."""
        for i in range(3):
            trail.log("test", {"i": i})
        self._tamper(trail, entry_id=1, column="timestamp", new_value=0.0)
        result = trail.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 1

    def test_tampering_previous_hash_breaks_chain(self, trail: ImmutableAuditTrail):
        """Rewriting an entry's ``previous_hash`` (without updating the
        subsequent entry's link) breaks the chain at the tampered row —
        the stored ``previous_hash`` no longer matches the actual hash
        of the preceding entry."""
        for i in range(3):
            trail.log("test", {"i": i})
        # Tamper with entry_id=2 — set previous_hash to garbage.
        self._tamper(
            trail, entry_id=2, column="previous_hash", new_value="f" * 64
        )
        result = trail.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 2

    def test_tampering_entry_hash_breaks_chain(self, trail: ImmutableAuditTrail):
        """Directly modifying an entry's ``entry_hash`` (without
        recomputing the chain) breaks the chain at the tampered row
        AND at the subsequent row (whose ``previous_hash`` no longer
        matches the tampered ``entry_hash``). The first break is the
        earliest one — at the tampered row itself."""
        for i in range(3):
            trail.log("test", {"i": i})
        self._tamper(
            trail, entry_id=2, column="entry_hash", new_value="a" * 64
        )
        result = trail.verify_chain()
        assert result["valid"] is False
        # First break is at entry 2 — its recomputed hash != stored hash.
        assert result["broken_at"] == 2

    def test_tampering_last_entry_breaks_chain(self, trail: ImmutableAuditTrail):
        """Tampering with the LAST entry must still be detected (the
        chain verifier doesn't trust the in-memory ``_last_hash`` —
        it recomputes every hash on read)."""
        for i in range(3):
            trail.log("test", {"i": i})
        self._tamper(
            trail, entry_id=3, column="event_type", new_value="TAMPERED_LAST"
        )
        result = trail.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 3


# ── (D) get_entries ────────────────────────────────────────────────────────


class TestGetEntries:
    """``get_entries`` returns rows newest-first with pagination."""

    def test_get_entries_returns_newest_first(self, trail: ImmutableAuditTrail):
        """``get_entries`` returns rows in descending ``entry_id`` order
        (most-recent-first)."""
        for i in range(5):
            trail.log(f"event_{i}", {"i": i})
        rows = trail.get_entries(limit=100)
        assert len(rows) == 5
        # Newest first.
        assert [r["entry_id"] for r in rows] == [5, 4, 3, 2, 1]

    def test_get_entries_respects_limit(self, trail: ImmutableAuditTrail):
        """``limit=N`` returns at most N rows."""
        for i in range(10):
            trail.log(f"event_{i}", {"i": i})
        rows = trail.get_entries(limit=3)
        assert len(rows) == 3
        assert [r["entry_id"] for r in rows] == [10, 9, 8]

    def test_get_entries_supports_offset_pagination(
        self, trail: ImmutableAuditTrail
    ):
        """``offset=N`` skips the first N rows (in descending order)
        so the dashboard can paginate through the chain."""
        for i in range(10):
            trail.log(f"event_{i}", {"i": i})
        page1 = trail.get_entries(limit=3, offset=0)
        page2 = trail.get_entries(limit=3, offset=3)
        assert [r["entry_id"] for r in page1] == [10, 9, 8]
        assert [r["entry_id"] for r in page2] == [7, 6, 5]

    def test_get_entries_filters_by_event_type(self, trail: ImmutableAuditTrail):
        """``event_type=X`` filters to only rows of that type."""
        trail.log("trade_executed", {"a": 1})
        trail.log("kill_switch_activated", {"b": 2})
        trail.log("trade_executed", {"c": 3})
        trail.log("position_closed", {"d": 4})

        rows = trail.get_entries(event_type="trade_executed", limit=100)
        assert len(rows) == 2
        assert all(r["event_type"] == "trade_executed" for r in rows)
        # Newest-first within the filter.
        assert [r["entry_id"] for r in rows] == [3, 1]

    def test_get_entries_on_empty_chain_returns_empty_list(
        self, trail: ImmutableAuditTrail
    ):
        """An empty chain returns ``[]`` (not None, not an exception)."""
        assert trail.get_entries() == []


# ── (E) get_stats ──────────────────────────────────────────────────────────


class TestGetStats:
    """``get_stats`` reports aggregate chain stats."""

    def test_get_stats_on_empty_chain(self, trail: ImmutableAuditTrail):
        """An empty chain reports zero entries + genesis tail hash."""
        stats = trail.get_stats()
        assert stats["total_entries"] == 0
        assert stats["event_types"] == {}
        assert stats["latest_timestamp"] is None
        assert stats["last_hash"] == GENESIS_HASH
        assert stats["sequence"] == 0

    def test_get_stats_reports_total_and_per_type_counts(
        self, trail: ImmutableAuditTrail
    ):
        """``total_entries`` and ``event_types`` reflect the chain."""
        trail.log("trade_executed", {"x": 1})
        trail.log("trade_executed", {"x": 2})
        trail.log("kill_switch_activated", {"y": 1})
        trail.log("position_closed", {"z": 1})

        stats = trail.get_stats()
        assert stats["total_entries"] == 4
        assert stats["event_types"] == {
            "trade_executed": 2,
            "kill_switch_activated": 1,
            "position_closed": 1,
        }

    def test_get_stats_reports_latest_timestamp_and_tail(
        self, trail: ImmutableAuditTrail
    ):
        """``latest_timestamp`` is the timestamp of the last entry;
        ``last_hash`` / ``sequence`` reflect the in-memory tail state."""
        e1 = trail.log("a", {"x": 1})
        e2 = trail.log("b", {"x": 2})
        assert e1 is not None and e2 is not None

        stats = trail.get_stats()
        assert stats["latest_timestamp"] == e2.timestamp
        assert stats["last_hash"] == e2.entry_hash
        assert stats["sequence"] == 2


# ── (F) API routes ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_trail(monkeypatch, tmp_path: Path) -> ImmutableAuditTrail:
    """Replace ``core.immutable_audit.immutable_audit`` with a fresh
    ``ImmutableAuditTrail`` constructed on a ``tmp_path`` SQLite file.

    The route handlers in ``register_routes`` reference the module
    global ``immutable_audit`` at call time (closure over the module
    namespace), so the swap is picked up by every handler without
    re-registration.

    Mirrors the ``isolated_flags`` fixture in
    ``tests/test_feature_flags.py`` — same monkeypatch-the-module-global
    pattern, same hermetic-per-test SQLite file.
    """
    db_path = tmp_path / "test_immutable_audit_api.db"
    fresh = ImmutableAuditTrail(db_path=db_path)
    monkeypatch.setattr(immutable_audit_module, "immutable_audit", fresh)
    return fresh


@pytest.fixture
def client(isolated_trail: ImmutableAuditTrail) -> TestClient:
    """Fresh ``FastAPI`` app with only the immutable-audit routes registered.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` (W17-5 block) so the route definitions / Pydantic
    validation annotations exercised here are byte-identical to what
    the live server exposes — without the bearer-token auth middleware
    or the heavy ``lifespan`` startup.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestImmutableAuditRoutes:
    """HTTP-level coverage of the four ``/api/audit/immutable/*`` routes."""

    def test_list_returns_200_with_empty_chain(self, client: TestClient):
        """``GET /api/audit/immutable`` on an empty chain returns 200
        with ``count=0`` and an empty ``entries`` list."""
        resp = client.get("/api/audit/immutable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["entries"] == []

    def test_list_returns_entries_newest_first(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``GET /api/audit/immutable`` returns entries newest-first
        after a few are logged."""
        isolated_trail.log("trade_executed", {"token_id": "T1"})
        isolated_trail.log("kill_switch_activated", {"reason": "test"})
        isolated_trail.log("position_closed", {"token_id": "T1"})

        resp = client.get("/api/audit/immutable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        entry_ids = [e["entry_id"] for e in body["entries"]]
        assert entry_ids == [3, 2, 1]

    def test_list_filters_by_event_type(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """The ``event_type`` query param filters to a single type."""
        isolated_trail.log("trade_executed", {"a": 1})
        isolated_trail.log("kill_switch_activated", {"b": 2})
        isolated_trail.log("trade_executed", {"c": 3})

        resp = client.get("/api/audit/immutable", params={"event_type": "trade_executed"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert all(e["event_type"] == "trade_executed" for e in body["entries"])

    def test_list_pagination(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``limit`` + ``offset`` paginates through the chain."""
        for i in range(7):
            isolated_trail.log("event", {"i": i})

        page1 = client.get("/api/audit/immutable", params={"limit": 3, "offset": 0})
        page2 = client.get("/api/audit/immutable", params={"limit": 3, "offset": 3})
        assert page1.status_code == 200
        assert page2.status_code == 200
        assert [e["entry_id"] for e in page1.json()["entries"]] == [7, 6, 5]
        assert [e["entry_id"] for e in page2.json()["entries"]] == [4, 3, 2]

    def test_list_rejects_invalid_limit(self, client: TestClient):
        """Out-of-range ``limit`` returns 422 (FastAPI's Query validation)."""
        resp = client.get("/api/audit/immutable", params={"limit": 0})
        assert resp.status_code == 422
        resp = client.get("/api/audit/immutable", params={"limit": 1001})
        assert resp.status_code == 422

    def test_verify_returns_valid_on_intact_chain(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``GET /api/audit/immutable/verify`` returns ``valid=True``
        on an intact chain."""
        isolated_trail.log("a", {"x": 1})
        isolated_trail.log("b", {"x": 2})

        resp = client.get("/api/audit/immutable/verify")
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["broken_at"] is None
        assert body["checked"] == 2
        assert body["last_hash"] == isolated_trail._last_hash

    def test_verify_returns_invalid_on_tampered_chain(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``GET /api/audit/immutable/verify`` returns ``valid=False``
        + ``broken_at=<entry_id>`` after a row is tampered with."""
        isolated_trail.log("a", {"x": 1})
        isolated_trail.log("b", {"x": 2})
        isolated_trail.log("c", {"x": 3})

        # Tamper with entry_id=2 directly on disk (bypassing the log path).
        with sqlite3.connect(isolated_trail._db_path) as conn:
            conn.execute(
                "UPDATE audit_chain SET event_type = ? WHERE entry_id = ?",
                ("TAMPERED", 2),
            )

        resp = client.get("/api/audit/immutable/verify")
        assert resp.status_code == 200  # 200 with valid=False (operator alert, not a 5xx)
        body = resp.json()
        assert body["valid"] is False
        assert body["broken_at"] == 2
        assert body["checked"] == 3

    def test_stats_returns_aggregate_counts(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``GET /api/audit/immutable/stats`` reports total / per-type
        counts / latest timestamp / tail hash / sequence."""
        isolated_trail.log("trade_executed", {"a": 1})
        isolated_trail.log("trade_executed", {"b": 2})
        isolated_trail.log("kill_switch_activated", {"c": 3})

        resp = client.get("/api/audit/immutable/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_entries"] == 3
        assert body["event_types"] == {
            "trade_executed": 2,
            "kill_switch_activated": 1,
        }
        assert body["sequence"] == 3

    def test_log_endpoint_appends_to_chain(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """``POST /api/audit/immutable/log`` appends an entry to the
        chain and returns the inserted entry (with its hash)."""
        resp = client.post(
            "/api/audit/immutable/log",
            json={"event_type": "manual_event", "payload": {"k": "v"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        entry = body["entry"]
        assert entry["event_type"] == "manual_event"
        assert entry["payload"] == '{"k": "v"}'
        assert entry["previous_hash"] == GENESIS_HASH
        assert entry["sequence"] == 1
        assert len(entry["entry_hash"]) == 64  # SHA-256 hex

        # The entry was actually persisted on disk.
        rows = isolated_trail.get_entries()
        assert len(rows) == 1
        assert rows[0]["entry_hash"] == entry["entry_hash"]

    def test_log_endpoint_validates_request_body(self, client: TestClient):
        """``POST /api/audit/immutable/log`` requires ``event_type``
        (non-empty string) and ``payload`` (object). Missing or invalid
        fields return 422."""
        # Missing event_type.
        resp = client.post(
            "/api/audit/immutable/log", json={"payload": {"x": 1}}
        )
        assert resp.status_code == 422

        # Empty event_type.
        resp = client.post(
            "/api/audit/immutable/log",
            json={"event_type": "", "payload": {"x": 1}},
        )
        assert resp.status_code == 422

    def test_log_endpoint_chains_correctly(
        self, client: TestClient, isolated_trail: ImmutableAuditTrail
    ):
        """Two consecutive ``POST /log`` calls produce a contiguous
        chain: the second entry's ``previous_hash`` equals the first
        entry's ``entry_hash``."""
        resp1 = client.post(
            "/api/audit/immutable/log",
            json={"event_type": "first", "payload": {"n": 1}},
        )
        resp2 = client.post(
            "/api/audit/immutable/log",
            json={"event_type": "second", "payload": {"n": 2}},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        e1 = resp1.json()["entry"]
        e2 = resp2.json()["entry"]
        assert e2["previous_hash"] == e1["entry_hash"]
        assert e2["sequence"] == 2

        # The chain verifies as intact.
        verify = client.get("/api/audit/immutable/verify").json()
        assert verify["valid"] is True
        assert verify["checked"] == 2


# ── (G) Integration: log() returns None on persistence failure ─────────────


class TestLogFailureHandling:
    """``log()`` must return None (not raise) on persistence failure so
    the trading pipeline is never blocked by a broken audit write."""

    def test_log_returns_none_on_unwritable_db(self, tmp_path: Path):
        """If the db path is unwritable, ``log`` returns None and the
        in-memory sequence is rolled back so the next call doesn't skip
        a number."""
        # Point the trail at a path whose parent doesn't exist and
        # cannot be created (the parent's parent has no write perms).
        # We use a non-existent file inside a read-only directory.
        # Simpler: just point the db_path at a directory (not a file)
        # so sqlite3.connect raises.
        bad_path = tmp_path / "i_am_a_directory_not_a_db"
        bad_path.mkdir(parents=True, exist_ok=True)

        trail = ImmutableAuditTrail(db_path=bad_path / "nested.db")
        # _init_db created the directory + an empty db file — the trail
        # is "alive" but we'll force a failure by removing write perms.
        # Use chmod to make the file read-only.
        # Actually the simplest path: re-bind _db_path to a directory
        # (which is not a valid SQLite db file).
        trail._db_path = bad_path  # points at the directory, not a file

        # _sequence starts at 0; after a failed log, it should NOT be 1.
        result = trail.log("failing_event", {"x": 1})
        assert result is None
        # The in-memory sequence was rolled back.
        assert trail._sequence == 0


# ── (H) Singleton importability ────────────────────────────────────────────


def test_module_singleton_is_constructed():
    """The module-level singleton ``immutable_audit`` is constructed at
    import time and is an instance of :class:`ImmutableAuditTrail`.

    (This is the contract the production ``api/server.py`` W17-5 block
    relies on — ``from core.immutable_audit import immutable_audit``
    must succeed and yield a usable trail.)
    """
    from core.immutable_audit import immutable_audit as singleton

    assert isinstance(singleton, ImmutableAuditTrail)
    # Tail hash is either GENESIS_HASH (empty chain) or a 64-char hex
    # (chain has at least one entry — possible if a prior test wrote
    # to the conftest-redirected db).
    assert (
        singleton._last_hash == GENESIS_HASH
        or (len(singleton._last_hash) == 64 and all(c in "0123456789abcdef" for c in singleton._last_hash))
    )
